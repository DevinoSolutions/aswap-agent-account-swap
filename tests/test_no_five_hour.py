"""Plans with no five-hour window: only the weekly limit applies.

Measured 2026-10-31: personal Max accounts (5x and 20x alike) started
answering the usage endpoint with ``"five_hour": null`` beside a normal
``seven_day``, and their rate-limit headers carry no ``5h-*`` entries at all
(``representative-claim: seven_day``). A Team seat on the same day still had
its 5-hour window, and a 5h-limited account whose window has not started
reads ``{"utilization": 0, "resets_at": null}``, never null. So an explicit
null means "no 5-hour limit", not "cold".
"""

from __future__ import annotations

from claude_swap import oauth
from claude_swap.json_output import usage_to_json
from claude_swap.switcher import _format_usage_lines

from tests.test_warmup import _account, _engine

WEEKLY = {"utilization": 40.0, "resets_at": "2026-11-05T23:00:00+00:00"}
NO_5H = {"seven_day": {"pct": 40.0}, "no_five_hour": True}


class TestUsageResult:
    def test_an_explicit_null_five_hour_is_a_plan_without_one(self):
        usage = oauth.build_usage_result({"five_hour": None, "seven_day": WEEKLY})
        assert usage["no_five_hour"] is True
        assert "five_hour" not in usage
        assert oauth.has_no_five_hour(usage)

    def test_a_cold_five_hour_window_is_still_a_window(self):
        usage = oauth.build_usage_result({
            "five_hour": {"utilization": 0.0, "resets_at": None},
            "seven_day": WEEKLY,
        })
        assert "no_five_hour" not in usage
        assert not oauth.has_no_five_hour(usage)

    def test_a_response_without_the_key_says_nothing(self):
        usage = oauth.build_usage_result({"seven_day": WEEKLY})
        assert "no_five_hour" not in usage

    def test_a_null_five_hour_with_no_other_window_says_nothing(self):
        assert oauth.build_usage_result({"five_hour": None, "seven_day": None}) is None

    def test_a_five_hour_window_wins_over_a_stale_flag(self):
        assert not oauth.has_no_five_hour(
            {"no_five_hour": True, "five_hour": {"pct": 3.0}}
        )


class TestWarmup:
    def test_an_account_without_a_five_hour_window_is_never_warmed(self, tmp_path):
        engine, _switcher, sessions, events = _engine(tmp_path, [_account("1", NO_5H)])

        summary = engine.tick()

        assert summary.warmed == 0
        assert sessions.calls == []
        assert events[0].kind == "no-five-hour"

    def test_a_stale_reading_without_one_is_not_refetched_or_warmed(self, tmp_path):
        account = _account("1", NO_5H, age_s=600, last_error="http-429")
        engine, switcher, sessions, events = _engine(tmp_path, [account])

        engine.tick()

        assert switcher.snapshot_calls == 1
        assert sessions.calls == []
        assert events[0].kind == "no-five-hour"


class TestDisplay:
    def test_list_says_there_is_no_five_hour_limit(self):
        lines = _format_usage_lines(NO_5H)
        assert any("5h" in line and "none" in line for line in lines)

    def test_list_shows_nothing_extra_for_a_five_hour_plan(self):
        lines = _format_usage_lines({"seven_day": {"pct": 40.0}})
        assert not any("none" in line for line in lines)

    def test_json_marks_the_plan(self):
        out = usage_to_json(NO_5H)
        assert out["noFiveHour"] is True
        assert "fiveHour" not in out
        assert "noFiveHour" not in usage_to_json({"seven_day": {"pct": 40.0}})
