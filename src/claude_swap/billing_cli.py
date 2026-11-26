"""``aswap billing login|status|tier|plan|paid|forget|forget-anchor``."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from claude_swap import billing_session as bs
from claude_swap import token_push as tp
from claude_swap import weekly_anchor as wa
from claude_swap.billing import billing_text, has_no_plan, limit_resets_text, live_resets_used, resets_used_text
from claude_swap.printer import accent, dimmed, error, muted


def _switcher(debug: bool):
    from claude_swap.switcher import ClaudeAccountSwitcher

    return ClaudeAccountSwitcher(debug=debug)


def _login(sw, args) -> None:
    from claude_swap.locking import FileLock
    from claude_swap.switcher import AccountNotFoundError

    mint = not args.no_token
    account = getattr(args, "account", None)

    # --- resolve identity: existing account, or open login for anyone ---
    existing_num = None
    if account:
        try:
            existing_num, email, org = sw.resolve_account(account)
        except AccountNotFoundError:
            email, org = account, ""  # treat as email hint for a new account

    if existing_num:
        # Known account: constrained capture.
        print(f"Account-{existing_num}: {accent(email)}")
        identity = (email, org or "")
        if mint:
            key, browser = bs.capture_session(
                identity, sw.backup_dir, chrome=args.chrome,
                timeout_s=args.timeout, keep_browser=True,
            )
        else:
            key = bs.capture_session(
                identity, sw.backup_dir, chrome=args.chrome,
                timeout_s=args.timeout,
            )
            browser = None
    else:
        # New or unknown account: accept whoever signs in.
        captured = bs.capture_any_session(
            sw.backup_dir, email_hint=account, chrome=args.chrome,
            timeout_s=args.timeout, keep_browser=mint,
        )
        key = captured.session_key
        email = captured.email
        org = captured.org_uuid
        browser = captured.browser
        print(f"Signed in as: {accent(email)}")
        # Check if this account already exists under a different identifier.
        identity = (email, org or "")
        seq = sw._get_sequence_data() or {}
        for slot_num, rec in seq.get("accounts", {}).items():
            if rec.get("email", "").lower() == email.lower():
                existing_num = slot_num
                break

    # --- save the billing session ---
    sw._sessions.put(identity, key)
    view = None
    if existing_num:
        info = [i for i in sw._build_accounts_info() if str(i[0]) == existing_num]
        sw._billing.refresh(info, force=True)
        view = sw._billing_views(info).get(existing_num)
        print(f"{accent('Saved')} claude.ai session for Account-{existing_num} ({email})")
        print(f"  {billing_text(view) or 'no billing data returned'}")
        rec = sw._billing.records({existing_num: identity}).get(existing_num) or {}
        if rec.get("source") != "exact":
            print(dimmed(f"  subscription_details keys: {rec.get('detailKeys')}"))
    else:
        print(f"{accent('Saved')} claude.ai session for {email} (new account)")

    if mint and browser is not None and has_no_plan(view):
        # claude.ai would show "a Pro or Max plan is required" and never call back.
        browser.close()
        browser = None
        _say_no_plan(email)

    # --- mint an OAuth token and store credentials ---
    if mint and browser is not None:
        minted = False

        def fresh(new_key: str) -> None:
            # A reauth sign-in revoked the session just saved; keep the new one.
            nonlocal key
            key = new_key
            sw._sessions.put(identity, new_key)

        try:
            credentials = tp.mint_credentials(
                browser, sw.backup_dir, email=email, on_session=fresh,
            )
            # Chrome is only needed again for another mint; closed, it frees
            # its memory through the pacing wait before each host's token.
            browser.close()
            if existing_num:
                # Update existing account credentials in place.
                with FileLock(sw.lock_file):
                    sw._write_account_credentials(
                        existing_num, email, credentials, reason="login-mint",
                    )
                    sw._usage_store.clear_dead_token(
                        [existing_num], {existing_num: identity},
                    )
                print(f"{accent('Minted')} Claude Code token for Account-{existing_num}")
            else:
                # New account: inject into live files and call add_account.
                _inject_and_add(sw, email, org, credentials)
            minted = True
        except bs.NoPlan:
            _say_no_plan(email)
        except bs.OAuthError as e:
            error(f"OAuth token minting failed: {e}")
            if existing_num:
                print(dimmed("  session was saved; run 'aswap add' after 'claude /login' to add CC credentials separately"))
            else:
                print(dimmed("  session was saved but the account was not added to aswap"))
                print(dimmed(f"  to add it manually: claude /login as {email}, then aswap add"))
        finally:
            browser.close()  # already closed on success; this covers a failed mint
        # Each other machine gets a token of its own (a shared refresh token
        # would be spent by whichever machine refreshes first).
        hosts = () if args.no_push else tp.configured_hosts(sw.backup_dir, args.push_to)
        if minted and hosts:
            try:
                tp.push_to_hosts(
                    lambda: bs.open_signed_in(sw.backup_dir, email, key, chrome=args.chrome),
                    sw.backup_dir, email, key, hosts, on_session=fresh,
                )
            except bs.SignedOut as e:
                error(f"No tokens pushed ({e}); run 'aswap token push {email}'")
    elif not existing_num and not mint:
        print(dimmed(f"  session saved; to add this account: claude /login as {email}, then aswap add"))


def _say_no_plan(email: str) -> None:
    error(f"No Claude Code token: {email} has no paid plan (Claude Code needs Pro or Max)")
    print(dimmed("  the claude.ai session was saved; buy a plan, then run 'aswap login' again for the token"))


def _inject_and_add(sw, email: str, org_uuid: str, credentials: str) -> None:
    """Write minted credentials to the live files so ``add_account`` can pick them up."""
    from claude_swap.credentials import get_credentials_path

    cred_path = get_credentials_path()
    config_path = sw._get_claude_config_path()

    # Back up whatever's there now.
    old_creds = None
    old_config = None
    try:
        old_creds = cred_path.read_text(encoding="utf-8")
    except (OSError, FileNotFoundError):
        pass
    try:
        old_config = config_path.read_text(encoding="utf-8")
    except (OSError, FileNotFoundError):
        pass

    try:
        # Write the minted credential.
        cred_path.parent.mkdir(parents=True, exist_ok=True)
        _write_private(cred_path, credentials)

        # Write a config that ``add_account`` can read for identity fields.
        import json as _json

        config_blob = _json.dumps({
            "oauthAccount": {
                "emailAddress": email,
                "accountUuid": "",
                "organizationUuid": org_uuid or None,
                "organizationName": None,
            }
        })
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(config_blob, encoding="utf-8")

        sw.add_account()
    finally:
        # Restore original files so we don't clobber the live CC login.
        if old_creds is not None:
            _write_private(cred_path, old_creds)
        elif cred_path.exists():
            cred_path.unlink()
        if old_config is not None:
            config_path.write_text(old_config, encoding="utf-8")
        elif config_path.exists():
            config_path.unlink()


def _write_private(path, text: str) -> None:
    """Write a credential file readable by the owner only (0o600 off Windows)."""
    import os

    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    if os.name != "nt":
        os.chmod(path, 0o600)


def _refresh(sw, args) -> None:
    """Re-read billing now and say whether a cancellation is confirmed."""
    import time as _time

    from claude_swap.billing import confirmed_end_ts

    info = sw._build_accounts_info()
    if args.accounts:
        wanted = {sw.resolve_account(a)[0] for a in args.accounts}
        info = [i for i in info if str(i[0]) in wanted]
    sw._billing.refresh(info, force=True)
    views = sw._billing_views(info)
    for num, email, _on, org, _a, _c, _al in info:
        key = str(num)
        record = sw._billing.records({key: (email, org or "")}).get(key)
        confirmed = confirmed_end_ts(record, _time.time()) is not None
        session = sw._billing.session_state((email, org or "")) or "none"
        if confirmed:
            note = accent("  cancellation confirmed: auto treats the end as its reset")
        elif session != "ok":
            note = dimmed("  (no claude.ai session: a cancellation cannot be confirmed; aswap login)")
        else:
            note = ""
        print(f"  {num}: {email}  {billing_text(views.get(key)) or 'unknown'}{note}")


def _status(sw, args) -> None:
    info = sw._build_accounts_info()
    billing = sw._billing_views(info)
    weekly = sw._weekly_views(info)
    rows = []
    for num, email, _on, org, _a, _c, _al in info:
        identity = (email, org or "")
        rows.append({
            "number": num,
            "email": email,
            "session": sw._billing.session_state(identity),
            "billing": billing.get(str(num)),
            "weeklyReset": weekly.get(str(num)),
            "resetsUsed": live_resets_used(sw._anchors.get(identity)),
        })
    present = {f"{r['email']}|{(i[3] or '')}" for r, i in zip(rows, info)}
    removed = [
        {"email": r["email"], "lastSlot": r.get("lastSlot"), "weeklyReset": wa.weekly_view(r),
         "resetsUsed": live_resets_used(r)}
        for k, r in sw._anchors.rows().items()
        if k not in present
    ]
    if args.json:
        print(json.dumps({"accounts": rows, "remembered": removed}, indent=2))
        return
    for r in rows:
        print(f"  {r['number']}: {r['email']}  {muted('session: ' + (r['session'] or 'none'))}")
        print(f"     Weekly: {wa.weekly_text(r['weeklyReset']) or 'unknown'}")
        print(f"     Bills:  {billing_text(r['billing']) or 'unknown'}{dimmed(_basis_text(r['billing']))}")
        resets = limit_resets_text(r["billing"])
        if resets:
            print(f"     Resets: {resets}")
        used = resets_used_text({"resetsUsed": r["resetsUsed"]})
        if used:
            print(dimmed(f"     Resets used: {used}"))
    if removed:
        print(dimmed("Remembered (not in aswap):"))
        for r in removed:
            print(f"  {r['email']} (was slot {r['lastSlot']})  Weekly: {wa.weekly_text(r['weeklyReset']) or 'unknown'}")
            used = resets_used_text({"resetsUsed": r["resetsUsed"]})
            if used:
                print(dimmed(f"     Resets used: {used}"))


_BASIS = {
    "subscription-created": "first purchase",
    "resubscribed": "resubscribe detected",
    "paid": "purchase recorded",
}


def _basis_text(view: dict | None) -> str:
    basis = (view or {}).get("basis") or {}
    at = basis.get("at")
    if not at or (view or {}).get("source") == "exact":
        return ""
    try:
        when = datetime.fromisoformat(str(at).replace("Z", "+00:00")).astimezone()
    except ValueError:
        return ""
    return f"  (from {_BASIS.get(basis.get('source'), basis.get('source'))} {when:%b %d %H:%M})"


def _parse_when(text: str | None) -> datetime:
    """Local date/time (or now); a bare date means local noon."""
    if not text:
        return datetime.now(timezone.utc)
    value = datetime.fromisoformat(text.strip())
    if len(text.strip()) <= 10:
        value = value.replace(hour=12)
    if value.tzinfo is None:
        value = value.astimezone()  # interpret as local time
    return value.astimezone(timezone.utc)


def _identities_for(sw, account: str) -> list[tuple[str, str]]:
    """An account in aswap (NUM|EMAIL), else remembered identities by email."""
    try:
        _num, email, org = sw.resolve_account(account)
        return [(email, org or "")]
    except Exception:
        hits = [k for k in sw._anchors.rows() if k.split("|")[0].lower() == account.lower()]
        return [tuple(k.split("|", 1)) for k in hits]


def _tier(sw, args) -> None:
    info = sw._build_accounts_info()
    if args.accounts:
        wanted = {sw.resolve_account(a)[0] for a in args.accounts}
        info = [i for i in info if str(i[0]) in wanted]
    result = sw._billing.check_tiers(info)
    rows = [{"number": str(i[0]), "email": i[1], **result.get(str(i[0]), {})} for i in info]
    if args.json:
        print(json.dumps(rows, indent=2))
        return
    for r in rows:
        if r.get("error"):
            print(f"  {r['number']}: {r['email']}  {muted('unknown (' + r['error'] + ')')}")
            continue
        text = wa.tier_label(wa.normalize_tier(r.get("tier")))
        status = r.get("status")
        if status and status not in ("active", "trialing"):
            text += f" · {status.replace('_', ' ')}"
        before = r.get("previousTier")
        if before and before != r.get("tier"):
            text = f"{accent(text)}  (was {wa.tier_label(wa.normalize_tier(before))})"
        print(f"  {r['number']}: {r['email']}  {text}")


def _paid(sw, args) -> None:
    from claude_swap.billing import next_renewal

    identities = _identities_for(sw, args.account)
    if not identities:
        error(f"Error: no account or remembered identity matches {args.account}")
        sys.exit(1)
    when = None if args.clear else _parse_when(args.when)
    now = datetime.now(timezone.utc)
    for identity in identities:
        sw._anchors.set_period_start(identity, when.isoformat() if when else None)
        if when is None:
            print(f"cleared the recorded purchase for {identity[0]}")
            continue
        nxt = next_renewal(when, now)
        print(
            f"{accent('Recorded')} purchase for {identity[0]} at "
            f"{when.astimezone():%a %b %d %H:%M} local -> renews ~{nxt.astimezone():%a %b %d}"
        )


def _cancelled(sw, args) -> None:
    """Record a cancel made on claude.ai, for accounts aswap cannot read."""
    when = None if args.clear else _parse_when(args.at)
    recorded: set[str] = set()
    for account in args.accounts:
        identities = _identities_for(sw, account)
        if not identities:
            error(f"Error: no account or remembered identity matches {account}")
            sys.exit(1)
        for identity in identities:
            sw._anchors.set_cancelled(identity, when.isoformat() if when else None)
            recorded.add(identity[0].lower())
            if when is None:
                print(f"cleared the recorded cancel for {identity[0]}")
            else:
                print(f"{accent('Recorded')} cancel for {identity[0]} at {when.astimezone():%a %b %d %H:%M} local")
    if when is None:
        return
    info = sw._build_accounts_info()
    views = sw._billing_views(info)
    for num, email, *_ in info:
        if email.lower() in recorded:
            print(f"  {num}: {email}  {billing_text(views.get(str(num))) or 'unknown'}")


def _parse_ts(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _upgrade_for(entry, period_end, now, min_window_h) -> dict:
    last = getattr(entry, "last_good", None) if entry is not None else None
    seven = (last or {}).get("seven_day") if isinstance(last, dict) else None
    seven = seven if isinstance(seven, dict) else {}
    used = seven.get("pct")
    return wa.upgrade_plan(
        now=now,
        weekly_used_pct=float(used) if isinstance(used, (int, float)) else None,
        weekly_reset=_parse_ts(seven.get("resets_at")),
        period_end=period_end,
        min_window_h=min_window_h,
    )


def _plan(sw, args) -> None:
    now = datetime.now(timezone.utc)
    info = sw._build_accounts_info()
    by_key = {f"{i[1]}|{i[3] or ''}": str(i[0]) for i in info}
    billing = sw._billing_views(info)
    entries = sw.usage_entries_by_account(fetch=set())
    out = []
    for key, row in sw._anchors.rows().items():
        anchor = wa.current_anchor(row)
        if anchor is None:
            continue
        num = by_key.get(key)
        view = billing.get(num) if num else None
        status = (view or {}).get("subscriptionStatus") or row.get("subscriptionStatus")
        tier = (view or {}).get("rateLimitTier") or row.get("tier")
        renewal = upgrade = None
        if num is None:
            state = "removed"
        elif sw.is_account_disabled(num):
            # Disabled = retired (banned, dead login): no buy or renew advice.
            if not args.all:
                continue
            state = "disabled"
        elif (view or {}).get("endsAt"):
            state = "ending"
        elif status in (None, "active", "trialing"):
            state = "active"
            renews = _parse_ts((view or {}).get("nextBillingAt"))
            if renews is not None:
                renewal = wa.renewal_plan(anchor, renews, args.min_window_hours)
            if wa.normalize_tier(tier) == "max_5x":
                upgrade = _upgrade_for(entries.get(num), renews, now, args.min_window_hours)
            worth_showing = (renewal or {}).get("lapse") or (upgrade or {}).get("action") in ("now", "after-reset")
            if not (args.all or worth_showing):
                continue
        else:
            state = "lapsed"
        plan = wa.resub_plan(anchor, now, args.min_window_hours)
        item = {
            "email": row["email"],
            "slot": num or row.get("lastSlot"),
            "state": state,
            "tier": tier,
            "anchor": wa.anchor_label(anchor, local=False),
            "anchorLocal": wa.anchor_label(anchor),
            "nextResetInHours": round((plan["nextReset"] - now).total_seconds() / 3600, 1),
            "recommendedAt": plan["recommendedAt"].isoformat(),
            "quotas": plan["quotas"],
            "quotasIfNow": plan["quotasIfNow"],
            "windowsHours": plan["windows"],
            "resetsUsed": live_resets_used(row),
        }
        if renewal is not None:
            item["renewal"] = {
                **renewal,
                "renewsAt": renewal["renewsAt"].isoformat(),
                "resubAt": renewal["resubAt"].isoformat(),
            }
        if upgrade is not None:
            item["upgrade"] = {**upgrade, "at": upgrade["at"].isoformat() if upgrade.get("at") else None}
        out.append(item)
    out.sort(key=lambda r: (r["state"] == "active", r["recommendedAt"]))
    if args.json:
        print(json.dumps(out, indent=2))
        return
    if not out:
        print(dimmed("Nothing to buy, renew differently or upgrade right now."))
        return
    for r in out:
        print(f"  {r['slot']}: {r['email']}  [{r['state']}, {wa.tier_label(wa.normalize_tier(r['tier']))}]  weekly reset {r['anchorLocal']}")
        if r["state"] not in ("active", "disabled"):
            when =datetime.fromisoformat(r["recommendedAt"]).astimezone()
            print(f"     buy now -> {r['quotasIfNow']} weekly quotas (next reset in {r['nextResetInHours']}h)")
            print(f"     best: {when:%a %b %d %H:%M} local -> {r['quotas']} quotas (windows h: {r['windowsHours']})")
            used = resets_used_text({"resetsUsed": r.get("resetsUsed")})
            if used:
                print(dimmed(f"     resets already used (gone if brought back before they expire): {used}"))
        ren = r.get("renewal")
        if ren:
            renews = datetime.fromisoformat(ren["renewsAt"]).astimezone()
            line = f"     renews {renews:%a %b %d %H:%M}: {ren['renewQuotas']} quotas next month"
            if ren["lapse"]:
                resub = datetime.fromisoformat(ren["resubAt"]).astimezone()
                line += accent(
                    f" -> cancel before it renews, buy again {resub:%a %b %d %H:%M} for "
                    f"{ren['resubQuotas']} ({ren['gapDays']:.1f}-day gap)"
                )
            print(line)
        up = r.get("upgrade")
        if up:
            text = {"now": accent("upgrade to 20x now"), "after-reset": "upgrade to 20x later",
                    "burn": "keep on 5x", "skip": "do not upgrade", "unknown": "upgrade: unknown"}[up["action"]]
            if up.get("at") and up["action"] == "after-reset":
                text += f" ({datetime.fromisoformat(up['at']).astimezone():%a %b %d %H:%M})"
            print(f"     {text}: {up['reason']}")


def billing_command(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="aswap billing", description="Exact billing, weekly resets, resubscribe planning.")
    parser.add_argument("--debug", action="store_true")
    sub = parser.add_subparsers(dest="action")
    p = sub.add_parser("login", help="sign in: capture session + mint CC token (opens Chrome)")
    p.add_argument("account", nargs="?", metavar="NUM|EMAIL",
                   help="existing slot/email, or a new email; omit to sign in as anyone")
    p.add_argument("--chrome", metavar="PATH")
    # 600 s: a claude.ai sign-in code stays valid for ten minutes.
    p.add_argument("--timeout", type=float, default=None,
                   help="seconds to wait for the sign-in (default 600; 300 with --auto)")
    p.add_argument("--auto", action="store_true",
                   help="hands-free re-login of an existing account: the sign-in link is read "
                        "from the login mailboxes (aswap inbox), only the slot is updated, and "
                        "the first wall (no mail, captcha, phone check) ends it")
    p.add_argument("--no-token", action="store_true",
                   help="skip minting a Claude Code OAuth token (session only)")
    p.add_argument("--push-to", metavar="HOST[,HOST]",
                   help="also give these SSH hosts a token of their own (default: login.pushTo)")
    p.add_argument("--no-push", action="store_true", help="skip login.pushTo for this login")
    p = sub.add_parser("status", help="sessions, billing and weekly reset per account")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser(
        "refresh",
        help="re-read billing now (e.g. right after cancelling, to confirm it for auto)",
    )
    p.add_argument("accounts", nargs="*", metavar="NUM|EMAIL", help="default: every account")
    p = sub.add_parser("tier", help="live plan tier (5x / 20x / free) per account, read now")
    p.add_argument("accounts", nargs="*", metavar="NUM|EMAIL", help="default: every account")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("plan", help="when to buy, renew or upgrade accounts (most weekly quotas per payment)")
    p.add_argument(
        "--min-window-hours", type=float, default=wa.MIN_WINDOW_H,
        help=f"shortest first/last weekly window that still counts as a quota (default {wa.MIN_WINDOW_H:g})",
    )
    p.add_argument("--all", action="store_true", help="include every active account")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("paid", help="record when an account was (re)purchased; renewals count from it")
    p.add_argument("account", metavar="NUM|EMAIL")
    p.add_argument("when", nargs="?", help="purchase date/time, local (YYYY-MM-DD or 'YYYY-MM-DD HH:MM'); default now")
    p.add_argument("--clear", action="store_true", help="forget the recorded purchase")
    p = sub.add_parser(
        "cancelled",
        help="record that accounts were cancelled on claude.ai (for accounts without a billing session)",
    )
    p.add_argument("accounts", nargs="+", metavar="NUM|EMAIL")
    p.add_argument("--at", help="cancel date/time, local (YYYY-MM-DD or 'YYYY-MM-DD HH:MM'); default now")
    p.add_argument("--clear", action="store_true", help="forget the recorded cancel (e.g. resubscribed)")
    p = sub.add_parser("forget", help="drop an account's claude.ai session")
    p.add_argument("account", metavar="NUM|EMAIL")
    p = sub.add_parser("forget-anchor", help="drop a remembered weekly reset")
    p.add_argument("email")
    args = parser.parse_args(argv)
    if args.action is None:
        parser.error("a subcommand is required: login, status, refresh, tier, plan, paid, cancelled, forget, forget-anchor")
    try:
        sw = _switcher(args.debug)
        if args.action == "login" and args.auto:
            from claude_swap import relogin

            relogin.login_auto(
                sw, args.account, chrome=args.chrome,
                timeout_s=args.timeout or relogin.AUTO_TIMEOUT_S,
            )
        elif args.action == "login":
            if args.timeout is None:
                args.timeout = 600.0
            _login(sw, args)
        elif args.action == "status":
            _status(sw, args)
        elif args.action == "refresh":
            _refresh(sw, args)
        elif args.action == "tier":
            _tier(sw, args)
        elif args.action == "plan":
            _plan(sw, args)
        elif args.action == "paid":
            _paid(sw, args)
        elif args.action == "cancelled":
            _cancelled(sw, args)
        elif args.action == "forget":
            _num, email, org = sw.resolve_account(args.account)
            print("forgot session" if sw._sessions.forget((email, org or "")) else "no session stored")
        elif args.action == "forget-anchor":
            hits = [k for k in sw._anchors.rows() if k.split("|")[0].lower() == args.email.lower()]
            for k in hits:
                email, org = k.split("|", 1)
                sw._anchors.forget((email, org))
            print(f"forgot {len(hits)} anchor row(s)")
    except bs.BillingSessionError as e:
        error(f"Error: {e}")
        sys.exit(1)
    except Exception as e:  # noqa: BLE001 - CLI boundary
        from claude_swap.exceptions import ClaudeSwitchError

        if isinstance(e, ClaudeSwitchError):
            error(f"Error: {e}")
            sys.exit(1)
        raise


# --- aswap reset -------------------------------------------------------------

_WEEKLY_KINDS = ("7d", "weekly", "full", "week")


def _pick_grant(grants: list[dict], kind: str) -> list[dict]:
    """The account's grants of ``kind`` ("7d" or a model name), unspent first."""
    want = "Weekly" if kind.lower() in _WEEKLY_KINDS else None
    found = [
        g for g in grants
        if (g["name"] == want if want else g["name"].lower() == kind.lower())
    ]
    return sorted(found, key=lambda g: (g["left"] <= 0, g["expiresAt"] or "9999"))


