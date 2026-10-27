"""Tests for weekly reset anchors: maths, the durable store, the planner."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from claude_swap import weekly_anchor as wa


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


ID = ("a@example.com", "org-1")


class TestAnchorMaths:
    def test_rounds_api_jitter_up_to_the_hour(self):
        assert wa.anchor_from_reset("2026-10-10T21:59:59.961841+00:00") == {
            "weekday": 5,
            "hhmm": "22:00",
        }

    def test_rounds_down_just_past(self):
        assert wa.anchor_from_reset("2026-10-15T23:00:00.348769+00:00") == {
            "weekday": 3,
            "hhmm": "23:00",
        }

    def test_sunday_is_six_and_offsets_convert_to_utc(self):
        assert wa.anchor_from_reset("2026-10-11T05:00:00-04:00") == {
            "weekday": 6,
            "hhmm": "09:00",
        }

    def test_garbage_is_none(self):
        assert wa.anchor_from_reset("not a date") is None
        assert wa.anchor_from_reset(None) is None

    def test_minute_distance_is_circular(self):
        sun_2359 = {"weekday": 6, "hhmm": "23:59"}
        mon_0001 = {"weekday": 0, "hhmm": "00:01"}
        assert wa.minute_distance(sun_2359, mon_0001) == 2

    def test_next_occurrence_later_same_day(self):
        anchor = {"weekday": 5, "hhmm": "22:00"}  # Sat
        assert wa.next_occurrence(anchor, _dt("2026-10-10T13:00:00")) == _dt(
            "2026-10-10T22:00:00"
        )

    def test_next_occurrence_wraps_the_week(self):
        anchor = {"weekday": 2, "hhmm": "05:00"}  # Wed
        assert wa.next_occurrence(anchor, _dt("2026-10-10T13:00:00")) == _dt(
            "2026-10-14T05:00:00"
        )

    def test_next_occurrence_exactly_at_moves_a_week(self):
        anchor = {"weekday": 5, "hhmm": "22:00"}
        assert wa.next_occurrence(anchor, _dt("2026-10-10T22:00:00")) == _dt(
            "2026-10-17T22:00:00"
        )


class FakeClock:
    def __init__(self, t: float):
        self.t = t

    def __call__(self) -> float:
        return self.t


X20 = "default_claude_max_20x"
X5 = "default_claude_max_5x"


class TestTiers:
    def test_normalize(self):
        assert wa.normalize_tier(X20) == "max_20x"
        assert wa.normalize_tier(X5) == "max_5x"
        assert wa.normalize_tier(None) == wa.UNKNOWN_TIER
        assert wa.normalize_tier("  ") == wa.UNKNOWN_TIER

    def test_label(self):
        assert wa.tier_label("max_20x") == "20x"
        assert wa.tier_label("max_5x") == "5x"


class TestOffSchedule:
    def _row(self, tmp_path):
        store = wa.WeeklyAnchorStore(tmp_path, clock=FakeClock(_dt("2026-10-10T12:00:00").timestamp()))
        store.observe(ID, "2026-10-15T10:00:00+00:00", slot=24, tier=X5)
        return store.get(ID)

    def test_on_schedule_reading_is_not_suspect(self, tmp_path):
        assert wa.off_schedule(self._row(tmp_path), X5, "2026-10-22T10:00:30+00:00") is False

    def test_reading_off_the_tier_schedule_is_suspect(self, tmp_path):
        assert wa.off_schedule(self._row(tmp_path), X5, "2026-10-17T03:00:00+00:00") is True

    def test_unknown_tier_checks_the_last_known_tier(self, tmp_path):
        assert wa.off_schedule(self._row(tmp_path), None, "2026-10-17T03:00:00+00:00") is True

    def test_a_tier_with_no_schedule_yet_is_not_suspect(self, tmp_path):
        assert wa.off_schedule(self._row(tmp_path), X20, "2026-10-17T03:00:00+00:00") is False
        assert wa.off_schedule(None, X5, "2026-10-17T03:00:00+00:00") is False


class TestStore:
    def _store(self, tmp_path, t="2026-10-10T12:00:00"):
        clock = FakeClock(_dt(t).timestamp())
        return wa.WeeklyAnchorStore(tmp_path, clock=clock), clock

    @staticmethod
    def _blk(store, tier="max_20x"):
        return store.get(ID)["tiers"][tier]

    def test_first_observation_sets_anchor(self, tmp_path):
        store, _ = self._store(tmp_path)
        store.observe(ID, "2026-10-15T23:00:00.3+00:00", slot=14, tier=X20)
        row = store.get(ID)
        assert row["tier"] == "max_20x"
        assert wa.current_anchor(row) == {"weekday": 3, "hhmm": "23:00"}
        assert row["lastSlot"] == 14
        assert self._blk(store)["moved"] is False
        assert len(self._blk(store)["observations"]) == 1

    def test_each_tier_keeps_its_own_schedule(self, tmp_path):
        store, clock = self._store(tmp_path)
        store.observe(ID, "2026-10-15T10:00:00+00:00", slot=24, tier=X5)
        clock.t += 86400
        store.observe(ID, "2026-10-17T03:00:00+00:00", slot=24, tier=X20)  # upgrade
        clock.t += 7 * 86400
        store.observe(ID, "2026-10-22T10:00:00+00:00", slot=24, tier=X5)  # downgrade
        row = store.get(ID)
        assert row["tiers"]["max_5x"]["anchor"] == {"weekday": 3, "hhmm": "10:00"}
        assert row["tiers"]["max_20x"]["anchor"] == {"weekday": 5, "hhmm": "03:00"}
        assert row["tiers"]["max_5x"]["moved"] is False  # 5x schedule came back
        assert row["tier"] == "max_5x"
        assert wa.current_anchor(row) == {"weekday": 3, "hhmm": "10:00"}

    def test_unknown_tier_reading_folds_into_first_known_tier(self, tmp_path):
        store, clock = self._store(tmp_path)
        store.observe(ID, "2026-10-15T23:00:00+00:00", slot=14)
        clock.t += 7 * 86400
        store.observe(ID, "2026-10-22T23:00:00+00:00", slot=14, tier=X20)
        row = store.get(ID)
        assert set(row["tiers"]) == {"max_20x"}
        assert len(row["tiers"]["max_20x"]["observations"]) == 2

    def test_unknown_tier_later_uses_the_last_known_tier(self, tmp_path):
        store, clock = self._store(tmp_path)
        store.observe(ID, "2026-10-15T23:00:00+00:00", slot=14, tier=X20)
        clock.t += 7 * 86400
        store.observe(ID, "2026-10-22T23:00:00+00:00", slot=14)
        assert set(store.get(ID)["tiers"]) == {"max_20x"}

    def test_same_week_reobservation_does_not_append(self, tmp_path):
        store, clock = self._store(tmp_path)
        store.observe(ID, "2026-10-15T23:00:00.3+00:00", slot=14, tier=X20)
        clock.t += 600
        store.observe(ID, "2026-10-15T22:59:59.9+00:00", slot=14, tier=X20)
        assert len(self._blk(store)["observations"]) == 1

    def test_same_week_does_not_rewrite_file_each_poll(self, tmp_path):
        store, clock = self._store(tmp_path)
        store.observe(ID, "2026-10-15T23:00:00+00:00", slot=14, tier=X20)
        before = store.path.stat().st_mtime_ns
        clock.t += 600  # well inside the 6h lastSeen refresh interval
        store.observe(ID, "2026-10-15T23:00:00+00:00", slot=14, tier=X20)
        assert store.path.stat().st_mtime_ns == before

    def test_next_week_appends(self, tmp_path):
        store, clock = self._store(tmp_path)
        store.observe(ID, "2026-10-15T23:00:00+00:00", slot=14, tier=X20)
        clock.t += 7 * 86400
        store.observe(ID, "2026-10-22T23:00:00+00:00", slot=14, tier=X20)
        assert len(self._blk(store)["observations"]) == 2
        assert self._blk(store)["moved"] is False

    def test_shift_beyond_tolerance_marks_moved(self, tmp_path):
        store, clock = self._store(tmp_path)
        store.observe(ID, "2026-10-15T23:00:00+00:00", slot=14, tier=X20)
        clock.t += 7 * 86400
        store.observe(ID, "2026-10-23T10:00:00+00:00", slot=14, tier=X20)
        assert self._blk(store)["moved"] is True
        assert self._blk(store)["anchor"] == {"weekday": 4, "hhmm": "10:00"}

    def test_history_is_capped(self, tmp_path):
        store, clock = self._store(tmp_path)
        start = _dt("2026-10-15T23:00:00").timestamp()
        for week in range(15):
            clock.t += 7 * 86400
            ts = datetime.fromtimestamp(start + week * 7 * 86400, timezone.utc)
            store.observe(ID, ts.isoformat(), slot=14, tier=X20)
        assert len(self._blk(store)["observations"]) == wa.HISTORY_CAP

    def test_mark_removed_keeps_the_anchor(self, tmp_path):
        store, _ = self._store(tmp_path)
        store.observe(ID, "2026-10-15T23:00:00+00:00", slot=14, tier=X20)
        store.mark_removed(ID, slot=14)
        row = store.get(ID)
        assert wa.current_anchor(row) == {"weekday": 3, "hhmm": "23:00"}
        assert row["removedAt"] is not None
        assert row["lastSlot"] == 14

    def test_reobserving_a_removed_identity_clears_removed(self, tmp_path):
        store, clock = self._store(tmp_path)
        store.observe(ID, "2026-10-15T23:00:00+00:00", slot=14, tier=X20)
        store.mark_removed(ID, slot=14)
        clock.t += 7 * 86400
        store.observe(ID, "2026-10-22T23:00:00+00:00", slot=30, tier=X20)
        row = store.get(ID)
        assert row["removedAt"] is None
        assert row["lastSlot"] == 30

    def test_forget(self, tmp_path):
        store, _ = self._store(tmp_path)
        store.observe(ID, "2026-10-15T23:00:00+00:00", slot=14, tier=X20)
        assert store.forget(ID) is True
        assert store.get(ID) is None
        assert store.forget(ID) is False

    def test_note_subscription(self, tmp_path):
        store, _ = self._store(tmp_path)
        store.observe(ID, "2026-10-15T23:00:00+00:00", slot=14, tier=X20)
        store.note_subscription(ID, "active", "2026-10-20T14:00:00+00:00", slot=14)
        row = store.get(ID)
        assert row["subscriptionStatus"] == "active"
        assert row["lastSubscriptionEnd"] == "2026-10-20T14:00:00+00:00"

    def test_merge_unions_history_tiers_and_prefers_newer(self, tmp_path):
        a, _ = self._store(tmp_path / "a")
        b, cb = self._store(tmp_path / "b")
        a.observe(ID, "2026-10-15T23:00:00+00:00", slot=14, tier=X20)
        cb.t += 7 * 86400
        b.observe(ID, "2026-10-22T23:00:00+00:00", slot=14, tier=X20)
        b.observe(ID, "2026-10-22T10:00:00+00:00", slot=14, tier=X5)
        other = ("b@example.com", "org-2")
        b.observe(other, "2026-10-17T03:00:00+00:00", slot=19, tier=X20)
        changed = a.merge_rows(b.rows())
        assert changed == 2
        row = a.get(ID)
        assert len(row["tiers"]["max_20x"]["observations"]) == 2
        assert row["tiers"]["max_5x"]["anchor"] == {"weekday": 3, "hhmm": "10:00"}
        assert row["tier"] == "max_5x"  # b saw it most recently
        assert wa.current_anchor(a.get(other)) == {"weekday": 5, "hhmm": "03:00"}
        assert a.merge_rows(b.rows()) == 0  # idempotent

    def test_merge_ignores_garbage(self, tmp_path):
        a, _ = self._store(tmp_path)
        assert a.merge_rows({"x": "nope", "y": {"anchor": None}}) == 0
        assert a.merge_rows("nope") == 0

    def test_loads_the_seed_file_shape(self, tmp_path):
        seed = {
            "version": 1,
            "identities": {
                "a@example.com|org-1": {
                    "email": "a@example.com",
                    "organizationUuid": "org-1",
                    "tier": "max_20x",
                    "anchor": {"weekday": 3, "hhmm": "23:00"},
                    "observations": [
                        {"resetsAt": "2026-10-15T23:00:00+00:00", "seenAt": "x"}
                    ],
                    "moved": False,
                    "lastSlot": 14,
                    "removedAt": None,
                }
            },
        }
        (tmp_path / "weekly_anchors.json").write_text(json.dumps(seed))
        store, _ = self._store(tmp_path)
        row = store.get(ID)
        assert row["lastSlot"] == 14
        assert row["tiers"]["max_20x"]["anchor"] == {"weekday": 3, "hhmm": "23:00"}

    def test_corrupt_file_reads_as_empty(self, tmp_path):
        (tmp_path / "weekly_anchors.json").write_text("{broken")
        store, _ = self._store(tmp_path)
        assert store.rows() == {}


class TestPlanner:
    # Thu 23:00 UTC anchor; Oct 2026 has 31 days, Nov 30, Feb 2027 28.
    THU = {"weekday": 3, "hhmm": "23:00"}

    def test_windows_for_counts_resets_inside_the_paid_month(self):
        start = _dt("2026-11-11T23:00:00")  # Wed, one day before a Thu reset
        hours = [
            round((b - a).total_seconds() / 3600)
            for a, b in wa.windows_for(start, self.THU)
        ]
        assert hours == [24, 168, 168, 168, 168, 24]  # 30-day period

    def test_plan_recommends_a_day_before_the_reset(self):
        now = _dt("2026-11-09T12:00:00")  # Mon
        plan = wa.resub_plan(self.THU, now)
        assert plan["recommendedAt"] == _dt("2026-11-11T23:00:00")
        assert plan["quotas"] == 6
        assert plan["periodDays"] == 30

    def test_resubbing_right_after_a_reset_gets_five(self):
        now = _dt("2026-11-13T00:00:00")  # 1h after Thu 23:00 reset
        assert wa.resub_plan(self.THU, now)["quotasIfNow"] == 5

    def test_thirty_one_day_month_keeps_a_longer_tail(self):
        start = _dt("2026-10-14T23:00:00")  # Oct 14 → Nov 14 = 31 days
        hours = [
            round((b - a).total_seconds() / 3600)
            for a, b in wa.windows_for(start, self.THU)
        ]
        assert hours[0] == 24 and hours[-1] == 48 and len(hours) == 6

    def test_february_cannot_reach_six(self):
        now = _dt("2027-02-01T00:00:00")
        plan = wa.resub_plan(self.THU, now)
        assert plan["quotas"] <= 6
        start = _dt("2027-02-02T23:00:00")  # Feb 2 → Mar 2 = 28 days
        assert len(wa.windows_for(start, self.THU)) == 5

    def test_recommendation_is_never_in_the_past(self):
        now = _dt("2026-11-12T12:00:00")  # 11h before the Thu reset
        plan = wa.resub_plan(self.THU, now)
        assert plan["recommendedAt"] >= now

    def test_short_edge_windows_do_not_count(self):
        now = _dt("2026-11-12T22:30:00")  # 30 min before the reset
        plan = wa.resub_plan(self.THU, now)
        assert plan["quotasIfNow"] == 5  # the 30-min first window is unusable

    def test_thirty_one_day_month_centres_both_ends(self):
        # Oct 26 → Nov 26 is 31 days: 48h of slack split 36/36 beats 24/48.
        plan = wa.resub_plan(self.THU, _dt("2026-10-26T12:00:00"))
        assert plan["recommendedAt"] == _dt("2026-10-28T11:00:00")
        assert plan["windows"] == [36.0, 168.0, 168.0, 168.0, 168.0, 36.0]
        assert plan["quotas"] == 6

    def test_min_window_decides_what_counts(self):
        start = _dt("2026-11-11T23:00:00")  # 24h edges in a 30-day month
        assert wa.resub_plan(self.THU, start, min_window_h=24)["quotasIfNow"] == 6
        assert wa.resub_plan(self.THU, start, min_window_h=25)["quotasIfNow"] == 4


class TestRenewalPlan:
    THU = {"weekday": 3, "hhmm": "23:00"}

    def test_renewal_right_after_a_reset_is_worth_lapsing(self):
        # Renews 1h after the Thu reset: next month holds five quotas. Buying
        # again ~a day before a reset holds six, for a few unpaid days.
        plan = wa.renewal_plan(self.THU, _dt("2026-11-13T00:00:00"))
        assert plan["renewQuotas"] == 5
        assert plan["resubQuotas"] == 6
        assert plan["lapse"] is True
        assert plan["resubAt"] > plan["renewsAt"]

    def test_well_placed_renewal_keeps_renewing(self):
        plan = wa.renewal_plan(self.THU, _dt("2026-11-11T23:00:00"))
        assert plan["renewQuotas"] == 6
        assert plan["lapse"] is False

    def test_a_long_gap_that_loses_per_day_is_not_advised(self):
        # 5 quotas per 30 days renewing vs 6 per (gap + 30): lapse only when
        # the gap is under ~6 days.
        plan = wa.renewal_plan(self.THU, _dt("2026-11-13T00:00:00"))
        per_day_keep = plan["renewQuotas"] / 30
        assert plan["resubQuotas"] / (plan["gapDays"] + 30) >= per_day_keep


class TestUpgradePlan:
    NOW = _dt("2026-11-09T12:00:00")

    def _plan(self, used, reset_in_h, period_end=None):
        return wa.upgrade_plan(
            now=self.NOW,
            weekly_used_pct=used,
            weekly_reset=self.NOW + timedelta(hours=reset_in_h),
            period_end=period_end,
        )

    def test_burn_the_5x_week_first(self):
        assert self._plan(60, 72)["action"] == "burn"

    def test_spent_week_with_time_left_upgrades_now(self):
        plan = self._plan(99, 72)
        assert plan["action"] == "now" and plan["at"] == self.NOW

    def test_spent_too_close_to_the_reset_waits_for_the_next_week(self):
        plan = self._plan(100, 6)
        assert plan["action"] == "after-reset"
        assert plan["at"] == self.NOW + timedelta(hours=6 + wa.MIN_WINDOW_H)

    def test_period_ending_first_skips(self):
        plan = self._plan(100, 72, period_end=self.NOW + timedelta(hours=10))
        assert plan["action"] == "skip"

    def test_unknown_usage(self):
        assert wa.upgrade_plan(now=self.NOW, weekly_used_pct=None, weekly_reset=None)["action"] == "unknown"


class TestViews:
    def test_view_and_text_show_current_tier_and_others(self, tmp_path):
        store = wa.WeeklyAnchorStore(tmp_path, clock=FakeClock(_dt("2026-10-10T12:00:00").timestamp()))
        store.observe(ID, "2026-10-15T10:00:00+00:00", slot=24, tier=X5)
        store.clock.t += 86400
        store.observe(ID, "2026-10-17T03:00:00+00:00", slot=24, tier=X20)
        view = wa.weekly_view(store.get(ID), _dt("2026-10-12T00:00:00"))
        assert view["tier"] == "max_20x"
        assert view["nextAt"] == "2026-10-17T03:00:00+00:00"
        assert set(view["tiers"]) == {"max_5x", "max_20x"}
        text = wa.weekly_text(view)
        assert text.startswith("Sat 03:00 UTC")
        assert "· 20x" in text
        assert "[5x: Thu 10:00 UTC]" in text

    def test_no_row_no_view(self):
        assert wa.weekly_view(None) is None
        assert wa.weekly_text(None) is None
