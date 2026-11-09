"""Read claude.ai sign-in links from a shared IMAP inbox.

Every account's Anthropic mail is forwarded to one mailbox (``aswap inbox
set``). After ``aswap login`` presses Continue, aswap watches that mailbox
for the magic link claude.ai just sent and opens it in the login window, so
signing in needs no hands.

A link is ``https://claude.ai/magic-link#<nonce>:<base64 email>``: the email
it signs in is inside the link, so a forwarded copy is matched by its own
link, not by headers a forwarder may rewrite. Only messages that arrive after
the request (a higher IMAP UID than the inbox held before Continue) count, so
an older, already-used link is never opened.

The IMAP password is as sensitive as the session keys beside it: stored in
``<root>/login_inbox.json`` the same way, never logged, never printed.
"""

from __future__ import annotations

import base64
import binascii
import email
import email.policy
import imaplib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

from claude_swap import statedb
from claude_swap.settings import atomic_write_json

FILE_NAME = "login_inbox.json"
DEFAULT_HOST = "imap.gmail.com"
SENDER_DOMAIN = "mail.anthropic.com"

_LINK = re.compile(r"https://claude\.ai/magic-link#[0-9A-Za-z]+:[0-9A-Za-z+/=_-]+")


class InboxError(Exception):
    """The inbox could not be reached or read."""


@dataclass(frozen=True)
class InboxConfig:
    user: str
    password: str
    host: str = DEFAULT_HOST
    port: int = 993


def _path(root: Path) -> Path:
    return Path(root) / FILE_NAME


def load_inbox(root: Path) -> InboxConfig | None:
    """The configured inbox, or None when ``aswap inbox set`` never ran."""
    try:
        raw = json.loads(statedb.read_text(_path(root)))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    user, password = raw.get("user"), raw.get("password")
    if not (isinstance(user, str) and user and isinstance(password, str) and password):
        return None
    host = raw.get("host") if isinstance(raw.get("host"), str) and raw.get("host") else DEFAULT_HOST
    port = raw.get("port") if isinstance(raw.get("port"), int) else 993
    return InboxConfig(user=user, password=password, host=host, port=port)


def save_inbox(root: Path, config: InboxConfig) -> None:
    atomic_write_json(_path(root), {
        "version": 1,
        "user": config.user,
        "password": config.password,
        "host": config.host,
        "port": config.port,
    })


def forget_inbox(root: Path) -> bool:
    try:
        _path(root).unlink()
    except FileNotFoundError:
        return False
    return True


# --- parsing -----------------------------------------------------------------


def link_email(link: str) -> str | None:
    """The address a magic link signs in (the base64 after the colon)."""
    _, _, encoded = link.partition("#")[2].partition(":")
    if not encoded:
        return None
    padded = encoded + "=" * (-len(encoded) % 4)
    for decode in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            return decode(padded).decode("utf-8").strip().lower() or None
        except (binascii.Error, ValueError, UnicodeDecodeError):
            continue
    return None


def message_links(raw: bytes) -> list[str]:
    """Every claude.ai magic link in one RFC 822 message, in order."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    found: list[str] = []
    for part in msg.walk():
        if part.get_content_maintype() != "text":
            continue
        try:
            text = part.get_content()
        except (LookupError, ValueError):
            continue
        for link in _LINK.findall(text):
            if link not in found:
                found.append(link)
    return found


def _sender_ok(raw: bytes) -> bool:
    """From Anthropic, either directly or as a forward that kept the sender."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    headers = " ".join(str(msg.get(h, "")) for h in ("From", "X-Original-Sender", "Reply-To"))
    return SENDER_DOMAIN in headers.lower() or b"@" + SENDER_DOMAIN.encode() in raw


# --- IMAP --------------------------------------------------------------------


