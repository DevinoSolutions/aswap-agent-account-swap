"""Hands-free re-login of accounts whose refresh token died.

``aswap login N --auto`` and ``aswap relogin-dead`` sign an existing account
in again with no hands: aswap's own login browser (nodriver, one profile per
account) enters the email on claude.ai, the magic link claude.ai mails is
read from the connected login mailboxes (``aswap inbox``), opened in that
same browser, and a new Claude Code token is minted and stored in the
account's slot exactly as ``aswap login N`` stores it.

Only the slot moves: the active login (``~/.claude/.credentials.json`` and
``~/.claude.json``) is never written and never switched, so running sessions
are left alone. Anything only a person could finish (no mailbox, no mail,
a bot check, a captcha, phone verification) ends that account's attempt at
once with the reason; nothing is retried, so a wall never becomes a stream of
sign-in mails. Walls that are about this machine rather than one account (a
bot check, a captcha) stop a ``relogin-dead`` run.

Nothing here prints or logs a token, a session key, a sign-in link or a code.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass

from claude_swap import billing_session as bs
from claude_swap import inbox
from claude_swap import token_push as tp
from claude_swap.billing import has_no_plan
from claude_swap.printer import accent, dimmed, error

# A sign-in mail takes about a minute and the link page spins about another.
AUTO_TIMEOUT_S = 300.0
# Between two accounts of one run: never a burst of sign-in requests.
PACE_S = 20.0

OK = "ok"
SKIPPED = "skipped"
FAILED = "failed"


@dataclass(frozen=True)
class DeadAccount:
    num: str
    email: str
    org_uuid: str
    active: bool
    disabled: bool


@dataclass
class Result:
    num: str
    email: str
    status: str
    detail: str = ""
    wall: str | None = None


def dead_accounts(sw) -> list[DeadAccount]:
    """Every slot whose stored refresh token is dead ("re-login needed").

    Read-only: the same fingerprint-bound verdict ``aswap list`` shows, with
    no usage fetch and no refresh.
    """
    info = sw._build_accounts_info()
    identities = {str(n): (e, o or "") for n, e, _on, o, _a, _c, _al in info}
    _threshold, models = sw._poll_policy_inputs()
    entries = sw._usage_store.entries(identities, models)
    data = sw._get_sequence_data() or {}
    out = []
    for num, email, _org_name, org_uuid, active, creds, _alias in info:
        key = str(num)
        entry = entries.get(key)
        if entry is None or not sw._entry_token_dead(entry, key, email, creds, active):
            continue
        out.append(DeadAccount(
            num=key, email=email, org_uuid=org_uuid or "", active=bool(active),
            disabled=bool(sw._disabled_from_data(data, key)),
        ))
    return out


def relogin_account(
    sw, num: str, email: str, org_uuid: str, *,
    chrome: str | None = None, timeout_s: float = AUTO_TIMEOUT_S, out=print,
) -> None:
    """Sign slot ``num`` in again hands-free and store its new token.

    Raises ``billing_session.LoginWall`` at the first wall, ``NoPlan`` when
    the account has no paid plan, ``OAuthError`` when the mint fails.
    Writes only the slot (the backup store) and clears its dead mark.
    """
    from claude_swap.locking import FileLock

    identity = (email, org_uuid or "")
    key, browser = bs.capture_session(
        identity, sw.backup_dir, chrome=chrome, timeout_s=timeout_s,
        keep_browser=True, out=out, strict=True,
    )
    try:
        sw._sessions.put(identity, key)
        account = [i for i in sw._build_accounts_info() if str(i[0]) == str(num)]
        try:
            sw._billing.refresh(account, force=True)
            view = sw._billing_views(account).get(str(num))
        except Exception:  # noqa: BLE001 - billing is a hint here; the mint decides
            view = None
        if has_no_plan(view):
            raise bs.NoPlan(f"{email} has no paid plan (Claude Code needs Pro or Max)")

        def fresh(new_key: str) -> None:
            sw._sessions.put(identity, new_key)

        credentials = tp.mint_credentials(
            browser, sw.backup_dir, out=out, email=email, on_session=fresh, strict=True,
        )
    finally:
        browser.close()
    with FileLock(sw.lock_file):
        sw._write_account_credentials(str(num), email, credentials, reason="login-mint")
        sw._usage_store.clear_dead_token([str(num)], {str(num): identity})


def _say_inbox_missing() -> None:
    error("No login mailbox is connected, so a hands-free sign-in has nowhere to read the link from.")
    print(dimmed("  connect one: aswap inbox add google --token-file PATH | --client-file PATH"))


def require_inbox(root) -> bool:
    if inbox.load_sources(root):
        return True
    _say_inbox_missing()
    return False


def run_one(sw, num: str, email: str, org_uuid: str, *, chrome=None,
            timeout_s: float = AUTO_TIMEOUT_S, out=print) -> Result:
    """``relogin_account`` with every failure turned into a ``Result``."""
    try:
        relogin_account(sw, num, email, org_uuid, chrome=chrome, timeout_s=timeout_s, out=out)
    except bs.LoginWall as e:
        return Result(num, email, FAILED, str(e), wall=e.kind)
    except bs.NoPlan as e:
        return Result(num, email, FAILED, str(e), wall="no-plan")
    except bs.ReauthRequired as e:
        return Result(num, email, FAILED, str(e), wall="reauth")
    except bs.BillingSessionError as e:
        return Result(num, email, FAILED, str(e))
    return Result(num, email, OK, "new token stored")


def login_auto(sw, account: str | None, *, chrome=None, timeout_s: float = AUTO_TIMEOUT_S) -> None:
    """``aswap login <num|email> --auto``."""
    from claude_swap.switcher import AccountNotFoundError

    if not account:
        error("--auto needs an existing account: aswap login <num|email> --auto")
        sys.exit(2)
    try:
        num, email, org = sw.resolve_account(account)
    except AccountNotFoundError:
        error(f"--auto only signs existing accounts in again; {account} is not one (use aswap login)")
        sys.exit(2)
    if not require_inbox(sw.backup_dir):
        sys.exit(1)
    print(f"Account-{num}: {accent(email)}")
    result = run_one(sw, str(num), email, org or "", chrome=chrome, timeout_s=timeout_s)
    _report([result])
    if result.status != OK:
        sys.exit(1)


def _report(results: list[Result]) -> None:
    for r in results:
        mark = accent("OK    ") if r.status == OK else ("skip  " if r.status == SKIPPED else "FAILED")
        wall = f" [{r.wall}]" if r.wall else ""
        print(f"  {mark} Account-{r.num} {r.email}{wall}: {r.detail}")


def relogin_dead_command(argv: list[str], *, sleep=time.sleep) -> None:
    parser = argparse.ArgumentParser(
        prog="aswap relogin-dead",
        description=(
            "Sign every account whose refresh token died in again, hands-free: the "
            "claude.ai sign-in link is read from the connected login mailboxes "
            "(aswap inbox). Only the accounts' slots change; the active login is "
            "never switched."
        ),
    )
    parser.add_argument("accounts", nargs="*", metavar="NUM|EMAIL",
                        help="only these dead accounts (default: every one)")
    parser.add_argument("--dry-run", action="store_true", help="list what would be signed in, do nothing")
    parser.add_argument("--include-disabled", action="store_true",
                        help="also accounts disabled with aswap disable")
    parser.add_argument("--chrome", metavar="PATH")
    parser.add_argument("--timeout", type=float, default=AUTO_TIMEOUT_S,
                        help=f"seconds per account (default {int(AUTO_TIMEOUT_S)})")
    parser.add_argument("--pace", type=float, default=PACE_S,
                        help=f"seconds between two accounts (default {int(PACE_S)})")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)

    from claude_swap.switcher import ClaudeAccountSwitcher

    sw = ClaudeAccountSwitcher(debug=args.debug)
    dead = dead_accounts(sw)
    if args.accounts:
        want = {a.lower() for a in args.accounts}
        dead = [d for d in dead if d.num in want or d.email.lower() in want]
    results: list[Result] = []
    todo: list[DeadAccount] = []
    for d in dead:
        if d.active:
            results.append(Result(d.num, d.email, SKIPPED,
                                  "the active login: sign in with Claude Code /login instead"))
        elif d.disabled and not args.include_disabled:
            results.append(Result(d.num, d.email, SKIPPED, "disabled (--include-disabled to try it)"))
        else:
            todo.append(d)
    if not dead:
        print("No account needs a re-login.")
        return
    if args.dry_run:
        for d in todo:
            results.append(Result(d.num, d.email, SKIPPED, "would sign in (dry run)"))
        _report(sorted(results, key=lambda r: int(r.num) if r.num.isdigit() else 0))
        return
    if todo and not require_inbox(sw.backup_dir):
        sys.exit(1)
    stopped = None
    for i, d in enumerate(todo):
        if stopped is not None:
            results.append(Result(d.num, d.email, SKIPPED, f"run stopped at the {stopped} wall"))
            continue
        if i:
            sleep(args.pace)
        print(f"\nAccount-{d.num}: {accent(d.email)}")
        result = run_one(sw, d.num, d.email, d.org_uuid, chrome=args.chrome, timeout_s=args.timeout)
        _report([result])
        results.append(result)
        if result.wall in bs.WALL_STOPS_RUN:
            stopped = result.wall
    print("\nRe-login results:")
    _report(sorted(results, key=lambda r: int(r.num) if r.num.isdigit() else 0))
    if any(r.status == FAILED for r in results):
        sys.exit(1)
