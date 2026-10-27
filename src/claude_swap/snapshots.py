"""Snapshots of aswap's state database, and restore from them.

A hard crash on 2026-10-26 left ``sequence.json``, ``weekly_anchors.json``, a
credential and a config backup full of zero bytes. The roster had to be
rebuilt by hand from the config backups. The state now lives in a SQLite
database (``statedb.py``) that a crash cannot tear, but a bad write, a bug or
a failing disk still can, so ``aswap auto`` takes a snapshot per
``SNAPSHOT_INTERVAL_S`` and heals any damaged document from the newest
snapshot holding a valid copy, and ``aswap backup`` does the same on demand.

Each snapshot is ``<backup_dir>/snapshots/state-<UTC stamp>.db``, a
self-contained copy made with SQLite's online backup (consistent while other
processes write). It also carries the session profiles' credential files
(``FILE_PATTERNS``), which stay plain files because Claude Code reads them.

Snapshots from before the database (``<stamp>.json`` manifests over
``objects/<sha256>``) are still read when the database is first filled, to
recover a file that was already damaged; ``prune`` lets them age out.
"""

from __future__ import annotations

import base64
import binascii
import calendar
import contextlib
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from claude_swap import statedb
from claude_swap.fsutil import replace_with_retry

SNAPSHOT_INTERVAL_S = 3600
KEEP_RECENT = 48
KEEP_DAILY_DAYS = 30

# Plain files (relative globs) snapshotted beside the database's documents.
FILE_PATTERNS = ("sessions/*/.credentials.json",)

_PREFIX = "state-"

# Documents and files that belong to one account slot: (number, email or
# email slug). A restore of missing documents brings these back only while
# the slot is still in the roster, so a removed account stays removed.
_CLAUDE_SLOT_DOCS = (
    re.compile(r"credentials/\.creds-(\d+)-(.+)\.enc(?:\.prev)?"),
    re.compile(r"configs/\.claude-config-(\d+)-(.+)\.json"),
)
_CLAUDE_SESSION_FILE = re.compile(r"sessions/(\d+)-([^/]+)/\.credentials\.json")
_CODEX_SLOT_DOC = re.compile(r"codex/credentials/account-(\d+)\.json")


def _stamp(t: float) -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(t))


def _stamp_time(stamp: str) -> float:
    return float(calendar.timegm(time.strptime(stamp, "%Y%m%dT%H%M%SZ")))


def valid(rel: str, body: bytes) -> bool:
    """Whether a document body is usable: not empty, not zero-filled, and parsable."""
    if not body.strip(b"\x00 \t\r\n"):
        return False
    try:
        if rel.endswith((".enc", ".enc.prev")):
            body = base64.b64decode(body, validate=True)
        json.loads(body.decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return False
    return True


def _read_snapshot(path: Path) -> dict[str, bytes]:
    """Every document in a snapshot file; {} when it cannot be read."""
    try:
        conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    except (sqlite3.Error, ValueError):
        return {}
    try:
        rows = conn.execute("SELECT path, body FROM docs").fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    return {p: bytes(b) for p, b in rows}


def _private(path: Path) -> None:
    if sys.platform != "win32":
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)


# Windows: ERROR_SHARING_VIOLATION, ERROR_LOCK_VIOLATION. Another process
# holds the file open (SQLite opens without FILE_SHARE_DELETE).
_IN_USE_WINERRORS = (32, 33)


class DatabaseInUseError(OSError):
    """``state.db`` could not be moved because another process has it open."""


def _in_use(e: OSError) -> bool:
    return getattr(e, "winerror", None) in _IN_USE_WINERRORS


@dataclass
class RestoreResult:
    path: str
    stamp: str


def _roster_accounts(body: bytes | None) -> dict:
    """The ``accounts`` map of a sequence document, {} when unusable."""
    try:
        accounts = json.loads(body.decode("utf-8-sig")).get("accounts") if body else None
    except (ValueError, UnicodeDecodeError, AttributeError):
        return {}
    return accounts if isinstance(accounts, dict) else {}


