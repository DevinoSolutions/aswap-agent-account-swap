"""A running ``aswap codex auto`` picks up ``aswap config`` changes too.

The Claude engine learned this in #11; the Codex engine still read its
settings once at start. It now shares the same mechanism
(:class:`claude_swap.live_settings.LiveSettings`): same strict re-read, same
``settings reloaded`` line and ``settings-reloaded`` event, same pinning of
command-line flags and of a TUI session threshold, same sliced sleep.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import cli, statedb
from claude_swap.autoswitch import (
    ConfigWarningEvent,
    SettingsReloadedEvent,
    SwitchEvent,
    TickOutcome,
)
from claude_swap.codex_autoswitch import CodexAutoSwitchEngine
from claude_swap.settings import (
    AutoSwitchSettings,
    atomic_write_json,
    load_settings,
    set_setting,
    settings_path,
    unset_setting,
)

from tests.test_codex_autoswitch import FakeCodexSwitcher, _account


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def _engine(
    backup: Path,
    pct_active: float = 80,
    *,
    settings: AutoSwitchSettings | None = None,
    pinned=(),
    clock=None,
):
    # A managed backup root, like the real CodexAccountSwitcher's: settings
    # live in state.db and their fingerprint is the document's updated_at.
    statedb.manage(backup)
    switcher = FakeCodexSwitcher(
        backup, (_account("1", pct_active, active=True), _account("2", 10))
    )
    events: list = []
    engine = CodexAutoSwitchEngine(
        switcher,
        settings or load_settings(backup),
        events.append,
        clock=clock or _Clock(),
        pinned=pinned,
    )
    return engine, switcher, events


def _set(backup: Path, key: str, value: str) -> None:
    set_setting(backup, key, value)


def _reloads(events) -> list[SettingsReloadedEvent]:
    return [e for e in events if isinstance(e, SettingsReloadedEvent)]


class TestThresholdChange:
    def test_a_lowered_threshold_switches_on_the_next_tick(self, tmp_path):
        engine, switcher, events = _engine(tmp_path)
        assert engine.tick() is TickOutcome.NO_ACTION  # 80% < 90%

        _set(tmp_path, "autoswitch.threshold", "72")
        assert engine.tick() is TickOutcome.SWITCHED
        assert switcher.switched_to == ["2"]
        assert engine.settings.threshold == 72.0

    def test_the_change_is_logged_once_with_old_and_new(self, tmp_path, caplog):
        engine, _switcher, events = _engine(tmp_path, pct_active=10)
        _set(tmp_path, "autoswitch.threshold", "72")
        with caplog.at_level(logging.INFO, logger="claude-swap"):
            engine.tick()
            engine.tick()

        (event,) = _reloads(events)
        assert event.changes == (("autoswitch.threshold", 90.0, 72.0),)
        assert event.human() == "settings reloaded: autoswitch.threshold 90 -> 72"
        assert event.to_json()["event"] == "settings-reloaded"
        assert event.to_json()["changes"] == [
            {"key": "autoswitch.threshold", "old": 90.0, "new": 72.0}
        ]
        lines = [r.getMessage() for r in caplog.records if "settings reloaded" in r.getMessage()]
        assert lines == ["settings reloaded: autoswitch.threshold 90 -> 72"]

    def test_unset_goes_back_to_the_default(self, tmp_path):
        _set(tmp_path, "autoswitch.threshold", "72")
        engine, _switcher, events = _engine(tmp_path, pct_active=10)
        assert engine.settings.threshold == 72.0
        unset_setting(tmp_path, "autoswitch.threshold")
        engine.tick()
        assert engine.settings.threshold == 90.0
        assert _reloads(events)[-1].changes == (("autoswitch.threshold", 72.0, 90.0),)

    def test_a_cooldown_change_applies_to_the_running_cooldown(self, tmp_path):
        clock = _Clock()
        engine, switcher, _events = _engine(tmp_path, pct_active=95, clock=clock)
        # Inside the default 300s cooldown of an earlier switch.
        atomic_write_json(
            engine.state_path, {"schemaVersion": 1, "lastSwitchAt": clock.now - 100}
        )
        assert engine.tick() is TickOutcome.NO_ACTION
        _set(tmp_path, "autoswitch.cooldownSeconds", "60")
        assert engine.tick() is TickOutcome.SWITCHED
        assert switcher.switched_to == ["2"]


class TestCheapWhenUnchanged:
    def test_no_reread_without_a_write(self, tmp_path):
        engine, _switcher, _events = _engine(tmp_path, pct_active=10)
        _set(tmp_path, "autoswitch.threshold", "72")
        engine.tick()
        with patch("claude_swap.live_settings.reload_autoswitch_settings") as reread:
            engine.tick()
            engine.tick()
        reread.assert_not_called()

    def test_a_write_to_another_section_changes_nothing(self, tmp_path):
        engine, _switcher, events = _engine(tmp_path, pct_active=10)
        before = engine.settings
        _set(tmp_path, "ui.theme", "dark")
        engine.tick()
        assert engine.settings is before
        assert _reloads(events) == []


class TestInvalidValues:
    def test_an_invalid_value_keeps_the_last_good_one(self, tmp_path):
        engine, _switcher, events = _engine(tmp_path, pct_active=10)
        _set(tmp_path, "autoswitch.threshold", "72")
        engine.tick()
        # A hand edit / foreign writer: nothing validated it on the way in.
        atomic_write_json(
            settings_path(tmp_path),
            {"schemaVersion": 1, "autoswitch": {"threshold": 120, "cooldownSeconds": 0}},
        )
        engine.tick()

        assert engine.settings.threshold == 72.0  # not clamped to 99.9
        assert engine.settings.cooldown_seconds == 0.0  # the good key applies
        warning = next(e for e in events if isinstance(e, ConfigWarningEvent))
        assert "autoswitch.threshold must be between 50 and 99.9" in warning.message
        assert "keeping 72" in warning.message
        assert _reloads(events)[-1].changes == (
            ("autoswitch.cooldownSeconds", 300.0, 0.0),
        )

    def test_an_unreadable_document_keeps_every_setting(self, tmp_path):
        engine, _switcher, events = _engine(tmp_path, pct_active=10)
        _set(tmp_path, "autoswitch.threshold", "72")
        engine.tick()
        before = engine.settings
        db, rel = statedb.locate(settings_path(tmp_path))
        db.write(rel, "{not json")
        engine.tick()
        assert engine.settings is before
        warning = next(e for e in events if isinstance(e, ConfigWarningEvent))
        assert "keeping the current settings" in warning.message


class TestPinned:
    def test_a_command_line_flag_wins_over_a_stored_change(self, tmp_path):
        engine, _switcher, events = _engine(
            tmp_path,
            pct_active=10,
            settings=AutoSwitchSettings(threshold=85.0),
            pinned={"threshold"},
        )
        _set(tmp_path, "autoswitch.threshold", "72")
        _set(tmp_path, "autoswitch.cooldownSeconds", "10")
        engine.tick()

        assert engine.settings.threshold == 85.0
        assert engine.settings.cooldown_seconds == 10.0
        (event,) = _reloads(events)
        assert event.pinned == (("autoswitch.threshold", 72.0, 85.0),)
        assert "autoswitch.threshold stays 85" in event.human()

    def test_a_tui_session_threshold_is_not_undone(self, tmp_path):
        engine, _switcher, _events = _engine(tmp_path, pct_active=10)
        engine.apply_threshold(75.0)
        _set(tmp_path, "autoswitch.threshold", "95")
        engine.tick()
        assert engine.settings.threshold == 75.0


class TestSleepAndLoop:
    def test_a_settings_write_cuts_the_sleep_short(self, tmp_path):
        engine, _switcher, _events = _engine(tmp_path)
        waits: list[float] = []

        def fake_wait(timeout):
            waits.append(timeout)
            if len(waits) == 1:
                _set(tmp_path, "autoswitch.threshold", "72")
            return False

        with patch.object(engine._wake, "wait", side_effect=fake_wait):
            engine._sleep(600.0)
        assert len(waits) == 1  # woke after the slice that saw the write

    def test_without_a_write_the_whole_delay_is_slept(self, tmp_path):
        engine, _switcher, _events = _engine(tmp_path)
        waits: list[float] = []
        with patch.object(
            engine._wake, "wait", side_effect=lambda t: waits.append(t) or False
        ):
            engine._sleep(60.0)
        assert sum(waits) == pytest.approx(60.0)

    def test_wake_ends_the_sleep_at_once(self, tmp_path):
        engine, _switcher, _events = _engine(tmp_path)
        with patch.object(engine._wake, "wait", return_value=True) as wait:
            engine._sleep(600.0)
        assert wait.call_count == 1

    def test_a_running_loop_applies_a_change_without_a_restart(self, tmp_path):
        engine, switcher, events = _engine(tmp_path)
        ticked = threading.Event()
        real_tick = engine.tick

        def tick():
            outcome = real_tick()
            ticked.set()
            return outcome

        engine.tick = tick
        loop = threading.Thread(target=engine.run_loop, daemon=True)
        loop.start()
        try:
            assert ticked.wait(5)
            ticked.clear()
            _set(tmp_path, "autoswitch.threshold", "72")
            engine.wake()
            assert ticked.wait(5)
        finally:
            engine.stop()
            loop.join(5)
        assert not loop.is_alive()
        assert switcher.switched_to == ["2"]
        assert _reloads(events)[0].changes == (("autoswitch.threshold", 90.0, 72.0),)

    def test_a_stop_before_the_loop_starts_is_kept(self, tmp_path):
        # The TUI stops an engine and builds a new one when it swaps modes;
        # a stop that beats the worker thread must not be undone.
        engine, switcher, _events = _engine(tmp_path)
        engine.stop()
        assert engine.run_loop() == 0
        assert switcher.fetches == []


def test_reload_event_reports_nothing_secret(tmp_path):
    engine, _switcher, events = _engine(tmp_path, pct_active=10)
    _set(tmp_path, "autoswitch.threshold", "72")
    engine.tick()
    (event,) = _reloads(events)
    text = json.dumps(event.to_json()) + event.human()
    for secret in ("sk-", "rt-", "accessToken", "refreshToken", "id_token"):
        assert secret not in text
    assert not any(isinstance(e, SwitchEvent) for e in events)


class TestCodexAutoCommand:
    """`aswap codex auto` hands its flags to the engine as pinned keys."""

    class FakeEngine:
        instances: list = []

        def __init__(self, switcher, settings, on_event, *, dry_run=False,
                     state_path=None, clock=None, pinned=()):
            self.settings = settings
            self.pinned = set(pinned)
            type(self).instances.append(self)

        def tick(self):
            return TickOutcome.NO_ACTION

        def run_loop(self):
            return 0

        def stop(self):
            pass

    def _run(self, argv: list[str]) -> None:
        self.FakeEngine.instances = []
        with patch("claude_swap.codex_autoswitch.CodexAutoSwitchEngine", self.FakeEngine), \
             patch.object(sys, "argv", ["claude-swap", "codex", "auto", *argv]):
            with pytest.raises(SystemExit):
                cli.main()

    def test_flags_are_pinned_against_live_reloads(self, temp_home):
        self._run(["--once", "--threshold", "60", "--cooldown", "5"])
        engine = self.FakeEngine.instances[-1]
        assert engine.pinned == {"threshold", "cooldown_seconds"}
        assert engine.settings.threshold == 60.0
        self._run(["--once"])
        assert self.FakeEngine.instances[-1].pinned == set()

    def test_help_says_changes_apply_without_a_restart(self, temp_home, capsys):
        with patch.object(sys, "argv", ["claude-swap", "codex", "auto", "--help"]):
            with pytest.raises(SystemExit):
                cli.main()
        out = " ".join(capsys.readouterr().out.split())
        assert "picks up `aswap config set` changes within one tick" in out
