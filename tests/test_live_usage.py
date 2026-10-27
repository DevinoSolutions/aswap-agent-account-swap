"""Live statusline usage: parsing, attribution, merge, and the engine overlay."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from claude_swap import live_usage as lu
from claude_swap.autoswitch import LIVE_TICK_S, TickOutcome

from tests.test_autoswitch import EngineHarness, _entry_for, _iso_at

NOW = 1_000_000.0
WEEK_A = NOW + 3 * 86400  # account 1's weekly reset
WEEK_B = NOW + 5 * 86400  # account 2's
WEEK_C = NOW + 6 * 86400  # account 3's


def _limits(p5: float, r5: float | None, p7: float, r7: float | None) -> dict:
    return {
        "five_hour": {"used_percentage": p5, "resets_at": r5},
        "seven_day": {"used_percentage": p7, "resets_at": r7},
    }


class TestReadReadings:
    def test_parses_fresh_files_and_skips_stale_and_garbage(self, tmp_path: Path):
        lu.write_reading(tmp_path, "fresh", _limits(40, NOW + 3600, 10, WEEK_A), now=NOW - 5)
        lu.write_reading(tmp_path, "idle", _limits(1, NOW + 3600, 1, WEEK_A), now=NOW - lu.FRESH_S - 1)
        (lu.live_dir(tmp_path) / "junk.json").write_text("{not json")
        readings = lu.read_readings(tmp_path, NOW)
        assert [r["session"] for r in readings] == ["fresh"]
        assert readings[0]["five_hour"] == {"pct": 40.0, "resets_ts": NOW + 3600}

    def test_prunes_files_from_ended_sessions(self, tmp_path: Path):
        path = lu.write_reading(tmp_path, "old", _limits(1, None, 1, None), now=NOW - lu.PRUNE_S - 1)
        assert lu.read_readings(tmp_path, NOW) == []
        assert not path.exists()

    def test_missing_directory_is_empty(self, tmp_path: Path):
        assert lu.read_readings(tmp_path / "nope", NOW) == []

    @pytest.mark.parametrize(
        "value, expected",
        [(1_790_000_000, 1_790_000_000.0), (1_790_000_000_000, 1_790_000_000.0),
         ("2026-10-15T10:00:00Z", 1_792_058_400.0), ("bad", None), (True, None), (None, None)],
    )
    def test_to_ts(self, value, expected):
        assert lu.to_ts(value) == expected


class TestAttribute:
    def _r(self, week):
        return {"at": NOW, "seven_day": {"pct": 1.0, "resets_ts": week}}

    def test_matches_the_account_by_weekly_reset_modulo_a_week(self):
        weekly = {"1": WEEK_A, "2": WEEK_B}
        mine = self._r(WEEK_A - 7 * 86400 + 30)  # an earlier occurrence, rounded
        theirs = self._r(WEEK_B)
        assert lu.attribute([mine, theirs], weekly, "1") == [mine]

    def test_ambiguous_anchor_is_dropped(self):
        weekly = {"1": WEEK_A, "2": WEEK_A + 60}
        assert lu.attribute([self._r(WEEK_A)], weekly, "1") == []

    def test_unknown_active_weekly_reset_attributes_nothing(self):
        assert lu.attribute([self._r(WEEK_A)], {"1": None, "2": WEEK_B}, "1") == []

    def test_reading_without_a_weekly_reset_is_dropped(self):
        reading = {"at": NOW, "five_hour": {"pct": 90.0, "resets_ts": NOW + 60}}
        assert lu.attribute([reading], {"1": WEEK_A}, "1") == []


class TestMerge:
    def _live(self, p5, r5, p7=10.0, r7=WEEK_A):
        return {"at": NOW, "five_hour": {"pct": p5, "resets_ts": r5}, "seven_day": {"pct": p7, "resets_ts": r7}}

    def test_same_window_takes_the_higher_utilization(self):
        stored = {"five_hour": {"pct": 40.0, "resets_at": _iso_at(NOW + 3600)},
                  "seven_day": {"pct": 10.0, "resets_at": _iso_at(WEEK_A)}, "scoped": [{"name": "Fable", "pct": 3.0}]}
        merged, changed = lu.merge(stored, [self._live(85.0, NOW + 3600)], NOW)
        assert changed
        assert merged["five_hour"]["pct"] == 85.0
        assert merged["five_hour"]["live"] is True
        assert merged["seven_day"]["pct"] == 10.0  # not higher: unchanged
        assert merged["scoped"] == stored["scoped"]

    def test_lower_live_value_in_the_same_window_never_lowers(self):
        stored = {"five_hour": {"pct": 60.0, "resets_at": _iso_at(NOW + 3600)}}
        merged, changed = lu.merge(stored, [self._live(30.0, NOW + 3600)], NOW)
        assert merged["five_hour"]["pct"] == 60.0

    def test_older_instance_from_an_idle_session_is_ignored(self):
        stored = {"five_hour": {"pct": 5.0, "resets_at": _iso_at(NOW + 4 * 3600)}}
        merged, _ = lu.merge(stored, [self._live(99.0, NOW + 600)], NOW)
        assert merged["five_hour"]["pct"] == 5.0

    def test_newer_instance_replaces_a_stored_window_that_has_reset(self):
        stored = {"five_hour": {"pct": 97.0, "resets_at": _iso_at(NOW - 60)}}
        merged, changed = lu.merge(stored, [self._live(3.0, NOW + 5 * 3600)], NOW)
        assert changed and merged["five_hour"]["pct"] == 3.0

    def test_live_window_past_its_reset_carries_nothing(self):
        merged, changed = lu.merge(None, [self._live(99.0, NOW - 1, 10.0, NOW - 1)], NOW)
        assert (merged, changed) == (None, False)

    def test_among_readings_the_newest_instance_then_highest_pct_wins(self):
        readings = [self._live(90.0, NOW + 600), self._live(4.0, NOW + 5 * 3600), self._live(7.0, NOW + 5 * 3600)]
        merged, _ = lu.merge(None, readings, NOW)
        assert merged["five_hour"]["pct"] == 7.0

    def test_no_stored_value_takes_live(self):
        merged, changed = lu.merge(None, [self._live(50.0, NOW + 600)], NOW)
        assert changed and merged["five_hour"]["pct"] == 50.0 and merged["seven_day"]["pct"] == 10.0


@pytest.fixture
def harness(temp_home: Path) -> EngineHarness:
    h = EngineHarness(temp_home)  # threshold 90, strategy best
    h.seed(1, "a@example.com")
    h.seed(2, "b@example.com")
    h.make_live("a@example.com", 1)
    return h


def _stored(p5: float, week: float) -> dict:
    return {"five_hour": {"pct": p5, "resets_at": _iso_at(NOW + 3600)},
            "seven_day": {"pct": 10.0, "resets_at": _iso_at(week)}}


class TestEngineOverlay:
    def test_live_burn_past_the_threshold_switches_before_the_next_poll(self, harness):
        lu.write_reading(harness.switcher.backup_dir, "s1", _limits(95, NOW + 3600, 10, WEEK_A), now=NOW)
        outcome = harness.tick_with_usage({"1": _stored(40.0, WEEK_A), "2": _stored(5.0, WEEK_B)})
        assert outcome is TickOutcome.SWITCHED
        assert harness.active_number() == 2
        poll = next(e for e in harness.events if e.kind == "poll")
        assert poll.windows["1"]["5h"] == pytest.approx(95.0)

    def test_without_a_statusline_nothing_changes(self, harness):
        outcome = harness.tick_with_usage({"1": _stored(40.0, WEEK_A), "2": _stored(5.0, WEEK_B)})
        assert outcome is not TickOutcome.SWITCHED
        assert harness.active_number() == 1

    def test_a_reading_from_the_previous_account_is_not_applied(self, harness):
        # an idle session still reporting account 2 after the switch to 1
        lu.write_reading(harness.switcher.backup_dir, "old", _limits(99, NOW + 3600, 10, WEEK_B), now=NOW)
        outcome = harness.tick_with_usage({"1": _stored(40.0, WEEK_A), "2": _stored(5.0, WEEK_B)})
        assert outcome is not TickOutcome.SWITCHED
        assert harness.active_number() == 1

    def test_live_coverage_shortens_the_tick_and_skips_urgent_polling(self, harness):
        lu.write_reading(harness.switcher.backup_dir, "s1", _limits(50, NOW + 3600, 10, WEEK_A), now=NOW)
        entries = {"1": _entry_for(_stored(40.0, WEEK_A), NOW), "2": _entry_for(_stored(5.0, WEEK_B), NOW)}
        calls = []

        def fake(fetch=frozenset(), **kw):
            calls.append(set(fetch))
            return entries

        from unittest.mock import patch

        with patch.object(harness.switcher, "usage_entries_by_account", side_effect=fake):
            outcome = harness.engine.tick()
            delay = harness.engine._next_delay(outcome)
        assert delay <= LIVE_TICK_S
        assert all("1" not in fetched for fetched in calls)

    def test_weekly_anchor_identifies_the_account_when_usage_lacks_it(self, harness):
        lu.write_reading(harness.switcher.backup_dir, "s1", _limits(95, NOW + 3600, 10, WEEK_A), now=NOW)
        from unittest.mock import patch

        with patch.object(harness.switcher, "perish_inputs", return_value={"1": {"anchorNext": WEEK_A, "endsAt": None}}):
            outcome = harness.tick_with_usage({"1": {"five_hour": {"pct": 40.0}}, "2": _stored(5.0, WEEK_B)})
        assert outcome is TickOutcome.SWITCHED


class TestCompare:
    def test_same_window_closest_reading_is_compared(self):
        api = {"five_hour": {"pct": 52.0, "resets_at": _iso_at(NOW + 3600)},
               "seven_day": {"pct": 11.0, "resets_at": _iso_at(WEEK_A)}}
        readings = [
            {"at": NOW - 80, "five_hour": {"pct": 40.0, "resets_ts": NOW + 3600}},
            {"at": NOW - 10, "five_hour": {"pct": 50.0, "resets_ts": NOW + 3600},
             "seven_day": {"pct": 11.0, "resets_ts": WEEK_A}},
        ]
        rows = {r["window"]: r for r in lu.compare(api, readings, NOW)}
        assert rows["five_hour"]["live"] == 50.0 and rows["five_hour"]["diff"] == 2.0
        assert rows["five_hour"]["skew_s"] == 10.0
        assert rows["seven_day"]["diff"] == 0.0

    def test_other_instances_and_distant_readings_are_skipped(self):
        api = {"five_hour": {"pct": 5.0, "resets_at": _iso_at(NOW + 5 * 3600)}}
        readings = [
            {"at": NOW, "five_hour": {"pct": 90.0, "resets_ts": NOW + 600}},
            {"at": NOW - lu.COMPARE_WINDOW_S - 1, "five_hour": {"pct": 4.0, "resets_ts": NOW + 5 * 3600}},
        ]
        assert lu.compare(api, readings, NOW) == []


class TestLiveCheckLog:
    def _tick(self, harness, before_fetched, api):
        from unittest.mock import patch
        before = {"1": _entry_for(_stored(40.0, WEEK_A), before_fetched), "2": _entry_for(_stored(5.0, WEEK_B), NOW)}
        after = dict(before, **{"1": _entry_for(api, NOW)})
        calls = iter([before, after, after, after])
        with patch.object(harness.switcher, "usage_entries_by_account", side_effect=lambda **kw: next(calls)):
            harness.engine.tick()

    def test_agreement_logs_info(self, harness, caplog):
        lu.write_reading(harness.switcher.backup_dir, "s1", _limits(41, NOW + 3600, 10, WEEK_A), now=NOW - 5)
        with caplog.at_level("INFO", logger="claude-swap"):
            self._tick(harness, NOW - 400, _stored(42.0, WEEK_A))
        lines = [r for r in caplog.records if "live-check" in r.getMessage()]
        assert len(lines) == 1 and lines[0].levelname == "INFO"
        assert "five_hour api=42 live=41 diff=+1" in lines[0].getMessage()

    def test_disagreement_logs_a_warning(self, harness, caplog):
        lu.write_reading(harness.switcher.backup_dir, "s1", _limits(20, NOW + 3600, 10, WEEK_A), now=NOW - 5)
        with caplog.at_level("INFO", logger="claude-swap"):
            self._tick(harness, NOW - 400, _stored(42.0, WEEK_A))
        lines = [r for r in caplog.records if "live-check" in r.getMessage()]
        assert len(lines) == 1 and lines[0].levelname == "WARNING"

    def test_no_fresh_poll_logs_nothing(self, harness, caplog):
        lu.write_reading(harness.switcher.backup_dir, "s1", _limits(41, NOW + 3600, 10, WEEK_A), now=NOW - 5)
        with caplog.at_level("INFO", logger="claude-swap"):
            harness.tick_with_usage({"1": _stored(40.0, WEEK_A), "2": _stored(5.0, WEEK_B)})
        assert not [r for r in caplog.records if "live-check" in r.getMessage()]

    def test_unattributed_same_week_reading_is_flagged(self, harness, caplog):
        # account 2 shares account 1's anchor: attribution refuses, the check flags it
        from unittest.mock import patch
        lu.write_reading(harness.switcher.backup_dir, "s1", _limits(41, NOW + 3600, 10, WEEK_A), now=NOW - 5)
        before = {"1": _entry_for(_stored(40.0, WEEK_A), NOW - 400), "2": _entry_for(_stored(5.0, WEEK_A + 60), NOW)}
        after = dict(before, **{"1": _entry_for(_stored(42.0, WEEK_A), NOW)})
        calls = iter([before, after, after, after])
        with caplog.at_level("INFO", logger="claude-swap"), patch.object(
            harness.switcher, "usage_entries_by_account", side_effect=lambda **kw: next(calls)
        ):
            harness.engine.tick()
        msgs = [r.getMessage() for r in caplog.records if "live-check" in r.getMessage()]
        assert msgs and "not attributed" in msgs[0]
