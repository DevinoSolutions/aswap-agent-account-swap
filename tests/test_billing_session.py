"""Tests for claude.ai sessions: store, HTTP mapping, parser, browser capture."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_swap import billing_session as bs

ID = ("a@example.com", "org-1")


class TestStore:
    def test_round_trip_and_forget(self, tmp_path):
        store = bs.SessionStore(tmp_path)
        store.put(ID, "sk-secret")
        row = store.get(ID)
        assert row["sessionKey"] == "sk-secret"
        assert row["status"] == "ok"
        assert store.forget(ID) is True
        assert store.get(ID) is None
        assert store.forget(ID) is False

    def test_mark_expired(self, tmp_path):
        store = bs.SessionStore(tmp_path)
        store.put(ID, "sk")
        store.mark(ID, "expired")
        assert store.get(ID)["status"] == "expired"
        store.mark(("nobody@x", ""), "expired")  # unknown identity: no-op

    def test_merge_newer_capture_wins(self, tmp_path):
        a = bs.SessionStore(tmp_path / "a")
        a.put(ID, "old")
        theirs = {
            "a@example.com|org-1": {
                "email": "a@example.com",
                "organizationUuid": "org-1",
                "sessionKey": "new",
                "capturedAt": "2999-01-01T00:00:00+00:00",
                "status": "ok",
            },
            "junk": {"no": "key"},
        }
        assert a.merge_rows(theirs) == 1
        assert a.get(ID)["sessionKey"] == "new"
        assert a.merge_rows(theirs) == 0
        older = {"a@example.com|org-1": {**theirs["a@example.com|org-1"], "sessionKey": "older", "capturedAt": "2000"}}
        assert a.merge_rows(older) == 0
        assert a.merge_rows("nope") == 0


class TestParser:
    def test_flat_stripe_shape(self):
        out = bs.parse_subscription_details(
            {
                "status": "active",
                "current_period_end": "2026-10-12T14:28:34Z",
                "cancel_at_period_end": True,
                "billing_interval": "monthly",
            }
        )
        assert out["periodEnd"] == "2026-10-12T14:28:34+00:00"
        # Only claude.ai's own plan_ending_at confirms a cancellation; a
        # Stripe-style flag this endpoint has never sent is not evidence.
        assert out["cancelAtPeriodEnd"] is False
        assert out["status"] == "active"
        assert out["interval"] == "monthly"

    def test_nested_and_epoch_ms(self):
        out = bs.parse_subscription_details(
            {"subscription": {"status": "active", "next_charge_date": 1791808114000}}
        )
        assert out["periodEnd"].startswith("2026-10-12")
        assert out["cancelAtPeriodEnd"] is False

    def test_a_cancel_timestamp_alone_is_not_a_confirmed_cancellation(self):
        out = bs.parse_subscription_details(
            {"status": "active", "next_charge_date": "2026-10-12", "canceled_at": "2026-10-01T00:00:00Z"}
        )
        assert out["cancelAtPeriodEnd"] is False

    def test_unknown_shape_records_keys_only(self):
        out = bs.parse_subscription_details({"foo": 1, "bar": "x"})
        assert out["periodEnd"] is None
        assert out["cancelAtPeriodEnd"] is None
        assert out["keys"] == ["bar", "foo"]

    def test_not_a_dict(self):
        assert bs.parse_subscription_details([1, 2])["keys"] == []


# A real /subscription_details payload (2026-10-18, active monthly Max 5x).
REAL = {
    "plan_ending_before": None, "plan_ending_at": None,
    "next_charge_date": "2026-11-17", "next_charge_at": "2026-11-17T22:41:23Z",
    "payment_method": {"brand": "visa", "country": "CA", "last4": "0000", "type": "card"},
    "status": "active", "billing_interval": "monthly", "has_schedule": False,
    "payment_paused_until": None, "scheduled_downgrade": None, "trial_end_ts": None,
    "currency": "CAD", "pending_purchase": None,
}


class TestRealPayload:
    def test_active_plan_renews_at_next_charge_at(self):
        out = bs.parse_subscription_details(REAL)
        assert out["periodEnd"] == "2026-11-17T22:41:23+00:00"
        assert out["cancelAtPeriodEnd"] is False
        assert out["status"] == "active"
        assert out["interval"] == "monthly"
        assert out["scheduledDowngrade"] is None

    def test_plan_ending_at_means_it_ends_there(self):
        out = bs.parse_subscription_details(
            dict(REAL, next_charge_at=None, next_charge_date=None, plan_ending_at="2026-11-02T10:00:00Z")
        )
        assert out["periodEnd"] == "2026-11-02T10:00:00+00:00"
        assert out["cancelAtPeriodEnd"] is True

    def test_plan_ending_before_alone_is_not_a_cancellation(self):
        out = bs.parse_subscription_details(
            dict(REAL, plan_ending_before="2026-11-02T10:00:00Z")
        )
        assert out["periodEnd"] == "2026-11-17T22:41:23+00:00"
        assert out["cancelAtPeriodEnd"] is False

    def test_scheduled_downgrade_is_kept(self):
        out = bs.parse_subscription_details(dict(REAL, scheduled_downgrade={"plan": "pro"}))
        assert out["scheduledDowngrade"] == {"plan": "pro"}
        assert out["periodEnd"] == "2026-11-17T22:41:23+00:00"


def _serve(monkeypatch, status: int, body: str, seen: dict | None = None):
    def fake(url, headers, timeout):
        if seen is not None:
            seen.update(url=url, headers=headers)
        return status, body

    monkeypatch.setattr(bs, "_http_get", fake)


class TestHttp:
    def test_401_is_expired(self, monkeypatch):
        _serve(monkeypatch, 401, '{"error":"nope"}')
        with pytest.raises(bs.SessionExpired):
            bs.fetch_organizations("sk")

    def test_cloudflare_is_blocked_not_expired(self, monkeypatch):
        _serve(monkeypatch, 403, "<html>Just a moment...</html>")
        with pytest.raises(bs.SessionBlocked):
            bs.fetch_organizations("sk")

    def test_html_on_200_is_expired(self, monkeypatch):
        _serve(monkeypatch, 200, "<html>login</html>")
        with pytest.raises(bs.SessionExpired):
            bs.fetch_organizations("sk")

    def test_404_subscription_is_none(self, monkeypatch):
        _serve(monkeypatch, 404, "{}")
        assert bs.fetch_subscription_details("sk", "org")["status"] == "none"

    def test_sends_the_cookie(self, monkeypatch):
        seen = {}
        _serve(monkeypatch, 200, '[{"uuid": "org-1"}]', seen)
        assert bs.fetch_organizations("sk-1") == [{"uuid": "org-1"}]
        assert seen["headers"]["Cookie"] == "sessionKey=sk-1"
        assert seen["url"] == "https://claude.ai/api/organizations"


class _Done:
    def __init__(self, stdout: bytes, returncode: int = 0):
        self.stdout, self.returncode = stdout, returncode


class TestCurl:
    def test_key_goes_through_stdin_not_argv(self, monkeypatch):
        seen = {}

        def run(cmd, input, **kw):
            seen.update(cmd=cmd, config=input.decode())
            return _Done(b'{"ok": 1}\n__aswap_status__:200')

        monkeypatch.setattr(bs, "_curl", lambda: "curl")
        monkeypatch.setattr(bs.subprocess, "run", run)
        status, body = bs._http_get("https://claude.ai/api/x", {"Cookie": 'sessionKey=sk-"q"'}, 5)
        assert (status, body) == (200, '{"ok": 1}')
        assert not any("sk-" in part for part in seen["cmd"])
        assert 'header = "Cookie: sessionKey=sk-\\"q\\""' in seen["config"]
        assert 'url = "https://claude.ai/api/x"' in seen["config"]

    def test_no_status_is_unreachable(self, monkeypatch):
        monkeypatch.setattr(bs, "_curl", lambda: "curl")
        monkeypatch.setattr(bs.subprocess, "run", lambda *a, **k: _Done(b"\n__aswap_status__:000", 7))
        with pytest.raises(bs.BillingSessionError):
            bs._http_get("https://claude.ai/api/x", {}, 5)


class TestChrome:
    def test_devtools_port(self, tmp_path):
        assert bs.read_devtools_port(tmp_path) is None
        (tmp_path / "DevToolsActivePort").write_text("51234\n/devtools/browser/abc\n")
        assert bs.read_devtools_port(tmp_path) == (51234, "/devtools/browser/abc")
        (tmp_path / "DevToolsActivePort").write_text("garbage")
        assert bs.read_devtools_port(tmp_path) is None

    def test_find_chrome_explicit_and_env(self, tmp_path, monkeypatch):
        exe = tmp_path / "chrome.exe"
        exe.write_text("")
        assert bs.find_chrome(str(exe)) == exe
        monkeypatch.setenv("ASWAP_CHROME", str(exe))
        assert bs.find_chrome() == exe
        with pytest.raises(bs.CaptureError):
            bs.find_chrome(str(tmp_path / "missing.exe"))

    def test_profile_dir_is_slugged(self, tmp_path):
        assert bs.profile_dir(tmp_path, "A.B+c@X.com").name == "a.b_c_x.com"


class FakeBrowser:
    def __init__(self, cookie_values):
        self.values = list(cookie_values)
        self.closed = False

    def cookies(self):
        value = self.values.pop(0) if len(self.values) > 1 else self.values[0]
        return [] if value is None else [{"name": "sessionKey", "domain": ".claude.ai", "value": value}]

    def close(self):
        self.closed = True


class TestCapture:
    def _run(self, tmp_path, monkeypatch, browser, orgs, emails=None, timeout=60):
        exe = tmp_path / "chrome.exe"
        exe.write_text("")
        launched = {}

        def launcher(argv, **kw):
            launched["argv"] = argv
            profile = bs.profile_dir(tmp_path, ID[0])
            (profile / "DevToolsActivePort").write_text("9222\n/devtools/browser/x\n")

        t = [0.0]
        messages = []
        key = bs.capture_session(
            ID,
            tmp_path,
            chrome=str(exe),
            timeout_s=timeout,
            out=messages.append,
            launcher=launcher,
            connect=lambda url: browser,
            orgs_fetch=lambda k: orgs[k],
            email_fetch=lambda k: (emails or {}).get(k),
            clock=lambda: t[0],
            sleep=lambda s: t.__setitem__(0, t[0] + s),
        )
        return key, launched, messages

    def test_waits_for_login_then_returns_the_key(self, tmp_path, monkeypatch):
        browser = FakeBrowser([None, None, "sk-right"])
        key, launched, _ = self._run(tmp_path, monkeypatch, browser, {"sk-right": [{"uuid": "org-1"}]})
        assert key == "sk-right"
        assert browser.closed
        assert any(a.startswith("--user-data-dir=") for a in launched["argv"])
        assert "--remote-debugging-port=0" in launched["argv"]

    def test_rejects_the_wrong_account_and_keeps_waiting(self, tmp_path, monkeypatch):
        browser = FakeBrowser(["sk-wrong", "sk-wrong", "sk-right"])
        orgs = {"sk-wrong": [{"uuid": "org-9", "name": "Other"}], "sk-right": [{"uuid": "org-1"}]}
        key, _, messages = self._run(tmp_path, monkeypatch, browser, orgs)
        assert key == "sk-right"
        assert sum("not a@example.com" in m for m in messages) == 1

    def test_same_org_different_email_is_rejected(self, tmp_path, monkeypatch):
        browser = FakeBrowser(["sk-teammate", "sk-me"])
        orgs = {"sk-teammate": [{"uuid": "org-1"}], "sk-me": [{"uuid": "org-1"}]}
        emails = {"sk-teammate": "b@example.com", "sk-me": "A@example.com"}
        key, _, _ = self._run(tmp_path, monkeypatch, browser, orgs, emails)
        assert key == "sk-me"

    def test_timeout_closes_the_browser(self, tmp_path, monkeypatch):
        browser = FakeBrowser([None])
        with pytest.raises(bs.CaptureError, match="timed out"):
            self._run(tmp_path, monkeypatch, browser, {}, timeout=10)
        assert browser.closed


# --- switcher integration ------------------------------------------------------

from claude_swap.switcher import ClaudeAccountSwitcher  # noqa: E402
from claude_swap.usage_store import FetchRecord  # noqa: E402


@pytest.fixture
def switcher(temp_home: Path) -> ClaudeAccountSwitcher:
    sw = ClaudeAccountSwitcher()
    sw._setup_directories()
    sw._write_json(
        sw.sequence_file,
        {
            "activeAccountNumber": 1,
            "lastUpdated": "2024-01-01T00:00:00Z",
            "sequence": [1, 2],
            "accounts": {
                "1": {"email": "one@example.com", "organizationUuid": "org-1"},
                "2": {"email": "two@example.com", "organizationUuid": "org-2"},
            },
        },
    )
    creds = {"claudeAiOauth": {"accessToken": "a", "refreshToken": "r", "rateLimitTier": "default_claude_max_5x"}}
    sw._write_account_credentials("2", "two@example.com", json.dumps(creds))
    sw._write_account_config("2", "two@example.com", json.dumps({"oauthAccount": {"emailAddress": "two@example.com"}}))
    return sw


class TestSwitcherHooks:
    def _measure(self, sw, resets_at="2026-10-15T10:00:00+00:00"):
        identity = ("two@example.com", "org-2")
        usage = {"seven_day": {"utilization": 10.0, "resets_at": resets_at}}
        sw._usage_store.record({"2": FetchRecord(usage=usage)}, {"2": identity})

    def test_usage_poll_records_the_anchor_with_the_tier(self, switcher):
        self._measure(switcher)
        row = switcher._anchors.get(("two@example.com", "org-2"))
        assert row["tier"] == "max_5x"  # from the stored credentials
        assert row["tiers"]["max_5x"]["anchor"] == {"weekday": 3, "hhmm": "10:00"}
        assert row["lastSlot"] == 2

    def test_remove_keeps_the_anchor_and_drops_the_session(self, switcher, monkeypatch):
        self._measure(switcher)
        identity = ("two@example.com", "org-2")
        switcher._sessions.put(identity, "sk")
        monkeypatch.setattr("builtins.input", lambda *a: "y")
        switcher.remove_account("2")
        assert switcher._sessions.get(identity) is None
        row = switcher._anchors.get(identity)
        assert row["removedAt"] is not None
        assert row["tiers"]["max_5x"]["anchor"] == {"weekday": 3, "hhmm": "10:00"}
