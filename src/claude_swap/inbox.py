"""Read claude.ai sign-in links from your own mailboxes.

You forward each account's Anthropic mail (with your mail provider's own
rules) to one or more mailboxes and connect them with ``aswap inbox add``:
Gmail and Outlook through OAuth with read-only mail access, or any server
over IMAP. After ``aswap login`` presses Continue, aswap watches every
connected mailbox for the magic link claude.ai just sent and opens it in the
login window, so signing in needs no hands.

A link is ``https://claude.ai/magic-link#<nonce>:<base64 email>``: the email
it signs in is inside the link, so a forwarded copy is matched by its own
link, not by headers a forwarder may rewrite. Only mail that arrives after
the request counts (each mailbox is snapshotted before Continue), so an
older, already-used link is never opened.

Mailbox credentials (IMAP passwords, OAuth refresh tokens) are as sensitive
as the session keys beside them: stored in ``<root>/login_inbox.json`` the
same way, never logged, never printed.
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
from claude_swap.locking import FileLock
from claude_swap.settings import atomic_write_json

FILE_NAME = "login_inbox.json"
DEFAULT_HOST = "imap.gmail.com"
SENDER_DOMAIN = "mail.anthropic.com"
KINDS = ("google", "outlook", "imap")

_LINK = re.compile(r"https://claude\.ai/magic-link#[0-9A-Za-z]+:[0-9A-Za-z+/=_-]+")


class InboxError(Exception):
    """A mailbox could not be reached or read."""


# --- store -------------------------------------------------------------------
#
# {"version": 2, "sources": [{"kind": "google"|"outlook"|"imap",
#                             "address": ..., <kind-specific fields>}, ...]}
# Version 1 held a single IMAP mailbox at the top level.


def _path(root: Path) -> Path:
    return Path(root) / FILE_NAME


def _lock(root: Path) -> FileLock:
    return FileLock(Path(root) / ".login_inbox.lock")


def _valid(row: object) -> bool:
    return (
        isinstance(row, dict)
        and row.get("kind") in KINDS
        and isinstance(row.get("address"), str)
        and bool(row["address"])
    )


def load_sources(root: Path) -> list[dict]:
    """Every connected mailbox; empty when none (or the file is unreadable)."""
    try:
        raw = json.loads(statedb.read_text(_path(root)))
    except (OSError, ValueError, UnicodeDecodeError):
        return []
    if not isinstance(raw, dict):
        return []
    if "sources" not in raw and isinstance(raw.get("user"), str):  # version 1
        raw = {"sources": [{"kind": "imap", "address": raw["user"], **raw}]}
    rows = raw.get("sources")
    return [r for r in rows if _valid(r)] if isinstance(rows, list) else []


def _save(root: Path, rows: list[dict]) -> None:
    atomic_write_json(_path(root), {"version": 2, "sources": rows})


def _same(row: dict, kind: str, address: str) -> bool:
    return row["kind"] == kind and row["address"].lower() == address.lower()


def add_source(root: Path, row: dict) -> None:
    """Connect a mailbox, replacing an earlier connection to the same one."""
    with _lock(root):
        rows = [r for r in load_sources(root) if not _same(r, row["kind"], row["address"])]
        _save(root, [*rows, row])


def update_source(root: Path, kind: str, address: str, **fields) -> None:
    """Merge ``fields`` into one connection (a rotated refresh token)."""
    with _lock(root):
        rows = load_sources(root)
        for r in rows:
            if _same(r, kind, address):
                r.update(fields)
        _save(root, rows)


def remove_sources(root: Path, address: str | None) -> int:
    """Disconnect the mailbox at ``address`` (every mailbox when None)."""
    with _lock(root):
        rows = load_sources(root)
        keep = [] if address is None else [
            r for r in rows if r["address"].lower() != address.lower()
        ]
        if keep:
            _save(root, keep)
        else:
            try:
                _path(root).unlink()
            except FileNotFoundError:
                pass
        return len(rows) - len(keep)


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


def text_links(text: str) -> list[str]:
    """Every claude.ai magic link in ``text``, in order, without repeats."""
    return list(dict.fromkeys(_LINK.findall(text)))


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
        for link in text_links(text):
            if link not in found:
                found.append(link)
    return found


def from_anthropic(text: str | bytes) -> bool:
    """Sent by Anthropic, directly or as a forward (which keeps the original
    sender's address, or its mail.anthropic.com links, in the body)."""
    if isinstance(text, bytes):
        return SENDER_DOMAIN.encode() in text.lower()
    return SENDER_DOMAIN in text.lower()


def pick_link(links: list[str], account_email: str) -> str | None:
    """The last of ``links`` that signs in ``account_email``."""
    want = account_email.strip().lower()
    return next((link for link in reversed(links) if link_email(link) == want), None)


# --- sources -------------------------------------------------------------------
#
# A source is one connected mailbox: ``address``, ``snapshot()`` (what the
# mailbox holds now), ``find_link(email, snapshot)`` (a link that arrived
# since) and ``close()``. IMAP lives here; Gmail and Outlook in inbox_oauth.


@dataclass(frozen=True)
class InboxConfig:
    """An IMAP mailbox."""

    user: str
    password: str
    host: str = DEFAULT_HOST
    port: int = 993


def imap_row(config: InboxConfig) -> dict:
    return {
        "kind": "imap", "address": config.user, "password": config.password,
        "host": config.host, "port": config.port,
    }


def _imap_config(row: dict) -> InboxConfig:
    host = row.get("host") if isinstance(row.get("host"), str) and row.get("host") else DEFAULT_HOST
    port = row.get("port") if isinstance(row.get("port"), int) else 993
    return InboxConfig(user=row["address"], password=str(row.get("password") or ""), host=host, port=port)


class Inbox:
    """One IMAP connection to a mailbox's INBOX folder."""

    def __init__(self, config: InboxConfig, *, imap_factory=imaplib.IMAP4_SSL) -> None:
        self.config = config
        self.address = config.user
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

    snapshot = latest_uid

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
        for uid in reversed(self._uids(f"UID {after_uid + 1}:*")):
            if uid <= after_uid:  # "N:*" always returns the last message
                continue
            raw = self._fetch(uid)
            if raw is None or not from_anthropic(raw):
                continue
            link = pick_link(message_links(raw), account_email)
            if link:
                return link
        return None

    def anthropic_count(self) -> int:
        """How many messages from Anthropic the inbox holds."""
        return len(self._uids(f'FROM "{SENDER_DOMAIN}"'))


def open_source(root: Path, row: dict):
    """A live source for one stored connection (raises InboxError)."""
    if row["kind"] == "imap":
        return Inbox(_imap_config(row))
    from claude_swap import inbox_oauth

    return inbox_oauth.open_api_source(root, row)


def open_sources(root: Path, out) -> list:
    """Every connected mailbox that answers; one that does not is reported."""
    sources = []
    for row in load_sources(root):
        try:
            sources.append(open_source(root, row))
        except InboxError as e:
            out(f"Login inbox {row['address']} unreachable ({e}) — skipping it.")
    return sources


def wait_for_link(
    sources: list,
    account_email: str,
    snapshots: list,
    *,
    timeout_s: float = 180.0,
    poll_s: float = 4.0,
    stop=lambda: False,
    out=lambda _m: None,
    clock=time.monotonic,
    sleep=time.sleep,
) -> str | None:
    """Poll every source until one holds the link; None when ``timeout_s``
    runs out, ``stop()`` turns true or every source has failed."""
    live = list(zip(sources, snapshots))
    deadline = clock() + timeout_s
    while live and not stop():
        for pair in list(live):
            source, snap = pair
            try:
                link = source.find_link(account_email, snap)
            except InboxError as e:
                out(f"Login inbox {source.address}: {e} — no longer watching it.")
                live.remove(pair)
                continue
            if link:
                return link
        if clock() >= deadline:
            return None
        sleep(poll_s)
    return None


# --- CLI -----------------------------------------------------------------------


def inbox_command(argv: list[str]) -> None:
    import argparse
    import getpass
    import sys

    from claude_swap.printer import accent, dimmed, error
    from claude_swap.switcher import ClaudeAccountSwitcher

    parser = argparse.ArgumentParser(
        prog="aswap inbox",
        description=(
            "Mailboxes aswap login reads claude.ai sign-in links from. Forward each "
            "account's mail from mail.anthropic.com to one of them with your mail "
            "provider's rules."
        ),
    )
    sub = parser.add_subparsers(dest="action")
    add = sub.add_parser("add", help="connect a mailbox (several are fine)")
    kinds = add.add_subparsers(dest="kind")
    g = kinds.add_parser("google", help="Gmail / Google Workspace, read-only OAuth (gmail.readonly)")
    g.add_argument("--client-file", metavar="PATH",
                   help="OAuth client JSON downloaded from Google Cloud (Desktop app)")
    g.add_argument("--client-id")
    g.add_argument("--client-secret")
    g.add_argument("--hint", metavar="EMAIL", help="the address to sign in with")
    o = kinds.add_parser("outlook", help="Outlook.com / Microsoft 365, read-only OAuth (Mail.Read)")
    o.add_argument("--client-id", help="Application (client) ID of your Entra app registration")
    o.add_argument("--tenant", help="common (default), consumers, organizations or a tenant ID")
    o.add_argument("--hint", metavar="EMAIL", help="the address to sign in with")
    i = kinds.add_parser("imap", help="any IMAP server, with a (app) password")
    i.add_argument("user", metavar="EMAIL")
    i.add_argument("--host", default=DEFAULT_HOST, help=f"IMAP server (default {DEFAULT_HOST})")
    i.add_argument("--port", type=int, default=993)
    i.add_argument("--password-stdin", action="store_true",
                   help="read the password from stdin instead of prompting")
    sub.add_parser("list", help="the connected mailboxes (never their credentials)")
    sub.add_parser("test", help="read each connected mailbox once")
    rm = sub.add_parser("remove", help="disconnect a mailbox and delete its credentials")
    rm.add_argument("address", nargs="?", metavar="EMAIL")
    rm.add_argument("--all", action="store_true", help="every mailbox")
    args = parser.parse_args(argv)
    if args.action is None:
        parser.error("a subcommand is required: add, list, test, remove")

    root = ClaudeAccountSwitcher().backup_dir

    if args.action == "add":
        if args.kind is None:
            add.error("a mailbox kind is required: google, outlook, imap")
        try:
            if args.kind == "imap":
                if args.password_stdin:
                    password = sys.stdin.readline().strip()
                else:
                    password = getpass.getpass(f"IMAP password for {args.user}: ").strip()
                password = password.replace(" ", "")  # Google shows app passwords in groups of 4
                if not password:
                    raise InboxError("no password given")
                config = InboxConfig(user=args.user, password=password, host=args.host, port=args.port)
                with Inbox(config):
                    pass
                row = imap_row(config)
            else:
                from claude_swap import inbox_oauth

                row = inbox_oauth.connect(root, args, out=print)
        except InboxError as e:
            error(f"Error: {e} — nothing saved")
            sys.exit(1)
        add_source(root, row)
        print(f"{accent('Connected')} {row['address']} ({row['kind']})")
        return

    rows = load_sources(root)
    if args.action == "remove":
        if not args.address and not args.all:
            rm.error("name a mailbox, or pass --all")
        n = remove_sources(root, None if args.all else args.address)
        print(f"Disconnected {n} mailbox(es)" if n else "No such mailbox")
        if n and any(r["kind"] != "imap" for r in rows):
            print(dimmed("  aswap's access stays granted until you remove it in the account's "
                         "security settings (Google: myaccount.google.com/permissions; "
                         "Microsoft: account.live.com/consent/Manage)"))
        return
    if not rows:
        print("No mailbox connected (aswap inbox add google|outlook|imap)")
        sys.exit(1)
    if args.action == "list":
        for r in rows:
            where = f" on {r.get('host', DEFAULT_HOST)}" if r["kind"] == "imap" else ""
            print(f"  {r['address']}  ({r['kind']}{where})")
        return
    failed = False
    for r in rows:
        try:
            source = open_source(root, r)
            try:
                source.snapshot()
            finally:
                source.close()
            print(f"  {accent('OK')} {r['address']} ({r['kind']})")
        except InboxError as e:
            failed = True
            error(f"  {r['address']} ({r['kind']}): {e}")
    if failed:
        sys.exit(1)
