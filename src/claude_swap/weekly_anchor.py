"""Weekly reset anchors: per-account memory of when the 7-day window resets.

Every account's weekly window resets at a fixed weekday and time (UTC) that
survives cancelling and resubscribing — observed across Sep–Oct 2026 for all
15 accounts, including one removed from aswap and added back. The usage
endpoint reports it as ``seven_day.resets_at`` on every poll, even for an
idle window, so it costs nothing to record.

The store is MEMORY, not cache: rows are keyed by identity (email +
organization uuid), never by slot, and nothing that manipulates slots
deletes them. Only :meth:`WeeklyAnchorStore.forget` (``aswap billing
forget-anchor``) and ``aswap purge`` do. That is what lets the resubscribe
planner reason about accounts that are not currently in aswap at all.

Planner model: a monthly subscription started at ``S`` pays for
``[S, S + 1 month)``. A fresh subscription starts at 0 % weekly usage, and
the window resets at every anchor occurrence inside the period, so the
period holds ``1 + (resets inside it)`` weekly quotas. Starting shortly
before a reset gains one quota versus starting just after it.
"""

from __future__ import annotations

import calendar
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from claude_swap.fsutil import read_text_with_retry
from claude_swap.locking import FileLock
from claude_swap.settings import atomic_write_json

_logger = logging.getLogger("claude-swap")

Identity = tuple[str, str]

FILE_NAME = "weekly_anchors.json"
VERSION = 1
HISTORY_CAP = 12
# Anchors are minute-precise but the API jitters by under a second around the
# hour; anything beyond this is a real move worth flagging.
MOVE_TOLERANCE_MIN = 2
# One observation per weekly window: a re-read of the same window differs by
# milliseconds, a new week by 7 days.
_SAME_WINDOW_S = 3600
# Bound the write rate: an unchanged anchor refreshes lastSeen at most this often.
_LAST_SEEN_REFRESH_S = 6 * 3600
_WEEK_MIN = 7 * 24 * 60
DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def identity_key(identity: Identity) -> str:
    email, org = identity
    return f"{email}|{org or ''}"


