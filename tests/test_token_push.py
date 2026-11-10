"""``aswap token push|receive`` and the per-host push at ``aswap login``."""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import billing_cli
from claude_swap import token_push as tp
from claude_swap.models import Platform
from claude_swap.settings import load_login_settings, parse_hosts, set_setting
from claude_swap.switcher import ClaudeAccountSwitcher


def _switcher() -> ClaudeAccountSwitcher:
    s = ClaudeAccountSwitcher()
    s.platform = Platform.LINUX
    s._setup_directories()
    s._init_sequence_file()
    return s


def _blob(access: str = "at-new", refresh: str = "rt-new") -> str:
    return json.dumps({"claudeAiOauth": {"accessToken": access, "refreshToken": refresh, "expiresAt": 9}})


class TestPaceMint:
    def _run(self, root: Path, now: float, rng_value: float = 75.0):
        slept: list[float] = []
        tp.pace_mint(root, out=lambda *_: None, rng=lambda lo, hi: rng_value,
                     now=lambda: now, sleep=slept.append)
        return slept

    def test_first_mint_does_not_wait(self, tmp_path: Path):
        assert self._run(tmp_path, now=1000.0) == []

    def test_waits_a_random_gap_after_a_recent_mint(self, tmp_path: Path):
        self._run(tmp_path, now=1000.0)
        assert self._run(tmp_path, now=1010.0, rng_value=75.0) == [65.0]

    def test_the_gap_is_drawn_from_60_to_90_seconds(self, tmp_path: Path):
        seen = []
        tp.pace_mint(tmp_path, out=lambda *_: None,
                     rng=lambda lo, hi: seen.append((lo, hi)) or lo,
                     now=lambda: 0.0, sleep=lambda _s: None)
        assert seen == [(60.0, 90.0)]

    def test_no_wait_once_the_gap_has_passed(self, tmp_path: Path):
        self._run(tmp_path, now=1000.0)
        assert self._run(tmp_path, now=1200.0) == []

    def test_remembered_across_runs(self, tmp_path: Path):
        """A second process (the next `aswap login`) still waits."""
        self._run(tmp_path, now=1000.0)
        assert (tmp_path / tp.PACE_FILE).exists()
        assert self._run(tmp_path, now=1030.0, rng_value=60.0) == [30.0]

    def test_a_clock_that_went_back_never_waits_past_the_cap(self, tmp_path: Path):
        self._run(tmp_path, now=100000.0)
        assert self._run(tmp_path, now=1.0) == [90.0]


class TestSend:
    def _ok(self, calls, stdout="Account-3 (a@example.com): stored token\n", rc=0, stderr=""):
        def run(argv, **kw):
            calls.append((argv, kw))
            return subprocess.CompletedProcess(argv, rc, stdout=stdout, stderr=stderr)
        return run

    def test_token_travels_on_stdin_never_argv(self):
        calls = []
        report = tp.send("amin-desktop", "a@example.com", _blob(), "sk-session", run=self._ok(calls))
        argv, kw = calls[0]
        assert argv == ["ssh", "-o", "BatchMode=yes", "amin-desktop", "aswap", "token", "receive"]
        assert not any("at-new" in a or "rt-new" in a or "sk-session" in a for a in argv)
        payload = json.loads(kw["input"])
        assert payload == {"email": "a@example.com", "credentials": _blob(), "sessionKey": "sk-session"}
        assert report == "Account-3 (a@example.com): stored token"

    def test_remote_failure_names_the_host_and_reason(self):
        calls = []
        with pytest.raises(tp.PushError, match="amin-desktop: Error: x@y is not an account here"):
            tp.send("amin-desktop", "x@y", _blob(),
                    run=self._ok(calls, stdout="", rc=1, stderr="Error: x@y is not an account here\n"))

    def test_ssh_timeout_is_a_push_error(self):
        def run(argv, **kw):
            raise subprocess.TimeoutExpired(argv, 1)
        with pytest.raises(tp.PushError, match="ssh failed"):
            tp.send("h", "a@example.com", _blob(), run=run)