class Inbox:
    """One IMAP connection to the shared login inbox (INBOX folder)."""

    def __init__(self, config: InboxConfig, *, imap_factory=imaplib.IMAP4_SSL) -> None:
        self.config = config
        try:
            self._imap = imap_factory(config.host, config.port, timeout=30)
            self._imap.login(config.user, config.password)
            self._imap.select("INBOX", readonly=True)
        except (OSError, imaplib.IMAP4.error) as e:
            raise InboxError(f"{config.user} on {config.host}: {e}") from None

    def close(self) -> None:
        try:
            self._imap.logout()
        except (OSError, imaplib.IMAP4.error):
            pass

    def __enter__(self) -> "Inbox":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _uids(self, criteria: str) -> list[int]:
        try:
            self._imap.noop()  # lets the server report mail that just arrived
            status, data = self._imap.uid("SEARCH", None, criteria)
        except (OSError, imaplib.IMAP4.error) as e:
            raise InboxError(f"search failed: {e}") from None
        if status != "OK" or not data or not data[0]:
            return []
        return sorted(int(u) for u in data[0].split())

    def latest_uid(self) -> int:
        """The highest UID now in the inbox (0 when it is empty)."""
        uids = self._uids("ALL")
        return uids[-1] if uids else 0

    def _fetch(self, uid: int) -> bytes | None:
        try:
            status, data = self._imap.uid("FETCH", str(uid), "(BODY.PEEK[])")
        except (OSError, imaplib.IMAP4.error) as e:
            raise InboxError(f"fetch failed: {e}") from None
        if status != "OK":
            return None
        for item in data or ():
            if isinstance(item, tuple) and len(item) > 1 and isinstance(item[1], bytes):
                return item[1]
        return None

    def find_link(self, account_email: str, after_uid: int) -> str | None:
        """Newest magic link for ``account_email`` in mail newer than ``after_uid``."""
        want = account_email.strip().lower()
        for uid in reversed(self._uids(f"UID {after_uid + 1}:*")):
            if uid <= after_uid:  # "N:*" always returns the last message
                continue
            raw = self._fetch(uid)
            if raw is None or not _sender_ok(raw):
                continue
            for link in reversed(message_links(raw)):
                if link_email(link) == want:
                    return link
        return None

    def wait_for_link(
        self,
        account_email: str,
        after_uid: int,
        *,
        timeout_s: float = 180.0,
        poll_s: float = 4.0,
        stop=lambda: False,
        clock=time.monotonic,
        sleep=time.sleep,
    ) -> str | None:
        """Poll until the link arrives; None when ``timeout_s`` runs out or
        ``stop()`` turns true."""
        deadline = clock() + timeout_s
        while not stop():
            link = self.find_link(account_email, after_uid)
            if link or clock() >= deadline:
                return link
            sleep(poll_s)
        return None

    def anthropic_count(self) -> int:
        """How many messages from Anthropic the inbox holds."""
        return len(self._uids(f'FROM "{SENDER_DOMAIN}"'))


# --- CLI -----------------------------------------------------------------------


def inbox_command(argv: list[str]) -> None:
    import argparse
    import getpass
    import sys

    from claude_swap.printer import accent, error
    from claude_swap.switcher import ClaudeAccountSwitcher

    parser = argparse.ArgumentParser(
        prog="aswap inbox",
        description="The shared mailbox aswap login reads claude.ai sign-in links from.",
    )
    sub = parser.add_subparsers(dest="action")
    p = sub.add_parser("set", help="save the mailbox and its IMAP (app) password")
    p.add_argument("user", metavar="EMAIL")
    p.add_argument("--host", default=DEFAULT_HOST, help=f"IMAP server (default {DEFAULT_HOST})")
    p.add_argument("--port", type=int, default=993)
    p.add_argument("--password-stdin", action="store_true",
                   help="read the password from stdin instead of prompting")
    sub.add_parser("test", help="sign in to the mailbox and count Anthropic mail")
    sub.add_parser("show", help="print the configured mailbox (never the password)")
    sub.add_parser("forget", help="delete the saved mailbox and password")
    args = parser.parse_args(argv)
    if args.action is None:
        parser.error("a subcommand is required: set, test, show, forget")

    root = ClaudeAccountSwitcher().backup_dir
    if args.action == "set":
        if args.password_stdin:
            password = sys.stdin.readline().strip()
        else:
            password = getpass.getpass(f"IMAP password for {args.user}: ").strip()
        password = password.replace(" ", "")  # Google shows app passwords in groups of 4
        if not password:
            error("Error: no password given")
            sys.exit(1)
        config = InboxConfig(user=args.user, password=password, host=args.host, port=args.port)
        try:
            with Inbox(config):
                pass
        except InboxError as e:
            error(f"Error: sign-in failed, nothing saved: {e}")
            sys.exit(1)
        save_inbox(root, config)
        print(f"{accent('Saved')} login inbox {args.user} ({args.host})")
        return
    config = load_inbox(root)
    if args.action == "forget":
        print("Forgot the login inbox" if forget_inbox(root) else "No login inbox was set")
        return
    if config is None:
        print("No login inbox set (aswap inbox set EMAIL)")
        sys.exit(1)
    if args.action == "show":
        print(f"{config.user} on {config.host}:{config.port}")
        return
    try:
        with Inbox(config) as inbox:
            count = inbox.anthropic_count()
    except InboxError as e:
        error(f"Error: {e}")
        sys.exit(1)
    print(f"{accent('OK')} {config.user}: signed in; {count} message(s) from Anthropic in INBOX")
