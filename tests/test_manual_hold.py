"""A hand-picked account is left alone by the optional auto moves.

Measured 2026-10-27: the user switched to account 3 by hand at 11:06 and at
11:10 `aswap auto` made a consume-first move 3 -> 13 (73% used -> 85% used),
overriding the choice. After a switch made outside the engine, the optional
strategies hold off for ``autoswitch.manualHoldMinutes``; the moves that
protect the session (at-limit, over-threshold / limit-guard, failover) still
happen.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from claude_swap.autoswitch import (
    HOLD_NOTE_INTERVAL_S,
    OPTIONAL_TRIGGERS,
    NoSwitchEvent,
    SwitchEvent,
    TickOutcome,
    manual_hold_until,
)
from claude_swap.settings import AutoSwitchSettings, load_settings, set_setting

from tests.test_autoswitch import EngineHarness, _R_LATER, _R_LATEST, _R_SOON, _usage7


def _harness(temp_home: Path, **settings) -> EngineHarness:
    settings.setdefault("strategy", "consume-first")
    settings.setdefault("cooldown_seconds", 0)
    h = EngineHarness(temp_home, **settings)
    h.seed(1, "a@example.com")
    h.seed(2, "b@example.com")
    h.seed(3, "c@example.com")
    h.make_live("a@example.com", 1)
    return h


def _current(h: EngineHarness) -> str | None:
    return h.switcher.current_account_number()


# Account 2 resets soonest, so consume-first wants to move to it from 1 or 3.
_WANTS_2_FROM_3 = {
    "1": _usage7(10, 10, _R_LATEST),
    "2": _usage7(10, 10, _R_SOON),
    "3": _usage7(20, 20, _R_LATER),
}
# Nothing to gain anywhere: the active (1) already resets soonest.
_SETTLED_ON_1 = {
    "1": _usage7(20, 20, _R_SOON),
    "2": _usage7(10, 10, _R_LATER),
    "3": _usage7(10, 10, _R_LATEST),
}


def _switch_by_hand(h: EngineHarness, num: int, email: str) -> None:
    """What `aswap switch N`, the TUI or an in-app `/login` leave behind: the
    live login changed and the engine did not do it."""
    h.make_live(email, num)


def _reasons(h: EngineHarness) -> list[str]:
    return [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]


class TestClassification:
    def test_only_consume_first_is_optional(self):
        """Every other trigger protects the session: at-limit (account at its
        limit), proactive (over the threshold, or the limit guard projecting
        100% within minutes) and failover (usage unreadable)."""
        assert OPTIONAL_TRIGGERS == frozenset({"consume-first"})


class TestManualHold:
    def _manual_switch_to_3(self, h: EngineHarness) -> None:
        assert h.tick_with_usage(_SETTLED_ON_1) is TickOutcome.NO_ACTION
        _switch_by_hand(h, 3, "c@example.com")

    def test_consume_first_does_not_override_a_manual_switch(self, temp_home, caplog):
        h = _harness(temp_home)
        self._manual_switch_to_3(h)
        h.events.clear()

        with caplog.at_level(logging.INFO, logger="claude-swap"):
            outcome = h.tick_with_usage(_WANTS_2_FROM_3)

        assert outcome is TickOutcome.NO_ACTION
        assert _current(h) == "3"
        assert "manual-hold" in _reasons(h)
        assert not any(isinstance(e, SwitchEvent) for e in h.events)
        assert (
            "Manual switch to 3; holding it for 60 min unless it nears its limit"
            in caplog.text
        )

    def test_the_hold_is_reported_with_its_end(self, temp_home):
        h = _harness(temp_home)
        self._manual_switch_to_3(h)
        started = h.clock.now
        h.tick_with_usage(_WANTS_2_FROM_3)

        held = h.engine.manual_hold
        assert held is not None
        account, until = held
        assert account == "3"
        assert until == pytest.approx(started + 60 * 60)
        detail = next(
            e.detail for e in h.events
            if isinstance(e, NoSwitchEvent) and e.reason == "manual-hold"
        )
        assert "held until" in detail

    def test_the_hold_ends_after_the_configured_minutes(self, temp_home):
        h = _harness(temp_home, manual_hold_minutes=30)
        self._manual_switch_to_3(h)
        assert h.tick_with_usage(_WANTS_2_FROM_3) is TickOutcome.NO_ACTION

        h.clock.advance(31 * 60)
        outcome = h.tick_with_usage(_WANTS_2_FROM_3)

        assert outcome is TickOutcome.SWITCHED
        assert _current(h) == "2"
        assert h.engine.manual_hold is None

    def test_zero_minutes_disables_the_hold(self, temp_home):
        h = _harness(temp_home, manual_hold_minutes=0)
        self._manual_switch_to_3(h)

        assert h.tick_with_usage(_WANTS_2_FROM_3) is TickOutcome.SWITCHED
        assert _current(h) == "2"

    def test_at_limit_still_escapes_during_the_hold(self, temp_home):
        h = _harness(temp_home)
        self._manual_switch_to_3(h)
        h.tick_with_usage(_WANTS_2_FROM_3)
        h.events.clear()

        outcome = h.tick_with_usage({
            "1": _usage7(10, 10, _R_LATEST),
            "2": _usage7(10, 10, _R_SOON),
            "3": _usage7(100, 40, _R_LATER),
        })

        assert outcome is TickOutcome.SWITCHED
        sw = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert sw.trigger == "at-limit"

    def test_crossing_the_threshold_still_moves_during_the_hold(self, temp_home):
        """The threshold is the user's own definition of "nearing the limit"."""
        h = _harness(temp_home, strategy="best")
        self._manual_switch_to_3(h)
        h.events.clear()

        outcome = h.tick_with_usage({
            "1": _usage7(10, 10, _R_LATEST),
            "2": _usage7(10, 10, _R_SOON),
            "3": _usage7(95, 40, _R_LATER),
        })

        assert outcome is TickOutcome.SWITCHED
        sw = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert sw.trigger == "proactive"

    def test_an_auto_switch_ends_the_hold(self, temp_home):
        h = _harness(temp_home)
        self._manual_switch_to_3(h)
        h.tick_with_usage(_WANTS_2_FROM_3)
        h.tick_with_usage({
            "1": _usage7(10, 10, _R_LATEST),
            "2": _usage7(10, 10, _R_SOON),
            "3": _usage7(100, 40, _R_LATER),
        })
        assert _current(h) == "2"
        assert "manualHold" not in h.state()
        h.events.clear()

        # Back on the engine's own landing: consume-first is free again.
        outcome = h.tick_with_usage({
            "1": _usage7(10, 10, "2024-01-04T00:00:00Z"),
            "2": _usage7(10, 10, _R_SOON),
            "3": _usage7(10, 10, _R_LATEST),
        })
        assert outcome is TickOutcome.SWITCHED
        assert _current(h) == "1"

    def test_a_new_manual_switch_restarts_the_hold(self, temp_home, caplog):
        h = _harness(temp_home)
        self._manual_switch_to_3(h)
        h.tick_with_usage(_WANTS_2_FROM_3)

        h.clock.advance(50 * 60)
        _switch_by_hand(h, 1, "a@example.com")
        restarted = h.clock.now
        with caplog.at_level(logging.INFO, logger="claude-swap"):
            h.tick_with_usage({
                "1": _usage7(20, 20, _R_LATER),
                "2": _usage7(10, 10, _R_SOON),
                "3": _usage7(10, 10, _R_LATEST),
            })
        assert "Manual switch to 1;" in caplog.text
        assert h.engine.manual_hold == ("1", pytest.approx(restarted + 3600))

        # 70 minutes after the FIRST hand switch, 20 after the second: held.
        h.clock.advance(20 * 60)
        outcome = h.tick_with_usage({
            "1": _usage7(20, 20, _R_LATER),
            "2": _usage7(10, 10, _R_SOON),
            "3": _usage7(10, 10, _R_LATEST),
        })
        assert outcome is TickOutcome.NO_ACTION
        assert _current(h) == "1"

    def test_the_hold_survives_an_engine_restart(self, temp_home, caplog):
        h = _harness(temp_home)
        self._manual_switch_to_3(h)
        h.tick_with_usage(_WANTS_2_FROM_3)

        h.engine = h._make_engine()  # `aswap auto` restarted
        h.events.clear()
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="claude-swap"):
            outcome = h.tick_with_usage(_WANTS_2_FROM_3)

        assert outcome is TickOutcome.NO_ACTION
        assert "manual-hold" in _reasons(h)
        # The restart did not mistake the old hand switch for a new one.
        assert "Manual switch to" not in caplog.text

    def test_a_switch_seen_only_in_state_starts_a_hold(self, temp_home):
        """`aswap auto` was not running when the user switched: the engine's
        last landing (state file) disagrees with the live login on start."""
        h = _harness(temp_home)
        h.tick_with_usage({
            "1": _usage7(20, 20, _R_LATER),
            "2": _usage7(10, 10, _R_SOON),
            "3": _usage7(10, 10, _R_LATEST),
        })
        assert _current(h) == "2"  # the engine's own move

        _switch_by_hand(h, 3, "c@example.com")
        h.engine = h._make_engine()
        outcome = h.tick_with_usage(_WANTS_2_FROM_3)

        assert outcome is TickOutcome.NO_ACTION
        assert _current(h) == "3"

    def test_cron_once_runs_notice_a_hand_switch(self, temp_home):
        """`aswap auto --once` from cron is a fresh engine every tick, with no
        engine landing on record yet: the state file still carries the
        account the last run saw."""
        h = _harness(temp_home)
        assert h.tick_with_usage(_SETTLED_ON_1) is TickOutcome.NO_ACTION

        _switch_by_hand(h, 3, "c@example.com")
        h.engine = h._make_engine()
        outcome = h.tick_with_usage(_WANTS_2_FROM_3)

        assert outcome is TickOutcome.NO_ACTION
        assert "manual-hold" in _reasons(h)

    def test_a_sibling_engine_does_not_restart_or_relog_the_hold(
        self, temp_home, caplog
    ):
        h = _harness(temp_home)
        sibling = h._make_engine()
        self._manual_switch_to_3(h)
        h.tick_with_usage(_WANTS_2_FROM_3)
        since = h.state()["manualHold"]["since"]
        caplog.clear()

        h.clock.advance(60)
        h.engine, original = sibling, h.engine
        with caplog.at_level(logging.INFO, logger="claude-swap"):
            outcome = h.tick_with_usage(_WANTS_2_FROM_3)
        h.engine = original

        assert outcome is TickOutcome.NO_ACTION
        assert h.state()["manualHold"]["since"] == since
        assert "Manual switch to" not in caplog.text

    def test_suppression_is_logged_once_not_every_poll(self, temp_home, caplog):
        h = _harness(temp_home)
        self._manual_switch_to_3(h)
        with caplog.at_level(logging.INFO, logger="claude-swap"):
            for _ in range(5):
                h.tick_with_usage(_WANTS_2_FROM_3)
                h.clock.advance(60)
            assert caplog.text.count("Manual hold:") == 1
            h.clock.advance(HOLD_NOTE_INTERVAL_S)
            h.tick_with_usage(_WANTS_2_FROM_3)
        assert caplog.text.count("Manual hold:") == 2
        assert caplog.text.count("Manual switch to") == 1

    def test_dry_run_holds_without_writing_state(self, temp_home):
        h = _harness(temp_home)
        h.engine = h._make_engine(dry_run=True)
        self._manual_switch_to_3(h)
        h.events.clear()

        outcome = h.tick_with_usage(_WANTS_2_FROM_3)

        assert outcome is TickOutcome.NO_ACTION
        assert "manual-hold" in _reasons(h)
        assert "manualHold" not in h.state()


