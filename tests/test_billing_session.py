"""Tests for claude.ai sessions: store, HTTP mapping, parser, browser capture."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_swap import billing_session as bs

# Captured at import, before conftest's autouse stub replaces it per test.
_REAL_FETCH_RESETS = bs.fetch_limit_resets

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

    def test_an_expired_mark_is_retried_after_a_while(self, tmp_path):
        store = bs.SessionStore(tmp_path)
        store.put(ID, "sk")
        store.mark(ID, "expired", now=1000.0)
        row = store.get(ID)
        assert row["retryAt"] == 1000.0 + bs.EXPIRED_RETRY_S
        assert not bs.session_usable(row, 1000.0 + bs.EXPIRED_RETRY_S - 1)
        assert bs.session_usable(row, 1000.0 + bs.EXPIRED_RETRY_S)
        # Failing again pushes the next try out; working again clears it.
        store.mark(ID, "expired", now=5000.0)
        assert store.get(ID)["retryAt"] == 5000.0 + bs.EXPIRED_RETRY_S
        store.mark(ID, "ok")
        assert "retryAt" not in store.get(ID) and bs.session_usable(store.get(ID), 0.0)

    def test_a_mark_from_before_retries_existed_is_due_at_once(self):
        assert bs.session_usable({"sessionKey": "sk", "status": "expired"}, 0.0)
        assert bs.session_usable({"sessionKey": "sk", "status": "ok"}, 0.0)
        assert not bs.session_usable({"status": "ok"}, 0.0)
        assert not bs.session_usable(None, 0.0)

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

    def test_permission_error_with_a_working_session_is_no_plan(self, monkeypatch):
        # Measured 2026-11-10 on a lapsed account: organizations 200,
        # subscription_details 403 permission_error.
        def fake(url, headers, timeout):
            if url.endswith("/subscription_details"):
                return 403, '{"type":"error","error":{"type":"permission_error","message":"Missing permissions."}}'
            return 200, '[{"uuid": "org"}]'

        monkeypatch.setattr(bs, "_http_get", fake)
        assert bs.fetch_subscription_details("sk", "org")["status"] == "none"

    def test_permission_error_with_a_dead_session_is_expired(self, monkeypatch):
        _serve(monkeypatch, 403, '{"type":"error","error":{"type":"permission_error"}}')
        with pytest.raises(bs.SessionExpired):
            bs.fetch_subscription_details("sk", "org")

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


# --- login email prefill -------------------------------------------------------


class FakeElement:
    def __init__(self, log, name):
        self._log, self._name = log, name

    async def focus(self):
        self._log.append(("focus", self._name))

    async def click(self):
        self._log.append(("click", self._name))


class FakeTab:
    def __init__(self, has_form=True):
        self.log: list = []
        self._has_form = has_form

    async def select(self, selector, timeout=10):
        if not self._has_form:
            raise TimeoutError(selector)
        name = "email" if selector == bs._EMAIL_INPUT else "continue"
        return FakeElement(self.log, name)

    async def send(self, command):
        self.log.append(("send", command))


class TestPrefillEmail:
    def _run(self, tab, email="a@example.com"):
        import asyncio

        messages: list[str] = []
        ok = asyncio.run(bs._nd_prefill_email(tab, email, messages.append, wait_s=0))
        return ok, messages

    def test_types_the_email_then_presses_continue(self):
        tab = FakeTab()
        ok, messages = self._run(tab)
        assert ok
        steps = [s[0] if s[0] == "send" else s for s in tab.log]
        assert steps == [("focus", "email"), "send", ("click", "continue")]
        assert any("a@example.com" in m for m in messages)

    def test_signed_in_profile_without_a_form_is_left_alone(self):
        tab = FakeTab(has_form=False)
        ok, messages = self._run(tab)
        assert not ok
        assert tab.log == [] and messages == []

    def test_capture_and_mint_share_one_event_loop(self):
        # A nodriver browser is bound to the loop that started it: the mint
        # after capture used to fail with "Event loop is closed".
        import asyncio

        async def loop_id():
            return id(asyncio.get_running_loop())

        assert bs._nd_run(loop_id()) == bs._nd_run(loop_id())

    def test_no_email_starts_no_prefill(self):
        assert bs._start_prefill(FakeTab(), None, print) is None
        assert bs._start_prefill(FakeTab(), "7", print) is None


class CheckingTab(FakeTab):
    """A login page whose browser check fails ``failures`` times."""

    def __init__(self, failures):
        super().__init__()
        self._failures = failures

    async def evaluate(self, expression, return_by_value=False):
        assert expression == bs._AFTER_CONTINUE_JS
        clicks = sum(1 for step in self.log if step == ("click", "continue"))
        return "error" if clicks <= self._failures else "moved"

    async def reload(self):
        self.log.append(("reload",))


class TestBrowserCheckRetry:
    """Owner ask 2026-11-16: "Couldn't verify your browser. Try again." is
    retried by itself, up to 5 times."""

    def _run(self, tab):
        import asyncio

        messages: list[str] = []
        ok = asyncio.run(bs._nd_prefill_email(
            tab, "a@example.com", messages.append, wait_s=0, check_wait_s=0))
        return ok, messages

    def test_a_failed_check_reloads_and_presses_continue_again(self):
        tab = CheckingTab(failures=2)
        ok, messages = self._run(tab)
        assert ok
        assert tab.log.count(("click", "continue")) == 3
        assert tab.log.count(("reload",)) == 2
        assert sum("trying again" in m for m in messages) == 2
        assert not any("still couldn't" in m for m in messages)

    def test_gives_up_after_five_retries(self):
        tab = CheckingTab(failures=99)
        ok, messages = self._run(tab)
        assert ok  # the person can still press Continue themselves
        assert tab.log.count(("click", "continue")) == bs.BROWSER_CHECK_RETRIES + 1 == 6
        assert tab.log.count(("reload",)) == 5
        assert any("after 5 retries" in m for m in messages)

    def test_no_retry_when_the_check_passes(self):
        tab = CheckingTab(failures=0)
        ok, messages = self._run(tab)
        assert ok and ("reload",) not in tab.log
        assert tab.log.count(("click", "continue")) == 1


class TestOAuthMint:
    def test_authorize_url_is_the_subscription_login(self):
        from urllib.parse import parse_qs

        q = parse_qs(bs._authorize_query("http://localhost:1/callback", "ch", "st"))
        assert bs._OAUTH_AUTHORIZE == "https://claude.com/cai/oauth/authorize"
        assert q["code"] == ["true"] and q["state"] == ["st"]
        assert "user:inference" in q["scope"][0].split()
        assert "user:sessions:claude_code" in q["scope"][0].split()

    def test_credential_carries_the_plan_from_the_profile(self):
        token = {"access_token": "at", "refresh_token": "rt", "expires_in": 60, "scope": "user:inference"}
        profile = {"organization": {"organization_type": "claude_max", "rate_limit_tier": "default_claude_max_20x"}}
        oauth = json.loads(bs.build_credential_blob(token, profile))["claudeAiOauth"]
        assert oauth["subscriptionType"] == "max"
        assert oauth["rateLimitTier"] == "default_claude_max_20x"

    def test_credential_without_a_profile_still_builds(self):
        token = {"access_token": "at", "refresh_token": "rt", "expires_in": 60}
        oauth = json.loads(bs.build_credential_blob(token))["claudeAiOauth"]
        assert oauth["accessToken"] == "at" and "subscriptionType" not in oauth


# --- switcher integration ------------------------------------------------------

# Shape measured 2026-11-26 on a Max 20x account (claude.ai Settings > Usage
# showed "Fable reset, expires Mar 9" and "Weekly reset, expires Dec 10").
_CEDAR_USAGE = {
    "limits": [
        {"kind": "weekly_all", "scope": None, "percent": 100},
        {"kind": "weekly_scoped", "scope": {"model": {"id": None, "display_name": "Fable"}}, "percent": 22},
    ],
    "cedar_ember": {
        "eligible": True,
        "grants": [
            {
                "id": "launch-2610-fable",
                "label": "New model launch: one Fable reset for Max, Team Premium and Enterprise Premium",
                "resets_total": 1, "resets_left": 1,
                "ends_at": "2027-03-09T07:59:00+00:00",
                "clears": ["seven_day_overage_included"],
                "usable_now": True, "type": "full", "gives_back_weekly": True,
            },
            {
                "id": "full-reset-2611",
                "label": "One full limit reset (November 2026)",
                "resets_total": 1, "resets_left": 1,
                "ends_at": "2026-12-10T22:00:00+00:00",
                "clears": ["seven_day", "seven_day_overage_included"],
                "usable_now": True, "type": "weekly", "gives_back_weekly": False,
            },
            {
                "id": "spent", "label": "One full limit reset (October 2026)",
                "resets_total": 1, "resets_left": 0,
                "ends_at": "2026-11-10T22:00:00+00:00", "clears": ["seven_day"],
            },
        ],
    },
}


class TestLimitResets:
    def test_names_and_order_match_claude_ai(self):
        out = bs.parse_limit_resets(_CEDAR_USAGE)
        assert [(r["name"], r["left"], r["expiresAt"]) for r in out] == [
            ("Weekly", 1, "2026-12-10T22:00:00+00:00"),
            ("Fable", 1, "2027-03-09T07:59:00+00:00"),
        ]
        assert all(r["usableNow"] for r in out)

    def test_spent_grants_are_left_out(self):
        assert "spent" not in {r["id"] for r in bs.parse_limit_resets(_CEDAR_USAGE)}

    @pytest.mark.parametrize("payload", [None, [], {}, {"cedar_ember": None},
                                         {"cedar_ember": {"eligible": False, "grants": []}}])
    def test_no_block_means_no_resets(self, payload):
        assert bs.parse_limit_resets(payload) == []

    def test_unknown_model_reset_without_a_scoped_limit(self):
        payload = {"cedar_ember": {"grants": [
            {"id": "x", "label": "A reset", "resets_left": 2, "resets_total": 3,
             "clears": ["seven_day_overage_included"], "usable_now": False},
        ]}}
        [r] = bs.parse_limit_resets(payload)
        assert (r["name"], r["left"], r["total"], r["expiresAt"], r["usableNow"]) == (
            "Model", 2, 3, None, False)

    def test_fetch_asks_for_the_resets(self, monkeypatch):
        seen = []

        def fake_request(path, key, timeout=40.0):
            seen.append(path)
            return _CEDAR_USAGE

        monkeypatch.setattr(bs, "_request", fake_request)
        out = _REAL_FETCH_RESETS("sk", "org-1")
        assert seen == ["/api/organizations/org-1/usage?cedar_ember=1&skip_spend=1"]
        assert len(out) == 2


class TestUseLimitReset:
    def test_grants_list_spent_ones_and_the_cooldown(self):
        payload = json.loads(json.dumps(_CEDAR_USAGE))
        payload["cedar_ember"]["cooldown_until"] = "2026-11-26T20:59:49.095636+00:00"
        payload["cedar_ember"]["grants"][0].update(usable_now=False, unavailable_reason="cooldown")
        out = {g["id"]: g for g in bs.reset_grants(payload)}
        assert out["spent"]["left"] == 0 and out["spent"]["name"] == "Weekly"
        assert out["launch-2610-fable"]["reason"] == "cooldown"
        assert not out["launch-2610-fable"]["usableNow"] and out["full-reset-2611"]["usableNow"]
        assert out["full-reset-2611"]["cooldownUntil"].startswith("2026-11-26T20:59:49")
        assert bs.reset_grants({}) == []

    def test_usage_percents(self):
        payload = dict(_CEDAR_USAGE, limits=[{"kind": "session", "percent": 3}, *_CEDAR_USAGE["limits"]])
        assert bs.usage_percents(payload) == {"5h": 3.0, "7d": 100.0, "Fable": 22.0}
        assert bs.usage_percents(None) == {}

    def test_use_posts_what_the_page_sends(self, monkeypatch):
        calls = []

        def fake_http(url, headers, timeout, body=None):
            calls.append((url, headers, body))
            return 200, ""

        monkeypatch.setattr(bs, "_http_get", fake_http)
        assert bs.use_limit_reset("sk", "org-1", "full-reset-2611") == {}
        [(url, headers, body)] = calls
        assert url.endswith("/api/organizations/org-1/reset_rate_limits")
        sent = json.loads(body)
        assert sent["program"] == "cedar_ember" and sent["grant_id"] == "full-reset-2611"
        assert len(sent["request_id"]) == 36
        assert headers["Content-Type"] == "application/json"
        assert headers["Cookie"] == "sessionKey=sk"

    def test_get_sends_no_body(self, monkeypatch):
        calls = []
        monkeypatch.setattr(bs, "_http_get", lambda url, h, t, *body: calls.append(body) or (200, "{}"))
        bs._request("/api/x", "sk")
        assert calls == [()]


class _FakeSessions:
    def __init__(self, key):
        self.key = key

    def get(self, identity):
        return {"sessionKey": self.key} if self.key else None


class _FakeBilling:
    def __init__(self):
        self.stored = []

    def store_limit_resets(self, identity, resets):
        self.stored.append((identity, resets))


class _FakeSwitcher:
    def __init__(self, key="sk"):
        self._sessions = _FakeSessions(key)
        self._billing = _FakeBilling()

    def resolve_account(self, ident):
        return "8", "hello@example.com", "org-1"


class TestResetCommand:
    @pytest.fixture
    def cli(self, monkeypatch):
        from claude_swap import billing_cli

        sw = _FakeSwitcher()
        monkeypatch.setattr(billing_cli, "_switcher", lambda debug: sw)
        after = json.loads(json.dumps(_CEDAR_USAGE))
        after["limits"][0]["percent"] = 0
        after["cedar_ember"]["grants"][1]["resets_left"] = 0
        used = []
        monkeypatch.setattr(bs, "read_usage", lambda key, org: after if used else _CEDAR_USAGE)
        monkeypatch.setattr(bs, "use_limit_reset", lambda key, org, gid: used.append((key, org, gid)) or {})
        return billing_cli, sw, used

    def test_uses_the_weekly_reset(self, cli, capsys):
        billing_cli, sw, used = cli
        billing_cli.reset_command(["hello@example.com", "7d"])
        assert used == [("sk", "org-1", "full-reset-2611")]
        out = capsys.readouterr().out
        assert "used its 7d reset" in out
        assert "before: 7d 100% · Fable 22%" in out and "after:  7d 0% · Fable 22%" in out
        assert "resets left: Fable ×1" in out
        [(identity, resets)] = sw._billing.stored
        assert identity == ("hello@example.com", "org-1") and [r["name"] for r in resets] == ["Fable"]

    def test_model_kind_is_case_insensitive(self, cli):
        billing_cli, _sw, used = cli
        billing_cli.reset_command(["8", "fable"])
        assert used == [("sk", "org-1", "launch-2610-fable")]

    def test_dry_run_and_listing_use_nothing(self, cli, capsys):
        billing_cli, _sw, used = cli
        billing_cli.reset_command(["8", "7d", "--dry-run"])
        assert "would use its 7d reset" in capsys.readouterr().out
        billing_cli.reset_command(["8"])
        out = capsys.readouterr().out
        assert "7d ×1" in out and "Fable ×1" in out and "use one: aswap reset 8 7d|fable" in out
        assert "Nov 10" not in out  # a spent reset that expired is not listed
        assert used == []

    def test_spent_or_cooling_down_is_refused(self, monkeypatch, capsys):
        from claude_swap import billing_cli

        monkeypatch.setattr(billing_cli, "_switcher", lambda debug: _FakeSwitcher())
        cooling = json.loads(json.dumps(_CEDAR_USAGE))
        cooling["cedar_ember"]["cooldown_until"] = "2026-11-26T20:59:49+00:00"
        for g in cooling["cedar_ember"]["grants"]:
            g.update(usable_now=False, unavailable_reason="cooldown")
        monkeypatch.setattr(bs, "read_usage", lambda key, org: cooling)
        monkeypatch.setattr(bs, "use_limit_reset", lambda *a: pytest.fail("must not spend"))
        with pytest.raises(SystemExit):
            billing_cli.reset_command(["8", "7d"])
        assert "cooldown until" in capsys.readouterr().err
        with pytest.raises(SystemExit):
            billing_cli.reset_command(["8", "opus"])
        assert "no opus reset left (has: 7d ×1, Fable ×1)" in capsys.readouterr().err

    def test_no_session(self, monkeypatch, capsys):
        from claude_swap import billing_cli

        monkeypatch.setattr(billing_cli, "_switcher", lambda debug: _FakeSwitcher(key=None))
        with pytest.raises(SystemExit):
            billing_cli.reset_command(["8", "7d"])
        assert "no claude.ai session" in capsys.readouterr().err


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
