"""Auto-switch engine: poll usage, switch accounts before they hit rate limits.

``AutoSwitchEngine`` is UI-agnostic — no printing, no argparse, no TUI
imports. It composes a :class:`ClaudeAccountSwitcher`, evaluates a threshold
policy each :meth:`~AutoSwitchEngine.tick`, and reports everything through
typed events handed to an ``on_event`` callback; the CLI renders them as
human lines or JSONL, and any future frontend (TUI dashboard, menubar) can
consume the same stream.

Policy in one paragraph: when the active account's *binding window* (the
higher of its 5h/7d utilization) crosses ``settings.threshold``, switch to
the candidate with the most headroom — proactively, so the old account is
still valid while a running Claude Code picks the new one up (this is what
makes the macOS ~30s Keychain cache latency harmless). Candidates must sit
``hysteresis_pct`` below the threshold so two accounts hovering at the line
never ping-pong, and a ``cooldown_seconds`` floor bounds the switch rate
(bypassed only when the active account is hard at its limit). Before
activation the target's token is *freshened* (refreshed if it expires within
10 minutes — twice Claude Code's refresh buffer, so a running Claude Code's
under-lock re-read sees a fresh token and aborts its own refresh); a target
whose refresh token is dead gets quarantined instead of activated. When the
active account's own usage becomes unreadable for ``unhealthy_ticks``
consecutive ticks, the engine fails over to any healthy candidate.

A switch made outside the engine holds off its optional moves for
``manual_hold_minutes`` (see ``OPTIONAL_TRIGGERS``).

Cooldown, quarantine and the manual hold persist in
``<backup_root>/autoswitch_state.json``
(so cron-driven ``aswap auto --once`` ticks behave across processes), mutated
read-modify-write under a dedicated file lock.
"""

from __future__ import annotations

import enum
import json
import logging
import math
import random
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import ClassVar

from claude_swap.endpoints import scaled  # test-only clock scale (1.0 outside tests)
from claude_swap import edge, gate, live_usage, oauth, poll_policy, snapshots, statedb
from claude_swap.exceptions import ClaudeSwitchError, RosterChangedError
from claude_swap.json_output import SCHEMA_VERSION, USAGE_TOKEN_EXPIRED
from claude_swap.locking import FileLock
from claude_swap.poll_policy import (
    ESCALATION_MARGIN_PCT,
    RESET_SLACK_S,
    binding_pct,
)
from claude_swap.live_settings import (
    SETTINGS_WATCH_S,  # noqa: F401  (re-exported; see live_settings.py)
    LiveSettings,
    SettingsReload,
    sliced_wait,
)
from claude_swap.settings import (
    AutoSwitchSettings,
    atomic_write_json,
    format_setting_value,
    parse_model_names,
)
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.usage_store import due_candidate, plan_oversleeps_interval
from claude_swap.warmup import (
    DEFAULT_INTERVAL_SECONDS as WARMUP_INTERVAL_SECONDS,
    WarmupEngine,
    WarmupEvent,
)

STATE_FILENAME = "autoswitch_state.json"
STATE_SCHEMA_VERSION = 1

_logger = logging.getLogger("claude-swap")

# Systemic freshen refusals, MOST ACTIONABLE FIRST. Deterministic conditions
# that every candidate hits identically, so the tick reports one of them —
# and the order decides which, because reporting the wrong one is how a cause
# needing a human hides behind one that clears itself. store-unmirrored and
# invalid_client stay until somebody unsets an env var or fixes a client
# registration, and stash-unreadable until they unlock a keychain, fix a mode,
# or purge the row; consume-busy is gone by the next pass. stash-unreadable is
# the one that is per-SLOT rather than global, which costs nothing here: this
# message is only ever emitted when NO candidate freshened, so naming the real
# cause of the only slot that had one beats "(network?)".
_SYSTEMIC_MESSAGES = {
    "store-unmirrored": "CLAUDE_SECURESTORAGE_CONFIG_DIR is set — unset it or "
                        "crun aswap from a normal shell",
    "invalid_client": "aswap's OAuth client was rejected — systemic, not this "
                      "account",
    "stash-unreadable": "a stashed successor is unreadable — unlock the "
                        "keychain or fix the file, then retry; "
                        "`aswap unclaimed` inspects it",
    "consume-busy": "canother aswap surface holds the slot — retries next pass",
}
# Insertion order IS the precedence order, so the remedy and its rank cannot
# drift apart.
_SYSTEMIC_STATUSES = tuple(_SYSTEMIC_MESSAGES)

# Freshen targets whose access token expires within this window: twice Claude
# Code's own 5-minute refresh buffer, so its post-lock "abort refresh if not
# expired" re-read holds with margin after our swap.
FRESHEN_BUFFER_MS = 10 * 60 * 1000

# Sleep caps around a known quota reset (RESET_SLACK_S lives in poll_policy
# with the rest of the cadence numbers). Recheck at the exhausted-account poll
# cadence: providers can grant quota before the previously reported reset, and
# a long engine sleep must not suppress the fetch that discovers it.
MAX_SLEEP_S = poll_policy.EXHAUSTED_INTERVAL_S
NO_RESET_FALLBACK_S = scaled(300.0)
# Tick cadence while the statusline feeds live usage for the active account
# (see live_usage). Reading those files is free — the usage-endpoint budget
# is untouched — so the engine can react within seconds when many parallel
# sessions burn the active account faster than any poll interval.
LIVE_TICK_S = scaled(20.0)
# Floor for the faster live ticks taken while the active account closes on
# its limit.
LIVE_MIN_TICK_S = scaled(5.0)
# The active account's last stretch (projected limit within GUARD_PREFETCH_S,
# or riding an open gate): auto checks every 3-5 s, randomised so two
# machines never fall into step, and probes the gate on each check unless a
# live statusline reading is fresher. The probe's headers are the newest
# usage there is, and a window reading 100% moves the work at once, before
# any request is refused (owner rule 2026-10-29: never stop running scripts
# to squeeze the last of an account).
ENDGAME_TICK_MIN_S = scaled(3.0)
ENDGAME_TICK_MAX_S = scaled(5.0)
# How often a running engine heals damaged state files and checks whether a
# snapshot is due (snapshots themselves are hourly; see snapshots.py).
MAINTAIN_INTERVAL_S = scaled(300.0)

# Idle-hold cap (elapsed, not ticks — the hold itself slows the cadence to
# NO_RESET_FALLBACK_S): an owned-and-expired token normally means Claude Code
# is idle and will self-heal on next use, but a *dead* refresh token with an
# active user would look identical forever, so after this long the engine
# falls back to normal unhealthy counting.
IDLE_HOLD_MAX_S = scaled(30 * 60.0)

# Anti-flap margin for the every-account-above-threshold escape, measured on
# the axis that escape ranks by: a target must come back at least this much
# sooner than the account we are leaving. Five minutes is comfortably longer
# than one poll cycle, so two accounts whose windows roll over close together
# cannot trade places on measurement jitter — the reverse move never clears
# the margin. The percentage-point hysteresis is unmeetable in this state by
# construction (everything is within a few points of its limit), which is why
# it needs its own unit rather than a reused one.
RECOVERY_HYSTERESIS_S = 300.0

# Horizon past which a sooner reset stops being worth real headroom. The escape
# above was measured on minutes-scale resets; days-scale is the opposite trade,
# since neither account returns within the session. 4h keeps most of a 5-hour
# cycle on the recovery ranking: a 5h window can be up to 5h from resetting, so
# a peer bound by one that is 4h-5h out falls back to headroom ranking instead.
# Deliberately the conservative side of that boundary -- ranking by headroom
# where the reset is still an hour away costs at most one extra move, while a
# wider horizon would rank by a reset the session may never see.
RECOVERY_HORIZON_S = 4 * 3600.0

# Anti-flap margin on the headroom axis, as a RATIO rather than percentage
# points: strictly-more is no margin at all — one point moves the engine, the
# target burns it back, and it ping-pongs. A ratio makes the move one-way.
HORIZON_HEADROOM_RATIO = 2.0

# The two anti-flap thresholds are both anchored to `active_headroom`, which
# leaves a band where a peer is VISIBLE (the spent clause goes false, so the
# reset axis switches off) yet UNCHOOSABLE (it misses the margin). Measured,
# active 3.00 pts took a 0.10-pt peer over a 5.99-pt one, discarding 60x the
# runway; and inside the band monotonicity inverts, so adding headroom flips a
# move into a refusal.
#
# No single constant removes both ends — the two band widths sum to
# `active x HORIZON_HEADROOM_RATIO - SPENT_HEADROOM_PCT`, so shrinking one
# grows the other. Measured, not argued. The fix is in the ranking instead:
# `best_candidate_headroom` carries no floor, and margin failures are
# re-admitted through a one-way fallback used only when nothing else
# qualifies.

# Below this an account is spent, and headroom comparisons between two spent
# accounts compare noise (a point is under ten minutes of work, less than two
# poll intervals). When EVERY candidate is down here, rank by reset instead —
# sit where quota returns first, however far out — rather than parking on
# whichever account we happen to hold.
SPENT_HEADROOM_PCT = 3.0

# Capacity of each plan tier relative to Max 5x (Max 20x is four times it, Pro
# a fifth). A utilization percentage only compares inside one tier: 10 points
# of a 20x account is four times the work of 10 points of a 5x one. Unknown
# tiers count as 5x.
TIER_CAPACITY = {"max_20x": 4.0, "max_5x": 1.0, "pro": 0.2, "ai": 0.2}
DEFAULT_CAPACITY = 1.0

# Burn rate of the work itself, measured on the active account's binding
# window and converted to capacity units so it carries over to any target.
# Live statusline readings put samples ~20s apart; a minute of evidence is
# the least the rate is trusted on.
BURN_WINDOW_S = 10 * 60.0
BURN_MIN_SPAN_S = 60.0
# Readings are whole percentages, and the two sources round differently (the
# usage endpoint reads one point above the statusline's headers; see gate.py),
# so any rise between two readings can overstate the real one by just under
# two points. The limit guard's fast pace (`BurnTracker.fast_rate`) subtracts
# this much from every rise, which makes it a lower bound on the real pace: a
# rounding step can never project a limit that is not coming.
BURN_QUANTUM_PCT = 2.0

# A move that is not an escape must buy at least this long on the target at
# the current burn rate. Measured 2026-10-25: five switches in 6.5 minutes,
# each landing on a 5x account with a few points left.
MIN_LANDING_RUNWAY_S = 15 * 60.0
# Burn assumed for the landing check when the active account is idle and no
# rate has been seen yet, in 5x points per second (1 point a minute). Without
# it an idle spell switched the check off: 2026-10-26 consume-first hopped
# 13 -> 26 -> 15 -> 32 in ten minutes, landing on accounts 95-97% used.
DEFAULT_LANDING_BURN = 1.0 / 60
# The landing check sizes a target by the fastest burn seen this long ago or
# less, not the instant rate. 2026-10-27: consume-first went back to a 5x
# account with 7 points left 19 minutes after leaving it at 4.7 points a
# minute, because a lull had dropped the instant rate to 0.4.
LANDING_BURN_WINDOW_S = 30 * 60.0
# consume-first exception to both runway rules: an account whose weekly quota
# perishes sooner than the active one's is drained whatever its size, because
# whatever is left there at its reset is lost. The only condition is that the
# stay is real work: at the landing burn it must outlast LIMIT_GUARD_S by this
# much, or the guard would move straight off it again (the 2026-10-25/26/27
# hops above all landed inside it). Owner rule 2026-10-29: squeeze every
# account, soonest-expiring first, down to its last point.
DRAIN_MIN_USE_S = 30.0

# Switch below the threshold when the active account would reach 100% within
# this long at the current burn rate. Only a sudden burst can trip it: at a
# steady pace the live reading reaches the threshold first, and that (or a
# closed gate) moves the work, the way a person watching the meter would.
# A live reading of 100% is still inside Claude's grace before requests are
# refused, so riding to it is safe; the switch itself takes about a second.
# Was 3 minutes until 2026-10-29, which left ~9 5x-points on every account.
LIMIT_GUARD_S = 30.0
# Candidate usage is refreshed this far ahead of a projected limit, so the
# move the guard or the threshold is about to force lands on fresh numbers.
GUARD_PREFETCH_S = 3 * 60.0

# A proactive move must buy meaningfully more time than it leaves: the
# target's runway (at the same peak burn the landing check uses) at least
# RUNWAY_RATIO times the active's, or RUNWAY_MARGIN_S longer. Otherwise the
# engine rides the active to its limit and lets the at-limit escape move it.
# Measured 2026-10-27: the limit guard cycled 1 -> 14 -> 23 -> 1 every
# 10-60 s, each move "target runway ~1 min; limit projected in 0.7 min".
RUNWAY_RATIO = 2.0
RUNWAY_MARGIN_S = 5 * 60.0

# Triggers that only OPTIMISE where the work runs; nothing breaks if they
# wait. After a switch made outside the engine (by hand: `aswap switch`, the
# TUI, an in-app `/login`) these hold off for `manual_hold_minutes`. Every
# other trigger protects the session and still fires during the hold:
#
#   at-limit    the active account is at its limit
#   proactive   over the threshold, or the limit guard projects 100% within
#               LIMIT_GUARD_S (the all-above soonest-recovery escape is a
#               proactive move too: the active is over the threshold there)
#   failover    the active account's usage has been unreadable for a while
#
# Measured 2026-10-27: a hand switch to account 3 at 11:06 was undone at
# 11:10 by consume-first (3 at 73% -> 13 at 85%).
OPTIONAL_TRIGGERS = frozenset({"consume-first"})
# A held-off optional move is logged at most this often (the hold itself is
# logged once, when it starts); the NoSwitchEvent still goes out every tick.
HOLD_NOTE_INTERVAL_S = scaled(15 * 60.0)


def manual_hold_until(
    state: dict, current: str | None, minutes: float, now: float
) -> float | None:
    """When the manual hold on ``current`` ends, or None when none is in force.

    Reads the ``manualHold`` record of the autoswitch state file. A hold on
    any other account, an expired one, or ``minutes <= 0`` is no hold.
    """
    hold = state.get("manualHold") if isinstance(state, dict) else None
    if not isinstance(hold, dict) or current is None or minutes <= 0:
        return None
    since = hold.get("since")
    if (
        str(hold.get("account")) != str(current)
        or isinstance(since, bool)
        or not isinstance(since, (int, float))
    ):
        return None
    until = since + minutes * 60.0
    return until if now < until else None


def clock_label(ts: float) -> str:
    """Local wall-clock ``HH:MM`` for a hold's end."""
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def tier_capacity(tier: object) -> float:
    from claude_swap.weekly_anchor import normalize_tier

    return TIER_CAPACITY.get(normalize_tier(tier), DEFAULT_CAPACITY)


class BurnTracker:
    """Recent burn rate of the work, in capacity units per second.

    Samples are (time, account, binding utilization %, account capacity).
    Only consecutive samples of the same account count, and a drop (a window
    rolled over) is skipped rather than read as negative burn.

    The time is when the number was MEASURED (the usage fetch, or the
    statusline reading), not when a tick looked at it. A tick re-serving a
    stored reading adds no evidence and is ignored; a fresh reading after a
    long gap is credited with the whole gap, so the burn it shows is not
    squeezed into one tick interval.
    """

    def __init__(self) -> None:
        self._samples: list[tuple[float, str, float, float]] = []

    def _is_new(self, sample: tuple[float, str, float, float]) -> bool:
        last = self._samples[-1] if self._samples else None
        return last is None or last[1] != sample[1] or sample[0] > last[0]

    def record(self, now: float, account: str, used_pct: float, capacity: float) -> None:
        sample = (now, account, used_pct, capacity)
        if not self._is_new(sample):
            return  # the same (or an older) reading again: no new evidence
        self._samples.append(sample)
        self._samples = [s for s in self._samples if now - s[0] <= BURN_WINDOW_S]

    def renumber(self, mapping: dict[str, str]) -> None:
        """Follow a renumber (``mapping``: old -> new for every account still
        on the roster); samples of accounts that left are dropped."""
        self._samples = [
            (t, mapping[str(a)], pct, cap)
            for t, a, pct, cap in self._samples
            if str(a) in mapping
        ]

    def rate(self) -> float | None:
        burned = span = 0.0
        for prev, cur in zip(self._samples, self._samples[1:]):
            if prev[1] != cur[1] or cur[0] <= prev[0] or cur[2] < prev[2]:
                continue
            burned += (cur[2] - prev[2]) * cur[3]
            span += cur[0] - prev[0]
        if span < BURN_MIN_SPAN_S or burned <= 0:
            return None
        return burned / span

    def fast_rate(
        self, pending: tuple[float, str, float, float] | None = None
    ) -> float | None:
        """The fastest pace the latest stretch of readings PROVES, or None.

        ``rate()`` averages up to ``BURN_WINDOW_S`` and wants a minute of
        evidence, so it lags a sudden burn: right after a start or a switch it
        does not exist for a minute, and after a quiet spell a steep burn is
        diluted by the quiet minutes (measured: ten idle minutes at 95%, then
        five points a minute, and the guard never fired before 100%).

        This looks only at the trailing run of the current account, from the
        newest reading back, and for each earlier reading takes the rise
        minus ``BURN_QUANTUM_PCT`` over the time between them. Every one of
        those is a lower bound on the real pace over its stretch, so the
        result is too, and it needs no minimum span: two readings 20 s apart
        that rose 30 points prove 84 points a minute, two that rose one point
        prove nothing. A quiet stretch at the end pulls it down by itself,
        since every stretch ends at the newest reading.

        ``pending`` is a reading not yet recorded (the escalation decision
        runs before the tick records its sample).
        """
        samples = self._samples
        if pending is not None and self._is_new(pending):
            samples = [*samples, pending]
        if len(samples) < 2:
            return None
        end = samples[-1]
        best = 0.0
        prev = end
        for start in reversed(samples[:-1]):
            if start[1] != end[1] or start[0] >= prev[0] or start[2] > prev[2]:
                break  # another account, or a rollover: the run ends here
            rise = end[2] - start[2] - BURN_QUANTUM_PCT
            if rise > 0:
                best = max(best, rise * end[3] / (end[0] - start[0]))
            prev = start
        return best or None


