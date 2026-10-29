"""The rate-limit gate: what the API itself says about an account at "100%".

The usage endpoint (``oauth.request_usage_data``) reports whole percentages
that read as rounded UP: measured 2026-10-27, seven accounts it called 100%
were still serving requests, and a one-token request on each answered with
``anthropic-ratelimit-unified-status: allowed_warning`` and a 7-day
utilization of 0.99. Only the ones the gate answered ``rejected`` (1.0 or
more) were really blocked. Where the two sources differ, the endpoint is one
point above the headers, so an endpoint 100 with an open gate is an account
with somewhere under a point left — real capacity aswap used to walk away
from.

The probe is the cheapest request that carries those headers: one
``max_tokens: 1`` message on the smallest model, sent with the slot's current
access token. It costs a few input tokens and one output token, and it is
spent only where it can change a decision (see :func:`due`): an account whose
endpoint reading is at 100% on the 5-hour or 7-day window, throttled per
account, never for a window that resets within minutes anyway.

A ``429`` that carries the unified headers is the gate saying ``rejected``:
data, not an error.

Results live in ``cache/gate.json`` (a state document like
``cache/usage.json``), identity-guarded per slot the same way.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from claude_swap.endpoints import scaled  # test-only clock scale (1.0 outside tests)
from claude_swap import endpoints, oauth, statedb
from claude_swap.locking import FileLock
from claude_swap.settings import atomic_write_json

_logger = logging.getLogger("claude-swap")

GATE_URL = "https://api.anthropic.com/v1/messages"  # default; see endpoints
GATE_PATH = "/v1/messages"
PROBE_MODEL = "claude-haiku-5-5"
# Claude Code's own system line: OAuth subscription tokens are accepted on
# /v1/messages only for Claude Code traffic.
PROBE_SYSTEM = "You are Claude Code, Anthropic's official CLI for Claude."
ANTHROPIC_VERSION = "2023-06-01"
_PREFIX = "anthropic-ratelimit-unified-"
SCHEMA_VERSION = 1

OPEN_STATUSES = ("allowed", "allowed_warning")
REJECTED = "rejected"
# Gate statuses as shown to people and in JSON.
OPEN = "open"
BLOCKED = "blocked"

# One probe per account per this long, whatever the outcome (a failure is
# throttled exactly like a success, so a broken probe cannot become a loop).
PROBE_INTERVAL_S = scaled(10 * 60.0)
# The ACTIVE account riding an open gate is re-probed this often: it is
# burning its last point, and the next closed answer is what moves the work.
# A probe is one one-token request. Was 150 s until 2026-10-29: at three
# points a minute a 5x spends its last point in 20 s.
RIDE_REPROBE_S = scaled(30.0)
# A probe is trusted for decisions this long (a little over one interval, so
# an account due for its next probe is not dropped between two ticks)...
FRESH_S = PROBE_INTERVAL_S + 60.0
# ...except for the active account riding the gate: two missed re-probes and
# it falls back to the endpoint's 100%, which is the at-limit escape.
ACTIVE_FRESH_S = 2 * RIDE_REPROBE_S
# No probe for a full window that resets within this long: it is back soon
# anyway, and the probe would be spent on a few minutes.
RESET_SKIP_S = 5 * 60.0
# Probes per engine tick, active account included.
MAX_PROBES_PER_TICK = 3
# Headroom credited to an endpoint-100 window behind an open gate: the
# middle of the one-point band between the two sources (the gate says under
# 100, the endpoint rounds up to 100). Ranks behind any measured headroom.
OPEN_HEADROOM = 0.5

# Usage-dict window keys the gate speaks for, with their header names.
_WINDOWS = (("five_hour", "5h"), ("seven_day", "7d"))
_GATED_LABELS = ("5h", "7d")


@dataclass(frozen=True)
class GateOutcome:
    """One probe: ``gate`` on any answer that carried the headers, else
    ``error`` (a short kind, never request or credential detail)."""

    gate: dict | None
    error: str | None = None


def _iso(ts: float) -> str:
    return (
        datetime.fromtimestamp(ts, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _float(value: object) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def parse_headers(headers: Mapping[str, str] | None, now: float) -> dict | None:
    """The gate from a response's unified rate-limit headers, or None.

    ``pct`` is utilization x 100 (the header carries two decimals, so whole
    percents; it can exceed 100 once rejected).
    """
    if headers is None:
        return None

    def get(name: str) -> str | None:
        value = headers.get(_PREFIX + name)
        return value.strip() if isinstance(value, str) and value.strip() else None

    status = get("status")
    if status is None:
        return None
    gate: dict = {"status": status, "probedAt": now}
    for key, label in _WINDOWS:
        window: dict = {}
        util = _float(get(f"{label}-utilization"))
        if util is not None:
            window["pct"] = round(util * 100.0, 2)
        win_status = get(f"{label}-status")
        if win_status is not None:
            window["status"] = win_status
        reset = _float(get(f"{label}-reset"))
        if reset is not None:
            window["resets_at"] = _iso(reset)
        if window:
            gate[key] = window
    claim = get("representative-claim")
    if claim is not None:
        gate["claim"] = claim
    return gate


def request_gate(access_token: str, now: float) -> GateOutcome:
    """Send the one-token probe and read the gate off the answer."""
    body = json.dumps({
        "model": PROBE_MODEL,
        "max_tokens": 1,
        "system": PROBE_SYSTEM,
        "messages": [{"role": "user", "content": "hi"}],
    }).encode()
    req = urllib.request.Request(
        endpoints.api_base() + GATE_PATH,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {access_token}",
            "anthropic-beta": oauth.OAUTH_BETA_HEADER,
            "anthropic-version": ANTHROPIC_VERSION,
            "Content-Type": "application/json",
            "User-Agent": "claude-swap/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            gate = parse_headers(resp.headers, now)
    except urllib.error.HTTPError as e:
        # A blocked account answers 429 WITH the headers: that is the gate
        # saying "rejected", the very thing the probe is for.
        gate = parse_headers(e.headers, now)
        if gate is None:
            return GateOutcome(None, error=f"http-{e.code}")
    except Exception as e:
        kind, _ = oauth._classify_usage_error(e)
        return GateOutcome(None, error=kind)
    if gate is None:
        return GateOutcome(None, error="no-gate-headers")
    return GateOutcome(gate)


# -- verdicts ----------------------------------------------------------------


def _reset_ts(resets_at: object) -> float | None:
    if not isinstance(resets_at, str) or not resets_at:
        return None
    try:
        return datetime.fromisoformat(resets_at.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def gate_of(record: dict | None) -> dict | None:
    gate = record.get("gate") if isinstance(record, dict) else None
    return gate if isinstance(gate, dict) else None


def _window_full(window: object) -> bool:
    """A gated window at its limit: refused, or reading 100% or more.

    Headers carry whole percents rounded down, so 100 means the account is
    already into Claude's grace before requests are refused. That is the
    moment to move the work (owner rule 2026-10-29): waiting for the refusal
    means a request has already failed.
    """
    if not isinstance(window, dict):
        return False
    pct = window.get("pct")
    return window.get("status") == REJECTED or (
        isinstance(pct, (int, float)) and pct >= 100.0
    )


def is_full(gate: dict | None) -> bool:
    """The gate says this account is at its limit (see ``_window_full``)."""
    if not isinstance(gate, dict):
        return False
    return gate.get("status") == REJECTED or any(
        _window_full(gate.get(key)) for key, _ in _WINDOWS
    )


def is_fresh(record: dict | None, now: float, fresh_s: float = FRESH_S) -> bool:
    """A gate answer recent enough to decide on, whose full window (if any)
    has not reset since."""
    gate = gate_of(record)
    if gate is None:
        return False
    probed = gate.get("probedAt")
    if not isinstance(probed, (int, float)) or now - probed > fresh_s:
        return False
    if is_full(gate):
        for key, _ in _WINDOWS:
            window = gate.get(key)
            if _window_full(window):
                ts = _reset_ts(window.get("resets_at"))
                if ts is not None and ts <= now:
                    return False
    return True


def _full_windows(usage: dict | None, models: Sequence[str]) -> list[tuple[str, float, str | None]]:
    return [w for w in oauth.relevant_windows(usage, models) if w[1] >= 100.0]


def apply(
    usage: dict | None,
    record: dict | None,
    models: Sequence[str],
    now: float,
    fresh_s: float = FRESH_S,
) -> tuple[float | None, str | None]:
    """Decision headroom for one account with its gate folded in.

    Returns ``(headroom, verdict)``: ``verdict`` is ``BLOCKED`` when a fresh
    gate says rejected or reads 100% on a window (at the limit whatever the
    endpoint reads; see ``_window_full``), ``OPEN``
    when a fresh open gate lifts an endpoint reading of 100% on the 5h/7d
    windows (headroom ``OPEN_HEADROOM``), and ``None`` when the gate changes
    nothing — then ``headroom`` is the endpoint's own. A per-model window at
    100% is not the gate's to open: the probe runs on a different model.
    """
    raw = oauth.account_headroom(usage, models)
    if not is_fresh(record, now, fresh_s):
        return raw, None
    gate = gate_of(record) or {}
    if is_full(gate):
        return min(raw, 0.0) if raw is not None else 0.0, BLOCKED
    if gate.get("status") not in OPEN_STATUSES or raw is None or raw > 0:
        return raw, None
    full = _full_windows(usage, models)
    if not full or any(label not in _GATED_LABELS for label, _, _ in full):
        return raw, None
    return OPEN_HEADROOM, OPEN


def from_live(readings: Sequence[dict]) -> dict | None:
    """A gate record from the newest statusline reading of the active account.

    The statusline is fed by the same headers on every real response, so a
    fresh reading is a free gate probe: under 100% it is open, at 100% it is
    closed.
    """
    newest = max(
        (r for r in readings if isinstance(r.get("at"), (int, float))),
        key=lambda r: r["at"],
        default=None,
    )
    if newest is None:
        return None
    gate: dict = {"probedAt": float(newest["at"]), "source": "live"}
    blocked = False
    for key, _ in _WINDOWS:
        window = newest.get(key)
        if not isinstance(window, dict) or not isinstance(window.get("pct"), (int, float)):
            continue
        pct = float(window["pct"])
        status = REJECTED if pct >= 100.0 else "allowed"
        blocked = blocked or status == REJECTED
        entry: dict = {"pct": pct, "status": status}
        if isinstance(window.get("resets_ts"), (int, float)):
            entry["resets_at"] = _iso(window["resets_ts"])
        gate[key] = entry
    if not any(key in gate for key, _ in _WINDOWS):
        return None
    gate["status"] = REJECTED if blocked else "allowed"
    return {"gate": gate}


def due(
    usage: dict | None,
    record: dict | None,
    models: Sequence[str],
    now: float,
    interval_s: float = PROBE_INTERVAL_S,
    *,
    any_level: bool = False,
) -> bool:
    """Would a probe of this account now be worth its request?

    Only an endpoint reading of 100% on the 5h/7d windows can be changed by
    the gate, so nothing else is probed (a 99% account is already usable);
    a full per-model window cannot be opened by it either. ``any_level``
    lifts that for the active account's last stretch, where the probe is the
    freshest usage reading there is. A full window that resets within
    ``RESET_SKIP_S`` is left to reset. The last attempt, successful or not,
    throttles the next one by ``interval_s``.
    """
    full = _full_windows(usage, models)
    if not any_level and (
        not full or any(label not in _GATED_LABELS for label, _, _ in full)
    ):
        return False
    for _, _, resets_at in full:
        ts = _reset_ts(resets_at)
        if ts is not None and ts - now <= RESET_SKIP_S:
            return False
    last = record.get("attemptedAt") if isinstance(record, dict) else None
    return not (isinstance(last, (int, float)) and now - last < interval_s)


# -- display -----------------------------------------------------------------


def annotate(usage: dict | None, record: dict | None, now: float) -> dict | None:
    """A copy of ``usage`` whose near-full 5h/7d windows carry the gate.

    Each annotated window gains ``gate: {pct, status, probedAt}`` with
    ``status`` ``open`` or ``blocked``. Only fresh answers, and only windows
    where the gate says something the endpoint does not (99% and up, or
    rejected). Unchanged ``usage`` when there is nothing to add.
    """
    if not isinstance(usage, dict) or not is_fresh(record, now):
        return usage
    gate = gate_of(record) or {}
    out = usage
    for key, _ in _WINDOWS:
        window = usage.get(key)
        seen = gate.get(key)
        if not isinstance(window, dict) or not isinstance(seen, dict):
            continue
        status = seen.get("status") or gate.get("status")
        full = status == REJECTED or _window_full(seen)
        pct = window.get("pct")
        if not full and not (isinstance(pct, (int, float)) and pct >= 99.0):
            continue
        note: dict = {
            "status": BLOCKED if full else OPEN,
            "probedAt": gate.get("probedAt"),
        }
        if isinstance(seen.get("pct"), (int, float)):
            note["pct"] = seen["pct"]
        if out is usage:
            out = dict(usage)
        out[key] = dict(window, gate=note)
    return out


def window_note(window: dict | None) -> str | None:
    """``open, gate 99%`` / ``blocked`` for an annotated window, else None.

    A window the display has rolled over (its reset passed) has no gate to
    speak of any more.
    """
    if not isinstance(window, dict) or window.get("resetAgoS") is not None:
        return None
    note = window.get("gate")
    if not isinstance(note, dict):
        return None
    if note.get("status") == BLOCKED:
        return BLOCKED
    pct = note.get("pct")
    return f"{OPEN}, gate {pct:.0f}%" if isinstance(pct, (int, float)) else OPEN


def gate_to_json(note: dict) -> dict:
    out: dict = {"status": note.get("status")}
    if isinstance(note.get("pct"), (int, float)):
        out["pct"] = note["pct"]
    probed = note.get("probedAt")
    if isinstance(probed, (int, float)):
        out["probedAt"] = _iso(probed)
    return out


# -- store -------------------------------------------------------------------

Identity = tuple[str, str]


class GateStore:
    """The ``cache/gate.json`` table: one row per slot, identity-guarded like
    ``cache/usage.json`` (a row whose stored identity differs is invisible to
    reads and replaced on write)."""

    def __init__(self, cache_dir: Path, clock: Callable[[], float] = time.time):
        self.path = cache_dir / "gate.json"
        self._lock_path = cache_dir / ".gate.lock"
        self.clock = clock

    def _read_rows(self) -> dict[str, dict]:
        try:
            raw = json.loads(statedb.read_text(self.path))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return {}
        if not isinstance(raw, dict) or raw.get("schemaVersion") != SCHEMA_VERSION:
            return {}
        rows = raw.get("accounts")
        return rows if isinstance(rows, dict) else {}

    @staticmethod
    def _matches(row: object, identity: Identity) -> bool:
        return (
            isinstance(row, dict)
            and row.get("email") == identity[0]
            and row.get("organizationUuid", "") == identity[1]
        )

    def rekey(self, moves: Mapping[str, tuple[str, Identity]]) -> None:
        """Carry rows to the accounts' new slot numbers after a move/renumber
        (``moves``: old -> (new, identity)); see ``renumber.rekey_rows``."""
        from claude_swap.renumber import rekey_rows

        with FileLock(self._lock_path):
            rows, changed = rekey_rows(self._read_rows(), dict(moves))
            if changed:
                atomic_write_json(
                    self.path, {"schemaVersion": SCHEMA_VERSION, "accounts": rows}
                )

    def records(self, identities: Mapping[str, Identity]) -> dict[str, dict]:
        rows = self._read_rows()
        return {
            num: rows[num]
            for num, identity in identities.items()
            if self._matches(rows.get(num), identity)
        }

    def record(
        self,
        outcomes: Mapping[str, GateOutcome],
        identities: Mapping[str, Identity],
        now: float,
    ) -> dict[str, dict]:
        """Store these probe outcomes; returns the rows written.

        A failed probe keeps the row's last answer (it may still be fresh)
        and only moves ``attemptedAt``/``error`` along.
        """
        written: dict[str, dict] = {}
        with FileLock(self._lock_path):
            rows = self._read_rows()
            for num, outcome in outcomes.items():
                identity = identities.get(num)
                if identity is None:
                    continue
                old = rows.get(num)
                row = dict(old) if self._matches(old, identity) else {
                    "email": identity[0], "organizationUuid": identity[1],
                }
                row["attemptedAt"] = now
                if outcome.gate is not None:
                    row["gate"] = outcome.gate
                    row.pop("error", None)
                else:
                    row["error"] = outcome.error
                rows[num] = row
                written[num] = row
            atomic_write_json(
                self.path, {"schemaVersion": SCHEMA_VERSION, "accounts": rows}
            )
        return written
