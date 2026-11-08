"""``aswap token push|receive``: a Claude Code token of its own for each machine.

A Claude Code refresh token is single-use: whichever machine refreshes first
spends it, and another machine still holding it gets ``invalid_grant``. So a
second machine never gets a copy of this one's token. It gets a token minted
for it from the account's saved claude.ai login, sent over SSH on stdin
(never on a command line) and stored only there.

Every mint is paced: it starts at least a random 60-90 s after the previous
one on this machine, remembered across runs, so back-to-back logins and
pushes never hammer the authorize endpoint.
"""

from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
import time
from pathlib import Path

from claude_swap import billing_session as bs
from claude_swap import oauth, statedb
from claude_swap.printer import accent, dimmed, error
from claude_swap.settings import atomic_write_json, load_login_settings, parse_hosts

MINT_GAP_S = (60.0, 90.0)
PACE_FILE = "oauth_mint_pace.json"


class PushError(Exception):
    """A token could not be delivered to (or stored on) a machine."""


def pace_mint(root: Path, *, out=print, rng=random.uniform, now=time.time, sleep=time.sleep) -> None:
    """Hold off until a random 60-90 s have passed since this machine's last mint."""
    path = Path(root) / PACE_FILE
    try:
        last = float(json.loads(statedb.read_text(path)).get("last", 0))
    except (OSError, ValueError, TypeError, AttributeError, UnicodeDecodeError):
        last = 0.0
    # Capped: a clock that jumped back must not stall a login for hours.
    wait = min(last + rng(*MINT_GAP_S) - now(), MINT_GAP_S[1])
    if wait > 0:
        out(dimmed(f"Waiting {int(wait)}s before the next token (paces the sign-in endpoint)..."))
        sleep(wait)
    atomic_write_json(path, {"last": now()})


def mint_credentials(browser, root: Path, *, out=print) -> str:
    """Mint a fresh token from a signed-in browser; returns Claude Code's credential JSON."""
    pace_mint(root, out=out)
    token_data = bs.mint_oauth_tokens(browser, out=out)
    try:
        profile = oauth.request_profile_data(token_data["access_token"])
    except Exception:  # noqa: BLE001 - plan fields stay unset; the token still works
        profile = None
    return bs.build_credential_blob(token_data, profile)


