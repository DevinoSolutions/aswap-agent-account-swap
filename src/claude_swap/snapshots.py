"""Validated, deduplicated snapshots of aswap's state, and restore from them.

A hard crash on 2026-10-26 left ``sequence.json``, ``weekly_anchors.json``, a
credential and a config backup full of zero bytes. The roster had to be
rebuilt by hand from the config backups. Snapshots make that a one-command
restore: ``aswap auto`` takes one per ``SNAPSHOT_INTERVAL_S`` and heals any
damaged file from the newest snapshot holding a valid copy, and
``aswap backup`` does the same on demand.

Layout under ``<backup_dir>/snapshots/``: ``objects/<sha256>`` holds each
distinct file body once, and ``<UTC stamp>.json`` maps relative paths to
objects. Only files that pass validation are recorded, so a damaged file
never displaces its last good copy.
"""

from __future__ import annotations

import base64
import binascii
import calendar
import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from claude_swap.fsutil import replace_with_retry

SNAPSHOT_INTERVAL_S = 3600
KEEP_RECENT = 48
KEEP_DAILY_DAYS = 30

# Relative globs of the state worth keeping. Caches, logs and session
# profiles' plugin trees regenerate on their own and are left out.
PATTERNS = (
    "sequence.json",
    "weekly_anchors.json",
    "billing_sessions.json",
    "settings.json",
    "mappings.json",
    "warmup_state.json",
    "autoswitch_state.json",
    "credentials/.creds-*.enc",
    "configs/.claude-config-*.json",
    "codex/sequence.json",
    "codex/credentials/*.json",
    "sessions/*/.credentials.json",
)


def _stamp(t: float) -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(t))


def _stamp_time(stamp: str) -> float:
    return float(calendar.timegm(time.strptime(stamp, "%Y%m%dT%H%M%SZ")))


