"""Tier capacity, burn rate, landing runway and the limit guard (autoswitch)."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap.autoswitch import (
    LIMIT_GUARD_S,
    LIVE_MIN_TICK_S,
    BurnTracker,
    NoSwitchEvent,
    SwitchEvent,
    TickOutcome,
    tier_capacity,
)
from tests.test_autoswitch import EngineHarness, _usage, _usage7

_R_SOON = "2024-01-05T00:00:00Z"
_R_LATER = "2024-01-08T00:00:00Z"

TIERS = {"5x": "default_claude_max_5x", "20x": "default_claude_max_20x"}


def _harness(temp_home: Path, tiers: dict[str, str] | None = None, **settings) -> EngineHarness:
    h = EngineHarness(temp_home, **settings)
    h.seed(1, "a@example.com")
    h.seed(2, "b@example.com")
    h.seed(3, "c@example.com")
    h.make_live("a@example.com", 1)
    inputs = {
        num: {"endsAt": None, "anchorNext": None, "tier": TIERS[t]}
        for num, t in (tiers or {}).items()
    }
    patcher = patch.object(h.switcher, "perish_inputs", return_value=inputs)
    patcher.start()
    h._patcher = patcher
    return h


@pytest.fixture(autouse=True)
def _stop_patches():
    yield
    patch.stopall()


def _switch(h: EngineHarness) -> SwitchEvent:
    return next(e for e in h.events if isinstance(e, SwitchEvent))


class TestTierCapacity:
    def test_capacity_per_tier(self):
        assert tier_capacity("default_claude_max_20x") == 4.0
        assert tier_capacity("default_claude_max_5x") == 1.0
        assert tier_capacity("default_claude_ai") == 0.2
        assert tier_capacity(None) == 1.0

    def test_20x_with_less_percent_left_beats_5x_with_more(self, temp_home):
        # 20 points of a 20x account (80 in 5x units) is more work than 60
        # points of a 5x account; percentages alone picked the 5x.
        h = _harness(temp_home, {"1": "5x", "2": "5x", "3": "20x"})
        outcome = h.tick_with_usage({"1": _usage(95), "2": _usage(40), "3": _usage(80)})
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 3

    def test_single_tier_fleet_ranks_by_percent_as_before(self, temp_home):
        h = _harness(temp_home, {"1": "20x", "2": "20x", "3": "20x"})
        outcome = h.tick_with_usage({"1": _usage(95), "2": _usage(40), "3": _usage(80)})
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2

    def test_hysteresis_is_measured_in_work(self, temp_home):
        # Leaving a 20x at 95% (5 pts = 20 units) for a 5x at 75% (25 units)
        # gains 1.25 points of the biggest tier, under the 10-point margin.
        h = _harness(temp_home, {"1": "20x", "2": "5x"})
        outcome = h.tick_with_usage({"1": _usage(95), "2": _usage(75), "3": _usage(100)})
        assert outcome is TickOutcome.BLOCKED
        assert h.active_number() == 1

    def test_at_limit_still_escapes_to_a_small_account(self, temp_home):
        h = _harness(temp_home, {"1": "20x", "2": "5x"})
        outcome = h.tick_with_usage({"1": _usage(100), "2": _usage(75), "3": _usage(100)})
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2


class TestBurnTracker:
    def test_rate_in_capacity_units(self):
        t = BurnTracker()
        t.record(0.0, "1", 50.0, 4.0)
        t.record(60.0, "1", 55.0, 4.0)
        assert t.rate() == pytest.approx(5.0 * 4.0 / 60.0)

    def test_needs_a_minute_of_evidence(self):
        t = BurnTracker()
        t.record(0.0, "1", 50.0, 1.0)
        t.record(30.0, "1", 55.0, 1.0)
        assert t.rate() is None

    def test_rollover_and_account_changes_are_skipped(self):
        t = BurnTracker()
        t.record(0.0, "1", 90.0, 1.0)
        t.record(60.0, "1", 96.0, 1.0)   # +6 over 60s
        t.record(120.0, "2", 10.0, 4.0)  # switched: no leg across accounts
        t.record(180.0, "2", 12.0, 4.0)  # +2 x 4 over 60s
        t.record(240.0, "2", 0.0, 4.0)   # window rolled over: skipped
        assert t.rate() == pytest.approx((6.0 + 8.0) / 120.0)

    def test_old_samples_age_out(self):
        t = BurnTracker()
        t.record(0.0, "1", 10.0, 1.0)
        t.record(60.0, "1", 20.0, 1.0)
        t.record(10_000.0, "1", 30.0, 1.0)
        assert t.rate() is None


class TestLimitGuard:
    def test_switches_below_threshold_when_the_limit_is_minutes_away(self, temp_home, caplog):
        h = _harness(temp_home, {"1": "5x", "2": "20x", "3": "20x"})
        assert h.tick_with_usage({"1": _usage(70), "2": _usage(10), "3": _usage(50)}) is TickOutcome.NO_ACTION
        h.clock.advance(60)
        # 10 points a minute with 20 left: two minutes to the limit.
        with caplog.at_level(logging.INFO, logger="claude-swap"):
            outcome = h.tick_with_usage({"1": _usage(80), "2": _usage(10), "3": _usage(50)})
        assert outcome is TickOutcome.SWITCHED
        assert _switch(h).trigger == "proactive"
        assert h.active_number() == 2
        line = next(r.getMessage() for r in caplog.records if "Auto-switch" in r.getMessage())
        assert "limit projected in 2.0 min" in line
        assert "from 1 80% used, 5x" in line and "to 2 10% used, 20x" in line

    def test_slow_burn_below_threshold_stays(self, temp_home):
        h = _harness(temp_home, {"1": "5x", "2": "20x"})
        h.tick_with_usage({"1": _usage(70), "2": _usage(10), "3": _usage(50)})
        h.clock.advance(600)
        outcome = h.tick_with_usage({"1": _usage(71), "2": _usage(10), "3": _usage(50)})
        assert outcome is TickOutcome.NO_ACTION
        assert h.engine._time_to_limit > LIMIT_GUARD_S

    def test_guard_ignores_the_cooldown(self, temp_home):
        h = _harness(temp_home, {"1": "5x", "2": "20x"})
        h.engine._mutate_state(lambda s: s.update(lastSwitchAt=h.clock.now))
        h.tick_with_usage({"1": _usage(70), "2": _usage(10), "3": _usage(50)})
        h.clock.advance(60)
        outcome = h.tick_with_usage({"1": _usage(80), "2": _usage(10), "3": _usage(50)})
        assert outcome is TickOutcome.SWITCHED

    def test_live_ticks_speed_up_near_the_limit(self, temp_home):
        h = _harness(temp_home)
        h.engine._live_active = True
        h.engine._time_to_limit = 40.0
        assert h.engine._next_delay(TickOutcome.NO_ACTION) == pytest.approx(10.0)
        h.engine._time_to_limit = 4.0
        assert h.engine._next_delay(TickOutcome.NO_ACTION) == LIVE_MIN_TICK_S


class TestLandingRunway:
    def _burning(self, h: EngineHarness, used: float) -> None:
        # One point a minute on the 5x active account, recorded before the tick.
        h.engine._burn.record(h.clock.now - 120, "1", used - 2.0, 1.0)

    def test_consume_first_skips_a_target_that_would_not_last(self, temp_home):
        h = _harness(temp_home, {"1": "5x", "2": "5x", "3": "5x"}, strategy="consume-first")
        self._burning(h, 50)
        outcome = h.tick_with_usage({
            "1": _usage7(50, 20, _R_LATER),
            "2": _usage7(88, 10, _R_SOON),   # 12 points: 12 minutes at this rate
            "3": _usage7(10, 10, _R_LATER),
        })
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 1
        assert [e.reason for e in h.events if isinstance(e, NoSwitchEvent)] == [
            "already-consuming-soonest"
        ]

    def test_consume_first_takes_a_target_with_runway(self, temp_home):
        h = _harness(temp_home, {"1": "5x", "2": "20x", "3": "5x"}, strategy="consume-first")
        self._burning(h, 50)
        outcome = h.tick_with_usage({
            "1": _usage7(50, 20, _R_LATER),
            "2": _usage7(88, 10, _R_SOON),   # 12 points of a 20x: 48 minutes
            "3": _usage7(10, 10, _R_LATER),
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2

    def test_proactive_prefers_a_long_runway_over_a_sooner_reset(self, temp_home):
        h = _harness(temp_home, {"1": "5x", "2": "5x", "3": "5x"}, strategy="consume-first")
        self._burning(h, 95)
        # #2 resets sooner but holds 12 minutes at one point a minute; #3 20.
        outcome = h.tick_with_usage({
            "1": _usage7(95, 20, _R_LATER),
            "2": _usage7(88, 10, _R_SOON),
            "3": _usage7(80, 10, _R_LATER),
        })
        assert outcome is TickOutcome.SWITCHED
        assert _switch(h).trigger == "proactive"
        assert h.active_number() == 3

    def test_an_escape_still_lands_short_when_nothing_else_has_room(self, temp_home):
        h = _harness(temp_home, {"1": "5x", "2": "5x", "3": "5x"}, strategy="consume-first")
        self._burning(h, 95)
        outcome = h.tick_with_usage({
            "1": _usage7(95, 20, _R_LATER),
            "2": _usage7(88, 10, _R_SOON),
            "3": _usage7(100, 10, _R_LATER),
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2

    def test_idle_active_account_still_guards_the_landing(self, temp_home):
        # No burn measured (idle): the default rate still keeps consume-first
        # off a 20x with 3 points left (12 minutes at a point a minute).
        h = _harness(temp_home, {"1": "20x", "2": "20x", "3": "20x"}, strategy="consume-first")
        outcome = h.tick_with_usage({
            "1": _usage7(43, 20, _R_LATER),
            "2": _usage7(97, 10, _R_SOON),
            "3": _usage7(10, 10, _R_LATER),
        })
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 1

    def test_idle_active_account_lands_where_there_is_room(self, temp_home):
        h = _harness(temp_home, {"1": "20x", "2": "20x", "3": "20x"}, strategy="consume-first")
        outcome = h.tick_with_usage({
            "1": _usage7(43, 20, _R_LATER),
            "2": _usage7(80, 10, _R_SOON),
            "3": _usage7(10, 10, _R_LATER),
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2
