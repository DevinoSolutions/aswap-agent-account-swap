"""Next-billing-date estimates for Claude subscription accounts.

``/api/oauth/profile`` exposes no renewal date, but it does carry the
organization's ``subscription_created_at``. Stripe renews a monthly
subscription on that anchor's day of month (clamped to the month's last day,
so a Jan 31 anchor bills Feb 28, then Mar 31), which makes the next renewal
derivable. It is an ESTIMATE: an annual plan, or a plan change that reset
the billing anchor, would move the real date, and nothing in the profile
says which applies.

The anchor almost never changes, so it is cached per slot in
``cache/billing.json`` and re-read at most once per ``SUCCESS_TTL_S``. The
fetch uses the slot's stored access token only while it is still valid —
this module never refreshes a token (refresh is the usage path's job, under
its consume gate), so a slot with an expired token simply waits for the
next usage poll to rotate it. The cache is advisory: concurrent writers can
drop each other's update, which costs one extra fetch later.
"""

from __future__ import annotations

import calendar
import json
import logging
import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from claude_swap import oauth
from claude_swap import statedb
from claude_swap.fsutil import replace_with_retry
from claude_swap.weekly_anchor import normalize_tier, tier_label

_logger = logging.getLogger("claude-swap")

SUCCESS_TTL_S = 24 * 3600
EXACT_TTL_S = 6 * 3600
# A lapsed subscription can be bought again any minute, so its row is
# re-checked far sooner than an active one.
LAPSED_TTL_S = 30 * 60
FAILURE_RETRY_S = 3600
# A tier check asked for an off-schedule weekly reading stays open this long;
# an older one is asked again rather than answered by an unrelated fetch.
TIER_CHECK_MAX_AGE_S = 6 * 3600
_MAX_WORKERS = 8
_LAUNCH_STAGGER_S = 0.1
_CACHE_VERSION = 1

# Statuses that still renew on the anchor; anything else (past_due, canceled,
# unpaid, ...) is shown as the status itself.
_RENEWING_STATUSES = frozenset({"active", "trialing"})


def next_renewal(anchor: datetime, now: datetime) -> datetime:
    """First monthly renewal of ``anchor`` strictly after ``now``.

    Each period is computed from the anchor itself, not chained from the
    previous period, so month-end clamping never drifts (Jan 31 → Feb 28 →
    Mar 31).
    """
    months = (now.year - anchor.year) * 12 + (now.month - anchor.month)
    months = max(months, 1)
    while True:
        candidate = _add_months(anchor, months)
        if candidate > now:
            return candidate
        months += 1


def _add_months(anchor: datetime, months: int) -> datetime:
    index = anchor.month - 1 + months
    year, month = anchor.year + index // 12, index % 12 + 1
    day = min(anchor.day, calendar.monthrange(year, month)[1])
    return anchor.replace(year=year, month=month, day=day)


def parse_plan(data: object) -> dict | None:
    """Plan, tier and status of a profile payload — present even on a free account."""
    org = data.get("organization") if isinstance(data, dict) else None
    if not isinstance(org, dict):
        return None
    status = org.get("subscription_status")
    plan = org.get("organization_type")
    tier = org.get("rate_limit_tier")
    return {
        "subscriptionStatus": status if isinstance(status, str) else None,
        "plan": plan if isinstance(plan, str) else None,
        "rateLimitTier": tier if isinstance(tier, str) else None,
    }


def parse_profile(data: object) -> dict | None:
    """The billing fields of a profile payload, or None when it has none."""
    fields = parse_plan(data)
    if fields is None:
        return None
    created = data["organization"].get("subscription_created_at")
    if not isinstance(created, str) or _parse_iso(created) is None:
        return None
    return {"subscriptionCreatedAt": created, **fields}


def looks_lapsed(record: dict | None) -> bool:
    """Whether a cached record says the plan stopped renewing or is free."""
    if not record:
        return False
    status = record.get("subscriptionStatus")
    if status is not None and status not in _RENEWING_STATUSES:
        return True
    return normalize_tier(record.get("rateLimitTier")) == "ai"


def _tier_name(tier: object) -> str:
    return tier_label(normalize_tier(tier))


