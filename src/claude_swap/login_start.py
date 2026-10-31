"""Start `aswap auto` when the user logs in (``autoswitch.startOnLogin``).

One per-user entry, no admin rights, nothing that outlives the setting:

- Windows: a value in ``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run``
  that opens ``aswap auto`` in a Windows Terminal tab (a plain console when
  Windows Terminal is missing).
- macOS: a LaunchAgent in ``~/Library/LaunchAgents`` (RunAtLoad), output in
  the data dir.
- Linux: an XDG autostart entry in ``~/.config/autostart`` (desktop
  sessions; a headless box wants a systemd user unit instead).

The entry runs ``aswap auto --at-login``, which exits at once when the
setting has been turned off since, or when another ``aswap auto`` is already
checking (its heartbeat is on time). Measured 2026-10-30: after a crash and
reboot nothing restarted auto, and the laptop ran an hour without switching.
"""

from __future__ import annotations

import json
import logging
import os
import plistlib
import shutil
import sys
from pathlib import Path

from claude_swap.autoswitch import HEARTBEAT_FILENAME, HEARTBEAT_GRACE_S

_logger = logging.getLogger(__name__)

ENTRY_NAME = "aswap auto"
LOGIN_FLAG = "--at-login"
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_AGENT_LABEL = "ca.devino.aswap.auto"


class _WindowsRunKey:
    """The current user's Run key (tests swap this for an in-memory fake)."""

    def get(self, name: str) -> str | None:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
                return str(winreg.QueryValueEx(key, name)[0])
        except OSError:
            return None

    def set(self, name: str, value: str) -> None:
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)

    def delete(self, name: str) -> None:
        import winreg

        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE
            ) as key:
                winreg.DeleteValue(key, name)
        except FileNotFoundError:
            pass


_run_key = _WindowsRunKey()


def aswap_executable() -> str | None:
    """The installed ``aswap`` launcher, by PATH first, then this process."""
    found = shutil.which("aswap")
    if found:
        return str(Path(found).resolve())
    argv0 = Path(sys.argv[0]) if sys.argv and sys.argv[0] else None
    if argv0 is not None and argv0.stem.lower() == "aswap" and argv0.exists():
        return str(argv0.resolve())
    return None


def windows_command(exe: str, wt: str | None) -> str:
    if wt:
        return f'"{wt}" -w new nt --title "{ENTRY_NAME}" "{exe}" auto {LOGIN_FLAG}'
    return f'"{exe}" auto {LOGIN_FLAG}'


def _agent_path(home: Path) -> Path:
    return home / "Library" / "LaunchAgents" / f"{_AGENT_LABEL}.plist"


def _autostart_path(home: Path) -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(home / ".config")
    return Path(base) / "autostart" / "aswap-auto.desktop"


def _agent_body(exe: str, backup_dir: Path) -> bytes:
    log = str(Path(backup_dir) / "login-start.log")
    return plistlib.dumps({
        "Label": _AGENT_LABEL,
        "ProgramArguments": [exe, "auto", LOGIN_FLAG],
        "RunAtLoad": True,
        "StandardOutPath": log,
        "StandardErrorPath": log,
    })


def _desktop_body(exe: str) -> bytes:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=aswap auto\n"
        "Comment=Switch Claude accounts before they hit their limits\n"
        f'Exec="{exe}" auto {LOGIN_FLAG}\n'
        "Terminal=true\n"
        "X-GNOME-Autostart-enabled=true\n"
    ).encode()


def _sync_file(path: Path, body: bytes | None) -> str | None:
    """Write ``body`` to ``path`` (None removes it); what changed, or None."""
    try:
        current = path.read_bytes()
    except OSError:
        current = None
    if body is None:
        if current is None:
            return None
        path.unlink()
        return "removed"
    if current == body:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return "installed" if current is None else "updated"


def sync(enabled: bool, backup_dir: Path, *, platform: str | None = None) -> str | None:
    """Make the login entry match the setting.

    Returns ``"installed"``, ``"updated"``, ``"removed"`` or None (nothing to
    do). Never raises: a failure is logged and returned as ``"failed: ..."``.
    """
    platform = platform or sys.platform
    exe = aswap_executable() if enabled else None
    if enabled and exe is None:
        _logger.warning("⚠️ Start on login: could not find the aswap executable")
        return "failed: aswap executable not found"
    home = Path.home()
    try:
        if platform == "win32":
            want = windows_command(exe, shutil.which("wt")) if exe else None
            have = _run_key.get(ENTRY_NAME)
            if want is None:
                if have is None:
                    return None
                _run_key.delete(ENTRY_NAME)
                result = "removed"
            elif have == want:
                return None
            else:
                _run_key.set(ENTRY_NAME, want)
                result = "installed" if have is None else "updated"
        elif platform == "darwin":
            result = _sync_file(
                _agent_path(home), _agent_body(exe, backup_dir) if exe else None
            )
        else:
            result = _sync_file(_autostart_path(home), _desktop_body(exe) if exe else None)
    except OSError as e:
        _logger.warning("⚠️ Start on login: could not update the login entry: %s", e)
        return f"failed: {e}"
    if result:
        _logger.info("🚀 Start on login: entry %s", result)
    return result


def skip_reason(enabled: bool, backup_dir: Path, now: float) -> str | None:
    """Why a login launch should exit at once, or None to run."""
    if not enabled:
        return "start on login is off (autoswitch.startOnLogin); not starting"
    try:
        beat = json.loads(
            (Path(backup_dir) / HEARTBEAT_FILENAME).read_text(encoding="utf-8")
        )
        pid, next_by = beat.get("pid"), float(beat["nextBy"])
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None  # no loop stamped, or nothing readable: start
    if pid != os.getpid() and now <= next_by + HEARTBEAT_GRACE_S:
        return "another aswap auto is already checking; not starting a second"
    return None
