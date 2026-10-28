"""A switch while something else holds ``~/.claude.json`` open (Windows).

Every running Claude Code re-reads ``~/.claude.json``, and on Windows a
rename onto a file ANY process has open fails with ERROR_ACCESS_DENIED, even
when that handle allows FILE_SHARE_DELETE as Node's do. On 2026-10-27 that
made two auto-switches on a machine with ~35 Claude Code sessions fail and
roll back (``[WinError 5] Access is denied: '.claude.<pid>.tmp' ->
'.claude.json'``). Here the test process plays the reader, with the same
share mode Node uses, and keeps the file open well past aswap's old ~0.75 s
replace budget once aswap is waiting to publish.
"""

from __future__ import annotations

import json
import sys
import threading
import time

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="Windows rename-onto-open-file semantics"
)

# Past the old ~0.75 s budget, well inside the new ~5.4 s one.
HOLD_AFTER_PENDING_S = 1.5


def _open_like_node(path):
    """GENERIC_READ, FILE_SHARE_READ|WRITE|DELETE: how libuv opens a file."""
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = k32.CreateFileW(str(path), 0x80000000, 0x7, None, 3, 0x80, None)
    if handle in (None, wintypes.HANDLE(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())
    return lambda: k32.CloseHandle(handle)


def test_switch_outlasts_a_reader_holding_claude_json(box):
    alice = box.account("alice")
    bob = box.account("bob")
    alice_login = box.login(alice["email"])
    box.aswap("add")
    box.login(bob["email"])
    box.aswap("add")
    assert box.live_email() == bob["email"]
    before = json.loads(box.config_path.read_text(encoding="utf-8"))

    seen = {"pending": None, "held_for": 0.0}
    close = _open_like_node(box.config_path)

    def reader():
        # Hold until aswap has its new config written beside the live one
        # (the rename is due), then keep holding past the old budget.
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                pending = list(box.home.glob(".claude.*.tmp"))
                if pending:
                    seen["pending"] = pending[0].name
                    break
                time.sleep(0.005)
            if seen["pending"]:
                started = time.monotonic()
                time.sleep(HOLD_AFTER_PENDING_S)
                seen["held_for"] = time.monotonic() - started
        finally:
            close()

    holder = threading.Thread(target=reader, daemon=True)
    holder.start()
    try:
        done = box.aswap("switch", "1", check=False)
    finally:
        holder.join(timeout=90)

    # The hold really overlapped aswap's rename, for longer than it used to
    # tolerate, and the switch still landed rather than rolling back.
    assert seen["pending"], "aswap never staged a ~/.claude.json replace"
    assert seen["held_for"] >= HOLD_AFTER_PENDING_S
    assert done.returncode == 0, done.stdout + done.stderr

    assert box.live_email() == alice["email"]
    assert box.live_credentials()["refreshToken"] == alice_login["refreshToken"]
    after = json.loads(box.config_path.read_text(encoding="utf-8"))
    assert after["oauthAccount"]["accountUuid"] == alice["uuid"]
    # No lost state: every unrelated key Claude Code keeps there survived.
    assert {k: v for k, v in after.items() if k != "oauthAccount"} == {
        k: v for k, v in before.items() if k != "oauthAccount"
    }
    assert box.list_json()["claude"]["activeAccountNumber"] == 1
    assert list(box.home.glob(".claude.*.tmp")) == []
