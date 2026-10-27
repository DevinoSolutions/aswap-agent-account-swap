"""A cached usage reading must not present an already-reset window as current."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from claude_swap import oauth
from claude_swap.switcher import _format_usage_lines

NOW = datetime(2026, 10, 26, 18, 0, tzinfo=timezone.utc)


def _iso(dt):
    return dt.isoformat()


class TestRollPassedWindows:
    def test_passed_windows_read_zero_and_remember_the_old_value(self):
        usage = {
            "five_hour": {"pct": 96.0, "resets_at": _iso(NOW - timedelta(hours=2)), "countdown": "0m"},
            "seven_day": {"pct": 99.0, "resets_at": _iso(NOW - timedelta(hours=3))},
            "scoped": [{"name": "Fable", "pct": 40.0, "resets_at": _iso(NOW - timedelta(days=8))}],
        }
        out = oauth.roll_passed_windows(usage, NOW.timestamp())
        five, seven, fable = out["five_hour"], out["seven_day"], out["scoped"][0]
        assert (five["pct"], five["wasPct"], "resets_at" in five, "countdown" in five) == (0.0, 96.0, False, False)
        assert five["resetAgoS"] == 2 * 3600
        # Weekly windows move on to their next 7-day boundary.
        assert seven["pct"] == 0.0
        assert datetime.fromisoformat(seven["resets_at"]) == NOW - timedelta(hours=3) + timedelta(days=7)
        assert datetime.fromisoformat(fable["resets_at"]) == NOW - timedelta(days=8) + timedelta(days=14)
        # The input is untouched.
        assert usage["five_hour"]["pct"] == 96.0

    def test_future_and_unknown_windows_are_left_alone(self):
        usage = {
            "five_hour": {"pct": 50.0, "resets_at": _iso(NOW + timedelta(hours=1))},
            "seven_day": {"pct": 20.0},
        }
        assert oauth.roll_passed_windows(usage, NOW.timestamp()) == usage


def test_list_lines_say_the_window_reset(monkeypatch):
    monkeypatch.setattr("claude_swap.switcher.time.time", lambda: NOW.timestamp())
    usage = {"five_hour": {"pct": 96.0, "resets_at": _iso(NOW - timedelta(hours=2, minutes=5))}}
    (line,) = _format_usage_lines(usage)
    assert "0%" in line and "reset 2h 5m ago, was 96%" in line
