"""The last-point clock (edge.py)."""

from __future__ import annotations

from claude_swap import edge
from claude_swap.edge import EDGE_MARGIN_S, EDGE_SHARE, UNKNOWN, LastPointClock

NOW = 1_800_000_000.0


def _read(clock: LastPointClock, at: float, d7: float, d5: float | None = None, now=None):
    gate = {"probedAt": at, "seven_day": {"pct": d7}}
    if d5 is not None:
        gate["five_hour"] = {"pct": d5}
    clock.observe("1", gate, at if now is None else now)


class TestLastPointClock:
    def test_no_deadline_below_the_last_point(self):
        c = LastPointClock()
        _read(c, NOW, 97.0)
        _read(c, NOW + 5, 98.0)
        assert c.deadline(60.0) is None
        assert not c.running()

    def test_starts_from_the_last_reading_under_99(self):
        c = LastPointClock()
        _read(c, NOW, 98.0)
        _read(c, NOW + 4, 99.0)
        assert c.running()
        assert c.deadline(60.0) == NOW + 60.0 * EDGE_SHARE - EDGE_MARGIN_S

    def test_first_seen_at_99_is_unknown(self):
        c = LastPointClock()
        _read(c, NOW, 99.0)
        assert c.deadline(60.0) == UNKNOWN
        assert not c.running()

    def test_a_point_measured_shorter_than_the_burn_caps_it(self):
        c = LastPointClock()
        _read(c, NOW, 96.0)
        _read(c, NOW + 3, 97.0)  # tick to 97 in (NOW, NOW+3]
        _read(c, NOW + 30, 97.0)
        _read(c, NOW + 33, 98.0)  # tick to 98 in (NOW+30, NOW+33]: 27 s at least
        _read(c, NOW + 50, 98.0)
        _read(c, NOW + 53, 99.0)
        assert c.deadline(600.0) == NOW + 50 + 17.0 * EDGE_SHARE - EDGE_MARGIN_S

    def test_the_earliest_window_wins(self):
        c = LastPointClock()
        _read(c, NOW, 98.0, 98.0)
        _read(c, NOW + 5, 98.0, 99.0)
        _read(c, NOW + 20, 99.0, 99.0)
        assert c.deadline(60.0) == NOW + 60.0 * EDGE_SHARE - EDGE_MARGIN_S

    def test_a_rollover_stops_the_clock(self):
        c = LastPointClock()
        _read(c, NOW, 98.0)
        _read(c, NOW + 5, 99.0)
        _read(c, NOW + 10, 3.0)
        assert c.deadline(60.0) is None

    def test_another_account_starts_over(self):
        c = LastPointClock()
        _read(c, NOW, 98.0)
        c.observe("2", {"probedAt": NOW + 5, "seven_day": {"pct": 99.0}}, NOW + 5)
        assert c.deadline(60.0) == UNKNOWN

    def test_stale_and_repeated_readings_are_ignored(self):
        c = LastPointClock()
        _read(c, NOW, 99.0, now=NOW + edge.READING_FRESH_S + 1)
        assert c.deadline(60.0) is None
        _read(c, NOW + 100, 98.0)
        _read(c, NOW + 100, 99.0)  # same probedAt: not a new reading
        assert c.deadline(60.0) is None

    def test_a_window_past_its_reset_is_ignored(self):
        c = LastPointClock()
        gate = {"probedAt": NOW, "seven_day": {"pct": 100.0, "resets_at": "2020-01-01T00:00:00Z"}}
        c.observe("1", gate, NOW)
        assert c.deadline(60.0) is None

    def test_no_burn_known_means_unknown(self):
        c = LastPointClock()
        _read(c, NOW, 98.0)
        _read(c, NOW + 4, 99.0)
        assert c.deadline(None) == UNKNOWN