def _parse(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def anchor_from_reset(resets_at: object) -> dict | None:
    """``{"weekday": 0-6 (Mon=0), "hhmm": "HH:MM"}`` in UTC, or None.

    Rounded to the nearest minute: the API reports ``21:59:59.96`` for a
    22:00 reset.
    """
    dt = _parse(resets_at)
    if dt is None:
        return None
    dt = (dt + timedelta(seconds=30)).replace(second=0, microsecond=0)
    return {"weekday": dt.weekday(), "hhmm": dt.strftime("%H:%M")}


def _valid_anchor(anchor: object) -> bool:
    if not isinstance(anchor, dict):
        return False
    wd, hhmm = anchor.get("weekday"), anchor.get("hhmm")
    if not isinstance(wd, int) or isinstance(wd, bool) or not 0 <= wd <= 6:
        return False
    if not isinstance(hhmm, str) or len(hhmm) != 5 or hhmm[2] != ":":
        return False
    try:
        h, m = int(hhmm[:2]), int(hhmm[3:])
    except ValueError:
        return False
    return 0 <= h < 24 and 0 <= m < 60


def minute_of_week(anchor: dict) -> int:
    return anchor["weekday"] * 1440 + int(anchor["hhmm"][:2]) * 60 + int(anchor["hhmm"][3:])


def minute_distance(a: dict, b: dict) -> int:
    d = abs(minute_of_week(a) - minute_of_week(b)) % _WEEK_MIN
    return min(d, _WEEK_MIN - d)


def next_occurrence(anchor: dict, now: datetime) -> datetime:
    """The first anchor instant strictly after ``now`` (UTC)."""
    now = now.astimezone(timezone.utc)
    week_start = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    candidate = week_start + timedelta(minutes=minute_of_week(anchor))
    while candidate <= now:
        candidate += timedelta(days=7)
    return candidate


def anchor_label(anchor: dict, *, local: bool = True) -> str:
    """``Thu 23:00 UTC (Thu 19:00 local)``."""
    utc = f"{DAY_NAMES[anchor['weekday']]} {anchor['hhmm']} UTC"
    if not local:
        return utc
    when = next_occurrence(anchor, datetime.now(timezone.utc)).astimezone()
    return f"{utc} ({DAY_NAMES[when.weekday()]} {when:%H:%M} local)"


def add_months(anchor: datetime, months: int) -> datetime:
    """``anchor`` plus whole months, clamping the day to the month's end."""
    index = anchor.month - 1 + months
    year, month = anchor.year + index // 12, index % 12 + 1
    day = min(anchor.day, calendar.monthrange(year, month)[1])
    return anchor.replace(year=year, month=month, day=day)


# --- tiers -------------------------------------------------------------------

UNKNOWN_TIER = "unknown"


def normalize_tier(raw: object) -> str:
    """``default_claude_max_20x`` → ``max_20x``; anything unusable → unknown.

    Each plan tier has its own weekly schedule for the same account, and the
    schedule comes back when the account returns to that tier (seen on a
    20x → 5x downgrade), so anchors are remembered per tier.
    """
    if not isinstance(raw, str) or not raw.strip():
        return UNKNOWN_TIER
    tier = raw.strip().lower()
    for prefix in ("default_claude_", "default_"):
        if tier.startswith(prefix):
            tier = tier[len(prefix):]
            break
    return tier or UNKNOWN_TIER


def tier_label(tier: str) -> str:
    return {"max_20x": "20x", "max_5x": "5x", "ai": "free", "pro": "Pro"}.get(tier, tier)


# --- store -------------------------------------------------------------------


def _new_tier() -> dict:
    return {"anchor": None, "observations": [], "moved": False, "firstSeen": None, "lastSeen": None}


def _new_row(identity: Identity) -> dict:
    return {
        "email": identity[0],
        "organizationUuid": identity[1] or "",
        "tier": None,
        "tiers": {},
        "plan": None,
        "subscriptionStatus": None,
        "subscriptionCreatedAt": None,
        "lastSlot": None,
        "removedAt": None,
        "lastSubscriptionEnd": None,
        "periodStart": None,
        "periodSource": None,
        "lapse": None,
    }


# ---- billing period memory ----------------------------------------------------
#
# The OAuth profile's ``subscription_created_at`` is the FIRST purchase and is
# kept when a lapsed subscription is bought again (measured 2026-10-18:
# legal@/support@ bought again Oct 17 still report Jul 16), so renewals
# estimated from it are wrong for every resubscribed account. What resets the
# monthly cycle is observable here instead: a lapsed account answers the usage
# endpoint with HTTP 403 and its profile status leaves active/trialing; the
# first success after a CONFIRMED lapse is the new purchase (to within one
# poll). ``aswap billing paid`` records it by hand when aswap was not
# watching. Rows keep ``periodStart`` (ISO) + ``periodSource``
# ("resubscribed" | "paid") and, while lapsed, ``lapse`` =
# {since, last, confirmed}.

_RENEWING = frozenset({"active", "trialing"})
# A 403 streak this long is a lapse, not a blip (lapsed accounts are polled
# every few minutes and answer 403 for days).
LAPSE_CONFIRM_S = 3600.0


def _lapse_confirmed(row: dict) -> bool:
    lapse = row.get("lapse")
    if isinstance(lapse, dict):
        if lapse.get("confirmed"):
            return True
        since, last = _parse(lapse.get("since")), _parse(lapse.get("last"))
        if since and last and (last - since).total_seconds() >= LAPSE_CONFIRM_S:
            return True
    status = row.get("subscriptionStatus")
    return status is not None and status not in _RENEWING


def _lapsed_state(row: dict) -> bool:
    return row.get("lapse") is not None or (
        row.get("subscriptionStatus") is not None and row["subscriptionStatus"] not in _RENEWING
    )


def _resume(row: dict, now_iso: str) -> bool:
    """A usable account after a lapse; a CONFIRMED lapse starts a new period."""
    if not _lapsed_state(row):
        return False
    resumed = _lapse_confirmed(row)
    row["lapse"] = None
    if resumed:
        row["periodStart"] = now_iso
        row["periodSource"] = "resubscribed"
    return True


def period_start(row: dict | None) -> tuple[str, str] | None:
    """``(iso, source)`` of the latest known billing-period start, or None."""
    if not row or not isinstance(row.get("periodStart"), str):
        return None
    return row["periodStart"], row.get("periodSource") or "paid"


def _upgrade_row(row: dict) -> dict:
    """Seed/legacy rows kept one anchor at top level; fold it into ``tiers``."""
    if isinstance(row.get("tiers"), dict):
        return row
    tier = row.get("tier") or UNKNOWN_TIER
    legacy = {k: row.pop(k, None) for k in ("anchor", "observations", "moved", "firstSeen", "lastSeen")}
    legacy["observations"] = legacy["observations"] or []
    legacy["moved"] = bool(legacy["moved"])
    row["tiers"] = {tier: legacy} if _valid_anchor(legacy["anchor"]) else {}
    row["tier"] = row.get("tier") or (tier if row["tiers"] else None)
    return row


def current_tier(row: dict | None) -> dict | None:
    """The tier block for the account's most recent tier (or its only one)."""
    if not row:
        return None
    tiers = row.get("tiers") or {}
    block = tiers.get(row.get("tier") or "")
    if block is None and len(tiers) == 1:
        block = next(iter(tiers.values()))
    return block if block and _valid_anchor(block.get("anchor")) else None


def current_anchor(row: dict | None) -> dict | None:
    block = current_tier(row)
    return block["anchor"] if block else None


def off_schedule(row: dict | None, tier: object, resets_at: object) -> bool:
    """The reading misses the remembered anchor of the tier it would be filed under.

    Each tier keeps its own weekly schedule, so this is the first sign of a
    plan change (5x <-> 20x) — usually long before the cached tier is re-read.
    """
    anchor = anchor_from_reset(resets_at)
    if anchor is None or not row:
        return False
    name = normalize_tier(tier)
    tiers = row.get("tiers") or {}
    if name == UNKNOWN_TIER and row.get("tier") in tiers:
        name = row["tier"]
    block = tiers.get(name)
    if not block or not _valid_anchor(block.get("anchor")):
        return False
    return minute_distance(block["anchor"], anchor) > MOVE_TOLERANCE_MIN


class WeeklyAnchorStore:
    """``<root>/weekly_anchors.json``: identity → {tier → anchor history}."""

    def __init__(self, root: Path, clock=time.time) -> None:
        self.root = Path(root)
        self.path = self.root / FILE_NAME
        self.clock = clock

    def _lock(self) -> FileLock:
        return FileLock(self.root / ".weekly_anchors.lock")

    def rows(self) -> dict[str, dict]:
        try:
            raw = json.loads(read_text_with_retry(self.path))
        except (OSError, ValueError, UnicodeDecodeError):
            return {}
        rows = raw.get("identities") if isinstance(raw, dict) else None
        if not isinstance(rows, dict):
            return {}
        return {k: _upgrade_row(v) for k, v in rows.items() if isinstance(v, dict)}

    def _save(self, rows: dict[str, dict]) -> None:
        atomic_write_json(self.path, {"version": VERSION, "identities": rows})

    def get(self, identity: Identity) -> dict | None:
        return self.rows().get(identity_key(identity))

    def observe(
        self,
        identity: Identity,
        resets_at: object,
        slot: int | None,
        tier: object = None,
    ) -> None:
        """Record one ``seven_day.resets_at`` reading for the account's tier."""
        anchor = anchor_from_reset(resets_at)
        if anchor is None:
            return
        tier_name = normalize_tier(tier)
        reset_dt = _parse(resets_at)
        now = self.clock()
        now_iso = _iso(now)
        key = identity_key(identity)
        with self._lock():
            rows = self.rows()
            row = rows.get(key)
            changed = False
            if row is None:
                row = rows[key] = _new_row(identity)
                changed = True
            tiers = row.setdefault("tiers", {})
            if tier_name != UNKNOWN_TIER and tier_name not in tiers and UNKNOWN_TIER in tiers:
                # Readings taken before the tier was known belong to the first
                # tier we learn — tiers change rarely, readings every poll.
                tiers[tier_name] = tiers.pop(UNKNOWN_TIER)
                changed = True
            if tier_name == UNKNOWN_TIER and row.get("tier") in tiers:
                tier_name = row["tier"]  # unknown now, but we know what it was
            block = tiers.get(tier_name)
            if block is None:
                block = tiers[tier_name] = _new_tier()
                block["firstSeen"] = now_iso
                changed = True
            if row.get("tier") != tier_name:
                row["tier"] = tier_name
                changed = True
            obs = block.setdefault("observations", [])
            last = _parse(obs[-1].get("resetsAt")) if obs and isinstance(obs[-1], dict) else None
            if last is None or abs((last - reset_dt).total_seconds()) > _SAME_WINDOW_S:
                obs.append({"resetsAt": reset_dt.isoformat(), "seenAt": now_iso})
                del obs[:-HISTORY_CAP]
                changed = True
            if not _valid_anchor(block.get("anchor")):
                block["anchor"] = anchor
                changed = True
            elif minute_distance(block["anchor"], anchor) > MOVE_TOLERANCE_MIN:
                block["anchor"] = anchor
                block["moved"] = True
                changed = True
            if slot is not None and row.get("lastSlot") != slot:
                row["lastSlot"] = slot
                changed = True
            if row.get("removedAt"):
                row["removedAt"] = None
                changed = True
            seen = _parse(block.get("lastSeen"))
            if changed or seen is None or now - seen.timestamp() > _LAST_SEEN_REFRESH_S:
                block["lastSeen"] = now_iso
                self._save(rows)

    def _update(self, identity: Identity, fields: dict) -> None:
        key = identity_key(identity)
        with self._lock():
            rows = self.rows()
            row = rows.get(key)
            if row is None:
                row = rows[key] = _new_row(identity)
            if all(row.get(k) == v for k, v in fields.items()):
                return
            row.update(fields)
            self._save(rows)

    def note_subscription(
        self,
        identity: Identity,
        status: str | None,
        period_end: str | None,
        slot: int | None = None,
        *,
        plan: str | None = None,
        created_at: str | None = None,
    ) -> None:
        fields: dict = {"subscriptionStatus": status}
        if period_end:
            fields["lastSubscriptionEnd"] = period_end
        if slot is not None:
            fields["lastSlot"] = slot
        if plan:
            fields["plan"] = plan
        if created_at:
            fields["subscriptionCreatedAt"] = created_at
        key = identity_key(identity)
        now_iso = _iso(self.clock())
        with self._lock():
            rows = self.rows()
            row = rows.get(key)
            if row is None:
                row = rows[key] = _new_row(identity)
            before = json.dumps(row, sort_keys=True)
            if status is not None and status not in _RENEWING:
                lapse = row.get("lapse") if isinstance(row.get("lapse"), dict) else {"since": now_iso}
                lapse.update(last=now_iso, confirmed=True)
                row["lapse"] = lapse
            elif status in _RENEWING:
                _resume(row, now_iso)  # reads the PREVIOUS status: before the update
            row.update(fields)
            if json.dumps(row, sort_keys=True) != before:
                self._save(rows)

    def note_forbidden(self, identity: Identity, slot: int | None = None) -> None:
        """The usage endpoint answered 403: the subscription is not active."""
        key = identity_key(identity)
        now = self.clock()
        now_iso = _iso(now)
        with self._lock():
            rows = self.rows()
            row = rows.get(key)
            if row is None:
                row = rows[key] = _new_row(identity)
            lapse = row.get("lapse") if isinstance(row.get("lapse"), dict) else None
            if lapse is None:
                row["lapse"] = {"since": now_iso, "last": now_iso, "confirmed": False}
            else:
                last = _parse(lapse.get("last"))
                if lapse.get("confirmed") and last and now - last.timestamp() < 3600:
                    return  # already known; refresh "last" at most hourly
                lapse["last"] = now_iso
                if _lapse_confirmed(row):
                    lapse["confirmed"] = True
            if slot is not None:
                row["lastSlot"] = slot
            self._save(rows)

    def note_usable(self, identity: Identity) -> bool:
        """A successful usage poll; True when it ended a confirmed lapse."""
        key = identity_key(identity)
        row = self.rows().get(key)
        if row is None or not _lapsed_state(row):
            return False  # the common case: one file read, no lock, no write
        now_iso = _iso(self.clock())
        with self._lock():
            rows = self.rows()
            row = rows.get(key)
            if row is None or not _resume(row, now_iso):
                return False
            resumed = row.get("periodStart") == now_iso and row.get("periodSource") == "resubscribed"
            if resumed:
                row["subscriptionStatus"] = "active"
            self._save(rows)
            return resumed

    def set_period_start(self, identity: Identity, when: str | None, source: str = "paid") -> None:
        """Record (or with ``when=None`` clear) the current period's start."""
        self._update(identity, {"periodStart": when, "periodSource": source if when else None})

    def mark_removed(self, identity: Identity, slot: int) -> None:
        if self.get(identity) is None:
            return  # nothing remembered; nothing to annotate
        self._update(identity, {"removedAt": _iso(self.clock()), "lastSlot": slot})

    def forget(self, identity: Identity) -> bool:
        key = identity_key(identity)
        with self._lock():
            rows = self.rows()
            if key not in rows:
                return False
            del rows[key]
            self._save(rows)
            return True

    def merge_rows(self, incoming: object) -> int:
        """Merge rows from another machine's export; returns rows changed."""
        if not isinstance(incoming, dict):
            return 0
        changed = 0
        with self._lock():
            rows = self.rows()
            for key, theirs in incoming.items():
                if not isinstance(key, str) or not isinstance(theirs, dict):
                    continue
                theirs = _upgrade_row(json.loads(json.dumps(theirs)))
                if not isinstance(theirs.get("email"), str) or not _clean_tiers(theirs):
                    continue
                mine = rows.get(key)
                merged = _merge_row(mine, theirs) if mine else theirs
                if merged != mine:
                    rows[key] = merged
                    changed += 1
            if changed:
                self._save(rows)
        return changed


def _clean_tiers(row: dict) -> dict:
    out = {}
    for name, block in (row.get("tiers") or {}).items():
        if isinstance(block, dict) and _valid_anchor(block.get("anchor")):
            block = dict(block)
            block["observations"] = [
                o
                for o in block.get("observations") or []
                if isinstance(o, dict) and _parse(o.get("resetsAt"))
            ][-HISTORY_CAP:]
            out[name] = block
    row["tiers"] = out
    return out


def _merge_tier(mine: dict | None, theirs: dict) -> dict:
    if mine is None:
        return theirs
    newer, older = (
        (theirs, mine)
        if (theirs.get("lastSeen") or "") > (mine.get("lastSeen") or "")
        else (mine, theirs)
    )
    merged = dict(older)
    merged.update({k: v for k, v in newer.items() if v is not None})
    by_window: dict[str, dict] = {}
    for o in (older.get("observations") or []) + (newer.get("observations") or []):
        bucket = (_parse(o["resetsAt"]) + timedelta(minutes=30)).strftime("%Y-%m-%dT%H")
        by_window.setdefault(bucket, o)
    merged["observations"] = sorted(by_window.values(), key=lambda o: o["resetsAt"])[-HISTORY_CAP:]
    merged["moved"] = bool(
        mine.get("moved")
        or theirs.get("moved")
        or minute_distance(mine["anchor"], theirs["anchor"]) > MOVE_TOLERANCE_MIN
    )
    firsts = [b.get("firstSeen") for b in (mine, theirs) if b.get("firstSeen")]
    merged["firstSeen"] = min(firsts) if firsts else None
    return merged


def _row_seen(row: dict) -> str:
    return max((b.get("lastSeen") or "" for b in (row.get("tiers") or {}).values()), default="")


def _merge_row(mine: dict, theirs: dict) -> dict:
    _clean_tiers(mine)
    newer = theirs if _row_seen(theirs) > _row_seen(mine) else mine
    merged = {**mine, **{k: v for k, v in newer.items() if k != "tiers" and (v is not None or k in ("removedAt", "lapse"))}}
    starts = [r for r in (mine, theirs) if isinstance(r.get("periodStart"), str)]
    if starts:  # the latest known period start wins, whichever machine saw it
        latest = max(starts, key=lambda r: _parse(r["periodStart"]) or datetime.min.replace(tzinfo=timezone.utc))
        merged["periodStart"], merged["periodSource"] = latest["periodStart"], latest.get("periodSource")
    tiers = dict(mine.get("tiers") or {})
    for name, block in (theirs.get("tiers") or {}).items():
        tiers[name] = _merge_tier(tiers.get(name), block)
    merged["tiers"] = tiers
    return merged


# --- planner -----------------------------------------------------------------


def windows_for(start: datetime, anchor: dict) -> list[tuple[datetime, datetime]]:
    """Weekly windows inside the monthly period paid for at ``start``."""
    end = add_months(start, 1)
    windows = []
    cursor = start
    reset = next_occurrence(anchor, start)
    while reset < end:
        windows.append((cursor, reset))
        cursor = reset
        reset += timedelta(days=7)
    windows.append((cursor, end))
    return windows


def _usable(windows: list[tuple[datetime, datetime]], min_window_h: float) -> int:
    return sum(1 for a, b in windows if (b - a).total_seconds() >= min_window_h * 3600)


# Hours it takes to burn one full weekly quota. The weekly window holds about
# three and a half 5-hour windows (live readings, 2026-10-25: +21 points of 5h
# for +6 of 7d), so burning one back to back takes ~16h; 18h leaves margin.
# A paid period's first and last windows shorter than this are not counted.
MIN_WINDOW_H = 18.0
# Every phase of the weekly cycle is reached within a week; one more day
# covers a month-length change (a 31-day period instead of 30) near the end
# of a month.
_SEARCH_HORIZON_H = 8 * 24


def _hours(windows: list[tuple[datetime, datetime]]) -> list[float]:
    return [round((b - a).total_seconds() / 3600, 1) for a, b in windows]


def resub_plan(
    anchor: dict,
    now: datetime,
    min_window_h: float = MIN_WINDOW_H,
) -> dict:
    """Best time to (re)subscribe so one paid month holds the most weekly quotas.

    A month can hold six: a short window before the first reset, four full
    weeks, and a short tail after the last reset, when both short ends are
    long enough (``min_window_h``) to burn a quota. Searched hour by hour
    over the next eight days; among starts reaching the most quotas, the one
    whose shorter end is longest wins (most margin to burn both ends), then
    the earliest.
    """
    now = now.astimezone(timezone.utc)
    first = next_occurrence(anchor, now)
    top = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    candidates = [now] + [top + timedelta(hours=h) for h in range(_SEARCH_HORIZON_H)]
    best = None
    for start in candidates:
        wins = windows_for(start, anchor)
        quotas = _usable(wins, min_window_h)
        edge = min((wins[0][1] - wins[0][0]), (wins[-1][1] - wins[-1][0]))
        score = (quotas, edge)
        if best is None or score > best[0]:
            best = (score, start, wins)
    (quotas, _edge), start, wins = best
    return {
        "recommendedAt": start,
        "quotas": quotas,
        "windows": _hours(wins),
        "periodDays": (add_months(start, 1) - start).days,
        "quotasIfNow": _usable(windows_for(now, anchor), min_window_h),
        "nextReset": first,
    }


def renewal_plan(
    anchor: dict,
    renews_at: datetime,
    min_window_h: float = MIN_WINDOW_H,
) -> dict:
    """Let a subscription renew, or cancel it and resubscribe at the best time?

    Renewing keeps the old billing date, whatever phase of the weekly cycle
    it falls on. Lapsing at the renewal and buying again later costs a gap
    with no plan but can start the next month where it holds more quotas.
    Compared per day (gap included), not just per payment.
    """
    renews_at = renews_at.astimezone(timezone.utc)
    keep = windows_for(renews_at, anchor)
    keep_days = (add_months(renews_at, 1) - renews_at).total_seconds() / 86400
    alt = resub_plan(anchor, renews_at, min_window_h)
    gap_days = (alt["recommendedAt"] - renews_at).total_seconds() / 86400
    keep_quotas = _usable(keep, min_window_h)
    return {
        "renewsAt": renews_at,
        "renewQuotas": keep_quotas,
        "renewWindows": _hours(keep),
        "resubAt": alt["recommendedAt"],
        "resubQuotas": alt["quotas"],
        "gapDays": round(gap_days, 1),
        # Lapse only when it buys more quotas for the same payment and not
        # fewer per day once the unpaid gap is counted.
        "lapse": alt["quotas"] > keep_quotas
        and alt["quotas"] / (gap_days + alt["periodDays"]) >= keep_quotas / keep_days,
    }


# Weekly utilization from which a 5x week counts as spent: the rest is
# minutes of work, cheaper to lose than to delay the upgrade for.
UPGRADE_SPENT_PCT = 95.0


def upgrade_plan(
    *,
    now: datetime,
    weekly_used_pct: float | None,
    weekly_reset: datetime | None,
    period_end: datetime | None = None,
    min_window_h: float = MIN_WINDOW_H,
) -> dict:
    """When to move a 5x account to 20x.

    Upgrading resets the account's usage and keeps its weekly schedule, so the
    5x week burned before the upgrade comes on top of a full 20x week. The
    best moment is right after the 5x week is spent, while enough of the week
    is left to burn the fresh 20x quota. Spent too close to the reset: burn
    the next 5x week first, then upgrade.
    """
    now = now.astimezone(timezone.utc)
    if weekly_used_pct is None or weekly_reset is None:
        return {"action": "unknown", "reason": "weekly usage not read yet"}
    to_reset_h = (weekly_reset - now).total_seconds() / 3600
    if weekly_used_pct < UPGRADE_SPENT_PCT:
        return {
            "action": "burn",
            "reason": f"burn the 5x week first ({100 - weekly_used_pct:.0f}% left); "
            "upgrading resets it",
            "at": None,
        }
    if to_reset_h >= min_window_h:
        at, action = now, "now"
        reason = f"5x week spent, {to_reset_h:.0f}h left to burn a fresh 20x week"
    else:
        at, action = weekly_reset + timedelta(hours=min_window_h), "after-reset"
        reason = (
            f"only {to_reset_h:.0f}h left this week: burn the next 5x week "
            "first, then upgrade"
        )
    if period_end is not None and (period_end - at).total_seconds() < min_window_h * 3600:
        return {"action": "skip", "reason": "the paid period ends before an upgrade pays off", "at": None}
    return {"action": action, "reason": reason, "at": at}


# --- views -------------------------------------------------------------------


def weekly_view(row: dict | None, now: datetime | None = None) -> dict | None:
    """JSON/display view of an identity's remembered schedule (current tier first)."""
    block = current_tier(row)
    if block is None:
        return None
    now = now or datetime.now(timezone.utc)
    tiers = {
        name: {
            "anchor": b["anchor"],
            "moved": bool(b.get("moved")),
            "observations": len(b.get("observations") or []),
        }
        for name, b in (row.get("tiers") or {}).items()
        if _valid_anchor(b.get("anchor"))
    }
    return {
        "tier": row.get("tier"),
        "anchor": block["anchor"],
        "nextAt": next_occurrence(block["anchor"], now).isoformat(),
        "moved": bool(block.get("moved")),
        "tiers": tiers,
    }


def weekly_text(view: dict | None) -> str | None:
    """``Thu 23:00 UTC (Thu 19:00 local) · 20x  [5x: Wed 05:00 UTC]``."""
    if not view:
        return None
    text = anchor_label(view["anchor"])
    tier = view.get("tier")
    if tier and tier != UNKNOWN_TIER:
        text += f" · {tier_label(tier)}"
    if view.get("moved"):
        text += " · moved"
    others = [
        f"{tier_label(name)}: {DAY_NAMES[t['anchor']['weekday']]} {t['anchor']['hhmm']} UTC"
        for name, t in (view.get("tiers") or {}).items()
        if name not in (tier, UNKNOWN_TIER)
    ]
    if others:
        text += f"  [{'; '.join(others)}]"
    return text
