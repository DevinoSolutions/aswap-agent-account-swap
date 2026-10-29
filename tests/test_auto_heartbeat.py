"""A stalled `aswap auto` is visible in `aswap status`.

Measured 2026-10-29 on amin-desktop: a freshly launched `aswap auto` sat alive
for 20 minutes at 0.4 s of CPU without one tick or log line, `aswap status`
still showed its manual hold, and the active account ran into its limit with
every other account free. The loop now stamps a heartbeat before each wait,
and status warns once the announced next check is well overdue.
"""

from __future__ import annotations

import json
import os
import time
from unittest.mock import patch

import pytest

from claude_swap.autoswitch import (
    HEARTBEAT_FILENAME,
    HEARTBEAT_GRACE_S,
    TickOutcome,
    clear_heartbeat,
    heartbeat_stalled_since,
    write_heartbeat,
)
from claude_swap.usage_store import UsageEntry

from tests.test_autoswitch import EngineHarness


def _harness(temp_home) -> EngineHarness:
    h = EngineHarness(temp_home)
    h.seed(1, "a@example.com")
    h.make_live("a@example.com", 1)
    return h


def _status(h: EngineHarness, capsys) -> str:
    with patch.object(h.switcher, "_active_account_usage", return_value=UsageEntry()):
        h.switcher.status()
    return capsys.readouterr().out


class TestHeartbeatFile:
    def test_on_time_is_not_stalled(self, tmp_path):
        write_heartbeat(tmp_path, 1000.0, 1060.0)
        assert heartbeat_stalled_since(tmp_path, 1060.0 + HEARTBEAT_GRACE_S) is None

    def test_overdue_reports_the_last_check(self, tmp_path):
        write_heartbeat(tmp_path, 1000.0, 1060.0)
        assert heartbeat_stalled_since(tmp_path, 1061.0 + HEARTBEAT_GRACE_S) == 1000.0

    def test_missing_or_garbled_is_not_stalled(self, tmp_path):
        assert heartbeat_stalled_since(tmp_path, 1e12) is None
        (tmp_path / HEARTBEAT_FILENAME).write_bytes(b"\0\0\0")
        assert heartbeat_stalled_since(tmp_path, 1e12) is None

    def test_clear_leaves_another_process_stamp(self, tmp_path):
        path = tmp_path / HEARTBEAT_FILENAME
        path.write_text(json.dumps({"pid": os.getpid() + 1, "at": 1, "nextBy": 2}))
        clear_heartbeat(tmp_path)
        assert path.exists()
        write_heartbeat(tmp_path, 1.0, 2.0)
        clear_heartbeat(tmp_path)
        assert not path.exists()


class TestRunLoopStamps:
    def test_each_wait_is_announced_and_a_clean_stop_clears_it(self, temp_home):
        h = _harness(temp_home)
        path = h.switcher.backup_dir / HEARTBEAT_FILENAME
        seen = []

        def fake_tick():
            seen.append(json.loads(path.read_text(encoding="utf-8")))
            if len(seen) >= 2:
                h.engine.stop()
            return TickOutcome.NO_ACTION

        with patch.object(h.engine, "tick", side_effect=fake_tick), patch.object(
            h.engine, "_sleep"
        ) as sleep:
            assert h.engine.run_loop() == 0
        delay = sleep.call_args_list[0].args[0]
        assert seen[1]["nextBy"] - seen[1]["at"] == pytest.approx(delay)
        assert not path.exists()

    def test_a_dry_run_never_stamps(self, temp_home):
        h = _harness(temp_home)
        h.engine.dry_run = True
        stamped = []
        with patch(
            "claude_swap.autoswitch.write_heartbeat",
            side_effect=lambda *a: stamped.append(a),
        ), patch.object(
            h.engine, "tick", side_effect=lambda: (h.engine.stop(), TickOutcome.NO_ACTION)[1]
        ), patch.object(h.engine, "_sleep"):
            h.engine.run_loop()
        assert stamped == []


class TestStatusLine:
    def test_status_warns_about_a_stalled_loop(self, temp_home, capsys):
        h = _harness(temp_home)
        now = time.time()
        write_heartbeat(h.switcher.backup_dir, now - 1200, now - 1140)
        out = _status(h, capsys)
        assert "auto: no check since" in out
        assert "(20 min)" in out

    def test_status_is_quiet_while_the_loop_checks(self, temp_home, capsys):
        h = _harness(temp_home)
        now = time.time()
        write_heartbeat(h.switcher.backup_dir, now, now + 60)
        assert "no check since" not in _status(h, capsys)

    def test_status_is_quiet_without_a_loop(self, temp_home, capsys):
        assert "no check since" not in _status(_harness(temp_home), capsys)