class TestStatusLine:
    def test_status_names_the_hold(self, temp_home, capsys):
        import json
        import time
        from unittest.mock import patch

        from claude_swap.usage_store import UsageEntry

        h = _harness(temp_home)
        (h.switcher.backup_dir / "autoswitch_state.json").write_text(
            json.dumps({"manualHold": {"account": "1", "since": time.time()}})
        )
        with patch.object(
            h.switcher, "_active_account_usage", return_value=UsageEntry()
        ):
            h.switcher.status()

        out = capsys.readouterr().out
        assert "auto: holding Account-1 (switched by hand) until" in out

    def test_status_is_quiet_without_a_hold(self, temp_home, capsys):
        from unittest.mock import patch

        from claude_swap.usage_store import UsageEntry

        h = _harness(temp_home)
        with patch.object(
            h.switcher, "_active_account_usage", return_value=UsageEntry()
        ):
            h.switcher.status()

        assert "holding" not in capsys.readouterr().out


class TestHoldReader:
    def test_reads_an_active_hold_for_the_current_account(self):
        state = {"manualHold": {"account": "3", "since": 1000.0}}
        assert manual_hold_until(state, "3", 60, now=1000.0 + 59 * 60) == (
            1000.0 + 3600
        )

    def test_none_for_another_account_an_expired_hold_or_disabled(self):
        state = {"manualHold": {"account": "3", "since": 1000.0}}
        assert manual_hold_until(state, "2", 60, now=1001.0) is None
        assert manual_hold_until(state, "3", 60, now=1000.0 + 3600) is None
        assert manual_hold_until(state, "3", 0, now=1001.0) is None
        assert manual_hold_until({}, "3", 60, now=1001.0) is None
        assert manual_hold_until({"manualHold": "junk"}, "3", 60, now=1.0) is None


class TestSetting:
    def test_default_is_sixty_minutes(self, tmp_path):
        assert AutoSwitchSettings().manual_hold_minutes == 60
        assert load_settings(tmp_path).manual_hold_minutes == 60

    def test_settable_via_config(self, tmp_path):
        set_setting(tmp_path, "autoswitch.manualHoldMinutes", "15")
        assert load_settings(tmp_path).manual_hold_minutes == 15
        set_setting(tmp_path, "autoswitch.manualHoldMinutes", "0")
        assert load_settings(tmp_path).manual_hold_minutes == 0

    def test_rejects_out_of_range(self, tmp_path):
        from claude_swap.exceptions import ConfigError

        with pytest.raises(ConfigError):
            set_setting(tmp_path, "autoswitch.manualHoldMinutes", "-5")