class TestReceive:
    def test_stores_the_token_and_login_in_the_matching_slot(self, temp_home: Path):
        sw = _switcher()
        sw.add_account_from_token("sk-ant-oat01-old", email="a@example.com")
        num, email, _org = sw.resolve_account("a@example.com")
        payload = json.dumps({"email": "A@example.com", "credentials": _blob(), "sessionKey": "sk-1"})
        report = tp.receive(sw, io.StringIO(payload))
        assert report == f"Account-{num} (a@example.com): stored token and claude.ai login"
        stored = json.loads(sw._read_account_credentials(num, email))["claudeAiOauth"]
        assert stored["refreshToken"] == "rt-new"
        assert sw._sessions.get((email, ""))["sessionKey"] == "sk-1"

    def test_without_a_session_only_the_token_is_stored(self, temp_home: Path):
        sw = _switcher()
        sw.add_account_from_token("sk-ant-oat01-old", email="a@example.com")
        report = tp.receive(sw, io.StringIO(json.dumps({"email": "a@example.com", "credentials": _blob()})))
        assert report.endswith("stored token")

    def test_an_unknown_account_is_refused(self, temp_home: Path):
        sw = _switcher()
        payload = json.dumps({"email": "nobody@example.com", "credentials": _blob()})
        with pytest.raises(tp.PushError, match="not an account here"):
            tp.receive(sw, io.StringIO(payload))

    @pytest.mark.parametrize("payload", [
        "not json",
        json.dumps({"email": "a@example.com"}),
        json.dumps({"email": "a@example.com", "credentials": "{}"}),
    ])
    def test_malformed_payload_is_refused(self, temp_home: Path, payload: str):
        with pytest.raises(tp.PushError, match="malformed"):
            tp.receive(_switcher(), io.StringIO(payload))

    def test_a_token_without_refresh_is_refused(self, temp_home: Path):
        sw = _switcher()
        sw.add_account_from_token("sk-ant-oat01-old", email="a@example.com")
        payload = json.dumps({"email": "a@example.com", "credentials": _blob(refresh="")})
        with pytest.raises(tp.PushError, match="no refresh token"):
            tp.receive(sw, io.StringIO(payload))


class TestPushToHosts:
    def _wire(self, monkeypatch, events):
        """Record pace / open / mint / close / send in order."""
        monkeypatch.setattr(tp, "pace_mint", lambda root, out=print: events.append("pace"))
        n = iter(range(1, 10))

        def mint(browser, root, out=print, paced=True):
            assert not paced and not browser.closed
            events.append("mint")
            return f"blob-{next(n)}"

        monkeypatch.setattr(tp, "mint_credentials", mint)

        class Browser(_FakeBrowser):
            def close(self):
                events.append("close")
                super().close()

        def opener():
            events.append("open")
            return Browser()

        return opener

    def test_each_host_gets_its_own_mint_with_chrome_open_only_for_it(self, tmp_path: Path, monkeypatch):
        events = []
        opener = self._wire(monkeypatch, events)
        sent = []
        monkeypatch.setattr(tp, "send", lambda host, email, creds, key: sent.append((host, creds)) or "ok")
        landed = tp.push_to_hosts(opener, tmp_path, "a@example.com", "sk", ["h1", "h2"], out=lambda *_: None)
        assert landed == 2
        assert sent == [("h1", "blob-1"), ("h2", "blob-2")]
        # The pacing wait happens with Chrome closed.
        assert events == ["pace", "open", "mint", "close"] * 2

    def test_one_failing_host_does_not_stop_the_rest(self, tmp_path: Path, monkeypatch):
        opener = self._wire(monkeypatch, [])

        def send(host, email, creds, key):
            if host == "down":
                raise tp.PushError("down: ssh failed (TimeoutExpired)")
            return "ok"

        monkeypatch.setattr(tp, "send", send)
        assert tp.push_to_hosts(opener, tmp_path, "a@example.com", None, ["down", "up"], out=lambda *_: None) == 1

    def test_chrome_is_closed_when_the_mint_fails(self, tmp_path: Path, monkeypatch):
        events = []
        opener = self._wire(monkeypatch, events)

        def mint(browser, root, out=print, paced=True):
            raise tp.bs.SignedOut("asked to sign in")

        monkeypatch.setattr(tp, "mint_credentials", mint)
        with pytest.raises(tp.bs.SignedOut):
            tp.push_to_hosts(opener, tmp_path, "a@example.com", None, ["h1"], out=lambda *_: None)
        assert events == ["pace", "open", "close"]