def _recovery_is_useful(
    candidate_recovery_ts: float,
    active_recovery_ts: float,
    active_headroom: float,
    best_candidate_headroom: float,
    now: float,
) -> bool:
    """Rank THIS candidate by soonest reset, rather than by headroom?

    Two clauses, one place. Deciding it from four scattered gates left
    reachable holes in four of the sixteen combinations.

    Reset wins when everything worth having is spent — below
    ``SPENT_HEADROOM_PCT`` a headroom edge is under two poll intervals, so the
    only real question is which account returns first. Asked of the active and
    the BEST candidate, not of every account: an unknown headroom is not
    evidence of an empty one, and requiring all of them let a single sentinel
    row veto the check for everybody.

    It also wins when THIS candidate is back soon. Asked per candidate, not
    once on the active: an active bound by its weekly window sits days out
    while a peer's five-hour window returns in minutes.

    Past the horizon we rank by headroom, and when no candidate meets the ratio
    nobody qualifies — an active holding more quota than any peer can offer
    should keep the work.

    THE AXIS IS A PROPERTY OF THE PAIR, not of the candidate alone, and that
    is what stops the two guards leaking into each other. Each anti-flap gate
    is one-way on its OWN axis — hysteresis on recovery, a ratio on headroom —
    but a switch swaps which account is "active". Keying the choice on the
    candidate alone flipped the axis with it, so a pair straddling the horizon
    took one gate going out and the OTHER coming back, and neither guard ever
    saw both legs:

        acct 1   8 points, reset 109h out      acct 2   3 points, reset 3.5h out
        active=1 -> candidate inside  -> recovery: 3.5h < 109h      moves
        active=2 -> candidate outside -> headroom: 8 >= 3*2         moves back

    Measured: 47 credential rewrites over 3.9h on frozen inputs, ending only
    when the sooner reset landed. ``either side inside`` is symmetric under
    that swap, so the axis survives the move and the gate that permitted the
    outbound leg is the one asked about the return — where hysteresis refuses
    it, because the account we just left is now the distant one.

    Why EITHER and not BOTH: requiring both would refuse the #202 case this
    horizon exists to preserve — a weekly-bound active sitting days out while
    a peer's five-hour window returns in eight minutes. Measured across
    multiple ticks, both rules were checked against both shapes:

        rule    oscillating pair        #202 pair
        cand    moves, moves back       moves, moves back   (the bug)
        both    never moves             never moves         (breaks #202)
        either  moves, then holds       moves, then holds   (wanted)

    The #202 case oscillated on the original code too — its test ticks once,
    so it only ever observed the outbound leg.

    The step between the two axes is NOT monotone, and an earlier version of
    this docstring claimed it was ("0 inversions"). Re-swept directly over the
    predicate — active 1..12 pts against a peer walked 0.5..40 at 0.05, four
    reset shapes — a strictly-better peer does flip a move into a refusal, and
    every case sits on one point: `peer_h` crossing SPENT_HEADROOM_PCT, where
    the spent clause goes false and the axis changes underneath the comparison.

    That is the axis boundary, not a leak, and it is NOT reachable through
    `tick()`: the fallback at the bottom of the ranking loop re-admits exactly
    those candidates. A reviewer re-took the same sweep and got a different
    count from mine, which is the point — the count depends on the sweep's step
    and reset shapes, so it is stated as WHERE rather than HOW MANY.
    """
    if (
        active_headroom <= SPENT_HEADROOM_PCT
        and best_candidate_headroom <= SPENT_HEADROOM_PCT
    ):
        # The axis CAN change as the fleet burns, and that is not a leak:
        #
        #     out    active 2.0 / peer 4.0   headroom axis, 4.0 >= 2.0x2
        #     back   active 3.0 / peer 2.0   recovery  axis, 10h vs 80h
        #
        # Each leg is legitimate on the axis its own state selects, and the
        # transition is in the DATA rather than in the gates: constraining
        # either gate does not remove it.
        return True
    return (
        candidate_recovery_ts - now <= RECOVERY_HORIZON_S
        or active_recovery_ts - now <= RECOVERY_HORIZON_S
    )


# Adaptive scheduling: the baseline request volume is O(1) per tick — the
# active account plus ONE due candidate (stalest data first) — instead of
# every account in parallel, and the per-account cadence itself (movement,
# threshold distance, urgent mode, 429 recovery) lives in poll_policy, is
# persisted in the usage store by whichever collector fetched, and is shared
# by every surface. The engine escalates to a full candidate refresh only
# when a switch could actually be near: active utilization within
# ESCALATION_MARGIN_PCT of the threshold, or active usage unknown (failover
# needs fresh candidate data). The consume-first trigger can fire outside
# that escalation band; there it decides provisionally on the stored
# snapshot and escalates at commit time, when a switch would actually fire
# (the two-phase commit in _tick_inner).


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def pct_label(value: float) -> str:
    """A percentage for display, as configured: 85.555555 stays itself
    (never a rounded "85.5556") and 99.9 never becomes a lying "100" the
    way ``.0f`` renders it. Ten significant digits still absorb IEEE float
    noise (~15th digit) in computed utilizations (100.0 - headroom).
    Displayed comparisons must format BOTH sides with this helper — mixing
    formatters can render an impossible "85.5556% < 85.555555%"."""
    return f"{value:.10g}"


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AutoSwitchEvent:
    """Base event. ``to_json()`` payloads are additive: consumers must ignore
    unknown ``event`` kinds and unknown fields."""

    kind: ClassVar[str] = "event"
    ts: str = field(default_factory=_now_iso, kw_only=True)

    def _fields(self) -> dict:
        return {}

    def to_json(self) -> dict:
        return {
            "schemaVersion": SCHEMA_VERSION,
            "event": self.kind,
            "ts": self.ts,
            **self._fields(),
        }

    def human(self) -> str:  # pragma: no cover - overridden
        return self.kind


@dataclass(frozen=True)
class PollEvent(AutoSwitchEvent):
    kind: ClassVar[str] = "poll"
    active: dict | None  # account_ref shape, or None
    headroom: dict[str, float | None]  # account number → headroom pct (None=unknown)
    threshold: float
    # account number → last fetch-error cause ("http-429", "timeout", ...) for
    # accounts whose usage is unknown this tick. Additive field.
    fetch_errors: dict[str, str] = field(default_factory=dict)
    # account number → ordered window label → utilization pct ("5h", "7d",
    # then scoped model display names). Additive field: the binding pct alone
    # (e.g. "89%") hides which window binds — #115 was reported off that
    # ambiguity.
    windows: dict[str, dict[str, float]] = field(default_factory=dict)

    def _fields(self) -> dict:
        fields = {
            "active": self.active,
            "headroomPct": self.headroom,
            "threshold": self.threshold,
        }
        if self.fetch_errors:
            fields["fetchErrors"] = self.fetch_errors
        if self.windows:
            fields["windowsPct"] = self.windows
        return fields

    def _describe(self, num: str) -> str:
        wins = self.windows.get(num)
        if wins:
            return " · ".join(f"{name} {pct:.0f}%" for name, pct in wins.items())
        h = self.headroom.get(num)
        if h is not None:
            return f"{100 - h:.0f}%"
        err = self.fetch_errors.get(num)
        return f"? ({err})" if err else "?"

    def human(self) -> str:
        if self.active is None:
            return "poll: no active account"
        num = self.active.get("number")
        h = self.headroom.get(str(num))
        if h is not None:
            used = f"{100 - h:.0f}% used"
        else:
            err = self.fetch_errors.get(str(num))
            used = f"usage unknown ({err})" if err else "usage unknown"
        others = ", ".join(
            f"#{n}: {self._describe(n)}"
            for n in self.headroom
            if n != str(num)
        )
        tail = f" | others: {others}" if others else ""
        return (
            f"Account-{num} ({self.active.get('email')}): {used} "
            f"(switch at {pct_label(self.threshold)}%){tail}"
        )


@dataclass(frozen=True)
class SwitchEvent(AutoSwitchEvent):
    kind: ClassVar[str] = "switch"
    trigger: str  # "proactive" | "at-limit" | "failover" | "consume-first"
    from_ref: dict | None
    to_ref: dict | None
    warnings: list[str] = field(default_factory=list)
    dry_run: bool = False

    def _fields(self) -> dict:
        return {
            "trigger": self.trigger,
            "from": self.from_ref,
            "to": self.to_ref,
            "warnings": self.warnings,
            "dryRun": self.dry_run,
        }

    def human(self) -> str:
        src = (
            f"Account-{self.from_ref.get('number')}" if self.from_ref else "(none)"
        )
        dst = (
            f"Account-{self.to_ref.get('number')} ({self.to_ref.get('email')})"
            if self.to_ref
            else "?"
        )
        prefix = "[dry-run] would switch" if self.dry_run else "Switched"
        return f"{prefix} {src} -> {dst} ({self.trigger})"


@dataclass(frozen=True)
class NoSwitchEvent(AutoSwitchEvent):
    kind: ClassVar[str] = "no-switch"
    reason: str
    detail: str = ""

    def _fields(self) -> dict:
        return {"reason": self.reason, "detail": self.detail}

    def human(self) -> str:
        return f"no switch: {self.reason}" + (f" ({self.detail})" if self.detail else "")


@dataclass(frozen=True)
class QuarantineEvent(AutoSwitchEvent):
    kind: ClassVar[str] = "account-quarantined"
    number: str
    email: str
    reason: str

    def _fields(self) -> dict:
        return {"number": self.number, "email": self.email, "reason": self.reason}

    def human(self) -> str:
        return (
            f"Account-{self.number} ({self.email}) quarantined: {self.reason}. "
            f"Log in with it and run 'aswap --add-account --slot {self.number}' "
            "to recover."
        )


@dataclass(frozen=True)
class UnquarantineEvent(AutoSwitchEvent):
    kind: ClassVar[str] = "account-unquarantined"
    number: str
    email: str
    reason: str = "credentials-replaced"

    def _fields(self) -> dict:
        return {"number": self.number, "email": self.email, "reason": self.reason}

    def human(self) -> str:
        return f"Account-{self.number} ({self.email}) back in rotation ({self.reason})"


@dataclass(frozen=True)
class AllExhaustedEvent(AutoSwitchEvent):
    kind: ClassVar[str] = "all-exhausted"
    earliest_reset_at: str | None

    def _fields(self) -> dict:
        return {"earliestResetAt": self.earliest_reset_at}

    def human(self) -> str:
        if self.earliest_reset_at:
            return f"all accounts exhausted; earliest reset {self.earliest_reset_at}"
        return "all accounts exhausted; no reset time known"


@dataclass(frozen=True)
class SleepEvent(AutoSwitchEvent):
    kind: ClassVar[str] = "sleep"
    seconds: float
    until: str

    def _fields(self) -> dict:
        return {"seconds": round(self.seconds, 1), "until": self.until}

    def human(self) -> str:
        return f"sleeping {self.seconds / 60:.0f}m (until {self.until})"


@dataclass(frozen=True)
class ErrorEvent(AutoSwitchEvent):
    kind: ClassVar[str] = "error"
    message: str
    transient: bool = True

    def _fields(self) -> dict:
        return {"message": self.message, "transient": self.transient}

    def human(self) -> str:
        return f"error: {self.message}" + (" (will retry)" if self.transient else "")


@dataclass(frozen=True)
class ConfigWarningEvent(AutoSwitchEvent):
    """A configuration value is syntactically fine but provably inert (e.g.
    an ``autoswitch.model`` name no account reports). Not an error: the
    engine keeps running on the axes that do exist."""

    kind: ClassVar[str] = "config-warning"
    message: str

    def _fields(self) -> dict:
        return {"message": self.message}

    def human(self) -> str:
        return f"warning: {self.message}"


@dataclass(frozen=True)
class SettingsReloadedEvent(AutoSwitchEvent):
    """A running engine picked up a settings change (``aswap config set``,
    ``unset`` or ``import``) and uses it from this tick on.

    ``changes`` holds ``(key, old, new)`` per setting whose effective value
    moved; ``pinned`` holds ``(key, stored, kept)`` per setting whose stored
    value changed but that a command-line flag (or the TUI's session
    threshold) holds for this process. Only setting values are reported."""

    kind: ClassVar[str] = "settings-reloaded"
    changes: tuple[tuple[str, object, object], ...] = ()
    pinned: tuple[tuple[str, object, object], ...] = ()

    def _fields(self) -> dict:
        return {
            "changes": [
                {"key": key, "old": old, "new": new}
                for key, old, new in self.changes
            ],
            "pinned": [
                {"key": key, "stored": stored, "kept": kept}
                for key, stored, kept in self.pinned
            ],
        }

    def human(self) -> str:
        parts = [
            f"{key} {format_setting_value(old)} -> {format_setting_value(new)}"
            for key, old, new in self.changes
        ]
        parts += [
            f"{key} stays {format_setting_value(kept)} (set for this run; "
            f"stored {format_setting_value(stored)} applies after a restart)"
            for key, stored, kept in self.pinned
        ]
        return "settings reloaded: " + "; ".join(parts)


def apply_settings_reload(engine, reload: SettingsReload | None) -> None:
    """Apply one :meth:`LiveSettings.poll` result to a running engine.

    Shared by the Claude and Codex engines so both report a reload the same
    way: one ``config-warning`` per refused value, then the new settings
    (carried into derived state by ``engine._settings_applied(old, new)``),
    then one ``settings-reloaded`` event -- after the assignment, so a
    listener that reads ``engine.settings`` already sees the new values.
    """
    if reload is None:
        return
    for message in reload.problems:
        _logger.warning("settings reload: %s", message)
        engine._emit(ConfigWarningEvent(message=message))
    if not reload.changes and not reload.pinned:
        return
    if reload.changes:
        old, engine.settings = engine.settings, reload.settings
        engine._settings_applied(old, reload.settings)
    event = SettingsReloadedEvent(changes=reload.changes, pinned=reload.pinned)
    _logger.info("%s", event.human())
    engine._emit(event)


@dataclass(frozen=True)
class WarmupAutoEvent(AutoSwitchEvent):
    """One guarded five-hour warm-up decision emitted by ``aswap auto``."""

    kind: ClassVar[str] = "warmup"
    action: str
    number: str
    email: str
    detail: str

    def _fields(self) -> dict:
        return {
            "action": self.action,
            "account": {"number": self.number, "email": self.email},
            "detail": self.detail,
        }

    def human(self) -> str:
        suffix = " (5h warm-up sent)" if self.action == "warmed" else ""
        return (
            f"warm-up {self.action} Account-{self.number} ({self.email}): "
            f"{self.detail}{suffix}"
        )


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class TickOutcome(enum.Enum):
    """Outcome of one evaluation tick; values double as --once exit codes."""

    SWITCHED = 0
    ERROR = 1
    NO_ACTION = 2
    BLOCKED = 3  # wanted to switch but no viable target / all exhausted


# Quarantine state persisted fingerprints from a local refresh-token-only
# helper; oauth.credential_fingerprint is identical for refresh-token creds.
# Setup-token quarantines stored None where the shared helper now yields a
# full-content hash — those release once on first recheck and re-quarantine on
# the next dead freshen (one harmless extra cycle, migration only).
_refresh_fingerprint = oauth.credential_fingerprint


def _window_pcts(
    usage: dict | None, models: tuple[str, ...] = ()
) -> dict[str, float]:
    """Ordered window label → pct: "5h", "7d", then configured scoped names.

    Deliberately restricted to the windows the *decision* reads (same
    ``models`` filter): showing an unconfigured scoped window at 100% next
    to a switch onto that account would look like a bug, when the engine
    correctly ignored it. Full per-model usage lives in ``aswap list``.
    """
    return {
        name: pct for name, pct, _ in oauth.relevant_windows(usage, models)
    }


# Reset math moved to poll_policy with the cadence numbers; aliased for the
# engine's sleep scheduling and the test suite.
_limiting_reset_ts = poll_policy.limiting_reset_ts
_earliest_future_reset_ts = poll_policy.earliest_future_reset_ts
_parse_reset_ts = poll_policy.parse_reset_ts


def _seven_day_reset_ts(usage: dict | str | None, now: float) -> float | None:
    """Epoch of an account's 7-day (weekly) window reset, or None if unknown
    or already past.

    The consume-first strategy ranks by this — the weekly window is the
    perishable quota (the 5-hour one recycles too fast to be worth planning
    around). A stale snapshot can carry a ``resets_at`` that has since
    elapsed; treated as a real instant it would sort the *just-rolled-over*
    account (the least perishable quota of all) as "soonest", so past ==
    unknown. Plain ``ts <= now``: RESET_SLACK_S is poll-scheduling lag
    tolerance, not ranking input — padding here would turn a genuinely
    imminent reset into a false reset-unknown hold.
    """
    if isinstance(usage, dict):
        window = usage.get("seven_day")
        if isinstance(window, dict):
            ts = _parse_reset_ts(window.get("resets_at"))
            if ts is not None and ts > now:
                return ts
    return None


def _perishable_ts(
    inputs: dict[str, dict], num: str, usage: dict | str | None, now: float
) -> float | None:
    """When this account's weekly quota is lost, as a consume-first sort key.

    Normally its weekly reset. A subscription scheduled to end sooner loses
    the quota at its end, so that wins ("ending first"). With no readable
    ``resets_at`` the remembered weekly anchor stands in, so a freshly
    re-added account ranks correctly on its first tick.
    """
    ts = _seven_day_reset_ts(usage, now)
    extra = inputs.get(num) or {}
    if ts is None:
        anchor_next = extra.get("anchorNext")
        if anchor_next is not None and anchor_next > now:
            ts = anchor_next
    end = extra.get("endsAt")
    if end is not None and end > now and (ts is None or end < ts):
        return end
    return ts


def _binding_recovery_ts(
    usage: dict | str | None, models: Sequence[str], now: float
) -> float:
    """When this account's *binding* window comes back, as a sort key.

    The binding window is the one holding the account back — the highest
    utilization among the windows that gate it (the same set
    ``account_headroom`` measures, so ranking and headroom can never disagree
    about which window matters). Its reset is the moment the account becomes
    useful again.

    Not the weekly window: with every account in the 90s the thing that
    decides where to go is which 5-hour window rolls over first, and that is
    routinely minutes away while the weekly one is days away.

    Returns ``inf`` when unknown or already past, so such accounts sort last
    rather than masquerading as "back immediately" — a stale ``resets_at``
    would otherwise rank a snapshot nobody has refreshed above a measured,
    genuinely imminent one.
    """
    # Pick the BINDING window first, then ask for its reset. Filtering on the
    # reset before the max lets a lower window win whenever the binding one's
    # reset is unknown or past — measured: 7d at 95% with no resets_at and 5h
    # at 40% resetting in an hour returned "back in an hour", which is the
    # opposite of what binds. An account whose binding window has no usable
    # reset is one we cannot schedule around, and inf sorts it last.
    windows = list(oauth.relevant_windows(usage, models))
    if not windows:
        return float("inf")
    _label, _pct, resets_at = max(windows, key=lambda w: w[1])
    ts = _parse_reset_ts(resets_at)
    return ts if ts is not None and ts > now else float("inf")


def _every_account_above_threshold(
    candidates: Sequence[str],
    headroom: dict[str, float | None],
    active_headroom: float | None,
    threshold: float,
) -> bool:
    """Whether the active account AND every measured candidate are at or over
    the threshold — the state where "land somewhere healthy" has no answer.

    Requires the active account's own headroom to be known: without it we do
    not know we are in this state, and guessing here would relax the landing
    rule on an ordinary tick. An unmeasured candidate does not block the
    verdict (it may be healthy, but it cannot be *chosen* either — the caller
    skips ``None`` headroom) as long as at least one candidate was measured.
    """
    if active_headroom is None or (100.0 - active_headroom) < threshold:
        return False
    measured = [headroom.get(n) for n in candidates if headroom.get(n) is not None]
    if not measured:
        return False
    return all((100.0 - h) >= threshold for h in measured)