def _in_roster(rel: str, claude: dict, codex: dict) -> bool:
    """Whether ``rel`` is global state or belongs to a slot still in a roster.

    A slot matches on number AND email: a number reused by a newer account
    must not get the removed account's credentials back.
    """
    from claude_swap.session import slugify_email

    def email_of(num: str) -> str | None:
        account = claude.get(num)
        email = account.get("email") if isinstance(account, dict) else None
        return email if isinstance(email, str) else None

    for pattern in _CLAUDE_SLOT_DOCS:
        m = pattern.fullmatch(rel)
        if m:
            return email_of(m[1]) == m[2]
    m = _CLAUDE_SESSION_FILE.fullmatch(rel)
    if m:
        email = email_of(m[1])
        return email is not None and slugify_email(email) == m[2]
    m = _CODEX_SLOT_DOC.fullmatch(rel)
    if m:
        return m[1] in codex
    return True


class SnapshotStore:
    def __init__(self, backup_dir: Path):
        self.backup_dir = Path(backup_dir)
        self.root = self.backup_dir / "snapshots"
        self.objects = self.root / "objects"  # pre-database snapshots
        self.db = statedb.open_db(self.backup_dir)

    # -- reading ---------------------------------------------------------

    def _path(self, stamp: str) -> Path:
        return self.root / f"{_PREFIX}{stamp}.db"

    def stamps(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.stem[len(_PREFIX):] for p in self.root.glob(f"{_PREFIX}*.db"))

    def docs(self, stamp: str) -> dict[str, bytes]:
        return _read_snapshot(self._path(stamp))

    def current(self) -> dict[str, bytes | None]:
        """Every tracked document and file now, ``None`` for an unreadable file."""
        out: dict[str, bytes | None] = {rel: body for rel, body, _t in self.db.items()}
        for pattern in FILE_PATTERNS:
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

    def newest_valid(self, rel: str, at: str | None = None) -> tuple[str, bytes] | None:
        """``(stamp, body)`` of the newest valid copy of ``rel`` at or before ``at``.

        Looks through the database snapshots, then the pre-database ones.
        """
        for stamp in reversed([s for s in self.stamps() if at is None or s <= at]):
            body = self.docs(stamp).get(rel)
            if body is not None and valid(rel, body):
                return stamp, body
        for stamp in reversed([s for s in self._legacy_stamps() if at is None or s <= at]):
            digest = self._legacy_manifest(stamp).get(rel)
            body = self._legacy_object(digest) if digest else None
            if body is not None and valid(rel, body):
                return stamp, body
        return None

    # -- writing ---------------------------------------------------------

    def take(self, now: float | None = None) -> str | None:
        """Snapshot the database (and session credentials); None if unchanged."""
        now = time.time() if now is None else now
        files = {}
        for pattern in FILE_PATTERNS:
            for path in self.backup_dir.glob(pattern):
                try:
                    body = path.read_bytes()
                except OSError:
                    continue
                rel = path.relative_to(self.backup_dir).as_posix()
                if valid(rel, body):  # never let a damaged file displace a good copy
                    files[rel] = body
        stamps = self.stamps()
        if stamps:
            now_docs = {rel: body for rel, body, _t in self.db.items()} | files
            if self.docs(stamps[-1]) == now_docs:
                return None
        stamp = _stamp(now)
        dest = self._path(stamp)
        self.root.mkdir(parents=True, exist_ok=True)
        if sys.platform != "win32":
            os.chmod(self.root, 0o700)
        self.db.backup_to(dest)
        if files:
            conn = sqlite3.connect(dest)
            try:
                with conn:
                    conn.executemany(
                        "INSERT OR REPLACE INTO docs(path, body, updated_at) VALUES(?, ?, ?)",
                        [(rel, body, now) for rel, body in files.items()],
                    )
            finally:
                conn.close()
        _private(dest)
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
                self._path(s).unlink(missing_ok=True)
        self._prune_legacy(now)

    def restore(self, *, paths: list[str] | None = None, at: str | None = None,
                missing: bool = False, dry_run: bool = False) -> list[RestoreResult]:
        """Put back each path from the newest snapshot (at or before ``at``) with a valid copy.

        ``paths`` defaults to the damaged documents, plus with ``missing``
        the ones :meth:`missing` says belong to the current roster.
        ``missing`` is for a person recovering by hand: the hourly heal
        leaves it off, since a document is normally missing because its
        account was removed. Paths with no valid copy anywhere are skipped.
        Database documents are restored in one transaction.
        """
        if paths is None:
            targets = self.damaged()
            if missing:
                targets += self.missing(at)[0]
        else:
            targets = paths
        found: list[tuple[str, str, bytes]] = []
        for rel in targets:
            hit = self.newest_valid(rel, at)
            if hit is not None:
                found.append((rel, hit[0], hit[1]))
        if not dry_run and found:
            with self.db.transaction():
                for rel, _stamp_, body in found:
                    if statedb.is_doc(rel):
                        self.db.write(rel, body)
            for rel, _stamp_, body in found:
                if not statedb.is_doc(rel):
                    _write_file(self.backup_dir / rel, body)
        return [RestoreResult(rel, stamp) for rel, stamp, _b in found]

    def missing(self, at: str | None = None) -> tuple[list[str], list[str]]:
        """Paths the newest snapshot (at or before ``at``) has and the store
        lacks, split into ``(restorable, removed)``.

        Removed are those of account slots no longer in the roster: the
        account was most likely removed on purpose, and bringing back its
        credentials would resurrect it. The roster is the current sequence
        document, or the snapshot's copy when the current one is itself
        missing or damaged (and so about to be restored).
        """
        stamps = [s for s in self.stamps() if at is None or s <= at]
        if not stamps:
            return [], []
        absent = sorted(set(self.docs(stamps[-1])) - set(self.current()))
        if not absent:
            return [], []

        def roster(rel: str) -> dict:
            body = self.db.read(rel)
            if body is None or not valid(rel, body):
                hit = self.newest_valid(rel, at)
                body = hit[1] if hit else None
            return _roster_accounts(body)

        claude, codex = roster("sequence.json"), roster("codex/sequence.json")
        keep = [rel for rel in absent if _in_roster(rel, claude, codex)]
        return keep, [rel for rel in absent if rel not in keep]

    def replace_database(self, at: str | None = None) -> str | None:
        """Move an unusable ``state.db`` aside and rebuild it from a snapshot.

        For a database that will not open or fails its integrity check.
        Every aswap process must be stopped first: on Windows the move fails
        while one holds the file open, which raises DatabaseInUseError. A
        failed move puts back whatever it had already moved, so the database
        is never left split from its ``-wal``. Returns the stamp used, or
        None when there is no snapshot.
        """
        stamps = [s for s in self.stamps() if at is None or s <= at]
        if not stamps:
            return None
        stamp = stamps[-1]
        statedb.close(self.backup_dir)
        aside = f".damaged-{int(time.time())}"
        moved: list[tuple[Path, Path]] = []
        try:
            for suffix in ("", "-wal", "-shm"):
                src = Path(f"{self.db.path}{suffix}")
                if src.exists():
                    dst = Path(f"{self.db.path}{suffix}{aside}")
                    os.replace(src, dst)
                    moved.append((src, dst))
        except OSError as e:
            for src, dst in reversed(moved):
                with contextlib.suppress(OSError):
                    os.replace(dst, src)
            if _in_use(e):
                raise DatabaseInUseError(e.errno, f"{self.db.path} is in use by another process",
                                         str(self.db.path)) from e
            raise
        self.db = statedb.open_db(self.backup_dir)
        docs = self.docs(stamp)
        with self.db.transaction():
            for rel, body in docs.items():
                if statedb.is_doc(rel):
                    self.db.write(rel, body)
        return stamp

    def export(self, dest: Path) -> list[str]:
        """Write every document out as the file it used to be, under ``dest``."""
        dest = Path(dest)
        written = []
        for rel, body, _t in self.db.items():
            _write_file(dest / rel, body)
            written.append(rel)
        return written

    # -- pre-database snapshots ---------------------------------------------

    def _legacy_stamps(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.stem for p in self.root.glob("*.json"))

    def _legacy_manifest(self, stamp: str) -> dict[str, str]:
        try:
            data = json.loads((self.root / f"{stamp}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        files = data.get("files") if isinstance(data, dict) else None
        return files if isinstance(files, dict) else {}

    def _legacy_object(self, digest: str) -> bytes | None:
        try:
            body = (self.objects / digest).read_bytes()
        except OSError:
            return None
        return body if hashlib.sha256(body).hexdigest() == digest else None

    def _prune_legacy(self, now: float) -> None:
        """Drop pre-database snapshots once they are older than a month."""
        legacy = self._legacy_stamps()
        for s in legacy:
            try:
                expired = now - _stamp_time(s) > KEEP_DAILY_DAYS * 86400
            except ValueError:
                continue  # not one of ours
            if expired:
                (self.root / f"{s}.json").unlink(missing_ok=True)
        live = {d for s in self._legacy_stamps() for d in self._legacy_manifest(s).values()}
        if self.objects.is_dir():
            for obj in self.objects.iterdir():
                if obj.name not in live and not obj.name.endswith(".tmp"):
                    obj.unlink(missing_ok=True)
            with contextlib.suppress(OSError):
                self.objects.rmdir()  # only once empty


def _write_file(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(body)
    _private(tmp)
    replace_with_retry(tmp, path)


def maintain(backup_dir: Path, logger, *, now: float | None = None) -> None:
    """Import stray files, heal damage, then snapshot if due. Never raises."""
    try:
        store = SnapshotStore(backup_dir)
        # An aswap from before the database, still running after an upgrade,
        # keeps writing files; carry those writes over.
        store.db.import_files()
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
    """``aswap backup [take|list|check|restore|export]``."""
    import argparse

    from claude_swap.switcher import ClaudeAccountSwitcher

    parser = argparse.ArgumentParser(
        prog="aswap backup",
        description="Snapshots of accounts, credentials and settings; restore after a crash.",
    )
    sub = parser.add_subparsers(dest="action")
    sub.add_parser("take", help="snapshot now (the default)")
    sub.add_parser("list", help="list snapshots")
    sub.add_parser("check", help="check the state database and list damaged documents")
    p = sub.add_parser(
        "restore",
        help="restore damaged documents, and missing ones of accounts still in the "
             "roster, from the newest valid snapshot (--all: every document)",
    )
    p.add_argument("--at", metavar="STAMP", help="use snapshots at or before this stamp (see list)")
    p.add_argument("--all", action="store_true",
                   help="restore every document in the snapshot, removed accounts included")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("export", help="write every document out as plain files into DIR")
    p.add_argument("dir", metavar="DIR")
    args = parser.parse_args(argv)

    store = SnapshotStore(ClaudeAccountSwitcher().backup_dir)
    action = args.action or "take"
    if action == "take":
        stamp = store.take()
        print(f"Snapshot {stamp} taken" if stamp else "Nothing changed since the last snapshot")
    elif action == "list":
        for s in store.stamps():
            print(f"{s}  {len(store.docs(s))} documents")
    elif action == "check":
        try:
            problems = [r for r in store.db.integrity_check() if r != "ok"]
        except statedb.StateDBError as e:
            problems = [str(e)]
        if problems:
            print(f"{store.db.path} failed its integrity check:")
            for line in problems[:20]:
                print(f"  {line}")
            print("Stop every aswap process, then run `aswap backup restore`.")
            return
        bad = store.damaged()
        print("\n".join(bad) if bad else "No damaged documents")
    elif action == "export":
        dest = Path(args.dir).expanduser()
        written = store.export(dest)
        print(f"Exported {len(written)} documents to {dest}")
        if written:
            print("They include credentials: keep the directory private and delete it when done.")
    else:
        try:
            healthy = store.db.integrity_check() == ["ok"]
        except statedb.StateDBError:
            healthy = False
        if not healthy:
            if args.dry_run:
                stamps = [s for s in store.stamps() if args.at is None or s <= args.at]
                print(f"Would rebuild the damaged database from {stamps[-1]}"
                      if stamps else "The database is damaged and there is no snapshot")
                return
            try:
                stamp = store.replace_database(args.at)
            except DatabaseInUseError:
                print(f"{store.db.path} is open in another aswap process, so it cannot be "
                      "replaced. Stop aswap auto and the dashboard, then retry.",
                      file=sys.stderr)
                sys.exit(1)
            except OSError as e:
                print(f"Could not move the damaged database aside ({e}). "
                      "Stop aswap auto and the dashboard, then retry.", file=sys.stderr)
                sys.exit(1)
            print(f"Rebuilt the database from snapshot {stamp}; the damaged copy was kept "
                  "beside it" if stamp else "The database is damaged and there is no snapshot")
            if stamp:
                print("Restart `aswap auto` so it rereads the restored state.")
            return
        paths = None
        removed: list[str] = []
        if args.all:
            stamps = [s for s in store.stamps() if args.at is None or s <= args.at]
            paths = sorted(store.docs(stamps[-1])) if stamps else []
        else:
            removed = store.missing(args.at)[1]
        done = store.restore(paths=paths, at=args.at, missing=True, dry_run=args.dry_run)
        verb = "Would restore" if args.dry_run else "Restored"
        for r in done:
            print(f"{verb} {r.path} from {r.stamp}")
        if not done:
            print("Nothing to restore")
        if removed:
            print(f"Left out {len(removed)} documents of accounts no longer in the roster "
                  "(--all brings them back)")
        if done and not args.dry_run:
            print("Restart `aswap auto` so it rereads the restored state.")
