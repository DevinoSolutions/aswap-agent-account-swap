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
    def test_each_host_gets_its_own_mint(self, tmp_path: Path, monkeypatch):
        mints = iter(["blob-1", "blob-2"])
        monkeypatch.setattr(tp, "mint_credentials", lambda browser, root, out=print: next(mints))
        sent = []
        monkeypatch.setattr(tp, "send", lambda host, email, creds, key: sent.append((host, creds)) or "ok")
        landed = tp.push_to_hosts(object(), tmp_path, "a@example.com", "sk", ["h1", "h2"], out=lambda *_: None)
        assert landed == 2
        assert sent == [("h1", "blob-1"), ("h2", "blob-2")]

    def test_one_failing_host_does_not_stop_the_rest(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(tp, "mint_credentials", lambda browser, root, out=print: "blob")

        def send(host, email, creds, key):
            if host == "down":
                raise tp.PushError("down: ssh failed (TimeoutExpired)")
            return "ok"

        monkeypatch.setattr(tp, "send", send)
        assert tp.push_to_hosts(object(), tmp_path, "a@example.com", None, ["down", "up"], out=lambda *_: None) == 1


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
        monkeypatch.setattr(billing_cli.bs, "capture_session", lambda *a, **kw: ("sk-login", browser))
        mints = iter([_blob("at-laptop", "rt-laptop"), _blob("at-h1", "rt-h1")])
        monkeypatch.setattr(tp, "mint_credentials", lambda b, root, out=print: next(mints))
        sent = []
        monkeypatch.setattr(tp, "send", lambda host, email, creds, key: sent.append((host, creds, key)) or "ok")
        monkeypatch.setattr(sw._billing, "refresh", lambda *a, **kw: None)
        with patch.object(billing_cli, "_switcher", return_value=sw):
            billing_cli.billing_command(argv)
        return browser, sent

    def test_login_keeps_its_token_and_pushes_a_different_one(self, temp_home: Path, monkeypatch):
        sw = _switcher()
        sw.add_account_from_token("sk-ant-oat01-old", email="a@example.com")
        set_setting(sw.backup_dir, "login.pushTo", "h1")
        browser, sent = self._login(sw, ["login", "a@example.com"], monkeypatch)
        num, email, _ = sw.resolve_account("a@example.com")
        mine = json.loads(sw._read_account_credentials(num, email))["claudeAiOauth"]
        assert mine["refreshToken"] == "rt-laptop"
        assert [(h, json.loads(c)["claudeAiOauth"]["refreshToken"], k) for h, c, k in sent] == [
            ("h1", "rt-h1", "sk-login"),
        ]
        assert browser.closed

    def test_no_push_skips_the_setting(self, temp_home: Path, monkeypatch):
        sw = _switcher()
        sw.add_account_from_token("sk-ant-oat01-old", email="a@example.com")
        set_setting(sw.backup_dir, "login.pushTo", "h1")
        _browser, sent = self._login(sw, ["login", "a@example.com", "--no-push"], monkeypatch)
        assert sent == []

    def test_login_waits_ten_minutes_for_a_sign_in_code(self):
        seen = []
        with patch.object(billing_cli, "_switcher", return_value=object()), \
             patch.object(billing_cli, "_login", lambda sw, args: seen.append(args.timeout)):
            billing_cli.billing_command(["login", "a@example.com"])
        assert seen == [600.0]
