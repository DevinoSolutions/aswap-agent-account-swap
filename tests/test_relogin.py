"""Hands-free re-login: token-file mailboxes, strict sign-in walls, relogin-dead."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from claude_swap import billing_session as bs
from claude_swap import inbox as ib
from claude_swap import inbox_oauth as io
from claude_swap import relogin

# --- mailboxes from an existing token file ----------------------------------


def _token_file(tmp_path, **over):
    body = {"refresh_token": "rt", "client_id": "cid", "client_secret": "sec",
            "token_uri": io.GOOGLE_TOKEN, "scopes": [io.GOOGLE_SCOPE]}
    body.update(over)
    path = tmp_path / "token.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


@pytest.fixture
def gmail(monkeypatch):
    calls = []

    def fake(url, *, data=None, token=None, headers=None, timeout=30.0):
        calls.append((url, data))
        if url == io.GOOGLE_TOKEN:
            return {"access_token": "at", "expires_in": 3600}
        if url == f"{io.GMAIL}/profile":
            return {"emailAddress": "hub@devino.ca"}
        if url.startswith(f"{io.GMAIL}/messages?"):
            return {"messages": []}
        raise AssertionError(url)

    monkeypatch.setattr(io, "_request", fake)
    return calls


class TestTokenFile:
    def test_the_row_keeps_the_path_not_the_token(self, gmail, tmp_path):
        path = _token_file(tmp_path)
        row = io.import_token_file(path)
        assert row == {"kind": "google", "address": "hub@devino.ca", "tokenFile": str(path.resolve())}
        assert "rt" not in json.dumps(row) and "sec" not in json.dumps(row)

    def test_a_token_without_a_mail_scope_is_refused(self, gmail, tmp_path):
        path = _token_file(tmp_path, scopes=["https://www.googleapis.com/auth/drive"])
        with pytest.raises(ib.InboxError, match="no Gmail read scope"):
            io.import_token_file(path)

    def test_the_hint_must_match_the_mailbox(self, gmail, tmp_path):
        with pytest.raises(ib.InboxError, match="not other@devino.ca"):
            io.import_token_file(_token_file(tmp_path), hint="other@devino.ca")

    def test_not_a_token_file(self, gmail, tmp_path):
        bad = tmp_path / "x.json"
        bad.write_text('{"installed": {}}', encoding="utf-8")
        with pytest.raises(ib.InboxError, match="not an authorized-user token file"):
            io.import_token_file(bad)

    def test_the_source_reads_the_file_and_writes_nothing_back(self, gmail, tmp_path):
        path = _token_file(tmp_path)
        row = io.import_token_file(path)
        ib.add_source(tmp_path, row)
        before = (tmp_path / ib.FILE_NAME).read_text(encoding="utf-8")
        source = io.open_api_source(tmp_path, row)
        assert source.snapshot() == set()
        refreshes = [d for url, d in gmail if url == io.GOOGLE_TOKEN]
        assert refreshes[-1]["refresh_token"] == "rt"
        assert (tmp_path / ib.FILE_NAME).read_text(encoding="utf-8") == before

    def test_connect_with_token_file_needs_no_browser(self, gmail, tmp_path):
        args = SimpleNamespace(kind="google", token_file=str(_token_file(tmp_path)), hint=None,
                               no_browser=False, client_file=None, client_id=None, client_secret=None)

        def no_browser(url):
            raise AssertionError("no browser for a token file")

        row = io.connect(tmp_path, args, out=lambda m: None, open_browser=no_browser)
        assert row["tokenFile"]

    def test_no_browser_prints_the_url_instead(self, tmp_path, monkeypatch):
        seen = {}

        def fake_authorize(build_url, *, host, out, open_browser, timeout_s=300.0):
            seen["opened"] = open_browser("https://example.test/auth")
            raise ib.InboxError("stop here")

        monkeypatch.setattr(io, "_authorize", fake_authorize)
        args = SimpleNamespace(kind="google", token_file=None, hint=None, no_browser=True,
                               client_file=None, client_id="cid", client_secret="sec")
        with pytest.raises(ib.InboxError, match="stop here"):
            io.connect(tmp_path, args, out=lambda m: None, open_browser=lambda u: "OPENED")
        assert seen["opened"] is None


# --- strict (hands-free) sign-in --------------------------------------------


class _Element:
    async def focus(self):
        pass

    async def click(self):
        pass

    async def clear_input(self):
        pass


class _Tab:
    def __init__(self, page_state="moved", has_form=True):
        self.page_state = page_state
        self.has_form = has_form
        self.opened: list[str] = []
        self.reloads = 0

    async def select(self, selector, timeout=10):
        if selector == bs._EMAIL_INPUT and not self.has_form:
            raise TimeoutError
        return _Element()

    async def send(self, command):
        pass

    async def evaluate(self, js, return_by_value=False):
        if js == bs._AFTER_CONTINUE_JS:
            return self.page_state
        return "/login (Claude)"

    async def reload(self):
        self.reloads += 1

    async def get(self, url):
        self.opened.append(url)


class _Source:
    def __init__(self, links):
        self.address = "hub@devino.ca"
        self.links = list(links)
        self.closed = False

    def snapshot(self):
        return set()

    def find_link(self, email, snap):
        return self.links.pop(0) if self.links else None

    def close(self):
        self.closed = True


def _strict(monkeypatch, tmp_path, tab, sources):
    monkeypatch.setattr(ib, "open_sources", lambda root, out: sources)
    monkeypatch.setattr(bs, "AUTO_LINK_WAIT_S", 0.01)
    messages: list[str] = []
    result = asyncio.run(bs._nd_prefill_email(
        tab, "a@example.com", messages.append, wait_s=0, inbox_root=tmp_path,
        check_wait_s=0, strict=True,
    ))
    return result, messages


class TestStrictPrefill:
    def test_opens_the_link(self, monkeypatch, tmp_path):
        tab = _Tab()
        ok, _ = _strict(monkeypatch, tmp_path, tab, [_Source(["LINK"])])
        assert ok and tab.opened == ["LINK"]

    def test_no_mailbox_is_a_wall_before_any_mail_is_requested(self, monkeypatch, tmp_path):
        tab = _Tab()
        with pytest.raises(bs.LoginWall) as e:
            _strict(monkeypatch, tmp_path, tab, [])
        assert e.value.kind == bs.WALL_NO_INBOX

    def test_no_mail_in_time_is_a_wall(self, monkeypatch, tmp_path):
        source = _Source([])
        with pytest.raises(bs.LoginWall) as e:
            _strict(monkeypatch, tmp_path, _Tab(), [source])
        assert e.value.kind == bs.WALL_NO_EMAIL
        assert "forwarded" in str(e.value) and source.closed

    def test_a_bot_check_that_keeps_failing_is_a_wall_after_few_retries(self, monkeypatch, tmp_path):
        tab = _Tab(page_state="error")
        with pytest.raises(bs.LoginWall) as e:
            _strict(monkeypatch, tmp_path, tab, [_Source(["LINK"])])
        assert e.value.kind == bs.WALL_BROWSER_CHECK
        assert tab.reloads == bs.AUTO_BROWSER_CHECK_RETRIES and tab.opened == []

    def test_no_form_is_a_wall_naming_the_page(self, monkeypatch, tmp_path):
        with pytest.raises(bs.LoginWall) as e:
            _strict(monkeypatch, tmp_path, _Tab(has_form=False), [_Source(["LINK"])])
        assert e.value.kind == bs.WALL_NO_FORM and "/login" in str(e.value)


class TestStopPrefill:
    def test_stopping_frees_the_mailbox_thread_without_the_loop(self):
        async def started():
            task = bs._start_prefill(_Tab(has_form=False), "a@example.com", lambda m: None, None)
            return task

        loop = asyncio.new_event_loop()
        try:
            task = loop.run_until_complete(started())
            bs._stop_prefill(task)  # the loop is no longer running here
            assert task.aswap_stop.is_set()
        finally:
            loop.run_until_complete(asyncio.sleep(0))
            loop.close()

    def test_strict_needs_an_email(self):
        with pytest.raises(bs.LoginWall):
            bs._start_prefill(_Tab(), None, lambda m: None, None, strict=True)


class TestWallWatch:
    def _tab(self, answers):
        class T:
            async def evaluate(self, js, return_by_value=False):
                return answers.pop(0)

        return T()

    def test_a_wall_counts_only_once_it_stays(self):
        watch = bs._WallWatch()
        tab = self._tab(["phone", "", "phone", "phone", "phone"])
        for _ in range(4):
            asyncio.run(watch.check(tab))
        with pytest.raises(bs.LoginWall) as e:
            asyncio.run(watch.check(tab))
        assert e.value.kind == bs.WALL_PHONE

    def test_captcha_stops_a_whole_run(self):
        assert bs.WALL_CAPTCHA in bs.WALL_STOPS_RUN
        assert bs.WALL_NO_EMAIL not in bs.WALL_STOPS_RUN


# --- relogin-dead ------------------------------------------------------------


class _Sw:
    backup_dir = None


def _dead(num, email, active=False, disabled=False):
    return relogin.DeadAccount(num=num, email=email, org_uuid="o", active=active, disabled=disabled)


@pytest.fixture
def loop(monkeypatch):
    import claude_swap.switcher as switcher

    tried: list[str] = []
    walls: dict[str, str] = {}
    monkeypatch.setattr(switcher, "ClaudeAccountSwitcher", lambda debug=False: _Sw())
    monkeypatch.setattr(relogin, "require_inbox", lambda root: True)

    def run_one(sw, num, email, org, *, chrome=None, timeout_s=0, out=print):
        tried.append(num)
        if num in walls:
            return relogin.Result(num, email, relogin.FAILED, "wall", wall=walls[num])
        return relogin.Result(num, email, relogin.OK, "new token stored")

    monkeypatch.setattr(relogin, "run_one", run_one)
    return SimpleNamespace(tried=tried, walls=walls, monkeypatch=monkeypatch)


class TestReloginDead:
    def _set(self, loop, accounts):
        loop.monkeypatch.setattr(relogin, "dead_accounts", lambda sw: accounts)

    def test_skips_the_active_and_disabled_accounts(self, loop, capsys):
        self._set(loop, [_dead("1", "a@x", active=True), _dead("2", "b@x", disabled=True),
                         _dead("3", "c@x")])
        relogin.relogin_dead_command([], sleep=lambda s: None)
        assert loop.tried == ["3"]
        out = capsys.readouterr().out
        assert "the active login" in out and "disabled" in out

    def test_a_captcha_stops_the_run_and_fails_it(self, loop, capsys):
        self._set(loop, [_dead("3", "c@x"), _dead("4", "d@x"), _dead("5", "e@x")])
        loop.walls["3"] = bs.WALL_CAPTCHA
        with pytest.raises(SystemExit) as e:
            relogin.relogin_dead_command([], sleep=lambda s: None)
        assert e.value.code == 1 and loop.tried == ["3"]
        assert "run stopped at the captcha wall" in capsys.readouterr().out

    def test_a_missing_mail_moves_on_to_the_next_account(self, loop):
        self._set(loop, [_dead("3", "c@x"), _dead("4", "d@x")])
        loop.walls["3"] = bs.WALL_NO_EMAIL
        with pytest.raises(SystemExit):
            relogin.relogin_dead_command([], sleep=lambda s: None)
        assert loop.tried == ["3", "4"]

    def test_dry_run_signs_nobody_in(self, loop, capsys):
        self._set(loop, [_dead("3", "c@x")])
        relogin.relogin_dead_command(["--dry-run"], sleep=lambda s: None)
        assert loop.tried == [] and "would sign in" in capsys.readouterr().out

    def test_named_accounts_only(self, loop):
        self._set(loop, [_dead("3", "c@x"), _dead("4", "d@x")])
        relogin.relogin_dead_command(["d@x"], sleep=lambda s: None)
        assert loop.tried == ["4"]

    def test_paced_between_accounts(self, loop):
        self._set(loop, [_dead("3", "c@x"), _dead("4", "d@x")])
        waits: list[float] = []
        relogin.relogin_dead_command(["--pace", "7"], sleep=waits.append)
        assert waits == [7.0]
