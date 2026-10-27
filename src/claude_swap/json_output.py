"""Serialization helpers for ``--json`` structured output.

Centralizes the schema-v1 shapes so ``--list``/``--status``/``--switch`` agree on
field names (camelCase, matching the export envelope in transfer.py) and on how the
internal usage dict is projected to JSON. Callers build payloads here; the CLI does
the single ``json.dumps`` (see cli.py).
"""

from __future__ import annotations

from datetime import datetime, timezone

from claude_swap import oauth, pace, poll_policy, usage_store

# Bump only on a breaking change to any payload shape. Scripts key off this.
SCHEMA_VERSION = 1

# Sentinel entries that ``_collect_usage`` / ``_fetch_active_usage`` yield in place
# of a usage dict. Kept here (the serialization hub) so the human renderer and the
# JSON projection agree instead of scattering raw strings.
USAGE_NO_CREDENTIALS = "no credentials"
USAGE_TOKEN_EXPIRED = "token expired"
# API-key (``/login`` managed key) accounts have no subscription quota; usage is
# reported as this sentinel instead of being fetched from the OAuth usage API.
USAGE_API_KEY = "api key"
# The active account's macOS Keychain was unreadable (locked / denied / timeout)
# with no plaintext fallback — distinct from a genuinely empty slot, so the user
# isn't misled into an unnecessary re-login.
USAGE_KEYCHAIN_UNAVAILABLE = "keychain unavailable"
# The stored refresh-token lineage is dead (repeated ``invalid_grant``). The
# account is quarantined from fetching until a re-login (``aswap login`` / ``add``)
# replaces the credential; distinct from "token expired" (which Claude Code can
# refresh on its own) because only the user can fix it.
USAGE_RELOGIN_REQUIRED = "re-login needed"
# The profile oracle proved the live credential belongs to a DIFFERENT account
# than the slot's identity (foreign credential under a stale config — partial
# cross-machine sync or a mid-``/login`` poll). Its quota is not this slot's, so
# recording it would poison history and autoswitch decisions; distinct from
# "token expired" because holding is wrong here — a switch repairs the drift
# (stash the foreign credential, restore the slot's backup), so autoswitch
# should treat the active as unknown-headroom and fail over.
USAGE_FOREIGN_CREDENTIAL = "foreign credential"


def _window_to_json(entry: dict) -> dict:
    """Project a 5h/7d usage window to JSON, preserving raw ``resetsAt``.

    ``countdown``/``clock`` are recomputed from ``resets_at`` at serialization
    time (the store may serve a measurement hours after its fetch); entries
    without ``resets_at`` fall back to the fetch-time strings.
    """
    out: dict = {"pct": entry["pct"]}
    if "resets_at" in entry:
        out["resetsAt"] = entry["resets_at"]
    cell = oauth.fresh_reset_strings(entry)
    if cell:
        out["countdown"], out["clock"] = cell
    return out


