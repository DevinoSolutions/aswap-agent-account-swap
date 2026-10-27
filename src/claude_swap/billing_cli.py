"""``aswap billing login|status|tier|plan|paid|forget|forget-anchor``."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from claude_swap import billing_session as bs
from claude_swap import weekly_anchor as wa
from claude_swap.billing import billing_text
from claude_swap.printer import accent, dimmed, error, muted


def _switcher(debug: bool):
    from claude_swap.switcher import ClaudeAccountSwitcher

    return ClaudeAccountSwitcher(debug=debug)


def _login(sw, args) -> None:
    num, email, org = sw.resolve_account(args.account)
    identity = (email, org or "")
    key = bs.capture_session(identity, sw.backup_dir, chrome=args.chrome, timeout_s=args.timeout)
    sw._sessions.put(identity, key)
    info = [i for i in sw._build_accounts_info() if str(i[0]) == num]
    sw._billing.refresh(info, force=True)
    view = sw._billing_views(info).get(num)
    print(f"{accent('Saved')} claude.ai session for Account-{num} ({email})")
    print(f"  {billing_text(view) or 'no billing data returned'}")
    rec = sw._billing.records({num: identity}).get(num) or {}
    if rec.get("source") != "exact":
        print(dimmed(f"  subscription_details keys: {rec.get('detailKeys')}"))


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
        })
    present = {f"{r['email']}|{(i[3] or '')}" for r, i in zip(rows, info)}
    removed = [
        {"email": r["email"], "lastSlot": r.get("lastSlot"), "weeklyReset": wa.weekly_view(r)}
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
    if removed:
        print(dimmed("Remembered (not in aswap):"))
        for r in removed:
            print(f"  {r['email']} (was slot {r['lastSlot']})  Weekly: {wa.weekly_text(r['weeklyReset']) or 'unknown'}")


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
            "nextResetInHours": round((plan["nextReset"] - now).total_seconds() / 3600, 1),
            "recommendedAt": plan["recommendedAt"].isoformat(),
            "quotas": plan["quotas"],
            "quotasIfNow": plan["quotasIfNow"],
            "windowsHours": plan["windows"],
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
        print(f"  {r['slot']}: {r['email']}  [{r['state']}, {wa.tier_label(wa.normalize_tier(r['tier']))}]  weekly reset {r['anchor']}")
        if r["state"] not in ("active", "disabled"):
            when =datetime.fromisoformat(r["recommendedAt"]).astimezone()
            print(f"     buy now -> {r['quotasIfNow']} weekly quotas (next reset in {r['nextResetInHours']}h)")
            print(f"     best: {when:%a %b %d %H:%M} local -> {r['quotas']} quotas (windows h: {r['windowsHours']})")
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
    p = sub.add_parser("login", help="capture a claude.ai session for an account (opens Chrome)")
    p.add_argument("account", metavar="NUM|EMAIL")
    p.add_argument("--chrome", metavar="PATH")
    p.add_argument("--timeout", type=float, default=300.0)
    p = sub.add_parser("status", help="sessions, billing and weekly reset per account")
    p.add_argument("--json", action="store_true")
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
    p = sub.add_parser("forget", help="drop an account's claude.ai session")
    p.add_argument("account", metavar="NUM|EMAIL")
    p = sub.add_parser("forget-anchor", help="drop a remembered weekly reset")
    p.add_argument("email")
    args = parser.parse_args(argv)
    if args.action is None:
        parser.error("a subcommand is required: login, status, tier, plan, paid, forget, forget-anchor")
    try:
        sw = _switcher(args.debug)
        if args.action == "login":
            _login(sw, args)
        elif args.action == "status":
            _status(sw, args)
        elif args.action == "tier":
            _tier(sw, args)
        elif args.action == "plan":
            _plan(sw, args)
        elif args.action == "paid":
            _paid(sw, args)
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