def _ref(number: str, email: str) -> dict:
    return {"number": int(number), "email": email}


def _headroom_by_account(
    usage: dict[str, dict | str | None], models: tuple[str, ...]
) -> dict[str, float | None]:
    """Per-account headroom derived from decision values."""
    return {
        num: oauth.account_headroom(
            value if isinstance(value, dict) else None, models
        )
        for num, value in usage.items()
    }


class AutoSwitchEngine:
    """Threshold-policy auto-switcher over a :class:`ClaudeAccountSwitcher`.

    ``on_event`` receives every :class:`AutoSwitchEvent`; exceptions it raises
    are not caught (a broken frontend should fail loudly in tests). ``clock``
    is wall time (persisted cooldown timestamps must survive processes).
    """

    def __init__(
        self,
        switcher: ClaudeAccountSwitcher,
        settings: AutoSwitchSettings,
        on_event: Callable[[AutoSwitchEvent], None],
        *,
        dry_run: bool = False,
        state_path: Path | None = None,
        clock: Callable[[], float] = time.time,
        pinned: Iterable[str] = (),
    ):
        self.switcher = switcher
        self.settings = settings
        # Live settings (see ``_reload_settings``): the fields this process
        # keeps whatever the document says are ``pinned`` (command-line
        # flags) plus any field the caller passed differently from the
        # document (a TUI session threshold, a caller's own settings).
        self._live = LiveSettings(switcher.backup_dir, settings, pinned)
        self._maintained_at = float("-inf")
        # Model(s) whose per-model weekly limit also binds the switch decision
        # (empty = account-wide 5h/7d only). ``settings.model`` is a comma-
        # separated list ("Fable", "Opus,Sonnet", "all"); parse once here and
        # pass everywhere usage windows are read — decisions, cadence, and
        # reset scheduling must all see the same axes.
        self._models = parse_model_names(settings.model)
        # Poll plans written by the collector must key on the same threshold/
        # models the engine decides with (CLI overrides included), not on
        # whatever the settings file happens to say.
        switcher.set_poll_policy_inputs(settings.threshold, self._models)
        self.on_event = on_event
        self.dry_run = dry_run
        self.state_path = state_path or (switcher.backup_dir / STATE_FILENAME)
        self.clock = clock
        self._stop = threading.Event()
        # Cuts the current inter-tick sleep short (a session threshold change
        # from the TUI should show a fresh decision now, not next interval).
        self._wake = threading.Event()
        self._unhealthy_ticks = 0
        # Both set per tick: a known-reset sleep target, and whether a BLOCKED
        # outcome is static enough (truly exhausted / no candidates) to wait
        # longer than the normal interval.
        self._sleep_until_ts: float | None = None
        self._blocked_wait_long = False
        # Idle-hold: when the active token expired while Claude Code owns it
        # (and is therefore idle), crawl instead of counting unhealthy ticks.
        # ``_idle_hold_since`` survives across ticks (elapsed-time cap);
        # ``_idle_hold_slow`` is per-tick like ``_blocked_wait_long``.
        self._idle_hold_since: float | None = None
        self._idle_hold_slow = False
        self._live_active = False
        # Tier capacity per slot (refreshed each tick), the work's burn rate,
        # and the active account's projected time to its limit.
        self._caps: dict[str, float] = {}
        self._burn = BurnTracker()
        self._burn_rate: float | None = None
        self._last_burn_rate: float | None = None
        self._recent_burns: list[tuple[float, float]] = []  # (collected_at, rate)
        self._time_to_limit: float | None = None
        self._limit_guard = False
        self._collected_at = 0.0
        # Rate-limit gate (gate.py): this tick's stored/live answers, the
        # verdict each one gave, the verdict last logged per account, and the
        # active's raw statusline readings. ``_runway_refused`` is the
        # active's runway when the last ranking refused targets on it.
        self._gate_recs: dict[str, dict] = {}
        self._gate_verdicts: dict[str, str] = {}
        self._gate_logged: dict[str, str] = {}
        self._live_now: list[dict] = []
        self._runway_refused: float | None = None
        # The active account is riding an open gate (endpoint 100%, API still
        # serving it): ticks then come at least every gate.RIDE_REPROBE_S.
        self._riding = False
        # The active account is in its last stretch (see ENDGAME_TICK_MIN_S).
        # Kept across ticks: the gate step of the next tick reads it.
        self._endgame = False
        # When the active account's last point runs out (see edge.py), and
        # whether this tick's move is that clock's (not a refusal's).
        self._edge = edge.LastPointClock()
        self._edge_deadline: float | None = None
        self._edge_forced = False
        self._edge_logged: tuple | None = None
        # One-shot typo guard for ``autoswitch.model``: resolved (and possibly
        # warned) on the first tick where every relevant account has readable
        # usage — adaptive polling legitimately leaves gaps before that.
        self._model_check_done = not self._models
        # Manual hold. ``_last_seen`` is the active account of the previous
        # tick — the fallback for spotting a hand switch before the state file
        # records one, and a dry run's own view; ``_stored_seen`` is the
        # state file's ``lastSeenActive`` as of that tick; ``_dry_hold`` is a
        # dry run's hold (never written); ``manual_hold`` is (account, until)
        # as of the last tick, for frontends; ``_hold_noted`` throttles the
        # suppression log.
        self._last_seen: str | None = None
        self._stored_seen: object = None
        self._dry_hold: dict | None = None
        self.manual_hold: tuple[str, float] | None = None
        self._hold_noted: tuple[tuple[str, float], float] | None = None
        # Auto-add (``autoswitch.autoAddAccounts``): the previous tick's
        # live-login observation — a login is adopted only once two
        # consecutive polls agree — and a once-per-process hint when off.
        self._live_login_seen: object = None
        self._auto_add_hinted = False
        # Active account as of the last plan re-check: a change re-reads a
        # plan cached as lapsed at once (see ``_recheck_plans``).
        self._plan_seen_active: str | None = None
        # The roster as of this tick, number -> (email, organization uuid).
        # Slot numbers can change under a running engine (``aswap renumber``,
        # ``move``, ``swap`` from another process); see ``_follow_roster``.
        self._roster: dict[str, tuple[str, str]] | None = None
        self._warmup: WarmupEngine | None = None
        self._next_warmup_at: float | None = None
        if settings.warmup_five_hour:
            self._enable_warmup()

    def _enable_warmup(self) -> None:
        self._warmup = WarmupEngine(
            self.switcher,
            emit=self._emit_warmup,
            dry_run=self.dry_run,
            clock=self.clock,
        )
        # A newly enabled or newly started auto process checks immediately.
        # The warmer's persistent state still suppresses duplicate requests.
        self._next_warmup_at = self.clock()

    def _emit_warmup(self, event: WarmupEvent) -> None:
        self._emit(
            WarmupAutoEvent(
                action=event.kind,
                number=event.account_number,
                email=event.email,
                detail=event.detail,
            )
        )

    def _run_warmup_if_due(self) -> bool:
        """Run one integrated pass when due; return whether that pass failed."""
        warmer = self._warmup
        due = self._next_warmup_at
        now = self.clock()
        if self._stop.is_set() or warmer is None or due is None or now < due:
            return False

        try:
            return warmer.tick().failed > 0
        except ClaudeSwitchError as exc:
            self._emit(
                ErrorEvent(message=f"five-hour warm-up: {exc}", transient=True)
            )
            return True
        except Exception as exc:  # pragma: no cover - defensive boundary
            self._emit(
                ErrorEvent(
                    message=f"five-hour warm-up: {type(exc).__name__}: {exc}",
                    transient=True,
                )
            )
            return True
        finally:
            # Schedule from completion so a slow pass cannot make the next
            # auto-switch tick immediately run another full account sweep.
            if self._warmup is warmer:
                self._next_warmup_at = self.clock() + WARMUP_INTERVAL_SECONDS

    # -- state file ---------------------------------------------------------

    def _state_lock(self) -> FileLock:
        return FileLock(self.state_path.parent / ".autoswitch_state.lock")

    def _read_state(self) -> dict:
        try:
            raw = json.loads(statedb.read_text(self.state_path))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _mutate_state(self, mutator: Callable[[dict], None]) -> dict:
        """Read-modify-write the state file under its lock; returns new state.

        The lock prevents two concurrent engines (loop + cron ``--once``) from
        overwriting each other's quarantine/cooldown updates. Never called
        while any other lock is held.
        """
        with self._state_lock():
            state = self._read_state()
            state["schemaVersion"] = STATE_SCHEMA_VERSION
            mutator(state)
            atomic_write_json(self.state_path, state)
            return state

    # -- quarantine -----------------------------------------------------------

    def _quarantine(self, number: str, email: str, reason: str) -> None:
        creds = self.switcher.read_account_credentials(number, email)
        fingerprint = _refresh_fingerprint(creds) if creds else None

        def add(state: dict) -> None:
            state.setdefault("quarantine", {})[number] = {
                "email": email,
                "reason": reason,
                "at": _now_iso(),
                "refreshTokenFingerprint": fingerprint,
            }

        self._mutate_state(add)
        self._emit(QuarantineEvent(number=number, email=email, reason=reason))

    def _release_recovered_quarantines(self, state: dict) -> dict:
        """Drop quarantine entries whose credential was replaced since.

        A changed refresh-token fingerprint (or a removed/re-added slot) means
        the user re-logged in and re-captured the account — the dead lineage
        is gone, so it re-enters rotation.
        """
        quarantine = state.get("quarantine")
        if not isinstance(quarantine, dict) or not quarantine:
            return state
        to_release: list[tuple[str, str, str]] = []
        # A quarantined account that now lives under another number (a
        # renumber/move the roster commit could not remap, e.g. an entry
        # written by an engine that decided on the old numbers) keeps its
        # quarantine there, as long as it is provably the same lineage.
        to_move: dict[str, str] = {}
        for number, entry in quarantine.items():
            email_now = self.switcher.account_email(number)
            if not email_now or email_now != entry.get("email"):
                moved_to = self._quarantine_moved_to(number, entry, quarantine)
                if moved_to is not None and moved_to not in to_move.values():
                    to_move[number] = moved_to
                    continue
                to_release.append(
                    (number, entry.get("email", ""), "account-replaced")
                )
                continue
            creds = self.switcher.read_account_credentials(number, email_now)
            fingerprint = _refresh_fingerprint(creds) if creds else None
            if fingerprint != entry.get("refreshTokenFingerprint"):
                to_release.append((number, email_now, "credentials-replaced"))
        if not to_release and not to_move:
            return state

        def drop(s: dict) -> None:
            q = s.get("quarantine")
            if isinstance(q, dict):
                for number, _, _ in to_release:
                    q.pop(number, None)
                moved = {
                    new: q.pop(old) for old, new in to_move.items() if old in q
                }
                for new, entry in moved.items():
                    q.setdefault(new, entry)

        state = self._mutate_state(drop)
        for number, email, reason in to_release:
            self._emit(UnquarantineEvent(number=number, email=email, reason=reason))
        return state

    def _quarantine_moved_to(
        self, number: str, entry: dict, quarantine: dict
    ) -> str | None:
        """The one other number holding ``entry``'s account and lineage, if any.

        Same email, exactly one such slot, not already quarantined, and the
        stored refresh-token fingerprint unchanged: the account moved, it was
        not re-captured, so the dead lineage is still dead.
        """
        email = entry.get("email")
        fingerprint = entry.get("refreshTokenFingerprint")
        if not email or not fingerprint:
            return None
        try:
            roster = self.switcher.roster_identities()
        except Exception:  # noqa: BLE001 - fall back to releasing
            return None
        hits = [n for n, (e, _org) in roster.items() if e == email and n != number]
        if len(hits) != 1 or hits[0] in quarantine:
            return None
        creds = self.switcher.read_account_credentials(hits[0], email)
        if not creds or _refresh_fingerprint(creds) != fingerprint:
            return None
        return hits[0]

    # -- roster changes under a running engine --------------------------------

    def _follow_roster(self) -> None:
        """Keep this engine's slot-keyed memory on the right accounts.

        Another process may renumber, move or swap accounts while this engine
        runs. The persisted state follows in the same transaction as the
        roster (``ClaudeAccountSwitcher._commit_roster``) and every switch
        names the account it expects (``switch_to(expect=...)``), so a stale
        number can neither switch to the wrong account nor misattribute a
        quarantine. What is left is this process's own memory: the previous
        active account, the plan re-check marker, logged gate verdicts, a dry
        run's hold and the burn samples. They are translated by identity here,
        once per tick; an account that left (or whose identity is ambiguous)
        is dropped.
        """
        try:
            roster = self.switcher.roster_identities()
        except Exception:  # noqa: BLE001 - next tick tries again
            return
        prev, self._roster = self._roster, roster
        if prev is None or prev == roster:
            return
        counts: dict[tuple[str, str], int] = {}
        for identity in roster.values():
            counts[identity] = counts.get(identity, 0) + 1
        by_identity = {identity: num for num, identity in roster.items()}
        mapping = {
            num: by_identity[identity]
            for num, identity in prev.items()
            if counts.get(identity) == 1
        }
        if all(old == new for old, new in mapping.items()) and len(mapping) == len(prev):
            return  # accounts added only: nothing moved

        def tr(value: object) -> str | None:
            return mapping.get(str(value)) if value is not None else None

        _logger.info(
            "Account numbers changed under the running engine: %s",
            ", ".join(f"{o}->{n}" for o, n in sorted(mapping.items()) if o != n) or "-",
        )
        self._last_seen = tr(self._last_seen)
        self._plan_seen_active = tr(self._plan_seen_active)
        self._gate_logged = {
            new: v for k, v in self._gate_logged.items() if (new := tr(k)) is not None
        }
        if isinstance(self._dry_hold, dict):
            account = tr(self._dry_hold.get("account"))
            self._dry_hold = (
                {**self._dry_hold, "account": account} if account is not None else None
            )
        # The state file's lastSeenActive was remapped with the roster; a dry
        # run compares against the value it last read, so follow it too.
        if self._stored_seen is not None and str(self._stored_seen) in mapping:
            new_seen = mapping[str(self._stored_seen)]
            self._stored_seen = (
                int(new_seen) if isinstance(self._stored_seen, int) else new_seen
            )
        self._hold_noted = None
        self.manual_hold = None
        self._burn.renumber(mapping)

    # -- freshening -----------------------------------------------------------

    def _freshen_target(self, number: str, email: str) -> str:
        """Ensure a candidate's stored token outlives Claude Code's 5-min
        refresh buffer before it gets activated.

        Returns ``"ok"``, ``"invalid_grant"`` (dead lineage — quarantine),
        ``"identity-conflict"`` (alive but authenticates as a different
        account — quarantine, do not activate), ``"transient"`` (network
        trouble — try again next tick) or ``"skip-live-session"``. Only ever
        touches the slot's *backup* store; the active credential belongs to
        Claude Code.
        """
        if self.switcher.account_kind_for(number) == "api_key":
            return "ok"  # API keys don't expire/refresh
        if self.switcher.live_session_pids_for(number, email):
            # A live `aswap run` session owns this account's token in its own
            # profile. Auto-activating it as the default login too would put
            # one rotating refresh token in two config dirs (the stale-copy
            # failure class) with nobody reading the warning — and its quota
            # is already being consumed by that session anyway. Manual
            # switch_to keeps its warn-and-proceed behavior; auto skips.
            return "skip-live-session"
        creds = self.switcher.read_account_credentials(number, email)
        if not creds:
            return "transient"
        data = oauth.extract_oauth_data(creds)
        if not data:
            return "invalid_grant"
        expires_at = data.get("expiresAt")
        now_ms = self.clock() * 1000
        near_expiry = (
            isinstance(expires_at, (int, float))
            and now_ms + FRESHEN_BUFFER_MS >= expires_at
        )
        if not near_expiry:
            return "ok"
        # The consume gate serializes every backup-rt POST (the recovery
        # branch in `_fetch_active_usage` is a second call site, under the
        # same per-slot consume lock):
        # it re-reads under the slot lock (our snapshot may be superseded),
        # consults the session profile for a newer generation, and persists
        # via fingerprint CAS — so a freshen racing the collector (or a
        # sibling surface) can no longer double-consume one grant.
        outcome = self.switcher.consume_backup_grant(number, email, creds)
        if outcome.error is None and outcome.credentials:
            # The gate already persisted the successor (or adopted a racing
            # writer's newer lineage) under its own lock.
            if self._note_token_identity(number, outcome.token_account):
                # The slot's stored credential authenticates as a *different*
                # account — activating it would put the user on the wrong
                # account with every gauge reading normal. Not a viable
                # target; the caller quarantines it (released automatically
                # once the credential is replaced by a re-add).
                return "identity-conflict"
            return "ok"
        if outcome.error in ("invalid_grant", "no_refresh_token"):
            return "invalid_grant"
        if outcome.error in _SYSTEMIC_STATUSES:
            # Deterministic conditions, not network trouble: every candidate
            # refuses identically and keeps refusing until something outside
            # this process changes — the shell for store-unmirrored (an
            # inherited CLAUDE_SECURESTORAGE_CONFIG_DIR), our OAuth client
            # registration for invalid_client. Reported distinctly so the tick
            # error names the real cause instead of "(network?)", which would
            # send the user to check a connection that is fine.
            return outcome.error
        return "transient"

    def _note_token_identity(
        self, number: str, token_account: dict | None
    ) -> bool:
        """Use the token endpoint's free identity to verify/backfill a slot.

        The refresh grant just ran against the slot's own stored credential,
        so ``token_account`` (when the server includes it) names who that
        credential really is. Returns True on a *conflict*: the credential
        authenticates under a different organization than the slot records
        (org compared first, whenever both sides record one), or as a
        different account uuid. An empty slot uuid (blank-uuid records from
        older versions, add-token placeholders) is backfilled — but only
        when no org conflict exists: a wrong-org credential is evidence the
        slot holds the wrong account, and backfilling *its* uuid would
        poison the slot's identity record (backfill never rewrites a
        non-empty uuid, so that corruption would be sticky).

        ``_parse_token_account`` already enforces a strict boundary, but this
        identity is opportunistic — re-check types here so malformed data can
        never break the freshen that carried it (the successor credential is
        already persisted by the time this runs).
        """
        if not isinstance(token_account, dict):
            return False
        ta_uuid = token_account.get("uuid")
        if not isinstance(ta_uuid, str) or not ta_uuid.strip():
            return False
        ta_uuid = ta_uuid.strip()
        slot_identity = self.switcher.account_identity(number)
        ta_org = token_account.get("organizationUuid")
        slot_org = slot_identity.get("organizationUuid") or ""
        if isinstance(ta_org, str) and ta_org and slot_org and ta_org != slot_org:
            return True
        if not slot_identity.get("uuid"):
            try:
                self.switcher.backfill_account_uuid(number, ta_uuid)
            except Exception as e:  # never let bookkeeping break a freshen
                _logger.debug("uuid backfill failed for account %s: %r", number, e)
            return False
        return slot_identity["uuid"] != ta_uuid

    # -- tick -----------------------------------------------------------------

    def tick(self) -> TickOutcome:
        """Evaluate once: maybe switch, then run a due warm-up pass. Never raises."""
        mono = time.monotonic()
        if not self.dry_run and mono - self._maintained_at >= MAINTAIN_INTERVAL_S:
            self._maintained_at = mono
            snapshots.maintain(self.switcher.backup_dir, _logger)
        try:
            outcome = self._tick_inner()
        except ClaudeSwitchError as e:
            self._emit(ErrorEvent(message=str(e), transient=True))
            outcome = TickOutcome.ERROR
        except Exception as e:  # pragma: no cover - safety net
            self._emit(
                ErrorEvent(message=f"{type(e).__name__}: {e}", transient=True)
            )
            outcome = TickOutcome.ERROR
        if self._run_warmup_if_due():
            return TickOutcome.ERROR
        return outcome

    def _tick_inner(self) -> TickOutcome:
        self._sleep_until_ts = None
        self._blocked_wait_long = False
        self._idle_hold_slow = False
        self._live_active = False
        self._time_to_limit = None
        self._limit_guard = False
        self._edge_deadline = None
        self._edge_forced = False
        self.manual_hold = None
        self._follow_roster()
        self._reload_settings()
        settings = self.settings
        self._caps = {
            num: tier_capacity(inputs["tier"])
            # Tiers do not depend on the time; no clock read (tests script it).
            for num, inputs in self._perish_inputs(None).items()
            if inputs.get("tier")
        }
        state = self._read_state()
        if not self.dry_run:
            # Dry-run must not write anything, so recovered quarantines are
            # only released (state mutation) on real ticks.
            state = self._release_recovered_quarantines(state)
        quarantined = set(
            state.get("quarantine", {})
            if isinstance(state.get("quarantine"), dict)
            else {}
        )

        self._auto_add_live_login()
        current = self.switcher.current_account_number()
        self._recheck_plans(current)
        if current is None:
            self._emit(
                PollEvent(active=None, headroom={}, threshold=settings.threshold)
            )
            if self.switcher.has_live_login():
                # Live login exists but aswap doesn't manage it: never act —
                # a switch would overwrite it without a backup.
                self._emit(
                    NoSwitchEvent(
                        reason="unmanaged-active-account",
                        detail=(
                            "added automatically once the login is stable"
                            if settings.auto_add_accounts and not self.dry_run
                            else "run 'aswap --add-account' to include it in rotation"
                        ),
                    )
                )
            else:
                self._emit(
                    NoSwitchEvent(
                        reason="no-active-account",
                        detail="log in and run 'aswap --add-account' first",
                    )
                )
            return TickOutcome.NO_ACTION

        current_email = self.switcher.account_email(current)
        active_ref = _ref(current, current_email) if current_email else {
            "number": int(current),
            "email": "",
        }

        entries, usage, headroom = self._collect_scheduled_usage(
            current, quarantined, threshold=settings.threshold
        )
        headroom = self._gate_pass(current, usage, quarantined)
        self._emit(
            PollEvent(
                active=active_ref,
                headroom=headroom,
                threshold=settings.threshold,
                fetch_errors={
                    num: entry.last_error
                    for num, entry in entries.items()
                    if usage.get(num) is None and entry.last_error
                },
                windows={
                    num: pcts
                    for num, value in usage.items()
                    if (pcts := _window_pcts(
                        value if isinstance(value, dict) else None, self._models
                    ))
                },
            )
        )

        if not settings.enabled:
            # Observe only. Deliberately AFTER the usage collection and the
            # poll event above, not an early exit: placement is external, but
            # the freshness of the usage cache is exactly what an external
            # placer (and `aswap watch`, and the TUI) reads. Exiting here
            # would leave every surface serving stale numbers to punish the
            # user for turning switching off.
            self._emit(
                NoSwitchEvent(
                    reason="placement-external",
                    detail="autoswitch.enabled=false; usage still collected",
                )
            )
            return TickOutcome.NO_ACTION

        hold_until = self._manual_hold_until(state, current)

        if not self._model_check_done:
            self._check_model_names(quarantined, usage)

        if (
            self.switcher.account_kind_for(current) == "api_key"
            and not settings.include_api_key_accounts
        ):
            self._emit(
                NoSwitchEvent(
                    reason="active-api-key",
                    detail="API-key accounts have no quota to watch",
                )
            )
            return TickOutcome.NO_ACTION

        active_headroom = headroom.get(current)
        # Riding an open gate: the endpoint says 100% but the API still serves
        # this account. Neither the limit guard nor the burn projection mean
        # anything on an estimated half point; the next closed gate (re-probed
        # every few minutes) is the at-limit escape.
        riding = self._gate_verdicts.get(current) == gate.OPEN
        if active_headroom is not None and active_headroom > 0 and self._edge_due(
            current, usage.get(current), entries.get(current)
        ):
            # The last point is spent by the clock, before any refusal.
            active_headroom = 0.0
            headroom[current] = 0.0
            riding = False
            self._edge_forced = True
        self._riding = riding
        if active_headroom is not None:
            self._unhealthy_ticks = 0
            self._idle_hold_since = None
            utilization = 100.0 - active_headroom
            # Burn is measured on the endpoint's own numbers, never the gate's
            # estimate, so a gate flip is not read as burn or as a rollover.
            self._burn.record(*self._burn_sample(
                current, usage.get(current), entries.get(current), utilization
            ))
            self._burn_rate = self._burn.rate()
            if self._burn_rate:
                self._last_burn_rate = self._burn_rate
                self._recent_burns.append((self._collected_at, self._burn_rate))
            # The projection takes the faster of the windowed average and the
            # pace the latest readings prove (`BurnTracker.fast_rate`): the
            # average lags a sudden burn by up to a minute. Only the guard
            # uses the fast pace; the landing and runway checks keep the
            # average and its peak.
            rate = max(self._burn_rate or 0.0, self._burn.fast_rate() or 0.0)
            if rate:
                self._time_to_limit = active_headroom * self._cap(current) / rate
            # Below the threshold but burning into the limit faster than the
            # next tick can react: move now, as if over the threshold. Above
            # it the cooldown still holds (only the at-limit escape skips it):
            # the window average carries the last account's burn onto a new
            # one, and letting it skip the cooldown there traded two spent
            # accounts back and forth (test_limit_guard_does_not_ping_pong_
            # between_spent_accounts).
            self._limit_guard = (
                utilization < settings.threshold
                and not riding
                and active_headroom > 0
                and self._time_to_limit is not None
                and rate > 0
                and self._time_to_limit <= LIMIT_GUARD_S
            )
            self._endgame = riding or (
                self._time_to_limit is not None
                and self._time_to_limit <= GUARD_PREFETCH_S
            )
            if utilization < settings.threshold and not self._limit_guard:
                if settings.strategy != "consume-first":
                    self._emit(
                        NoSwitchEvent(
                            reason="below-threshold",
                            # Both sides through pct_label: .0f utilization could
                            # display an impossible "100% < 99.9%".
                            detail=(
                                f"{pct_label(utilization)}% < "
                                f"{pct_label(settings.threshold)}%"
                                + (
                                    "; usage API says 100% but the gate is "
                                    "open, riding it" if riding else ""
                                )
                            ),
                        )
                    )
                    return TickOutcome.NO_ACTION
                # consume-first: below the threshold we still proactively move to
                # whichever account's weekly window resets soonest, to burn the
                # most-perishable quota first. Candidate selection decides whether
                # a sooner-resetting account with room actually exists.
                trigger = "consume-first"
            else:
                trigger = "at-limit" if active_headroom <= 0 else "proactive"
        else:
            if usage.get(current) == USAGE_TOKEN_EXPIRED:
                # Expired and the refresh could not complete this pass (lock
                # contention, unattributable lineage, failed persist, or the
                # row's failure backoff gating the fetch). The locked-refresh
                # path retries on later passes — no quota burn, nothing to
                # switch for yet; crawl slowly instead of burning failover
                # ticks (Finding 2 of the usage-lapse investigation).
                now = self.clock()
                if self._idle_hold_since is None:
                    self._idle_hold_since = now
                if now - self._idle_hold_since <= IDLE_HOLD_MAX_S:
                    self._unhealthy_ticks = 0
                    self._idle_hold_slow = True
                    self._emit(
                        NoSwitchEvent(
                            reason="active-idle",
                            detail=(
                                "token expired while Claude Code is idle; "
                                "resumes on next use"
                            ),
                        )
                    )
                    return TickOutcome.NO_ACTION
                # Held far longer than any idle nap should need — likely a
                # dead refresh token with an *active* user. Fall through to
                # normal unhealthy counting so failover can still happen.
                _logger.warning(
                    "Active token expired and owned for over %.0f minutes; "
                    "resuming unhealthy counting (dead refresh token?)",
                    IDLE_HOLD_MAX_S / 60,
                )
            else:
                self._idle_hold_since = None
            self._unhealthy_ticks += 1
            if self._unhealthy_ticks < settings.unhealthy_ticks:
                self._emit(
                    NoSwitchEvent(
                        reason="active-usage-unknown",
                        detail=(
                            f"{self._unhealthy_ticks}/{settings.unhealthy_ticks} "
                            "before failover"
                        ),
                    )
                )
                return TickOutcome.NO_ACTION
            trigger = "failover"

        if (
            trigger in ("proactive", "consume-first")
            and not self._limit_guard  # waiting out a cooldown means hitting the limit
            and self._in_cooldown(state)
        ):
            self._emit(NoSwitchEvent(reason="cooldown"))
            return TickOutcome.NO_ACTION

        # -- candidate selection ------------------------------------------
        candidates = [
            num
            for num in self.switcher.switchable_account_numbers()
            if num != current and num not in quarantined
        ]
        oauth_candidates = [
            n for n in candidates if self.switcher.account_kind_for(n) != "api_key"
        ]
        # The no-return bar itself lives in `_rank` below: it is a statement
        # about the CHOICE, so it belongs where the choice is made rather than
        # in this census of what exists. See `_no_return_account` for the
        # incident, the scoping, and the release.
        metered_candidates = (
            [n for n in candidates if self.switcher.account_kind_for(n) == "api_key"]
            if settings.include_api_key_accounts
            else []
        )
        if (
            trigger == "consume-first"
            and not oauth_candidates
            and active_headroom is not None
        ):
            # Healthy below-threshold account with no OAuth peer to compare
            # against — the same state `best` reports as below-threshold
            # NO_ACTION before ever reaching candidate selection. API-key
            # candidates don't change the outcome: they have no weekly window
            # to consume, so a consume-first nudge never targets them. Keep
            # the exit-code contract identical across strategies: cron
            # wrappers keying on BLOCKED must not see false "blocked" from
            # the flag alone.
            self._emit(
                NoSwitchEvent(
                    reason="below-threshold",
                    detail=(
                        f"{pct_label(100.0 - active_headroom)}% < "
                        f"{pct_label(settings.threshold)}%"
                    ),
                )
            )
            return TickOutcome.NO_ACTION
        if self._edge_forced:
            # Leaving a last point for another one in an unknown state would
            # only come straight back: only accounts with more than that.
            oauth_candidates = [
                n for n in oauth_candidates
                if (headroom.get(n) or 0.0) > gate.OPEN_HEADROOM
            ]
            if not oauth_candidates and not metered_candidates:
                self._emit(
                    NoSwitchEvent(
                        reason="last-point",
                        detail="no other account has more than its last point left",
                    )
                )
                return TickOutcome.NO_ACTION
        if not oauth_candidates and not metered_candidates:
            # Won't change until the user adds/recovers an account — no point
            # re-polling at full cadence.
            self._blocked_wait_long = True
            self._emit(NoSwitchEvent(reason="no-candidates"))
            return TickOutcome.BLOCKED

        consume_first = settings.strategy == "consume-first"

        def _rank(**kw):
            """Rank with the no-return bar, and WITHOUT it if that empties AND
            the barred account is a different proposition from the one we left.

            Emptiness alone cannot be the release. On two accounts there is
            exactly one candidate, so barring it ALWAYS empties the list —
            measured, sweeping active x barred headroom x both reset shapes,
            `n=2 barred-rank EMPTY=320 NONEMPTY=0`. An emptiness-only release
            therefore fires every tick and the bar is inert at the fleet size
            the flap was reported on: pcts 92/92, resets 500h/400h, 60 ticks
            gave `[1, 2, 1, 2]` with the bar on and the identical `[1, 2, 1, 2]`
            with `lastSwitchFrom` popped every tick.

            "BARRING LEAVES NOTHING" AND "WE ARE FLAPPING" ARE DIFFERENT
            STATES, and at n=2 they are always the same state — which is how
            one swallowed the other. The ranking cannot separate them: it sees
            only the present, and both look like an empty list. What separates
            them is WHY the ranking flipped. Traced at each leg of that walk:

                t8   1->2   left 1 holding 4.0 pts, 500h out
                t20  2->1   account 1 still 4.0 pts, still 500h out
                t22  1->2   account 2 still 2.0 pts, still 400h out

            Every return won because the ACTIVE burned down, never because the
            target recovered. So the release asks the one question the ranking
            cannot: is the account we left better than when we left it?

            ON BOTH AXES THE RANKING USES, and with the margins it already
            uses — ``SPENT_HEADROOM_PCT`` of headroom (below that an edge is
            under two poll intervals of work) or ``RECOVERY_HYSTERESIS_S``
            sooner. An account's headroom rises only when a window rolls over
            and its binding reset only moves nearer when a nearer window
            starts binding, so both are real events rather than the boundary
            crossings burn manufactures for free.

            Emptiness still decides whether to ASK. Where the bar leaves a
            real alternative it simply applies, so a fleet with somewhere else
            to go is untouched by any of this.

            Cheap: the retry runs only when the barred list came back empty,
            which is the tick that was about to do nothing anyway.
            """
            # Recomputed per snapshot, never once per tick: the consume-first
            # two-phase commit replaces `headroom` and `active_headroom` and
            # re-ranks, and the ratio release consumes exactly those two
            # values. Computed once, the bar answered from a snapshot the
            # ranking had already thrown away — `left=20 active=30` bars,
            # `left=90 active=10` releases, and phase 2 is where that flips.
            recovered = self._left_account_recovered(
                state,
                kw["usage"],
                kw["headroom"],
                kw["active_headroom"],
                kw["settings"],
                kw["now"],
                kw["current"],
            )
            no_return = self._no_return_account(
                trigger,
                state,
                kw["headroom"],
                kw["active_headroom"],
                recovered,
                kw["settings"],
                kw["current"],
            )
            ranked = self._rank_candidates(no_return=no_return, **kw)
            if no_return is not None and not ranked[0] and recovered:
                unbarred = self._rank_candidates(no_return=None, **kw)
                if unbarred[0]:
                    return unbarred
            return ranked

        decided_now = self.clock()
        ordered, any_known, active_reset_ts = _rank(
            trigger=trigger,
            consume_first=consume_first,
            oauth_candidates=oauth_candidates,
            usage=usage,
            headroom=headroom,
            current=current,
            active_headroom=active_headroom,
            settings=settings,
            now=decided_now,
        )

        if trigger in OPTIONAL_TRIGGERS and ordered and hold_until is not None:
            # Decided on the provisional ranking, before phase 2 below: a
            # held move must not spend the escalated fetches it would need.
            self._note_held_move(current, ordered[0], trigger, hold_until)
            return TickOutcome.NO_ACTION

        if trigger == "consume-first" and ordered:
            # Two-phase commit: the provisional pick may have ridden a
            # snapshot up to CANDIDATE_MAX_INTERVAL_S stale — consume-first
            # decides below the threshold, where the collector only escalates
            # inside the ESCALATION_MARGIN_PCT band (flat-traffic invariant).
            # A switch is imminent, so spend the fetches now and re-decide on
            # fresh data.
            # reserve() serves just-fetched accounts from the store, so this
            # is cheap in-tick and plan-bounded across ticks. The trigger is
            # deliberately NOT re-classified if the fresh active crossed the
            # threshold: a still-qualifying sooner target switches anyway,
            # and otherwise the next tick escalates normally and escapes.
            entries = self.switcher.usage_entries_by_account(
                fetch={current, *candidates}
            )
            usage = {num: entry.decision_value() for num, entry in entries.items()}
            headroom = self._apply_gates(current, usage, self._collected_at)
            active_headroom = headroom.get(current)
            decided_now = self.clock()
            ordered, any_known, active_reset_ts = _rank(
                trigger=trigger,
                consume_first=consume_first,
                oauth_candidates=oauth_candidates,
                usage=usage,
                headroom=headroom,
                current=current,
                active_headroom=active_headroom,
                settings=settings,
                now=decided_now,
            )

        if not ordered and metered_candidates and trigger != "consume-first":
            # Last resort when we must move: metered API-key accounts
            # (unmeasurable headroom). Never for a below-threshold consume-first
            # nudge — those API-key accounts have no weekly window to consume.
            ordered = metered_candidates

        if not ordered:
            if not any_known:
                # No candidate readable this tick — true for every strategy,
                # and must not be dressed up as a consume-first hold.
                self._emit(
                    NoSwitchEvent(
                        reason="no-comparison",
                        detail="no candidate has readable usage",
                    )
                )
                return TickOutcome.BLOCKED
            if trigger == "proactive" and self._runway_refused is not None:
                # Every target runs out about as fast as the active account:
                # moving would only start the same race somewhere else (and
                # the next tick would move again). Ride the active to its
                # limit; the at-limit escape moves the work when it is spent.
                self._emit(
                    NoSwitchEvent(
                        reason="riding-active",
                        detail=(
                            f"no target outlasts account {current}'s "
                            f"~{self._runway_refused / 60:.1f} min at the peak "
                            "burn; riding it to the limit"
                        ),
                    )
                )
                return TickOutcome.NO_ACTION
            if trigger == "consume-first":
                # Below the threshold and healthy: staying put is a correct
                # outcome, never a block. Distinguish *why* nothing qualified
                # so an opted-in user can see the strategy working (or inert).
                if active_reset_ts is None:
                    # The strictly-sooner filter skips every candidate when the
                    # active account's weekly reset is unknown — without this
                    # reason the strategy would look enabled while doing
                    # nothing, with no way to tell.
                    self._emit(
                        NoSwitchEvent(
                            reason="reset-unknown",
                            detail=(
                                "active account's weekly reset time is "
                                "unknown; consume-first is idle until it "
                                "is reported"
                            ),
                        )
                    )
                    return TickOutcome.NO_ACTION
                # Covers both "everyone resets later" and "sooner ones have no
                # room" — don't claim the active account resets first when the
                # real story may be exhausted candidates.
                self._emit(
                    NoSwitchEvent(
                        reason="already-consuming-soonest",
                        detail="no sooner-resetting account with room to spare",
                    )
                )
                return TickOutcome.NO_ACTION
            # "All exhausted" (and its bounded reset-aware sleep) only when it's
            # literally true: every candidate's usage is known and at its
            # limit. A candidate that merely failed the proactive hysteresis
            # gate, or one whose usage is unreadable this tick, can become
            # viable at any moment — and the active account can hit 100% and
            # need the at-limit escape — so those keep the normal cadence.
            candidate_headrooms = [headroom.get(n) for n in oauth_candidates]
            truly_exhausted = all(
                h is not None and h <= 0 for h in candidate_headrooms
            )
            if not truly_exhausted:
                self._emit(
                    NoSwitchEvent(
                        reason="no-qualifying-candidate",
                        detail=(
                            "no candidate is below the threshold and better "
                            "than the active account by the hysteresis "
                            "margin, or usage is unreadable this tick"
                        ),
                    )
                )
                return TickOutcome.BLOCKED
            self._blocked_wait_long = True
            earliest = self._earliest_recovery(usage)
            if earliest is not None:
                self._sleep_until_ts = earliest.timestamp() + RESET_SLACK_S
            self._emit(
                AllExhaustedEvent(
                    earliest_reset_at=(
                        earliest.isoformat().replace("+00:00", "Z")
                        if earliest
                        else None
                    )
                )
            )
            return TickOutcome.BLOCKED

        # -- freshen + switch ----------------------------------------------
        # The departure snapshot of the account we are leaving, taken from the
        # SAME `usage`/`headroom` the ranking just decided on — for
        # consume-first that is the phase-2 refetch, not the stale one.
        left_snapshot = (
            active_headroom,
            _binding_recovery_ts(usage.get(current), self._models, decided_now),
        )
        transient_failure = False
        systemic = ""
        for num in ordered:
            email = self.switcher.account_email(num)
            if trigger == "consume-first":
                # The phase-2 refetch is best-effort: the collector refuses
                # accounts in failure backoff or claimed by a concurrent
                # poller, which then serve their stored entries. Consume-first
                # is opportunistic, not an escape — never act on stale data
                # or slide to a worse-ranked target; hold and retry next tick.
                entry = entries.get(num)
                if entry is None or not entry.fresh(self.clock()):
                    self._emit(
                        NoSwitchEvent(
                            reason="stale-usage",
                            detail=(
                                f"account {num} usage could not be refreshed "
                                "this tick (backoff or a concurrent poller); "
                                "retrying"
                            ),
                        )
                    )
                    return TickOutcome.NO_ACTION
            if self.dry_run:
                # Dry-run stops at the decision: no token refresh, no
                # quarantine writes — freshening is a mutation.
                return self._perform(
                num, email, trigger, left_snapshot,
                detail=self._describe_move(current, num, headroom),
            )
            status = self._freshen_target(num, email)
            if status == "identity-conflict":
                # The slot's credential is alive but belongs to a different
                # account — switching onto it would silently run the wrong
                # account. Quarantine (auto-released once a re-add replaces
                # the credential).
                self._quarantine(num, email, "identity-conflict")
                continue
            if status == "invalid_grant":
                self._quarantine(num, email, "invalid_grant")
                continue
            if status == "transient":
                transient_failure = True
                continue
            if status in _SYSTEMIC_STATUSES:
                # ONE cause is reported, so it must be the one worth acting
                # on. Assigning unconditionally made it the LAST candidate's,
                # and `consume-busy` clears itself on the next pass while the
                # other two need a human — unset an env var, chase a rejected
                # client_id. So a busy slot sorting after an unmirrored one
                # named the harmless cause and hid the real one: exactly the
                # "reads as intermittent, nothing names it" trap these kinds
                # were split out of "transient" to escape.
                if not systemic or _SYSTEMIC_STATUSES.index(
                    status
                ) < _SYSTEMIC_STATUSES.index(systemic):
                    systemic = status
                continue
            if status == "skip-live-session":
                continue
            return self._perform(
                num, email, trigger, left_snapshot,
                detail=self._describe_move(current, num, headroom),
            )

        if systemic or transient_failure:
            self._emit(
                ErrorEvent(
                    message="could not freshen: " + _SYSTEMIC_MESSAGES[systemic]
                    if systemic
                    else "could not freshen any candidate (network?)",
                    transient=True,
                )
            )
            return TickOutcome.ERROR
        self._emit(NoSwitchEvent(reason="no-viable-target"))
        return TickOutcome.BLOCKED

    # -- manual hold ------------------------------------------------------------

    def _auto_add_live_login(self) -> None:
        """Add a new Claude Code login (or re-capture a re-login) unasked.

        Acts only when this poll's observation of the live login equals the
        previous poll's — identity AND credential lineage — so a ``/login``
        still being written, or a switch landing between polls, is never
        adopted; the switcher then re-checks everything under the switch
        locks. Never in a dry run (it writes the roster). With
        ``autoswitch.autoAddAccounts`` off, today's behaviour stays and an
        unmanaged live login gets one hint per process.
        """
        if self.dry_run:
            return
        try:
            if not self.settings.auto_add_accounts:
                if (
                    not self._auto_add_hinted
                    and self.switcher.current_account_number() is None
                    and self.switcher.has_live_login()
                ):
                    self._auto_add_hinted = True
                    _logger.info(
                        "The live Claude Code login is not managed and "
                        "autoswitch.autoAddAccounts is off; run 'aswap add' "
                        "to include it."
                    )
                return
            observe = getattr(self.switcher, "observe_live_login", None)
            if observe is None:
                return
            seen = observe()
            previous, self._live_login_seen = self._live_login_seen, seen
            if seen is None or seen != previous:
                return
            self.switcher.adopt_live_login(seen)
        except Exception:
            _logger.debug("auto-add pass failed", exc_info=True)

    def _recheck_plans(self, current: str | None) -> None:
        """Re-read plans cached as lapsed: the active account's at once when
        it changed (a switch by anyone, an in-app ``/login``), and every
        lapsed slot in the background every few minutes — profile endpoint
        only, so a usage 429 backoff cannot hold a revived plan hostage."""
        if self.dry_run:
            return
        try:
            if current is not None and current != self._plan_seen_active:
                recheck = getattr(self.switcher, "recheck_plan_if_lapsed", None)
                if recheck is not None:
                    recheck(current)
            self._plan_seen_active = current
            background = getattr(self.switcher, "recheck_lapsed_plans", None)
            if background is not None:
                background()
            # Hourly plan pass; never while the 3-5 s last stretch runs.
            hourly = getattr(self.switcher, "recheck_plans", None)
            if hourly is not None and not self._endgame:
                hourly()
        except Exception:
            _logger.debug("lapsed-plan re-check failed", exc_info=True)

    def _manual_hold_until(self, state: dict, current: str) -> float | None:
        """Notice a switch made outside this engine; when its hold ends.

        ``lastSeenActive`` in the state file is the account the engines last
        saw active; ``_perform`` moves it along with every engine switch. A
        live login anywhere else got there by hand — ``aswap switch``, the
        TUI, an in-app ``/login`` — so a new hold starts on it, restarting
        any older one. State written before that key existed falls back to
        where a hold or the engine last put the work, then to the previous
        tick. Everything lives in the state file, so the hold survives an
        ``aswap auto`` restart and a cron ``--once`` honours it too.

        A dry run writes nothing: it keeps its own view in memory, and takes
        the file's again only when a live engine elsewhere moves it.
        """
        minutes = self.settings.hold_minutes
        stored = state.get("lastSeenActive")
        if self.dry_run and self._last_seen is not None and stored == self._stored_seen:
            known = self._last_seen
        else:
            known = stored
        self._stored_seen = stored
        hold = self._dry_hold if self.dry_run else None
        if hold is None and isinstance(state.get("manualHold"), dict):
            hold = state["manualHold"]
        if known is None:
            known = hold.get("account") if hold else state.get("lastSwitchTo")
        if known is None:
            known = self._last_seen
        self._last_seen = current
        if known is None or str(known) != str(current):
            hold = self._see_active(current, hold, known is not None, minutes)
        if minutes <= 0 or hold is None:
            return None  # no clock read: tests script the tick's clock order
        until = manual_hold_until(
            {"manualHold": hold}, current, minutes, self.clock()
        )
        self.manual_hold = (current, until) if until is not None else None
        return until

    def _see_active(
        self, current: str, hold: dict | None, by_hand: bool, minutes: float
    ) -> dict | None:
        """Record a newly seen active account; start a hold if ``by_hand``."""
        start = by_hand and minutes > 0
        new_hold = {"account": str(current), "since": self.clock()} if start else None
        if self.dry_run:
            if new_hold is not None:
                self._dry_hold = hold = new_hold
        else:

            def record(state: dict) -> None:
                nonlocal hold, start
                if str(state.get("lastSeenActive")) == str(current):
                    # A sibling engine (the TUI's, a cron --once) noted this
                    # very switch between our read and this lock: keep its
                    # record so the hold neither restarts nor logs twice.
                    held = state.get("manualHold")
                    hold = held if isinstance(held, dict) else None
                    start = False
                    return
                state["lastSeenActive"] = str(current)
                if new_hold is not None:
                    state["manualHold"] = hold = new_hold

            self._mutate_state(record)
        if start:
            _logger.info(
                "Manual switch to %s; holding it for %s min unless it nears "
                "its limit", current, f"{minutes:g}",
            )
        return hold

    def _note_held_move(
        self, current: str, target: str, trigger: str, until: float
    ) -> None:
        label = clock_label(until)
        self._emit(
            NoSwitchEvent(
                reason="manual-hold",
                detail=(
                    f"account {current} was picked by hand; optional moves "
                    f"held until {label}"
                ),
            )
        )
        key, now = (current, until), self.clock()
        noted = self._hold_noted
        if noted is None or noted[0] != key or now - noted[1] >= HOLD_NOTE_INTERVAL_S:
            self._hold_noted = (key, now)
            _logger.info(
                "Manual hold: staying on account %s (%s would move to %s); "
                "held until %s", current, trigger, target, label,
            )

    def _no_return_account(
        self,
        trigger: str,
        state: dict,
        headroom: dict[str, float | None],
        active_headroom: float | None,
        recovered: bool,
        settings: AutoSwitchSettings,
        current: str | None = None,
    ) -> str | None:
        """The account this engine most recently left, while it is still barred.

        NEVER UNDO THE PREVIOUS MOVE. Each anti-flap gate is one-way on its own
        axis, but the axis is a property of the pair's STATE and burn changes
        that state: the ratio gate is relative (`h >= active x 2`) and the
        spent gate absolute (`active <= 3.0`), so a burning pair crosses the
        boundary repeatedly and each crossing re-opens a move. Measured, both
        resets past the horizon, only the active burning: `[1, 2, 1, 2]` where
        base makes one move.

        SCOPED like every sibling gate — `at-limit` and `failover` skip the
        anti-flap gates by design. Unscoped this stranded a 2-account fleet on
        an exhausted active with the peer at 0%.

        AND SCOPED TO THE ENGINE'S OWN LANDING (`lastSwitchTo == current`).
        The bar refuses to undo THIS ENGINE'S last move; once the user
        switches by hand the engine is no longer sitting where it put itself
        and that move is already undone, so the bar protects nothing and
        merely withholds the fleet's best account. Reproduced: engine 1 -> 2,
        user 2 -> 3 by hand, account 1 on 4 pts still barred against an
        active on 2, every reset far out, no release leg reachable — the
        `not recovered` return below fires before the ratio leg is read, and
        `recovered` is False because account 1 was 4 pts at departure too.
        The engine then holds the worse account until the at-limit escape.
        Both sides are `str`-normalised: `lastSwitchTo` is a `str` from
        `_perform` and `lastSwitchFrom` an `int` from `account_ref`. A state
        record written before `lastSwitchTo` existed has no such key and
        cannot prove the engine moved away, so it KEEPS the bar — the same
        conservative reading this module gives every other missing field, and
        the only one that does not silently drop the anti-flap bound for an
        upgrade cycle.

        RELEASED when the account we left now beats us by the same ratio the
        anti-flap margin uses: that is not the flip this bars, it is a move the
        outbound leg would have made on its own merits — BUT ONLY IF IT HAS
        ACTUALLY RECOVERED. The ratio compares against the ACTIVE, and the
        active burns, so ungated it comes true on a target that has done
        nothing. Measured on the cited walk, the barred account held 4.0 pts at
        departure and 4.0 pts at every return; the ratio fired purely because
        the active fell to 2.0, which is the flap arriving through the release
        instead of through the ranking. `recovered` is that gate, computed once
        per snapshot by `_left_account_recovered` and shared with the
        leaves-nothing retry in `_rank` so the two cannot disagree.

        THE LEAVES-NOTHING RELEASE IS NOT HERE. It used to be, and it was
        rewritten twice for the same reason both times: this function cannot
        see the gates that decide. `lastSwitchFrom` is rewritten only by a
        successful switch — the one thing the bar prevents — so a bar that
        empties the ranking is permanent, and each attempt to predict emptiness
        landed one gate short of the ranking loop:

            all(n == barred ...)              "does another account EXIST"
            (headroom.get(n) or 0.0) > 0.0    "is another account not at its limit"

        The loop also applies the threshold, `h >= active x HORIZON_HEADROOM_
        RATIO`, the spent fallback's `h >= active` plus a sooner reset, and the
        recovery hysteresis. A third account on ONE point clears both
        predicates above and none of those, so the n=2 stall came back at n>=3
        and then again one point up. Measured: barred peer 3.5 pts / back in
        10h, active 2 pts / 500h out, third 1 pt — 30 ticks all BLOCKED, and
        the same fleet with `lastSwitchFrom` popped switches on the first.

        `_rank` now ASKS the ranking instead: it ranks with the bar, and
        re-ranks without it when the result is empty. That is exact by
        construction, covers the recovery axis this predicate never could, and
        cannot be one gate behind because it is not a separate copy of the
        gates.

        EMPTINESS ALONE IS NOT THE RELEASE, though — see `_rank`. At n=2 the
        barred ranking is always empty, so an emptiness-only retry is a no-op
        at exactly the fleet size the flap was reported on. Both the retry and
        the ratio above are gated on `recovered`, which is the one question the
        ranking cannot answer: is this a different account from the one we
        left, or only a different active?
        """
        came_from = state.get("lastSwitchFrom")
        if trigger not in ("proactive", "consume-first") or came_from is None:
            return None
        # Only while we are still standing where that switch put us. A manual
        # switch away already undid the move, so there is nothing left to
        # refuse to undo. `str` on both sides: `lastSwitchTo` is written from
        # `_perform`'s `number: str`, `lastSwitchFrom` from `account_ref`'s
        # `number: int`, and an int/str mismatch here would disarm the bar
        # everywhere rather than only after a hand switch.
        landed_on = state.get("lastSwitchTo")
        if landed_on is not None and current is not None:
            if str(landed_on) != str(current):
                return None
        # No membership check on `oauth_candidates`: the loop compares
        # `num == no_return` while iterating that same list, so naming an
        # account that is not in it bars nothing. The check was a no-op and
        # nothing killed it under mutation.
        barred = str(came_from)
        if not recovered:
            return barred        # the ratio below burns true on its own; see above
        left_headroom = headroom.get(barred)
        if left_headroom is not None:
            if active_headroom is not None:
                if left_headroom >= active_headroom * HORIZON_HEADROOM_RATIO:
                    return None               # beats us outright; not a flip
            elif (
                settings is not None
                and left_headroom > 100.0 - settings.threshold
            ):
                # An unreadable active must not be silently scored as "the
                # peer does not beat it" -- same landing-eligible fallback
                # `_left_account_recovered` uses when it, too, has no active
                # to compare against.
                return None
        return barred

    def _left_account_recovered(
        self,
        state: dict,
        usage: dict[str, dict | str | None],
        headroom: dict[str, float | None],
        active_headroom: float | None,
        settings: AutoSwitchSettings,
        now: float,
        current: str | None = None,
    ) -> bool:
        """Is the account we left a better proposition than when we left it?

        This is the release the bar needs and the ranking cannot supply. A bar
        that leaves nothing is a stall, but "leaves nothing" is also what every
        flap looks like on two accounts, so lifting on emptiness alone lifts
        always. The distinction is not in the present state — it is between the
        present and the moment of departure, which is why `_perform` records
        that moment (`leftHeadroom` / `leftRecoveryAt`) alongside
        `lastSwitchFrom`.

        Measured on the walk this guard exists for, the barred account was
        IDENTICAL at every return — same headroom, same reset — and only the
        active had changed. That is the flap: the ranking flipped underneath a
        target that did nothing.

        FAILOVER FIRST, before any leg reads the active — checked ahead of
        dominance, because dominance-first starves this branch:
        `test_a_failover_departure_does_not_disarm_the_bar` broke on its
        FIRST tick the instant the active fell far enough for `4.0 > active
        x 2` to go true (measured at active=1.8, one fifth of a point past
        the boundary this branch's own sibling test already sits on).
        A `(None, None)` snapshot means severity was genuinely unmeasured at
        departure — there is no `leftHeadroom` to diff against and never was
        — so the two signals that do not depend on the active's LIVE state
        are (1) whether the peer, right now, would itself be a healthy place
        to land: `h > 100 - settings.threshold`, the same "would the ranking
        accept this as a landing spot" test `_rank_candidates` already runs
        (`:1617`) on every candidate, reused rather than inventing a fresh
        constant; and (2), when the landing floor cannot answer, whether the
        peer's own binding reset is meaningfully sooner than the active's.
        The landing floor is the exact complement of
        `_every_account_above_threshold`, so it is UNSATISFIABLE whenever
        the
        fleet is all-spent — the recovery leg is what keeps the hold from
        becoming unconditional in exactly that regime. Neither leg can tell
        "genuinely recovered" from "was already this good" — there is
        nothing recorded to tell them apart. Measured which side the landing
        floor should land on: sweeping mutations of this same leg for the
        ordinary path showed an absolute floor is silently reintroducible
        with a green suite, so this is not free of that risk either — the
        difference is this constant is `settings.threshold`, not a
        hardcoded number, so a
        user's OWN policy decides how conservative the hold is, and it moves
        when they change it (pinned directly, below). Deliberately MORE
        conservative than the ordinary path below: a peer sitting at 4 points
        held through the whole walk that broke a bare dominance leg, with no
        upper bound short of the peer crossing the threshold itself OR its
        binding reset pulling meaningfully ahead of the active's. Bounded,
        not permanent — at-limit still escapes untouched
        (`_no_return_account` scopes this trigger out entirely), and the
        recovery leg means the bound is no longer just "the active reaches
        its own hard limit": a peer that resets first releases the hold on
        its own schedule, without the active ever needing to burn down to it.

        THE ORDINARY PATH HAS A REAL BASELINE (`leftHeadroom` is a number,
        not null), so it gets three legs, checked in this order, each with
        the margin its axis already uses. Burn cannot manufacture any of
        them: headroom rises only when a window rolls over, the ratio needs
        the active to lose more than half its remaining headroom AND clear
        an extra `SPENT_HEADROOM_PCT` on top, and the binding reset moves
        nearer only when a nearer window starts binding.

          dominance   `> active x HORIZON_HEADROOM_RATIO + SPENT_HEADROOM_PCT`
                      against the ACTIVE. A peer moved AWAY from for a reason
                      other than headroom (e.g. consume-first's reset
                      ordering) can dominate the active from the moment it
                      was left, and self-improvement against its own
                      departure baseline never fires for an account that had
                      nothing to improve on. The `+SPENT_HEADROOM_PCT` on top
                      of the bare ratio is what the bare ratio misses:
                      measured on this branch's own flap fleet (peer frozen
                      4.0 pts, active burning 98.0% -> 98.4%), it flips
                      true at active=1.8 pts purely because the active kept
                      burning; the same walk with the margin added stays
                      false through active=1.6 and only opens once the active
                      is down to a genuine sliver (`x < 0.5`), which is the
                      at-limit escape's territory, not a return worth calling
                      anti-flap. `test_a_burn_walk_never_returns_to_what_it_
                      left` still settles with the margin in place — it makes
                      the boundary harder to cross, not impossible.
          headroom    `+SPENT_HEADROOM_PCT` against the DEPARTURE baseline
                      — below that an edge is under two poll intervals, the
                      same reason the spent band exists.
          recovery    `-RECOVERY_HYSTERESIS_S` against the DEPARTURE
                      baseline — the same margin the recovery axis ranks by
                      one gate later.

        NO SNAPSHOT MEANS RELEASE. State written before this field existed, or
        by a switch that never recorded one, carries no evidence either way —
        and of the two failure modes the permanent proactive lockout is the
        worse one, because it is persisted and survives a restart and a week of
        wall clock. Absence of evidence releases. This only gates the
        proactive/consume-first return; `_no_return_account` scopes at-limit
        and failover out of the bar by design, so either trigger still
        escapes the account untouched, and the next successful switch
        overwrites the snapshot outright.
        """
        came_from = state.get("lastSwitchFrom")
        if came_from is None:
            # Unreachable through `_no_return_account`, the only caller: it
            # returns `None` at its own `came_from is None` check before
            # `recovered` is ever read. Kept `True` (not load-bearing) so a
            # future direct caller gets "no evidence, release" rather than a
            # silent hold.
            return True
        barred = str(came_from)
        if "leftHeadroom" not in state:
            return True          # pre-upgrade record: genuinely no evidence
        h = headroom.get(barred)
        left_headroom = state.get("leftHeadroom")
        left_recovery = state.get("leftRecoveryAt")
        # A `consume-first` departure can ALSO write (None, None) -- the
        # same shape a real failover writes -- whenever the phase-2 refetch's
        # active row is unmeasurable for headroom but still has a known
        # weekly reset (the split shape `oauth.
        # build_usage_result` emits for `utilization: null` plus a
        # `resets_at`). Inferring "failover" from the two nulls then ran the
        # more permissive failover legs (landing floor + recovery-only) on
        # what was really an ordinary departure -- reachable on 32%+ of
        # swept fleets, both directions. `leftTrigger` records the actual
        # trigger so this never has to guess again; a record written before
        # this field existed has no such key, so fall back to the old
        # two-null inference for it (unchanged behaviour for pre-upgrade
        # state).
        left_trigger = state.get("leftTrigger")
        is_failover_snapshot = (
            left_trigger == "failover"
            if left_trigger is not None
            else (left_headroom is None and left_recovery is None)
        )
        if is_failover_snapshot:
            # Failover: real departure, severity unmeasured at the time --
            # not absence of evidence, and there is no baseline to diff
            # against (that is exactly what "unmeasured" means), so this
            # cannot use the active's HEADROOM at all -- see docstring for
            # why reading the active's headroom here is wrong, and the walk
            # that proved it. Two legs, both read-only against CURRENT state
            # (no departure baseline exists to diff against):
            #
            #   landing   `h > 100 - settings.threshold` -- would the
            #             ranking accept this peer as a landing spot right
            #             now (`_rank_candidates`, :1636)?
            #   recovery  the peer's binding reset is meaningfully sooner
            #             than the ACTIVE's binding reset -- the same axis
            #             `_recovery_is_useful` switches to once headroom
            #             stops being informative. Needed because `landing`
            #             is the exact complement of `_every_account_above_
            #             threshold`: whenever the fleet is all-spent,
            #             `landing` is unsatisfiable by construction, no
            #             matter how soon the peer's own
            #             window resets, and that regime is precisely where
            #             the recovery axis is the one the engine trusts.
            #
            # Burn cannot fake the recovery leg: a reset moves nearer only
            # when a nearer window starts binding, never as a side effect
            # of the active spending down -- the failure mode a bare
            # dominance leg has, guarded directly in the mutation table.
            if h is not None and h > 100.0 - settings.threshold:
                return True
            peer_recovery_ts = _binding_recovery_ts(usage.get(barred), self._models, now)
            active_recovery_ts = _binding_recovery_ts(usage.get(current), self._models, now)
            # The active's recovery must be a REAL measurement, not merely
            # "larger" -- `_binding_recovery_ts` returns `inf` for both
            # "never resets" and "we do not know" (no windows, no
            # `resets_at`, or a stale/past `resets_at`). Reading `inf` as
            # "never" here made `peer < inf - HYST` true for ANY finite
            # peer reset, releasing onto a peer arbitrarily far out on no
            # evidence. `math.isfinite` requires the active to have a
            # genuine, known reset before the comparison even runs --
            # unknown holds, exactly like unreadable already does on the
            # headroom axis.
            #
            # But two of the five `inf` states are ordinary shapes for an
            # active that is plainly alive and burning -- a `pct` reported
            # with no `resets_at`, or a `resets_at` already elapsed -- not
            # unknowns. Reading all five as "unknown, hold" pins the engine
            # on a near-spent active for up to a full window even when the
            # peer is back within `RECOVERY_HORIZON_S`, the
            # same constant this PR already uses for "near enough to
            # matter" (`_recovery_is_useful`). Requiring EITHER a known
            # active reset OR a peer inside that horizon keeps `isfinite`'s
            # intended release (a known active vs. an arbitrarily-far peer
            # still needs `isfinite`) while letting a near peer through
            # regardless of why the active's own reset reads `inf`.
            return (
                (
                    math.isfinite(active_recovery_ts)
                    or peer_recovery_ts - now <= RECOVERY_HORIZON_S
                )
                and peer_recovery_ts < active_recovery_ts - RECOVERY_HYSTERESIS_S
            )
        # Dominance over the ACTIVE, only reached once a real baseline is
        # confirmed to exist above -- a peer that was already miles ahead of
        # the active at departure (moved for a DIFFERENT reason -- e.g.
        # consume-first's reset ordering, not headroom) never "improves" on
        # its own baseline and would stall on self-improvement alone despite
        # dominating throughout.
        #
        # `active_headroom is None` means "we could not read the active this
        # tick" -- reachable through the consume-first two-phase commit,
        # which reassigns `active_headroom` from a fresh
        # refetch without re-classifying the trigger. That is a DIFFERENT
        # state from "readable, but does not dominate" and must not answer
        # the same way: fall back to the landing-eligible test the failover
        # branch above uses when IT has no baseline to compare against
        # either, rather than silently treating "unreadable" as "no
        # dominance".
        #
        # DEFENSIVE, not currently outcome-changing: measured exhaustively,
        # `active_headroom=None` on this path closes
        # BOTH of `_rank_candidates`'s gates before this leg is ever asked
        # (`_every_account_above_threshold` is False on a None active, and
        # the consume-first `active_reset_ts` gate is None too), so no
        # fleet shape has been found where this branch changes a `tick()`
        # outcome. Kept because the call IS reachable (confirmed via the
        # phase-2 refetch) and a future change to those gates could make it
        # live without anyone revisiting this function -- silently reading
        # None as "no dominance" would then be exactly the bug the
        # unreadable-active fallback was added for.
        if h is not None:
            if active_headroom is not None:
                if h > active_headroom * HORIZON_HEADROOM_RATIO + SPENT_HEADROOM_PCT:
                    return True
            elif h > 100.0 - settings.threshold:
                return True
        if (
            isinstance(left_headroom, (int, float))
            and h is not None
            and h >= min(left_headroom + SPENT_HEADROOM_PCT, 100.0)
        ):
            return True
        # `None` is the JSON-safe spelling of "unknown or already past", which
        # `_binding_recovery_ts` returns as `inf`: an account nobody can
        # schedule around. Moving off it onto a real reset IS the improvement.
        was = left_recovery if isinstance(left_recovery, (int, float)) else float("inf")
        return (
            _binding_recovery_ts(usage.get(barred), self._models, now)
            < was - RECOVERY_HYSTERESIS_S
        )

    def _perish_inputs(self, now: float | None) -> dict[str, dict]:
        """Slot -> {endsAt, anchorNext, tier} from local files; {} on any failure."""
        getter = getattr(self.switcher, "perish_inputs", None)
        if getter is None:
            return {}
        try:
            return getter(now) or {}
        except Exception:
            return {}

    def _cap(self, num: str) -> float:
        return self._caps.get(num, DEFAULT_CAPACITY)

    def _burn_sample(
        self, current: str, value: object, entry: object, fallback_pct: float
    ) -> tuple[float, str, float, float]:
        """The active account's burn sample: (measured at, account, binding %,
        capacity), on the endpoint's own numbers.

        The time is when the binding number was measured: the newest
        statusline reading when a live window supplies it, else the usage
        fetch. A stored reading re-served by a later tick keeps its fetch
        time, so the tracker sees it as no new evidence instead of a flat
        stretch, and the next fresh reading is credited with the whole gap
        since the last one. Without a measured time (a gate-only number) it
        is this tick's collection time; never later than that.
        """
        usage = value if isinstance(value, dict) else None
        raw = oauth.account_headroom(usage, self._models)
        if raw is None:
            return self._collected_at, current, fallback_pct, self._cap(current)
        pct = 100.0 - raw
        measured: float | None = None
        if usage is not None and self._live_now and any(
            isinstance(w := usage.get(key), dict)
            and w.get("live")
            and isinstance(w.get("pct"), (int, float))
            and w["pct"] >= pct
            for key in ("five_hour", "seven_day")
        ):
            measured = max(
                (r["at"] for r in self._live_now if isinstance(r.get("at"), (int, float))),
                default=None,
            )
        fetched = getattr(entry, "fetched_at", None)
        if measured is None and isinstance(fetched, (int, float)):
            measured = fetched
        at = self._collected_at if measured is None else min(measured, self._collected_at)
        return at, current, pct, self._cap(current)

    def _edge_due(self, current: str, value: object, entry: object) -> bool:
        """Feed the last-point clock and say whether its deadline has passed.

        The point is sized at the fastest burn in sight (the landing peak,
        the windowed average, the pace the latest readings prove), so the
        clock errs early; see edge.py for why 100% cannot be waited for.
        """
        now = self._collected_at
        self._edge.observe_endpoint(
            current,
            value if isinstance(value, dict) else None,
            getattr(entry, "fetched_at", None),
        )
        self._edge.observe(current, gate.gate_of(self._gate_recs.get(current)), now)
        rate = max(
            self._landing_rate(),
            self._burn_rate or 0.0,
            self._burn.fast_rate() or 0.0,
        )
        deadline = self._edge.deadline(self._cap(current) / rate if rate > 0 else None)
        self._edge_deadline = deadline
        if deadline is None or now < deadline:
            return False
        note = (current, tuple(self._edge.windows()))
        if self._edge_logged != note:
            self._edge_logged = note
            _logger.info(
                "Account %s: %s in its last point (%s); leaving before it is refused",
                current,
                "/".join(w.replace("_", "-") for w in note[1]),
                "start unseen" if deadline == edge.UNKNOWN else "clock ran out",
            )
        return True

    def _landing_rate(self) -> float:
        """The burn a switch target is judged by: the fastest rate seen within
        ``LANDING_BURN_WINDOW_S``, so a lull does not shrink the check; an
        idle spell falls back to the last rate seen, then to
        ``DEFAULT_LANDING_BURN``, instead of waving every target through."""
        cutoff = self._collected_at - LANDING_BURN_WINDOW_S
        self._recent_burns = [(t, r) for t, r in self._recent_burns if t >= cutoff]
        peak = max((r for _t, r in self._recent_burns), default=0.0)
        return (
            max(self._burn_rate or 0.0, peak)
            or self._last_burn_rate
            or DEFAULT_LANDING_BURN
        )

    def _runway_s(self, num: str, h: float | None, *, landing: bool = False) -> float | None:
        """Seconds of work the account holds at the current burn rate
        (``landing``: at ``_landing_rate``)."""
        rate = self._landing_rate() if landing else self._burn_rate
        if h is None or not rate:
            return None
        return max(h, 0.0) * self._cap(num) / rate

    def _describe_move(
        self, current: str, target: str, headroom: dict[str, float | None]
    ) -> str:
        from claude_swap.weekly_anchor import tier_label

        labels = {cap: tier_label(t) for t, cap in TIER_CAPACITY.items() if t != "ai"}

        def side(num: str) -> str:
            h = headroom.get(num)
            used = "usage unknown" if h is None else f"{pct_label(100.0 - h)}% used"
            tier = labels.get(self._caps[num], "?") if num in self._caps else "tier ?"
            return f"{num} {used}, {tier}"

        parts = [f"from {side(current)}", f"to {side(target)}"]
        if self._burn_rate:
            parts.append(f"burn {self._burn_rate * 60:.2f} 5x-pts/min")
            runway = self._runway_s(target, headroom.get(target))
            if runway is not None:
                parts.append(f"target runway ~{runway / 60:.0f} min")
        if self._limit_guard and self._time_to_limit is not None:
            parts.append(f"limit projected in {self._time_to_limit / 60:.1f} min")
        return "; ".join(parts)

    def _rank_candidates(
        self,
        *,
        trigger: str,
        consume_first: bool,
        oauth_candidates: list[str],
        no_return: str | None,
        usage: dict[str, dict | str | None],
        headroom: dict[str, float | None],
        current: str,
        active_headroom: float | None,
        settings: AutoSwitchSettings,
        now: float,
    ) -> tuple[list[str], bool, float | None]:
        """Filter and rank OAuth candidates for this tick's trigger.

        Returns ``(ordered, any_known, active_reset_ts)``. Pure — no emits,
        no state writes — so the consume-first two-phase commit can run it
        twice per tick: on the stored snapshot to decide provisionally, then
        on the escalated refetch to re-verify before switching.
        """
        # consume-first ranks by soonest weekly reset; a proactive (below-
        # threshold) target must reset strictly sooner than where we are.
        # "Reset" here is when the quota PERISHES: a subscription scheduled to
        # end before its weekly reset loses its quota at the end instead.
        perish = self._perish_inputs(now) if consume_first else {}
        active_reset_ts = (
            _perishable_ts(perish, current, usage.get(current), now)
            if consume_first
            else None
        )
        # When NOTHING is below the threshold — the active account and every
        # candidate all in the 90s — "land somewhere healthy" has no answer,
        # and holding out for one costs the user the session. Sitting still
        # means burning the active account to 100% and taking a hard limit,
        # with the peer that resets in 8 minutes never tried. So in that state
        # the goal changes from "most headroom" to "soonest back": move to
        # whichever account recovers first and keep working through its reset.
        #
        # Deliberately narrow. It engages only when every measured OAuth
        # account is at/over the threshold, so a single healthy peer still
        # wins the normal way, and RECOVERY_HYSTERESIS_S below replaces the
        # percentage-point margin so two accounts in the 90s cannot ping-pong.
        all_above = _every_account_above_threshold(
            oauth_candidates, headroom, active_headroom, settings.threshold
        )
        # "Is anything worth having?" — the most headroom any candidate with a
        # READABLE row offers. Two exclusions and no others:
        #
        # Unknown headrooms are skipped rather than counted as zero. A row we
        # cannot read is not evidence of an empty account — measured, one
        # sentinel row (expired token, locked keychain) made `all(...)` False
        # forever and parked the engine on the account resetting LAST.
        #
        # Nothing else is filtered, INCLUDING the no-return bar. An earlier
        # version of this comment claimed it was "scoped to choosable
        # candidates"; the code below has never done that and the two
        # paragraphs contradicted each other. Leaving the barred account in is
        # deliberate: this answers whether the FLEET has quota, and the bar is
        # about which account to move to, not about what exists. A peer just
        # above SPENT_HEADROOM_PCT can therefore turn the spent check off while
        # being unchoosable itself — the band is (SPENT_HEADROOM_PCT, active x
        # RATIO], up to 3 points wide at the defaults, and the one-way fallback
        # below is what stops that band parking the engine. A ratio floor used
        # to sit here too and inverted monotonicity; removing it is what let
        # the fallback do the job.
        best_candidate_headroom = max(
            (h for h in map(headroom.get, oauth_candidates) if h is not None),
            default=0.0,
        )
        active_recovery_ts = (
            _binding_recovery_ts(usage.get(current), self._models, now)
            if all_above
            else 0.0  # unread unless all_above; never a live sentinel
        )

        # Headroom in work, not percent: scaled by tier capacity relative to
        # the biggest tier in play, so a single-tier fleet compares exactly
        # as raw percentages do.
        active_cap = self._cap(current)
        scale = max(self._cap(n) for n in (current, *oauth_candidates))
        qualifying: list[tuple[tuple, str]] = []
        short: list[tuple[tuple, str]] = []
        fallback: list[tuple[tuple, str]] = []
        any_known = False
        # The runway rule (RUNWAY_RATIO / RUNWAY_MARGIN_S): a proactive move
        # away from an account that still works must land somewhere that
        # lasts meaningfully longer at the same peak burn. Escapes (at-limit,
        # failover) take whatever works; with everything above the threshold
        # (`all_above`) both ranking axes below carry their own anti-flap
        # margins (RECOVERY_HYSTERESIS_S, HORIZON_HEADROOM_RATIO) and the
        # question is when quota returns, so the rule stays out of it.
        active_runway = (
            self._runway_s(current, active_headroom, landing=True)
            if trigger == "proactive"
            and active_headroom is not None
            and active_headroom > 0
            else None
        )
        runway_refused = False
        self._runway_refused = None
        for num in oauth_candidates:
            h = headroom.get(num)
            if h is None:
                continue
            cap = self._cap(num)
            any_known = True          # it EXISTS and is readable either way
            if h <= 0:
                continue  # itself at its limit — never a target
            if num == no_return:
                continue  # the account we just left; see _no_return_account
            reset_ts = (
                _perishable_ts(perish, num, usage.get(num), now) if consume_first else None
            )
            recovery_ts = (
                _binding_recovery_ts(usage.get(num), self._models, now)
                if all_above
                else 0.0
            )
            if trigger in ("proactive", "consume-first"):
                # Landing must be healthy: an account at/over the threshold
                # would re-trigger on the very next tick. At-limit and failover
                # are escapes that skip this whole block — any account with real
                # headroom beats a blocked or dead one.
                if (100.0 - h) >= settings.threshold and not all_above:
                    continue
                if all_above:
                    # Checked before the strategies, because with nothing below
                    # the threshold the strategy question is moot: consume-first
                    # exists to spend perishable WEEKLY quota, and every account
                    # here is blocked on a window that returns in minutes. Both
                    # strategies want the same thing — the account that can work
                    # again first — so both take this gate and the matching key
                    # below. (Ordering matters: `if consume_first` catching
                    # first filtered on weekly ordering while the key sorted on
                    # binding recovery, two different axes, and left
                    # consume-first users with no anti-flap guard at all.)
                    #
                    # WHICH AXIS is decided per candidate, in one place — see
                    # _recovery_is_useful for the four holes that came from
                    # deciding it once, globally, from four scattered gates.
                    # Set and read under the same `all_above and trigger`
                    # condition, so it is always assigned before the key below.
                    by_recovery = _recovery_is_useful(
                        recovery_ts,
                        active_recovery_ts,
                        active_headroom or 0.0,
                        best_candidate_headroom,
                        now,
                    )
                    if by_recovery:
                        # Hysteresis on the axis we actually rank by. It bounds
                        # the flap RATE rather than making a reverse move
                        # impossible: the target must come back meaningfully
                        # sooner than where we are.
                        if recovery_ts >= active_recovery_ts - RECOVERY_HYSTERESIS_S:
                            continue
                    else:
                        # Headroom axis, with a RATIO margin. Also a rate bound,
                        # not impossibility — headroom moves, so a target that
                        # burns down to a quarter of what it beat can qualify
                        # in reverse. That takes a 4x relative burn instead of
                        # the one point a strictly-greater test would need.
                        if h < (active_headroom or 0.0) * HORIZON_HEADROOM_RATIO:
                            if (
                                (active_headroom or 0.0) <= SPENT_HEADROOM_PCT
                                and h >= (active_headroom or 0.0)
                                and recovery_ts
                                < active_recovery_ts - RECOVERY_HYSTERESIS_S
                            ):
                                fallback.append(((0, recovery_ts, -h * cap), num))
                            continue
                elif consume_first:
                    # Purely proactive on reset ordering: below the threshold,
                    # only move to accounts whose weekly window resets sooner
                    # than the active one (above the threshold we must move, so
                    # any healthy account qualifies and the sort picks soonest).
                    if trigger == "consume-first" and (
                        reset_ts is None
                        or active_reset_ts is None
                        or reset_ts >= active_reset_ts
                    ):
                        continue
                elif active_headroom is not None:
                    # best: the candidate must beat the active account by the
                    # full hysteresis margin (a one-way move like 99%→89%
                    # qualifies; near-line pairs can't flap back).
                    if (
                        h * cap - active_headroom * active_cap
                    ) / scale < settings.hysteresis_pct:
                        continue
            if all_above and trigger in ("proactive", "consume-first"):
                # Ranked on the axis its own gate decided, and TIERED so the two
                # stay comparable: a candidate returning inside the horizon
                # beats one that does not, whatever its headroom. Untiered, the
                # two key shapes were compared elementwise — a raw headroom
                # against an epoch timestamp — and headroom won on magnitude
                # alone. Falling through to the weekly key instead split the
                # filter and the sort across two axes, picking the candidate
                # with LESS headroom whenever its weekly reset was sooner.
                #
                # Scoped to the SAME triggers as the gate above: at-limit and
                # failover skip that gate deliberately, because there we are
                # escaping a dead account rather than optimising a return time.
                # `recovery_ts` in BOTH tiers. Tier 1 hard-coded 0.0 there,
                # which threw away a fact already in hand: two peers with equal
                # headroom past the horizon then tied, and the tie fell through
                # to sequence order. Measured — active 4 pts/300h, two peers
                # 8 pts each, one returning in 5h and one in 500h: base picks
                # the 5h account whichever slot it occupies, this branch picked
                # whichever came first in the list. Headroom still decides
                # first within the tier; the reset only breaks its ties, where
                # sooner is plainly better than lower slot number.
                key: tuple = (
                    (0, recovery_ts, -h * cap)
                    if by_recovery
                    else (1, -h * cap, recovery_ts)
                )
            elif consume_first:
                # Soonest weekly reset first (unknown resets sort last), most
                # headroom breaks ties, then sequence order.
                key = (reset_ts if reset_ts is not None else float("inf"), -h * cap)
            else:
                key = (-h * cap,)
            runway = self._runway_s(num, h, landing=True)
            # Draining perishable quota: see DRAIN_MIN_USE_S.
            drain = (
                consume_first
                and not all_above
                and reset_ts is not None
                and active_reset_ts is not None
                and reset_ts < active_reset_ts
            )
            if (
                active_runway is not None
                and runway is not None
                and not all_above
                and not drain
                and runway < min(
                    active_runway * RUNWAY_RATIO, active_runway + RUNWAY_MARGIN_S
                )
            ):
                runway_refused = True
                continue
            min_runway = LIMIT_GUARD_S + DRAIN_MIN_USE_S if drain else MIN_LANDING_RUNWAY_S
            if drain and h < 100.0 - edge.LAST_POINT_PCT:
                # Already in its last point: nothing in it can be timed (see
                # edge.py), so draining it is a gamble on a refusal.
                runway = 0.0
            if runway is not None and runway < min_runway:
                if trigger == "consume-first":
                    continue  # a voluntary move that would not last
                short.append((key, num))  # an escape still takes it, last
                continue
            qualifying.append((key, num))
        # Ascending by the strategy's key; list order (sequence order) breaks ties.
        qualifying.sort(key=lambda t: t[0])
        short.sort(key=lambda t: t[0])
        ranked = qualifying + short
        if not ranked:
            ranked = sorted(fallback, key=lambda t: t[0])
        if runway_refused and not ranked:
            self._runway_refused = active_runway
        return [num for _, num in ranked], any_known, active_reset_ts

    # -- rate-limit gate --------------------------------------------------------

    def _apply_gates(
        self, current: str, usage: dict[str, dict | str | None], now: float
    ) -> dict[str, float | None]:
        """Per-account decision headroom with fresh gate answers folded in.

        See ``gate.apply``: a fresh ``rejected`` is at the limit whatever the
        endpoint reads, a fresh open gate lifts an endpoint 100% to
        ``gate.OPEN_HEADROOM``. The active account's answer must be fresher
        (``gate.ACTIVE_FRESH_S``): it is the one burning its last point.
        Sentinel values are left alone (an expired token idles, it does not
        escape). Records each verdict for this tick in ``_gate_verdicts``.
        """
        headroom = _headroom_by_account(usage, self._models)
        self._gate_verdicts = {}
        for num, record in self._gate_recs.items():
            value = usage.get(num)
            if not isinstance(value, dict):
                continue
            h, verdict = gate.apply(
                value, record, self._models, now,
                gate.ACTIVE_FRESH_S if num == current else gate.FRESH_S,
            )
            if verdict is not None:
                headroom[num] = h
                self._gate_verdicts[num] = verdict
        return headroom

    def _gate_pass(
        self,
        current: str,
        usage: dict[str, dict | str | None],
        quarantined: set[str],
    ) -> dict[str, float | None]:
        """Probe the rate-limit gate where it can change this tick's decision,
        and return the gated headroom.

        Throttled hard (numbers in gate.py): only accounts the endpoint reads
        at 100% on 5h/7d, none whose full window resets within minutes, at
        most ``MAX_PROBES_PER_TICK`` per tick and one per account per
        ``PROBE_INTERVAL_S`` -- except the active account while it rides an
        open gate, re-probed every ``RIDE_REPROBE_S``. A fresh statusline
        reading of the active account is the same headers for free, so it
        replaces the active's probe. Candidates are probed only when the
        active is at its limit and no candidate has measured headroom: that
        is the at-limit escape, the one decision an open full account can
        change. Dry runs and observe-only mode never probe.
        """
        # The collection's own clock read: no extra one per tick.
        now = self._collected_at
        try:
            self._gate_recs = dict(self.switcher.gate_records())
        except Exception:
            _logger.debug("gate records unavailable", exc_info=True)
            self._gate_recs = {}
        live = gate.from_live(self._live_now) if self._live_now else None
        if live is not None:
            stored = gate.gate_of(self._gate_recs.get(current)) or {}
            if (stored.get("probedAt") or 0.0) < live["gate"]["probedAt"]:
                self._gate_recs[current] = live
        headroom = self._apply_gates(current, usage, now)
        probe = getattr(self.switcher, "probe_gates", None)
        if self.dry_run or not self.settings.enabled or probe is None:
            self._log_gate_verdicts(usage)
            return headroom

        budget = gate.MAX_PROBES_PER_TICK
        value = usage.get(current)
        # In the last stretch a live reading only stands in for the probe
        # while it is as fresh as the 3-5 s cadence.
        stale_live = live is not None and self._endgame and (
            now - live["gate"]["probedAt"] > ENDGAME_TICK_MAX_S
        )
        if (live is None or stale_live) and isinstance(value, dict):
            record = self._gate_recs.get(current)
            closed = gate.is_full(gate.gate_of(record))
            interval = gate.PROBE_INTERVAL_S if closed else gate.RIDE_REPROBE_S
            # A timed last point needs no 3-5 s probing (its next tick is the
            # refusal); a slower re-probe still catches a burst it missed.
            timed = self._edge.running() and not closed
            endgame = self._endgame and not closed and not timed
            if endgame:
                interval = ENDGAME_TICK_MIN_S * 0.8
            elif timed:
                interval = edge.EDGE_REPROBE_S
            if gate.due(
                value, record, self._models, now, interval, any_level=endgame or timed
            ):
                self._gate_recs.update(probe([current], now))
                budget -= 1
                headroom = self._apply_gates(current, usage, now)

        active_h = headroom.get(current)
        if budget > 0 and active_h is not None and active_h <= 0:
            candidates = [
                n
                for n in self.switcher.switchable_account_numbers()
                if n != current
                and n not in quarantined
                and self.switcher.account_kind_for(n) != "api_key"
            ]
            if not any((headroom.get(n) or 0.0) > 0 for n in candidates):

                def last_attempt(n: str) -> float:
                    at = (self._gate_recs.get(n) or {}).get("attemptedAt")
                    return at if isinstance(at, (int, float)) else float("-inf")

                full = sorted(
                    (
                        n
                        for n in candidates
                        if isinstance(usage.get(n), dict)
                        and gate.due(usage[n], self._gate_recs.get(n), self._models, now)
                    ),
                    # Biggest tier first (half a point of 20x is four times
                    # one of 5x), then the longest unprobed.
                    key=lambda n: (-self._cap(n), last_attempt(n)),
                )[:budget]
                if full:
                    self._gate_recs.update(probe(full, now))
                    headroom = self._apply_gates(current, usage, now)
        self._log_gate_verdicts(usage)
        return headroom

    def _log_gate_verdicts(self, usage: dict[str, dict | str | None]) -> None:
        """One INFO line when the gate changes what an account counts as."""
        for num, verdict in self._gate_verdicts.items():
            if self._gate_logged.get(num) == verdict:
                continue
            self._gate_logged[num] = verdict
            seen = gate.gate_of(self._gate_recs.get(num)) or {}
            pcts = [
                w["pct"]
                for key in ("five_hour", "seven_day")
                if isinstance(w := seen.get(key), dict)
                and isinstance(w.get("pct"), (int, float))
            ]
            gate_pct = f"{max(pcts):.0f}%" if pcts else "?"
            if verdict == gate.OPEN:
                value = usage.get(num)
                endpoint = binding_pct(
                    value if isinstance(value, dict) else None, self._models
                )
                _logger.info(
                    "Account %s: usage API says %s%% but the gate is open (%s); "
                    "keeping it usable",
                    num, f"{endpoint:.0f}" if endpoint is not None else "100", gate_pct,
                )
            else:
                _logger.info(
                    "Account %s: the gate is closed (%s); treating it as at its limit",
                    num, gate_pct,
                )

    # -- adaptive usage scheduling ---------------------------------------------

    def _collect_scheduled_usage(
        self,
        current: str,
        quarantined: set[str] = frozenset(),
        *,
        threshold: float | None = None,
    ) -> tuple[dict, dict[str, dict | str | None], dict[str, float | None]]:
        """Two-phase usage collection with an O(1) baseline.

        Phase A fetches the active account (when its persisted poll plan says
        it is due — poll_policy's urgent mode is what tightens that cadence
        near the band) plus ONE due candidate (the one with the stalest data
        — never-fetched first, then oldest fetch); everyone else is served
        from the usage store. Phase B refetches ALL candidates and recomputes
        before any switch decision when a switch could be near: active
        utilization within ``ESCALATION_MARGIN_PCT`` of the threshold, or
        active usage unknown (failover must not run on stale candidate data).
        At-limit, proactive, and ordinary unknown-usage failover selection
        never runs on the pre-escalation snapshot — those triggers imply the
        escalation condition (the deliberate exception: an owned-and-expired
        active is excluded above, so a post-idle-hold failover can run
        without escalating). The consume-first trigger can fire outside the
        escalation band, so it instead decides *provisionally* on the stored
        snapshot and, only when a switch would fire, re-runs an escalated
        collection and re-verifies the choice in ``_tick_inner`` (two-phase
        commit), plus a per-target ``UsageEntry.fresh`` gate before
        performing.

        Stalest-first needs no rotation cursor: it reads the persisted store,
        so the loop and cron-driven ``--once`` runs schedule identically.
        Backoff (``backoffUntil``) is enforced by the collector even for the
        active account — a Retry-After must never be defeated — and during an
        idle-hold no candidate is polled at all (slow crawl for everything).
        Adapted cadences are persisted by the collector itself after each
        fetch (shared with every other surface), not by the engine.

        Returns ``(entries, usage, headroom)`` where ``usage`` carries
        decision values and ``headroom`` the derived headroom per account.
        """
        now = self.clock()
        self._collected_at = now  # the burn sample's time, without another read
        # Quarantined accounts can never be switch targets, so spending the
        # single alternate poll slot (or an escalation fetch) on one is wasted.
        candidates = [
            n
            for n in self.switcher.switchable_account_numbers()
            if n != current and n not in quarantined
        ]

        pre = self.switcher.usage_entries_by_account(fetch=set())
        plan: set[str] = set()
        active_pre = pre.get(current)
        live = self._live_readings(current, pre, now)
        self._live_active = bool(live)
        self._live_now = live
        # The active account is nominated when never fetched, poll-due per its
        # persisted plan, or (no plan yet) past the normal cadence floor. The
        # collector's reserve() honors due-ness even inside the serve TTL, so
        # an urgent plan (60s while burning near the band) actually fetches.
        # A candidate-style plan (slower than any active plan can be) left
        # over from a role change the switcher never saw (e.g. a manual
        # login) is overridden past the active age cap. Exhausted accounts
        # carry their own bounded plan and become due normally.
        stale_candidate_plan = (
            active_pre is not None
            and active_pre.age_s is not None
            and active_pre.age_s >= poll_policy.ACTIVE_MAX_INTERVAL_S
            and (active_pre.poll_interval_s or 0.0)
            > poll_policy.ACTIVE_MAX_INTERVAL_S
            and (binding_pct(active_pre.last_good, self._models) or 0.0) < 100.0
        )
        overslept_plan = (
            active_pre is not None
            and plan_oversleeps_interval(active_pre, now)
        )
        if (
            active_pre is None
            or active_pre.age_s is None
            or stale_candidate_plan
            or overslept_plan
            or (
                active_pre.next_poll_at is not None
                and now >= active_pre.next_poll_at
            )
            or (
                active_pre.next_poll_at is None
                and active_pre.age_s >= poll_policy.MIN_INTERVAL_S
            )
        ) and not (
            # The statusline is reporting this account live: the API poll
            # only refreshes the windows it lacks, so it keeps the normal
            # ceiling instead of the urgent cadence that trips 429s.
            live
            and active_pre is not None
            and active_pre.age_s is not None
            and active_pre.age_s < poll_policy.ACTIVE_MAX_INTERVAL_S
        ):
            plan.add(current)
        if self._idle_hold_since is None:
            pick = due_candidate(candidates, pre, now)
            if pick is not None:
                plan.add(pick)
        entries = self.switcher.usage_entries_by_account(
            fetch=plan,
            # A candidate-style plan on the active slot is deliberately
            # overridden after the active age cap; every other baseline
            # nomination preserves a valid future plan under the store lock.
            scheduled=not stale_candidate_plan,
        )
        usage = {num: entry.decision_value() for num, entry in entries.items()}
        self._overlay_live(current, usage, live, now)

        active_value = usage.get(current)
        active_headroom = oauth.account_headroom(
            active_value if isinstance(active_value, dict) else None, self._models
        )
        # The caller's tick-snapshotted threshold, so one tick fetches and
        # decides on the same value even if apply_threshold() lands mid-tick.
        if threshold is None:
            threshold = self.settings.threshold
        escalate = bool(candidates) and (
            (active_headroom is None and active_value != USAGE_TOKEN_EXPIRED)
            or (
                active_headroom is not None
                and 100.0 - active_headroom >= threshold - ESCALATION_MARGIN_PCT
            )
            or (
                # Burning toward the limit guard (last tick's rate): the
                # move it is about to force needs fresh candidates.
                active_headroom is not None
                and (runway := self._runway_s(current, active_headroom)) is not None
                and runway <= GUARD_PREFETCH_S
            )
            or (
                # The same, on the pace this very reading proves: a sudden
                # burn fires the guard on its first tick, before any rate
                # from an earlier tick exists.
                active_headroom is not None
                and active_headroom > 0
                and (fast := self._burn.fast_rate(self._burn_sample(
                    current, active_value, entries.get(current), 0.0
                ))) is not None
                and active_headroom * self._cap(current) / fast <= GUARD_PREFETCH_S
            )
        )
        if escalate:
            escalation_fetch = {current, *candidates}
            # Escalation may beat ordinary candidate plans to obtain a fresh
            # switch decision, but a decision-trusted exhausted row cannot be
            # a target. Preserve any wider post-429 plan instead of refetching
            # that token at the bounded all-exhausted wake cadence.
            for num in tuple(escalation_fetch):
                entry = entries.get(num)
                value = usage.get(num)
                planned_headroom = oauth.account_headroom(
                    value if isinstance(value, dict) else None, self._models
                )
                if (
                    entry is not None
                    and entry.next_poll_at is not None
                    and now < entry.next_poll_at
                    and (entry.poll_interval_s or 0.0)
                    > poll_policy.EXHAUSTED_INTERVAL_S
                    and planned_headroom is not None
                    and planned_headroom <= 0
                ):
                    escalation_fetch.remove(num)
            entries = self.switcher.usage_entries_by_account(
                fetch=escalation_fetch
            )
            usage = {num: entry.decision_value() for num, entry in entries.items()}
            self._overlay_live(current, usage, live, now)

        self._live_check(current, active_pre, entries.get(current), live)
        headroom = _headroom_by_account(usage, self._models)
        return entries, usage, headroom

    def _live_readings(self, current: str, entries: dict, now: float) -> list[dict]:
        """Statusline readings attributable to the active account (never raises).

        Accounts are told apart by their weekly reset: the stored 7-day
        ``resets_at`` when known, else the remembered weekly anchor.
        """
        try:
            readings = live_usage.read_readings(self.switcher.backup_dir, now)
            if not readings:
                return []
            weekly: dict[str, float | None] = {}
            for num, entry in entries.items():
                last = entry.last_good if isinstance(entry.last_good, dict) else {}
                seven = last.get("seven_day") if isinstance(last.get("seven_day"), dict) else {}
                weekly[num] = live_usage.to_ts(seven.get("resets_at"))
            if any(ts is None for ts in weekly.values()) or current not in weekly:
                try:
                    for num, inputs in self.switcher.perish_inputs(now).items():
                        if weekly.get(num) is None and inputs.get("anchorNext"):
                            weekly[num] = float(inputs["anchorNext"])
                except Exception:
                    _logger.debug("weekly anchors unavailable", exc_info=True)
            return live_usage.attribute(readings, weekly, current)
        except Exception:
            _logger.debug("live usage read failed", exc_info=True)
            return []

    def _live_check(self, current: str, before, after, live: list[dict]) -> None:
        """Log how a fresh API poll of the active account compares with live.

        Evidence for trusting the statusline feed: one ``live-check`` line
        per fresh poll while live readings exist. ``diff`` is API minus live
        on the same window instance (usage rises between the two, so small
        positive diffs are expected); a larger gap is a WARNING. Readings of
        this very week that could not be attributed to the account are
        logged too — that would mean the weekly-reset matching is missing.
        """
        try:
            if after is None or after.fetched_at is None or not isinstance(after.last_good, dict):
                return
            if before is not None and before.fetched_at is not None and after.fetched_at <= before.fetched_at:
                return  # no fresh poll this tick
            api = after.last_good
            rows = live_usage.compare(api, live, after.fetched_at)
            if rows:
                worst = max(abs(r["diff"]) for r in rows)
                detail = ", ".join(
                    f"{r['window']} api={r['api']:.0f} live={r['live']:.0f} "
                    f"diff={r['diff']:+.0f} skew={r['skew_s']:.0f}s"
                    for r in rows
                )
                level = logging.WARNING if worst > live_usage.DISAGREE_PCT else logging.INFO
                _logger.log(level, "live-check account %s: %s", current, detail)
                return
            if not live:
                fresh = live_usage.read_readings(self.switcher.backup_dir, after.fetched_at)
                orphans = live_usage.same_week(fresh, api)
                if orphans:
                    _logger.warning(
                        "live-check account %s: %d live reading(s) of this week "
                        "were not attributed to it", current, len(orphans),
                    )
        except Exception:
            _logger.debug("live-check failed", exc_info=True)

    def _overlay_live(
        self,
        current: str,
        usage: dict[str, dict | str | None],
        live: list[dict],
        now: float,
    ) -> None:
        """Fold live statusline windows into the active account's decision value.

        Sentinels (expired token, API key, ...) are left alone: they carry a
        meaning live numbers cannot override.
        """
        if not live:
            return
        value = usage.get(current)
        if value is not None and not isinstance(value, dict):
            return
        merged, changed = live_usage.merge(value, live, now)
        if changed:
            usage[current] = merged
        # Live readings mean the account is in use: a cached lapse is stale.
        note = getattr(self.switcher, "note_live_usage", None)
        if note is not None:
            try:
                note(current)
            except Exception:
                _logger.debug("live plan re-check failed", exc_info=True)

    def _perform(
        self,
        number: str,
        email: str,
        trigger: str,
        left: tuple[float | None, float],
        detail: str = "",
    ) -> TickOutcome:
        if not self.settings.enabled:
            # Belt and braces. `_tick_inner` already short-circuits, so
            # nothing reaches here today — which is the point: this is the
            # last gate before `switch_to`, so no future path (a new
            # trigger, a caller that skips the tick) can slip a switch
            # through while the user believes placement is external.
            self._emit(
                NoSwitchEvent(
                    reason="placement-external",
                    detail="autoswitch.enabled=false; refusing to switch",
                )
            )
            return TickOutcome.NO_ACTION

        if self.dry_run:
            current = self.switcher.current_account_number()
            current_email = self.switcher.account_email(current) if current else ""
            self._emit(
                SwitchEvent(
                    trigger=trigger,
                    from_ref=_ref(current, current_email) if current else None,
                    to_ref=_ref(number, email),
                    dry_run=True,
                )
            )
            return TickOutcome.SWITCHED

        # Hold the state lock across the whole recheck -> switch -> record
        # sequence so two concurrent engines (loop + cron --once) make one
        # serialized decision: the loser re-reads the winner's lastSwitchAt
        # and backs off instead of double-switching. No deadlock cycle: the
        # switch path (aswap FileLock + Claude Code locks) never takes the
        # state lock.
        with self._state_lock():
            state = self._read_state()
            if (
                trigger in ("proactive", "consume-first")
                and not self._limit_guard
                and self._in_cooldown(state)
            ):
                self._emit(NoSwitchEvent(reason="cooldown"))
                return TickOutcome.NO_ACTION

            # The account this tick decided on, by identity: if another
            # process renumbered/moved accounts since, the number may now
            # name someone else, and switch_to refuses (under its lock)
            # instead of activating them. The next tick decides afresh.
            roster = (
                self._roster
                if self._roster is not None
                else self.switcher.roster_identities()
            )
            expect = roster.get(str(number))
            if expect is None or expect[0] != email:
                self._emit(
                    NoSwitchEvent(
                        reason="roster-changed",
                        detail=f"account {number} changed since this tick began",
                    )
                )
                return TickOutcome.NO_ACTION
            try:
                result = self.switcher.switch_to(
                    number, json_output=True, expect=expect
                )
            except RosterChangedError as e:
                self._emit(NoSwitchEvent(reason="roster-changed", detail=str(e)))
                return TickOutcome.NO_ACTION
            if not result or not result.get("switched"):
                self._emit(
                    NoSwitchEvent(
                        reason="already-active",
                        detail=(result or {}).get("reason", ""),
                    )
                )
                return TickOutcome.NO_ACTION

            state["schemaVersion"] = STATE_SCHEMA_VERSION
            state["lastSwitchAt"] = self.clock()
            state["lastSwitchTo"] = number
            # WHERE we came from, so the next tick can refuse to undo this,
            # and WHAT IT LOOKED LIKE, so that refusal has a release that burn
            # cannot fake. See `_left_account_recovered` for why the present
            # state alone cannot supply one. `inf` is stored as null: it is not
            # portable JSON, and every other reader of this file would have to
            # learn about it.
            state["lastSwitchFrom"] = (result.get("from") or {}).get("number")
            state["leftHeadroom"], recovery = left
            state["leftRecoveryAt"] = None if recovery == float("inf") else recovery
            # A `consume-first` phase-2 refetch can write the SAME (None,
            # None) shape a `failover` departure writes, whenever the
            # refetched active row has a `pct` but is otherwise unmeasurable
            # in the same tick its weekly reset is known --
            # `account_headroom` needs a numeric `pct`, `_seven_day_reset_ts`
            # needs only `resets_at`. Inferring the trigger from the two
            # nulls then runs the wrong legs. Record it directly so the
            # reader never has to guess.
            state["leftTrigger"] = trigger
            # The engine moved the work, so a hold on a hand-picked account
            # is over; the next hand switch starts a new one.
            state.pop("manualHold", None)
            state["lastSeenActive"] = number
            atomic_write_json(self.state_path, state)
        self.manual_hold = None

        # Why, next to the switcher's own "Switched from account X to Y" line:
        # without it a log cannot tell an engine move from a manual one.
        _logger.info("Auto-switch to account %s (%s): %s", number, trigger, detail or "-")

        self._emit(
            SwitchEvent(
                trigger=trigger,
                from_ref=result.get("from"),
                to_ref=result.get("to"),
                warnings=result.get("warnings", []),
            )
        )
        return TickOutcome.SWITCHED

    # -- helpers --------------------------------------------------------------

    def _in_cooldown(self, state: dict) -> bool:
        last = state.get("lastSwitchAt")
        if not isinstance(last, (int, float)):
            return False
        return (self.clock() - last) < self.settings.cooldown_seconds

    def _check_model_names(
        self, quarantined: set[str], usage: dict[str, dict | str | None]
    ) -> None:
        """One-shot ``autoswitch.model`` typo guard.

        A configured name that no account reports means the filter looks
        active while gating nothing. That's only provable once every
        relevant oauth account has readable usage this tick — adaptive
        polling legitimately leaves gaps before that — and never worth a
        forced refresh of its own.
        """
        wanted = {m.lower(): m for m in self._models if m.lower() != "all"}
        if not wanted:
            self._model_check_done = True  # bare "all" needs no name match
            return
        relevant = [
            n
            for n in self.switcher.switchable_account_numbers()
            if n not in quarantined
            and self.switcher.account_kind_for(n) != "api_key"
        ]
        values = [usage.get(n) for n in relevant]
        readable = [v for v in values if isinstance(v, dict)]
        if not readable or len(readable) != len(values):
            return  # not every account observed yet — re-check next tick
        seen = {
            s["name"].lower()
            for v in readable
            for s in (v.get("scoped") or [])
            if isinstance(s, dict) and isinstance(s.get("name"), str)
        }
        self._model_check_done = True
        missing = [name for low, name in wanted.items() if low not in seen]
        if missing:
            self._emit(
                ConfigWarningEvent(
                    message=(
                        f"autoswitch.model: {', '.join(missing)} matches no "
                        "account's usage windows — only the 5h/7d limits are "
                        "being watched for it (typo?)"
                    )
                )
            )

    def _earliest_recovery(
        self, usage: dict[str, dict | str | None]
    ) -> datetime | None:
        """Earliest moment any account becomes usable again (UTC), or None
        when that moment can't be proven.

        Per account that's the *latest* reset among its ≥100% relevant
        windows — an account blocked on both 5h and a scoped weekly limit
        isn't usable when the 5h rolls over — then the minimum across
        accounts, the active one included (its recovery also ends the
        blocked state). A blocked account whose exhausted windows carry no
        reset time at all could recover at any moment, so it makes the whole
        answer unprovable: return None and let the bounded blocked-cadence
        fallback re-check, rather than sleeping toward another account's
        later known reset."""
        earliest: float | None = None
        now = self.clock()
        for value in usage.values():
            if not isinstance(value, dict):
                continue
            blocked = [
                resets_at
                for _, pct, resets_at in oauth.relevant_windows(value, self._models)
                if pct >= 100.0
            ]
            if not blocked:
                continue  # not exhausted — doesn't gate the blocked state
            usable_at = _limiting_reset_ts(value, self._models)
            if usable_at is None or usable_at <= now:
                return None  # blocked with unprovable recovery — don't oversleep
            if earliest is None or usable_at < earliest:
                earliest = usable_at
        if earliest is None:
            return None
        return datetime.fromtimestamp(earliest, tz=timezone.utc)

    def _emit(self, event: AutoSwitchEvent) -> None:
        self.on_event(event)

    # -- loop -------------------------------------------------------------------

    def stop(self) -> None:
        """Ask ``run_loop`` to exit; wakes it from any sleep. Safe to call
        before the loop starts — the stop is never cleared, so the loop
        exits immediately (engines are single-use)."""
        self._stop.set()
        self._wake.set()
        if self._warmup is not None:
            self._warmup.stop()

    def wake(self) -> None:
        """Cut the current inter-tick sleep short and tick now."""
        self._wake.set()

    def apply_threshold(self, threshold: float) -> None:
        """Session override from the TUI: retarget the trigger and poll
        cadence mid-run. Threshold only — the model axes (and their derived
        state) are fixed at construction. The frozen-settings swap is atomic
        and each tick snapshots ``self.settings`` once, so no locking."""
        self.settings = replace(self.settings, threshold=threshold)
        self._live.pin("threshold")  # a stored change must not undo it
        self.switcher.set_poll_policy_inputs(threshold, self._models)

    def apply_warmup_enabled(self, enabled: bool) -> None:
        """Apply the TUI's persisted warm-up toggle to this running engine."""
        if enabled == self.settings.warmup_five_hour:
            return
        self.settings = replace(self.settings, warmup_five_hour=enabled)
        self._restart_warmup(enabled)
        self._wake.set()

    def _restart_warmup(self, enabled: bool) -> None:
        if self._warmup is not None:
            self._warmup.stop()
        self._warmup = None
        self._next_warmup_at = None
        if enabled:
            self._enable_warmup()

    # -- live settings ----------------------------------------------------------

    def _reload_settings(self) -> None:
        """Pick up an ``aswap config`` change made while this engine runs.

        Runs at the top of every tick, before the tick snapshots
        ``self.settings``, so a whole tick decides on one set of values. Costs
        one indexed lookup unless the settings document was written since the
        last look; then its autoswitch section is re-read strictly (see
        ``reload_autoswitch_settings``) and every key whose stored value
        changed is applied, except the pinned ones. Never raises: a failed
        reload keeps the settings the engine already has.

        What a change does NOT reset: the manual hold (it lives in the state
        file; a new ``manualHoldMinutes`` re-times the hold already running,
        and 0 ends it), cooldown and quarantine state, the burn-rate history,
        the unhealthy-tick count, gate verdicts and the usage cache. A
        ``model`` change does restart the burn samples -- they measured the
        old axes -- and the one-shot model-name check; an ``autoAddAccounts``
        change forgets the half-confirmed login observation, so a login is
        still adopted only after two polls under the new value.
        """
        apply_settings_reload(self, self._live.poll(self.settings))

    def _settings_applied(
        self, old: AutoSwitchSettings, new: AutoSwitchSettings
    ) -> None:
        """Carry a reloaded value into the state derived from it."""
        if new.model != old.model:
            self._models = parse_model_names(new.model)
            self._model_check_done = not self._models
            self._burn = BurnTracker()
        if new.threshold != old.threshold or new.model != old.model:
            self.switcher.set_poll_policy_inputs(new.threshold, self._models)
        if new.auto_add_accounts != old.auto_add_accounts:
            self._live_login_seen = None
            self._auto_add_hinted = False
        if new.warmup_five_hour != old.warmup_five_hour:
            self._restart_warmup(new.warmup_five_hour)

    def _sleep(self, delay: float) -> None:
        """Wait ``delay`` seconds, ending early on wake/stop or a settings change.

        Sliced by count, not against a deadline, so a test that stubs the
        wait still finishes in a bounded number of slices.
        """
        sliced_wait(delay, self._wake, self._stop, self._live.changed)

    def _next_delay(self, outcome: TickOutcome) -> float:
        interval = self.settings.interval_seconds
        delay: float | None = None
        if outcome is TickOutcome.BLOCKED:
            if self._sleep_until_ts is not None:
                until_reset = self._sleep_until_ts - self.clock()
                delay = min(max(until_reset, interval), MAX_SLEEP_S)
            elif self._blocked_wait_long:
                # Truly exhausted with no reset time known / no candidates.
                delay = max(interval, NO_RESET_FALLBACK_S)
            # Blocked on something that can resolve any tick (hysteresis,
            # unreadable usage) — keep the normal cadence so the at-limit
            # escape isn't missed.
        elif outcome is TickOutcome.NO_ACTION and self._idle_hold_slow:
            # Idle-hold: Claude is idle on an expired token — nothing changes
            # until the user comes back, so crawl. Worst case protection
            # resumes one slow tick after they do.
            delay = max(interval, NO_RESET_FALLBACK_S)
        if delay is None:
            # ±10% jitter so multiple machines don't synchronize their API hits.
            delay = self._respect_poll_plan(
                interval * (0.9 + 0.2 * random.random())
            )
            if self._live_active:
                delay = min(delay, LIVE_TICK_S)
                if self._time_to_limit is not None:
                    # Closing on the limit: re-read the (free) live feed
                    # several times before the projected hit.
                    delay = min(delay, max(LIVE_MIN_TICK_S, self._time_to_limit / 4))
            if self._riding:
                # Burning the last point behind an open gate: never sleep
                # past the next re-probe that can see it close.
                delay = min(delay, gate.RIDE_REPROBE_S)
            if self._endgame:
                delay = min(
                    delay, random.uniform(ENDGAME_TICK_MIN_S, ENDGAME_TICK_MAX_S)
                )
            if self._edge_deadline is not None:
                # Wake on the last point's deadline, not a tick after it.
                delay = min(delay, max(0.5, self._edge_deadline - self.clock()))
        return self._respect_warmup_deadline(delay)

    def _respect_warmup_deadline(self, delay: float) -> float:
        """Do not let a slow auto cadence delay an enabled warm-up sweep."""
        warmer = self._warmup
        due = self._next_warmup_at
        if warmer is None or due is None:
            return delay
        return min(delay, max(0.0, due - self.clock()))

    def _respect_poll_plan(self, delay: float) -> float:
        """Shorten a normal-cadence sleep to the store's own next-poll time.

        The planner tightens the active row to URGENT_INTERVAL_S while it
        burns toward the threshold, but the loop always slept
        ``interval_seconds`` — so the plan ran late. Measured mid-episode: the
        row was due 112s ago while the engine still had minutes of sleep left.

        Only ever shortens, never below the planner's floor: the 429 budget
        lives in the plan, and this makes the loop obey it rather than
        override it. Best-effort — the unshortened delay is always safe.
        """
        try:
            current = self.switcher.current_account_number()
            if current is None:
                return delay
            entry = self.switcher.usage_entries_by_account(fetch=set()).get(current)
            if entry is None or entry.next_poll_at is None:
                return delay
            due_in = entry.next_poll_at - self.clock()
            # Clamp the DEADLINE, not the result. max(min(delay, due_in), U)
            # raises a delay that was ALREADY below U: at the configurable
            # floor of 15s it turns a 13.5s jittered sleep into 60s, and at
            # the 60s default it flattens the entire lower jitter half.
            # Bounding due_in instead keeps "only ever shortens" true at every
            # configured interval, and still refuses to poll faster than the
            # planner's own floor when the row is overdue.
            return min(delay, max(due_in, poll_policy.URGENT_INTERVAL_S))
        except Exception:
            return delay

    def run_loop(self) -> int:
        """Tick forever (until :meth:`stop`); a failing tick never kills it."""
        while True:
            # Clear at the top, not after the wait: a wake() racing a wait
            # timeout is then never lost — the tick right after this clear
            # already sees whatever settings that wake announced.
            self._wake.clear()
            if self._stop.is_set():
                return 0
            try:
                outcome = self.tick()
            except Exception as e:  # pragma: no cover - tick() already guards
                self._emit(
                    ErrorEvent(message=f"{type(e).__name__}: {e}", transient=True)
                )
                outcome = TickOutcome.ERROR
            delay = self._next_delay(outcome)
            if delay > self.settings.interval_seconds * 1.5:
                until = datetime.now(timezone.utc) + timedelta(seconds=delay)
                self._emit(
                    SleepEvent(
                        seconds=delay,
                        until=until.isoformat(timespec="seconds").replace(
                            "+00:00", "Z"
                        ),
                    )
                )
            self._sleep(delay)
