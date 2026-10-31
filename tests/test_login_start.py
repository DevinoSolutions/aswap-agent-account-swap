"""`autoswitch.startOnLogin`: a per-user login entry that starts `aswap auto`.

Measured 2026-10-30: the laptop crashed, rebooted, and nothing restarted
`aswap auto`; it went an hour without switching until it was relaunched by
hand. The setting keeps one login entry in step with itself, and the entry's
launch (`aswap auto --at-login`) stands down when the setting was turned off
since or another loop is already checking.
"""

from __future__ import annotations

import json
import os
import plistlib
from pathlib import Path

import pytest

from claude_swap import cli, login_start
from claude_swap.autoswitch import HEARTBEAT_FILENAME, HEARTBEAT_GRACE_S, write_heartbeat
from claude_swap.settings import load_settings, set_setting

EXE = str(Path("/opt/bin/aswap").resolve())


@pytest.fixture
def exe(monkeypatch):
    monkeypatch.setattr(login_start, "aswap_executable", lambda: EXE)
    monkeypatch.setattr(login_start.shutil, "which", lambda name: None)
    return EXE


class TestWindows:
    def test_on_writes_the_run_value_and_off_removes_it(self, tmp_path, exe, block_real_login_entry):
        assert login_start.sync(True, tmp_path, platform="win32") == "installed"
        value = block_real_login_entry.values[login_start.ENTRY_NAME]
        assert value == f'"{EXE}" auto --at-login'
        assert login_start.sync(True, tmp_path, platform="win32") is None
        assert login_start.sync(False, tmp_path, platform="win32") == "removed"
        assert block_real_login_entry.values == {}
        assert login_start.sync(False, tmp_path, platform="win32") is None

    def test_windows_terminal_opens_it_in_a_titled_tab(self):
        cmd = login_start.windows_command("C:/u/aswap.exe", "C:/w/wt.exe")
        assert cmd == (
            '"C:/w/wt.exe" -w new nt --title "aswap auto" "C:/u/aswap.exe" auto --at-login'
        )

    def test_a_moved_install_updates_the_entry(self, tmp_path, exe, block_real_login_entry):
        block_real_login_entry.values[login_start.ENTRY_NAME] = '"C:/old/aswap.exe" auto --at-login'
        assert login_start.sync(True, tmp_path, platform="win32") == "updated"

    def test_no_executable_is_reported_not_raised(self, tmp_path, monkeypatch):
        monkeypatch.setattr(login_start, "aswap_executable", lambda: None)
        assert login_start.sync(True, tmp_path, platform="win32").startswith("failed")


class TestMacAndLinux:
    def test_macos_launch_agent(self, tmp_path, exe):
        assert login_start.sync(True, tmp_path, platform="darwin") == "installed"
        agent = Path.home() / "Library" / "LaunchAgents" / "ca.devino.aswap.auto.plist"
        body = plistlib.loads(agent.read_bytes())
        assert body["ProgramArguments"] == [EXE, "auto", "--at-login"]
        assert body["RunAtLoad"] is True
        assert login_start.sync(False, tmp_path, platform="darwin") == "removed"
        assert not agent.exists()

    def test_linux_autostart_entry(self, tmp_path, exe):
        assert login_start.sync(True, tmp_path, platform="linux") == "installed"
        entry = Path.home() / ".config" / "autostart" / "aswap-auto.desktop"
        assert f'Exec="{EXE}" auto --at-login' in entry.read_text()
        assert login_start.sync(False, tmp_path, platform="linux") == "removed"
        assert not entry.exists()


class TestSkipReason:
    def test_runs_when_on_and_no_loop_is_checking(self, tmp_path):
        assert login_start.skip_reason(True, tmp_path, 1000.0) is None

    def test_stands_down_when_the_setting_is_off(self, tmp_path):
        assert "off" in login_start.skip_reason(False, tmp_path, 1000.0)

    def test_stands_down_while_another_loop_is_on_time(self, tmp_path):
        (tmp_path / HEARTBEAT_FILENAME).write_text(
            json.dumps({"pid": os.getpid() + 1, "at": 1000.0, "nextBy": 1060.0})
        )
        assert "already" in login_start.skip_reason(True, tmp_path, 1060.0)

    def test_starts_over_a_stamp_left_by_a_crash(self, tmp_path):
        (tmp_path / HEARTBEAT_FILENAME).write_text(
            json.dumps({"pid": os.getpid() + 1, "at": 1000.0, "nextBy": 1060.0})
        )
        assert login_start.skip_reason(True, tmp_path, 1061.0 + HEARTBEAT_GRACE_S) is None

    def test_starts_over_a_garbled_stamp(self, tmp_path):
        (tmp_path / HEARTBEAT_FILENAME).write_bytes(b"\0\0")
        assert login_start.skip_reason(True, tmp_path, 1000.0) is None


class TestCli:
    def test_config_set_installs_and_unset_removes(self, temp_home, exe, block_real_login_entry, monkeypatch, capsys):
        monkeypatch.setattr(login_start.sys, "platform", "win32")
        cli._config_command(["set", "autoswitch.startOnLogin", "true"])
        assert login_start.ENTRY_NAME in block_real_login_entry.values
        assert "login entry installed" in capsys.readouterr().out
        cli._config_command(["unset", "autoswitch.startOnLogin"])
        assert block_real_login_entry.values == {}

    def test_other_keys_leave_the_entry_alone(self, temp_home, exe, block_real_login_entry, monkeypatch):
        monkeypatch.setattr(login_start.sys, "platform", "win32")
        block_real_login_entry.values[login_start.ENTRY_NAME] = "kept"
        cli._config_command(["set", "autoswitch.threshold", "95"])
        assert block_real_login_entry.values[login_start.ENTRY_NAME] == "kept"

    def test_a_login_launch_with_the_setting_off_exits_without_running(self, temp_home, monkeypatch, capsys):
        started = []
        monkeypatch.setattr(
            "claude_swap.autoswitch.AutoSwitchEngine.run_loop",
            lambda self: started.append(1) or 0,
        )
        cli._auto_command(["--at-login"])
        assert started == []
        assert "start on login is off" in capsys.readouterr().out

    def test_default_is_off(self, tmp_path):
        assert load_settings(tmp_path).start_on_login is False
        set_setting(tmp_path, "autoswitch.startOnLogin", "true")
        assert load_settings(tmp_path).start_on_login is True


class TestRunningLoop:
    def test_a_live_toggle_moves_the_entry(self, temp_home, exe, block_real_login_entry, monkeypatch):
        from dataclasses import replace

        from tests.test_autoswitch import EngineHarness

        monkeypatch.setattr(login_start.sys, "platform", "win32")
        h = EngineHarness(temp_home)
        old = h.engine.settings
        h.engine._settings_applied(old, replace(old, start_on_login=True))
        assert login_start.ENTRY_NAME in block_real_login_entry.values
        h.engine._settings_applied(replace(old, start_on_login=True), old)
        assert block_real_login_entry.values == {}