class TestPushCommand:
    IDENTITY = ("a@example.com", "")

    def _setup(self, monkeypatch, *, saved="sk-saved", orgs=None, signed_out_once=False):
        """A slot, its saved login, and fakes for every browser/network step."""
        sw = _switcher()
        sw.add_account_from_token("sk-ant-oat01-old", email="a@example.com")
        if saved:
            sw._sessions.put(self.IDENTITY, saved)
        log = {"opened": [], "signed_in": [], "pushed": [], "browsers": []}

        def fetch_orgs(key):
            if orgs is not None:
                raise orgs
            return [{"uuid": "x"}]

        def open_signed_in(root, email, key, chrome=None):
            log["opened"].append(key)
            log["browsers"].append(_FakeBrowser())
            return log["browsers"][-1]

        def capture_session(identity, root, chrome=None, timeout_s=0, keep_browser=False):
            assert not keep_browser  # the sign-in window closes once the login is saved
            log["signed_in"].append((identity, timeout_s))
            return "sk-fresh"

        def push_to_hosts(open_browser, root, email, key, hosts, out=print):
            open_browser().close()
            if signed_out_once and not log["signed_in"]:
                raise tp.bs.SignedOut("asked to sign in")
            log["pushed"].append((key, hosts))
            return len(hosts)

        monkeypatch.setattr(tp.bs, "fetch_organizations", fetch_orgs)
        monkeypatch.setattr(tp.bs, "open_signed_in", open_signed_in)
        monkeypatch.setattr(tp.bs, "capture_session", capture_session)
        monkeypatch.setattr(tp, "push_to_hosts", push_to_hosts)
        return sw, log

    def _push(self, sw, *extra):
        with patch("claude_swap.switcher.ClaudeAccountSwitcher", return_value=sw), \
                pytest.raises(SystemExit) as done:
            tp.token_command(["push", "a@example.com", "--to", "h1", *extra])
        return done.value.code

    def test_a_working_saved_login_needs_no_sign_in(self, temp_home: Path, monkeypatch):
        sw, log = self._setup(monkeypatch)
        assert self._push(sw) == 0
        assert log["opened"] == ["sk-saved"] and log["signed_in"] == []
        assert log["pushed"] == [("sk-saved", ("h1",))]
        assert all(b.closed for b in log["browsers"])

    def test_an_expired_login_asks_for_a_sign_in_and_saves_it(self, temp_home: Path, monkeypatch):
        sw, log = self._setup(monkeypatch, orgs=tp.bs.SessionExpired("401"))
        assert self._push(sw) == 0
        assert log["opened"] == ["sk-fresh"]
        assert log["signed_in"] == [(self.IDENTITY, 600.0)]
        assert log["pushed"] == [("sk-fresh", ("h1",))]
        assert sw._sessions.get(self.IDENTITY)["sessionKey"] == "sk-fresh"

    def test_no_saved_login_asks_for_a_sign_in(self, temp_home: Path, monkeypatch):
        sw, log = self._setup(monkeypatch, saved=None)
        assert self._push(sw, "--timeout", "120") == 0
        assert log["signed_in"] == [(self.IDENTITY, 120.0)]

    def test_unreachable_check_still_tries_the_saved_login(self, temp_home: Path, monkeypatch):
        sw, log = self._setup(monkeypatch, orgs=tp.bs.SessionBlocked("429"))
        assert self._push(sw) == 0
        assert log["opened"] == ["sk-saved"] and log["signed_in"] == []

    def test_signed_out_at_the_consent_page_signs_in_and_retries(self, temp_home: Path, monkeypatch):
        sw, log = self._setup(monkeypatch, signed_out_once=True)
        assert self._push(sw) == 0
        assert log["opened"] == ["sk-saved", "sk-fresh"]
        assert log["signed_in"] == [(self.IDENTITY, 600.0)]
        assert log["pushed"] == [("sk-fresh", ("h1",))]
        assert all(b.closed for b in log["browsers"])


class TestSignedOutDetection:
    """The mint stops within seconds when the consent page bounces to /login."""

    def _mint(self, on_login_page: bool, timeout_s: float):
        import asyncio

        class Tab:
            async def evaluate(self, js):
                return on_login_page if js == tp.bs._ON_LOGIN_PAGE_JS else False

        class Browser:
            async def get(self, url, new_tab=False):
                return Tab()

        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                tp.bs._nd_mint_oauth_tokens(Browser(), timeout_s=timeout_s, out=lambda *_: None))
        finally:
            loop.close()

    def test_login_page_raises_signed_out(self):
        with pytest.raises(tp.bs.SignedOut):
            self._mint(on_login_page=True, timeout_s=10.0)

    def test_consent_page_keeps_waiting(self):
        with pytest.raises(tp.bs.OAuthError, match="timed out") as e:
            self._mint(on_login_page=False, timeout_s=4.0)
        assert not isinstance(e.value, tp.bs.SignedOut)


