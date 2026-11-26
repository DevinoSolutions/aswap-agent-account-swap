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

    def test_secrets_go_only_to_a_google_token_endpoint(self, gmail, tmp_path):
        io.import_token_file(_token_file(tmp_path, token_uri="https://evil.example/token"))
        posted = [url for url, data in gmail if data and "refresh_token" in data]
        assert posted == [io.GOOGLE_TOKEN]

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

    clicks = 0

    async def select(self, selector, timeout=10):
        if selector == bs._EMAIL_INPUT and not self.has_form:
            raise TimeoutError
        element = _Element()
        if selector == bs._CONTINUE_BUTTON:
            async def click():
                self.clicks += 1

            element.click = click
        return element

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
        link = "https://claude.ai/magic-link#nonce:YUBleGFtcGxlLmNvbQ=="
        ok, messages = _strict(monkeypatch, tmp_path, tab, [_Source([link])])
        assert ok and tab.opened == [link]
        assert tab.clicks == 1  # one Continue, so one sign-in mail
        assert all(link not in m and "nonce" not in m for m in messages)

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


# --- relogin_account / login --auto: only the slot is ever written --------------


def _blob(access="at-NEW-SECRET", refresh="rt-NEW-SECRET"):
    return json.dumps({"claudeAiOauth": {"accessToken": access, "refreshToken": refresh, "expiresAt": 9}})


class _Browser:
    closed = False

    def close(self):
        self.closed = True


def _real_switcher():
    from claude_swap.models import Platform
    from claude_swap.switcher import ClaudeAccountSwitcher

    s = ClaudeAccountSwitcher()
    s.platform = Platform.LINUX
    s._setup_directories()
    s._init_sequence_file()
    return s


@pytest.fixture
def slot(temp_home, monkeypatch):
    """A switcher with one stored account and a live login that belongs to
    someone else; capture and mint are stubbed (no browser, no network)."""
    live_creds = temp_home / ".claude" / ".credentials.json"
    live_conf = temp_home / ".claude.json"
    live_creds.write_text(_blob("at-LIVE", "rt-LIVE"), encoding="utf-8")
    live_conf.write_text(json.dumps({"oauthAccount": {"emailAddress": "live@example.com"}}), encoding="utf-8")
    sw = _real_switcher()
    sw.add_account_from_token("sk-ant-oat01-old", email="b@example.com")
    num, email, org = sw.resolve_account("b@example.com")
    browser = _Browser()
    seen = SimpleNamespace(capture_kw=None, mint_kw=None, browser=browser, captures=0)

    def capture(identity, root, **kw):
        seen.captures += 1
        seen.capture_kw = kw
        return "sk-SESSION-SECRET", browser

    def mint(b, root, **kw):
        seen.mint_kw = kw
        return _blob()

    monkeypatch.setattr(bs, "capture_session", capture)
    monkeypatch.setattr(relogin.tp, "mint_credentials", mint)
    monkeypatch.setattr(sw._billing, "refresh", lambda *a, **kw: None)
    before = {p: p.read_bytes() for p in (live_creds, live_conf)}
    return SimpleNamespace(sw=sw, num=str(num), email=email, org=org or "", seen=seen, live=before)


class TestReloginAccount:
    def test_only_the_slot_is_written_never_the_live_login(self, slot):
        relogin.relogin_account(slot.sw, slot.num, slot.email, slot.org, out=lambda m: None)
        stored = json.loads(slot.sw._read_account_credentials(slot.num, slot.email))["claudeAiOauth"]
        assert stored["refreshToken"] == "rt-NEW-SECRET"
        for path, data in slot.live.items():
            assert path.read_bytes() == data, f"{path.name} was written"
        assert slot.seen.browser.closed
        assert slot.seen.capture_kw["strict"] is True and slot.seen.mint_kw["strict"] is True

    def test_a_wall_in_the_mint_closes_the_browser_and_keeps_the_old_token(self, slot, monkeypatch):
        old = slot.sw._read_account_credentials(slot.num, slot.email)

        def captcha(b, root, **kw):
            raise bs.LoginWall(bs.WALL_CAPTCHA, "captcha")

        monkeypatch.setattr(relogin.tp, "mint_credentials", captcha)
        result = relogin.run_one(slot.sw, slot.num, slot.email, slot.org, out=lambda m: None)
        assert result.status == relogin.FAILED and result.wall == bs.WALL_CAPTCHA
        assert slot.seen.browser.closed
        assert slot.sw._read_account_credentials(slot.num, slot.email) == old

    def test_the_active_slot_is_refused_before_any_browser(self, slot, monkeypatch):
        real = slot.sw._build_accounts_info

        def as_active():
            return [(n, e, on, o, str(n) == slot.num, c, a) for n, e, on, o, _x, c, a in real()]

        monkeypatch.setattr(slot.sw, "_build_accounts_info", as_active)
        with pytest.raises(relogin.ActiveSlot):
            relogin.relogin_account(slot.sw, slot.num, slot.email, slot.org, out=lambda m: None)
        assert relogin.run_one(slot.sw, slot.num, slot.email, slot.org).status == relogin.SKIPPED
        monkeypatch.setattr(relogin, "require_inbox", lambda root: True)
        with pytest.raises(SystemExit) as e:
            relogin.login_auto(slot.sw, slot.email)
        assert e.value.code == 2 and slot.seen.captures == 0

    def test_login_auto_output_never_carries_a_secret(self, slot, monkeypatch, capsys):
        monkeypatch.setattr(relogin, "require_inbox", lambda root: True)
        relogin.login_auto(slot.sw, slot.email)
        out = capsys.readouterr()
        text = out.out + out.err
        assert "new token stored" in text
        for secret in ("SESSION-SECRET", "NEW-SECRET", "magic-link"):
            assert secret not in text