def _kind_label(name: str) -> str:
    return "7d" if name == "Weekly" else name


def _percents_text(pcts: dict[str, float]) -> str:
    return " · ".join(f"{k} {v:.0f}%" for k, v in pcts.items()) or "unknown"


def reset_command(argv: list[str]) -> None:
    """``aswap reset ACCOUNT [7d|MODEL]``: list or spend a claude.ai limit
    reset ("Reset for free" on Settings > Usage) with the saved session."""
    parser = argparse.ArgumentParser(
        prog="aswap reset",
        description="Use one of an account's claude.ai limit resets (Settings > Usage, "
                    "\"Reset for free\"). Without a kind, list them.",
    )
    parser.add_argument("account", metavar="NUM|EMAIL")
    parser.add_argument(
        "kind", nargs="?", metavar="7d|MODEL",
        help="7d: the full reset (5-hour and weekly limits, per-model ones too); "
             "a model (e.g. fable): that model's reset",
    )
    parser.add_argument("--dry-run", action="store_true", help="say what would be used, use nothing")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)
    try:
        sw = _switcher(args.debug)
        num, email, org = sw.resolve_account(args.account)
        session = sw._sessions.get((email, org or ""))
        if not session or not session.get("sessionKey"):
            error(f"Error: no claude.ai session for {email}: run aswap billing login {email} --no-token")
            sys.exit(1)
        key = session["sessionKey"]
        before = bs.read_usage(key, org)
        grants = bs.reset_grants(before)
        pcts = bs.usage_percents(before)
        if not args.kind:
            remembered = {r["id"]: r for r in live_resets_used(sw._anchors.get((email, org or "")))}
            _print_resets(num, email, grants, pcts, args.json, remembered)
            return
        picked = _pick_grant(grants, args.kind)
        if not picked or picked[0]["left"] <= 0:
            have = ", ".join(f"{_kind_label(g['name'])} ×{g['left']}" for g in grants if g["left"] > 0)
            error(f"Error: {email} has no {args.kind} reset left (has: {have or 'none'})")
            sys.exit(1)
        grant = picked[0]
        if not grant["usableNow"]:
            why = grant["reason"] or "not usable now"
            if why == "cooldown" and grant["cooldownUntil"]:
                until = datetime.fromisoformat(grant["cooldownUntil"]).astimezone()
                why += f" until {until:%H:%M:%S} (a reset was just used)"
            error(f"Error: claude.ai won't use {email}'s {_kind_label(grant['name'])} reset now: {why}")
            sys.exit(1)
        label = _kind_label(grant["name"])
        if args.dry_run:
            print(f"  {num}: {email}  would use its {label} reset ({grant['id']})  now: {_percents_text(pcts)}")
            return
        result = bs.use_limit_reset(key, org, grant["id"])
        used_at = datetime.now(timezone.utc).isoformat()
        after = bs.read_usage(key, org)
        spent = [u for u in bs.parse_used_resets(after) if u["id"] == grant["id"]] or [
            {"id": grant["id"], "name": grant["name"], "used": 1, "total": None, "expiresAt": grant["expiresAt"]}
        ]
        try:
            sw._anchors.note_resets_used((email, org or ""), spent, via="aswap reset", used_at=used_at)
        except Exception:  # noqa: BLE001 - the reset itself went through
            pass
        sw._billing.store_limit_resets((email, org or ""), bs.parse_limit_resets(after))
        after_pcts = bs.usage_percents(after)
        if args.json:
            print(json.dumps({"account": num, "email": email, "used": grant["id"], "kind": label,
                              "before": pcts, "after": after_pcts, "response": result}))
            return
        print(accent(f"  {num}: {email}  used its {label} reset"))
        print(f"     before: {_percents_text(pcts)}")
        print(f"     after:  {_percents_text(after_pcts)}")
        left = [f"{_kind_label(g['name'])} ×{g['left']}" for g in bs.reset_grants(after) if g["left"] > 0]
        print(dimmed(f"     resets left: {', '.join(left) or 'none'}"))
    except bs.BillingSessionError as e:
        error(f"Error: {e}")
        sys.exit(1)
    except Exception as e:  # noqa: BLE001 - CLI boundary
        from claude_swap.exceptions import ClaudeSwitchError

        if isinstance(e, ClaudeSwitchError):
            error(f"Error: {e}")
            sys.exit(1)
        raise