def valid(rel: str, body: bytes) -> bool:
    """Whether a file body is usable: not empty, not zero-filled, and parsable."""
    if not body.strip(b"\x00 \t\r\n"):
        return False
    try:
        if rel.endswith(".enc"):
            body = base64.b64decode(body, validate=True)
        json.loads(body.decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return False
    return True


@dataclass
class RestoreResult:
    path: str
    stamp: str


class SnapshotStore:
    def __init__(self, backup_dir: Path):
        self.backup_dir = Path(backup_dir)
        self.root = self.backup_dir / "snapshots"
        self.objects = self.root / "objects"

    # -- reading ---------------------------------------------------------

    def stamps(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.stem for p in self.root.glob("*.json"))

    def manifest(self, stamp: str) -> dict[str, str]:
        try:
            data = json.loads((self.root / f"{stamp}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        files = data.get("files") if isinstance(data, dict) else None
        return files if isinstance(files, dict) else {}

    def _object(self, digest: str) -> bytes | None:
        try:
            body = (self.objects / digest).read_bytes()
        except OSError:
            return None
        return body if hashlib.sha256(body).hexdigest() == digest else None

    def current(self) -> dict[str, bytes | None]:
        """Every tracked file on disk now, ``None`` for an unreadable one."""
        out: dict[str, bytes | None] = {}
        for pattern in PATTERNS:
            for path in self.backup_dir.glob(pattern):
                rel = path.relative_to(self.backup_dir).as_posix()
                try:
                    out[rel] = path.read_bytes()
                except OSError:
                    out[rel] = None
        return out

    def damaged(self) -> list[str]:
        return sorted(rel for rel, body in self.current().items()
                      if body is None or not valid(rel, body))

    # -- writing ---------------------------------------------------------

    def _write(self, path: Path, body: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_bytes(body)
        replace_with_retry(tmp, path)

    def take(self, now: float | None = None) -> str | None:
        """Snapshot every valid tracked file; returns the stamp, or None if unchanged."""
        now = time.time() if now is None else now
        files: dict[str, str] = {}
        for rel, body in self.current().items():
            if body is None or not valid(rel, body):
                continue
            digest = hashlib.sha256(body).hexdigest()
            if not (self.objects / digest).exists():
                self._write(self.objects / digest, body)
            files[rel] = digest
        stamps = self.stamps()
        if stamps and self.manifest(stamps[-1]) == files:
            return None
        stamp = _stamp(now)
        self._write(self.root / f"{stamp}.json",
                    json.dumps({"created": stamp, "files": files}, indent=1).encode())
        self.prune(now)
        return stamp

    def prune(self, now: float | None = None) -> None:
        """Keep the newest ``KEEP_RECENT`` plus the first of each day for a month."""
        now = time.time() if now is None else now
        stamps = self.stamps()
        keep = set(stamps[-KEEP_RECENT:])
        seen_days: set[str] = set()
        for s in stamps:
            if now - _stamp_time(s) <= KEEP_DAILY_DAYS * 86400 and s[:8] not in seen_days:
                seen_days.add(s[:8])
                keep.add(s)
        for s in stamps:
            if s not in keep:
                (self.root / f"{s}.json").unlink(missing_ok=True)
        live = {d for s in keep for d in self.manifest(s).values()}
        if self.objects.is_dir():
            for obj in self.objects.iterdir():
                if obj.name not in live and not obj.name.endswith(".tmp"):
                    obj.unlink(missing_ok=True)

    def restore(self, *, paths: list[str] | None = None, at: str | None = None,
                dry_run: bool = False) -> list[RestoreResult]:
        """Put back each path from the newest snapshot (at or before ``at``) with a valid copy.

        ``paths`` defaults to the damaged files. Paths with no valid copy in
        any snapshot are skipped.
        """
        targets = self.damaged() if paths is None else paths
        stamps = [s for s in self.stamps() if at is None or s <= at]
        done: list[RestoreResult] = []
        for rel in targets:
            for stamp in reversed(stamps):
                digest = self.manifest(stamp).get(rel)
                body = self._object(digest) if digest else None
                if body is not None and valid(rel, body):
                    if not dry_run:
                        self._write(self.backup_dir / rel, body)
                    done.append(RestoreResult(rel, stamp))
                    break
        return done


def maintain(backup_dir: Path, logger, *, now: float | None = None) -> None:
    """Heal damaged files, then snapshot if the last one is due. Never raises."""
    try:
        store = SnapshotStore(backup_dir)
        for r in store.restore():
            logger.warning("Restored damaged %s from snapshot %s", r.path, r.stamp)
        now = time.time() if now is None else now
        checked = store.root / ".checked"
        try:
            last = checked.stat().st_mtime
        except OSError:
            last = 0.0
        if now - last < SNAPSHOT_INTERVAL_S:
            return
        stamp = store.take(now)
        checked.parent.mkdir(parents=True, exist_ok=True)
        checked.touch()
        os.utime(checked, (now, now))
        if stamp:
            logger.info("Snapshot %s taken", stamp)
    except Exception as e:  # pragma: no cover - never let backups break switching
        logger.warning("Snapshot maintenance failed: %s: %s", type(e).__name__, e)


def backup_command(argv: list[str]) -> None:
    """``aswap backup [take|list|check|restore]``."""
    import argparse

    from claude_swap.switcher import ClaudeAccountSwitcher

    parser = argparse.ArgumentParser(
        prog="aswap backup",
        description="Snapshots of accounts, credentials and settings; restore after a crash.",
    )
    sub = parser.add_subparsers(dest="action")
    sub.add_parser("take", help="snapshot now (the default)")
    sub.add_parser("list", help="list snapshots")
    sub.add_parser("check", help="list damaged state files")
    p = sub.add_parser("restore", help="restore damaged files (or --all) from the newest valid snapshot")
    p.add_argument("--at", metavar="STAMP", help="use snapshots at or before this stamp (see list)")
    p.add_argument("--all", action="store_true", help="restore every file in the snapshot, not just damaged ones")
    p.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    store = SnapshotStore(ClaudeAccountSwitcher().backup_dir)
    action = args.action or "take"
    if action == "take":
        stamp = store.take()
        print(f"Snapshot {stamp} taken" if stamp else "Nothing changed since the last snapshot")
    elif action == "list":
        for s in store.stamps():
            print(f"{s}  {len(store.manifest(s))} files")
    elif action == "check":
        bad = store.damaged()
        print("\n".join(bad) if bad else "No damaged files")
    else:
        paths = None
        if args.all:
            stamps = [s for s in store.stamps() if args.at is None or s <= args.at]
            paths = sorted(store.manifest(stamps[-1])) if stamps else []
        done = store.restore(paths=paths, at=args.at, dry_run=args.dry_run)
        verb = "Would restore" if args.dry_run else "Restored"
        for r in done:
            print(f"{verb} {r.path} from {r.stamp}")
        if not done:
            print("Nothing to restore")
        if done and not args.dry_run:
            print("Restart `aswap auto` so it rereads the restored files.")