def send(
    host: str,
    email: str,
    credentials: str,
    session_key: str | None = None,
    *,
    run=subprocess.run,
    timeout: float = 120.0,
) -> str:
    """Hand ``host``'s aswap a token for ``email``; returns its one-line report."""
    payload = json.dumps({"email": email, "credentials": credentials, "sessionKey": session_key})
    argv = ["ssh", "-o", "BatchMode=yes", host, "aswap", "token", "receive"]
    try:
        proc = run(
            argv, input=payload, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise PushError(f"{host}: ssh failed ({type(e).__name__})") from None
    out_lines = [line for line in (proc.stdout or "").splitlines() if line.strip()]
    if proc.returncode != 0:
        err_lines = [line for line in (proc.stderr or "").splitlines() if line.strip()]
        detail = (err_lines or out_lines or [f"exit {proc.returncode}"])[-1].strip()
        raise PushError(f"{host}: {detail}")
    return out_lines[-1].strip() if out_lines else "stored"


def receive(sw, stream=None) -> str:
    """Store a pushed token (and claude.ai login) in this machine's slot for its email."""
    from claude_swap.locking import FileLock
    from claude_swap.switcher import AccountNotFoundError

    stream = stream if stream is not None else sys.stdin
    try:
        payload = json.loads(stream.read())
        email = payload["email"]
        credentials = payload["credentials"]
        blob = json.loads(credentials)["claudeAiOauth"]
        complete = bool(blob["accessToken"]) and bool(blob["refreshToken"])
    except (ValueError, KeyError, TypeError):
        raise PushError("malformed token payload") from None
    if not complete:
        raise PushError("the pushed token has no refresh token")
    # The sender's spelling of the email may differ in case from this store's.
    accounts = (sw._get_sequence_data() or {}).get("accounts", {})
    stored = next(
        (rec["email"] for rec in accounts.values()
         if isinstance(rec.get("email"), str) and rec["email"].lower() == str(email).lower()),
        email,
    )
    try:
        num, email, org = sw.resolve_account(stored)
    except AccountNotFoundError:
        raise PushError(f"{email} is not an account here; add it on this machine first") from None
    identity = (email, org or "")
    with FileLock(sw.lock_file):
        sw._write_account_credentials(num, email, credentials, reason="login-mint")
        sw._usage_store.clear_dead_token([num], {num: identity})
    stored = ["token"]
    key = payload.get("sessionKey")
    if isinstance(key, str) and key:
        sw._sessions.put(identity, key)
        stored.append("claude.ai login")
    return f"Account-{num} ({email}): stored {' and '.join(stored)}"


def push_to_hosts(browser, root: Path, email: str, session_key: str | None, hosts, *, out=print) -> int:
    """Mint one token per host from ``browser`` and send each; returns how many landed."""
    landed = 0
    for host in hosts:
        try:
            credentials = mint_credentials(browser, root, out=out)
            report = send(host, email, credentials, session_key)
        except (bs.BillingSessionError, PushError) as e:
            error(f"{host}: no token pushed ({e})")
            continue
        out(f"{accent('Pushed')} its own token to {host}: {report}")
        landed += 1
    return landed


def configured_hosts(root: Path, override: str | None = None) -> tuple[str, ...]:
    return parse_hosts(override if override is not None else load_login_settings(root).push_to)


def _push(sw, args) -> int:
    hosts = configured_hosts(sw.backup_dir, args.to)
    if not hosts:
        error("Error: no hosts; pass --to HOST[,HOST] or set login.pushTo")
        return 1
    if args.all:
        targets = [
            (str(i[0]), i[1], i[3] or "")
            for i in sw._build_accounts_info()
            if not sw.is_account_disabled(str(i[0]))
        ]
    elif args.accounts:
        targets = [sw.resolve_account(a) for a in args.accounts]
    else:
        error("Error: give an account, or --all")
        return 1
    failed = 0
    for num, email, org in targets:
        identity = (email, org or "")
        print(f"Account-{num}: {accent(email)}")
        key = (sw._sessions.get(identity) or {}).get("sessionKey")
        if not key:
            print(dimmed(f"  no saved claude.ai login; run 'aswap login {email}' first"))
            failed += len(hosts)
            continue
        try:
            browser = bs.open_signed_in(sw.backup_dir, email, key, chrome=args.chrome)
        except bs.BillingSessionError as e:
            error(f"  could not open its browser ({e})")
            failed += len(hosts)
            continue
        try:
            landed = push_to_hosts(browser, sw.backup_dir, email, key, hosts)
        finally:
            browser.close()
        failed += len(hosts) - landed
    return 1 if failed else 0


def token_command(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog="aswap token",
        description="Give other machines a Claude Code token of their own, over SSH.",
    )
    parser.add_argument("--debug", action="store_true")
    sub = parser.add_subparsers(dest="action")
    p = sub.add_parser(
        "push",
        help="mint a separate token per host from the saved claude.ai login and send it",
    )
    p.add_argument("accounts", nargs="*", metavar="NUM|EMAIL")
    p.add_argument("--all", action="store_true", help="every account")
    p.add_argument("--to", metavar="HOST[,HOST]", help="default: the login.pushTo setting")
    p.add_argument("--chrome", metavar="PATH")
    sub.add_parser("receive", help="store a pushed token read from stdin (run by push over SSH)")
    args = parser.parse_args(argv)
    if args.action is None:
        parser.error("a subcommand is required: push, receive")

    from claude_swap.exceptions import ClaudeSwitchError
    from claude_swap.switcher import ClaudeAccountSwitcher

    try:
        sw = ClaudeAccountSwitcher(debug=args.debug)
        if args.action == "receive":
            print(receive(sw))
            return
        sys.exit(_push(sw, args))
    except (PushError, bs.BillingSessionError, ClaudeSwitchError) as e:
        error(f"Error: {e}")
        sys.exit(1)
