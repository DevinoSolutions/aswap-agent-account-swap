"""Login mailboxes: link parsing, IMAP/Gmail/Outlook polling, the login hook."""

from __future__ import annotations

import asyncio
import base64
import json
from email.message import EmailMessage
from types import SimpleNamespace

import pytest

from claude_swap import billing_session as bs
from claude_swap import inbox as ib
from claude_swap import inbox_oauth as io


def _link(email: str, nonce: str = "a" * 32) -> str:
    return f"https://claude.ai/magic-link#{nonce}:{base64.b64encode(email.encode()).decode()}"


def _html(link: str) -> str:
    return (f'<p><a clicktracking="off" href="{link}">Sign in</a></p>'
            '<a href="https://url8792.mail.anthropic.com/ls/click?upn=x">Help</a>')


def _mail(to: str, link: str | None, sender: str = "no-reply-x@mail.anthropic.com") -> bytes:
    msg = EmailMessage()
    msg["From"] = f"Anthropic <{sender}>"
    msg["To"] = to
    msg["Subject"] = "Your secure link to Claude is here"
    msg.set_content("Click the button below to finish signing in.")
    if link:
        msg.add_alternative(_html(link) if "anthropic" in sender else f'<a href="{link}">x</a>',
                            subtype="html")
    return msg.as_bytes()


class FakeImap:
    """Just enough of IMAP4_SSL: UID SEARCH/FETCH over a dict of messages."""

    def __init__(self, messages: dict[int, bytes], *, fail_login: bool = False):
        self.messages = messages
        self.fail_login = fail_login
        self.logged_out = False

    def __call__(self, host, port, timeout=None):
        return self

    def login(self, user, password):
        if self.fail_login:
            raise ib.imaplib.IMAP4.error("AUTHENTICATIONFAILED")

    def select(self, folder, readonly=False):
        return "OK", [b"1"]

    def noop(self):
        return "OK", []

    def uid(self, command, *args):
        if command == "SEARCH":
            criteria = args[-1]
            uids = sorted(self.messages)
            if criteria.startswith("UID "):
                low = int(criteria[4:].split(":")[0])
                # Like a real server, "N:*" returns the last message even when N > max.
                uids = [u for u in uids if u >= low] or uids[-1:]
            elif criteria.startswith("FROM"):
                uids = [u for u in uids if b"mail.anthropic.com" in self.messages[u]]
            return "OK", [" ".join(map(str, uids)).encode()]
        if command == "FETCH":
            uid = int(args[0])
            return "OK", [(f"{uid} (BODY[] {{1}}".encode(), self.messages[uid]), b")"]
        raise AssertionError(command)

    def logout(self):
        self.logged_out = True


CONFIG = ib.InboxConfig(user="logins@example.com", password="pw")


class TestParsing:
    def test_link_email_decodes_the_address(self):
        assert ib.link_email(_link("Hello@Devino.ca")) == "hello@devino.ca"

    def test_link_email_without_an_address(self):
        assert ib.link_email("https://claude.ai/magic-link#abc") is None

    def test_message_links_reads_the_html_part(self):
        link = _link("a@example.com")
        assert ib.message_links(_mail("a@example.com", link)) == [link]

    def test_message_without_a_link(self):
        assert ib.message_links(_mail("a@example.com", None)) == []

    def test_pick_link_wants_the_accounts_own_link(self):
        a, b = _link("a@example.com"), _link("b@example.com")
        assert ib.pick_link([a, b], "B@example.com") == b
        assert ib.pick_link([a], "c@example.com") is None


class TestImap:
    def test_finds_the_link_for_the_account(self):
        imap = FakeImap({
            1: _mail("a@example.com", _link("a@example.com", "1" * 32)),  # before the request
            2: _mail("b@example.com", _link("b@example.com")),
            3: _mail("a@example.com", _link("a@example.com", "3" * 32)),
        })
        with ib.Inbox(CONFIG, imap_factory=imap) as inbox:
            assert inbox.find_link("A@example.com", after_uid=1) == _link("a@example.com", "3" * 32)
        assert imap.logged_out

    def test_old_mail_is_never_used(self):
        imap = FakeImap({5: _mail("a@example.com", _link("a@example.com"))})
        with ib.Inbox(CONFIG, imap_factory=imap) as inbox:
            assert inbox.snapshot() == 5
            assert inbox.find_link("a@example.com", after_uid=5) is None

    def test_mail_from_someone_else_is_ignored(self):
        imap = FakeImap({2: _mail("a@example.com", _link("a@example.com"), sender="x@evil.test")})
        with ib.Inbox(CONFIG, imap_factory=imap) as inbox:
            assert inbox.find_link("a@example.com", after_uid=1) is None

    def test_bad_password_raises_inbox_error(self):
        with pytest.raises(ib.InboxError, match="AUTHENTICATIONFAILED"):
            ib.Inbox(CONFIG, imap_factory=FakeImap({}, fail_login=True))


