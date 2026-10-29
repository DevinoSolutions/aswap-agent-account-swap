"""A running ``aswap auto`` picks up ``aswap config`` changes without a restart.

The engine used to read its settings once, at start, so ``aswap config set
autoswitch.threshold 80`` did nothing to the long-running process people
actually leave on. Each tick now looks at the settings document's write time
and applies whatever changed, strictly validated, from that tick on.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap.autoswitch import (
    BurnTracker,
    ConfigWarningEvent,
    SettingsReloadedEvent,
    SwitchEvent,
    TickOutcome,
)
from claude_swap.settings import (
    atomic_write_json,
    set_setting,
    settings_path,
    unset_setting,
)

from tests.test_autoswitch import EngineHarness, _usage
from tests.test_manual_hold import _SETTLED_ON_1, _WANTS_2_FROM_3, _switch_by_hand


def _harness(temp_home: Path, **settings) -> EngineHarness:
    h = EngineHarness(temp_home, **settings)
    h.seed(1, "a@example.com")
    h.seed(2, "b@example.com")
    h.seed(3, "c@example.com")
    h.make_live("a@example.com", 1)
    return h


def _set(h: EngineHarness, key: str, value: str) -> None:
    set_setting(h.switcher.backup_dir, key, value)


def _reloads(h: EngineHarness) -> list[SettingsReloadedEvent]:
    return [e for e in h.events if isinstance(e, SettingsReloadedEvent)]


# Account 1 at 80%: under the default 90 threshold, over 72.
_HOT_1 = {"1": _usage(80), "2": _usage(10), "3": _usage(10)}


class TestThresholdChange:
    def test_a_lowered_threshold_switches_on_the_next_tick(self, temp_home):
        h = _harness(temp_home)
        assert h.tick_with_usage(_HOT_1) is TickOutcome.NO_ACTION

        _set(h, "autoswitch.threshold", "72")
        assert h.tick_with_usage(_HOT_1) is TickOutcome.SWITCHED

        assert h.engine.settings.threshold == 72.0
        (event,) = _reloads(h)
        assert event.changes == (("autoswitch.threshold", 90.0, 72.0),)
        # The poll planner keys on the same threshold the engine decides with.
        assert h.switcher._poll_inputs_override == (72.0, ())

    def test_the_change_is_logged_once_with_old_and_new(self, temp_home, caplog):
        h = _harness(temp_home)
        h.tick_with_usage(_HOT_1)
        _set(h, "autoswitch.threshold", "72")
        _set(h, "autoswitch.autoAddAccounts", "false")

        with caplog.at_level(logging.INFO, logger="claude-swap"):
            h.tick_with_usage(_HOT_1)
            h.tick_with_usage(_HOT_1)  # nothing new: nothing logged

        lines = [r.getMessage() for r in caplog.records if "settings reloaded" in r.getMessage()]
        assert lines == [
            "settings reloaded: autoswitch.threshold 90 -> 72; "
            "autoswitch.autoAddAccounts true -> false"
        ]
        (event,) = _reloads(h)
        assert event.to_json()["event"] == "settings-reloaded"
        assert event.to_json()["changes"] == [
            {"key": "autoswitch.threshold", "old": 90.0, "new": 72.0},
            {"key": "autoswitch.autoAddAccounts", "old": True, "new": False},
        ]

    def test_unset_goes_back_to_the_default(self, temp_home):
        h = _harness(temp_home)
        _set(h, "autoswitch.threshold", "72")
        h.tick_with_usage(_HOT_1)
        assert h.engine.settings.threshold == 72.0

        unset_setting(h.switcher.backup_dir, "autoswitch.threshold")
        h.tick_with_usage(_HOT_1)
        assert h.engine.settings.threshold == 90.0
        assert _reloads(h)[-1].changes == (("autoswitch.threshold", 72.0, 90.0),)


class TestCheapWhenUnchanged:
    def test_no_reread_without_a_write(self, temp_home):
        h = _harness(temp_home)
        _set(h, "autoswitch.threshold", "72")
        h.tick_with_usage(_HOT_1)

        with patch("claude_swap.live_settings.reload_autoswitch_settings") as reread:
            h.tick_with_usage(_HOT_1)
            h.tick_with_usage(_HOT_1)
        reread.assert_not_called()

    def test_a_write_to_another_section_changes_nothing(self, temp_home):
        h = _harness(temp_home)
        before = h.engine.settings
        set_setting(h.switcher.backup_dir, "ui.theme", "dark")
        h.tick_with_usage(_HOT_1)
        assert h.engine.settings is before
        assert _reloads(h) == []


class TestInvalidValues:
    def _write_raw(self, h: EngineHarness, section: dict) -> None:
        # A hand edit / foreign writer: nothing validated it on the way in.
        atomic_write_json(
            settings_path(h.switcher.backup_dir),
            {"schemaVersion": 1, "autoswitch": section},
        )

    def test_an_invalid_value_keeps_the_last_good_one(self, temp_home):
        h = _harness(temp_home)
        _set(h, "autoswitch.threshold", "72")
        h.tick_with_usage(_HOT_1)

        self._write_raw(h, {"threshold": 120, "cooldownSeconds": 0})
        h.tick_with_usage({"1": _usage(10), "2": _usage(10), "3": _usage(10)})

        assert h.engine.settings.threshold == 72.0  # not clamped to 99.9
        assert h.engine.settings.cooldown_seconds == 0.0  # the good key applies
        warning = next(e for e in h.events if isinstance(e, ConfigWarningEvent))
        assert "autoswitch.threshold must be between 50 and 99.9" in warning.message
        assert "keeping 72" in warning.message
        assert _reloads(h)[-1].changes == (("autoswitch.cooldownSeconds", 300.0, 0.0),)

    def test_a_truthy_string_does_not_turn_switching_off(self, temp_home):
        h = _harness(temp_home)
        self._write_raw(h, {"enabled": "false"})
        h.tick_with_usage(_HOT_1)
        assert h.engine.settings.enabled is True
        assert any(isinstance(e, ConfigWarningEvent) for e in h.events)

    def test_an_unreadable_document_keeps_every_setting(self, temp_home):
        h = _harness(temp_home)
        _set(h, "autoswitch.threshold", "72")
        h.tick_with_usage(_HOT_1)
        before = h.engine.settings

        from claude_swap import statedb

        db, rel = statedb.locate(settings_path(h.switcher.backup_dir))
        db.write(rel, "{not json")
        h.tick_with_usage(_HOT_1)

        assert h.engine.settings is before
        warning = next(e for e in h.events if isinstance(e, ConfigWarningEvent))
        assert "keeping the current settings" in warning.message


class TestPinned:
    def test_a_command_line_flag_wins_over_a_stored_change(self, temp_home):
        h = _harness(temp_home, threshold=85.0)
        h.engine = h._make_engine(pinned={"threshold"})
        _set(h, "autoswitch.threshold", "72")
        _set(h, "autoswitch.cooldownSeconds", "10")

        h.tick_with_usage(_HOT_1)

        assert h.engine.settings.threshold == 85.0
        assert h.engine.settings.cooldown_seconds == 10.0
        (event,) = _reloads(h)
        assert event.pinned == (("autoswitch.threshold", 72.0, 85.0),)
        assert "autoswitch.threshold stays 85" in event.human()

    def test_settings_passed_differently_from_the_document_are_pinned(self, temp_home):
        # A caller that built its own settings (the TUI after a session
        # threshold, a test) keeps them; the rest still follows the document.
        h = _harness(temp_home, threshold=80.0)
        _set(h, "autoswitch.threshold", "72")
        _set(h, "autoswitch.hysteresisPct", "5")
        h.tick_with_usage(_HOT_1)
        assert h.engine.settings.threshold == 80.0
        assert h.engine.settings.hysteresis_pct == 5.0

    def test_a_tui_session_threshold_is_not_undone(self, temp_home):
        h = _harness(temp_home)
        h.engine.apply_threshold(75.0)
        _set(h, "autoswitch.threshold", "95")
        h.tick_with_usage(_HOT_1)
        assert h.engine.settings.threshold == 75.0


class TestInMemoryStateSurvives:
    def test_burn_history_survives_a_threshold_change(self, temp_home):
        h = _harness(temp_home)
        h.tick_with_usage(_HOT_1)
        burn = h.engine._burn
        _set(h, "autoswitch.threshold", "95")
        h.tick_with_usage(_HOT_1)
        assert h.engine._burn is burn

    def test_a_model_change_restarts_burn_samples_and_the_name_check(self, temp_home):
        h = _harness(temp_home)
        h.tick_with_usage(_HOT_1)
        burn = h.engine._burn
        _set(h, "autoswitch.model", "Fable")
        h.tick_with_usage(_HOT_1)
        assert h.engine._models == ("Fable",)
        assert isinstance(h.engine._burn, BurnTracker) and h.engine._burn is not burn
        assert h.switcher._poll_inputs_override == (90.0, ("Fable",))

    def test_a_shorter_hold_retimes_the_running_hold(self, temp_home):
        h = _harness(temp_home, strategy="consume-first", cooldown_seconds=0)
        assert h.tick_with_usage(_SETTLED_ON_1) is TickOutcome.NO_ACTION
        _switch_by_hand(h, 3, "c@example.com")
        started = h.clock.now
        assert h.tick_with_usage(_WANTS_2_FROM_3) is TickOutcome.NO_ACTION
        assert h.engine.manual_hold == ("3", pytest.approx(started + 3600))

        # Lengthen it: the hold already running is extended, not restarted.
        _set(h, "autoswitch.manualHoldMinutes", "120")
        h.clock.advance(61 * 60)
        assert h.tick_with_usage(_WANTS_2_FROM_3) is TickOutcome.NO_ACTION
        assert h.engine.manual_hold == ("3", pytest.approx(started + 7200))

        # Shorten it below the time already held: it ends now.
        _set(h, "autoswitch.manualHoldMinutes", "30")
        assert h.tick_with_usage(_WANTS_2_FROM_3) is TickOutcome.SWITCHED
        assert h.switcher.current_account_number() == "2"


class TestAutoAddToggle:
    def _engine_with_login(self, temp_home):
        h = _harness(temp_home)
        h.switcher.observe_live_login = lambda: ("new@example.com", "lineage")
        adopted: list = []
        h.switcher.adopt_live_login = adopted.append
        return h, adopted

    def test_turning_it_off_stops_the_next_adoption(self, temp_home):
        h, adopted = self._engine_with_login(temp_home)
        h.tick_with_usage(_HOT_1)  # first sighting: not yet stable
        _set(h, "autoswitch.autoAddAccounts", "false")
        h.tick_with_usage(_HOT_1)  # would have adopted with the old value
        h.tick_with_usage(_HOT_1)
        assert adopted == []

    def test_turning_it_back_on_needs_two_fresh_polls(self, temp_home):
        h, adopted = self._engine_with_login(temp_home)
        h.tick_with_usage(_HOT_1)
        _set(h, "autoswitch.autoAddAccounts", "false")
        h.tick_with_usage(_HOT_1)
        _set(h, "autoswitch.autoAddAccounts", "true")
        h.tick_with_usage(_HOT_1)
        assert adopted == []  # the pre-off sighting does not count
        h.tick_with_usage(_HOT_1)
        assert adopted == [("new@example.com", "lineage")]


class TestWarmupToggle:
    def test_enabling_warmup_starts_the_warmer(self, temp_home):
        h = _harness(temp_home)
        assert h.engine._warmup is None
        _set(h, "autoswitch.warmupFiveHour", "true")
        with patch("claude_swap.autoswitch.WarmupEngine") as warmer:
            warmer.return_value.tick.return_value.failed = 0
            h.tick_with_usage(_HOT_1)
        assert h.engine._warmup is warmer.return_value


class TestSleepEndsOnAChange:
    def test_a_settings_write_cuts_the_sleep_short(self, temp_home):
        h = _harness(temp_home)
        waits: list[float] = []

        def fake_wait(timeout):
            waits.append(timeout)
            if len(waits) == 1:
                _set(h, "autoswitch.threshold", "72")
            return False

        with patch.object(h.engine._wake, "wait", side_effect=fake_wait):
            h.engine._sleep(600.0)
        assert len(waits) == 1  # woke after the slice that saw the write

    def test_without_a_write_the_whole_delay_is_slept(self, temp_home):
        h = _harness(temp_home)
        waits: list[float] = []
        with patch.object(
            h.engine._wake, "wait", side_effect=lambda t: waits.append(t) or False
        ):
            h.engine._sleep(60.0)
        assert sum(waits) == pytest.approx(60.0)

    def test_wake_still_ends_it_at_once(self, temp_home):
        h = _harness(temp_home)
        with patch.object(h.engine._wake, "wait", return_value=True) as wait:
            h.engine._sleep(600.0)
        assert wait.call_count == 1


def test_no_switch_event_reports_nothing_secret(temp_home):
    """Only setting names and values reach the log line and the JSON event."""
    h = _harness(temp_home)
    _set(h, "autoswitch.model", "Fable")
    h.tick_with_usage(_HOT_1)
    (event,) = _reloads(h)
    text = json.dumps(event.to_json()) + event.human()
    for secret in ("sk-", "rt-", "accessToken", "refreshToken"):
        assert secret not in text
    assert not any(isinstance(e, SwitchEvent) for e in h.events if e is event)