class TestRunOneClassification:
    @pytest.mark.parametrize("exc, status, wall", [
        (bs.LoginWall(bs.WALL_CAPTCHA, "x"), relogin.FAILED, bs.WALL_CAPTCHA),
        (bs.LoginWall(bs.WALL_BROWSER_CHECK, "x"), relogin.FAILED, bs.WALL_BROWSER_CHECK),
        (bs.LoginWall(bs.WALL_NO_EMAIL, "x"), relogin.FAILED, bs.WALL_NO_EMAIL),
        (bs.NoPlan("x"), relogin.FAILED, "no-plan"),
        (bs.ReauthRequired("x"), relogin.FAILED, "reauth"),
        (bs.OAuthError("x"), relogin.FAILED, None),
        (OSError("disk"), relogin.FAILED, None),
        (relogin.ActiveSlot("x"), relogin.SKIPPED, None),
    ])
    def test_each_failure_keeps_its_kind(self, monkeypatch, exc, status, wall):
        def boom(*a, **kw):
            raise exc

        monkeypatch.setattr(relogin, "relogin_account", boom)
        r = relogin.run_one(None, "3", "c@x", "o")
        assert (r.status, r.wall) == (status, wall)


# --- the mint's reauth sign-in keeps the wall kind (M1) -------------------------


class _MintTab:
    """The consent tab: claude.ai sends it to /login?reauth=1."""

    def __init__(self, wall_answer=""):
        self.wall_answer = wall_answer

    async def evaluate(self, js, return_by_value=False):
        if js == bs._REAUTH_PAGE_JS:
            return True
        if js == bs._WALL_JS:
            return self.wall_answer
        return False

    async def get(self, url):
        pass


class _MintBrowser:
    def __init__(self, tab):
        self.tab = tab

    async def get(self, url, new_tab=False):
        return self.tab


def _mint(monkeypatch, tab, prefill_coro=None, timeout_s=5.0):
    """Run the strict mint against a tab stuck on the reauth sign-in."""
    real_sleep = asyncio.sleep

    async def waiting_for_mail():
        await real_sleep(60)  # the mailbox watcher, still waiting

    def start(tab_, email, out, root, **kw):
        assert kw.get("strict") is True
        return asyncio.ensure_future((prefill_coro or waiting_for_mail)())

    async def quick(s, *a, **kw):  # the 0.5 s poll, without the wait
        return await real_sleep(0.001 if s < 5 else s)

    monkeypatch.setattr(bs, "_start_prefill", start)
    monkeypatch.setattr(bs, "REAUTH_WAIT_S", timeout_s)
    monkeypatch.setattr(asyncio, "sleep", quick)
    return asyncio.run(bs._nd_mint_oauth_tokens(
        _MintBrowser(tab), timeout_s, lambda m: None, email="a@example.com", root=None, strict=True,
    ))


class TestMintReauthWalls:
    def test_a_prefill_wall_keeps_its_kind(self, monkeypatch):
        async def wall():
            raise bs.LoginWall(bs.WALL_BROWSER_CHECK, "bot check")

        with pytest.raises(bs.LoginWall) as e:
            _mint(monkeypatch, _MintTab(), wall)
        assert e.value.kind == bs.WALL_BROWSER_CHECK
        assert e.value.kind in bs.WALL_STOPS_RUN

    def test_a_captcha_on_the_reauth_pages_is_seen(self, monkeypatch):
        with pytest.raises(bs.LoginWall) as e:
            _mint(monkeypatch, _MintTab(wall_answer="captcha"))
        assert e.value.kind == bs.WALL_CAPTCHA

    def test_no_mail_during_the_reauth_is_no_email(self, monkeypatch):
        with pytest.raises(bs.LoginWall) as e:
            _mint(monkeypatch, _MintTab(), timeout_s=0.05)
        assert e.value.kind == bs.WALL_NO_EMAIL


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
