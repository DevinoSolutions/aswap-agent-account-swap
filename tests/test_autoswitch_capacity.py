"""Tier capacity, burn rate, landing runway and the limit guard (autoswitch)."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap.autoswitch import (
    BURN_QUANTUM_PCT,
    LANDING_BURN_WINDOW_S,
    LIMIT_GUARD_S,
    LIVE_MIN_TICK_S,
    BurnTracker,
    NoSwitchEvent,
    SwitchEvent,
    TickOutcome,
    tier_capacity,
)
from claude_swap.usage_store import UsageEntry
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

    def test_a_reading_served_again_is_no_new_evidence(self):
        # Ticks 60 s apart re-serving one fetch, then the next fetch 240 s
        # after the first: the 8 points took 240 s, not the last tick's 60.
        t = BurnTracker()
        t.record(0.0, "1", 80.0, 1.0)
        t.record(0.0, "1", 80.0, 1.0)
        t.record(-30.0, "1", 79.0, 1.0)  # an older reading: ignored too
        t.record(240.0, "1", 88.0, 1.0)
        assert t.rate() == pytest.approx(8.0 / 240.0)
        assert t.fast_rate() == pytest.approx((8.0 - BURN_QUANTUM_PCT) / 240.0)


class TestFastRate:
    def test_a_jump_is_a_pace_without_a_minute_of_evidence(self):
        t = BurnTracker()
        t.record(0.0, "1", 50.0, 1.0)
        t.record(20.0, "1", 99.0, 1.0)
        assert t.rate() is None
        assert t.fast_rate() == pytest.approx((49.0 - BURN_QUANTUM_PCT) / 20.0)

    def test_rounding_steps_prove_nothing(self):
        # Two points in five seconds would read as 24 points a minute, but
        # whole-percent readings from two sources can show that much with no
        # burn at all.
        t = BurnTracker()
        t.record(0.0, "1", 80.0, 1.0)
        t.record(5.0, "1", 81.0, 1.0)
        t.record(10.0, "1", 82.0, 1.0)
        assert t.fast_rate() is None

    def test_quiet_history_does_not_dilute_a_steep_burn(self):
        t = BurnTracker()
        for i in range(31):  # ten quiet minutes at 95%
            t.record(i * 20.0, "1", 95.0, 1.0)
        t.record(620.0, "1", 96.0, 1.0)
        t.record(640.0, "1", 98.0, 1.0)  # five points a minute
        assert t.rate() == pytest.approx(3.0 / 600.0)
        fast = t.fast_rate()
        assert fast == pytest.approx((3.0 - BURN_QUANTUM_PCT) / 40.0)
        assert 2.0 / fast <= LIMIT_GUARD_S < 2.0 / t.rate()

    def test_takes_the_steepest_trailing_stretch(self):
        t = BurnTracker()
        t.record(0.0, "1", 10.0, 1.0)
        t.record(100.0, "1", 11.0, 1.0)
        t.record(120.0, "1", 21.0, 1.0)
        assert t.fast_rate() == pytest.approx((10.0 - BURN_QUANTUM_PCT) / 20.0)

    def test_a_quiet_end_pulls_it_down(self):
        t = BurnTracker()
        t.record(0.0, "1", 10.0, 1.0)
        t.record(20.0, "1", 30.0, 1.0)
        t.record(600.0, "1", 30.0, 1.0)
        assert t.fast_rate() == pytest.approx((20.0 - BURN_QUANTUM_PCT) / 600.0)

    def test_stops_at_an_account_change_and_a_rollover(self):
        t = BurnTracker()
        t.record(0.0, "1", 10.0, 1.0)
        t.record(20.0, "2", 60.0, 4.0)
        assert t.fast_rate() is None  # one reading of the new account
        t.record(40.0, "2", 70.0, 4.0)
        assert t.fast_rate() == pytest.approx((10.0 - BURN_QUANTUM_PCT) * 4.0 / 20.0)
        t.record(60.0, "2", 0.0, 4.0)   # the window rolled over
        t.record(80.0, "2", 1.0, 4.0)
        assert t.fast_rate() is None

    def test_a_pending_reading_is_counted_but_not_kept(self):
        t = BurnTracker()
        t.record(0.0, "1", 50.0, 1.0)
        assert t.fast_rate((20.0, "1", 99.0, 1.0)) == pytest.approx(47.0 / 20.0)
        assert t.fast_rate((0.0, "1", 99.0, 1.0)) is None  # not newer: ignored
        assert t.fast_rate() is None


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

    def test_a_jump_fires_the_guard_on_its_first_reading(self, temp_home):
        # Measured before: 50% -> 99% under a 99.9% threshold waited a full
        # minute of evidence for the windowed rate (three more live ticks).
        h = _harness(temp_home, threshold=99.9)
        assert h.tick_with_usage({"1": _usage(50), "2": _usage(10), "3": _usage(50)}) is TickOutcome.NO_ACTION
        h.clock.advance(20)
        outcome = h.tick_with_usage({"1": _usage(99), "2": _usage(10), "3": _usage(50)})
        assert outcome is TickOutcome.SWITCHED
        assert _switch(h).trigger == "proactive"
        assert h.active_number() == 2

    def test_a_steep_burn_after_a_quiet_spell_moves_before_the_limit(self, temp_home):
        # Measured before: ten quiet minutes diluted five points a minute to
        # 0.3, and the account ran into its limit with the guard silent.
        h = _harness(temp_home, threshold=99.9)
        others = {"2": _usage(10), "3": _usage(50)}
        for _ in range(31):
            assert h.tick_with_usage({"1": _usage(95), **others}) is TickOutcome.NO_ACTION
            h.clock.advance(20)
        assert h.tick_with_usage({"1": _usage(96), **others}) is TickOutcome.NO_ACTION
        h.clock.advance(20)
        outcome = h.tick_with_usage({"1": _usage(98), **others})
        assert outcome is TickOutcome.SWITCHED
        assert _switch(h).trigger == "proactive"
        assert h.engine._time_to_limit <= LIMIT_GUARD_S

    def test_a_reading_served_again_is_not_read_as_a_burst(self, temp_home):
        # No statusline: ticks every 60 s re-serve one fetch, and the next
        # fetch shows 8 points gained over 240 s (two a minute, six minutes
        # of runway). Timed by the tick, the last 60 s alone would have
        # claimed six points a minute and a limit two minutes out.
        h = _harness(temp_home, threshold=99.9)
        fetched = h.clock.now
        others = {"2": _usage(10), "3": _usage(50)}

        def entries(pct: float, at: float) -> dict:
            return {
                "1": UsageEntry(last_good=_usage(pct), fetched_at=at, age_s=h.clock.now - at),
                **{n: UsageEntry(last_good=v, fetched_at=h.clock.now, age_s=0.0) for n, v in others.items()},
            }

        for _ in range(4):
            assert h.tick_with_entries(entries(80, fetched)) is TickOutcome.NO_ACTION
            h.clock.advance(60)
        outcome = h.tick_with_entries(entries(88, h.clock.now))
        assert outcome is TickOutcome.NO_ACTION
        assert h.engine._time_to_limit == pytest.approx(12 * 60 / 2.0)

    def test_the_first_guard_tick_refreshes_the_candidates(self, temp_home):
        # 60% -> 70% in 20 s: far below the escalation band and with no rate
        # from an earlier tick, so only this reading's pace can ask for
        # fresh candidates before the guard picks one.
        h = _harness(temp_home, threshold=99.9)
        h.tick_with_usage({"1": _usage(60), "2": _usage(10), "3": _usage(50)})
        h.clock.advance(20)
        entries = {
            num: UsageEntry(last_good=value, fetched_at=h.clock.now, age_s=0.0)
            for num, value in {"1": _usage(70), "2": _usage(10), "3": _usage(50)}.items()
        }
        with patch.object(h.switcher, "usage_entries_by_account", return_value=entries) as m:
            assert h.engine.tick() is TickOutcome.SWITCHED
        assert any(
            call.kwargs.get("fetch") == {"1", "2", "3"} for call in m.call_args_list
        ), m.call_args_list

    def test_burn_samples_are_timed_by_their_reading(self, temp_home):
        h = _harness(temp_home)
        engine = h.engine
        engine._collected_at = 200.0
        engine._live_now = [{"at": 120.0}, {"at": 150.0}]
        live = {"five_hour": {"pct": 60.0, "live": True}, "seven_day": {"pct": 0.0}}
        api = {"five_hour": {"pct": 60.0}, "seven_day": {"pct": 0.0}}
        entry = UsageEntry(last_good=api, fetched_at=90.0, age_s=110.0)
        assert engine._burn_sample("1", live, entry, 0.0) == (150.0, "1", 60.0, 1.0)
        assert engine._burn_sample("1", api, entry, 0.0)[0] == 90.0
        assert engine._burn_sample("1", api, None, 0.0)[0] == 200.0
        ahead = UsageEntry(last_good=api, fetched_at=250.0, age_s=0.0)
        assert engine._burn_sample("1", api, ahead, 0.0)[0] == 200.0
        # No endpoint number at all (a gate-only reading): this tick, as given.
        assert engine._burn_sample("1", None, entry, 100.0) == (200.0, "1", 100.0, 1.0)

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

    def test_a_lull_does_not_shrink_the_landing_check(self, temp_home):
        # Burning a point a minute now, but four a minute ten minutes ago: #2's
        # 20 points last 5 minutes at the recent pace, not 20.
        h = _harness(temp_home, {"1": "5x", "2": "5x", "3": "5x"}, strategy="consume-first")
        self._burning(h, 50)
        h.engine._recent_burns = [(h.clock.now - 600, 4.0 / 60)]
        outcome = h.tick_with_usage({
            "1": _usage7(50, 20, _R_LATER),
            "2": _usage7(80, 10, _R_SOON),
            "3": _usage7(10, 10, _R_LATER),
        })
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 1

    def test_an_old_burst_stops_counting_after_the_window(self, temp_home):
        h = _harness(temp_home, {"1": "5x", "2": "5x", "3": "5x"}, strategy="consume-first")
        self._burning(h, 50)
        h.engine._recent_burns = [(h.clock.now - LANDING_BURN_WINDOW_S - 60, 4.0 / 60)]
        outcome = h.tick_with_usage({
            "1": _usage7(50, 20, _R_LATER),
            "2": _usage7(80, 10, _R_SOON),
            "3": _usage7(10, 10, _R_LATER),
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2

    def test_idle_active_account_lands_where_there_is_room(self, temp_home):
        h = _harness(temp_home, {"1": "20x", "2": "20x", "3": "20x"}, strategy="consume-first")
        outcome = h.tick_with_usage({
            "1": _usage7(43, 20, _R_LATER),
            "2": _usage7(80, 10, _R_SOON),
            "3": _usage7(10, 10, _R_LATER),
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2