class FakeSource:
    def __init__(self, address, links_by_poll, *, fail=False):
        self.address = address
        self._polls = list(links_by_poll)
        self.fail = fail
        self.closed = False

    def snapshot(self):
        return "snap"

    def find_link(self, email, snap):
        assert snap == "snap"
        if self.fail:
            raise ib.InboxError("gone")
        return self._polls.pop(0) if self._polls else None

    def close(self):
        self.closed = True


class TestWaitForLink:
    def test_polls_every_mailbox_until_one_has_it(self):
        a = FakeSource("a@x", [None, None])
        b = FakeSource("b@x", [None, "LINK"])
        sleeps: list[float] = []
        link = ib.wait_for_link([a, b], "u@x", ["snap", "snap"], sleep=sleeps.append)
        assert link == "LINK" and len(sleeps) == 1

    def test_a_failing_mailbox_is_dropped_not_fatal(self):
        bad = FakeSource("bad@x", [], fail=True)
        good = FakeSource("good@x", [None, "LINK"])
        said: list[str] = []
        link = ib.wait_for_link([bad, good], "u@x", ["snap", "snap"],
                                sleep=lambda s: None, out=said.append)
        assert link == "LINK"
        assert sum("bad@x" in m for m in said) == 1

    def test_gives_up_at_the_deadline(self):
        t = [0.0]

        def sleep(s):
            t[0] += s

        link = ib.wait_for_link([FakeSource("a@x", [])], "u@x", ["snap"], timeout_s=10,
                                poll_s=4, clock=lambda: t[0], sleep=sleep)
        assert link is None and t[0] >= 10

    def test_stops_when_asked(self):
        assert ib.wait_for_link([FakeSource("a@x", ["LINK"])], "u@x", ["snap"],
                                stop=lambda: True) is None


class TestStore:
    def test_add_replace_and_remove(self, tmp_path):
        assert ib.load_sources(tmp_path) == []
        ib.add_source(tmp_path, ib.imap_row(CONFIG))
        ib.add_source(tmp_path, {"kind": "google", "address": "g@x", "refreshToken": "r1"})
        ib.add_source(tmp_path, {"kind": "google", "address": "G@x", "refreshToken": "r2"})
        rows = ib.load_sources(tmp_path)
        assert [(r["kind"], r["address"]) for r in rows] == [("imap", CONFIG.user), ("google", "G@x")]
        ib.update_source(tmp_path, "google", "g@x", refreshToken="r3")
        assert ib.load_sources(tmp_path)[1]["refreshToken"] == "r3"
        assert ib.remove_sources(tmp_path, "g@x") == 1
        assert ib.remove_sources(tmp_path, None) == 1
        assert ib.load_sources(tmp_path) == [] and not (tmp_path / ib.FILE_NAME).exists()

    def test_reads_the_single_imap_mailbox_of_version_1(self, tmp_path):
        (tmp_path / ib.FILE_NAME).write_text(json.dumps(
            {"version": 1, "user": "u@x", "password": "pw", "host": "imap.x", "port": 993}))
        [row] = ib.load_sources(tmp_path)
        assert row["kind"] == "imap" and row["address"] == "u@x" and row["host"] == "imap.x"


# --- Gmail / Outlook ------------------------------------------------------------