class TestStopChrome:
    """Closing waits for Chrome to exit and closes its transport, however slow it quits."""

    def test_slow_quitting_chrome_is_reaped(self):
        import asyncio
        import sys

        import nodriver  # noqa: F401 - warm the slow import so it cannot pad the pause

        class Connection:
            async def send(self, cmd):
                return None

        class Browser:
            connection = Connection()

            def stop(self):
                pass  # nodriver's terminate can fail silently; the wait must not rely on it

        async def run():
            # A "Chrome" that takes 3 s to quit: longer than the old fixed 1.5 s pause.
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-c", "import time; time.sleep(3)",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            browser = Browser()
            browser._process = proc
            await tp.bs._nd_stop(browser)
            return proc

        loop = asyncio.new_event_loop()
        try:
            proc = loop.run_until_complete(run())
        finally:
            loop.close()
        assert proc.returncode is not None
        assert proc._transport.is_closing()


class TestHosts:
    def test_parse_hosts(self):
        assert parse_hosts(" amin-desktop, mac  ,amin-desktop ") == ("amin-desktop", "mac")
        assert parse_hosts(None) == ()

    def test_setting_round_trip_and_override(self, tmp_path: Path):
        set_setting(tmp_path, "login.pushTo", "amin-desktop,mac")
        assert load_login_settings(tmp_path).push_to == "amin-desktop,mac"
        assert tp.configured_hosts(tmp_path) == ("amin-desktop", "mac")
        assert tp.configured_hosts(tmp_path, "other") == ("other",)


class _FakeBrowser:
    closed = False

    def close(self):
        self.closed = True


class TestLoginPushes:
    def _login(self, sw, argv, monkeypatch):
        browser = _FakeBrowser()
        reopened = []
        monkeypatch.setattr(billing_cli.bs, "capture_session", lambda *a, **kw: ("sk-login", browser))

        def open_signed_in(root, email, key, chrome=None):
            # The login window must be gone before Chrome is opened again.
            assert browser.closed
            reopened.append((email, key))
            return _FakeBrowser()

        monkeypatch.setattr(billing_cli.bs, "open_signed_in", open_signed_in)
        monkeypatch.setattr(tp, "pace_mint", lambda root, out=print: None)
        mints = iter([_blob("at-laptop", "rt-laptop"), _blob("at-h1", "rt-h1")])
        monkeypatch.setattr(tp, "mint_credentials", lambda b, root, out=print, paced=True: next(mints))
        sent = []
        monkeypatch.setattr(tp, "send", lambda host, email, creds, key: sent.append((host, creds, key)) or "ok")
        monkeypatch.setattr(sw._billing, "refresh", lambda *a, **kw: None)
        with patch.object(billing_cli, "_switcher", return_value=sw):
            billing_cli.billing_command(argv)
        return browser, sent, reopened

    def test_login_keeps_its_token_and_pushes_a_different_one(self, temp_home: Path, monkeypatch):
        sw = _switcher()
        sw.add_account_from_token("sk-ant-oat01-old", email="a@example.com")
        set_setting(sw.backup_dir, "login.pushTo", "h1")
        browser, sent, reopened = self._login(sw, ["login", "a@example.com"], monkeypatch)
        num, email, _ = sw.resolve_account("a@example.com")
        mine = json.loads(sw._read_account_credentials(num, email))["claudeAiOauth"]
        assert mine["refreshToken"] == "rt-laptop"
        assert [(h, json.loads(c)["claudeAiOauth"]["refreshToken"], k) for h, c, k in sent] == [
            ("h1", "rt-h1", "sk-login"),
        ]
        assert browser.closed
        assert reopened == [("a@example.com", "sk-login")]

    def test_no_push_skips_the_setting(self, temp_home: Path, monkeypatch):
        sw = _switcher()
        sw.add_account_from_token("sk-ant-oat01-old", email="a@example.com")
        set_setting(sw.backup_dir, "login.pushTo", "h1")
        browser, sent, reopened = self._login(sw, ["login", "a@example.com", "--no-push"], monkeypatch)
        assert sent == [] and reopened == []
        assert browser.closed

    def test_login_waits_ten_minutes_for_a_sign_in_code(self):
        seen = []
        with patch.object(billing_cli, "_switcher", return_value=object()), \
             patch.object(billing_cli, "_login", lambda sw, args: seen.append(args.timeout)):
            billing_cli.billing_command(["login", "a@example.com"])
        assert seen == [600.0]
