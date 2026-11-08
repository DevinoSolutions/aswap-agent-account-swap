"""``priority_order``: every account in auto's switch preference (``ui.listOrder priority``)."""

from __future__ import annotations

from claude_swap.autoswitch import priority_order

NOW = 1_000_000.0
DAY = 86400.0


def _order(headroom, *, consume_first=True, perish=None, priority=(), unusable=(), caps=None):
    nums = list(headroom)
    return priority_order(
        nums,
        consume_first=consume_first,
        usage={n: None for n in nums},
        headroom=headroom,
        perish=perish or {},
        caps=caps or {},
        priority=list(priority),
        threshold=90.0,
        unusable=set(unusable),
        now=NOW,
    )


class TestConsumeFirst:
    def test_soonest_lost_quota_first_and_a_cancellation_beats_its_reset(self):
        perish = {
            "1": {"anchorNext": NOW + 5 * DAY},
            "2": {"anchorNext": NOW + 2 * DAY},
            # Resets in 6 days, but its plan ends tomorrow: that quota goes first.
            "3": {"anchorNext": NOW + 6 * DAY, "endsAt": NOW + 1 * DAY},
        }
        assert _order({"1": 80.0, "2": 80.0, "3": 80.0}, perish=perish) == ["3", "2", "1"]

    def test_unknown_reset_sorts_after_known_and_headroom_breaks_ties(self):
        perish = {"1": {"anchorNext": NOW + DAY}, "2": {"anchorNext": NOW + DAY}}
        assert _order({"9": 90.0, "1": 30.0, "2": 70.0}, perish=perish) == ["2", "1", "9"]


class TestBest:
    def test_most_headroom_first_scaled_by_tier(self):
        # 40% of a 4x account is more work than 60% of a 1x one.
        assert _order({"1": 60.0, "2": 40.0, "3": 20.0}, consume_first=False,
                      caps={"2": 4.0}) == ["2", "1", "3"]


class TestTiers:
    def test_priority_first_then_healthy_then_over_threshold_limit_unknown_unusable(self):
        headroom = {
            "1": None,   # unreadable
            "2": 0.0,    # at its limit
            "3": 5.0,    # over the 90% threshold
            "4": 1.0,    # last point: an escape only
            "5": 50.0,
            "6": 60.0,   # priority
            "7": 90.0,   # disabled
        }
        # 4 perishes soonest, yet ranks below 5: a last point is not real room.
        # Within that tier the strategy still decides, so it leads 3.
        perish = {"4": {"endsAt": NOW + 3600}}
        order = _order(headroom, perish=perish, priority=["6"], unusable=["7"])
        assert order == ["6", "5", "4", "3", "2", "1", "7"]

    def test_priority_account_without_room_is_not_first(self):
        assert _order({"1": 0.0, "2": 40.0}, priority=["1"]) == ["2", "1"]

    def test_slot_order_breaks_full_ties(self):
        assert _order({"3": 50.0, "1": 50.0, "2": 50.0}) == ["3", "1", "2"]