class FakeApi:
    """Routes io._request calls by URL prefix."""

    def __init__(self):
        self.calls: list[tuple[str, dict | None]] = []
        self.gmail: dict[str, bytes] = {}  # id -> raw message, newest last
        self.graph: list[dict] = []  # newest first
        self.refresh_token_out: str | None = None

    def __call__(self, url, *, data=None, token=None, headers=None, timeout=30.0):
        self.calls.append((url, data))
        if url.endswith("/token"):
            out = {"access_token": "at", "expires_in": 3600}
            if self.refresh_token_out:
                out["refresh_token"] = self.refresh_token_out
            return out
        if url.startswith(f"{io.GMAIL}/messages?"):
            return {"messages": [{"id": i} for i in reversed(self.gmail)]}
        if url.startswith(f"{io.GMAIL}/messages/"):
            mid = url.split("/messages/")[1].split("?")[0]
            return {"raw": base64.urlsafe_b64encode(self.gmail[mid]).decode()}
        if url.startswith(f"{io.GRAPH}/messages?"):
            return {"value": self.graph}
        raise AssertionError(url)


@pytest.fixture
def api(monkeypatch):
    fake = FakeApi()
    monkeypatch.setattr(io, "_request", fake)
    return fake


class TestGmail:
    def test_finds_only_new_mail_for_the_account(self, api, tmp_path):
        api.gmail["m1"] = _mail("a@example.com", _link("a@example.com", "1" * 32))
        row = {"kind": "google", "address": "hub@x", "clientId": "c", "clientSecret": "s",
               "refreshToken": "r"}
        source = io.open_api_source(tmp_path, row)
        snap = source.snapshot()
        assert source.find_link("a@example.com", snap) is None
        api.gmail["m2"] = _mail("b@example.com", _link("b@example.com"))
        api.gmail["m3"] = _mail("a@example.com", _link("a@example.com", "3" * 32))
        assert source.find_link("a@example.com", snap) == _link("a@example.com", "3" * 32)

    def test_asks_only_for_read_only_mail(self):
        assert io.GOOGLE_SCOPE == "https://www.googleapis.com/auth/gmail.readonly"


class TestOutlook:
    def test_finds_only_new_mail_for_the_account(self, api, tmp_path):
        old = {"id": "o1", "from": {"emailAddress": {"address": "no-reply@mail.anthropic.com"}},
               "body": {"content": _html(_link("a@example.com", "1" * 32))}}
        api.graph = [old]
        source = io.open_api_source(tmp_path, {"kind": "outlook", "address": "hub@x",
                                               "clientId": "c", "refreshToken": "r"})
        snap = source.snapshot()
        # Forwarded by a rule: the sender is the forwarding mailbox, the body keeps Anthropic's links.
        fwd = {"id": "o2", "from": {"emailAddress": {"address": "me@outlook.com"}},
               "body": {"content": _html(_link("a@example.com", "2" * 32))}}
        spam = {"id": "o3", "from": {"emailAddress": {"address": "x@evil.test"}},
                "body": {"content": f'<a href="{_link("a@example.com", "3" * 32)}">x</a>'}}
        api.graph = [spam, fwd, old]
        assert source.find_link("a@example.com", snap) == _link("a@example.com", "2" * 32)

    def test_a_rotated_refresh_token_is_saved(self, api, tmp_path):
        row = {"kind": "outlook", "address": "hub@x", "clientId": "c", "refreshToken": "r1"}
        ib.add_source(tmp_path, row)
        api.refresh_token_out = "r2"
        io.open_api_source(tmp_path, row)
        assert ib.load_sources(tmp_path)[0]["refreshToken"] == "r2"

    def test_asks_only_for_read_only_mail(self):
        assert set(io.MS_SCOPE.split()) == {
            "offline_access", "openid", "email", "https://graph.microsoft.com/Mail.Read"}