def _pace_fields(entry: dict, fetched_at: float | None) -> dict:
    """Weekly-window pace fields (issue #125): additive, JSON-only.

    Emitted only when pace is computable and not suppressed (see
    ``claude_swap.pace.compute_pace``). ``projectedExhaustionAt`` is a linear
    ETA — wide error bars against real, bursty usage — so it's kept out of
    every human-facing surface and only ever appears here.
    """
    if fetched_at is None:
        return {}
    result = pace.compute_pace(entry, fetched_at=fetched_at)
    if result is None:
        return {}
    out: dict = {
        "expectedPct": round(result.expected_pct, 1),
        "aheadOfPace": result.ahead,
    }
    eta = pace.projected_exhaustion_ts(result, fetched_at=fetched_at)
    if eta is not None:
        out["projectedExhaustionAt"] = (
            datetime.fromtimestamp(eta, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        )
    will_last = pace.will_last_to_reset(result)
    if will_last is not None:
        out["willLastToReset"] = will_last
    return out


def _weekly_window_to_json(entry: dict, fetched_at: float | None) -> dict:
    """A 7d/scoped window's JSON projection, with pace fields layered in."""
    out = _window_to_json(entry)
    out.update(_pace_fields(entry, fetched_at))
    return out


def _scoped_window_to_json(entry: dict, fetched_at: float | None) -> dict:
    """Project a per-model scoped weekly window, carrying its model name."""
    out = _weekly_window_to_json(entry, fetched_at)
    out["name"] = entry["name"]
    return out


def usage_to_json(usage: dict, fetched_at: float | None = None) -> dict:
    """Convert the internal usage dict to its camelCase JSON projection.

    Sub-keys are emitted only when present in the source (the API does not always
    return every window or pay-as-you-go spend). ``fetched_at`` is the
    measurement's fetch time; passing it adds pace fields to the weekly
    windows (``seven_day``, ``weekly``, ``scoped``) only — never ``five_hour``
    (issue #125).
    """
    out: dict = {}
    if "five_hour" in usage:
        out["fiveHour"] = _window_to_json(usage["five_hour"])
    if "seven_day" in usage:
        out["sevenDay"] = _weekly_window_to_json(usage["seven_day"], fetched_at)
    if "weekly" in usage:
        # Codex's canonical weekly window is the same 7-day cadence as
        # Claude's seven_day, so it earns the same pace fields.
        out["weekly"] = _weekly_window_to_json(usage["weekly"], fetched_at)
    if "reset_credits" in usage:
        resets = usage["reset_credits"]
        resets_out = {"available": resets["available"]}
        if "expires_at" in resets:
            resets_out["earliestExpiresAt"] = resets["expires_at"]
        out["resetCredits"] = resets_out
    if "spend" in usage:
        spend = usage["spend"]
        spend_out: dict = {
            "used": spend["used"],
            "limit": spend["limit"],
            "pct": spend["pct"],
            "currency": spend["currency"],
        }
        if "resets_at" in spend:
            spend_out["resetsAt"] = spend["resets_at"]
        cell = oauth.fresh_reset_strings(spend)
        if cell:
            spend_out["countdown"], spend_out["clock"] = cell
        out["spend"] = spend_out
    if "scoped" in usage:
        out["scoped"] = [_scoped_window_to_json(w, fetched_at) for w in usage["scoped"]]
    return out


def usage_fields(
    entry: dict | str | None, fetched_at: float | None = None
) -> tuple[str, dict | None]:
    """Map a collected usage entry to ``(usageStatus, usage|None)``.

    A collected entry is one of: a usage dict, the ``USAGE_TOKEN_EXPIRED`` sentinel
    (active token expired and the refresh was deferred this pass — lock
    contention, unattributable lineage, or a failed persist; retried
    automatically), the ``USAGE_API_KEY`` sentinel
    (managed API-key account, no subscription quota), the
    ``USAGE_KEYCHAIN_UNAVAILABLE`` sentinel (active Keychain unreadable), the
    ``USAGE_FOREIGN_CREDENTIAL`` sentinel (live credential proven to belong to
    another account; usage suppressed, a switch repairs the drift), the
    ``USAGE_NO_CREDENTIALS`` sentinel, or ``None`` (fetch failed). ``fetched_at``
    is forwarded to ``usage_to_json`` for the weekly pace fields (issue #125).
    """
    if isinstance(entry, dict):
        return "ok", usage_to_json(entry, fetched_at)
    if entry == USAGE_TOKEN_EXPIRED:
        return "token_expired", None
    if entry == USAGE_API_KEY:
        return "api_key", None
    if entry == USAGE_KEYCHAIN_UNAVAILABLE:
        return "keychain_unavailable", None
    if entry == USAGE_RELOGIN_REQUIRED:
        return "relogin_required", None
    if entry == USAGE_FOREIGN_CREDENTIAL:
        return "foreign_credential", None
    if isinstance(entry, str):
        return "no_credentials", None
    return "unavailable", None


def account_ref(number: int | None, email: str) -> dict:
    """A minimal account reference, used for switch ``from``/``to``."""
    return {"number": number, "email": email}


# ``usageStatus`` values that mean "this slot cannot serve a request right
# now, and no amount of waiting fixes it" — the user must act (re-login,
# unlock a keychain, enable the slot) before work may be routed to it.
# ``token_expired``/``unavailable`` are deliberately absent: both are
# transient and heal on the next successful poll.
INELIGIBLE_USAGE_STATUSES = frozenset(
    {
        "api_key",
        "relogin_required",
        "no_credentials",
        "foreign_credential",
        "keychain_unavailable",
    }
)


def eligibility(usage_status: str, disabled: bool) -> tuple[bool, str | None]:
    """``(eligible, ineligibleReason)`` for a slot: may work be routed here?

    Derived here rather than by each consumer so "never route to a dead slot"
    has one definition. ``disabled`` wins over a status reason: a held-out
    slot stays held out whatever its credential is doing.
    """
    if disabled:
        return False, "disabled"
    if usage_status in INELIGIBLE_USAGE_STATUSES:
        return False, usage_status
    return True, None


def usage_cache_fields() -> dict:
    """The freshness contract of the ``usage`` numbers in this payload.

    Emitted so a scripted consumer can reason about how stale a measurement
    is without hardcoding aswap's own constants (which move with the poll
    policy). ``serveTtlSeconds`` is when a cached row becomes re-fetchable;
    ``staleOkSeconds`` is how old a measurement may be and still be served
    as decision-grade.
    """
    return {
        "serveTtlSeconds": poll_policy.SERVE_TTL_S,
        "staleOkSeconds": usage_store.STALE_OK_S,
    }


def profile_fields(
    *,
    hydrated: bool,
    live_sessions: int,
    unreadable_records: int,
    identity_drifted: bool,
    token_expires_at: float | int | None,
) -> dict:
    """The ``--profiles`` block of an account row.

    Booleans, counts and one timestamp only — never credential material.
    ``profileUnreadableRecords`` travels beside ``profileLiveSessions``
    because a session record we could not read is not evidence that nothing
    is running (see ``session.profile_is_quiescent``).
    """
    return {
        "profileHydrated": hydrated,
        "profileLiveSessions": live_sessions,
        "profileUnreadableRecords": unreadable_records,
        "profileIdentityDrifted": identity_drifted,
        "profileTokenExpiresAt": iso_from_epoch_ms(token_expires_at),
    }


def iso_from_epoch_ms(expires_at: float | int | None) -> str | None:
    """Claude's millisecond ``expiresAt`` as ISO-8601 Z, or None.

    A timestamp, not a secret: it says when a token stops working, never
    what the token is.
    """
    if not isinstance(expires_at, (int, float)) or isinstance(expires_at, bool):
        return None
    return (
        datetime.fromtimestamp(expires_at / 1000.0, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def usage_freshness_fields(
    fetched_at: float | None, age_s: float | None
) -> dict:
    """Additive ``usageFetchedAt``/``usageAgeSeconds`` fields describing how
    old the served ``usage`` measurement is (the store may serve last-good
    data on fetch failure). Emitted under these names only alongside a
    non-null ``usage``; ``last_good_usage_fields`` reuses them renamed to
    ``lastGoodFetchedAt``/``lastGoodAgeSeconds`` for null-``usage`` rows."""
    if fetched_at is None:
        return {}
    fields: dict = {
        "usageFetchedAt": (
            datetime.fromtimestamp(fetched_at, tz=timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
    }
    if age_s is not None:
        fields["usageAgeSeconds"] = round(age_s, 1)
    return fields


def last_good_usage_fields(
    usage: dict | None, fetched_at: float | None, age_s: float | None
) -> dict:
    """Display-grade last-good usage, separate from decision-grade ``usage``."""
    if not isinstance(usage, dict) or fetched_at is None:
        return {}
    freshness = usage_freshness_fields(fetched_at, age_s)
    out = {
        "lastGoodUsage": usage_to_json(usage, fetched_at),
        "lastGoodFetchedAt": freshness["usageFetchedAt"],
    }
    if "usageAgeSeconds" in freshness:
        out["lastGoodAgeSeconds"] = freshness["usageAgeSeconds"]
    return out


def account_row(
    number: int,
    email: str,
    org_name: str,
    org_uuid: str,
    active: bool,
    usage_entry: dict | str | None,
    *,
    usage_fetched_at: float | None = None,
    usage_age_s: float | None = None,
    last_good_usage: dict | None = None,
    alias: str = "",
    disabled: bool = False,
    profile_path: str = "",
    profile: dict | None = None,
    usage_deferred_reason: str | None = None,
    billing: dict | None = None,
    weekly: dict | None = None,
) -> dict:
    """A full account row for ``--list``.

    ``profile_path`` and the derived ``eligible``/``ineligibleReason`` are
    additive and always emitted (pure derivation, no filesystem work).
    ``profile`` is the optional ``--profiles`` block built by
    :func:`profile_fields`; it costs a file read and a PID scan per account,
    which is why it is opt-in.

    ``usage_deferred_reason`` names why this row's number is not moving when
    the cause is structural rather than a failure. Without it the state is
    invisible: a resident-but-idle profile never rotates, its token expires,
    the row reports ``token_expired`` forever with no error and no strike,
    and nothing says the poll is being deliberately deferred to the process
    that owns the token family.
    """
    status, usage = usage_fields(usage_entry, usage_fetched_at)
    can_route, ineligible_reason = eligibility(status, disabled)
    row = {
        "number": number,
        "email": email,
        "organizationName": org_name,
        "organizationUuid": org_uuid,
        "isOrganization": bool(org_uuid),
        "active": active,
        "usageStatus": status,
        "usage": usage,
        "profilePath": profile_path,
        "eligible": can_route,
        "ineligibleReason": ineligible_reason,
        "usageDeferredReason": usage_deferred_reason,
    }
    if alias:
        row["alias"] = alias
    # Additive: present once the slot's subscription anchor has been read.
    # nextBillingAt is derived (monthly from subscriptionCreatedAt), hence
    # nextBillingEstimated.
    if billing is not None:
        row["billing"] = billing
    # Additive: the remembered weekly reset (per plan tier; see weekly_anchor).
    if weekly is not None:
        row["weeklyReset"] = weekly
    if profile is not None:
        row.update(profile)
    # Additive field: present only when the slot is held out of rotation, so
    # existing consumers keying on the base schema are unaffected.
    if disabled:
        row["disabled"] = True
    if usage is not None:
        row.update(usage_freshness_fields(usage_fetched_at, usage_age_s))
    else:
        row.update(
            last_good_usage_fields(
                last_good_usage, usage_fetched_at, usage_age_s
            )
        )
    return row


def error_envelope(exc: Exception) -> dict:
    """The structured error payload emitted on a handled ClaudeSwitchError."""
    return {
        "schemaVersion": SCHEMA_VERSION,
        "error": {"type": type(exc).__name__, "message": str(exc)},
    }