def _parse_iso(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def billing_view(
    record: dict | None,
    now: float | None = None,
    session_state: str | None = None,
    period_start: tuple[str, str] | None = None,
) -> dict | None:
    """Display/JSON view of a cached record.

    ``source == "exact"`` records come from claude.ai ``subscription_details``:
    the real period end, and whether the subscription is set to end there
    (``endsAt``). Estimate records derive ``nextBillingAt`` monthly from the
    subscription anchor. A lapsed subscription has no next charge.

    The anchor is the LATER of the profile's ``subscriptionCreatedAt`` (the
    first purchase, kept across resubscribing) and ``period_start`` -- the
    remembered start of the current period (``weekly_anchor.period_start``:
    a detected resubscribe, or one set with ``aswap billing paid``).
    ``basis`` says which one the estimate used.
    """
    if not isinstance(record, dict):
        return None
    now_ts = now if now is not None else time.time()
    now_dt = datetime.fromtimestamp(now_ts, tz=timezone.utc)
    status = record.get("subscriptionStatus")
    view: dict = {
        "source": "estimate",
        "subscriptionCreatedAt": record.get("subscriptionCreatedAt"),
        "subscriptionStatus": status,
        "plan": record.get("plan"),
        "rateLimitTier": record.get("rateLimitTier"),
        "nextBillingAt": None,
        "nextBillingEstimated": True,
        "endsAt": None,
        "sessionState": session_state,
    }
    renewing = status is None or status in _RENEWING_STATUSES
    if record.get("source") == "exact":
        period_end = _parse_iso(record.get("currentPeriodEnd") or "")
        if period_end is not None and period_end > now_dt:
            view["source"] = "exact"
            view["nextBillingEstimated"] = False
            if record.get("cancelAtPeriodEnd"):
                view["endsAt"] = period_end.isoformat()
            elif renewing:
                view["nextBillingAt"] = period_end.isoformat()
            return view
        if status is not None and not renewing:
            view["source"] = "exact"
            return view
        # Exact period already passed (not refreshed yet): fall back below.
    anchor = _parse_iso(record.get("subscriptionCreatedAt") or "")
    basis = ("subscription-created", record.get("subscriptionCreatedAt"))
    if period_start is not None:
        remembered = _parse_iso(period_start[0] or "")
        if remembered is not None and (anchor is None or remembered > anchor):
            anchor, basis = remembered, (period_start[1], period_start[0])
    if anchor is None:
        return view if view["source"] == "exact" or status else None
    view["basis"] = {"source": basis[0], "at": basis[1]}
    if renewing:
        view["nextBillingAt"] = next_renewal(anchor, now_dt).isoformat()
    return view


def ends_at_ts(view: dict | None) -> float | None:
    """Epoch of a scheduled subscription end, or None."""
    value = (view or {}).get("endsAt")
    parsed = _parse_iso(value) if isinstance(value, str) else None
    return parsed.timestamp() if parsed else None


def _when(iso: str) -> str:
    countdown, _clock = oauth.format_reset(iso)
    local = datetime.fromisoformat(iso).astimezone()
    return f"{local.strftime(f'%b {local.day} %H:%M'):<12}  in {countdown}"


def billing_text(view: dict | None) -> str | None:
    """One-line summary.

    ``renews Oct 15 02:18  in 21d 20h`` (exact) · ``renews ~Oct 15 …``
    (estimated) · ``ends Oct 12 14:28  in 4d (cancelling)`` · the status.
    Local time, always with the date: a renewal is a calendar event.
    """
    if not view:
        return None
    note = ""
    if view.get("sessionState") == "expired":
        note = "  · session expired: aswap billing login"
    if view.get("endsAt"):
        return f"ends {_when(view['endsAt'])} (cancelling){note}"
    next_at = view.get("nextBillingAt")
    if next_at:
        mark = "~" if view.get("nextBillingEstimated", True) else ""
        return f"renews {mark}{_when(next_at)}{note}"
    status = view.get("subscriptionStatus") or "unknown"
    return f"subscription {status.replace('_', ' ')}{note}"


class BillingCache:
    """Per-slot billing snapshots in ``<cache_dir>/billing.json``.

    Rows are bound to the slot's (email, organization uuid) so a slot that
    changes hands never shows the previous owner's billing date. With a
    claude.ai session (``sessions``) a row is exact; otherwise it carries the
    OAuth-profile anchor estimate. Results are also copied into the
    weekly-anchor memory (``anchors``) so a removed account keeps them.
    """

    def __init__(self, cache_dir: Path, clock=time.time, sessions=None, anchors=None) -> None:
        self.path = Path(cache_dir) / "billing.json"
        self.clock = clock
        self.sessions = sessions
        self.anchors = anchors

    def _load(self) -> dict[str, dict]:
        try:
            raw = json.loads(statedb.read_text(self.path))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return {}
        accounts = raw.get("accounts") if isinstance(raw, dict) else None
        return accounts if isinstance(accounts, dict) else {}

    def _save(self, accounts: dict[str, dict]) -> None:
        payload = {"version": _CACHE_VERSION, "accounts": accounts}
        if statedb.try_write(self.path, json.dumps(payload)):
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".billing-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            replace_with_retry(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @staticmethod
    def _matches(row: object, email: str, org_uuid: str) -> bool:
        return (
            isinstance(row, dict)
            and row.get("email") == email
            and (row.get("organizationUuid") or "") == (org_uuid or "")
        )

    def records(self, identities: dict[str, tuple[str, str]]) -> dict[str, dict | None]:
        """The cached record for each slot whose identity still matches."""
        rows = self._load()
        out: dict[str, dict | None] = {}
        for num, (email, org) in identities.items():
            row = rows.get(num)
            out[num] = row if self._matches(row, email, org) else None
        return out

    def expire(self, identity: tuple[str, str]) -> None:
        """Make the identity's row due now: its subscription just changed."""
        rows = self._load()
        hits = [r for r in rows.values() if self._matches(r, identity[0], identity[1])]
        if not any(r.get("nextCheckAt") for r in hits):
            return
        for row in hits:
            row["nextCheckAt"] = 0
        self._save(rows)

    def session_state(self, identity: tuple[str, str]) -> str | None:
        if self.sessions is None:
            return None
        row = self.sessions.get(identity)
        return row.get("status") if row else None

    def refresh(self, accounts_info: list[tuple], force: bool = False) -> None:
        """Fetch due slots: exact via a claude.ai session, plus the profile.

        ``accounts_info`` rows are ``_build_accounts_info`` tuples. Network
        runs in parallel and must not be called under a credential lock.
        """
        rows = self._load()
        now = self.clock()
        due: dict[str, tuple[str, str, str | None, str | None]] = {}
        for num, email, _org_name, org_uuid, _active, creds, _alias in accounts_info:
            key = str(num)
            row = rows.get(key)
            if (
                not force
                and self._matches(row, email, org_uuid)
                and now < row.get("nextCheckAt", 0)
            ):
                continue
            session = self.sessions.get((email, org_uuid or "")) if self.sessions else None
            session_key = (
                session["sessionKey"]
                if session and session.get("status") != "expired"
                else None
            )
            token = _usable_access_token(creds)
            if token or session_key:
                due[key] = (email, org_uuid or "", token, session_key)
        if not due:
            return

        def fetch(item):
            num, (_email, org, token, session_key) = item
            result: dict = {"exact": None, "profile": None, "session": None}
            if session_key:
                from claude_swap import billing_session as bs

                try:
                    result["exact"] = bs.fetch_subscription_details(session_key, org)
                    result["session"] = "ok"
                except bs.SessionExpired:
                    result["session"] = "expired"
                except Exception as e:  # blocked / network: retry later
                    _logger.debug("subscription_details failed for %s: %r", num, e)
                    result["session"] = "error"
            if token:
                try:
                    result["profile"] = parse_profile(oauth.request_profile_data(token))
                except Exception as e:  # advisory: never break the caller
                    _logger.debug("Billing profile fetch failed for account %s: %r", num, e)
            return num, result

        results = dict(_run_parallel(fetch, list(due.items())))

        rows = self._load()  # re-read: keep other writers' rows for other slots
        now = self.clock()
        for num, result in results.items():
            email, org, _token, _session_key = due[num]
            identity = (email, org)
            previous = rows.get(num)
            row = {"email": email, "organizationUuid": org}
            if self._matches(previous, email, org):
                row = {**previous, **row}  # keep last-good data on failure
            if result["profile"] is not None:
                _log_tier_change(num, email, row.get("rateLimitTier"), result["profile"]["rateLimitTier"])
                row.update(result["profile"])
            exact = result["exact"]
            if exact is not None:
                row.update(
                    source="exact",
                    currentPeriodEnd=exact.get("periodEnd"),
                    cancelAtPeriodEnd=exact.get("cancelAtPeriodEnd"),
                    interval=exact.get("interval"),
                    detailKeys=exact.get("keys"),
                )
                if exact.get("status"):
                    row["subscriptionStatus"] = exact["status"]
            elif result["session"] == "expired" or row.get("source") is None:
                row["source"] = "estimate"
            ok = exact is not None or result["profile"] is not None
            if ok:
                ttl = EXACT_TTL_S if exact is not None else SUCCESS_TTL_S
                status = row.get("subscriptionStatus")
                if status is not None and status not in _RENEWING_STATUSES:
                    ttl = min(ttl, LAPSED_TTL_S)
                row["fetchedAt"] = now
                row["nextCheckAt"] = now + ttl
            else:
                row["nextCheckAt"] = now + FAILURE_RETRY_S
            rows[num] = row
            if self.sessions is not None and result["session"] in ("ok", "expired"):
                try:
                    self.sessions.mark(identity, result["session"])
                except Exception:
                    _logger.debug("session mark failed", exc_info=True)
            if self.anchors is not None and ok:
                try:
                    self.anchors.note_subscription(
                        identity,
                        row.get("subscriptionStatus"),
                        row.get("currentPeriodEnd") if row.get("source") == "exact" else None,
                        int(num) if num.isdigit() else None,
                        plan=row.get("plan"),
                        created_at=row.get("subscriptionCreatedAt"),
                    )
                except Exception:
                    _logger.debug("anchor subscription note failed", exc_info=True)
        try:
            self._save(rows)
        except OSError as e:
            _logger.debug("Billing cache write failed: %r", e)

    def check_tiers(self, accounts_info: list[tuple]) -> dict[str, dict]:
        """Each slot's plan tier, read live from the profile now (no TTL).

        Profile only — one quick request per slot, no claude.ai session — so it
        answers "still 5x or 20x?" right after an upgrade. The cached plan
        fields are updated; a slot whose tier or status changed is made due so
        the next listing re-reads its billing dates as well.
        """
        slots = {str(i[0]): (i[1], i[3] or "", _usable_access_token(i[5])) for i in accounts_info}

        def fetch(item):
            num, (_email, _org, token) = item
            if not token:
                return num, None, "no usable token"
            try:
                fields = parse_plan(oauth.request_profile_data(token))
            except Exception as e:  # advisory: report it, never raise
                return num, None, f"profile fetch failed: {e.__class__.__name__}"
            return num, fields, None if fields else "no organization in profile"

        results = _run_parallel(fetch, list(slots.items())) if slots else []
        rows = self._load()
        out: dict[str, dict] = {}
        for num, fields, err in results:
            email, org, _token = slots[num]
            previous = rows.get(num) if self._matches(rows.get(num), email, org) else None
            before = (previous or {}).get("rateLimitTier")
            entry = {"tier": None, "previousTier": before, "plan": None, "status": None, "error": err}
            if fields is not None:
                entry.update(tier=fields["rateLimitTier"], plan=fields["plan"], status=fields["subscriptionStatus"])
                row = dict(previous) if previous else {"email": email, "organizationUuid": org, "nextCheckAt": 0}
                _log_tier_change(num, email, before, fields["rateLimitTier"])
                if previous and (
                    fields["rateLimitTier"] != before
                    or fields["subscriptionStatus"] != previous.get("subscriptionStatus")
                ):
                    row["nextCheckAt"] = 0
                row.update(fields)
                rows[num] = row
            out[num] = entry
        try:
            self._save(rows)
        except OSError as e:
            _logger.debug("Billing cache write failed: %r", e)
        return out

    def confirm_tier(self, identity: tuple[str, str]) -> bool:
        """Whether the cached tier was read after an off-schedule weekly reading.

        The first call stamps ``tierCheckAskedAt`` and makes the row due; once
        a fetch lands after that stamp the tier is confirmed (and the stamp
        cleared). No row: there is nothing to wait for.
        """
        rows = self._load()
        hits = [r for r in rows.values() if self._matches(r, identity[0], identity[1])]
        if not hits:
            return True
        now = self.clock()
        asked = hits[0].get("tierCheckAskedAt")
        if isinstance(asked, (int, float)) and now - asked < TIER_CHECK_MAX_AGE_S:
            if (hits[0].get("fetchedAt") or 0) < asked:
                return False
            for row in hits:
                row.pop("tierCheckAskedAt", None)
            self._save(rows)
            return True
        for row in hits:
            row.update(tierCheckAskedAt=now, nextCheckAt=0)
        self._save(rows)
        return False


def _run_parallel(fn, items: list) -> list:
    with ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, len(items))) as pool:
        futures = []
        for i, item in enumerate(items):
            if i:
                time.sleep(_LAUNCH_STAGGER_S)  # same request hygiene as usage
            futures.append(pool.submit(fn, item))
        return [f.result() for f in futures]


def _log_tier_change(num: str, email: str, before: object, after: object) -> None:
    if before and after and before != after:
        _logger.info(
            "Account %s (%s) plan tier changed: %s -> %s",
            num, email, _tier_name(before), _tier_name(after),
        )


def _usable_access_token(credentials: str) -> str | None:
    """The stored access token if it is unexpired and allowed to read the profile."""
    data = oauth.extract_oauth_data(credentials or "")
    if not data:
        return None
    token = data.get("accessToken")
    if not isinstance(token, str) or not token:
        return None
    if oauth.is_oauth_token_expired(data.get("expiresAt")):
        return None
    scopes = data.get("scopes")
    if isinstance(scopes, list) and "user:profile" not in scopes:
        return None  # setup-token credentials carry inference scope only
    return token