class TestConnect:
    def _browser(self, code="the-code"):
        """Stands in for the user: follows the authorize URL's redirect."""
        import urllib.parse
        import urllib.request

        def open_browser(url):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            back = f"{q['redirect_uri'][0]}/?code={code}&state={q['state'][0]}"
            import threading
            threading.Timer(0.2, lambda: urllib.request.urlopen(back, timeout=5).read()).start()
            return True

        return open_browser

    def test_google_connect_learns_the_address(self, api, tmp_path, monkeypatch):
        def fake(url, *, data=None, token=None, headers=None, timeout=30.0):
            if url == io.GOOGLE_TOKEN:
                assert data["code"] == "the-code" and data["code_verifier"]
                return {"access_token": "at", "refresh_token": "rt"}
            assert url == f"{io.GMAIL}/profile"
            return {"emailAddress": "hub@devino.ca"}

        monkeypatch.setattr(io, "_request", fake)
        args = SimpleNamespace(kind="google", client_file=None, client_id="cid",
                               client_secret="sec", hint=None)
        row = io.connect(tmp_path, args, out=lambda m: None, open_browser=self._browser())
        assert row == {"kind": "google", "address": "hub@devino.ca", "clientId": "cid",
                       "clientSecret": "sec", "refreshToken": "rt"}

    def test_outlook_connect_reads_the_id_token(self, tmp_path, monkeypatch):
        claims = base64.urlsafe_b64encode(json.dumps({"email": "hub@outlook.com"}).encode()).decode().rstrip("=")
        monkeypatch.setattr(io, "_request", lambda url, **kw: {
            "access_token": "at", "refresh_token": "rt", "id_token": f"h.{claims}.s"})
        args = SimpleNamespace(kind="outlook", client_id="cid", tenant=None, hint=None)
        row = io.connect(tmp_path, args, out=lambda m: None, open_browser=self._browser())
        assert row["address"] == "hub@outlook.com" and row["tenant"] == "common"

    def test_a_second_mailbox_reuses_the_client(self, tmp_path):
        ib.add_source(tmp_path, {"kind": "google", "address": "a@x", "clientId": "cid",
                                 "clientSecret": "sec", "refreshToken": "r"})
        args = SimpleNamespace(client_file=None, client_id=None, client_secret=None)
        assert io._google_client(tmp_path, args) == ("cid", "sec")

    def test_no_client_is_explained(self, tmp_path):
        args = SimpleNamespace(client_file=None, client_id=None, client_secret=None)
        with pytest.raises(ib.InboxError, match="Desktop-app client"):
            io._google_client(tmp_path, args)


# --- the login hook ------------------------------------------------------------


class FakeElement:
    async def focus(self):
        pass

    async def click(self):
        pass


class FakeTab:
    def __init__(self, on_click=None):
        self.opened: list[str] = []
        self._on_click = on_click

    async def select(self, selector, timeout=10):
        element = FakeElement()
        if selector == bs._CONTINUE_BUTTON and self._on_click:
            on_click = self._on_click

            async def click():
                on_click()

            element.click = click
        return element

    async def send(self, command):
        pass

    async def get(self, url):
        self.opened.append(url)


class TestPrefillWithInbox:
    def _run(self, monkeypatch, tmp_path, sources, tab):
        monkeypatch.setattr(ib, "open_sources", lambda root, out: sources)
        messages: list[str] = []
        ok = asyncio.run(bs._nd_prefill_email(
            tab, "a@example.com", messages.append, wait_s=0, inbox_root=tmp_path))
        return ok, messages

    def test_opens_the_link_that_arrives_after_continue(self, monkeypatch, tmp_path):
        imap = FakeImap({1: _mail("a@example.com", _link("a@example.com", "1" * 32))})
        inbox = ib.Inbox(CONFIG, imap_factory=imap)
        fresh = _link("a@example.com", "2" * 32)
        tab = FakeTab(on_click=lambda: imap.messages.__setitem__(2, _mail("a@example.com", fresh)))
        ok, messages = self._run(monkeypatch, tmp_path, [inbox], tab)
        assert ok and tab.opened == [fresh]
        assert any("watching logins@example.com" in m for m in messages)
        assert all(fresh not in m for m in messages)  # the link is never printed
        assert imap.logged_out

    def test_no_mailbox_leaves_the_sign_in_to_you(self, monkeypatch, tmp_path):
        tab = FakeTab()
        ok, messages = self._run(monkeypatch, tmp_path, [], tab)
        assert ok and tab.opened == []
        assert any("finish with the code" in m for m in messages)

    def test_a_mailbox_that_cannot_snapshot_is_skipped(self, monkeypatch, tmp_path):
        bad = FakeSource("bad@x", [])
        bad.snapshot = lambda: (_ for _ in ()).throw(ib.InboxError("down"))
        good = FakeSource("good@x", ["LINK"])
        tab = FakeTab()
        ok, messages = self._run(monkeypatch, tmp_path, [bad, good], tab)
        assert ok and tab.opened == ["LINK"]
        assert bad.closed and good.closed
        assert any("bad@x unreadable" in m for m in messages)
