"""The last-point clock: leave an account before a window reaches 100%.

Measured 2026-10-29 on amin-desktop: the 5-hour window refuses requests at
100% with no grace (a probe read ``five_hour: rejected, 100``), and every
gate reading (probe headers or the statusline) is a whole percent rounded
down. Watching for 100% therefore sees it only after a request has failed,
however often the account is polled: at three 5x-points a minute one point
of a 20x account lasts about 80 s, and nothing inside it is visible.

What is visible is the moment a window ticks from 98 to 99. From then the
point that is left lasts one point's worth of burn, so the clock leaves at
that moment plus a safe share of it. The tick is bracketed by two real
readings (the last one under 99, the first at 99) and the clock starts from
the earlier, so it can only run early. A window that already read 99 the
first time it was seen during this stay has no such moment: its last point
is in an unknown state, and the clock says leave now.

Once a clock runs, probing the window again is pointless (the next tick is
the refusal), so the engine stops probing it and sleeps until the deadline.
"""

from __future__ import annotations

from claude_swap.endpoints import scaled  # test-only clock scale (1.0 outside tests)
from claude_swap.gate import _reset_ts

WINDOWS = ("five_hour", "seven_day")
LAST_POINT_PCT = 99.0
# Share of the last point the clock spends, and a margin for a late reading
# and the switch itself (about a second).
EDGE_SHARE = 0.8
EDGE_MARGIN_S = 8.0
# While a clock runs the window is re-probed this often, not every 3-5 s:
# only a burst faster than the clock assumed shows up there, as a refusal.
EDGE_REPROBE_S = scaled(10.0)
# Deadline of a window whose last point started out of sight: leave now.
UNKNOWN = float("-inf")
# Readings older than this when first seen are not evidence of the present
# (a candidate probed minutes before it became active).
READING_FRESH_S = 60.0


class LastPointClock:
    """Per-window last-point deadlines of the ACTIVE account.

    Fed every tick with the active account's newest gate record (a probe or
    a statusline reading). A change of account starts over: what was seen
    on another account says nothing about this one.
    """

    def __init__(self) -> None:
        self._account: str | None = None
        self._seen_at = float("-inf")
        self._last: dict[str, tuple[float, float]] = {}  # window -> (pct, at)
        self._below: dict[str, float] = {}  # newest reading under 99
        self._crossed: dict[str, float] = {}  # earliest the 99 tick can be
        self._unknown: set[str] = set()
        self._tick_at: dict[str, tuple[float, float, float]] = {}  # pct, lo, hi
        self._point_s: dict[str, float] = {}  # shortest a point has lasted

    def reset(self, account: str | None) -> None:
        self.__init__()
        self._account = account

    def observe(self, account: str, gate: dict | None, now: float) -> None:
        if account != self._account:
            self.reset(account)
        if not isinstance(gate, dict):
            return
        at = gate.get("probedAt")
        if not isinstance(at, (int, float)) or at <= self._seen_at:
            return  # the same (or an older) reading again
        if now - at > READING_FRESH_S:
            return
        self._seen_at = float(at)
        for key in WINDOWS:
            window = gate.get(key)
            if not isinstance(window, dict):
                continue
            pct = window.get("pct")
            reset = _reset_ts(window.get("resets_at"))
            if reset is not None and reset <= now:
                continue  # read before a rollover since
            if isinstance(pct, (int, float)):
                self._window(key, float(pct), float(at))

    def _window(self, key: str, pct: float, at: float) -> None:
        last = self._last.get(key)
        self._last[key] = (pct, at)
        if last is not None and pct == last[0] + 1:
            # A tick to `pct` happened in (last at, at]. The point before it
            # lasted at least from the previous tick's latest bound to this
            # tick's earliest one.
            prev = self._tick_at.get(key)
            if prev is not None and prev[0] == last[0] and last[1] > prev[2]:
                self._point_s[key] = last[1] - prev[2]
            self._tick_at[key] = (pct, last[1], at)
        elif last is None or pct < last[0]:
            self._tick_at.pop(key, None)  # first sight, or a rollover
        if pct < LAST_POINT_PCT:
            self._below[key] = at
            self._crossed.pop(key, None)
            self._unknown.discard(key)
        elif key not in self._crossed and key not in self._unknown:
            below = self._below.get(key)
            if below is None:
                self._unknown.add(key)
            else:
                self._crossed[key] = below

    def running(self) -> bool:
        """A window is in its last point and timed (no probe can help)."""
        return bool(self._crossed)

    def deadline(self, point_s: float | None) -> float | None:
        """When to leave, or None when no window is in its last point.

        ``point_s``: how long one point of this account lasts at the current
        (peak) burn. The shortest point measured on the window itself caps
        it. ``UNKNOWN`` when a window's last point started out of sight.
        """
        if self._unknown:
            return UNKNOWN
        deadlines = []
        for key, start in self._crossed.items():
            spans = [s for s in (point_s, self._point_s.get(key)) if s and s > 0]
            if not spans:
                return UNKNOWN
            deadlines.append(start + min(spans) * EDGE_SHARE - EDGE_MARGIN_S)
        return min(deadlines) if deadlines else None

    def windows(self) -> list[str]:
        return sorted({*self._crossed, *self._unknown})