def _print_resets(
    num: str, email: str, grants: list[dict], pcts: dict, as_json: bool, remembered: dict | None = None
) -> None:
    remembered = remembered or {}
    if as_json:
        print(json.dumps({"account": num, "email": email, "usage": pcts, "resets": grants,
                          "used": list(remembered.values())}))
        return
    now = datetime.now(timezone.utc).isoformat()
    grants = [g for g in grants if g["left"] > 0 or (g["expiresAt"] or "9999") > now]
    print(f"  {num}: {email}  now: {_percents_text(pcts)}")
    if not grants:
        print(dimmed("     no limit resets"))
    for g in grants:
        ends = datetime.fromisoformat(g["expiresAt"]).astimezone() if g["expiresAt"] else None
        state = "ready" if g["usableNow"] else (g["reason"] or "not usable now")
        if g["left"] <= 0:
            state = "used"
            seen = resets_used_text({"resetsUsed": [remembered[g["id"]]]}) if g["id"] in remembered else None
            if seen:
                state = seen.split(" ", 1)[1]  # "used Nov 26 16:05 (aswap reset)"
        when = f"  until {ends:%b %d %H:%M}" if ends else ""
        print(f"     {_kind_label(g['name'])} ×{g['left']}{when}  ({state})")
    ready = list(dict.fromkeys(_kind_label(g["name"]).lower() for g in grants if g["left"] > 0 and g["usableNow"]))
    if ready:
        print(dimmed(f"     use one: aswap reset {num} {'|'.join(ready)}"))
