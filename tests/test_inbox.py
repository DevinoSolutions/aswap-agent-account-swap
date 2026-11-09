"""The shared login inbox: link parsing, IMAP polling, the login hook."""

from __future__ import annotations

import asyncio
import base64
from email.message import EmailMessage

import pytest

from claude_swap import billing_session as bs
from claude_swap import inbox as ib


def _link(email: str, nonce: str = "a" * 32) -> str:
    return f"https://claude.ai/magic-link#{nonce}:{base64.b64encode(email.encode()).decode()}"


def _mail(to: str, link: str | None, sender: str = "no-reply-x@mail.anthropic.com") -> bytes:
    msg = EmailMessage()
    msg["From"] = f"Anthropic <{sender}>"
    msg["To"] = to
    msg["Subject"] = "Your secure link to Claude is here"
    msg.set_content("Click the button below to finish signing in.")
    if link:
        msg.add_alternative(f'<p><a clicktracking="off" href="{link}">Sign in</a></p>', subtype="html")
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


class TestInbox:
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
            assert inbox.latest_uid() == 5
            assert inbox.find_link("a@example.com", after_uid=5) is None

    def test_mail_from_someone_else_is_ignored(self):
        imap = FakeImap({2: _mail("a@example.com", _link("a@example.com"), sender="x@evil.test")})
        with ib.Inbox(CONFIG, imap_factory=imap) as inbox:
            assert inbox.find_link("a@example.com", after_uid=1) is None

    def test_bad_password_raises_inbox_error(self):
        with pytest.raises(ib.InboxError, match="AUTHENTICATIONFAILED"):
            ib.Inbox(CONFIG, imap_factory=FakeImap({}, fail_login=True))

    def test_wait_polls_until_the_link_arrives(self):
        imap = FakeImap({1: _mail("x@example.com", None)})
        polls: list[float] = []

        def sleep(s):
            polls.append(s)
            imap.messages[2] = _mail("a@example.com", _link("a@example.com"))

        with ib.Inbox(CONFIG, imap_factory=imap) as inbox:
            link = inbox.wait_for_link("a@example.com", 1, sleep=sleep)
        assert link == _link("a@example.com") and len(polls) == 1

    def test_wait_gives_up_at_the_deadline(self):
        t = [0.0]

        def sleep(s):
            t[0] += s

        with ib.Inbox(CONFIG, imap_factory=FakeImap({1: b""})) as inbox:
            link = inbox.wait_for_link("a@example.com", 1, timeout_s=10, poll_s=4,
                                       clock=lambda: t[0], sleep=sleep)
        assert link is None and t[0] >= 10

    def test_wait_stops_when_asked(self):
        with ib.Inbox(CONFIG, imap_factory=FakeImap({1: b""})) as inbox:
            assert inbox.wait_for_link("a@example.com", 1, stop=lambda: True) is None


class TestStore:
    def test_round_trip_and_forget(self, tmp_path):
        assert ib.load_inbox(tmp_path) is None
        ib.save_inbox(tmp_path, CONFIG)
        assert ib.load_inbox(tmp_path) == CONFIG
        assert ib.forget_inbox(tmp_path)
        assert ib.load_inbox(tmp_path) is None and not ib.forget_inbox(tmp_path)


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
            original = element.click

            async def click():
                self._on_click()
                await original()

            element.click = click
        return element

    async def send(self, command):
        pass

    async def get(self, url):
        self.opened.append(url)


class TestPrefillWithInbox:
    def _run(self, monkeypatch, imap, tab):
        monkeypatch.setattr(ib.Inbox, "__init__", lambda self, config: (
            setattr(self, "config", config), setattr(self, "_imap", imap))[0])
        messages: list[str] = []
        ok = asyncio.run(bs._nd_prefill_email(
            tab, "a@example.com", messages.append, wait_s=0, inbox_config=CONFIG))
        return ok, messages

    def test_opens_the_link_that_arrives_after_continue(self, monkeypatch):
        imap = FakeImap({1: _mail("a@example.com", _link("a@example.com", "1" * 32))})
        fresh = _link("a@example.com", "2" * 32)
        tab = FakeTab(on_click=lambda: imap.messages.__setitem__(2, _mail("a@example.com", fresh)))
        ok, messages = self._run(monkeypatch, imap, tab)
        assert ok and tab.opened == [fresh]
        assert any("watching logins@example.com" in m for m in messages)
        assert all(fresh not in m for m in messages)  # the link is never printed
        assert imap.logged_out

    def test_unreachable_inbox_falls_back_to_a_manual_sign_in(self, monkeypatch):
        def boom(self, config):
            raise ib.InboxError("down")

        monkeypatch.setattr(ib.Inbox, "__init__", boom)
        messages: list[str] = []
        tab = FakeTab()
        ok = asyncio.run(bs._nd_prefill_email(
            tab, "a@example.com", messages.append, wait_s=0, inbox_config=CONFIG))
        assert ok and tab.opened == []
        assert any("unreachable" in m for m in messages)
