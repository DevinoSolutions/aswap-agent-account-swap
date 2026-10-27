"""aswap's durable state as documents in one SQLite database.

A hard crash on 2026-10-26 zero-filled ``sequence.json``, ``weekly_anchors.json``,
a credential and a config backup at once. Each had been written to a temp
file and renamed without an fsync, so the renames survived and the data did
not. Fsyncing every rename (``fsutil.replace_with_retry``) and hourly
snapshots (``snapshots.py``) narrowed that; this module removes it. The state
lives in ``<backup_dir>/state.db`` in WAL mode with ``synchronous=FULL``, so a
crash can only lose a transaction that had not committed, never damage one
that had.

Each former file is a row in ``docs`` keyed by its path relative to the
backup root (``sequence.json``, ``credentials/.creds-3-a@b.com.enc``), with
the exact bytes the file held. Keeping the file shape means ``aswap backup
export`` can write the files back out unchanged, and an old aswap can be
pointed at them by hand.

Two layers:

- :class:`StateDB` is the store: read, write, delete, list, and a
  transaction context for multi-document commits.
- The path functions (:func:`read_text`, :func:`try_write`, :func:`exists`,
  ...) take the absolute paths the rest of the code already uses. A path that
  names a state document under the backup root goes to the database; any
  other path (``~/.claude.json``, a session profile, a log) is a plain file,
  exactly as before. That is what lets the ~60 callers of the roster and the
  stores keep their paths.

Several processes share the database (``aswap auto``, the TUI, one-off
commands, the statusline). Each thread of each process gets its own
connection; writers take the write lock up front (``BEGIN IMMEDIATE``) so a
busy database is waited out by SQLite's busy handler instead of failing
mid-transaction, and the few cases the handler does not cover are retried
here. Every SQLite failure surfaces as :class:`StateDBError`, an ``OSError``,
so the many ``except OSError`` arms written for unreadable files treat an
unreadable database the same way.
"""

from __future__ import annotations

import contextlib
import fnmatch
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Iterator
from pathlib import Path

from claude_swap.fsutil import read_bytes_with_retry, read_text_with_retry, replace_with_retry

_logger = logging.getLogger("claude-swap")

DB_NAME = "state.db"
SCHEMA_VERSION = 1
MIGRATED_SUFFIX = ".migrated"
# SQLite's own wait for a lock, per statement.
BUSY_TIMEOUT_MS = 10_000
# Our retry budget on top of it, for the busy cases SQLite returns at once
# (opening a WAL database while another connection recovers it, a checkpoint
# racing a writer).
RETRY_BUDGET_S = 30.0

# Relative paths (posix, ``fnmatch`` per segment) that are state documents.
# Left out on purpose: ``live_usage/`` (written by the Node statusline),
# ``sessions/`` (profile dirs Claude Code itself reads), logs, ``*.lock``,
# the unclaimed-credential stash and swap staging (write-once forensic
# copies whose failure must abort a switch on their own terms), and anything
# outside the backup root.
DOC_PATTERNS = (
    "sequence.json",
    "weekly_anchors.json",
    "billing_sessions.json",
    "settings.json",
    "autoswitch_state.json",
    "codex_autoswitch_state.json",
    "warmup_state.json",
    "mappings.json",
    "menubar_settings.json",
    ".migrations.json",
    "credentials/.creds-*.enc",
    "credentials/.creds-*.enc.prev",
    "configs/.claude-config-*.json",
    "cache/*.json",
    "codex/sequence.json",
    "codex/credentials/*.json",
)


class StateDBError(OSError):
    """The state database could not be read or written.

    An ``OSError`` so that code written for files (``except OSError``) treats
    a locked, unreadable or damaged database as an unreadable file rather
    than letting a raw ``sqlite3`` error escape.
    """


def is_doc(rel: str) -> bool:
    """Whether a backup-root-relative posix path is a state document."""
    parts = rel.split("/")
    for pattern in DOC_PATTERNS:
        pp = pattern.split("/")
        if len(pp) == len(parts) and all(
            fnmatch.fnmatchcase(part, p) for part, p in zip(parts, pp)
        ):
            return True
    return False


def _busy(e: sqlite3.Error) -> bool:
    code = getattr(e, "sqlite_errorcode", None)
    if code is not None:
        return code & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
    msg = str(e).lower()
    return "locked" in msg or "busy" in msg


class StateDB:
    """Document store over ``<root>/state.db``.

    Opened lazily: constructing one touches nothing, and reads against a
    database that does not exist yet answer "absent" without creating it, so
    a no-op command on a fresh install leaves no backup dir behind (the same
    lazy-dir rule the logger follows). The first write creates it.

    Before the first read or write, any state files still on disk are
    imported (see :meth:`import_files`).
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.path = self.root / DB_NAME
        self._tls = threading.local()
        self._lock = threading.Lock()
        self._conns: list[sqlite3.Connection] = []
        self._generation = 0
        # The first-use import (see ensure_imported). An RLock so the
        # importing thread's own connections pass while the others wait.
        self._import_lock = threading.RLock()
        self._importing = False
        self._import_done = threading.Event()

    # -- connections -------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        self.root.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            self.path,
            timeout=BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,  # we issue BEGIN/COMMIT ourselves
            check_same_thread=False,  # close_all() closes from another thread
        )
        try:
            self._retry(lambda: self._configure(conn))
        except BaseException:
            conn.close()
            raise
        if os.name != "nt":
            for p in (self.path, Path(f"{self.path}-wal"), Path(f"{self.path}-shm")):
                with contextlib.suppress(OSError):
                    os.chmod(p, 0o600)
        return conn

    @staticmethod
    def _configure(conn: sqlite3.Connection) -> None:
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            raise sqlite3.OperationalError(f"could not enable WAL (got {mode})")
        # FULL: the WAL is synced at every commit, so a committed transaction
        # survives a power loss. NORMAL only promises consistency.
        conn.execute("PRAGMA synchronous=FULL")
        if conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS docs("
                    "path TEXT PRIMARY KEY, body BLOB NOT NULL, updated_at REAL NOT NULL)"
                )
                conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def _conn(self, *, create: bool) -> sqlite3.Connection | None:
        """This thread's connection, or None when there is no database and
        ``create`` is false."""
        tls = self._tls
        self.ensure_imported()  # may open this thread's connection itself
        conn = getattr(tls, "conn", None)
        if conn is not None and tls.pid == os.getpid() and tls.gen == self._generation:
            return conn
        if not create and not self.path.exists():
            return None
        conn = self._connect()
        with self._lock:
            self._conns.append(conn)
            tls.conn, tls.pid, tls.gen, tls.depth = conn, os.getpid(), self._generation, 0
        return conn

    def ensure_imported(self) -> None:
        """Run :meth:`import_files` once, before this process's first access.

        Other threads block until it has committed, so none reads the
        pre-import (empty) database; the importing thread itself re-enters
        here through its own transaction and passes.
        """
        if self._import_done.is_set():
            return
        with self._import_lock:
            if not self._import_done.is_set():
                self.import_files()

    def close(self) -> None:
        """Close every connection this process holds (any thread).

        Needed before the database file can be removed or replaced on
        Windows; a thread that uses the store afterwards reconnects.
        """
        with self._lock:
            conns, self._conns = self._conns, []
            self._generation += 1
        for conn in conns:
            with contextlib.suppress(sqlite3.Error):
                conn.close()

    # -- error handling ----------------------------------------------------

    def _retry(self, fn):
        """Run ``fn``, retrying a busy database, mapping errors to StateDBError."""
        deadline = time.monotonic() + RETRY_BUDGET_S
        delay = 0.01
        while True:
            try:
                return fn()
            except sqlite3.Error as e:
                if _busy(e) and time.monotonic() < deadline:
                    time.sleep(delay)
                    delay = min(delay * 2, 0.5)
                    continue
                raise StateDBError(f"{self.path}: {e}") from e

    def _run(self, sql: str, args: tuple = (), *, create: bool = False):
        """One statement on this thread's connection; None when no database."""
        conn = self._conn(create=create)
        if conn is None:
            return None
        if getattr(self._tls, "depth", 0):
            # Inside transaction(): the write lock is already held, and a
            # failed statement must not be retried around the transaction.
            try:
                return conn.execute(sql, args).fetchall()
            except sqlite3.Error as e:
                raise StateDBError(f"{self.path}: {e}") from e
        return self._retry(lambda: conn.execute(sql, args).fetchall())

    @contextlib.contextmanager
    def transaction(self) -> Iterator[StateDB]:
        """Commit several writes atomically. Nested uses join the outer one.

        Takes the write lock at BEGIN, so concurrent writers queue instead
        of failing halfway with a busy error SQLite cannot wait out.
        """
        conn = self._conn(create=True)
        tls = self._tls
        if tls.depth:
            tls.depth += 1
            try:
                yield self
            finally:
                tls.depth -= 1
            return
        self._retry(lambda: conn.execute("BEGIN IMMEDIATE"))
        tls.depth = 1
        try:
            yield self
        except BaseException:
            tls.depth = 0
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise
        tls.depth = 0
        try:
            conn.execute("COMMIT")
        except sqlite3.Error as e:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise StateDBError(f"{self.path}: {e}") from e

    # -- documents ---------------------------------------------------------

    def read(self, rel: str) -> bytes | None:
        rows = self._run("SELECT body FROM docs WHERE path=?", (rel,))
        return bytes(rows[0][0]) if rows else None

    def updated_at(self, rel: str) -> float | None:
        """When the document was last written (its mtime), None if absent."""
        rows = self._run("SELECT updated_at FROM docs WHERE path=?", (rel,))
        return rows[0][0] if rows else None

    def exists(self, rel: str) -> bool:
        return bool(self._run("SELECT 1 FROM docs WHERE path=?", (rel,)))

    def write(self, rel: str, body: bytes | str, *, updated_at: float | None = None) -> None:
        if isinstance(body, str):
            body = body.encode("utf-8")
        now = time.time() if updated_at is None else updated_at
        # updated_at stands in for a file mtime, and callers detect changes
        # by comparing it: keep it strictly increasing per document even
        # when two writes land within the clock's resolution.
        self._run(
            "INSERT INTO docs(path, body, updated_at) VALUES(?, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET body=excluded.body, "
            "updated_at=MAX(excluded.updated_at, docs.updated_at + 0.000001)",
            (rel, sqlite3.Binary(body), now),
            create=True,
        )

    def delete(self, rel: str) -> bool:
        """Remove a document; True when it was there."""
        if self._conn(create=False) is None:
            return False
        with self.transaction():
            existed = bool(self._run("SELECT 1 FROM docs WHERE path=?", (rel,)))
            self._run("DELETE FROM docs WHERE path=?", (rel,))
        return existed

    def list(self, prefix: str = "") -> list[str]:
        rows = self._run("SELECT path FROM docs ORDER BY path") or []
        return [r[0] for r in rows if r[0].startswith(prefix)]

    def items(self) -> list[tuple[str, bytes, float]]:
        """Every document as ``(path, body, updated_at)``, sorted by path."""
        rows = self._run("SELECT path, body, updated_at FROM docs ORDER BY path") or []
        return [(p, bytes(b), t) for p, b, t in rows]

    # -- maintenance -------------------------------------------------------

    def integrity_check(self) -> list[str]:
        """``PRAGMA integrity_check``: ``["ok"]`` for a sound database."""
        rows = self._run("PRAGMA integrity_check")
        return [r[0] for r in rows] if rows else ["ok"]

    def backup_to(self, dest: Path) -> None:
        """Copy the database, consistently, to ``dest`` (overwritten)."""
        conn = self._conn(create=True)
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f".{dest.name}.{os.getpid()}.tmp")

        def copy() -> None:
            tmp.unlink(missing_ok=True)
            out = sqlite3.connect(tmp)
            try:
                conn.backup(out)
                # A self-contained single file, not a WAL pair.
                out.execute("PRAGMA journal_mode=DELETE")
            finally:
                out.close()

        try:
            self._retry(copy)
            replace_with_retry(tmp, dest)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    # -- import of the pre-database files ----------------------------------

    def state_files(self) -> list[Path]:
        """State documents still lying on disk as plain files.

        A symlinked file stays a file: a link (a dotfiles deploy of
        ``settings.json``) is a request to keep the state at its target,
        and ``atomic_write_json`` writes through it for that reason.
        """
        found: list[Path] = []
        for pattern in DOC_PATTERNS:
            for path in self.root.glob(pattern):
                if path.is_file() and not path.is_symlink():
                    found.append(path)
        return sorted(found)

    def import_files(self) -> list[str]:
        """Import any state files on disk (see :meth:`_import_files`).

        Re-entry from the importing thread's own transaction is a no-op.
        A failure is not marked done, so every access raises until an
        import succeeds: serving the database without the files would read
        as "no accounts", and the next write would build a fresh roster
        over the real one.
        """
        with self._import_lock:
            if self._importing:
                return []
            self._importing = True
            try:
                imported = self._import_files()
            finally:
                self._importing = False
            self._import_done.set()
            return imported

    def _import_files(self) -> list[str]:
        """Move every valid state file on disk into the database.

        Runs before this process first touches the database, and again from
        ``snapshots.maintain``. The common case, nothing on disk, costs a
        few globs.

        Per file:

        - valid (non-empty, not zero-filled, parses; ``.enc`` base64 of
          JSON) and newer than the document, or no document yet: imported.
          An aswap from before the database, still running after an upgrade,
          keeps writing files; newest-wins carries those writes over.
        - valid but older than the document: already imported.
        - invalid, with no document yet: the newest valid copy in the
          pre-database snapshots is imported instead, when there is one.
          This is the 2026-10-26 case, a zero-filled roster.
        - invalid otherwise: skipped with a warning and left in place.

        All imports commit in one transaction. Only then are the imported
        and already-imported files renamed to ``<name>.migrated`` (not
        deleted), so an older aswap can be restored by renaming them back.
        A crash in between leaves files that the next run finds already
        imported. Two processes starting at once serialize on a file lock;
        the second finds the files renamed.

        Returns the imported document paths.
        """
        if not self.state_files():
            return []
        from claude_swap.locking import FileLock
        from claude_swap.snapshots import SnapshotStore, valid

        imported: list[str] = []
        # Documents a file overwrote after they were already in the database:
        # only an aswap from before the database writes those files.
        stale_writers: list[str] = []
        lock = FileLock(self.root / ".statedb-import.lock", timeout=60)
        if not lock.acquire():
            raise StateDBError(f"{self.root}: timed out waiting for another state import")
        try:
            files = self.state_files()
            if not files:
                return []
            settled: list[Path] = []
            legacy = SnapshotStore(self.root)
            with self.transaction():
                for path in files:
                    rel = path.relative_to(self.root).as_posix()
                    # A file that cannot be read aborts the import (and so
                    # every access, until it can be): serving the database
                    # without it would read as that state being absent.
                    body = read_bytes_with_retry(path)
                    mtime = path.stat().st_mtime
                    current = self.updated_at(rel)
                    if valid(rel, body):
                        if current is None or mtime > current:
                            if current is not None:
                                stale_writers.append(rel)
                            self.write(rel, body)
                            imported.append(rel)
                        settled.append(path)
                        continue
                    if current is None:
                        healed = legacy.newest_valid(rel)
                        if healed is not None:
                            stamp, good = healed
                            self.write(rel, good)
                            imported.append(rel)
                            _logger.warning(
                                "State import: %s is damaged; imported its copy "
                                "from snapshot %s instead", rel, stamp)
                            continue
                    _logger.warning(
                        "State import: skipped damaged %s (left in place)", rel)
            for path in settled:
                try:
                    replace_with_retry(path, path.with_name(path.name + MIGRATED_SUFFIX))
                except OSError as e:
                    _logger.warning("State import: could not rename %s: %s", path, e)
        finally:
            lock.release()
        if stale_writers:
            _logger.warning(
                "State import: %s reappeared as plain file(s) after the move to %s "
                "and replaced the database copy. Unless you exported them there "
                "yourself, an aswap from before %s is still running (aswap auto, "
                "the dashboard): stop it and start the new one.",
                ", ".join(stale_writers), DB_NAME, DB_NAME)
        if imported:
            _logger.info("State import: %d document(s) moved into %s",
                         len(imported), DB_NAME)
        return imported


# -- the path layer ---------------------------------------------------------

_dbs: dict[str, StateDB] = {}
_dbs_lock = threading.Lock()


def _key(root: Path | str) -> str:
    return os.path.normcase(os.path.abspath(root))


def open_db(root: Path | str) -> StateDB:
    """The process-wide store for a backup root (created on first use)."""
    key = _key(root)
    with _dbs_lock:
        db = _dbs.get(key)
        if db is None:
            db = _dbs[key] = StateDB(Path(root))
        return db


def close(root: Path | str) -> None:
    """Close a root's connections (before deleting or replacing its db)."""
    with _dbs_lock:
        db = _dbs.pop(_key(root), None)
    if db is not None:
        db.close()


def close_all() -> None:
    with _dbs_lock:
        dbs = list(_dbs.values())
        _dbs.clear()
    for db in dbs:
        db.close()


# Backup roots that switchers in this process were built over (see manage).
_managed: set[str] = set()


def manage(root: Path | str) -> None:
    """Route state paths under ``root`` to its database from now on.

    A switcher resolves its backup root once, at construction, and keeps
    using it; ``get_backup_root()`` follows ``$HOME``/``XDG_DATA_HOME`` as
    they are at each call. In a real process the two never differ. When they
    can (a test that patches the environment only around construction),
    registering the switcher's root keeps every read and write of its state
    on one side of the file/database line.
    """
    with _dbs_lock:
        _managed.add(os.path.abspath(root))


def reset() -> None:
    """Close every store and forget managed roots (test isolation)."""
    close_all()
    with _dbs_lock:
        _managed.clear()


def locate(path: os.PathLike | str) -> tuple[StateDB, str] | None:
    """The store and document key for ``path``, or None for a plain file.

    A path is a document when it lies under the aswap backup root (resolved
    now, so it follows ``$HOME``/``XDG_DATA_HOME`` like every other path in
    aswap, or registered with :func:`manage`), matches
    :data:`DOC_PATTERNS`, and is not a symlink.
    """
    from claude_swap.paths import get_backup_root

    full = os.path.abspath(path)
    folded = os.path.normcase(full)
    with _dbs_lock:
        roots = {os.path.abspath(get_backup_root()), *_managed}
    for root in sorted(roots, key=len, reverse=True):
        if folded.startswith(os.path.normcase(root) + os.sep):
            rel = full[len(root) + 1:].replace(os.sep, "/")
            if not is_doc(rel) or os.path.islink(full):
                return None
            return open_db(root), rel
    return None


def _missing(path) -> FileNotFoundError:
    return FileNotFoundError(2, "No such state document", str(path))


def read_bytes(path: os.PathLike | str) -> bytes:
    """``Path.read_bytes`` for a state path: FileNotFoundError when absent."""
    hit = locate(path)
    if hit is None:
        return Path(path).read_bytes()
    body = hit[0].read(hit[1])
    if body is None:
        raise _missing(path)
    return body


def read_text(path: os.PathLike | str) -> str:
    """UTF-8 text of a state path (files read with Windows-sharing retries)."""
    hit = locate(path)
    if hit is None:
        return read_text_with_retry(path)
    return read_bytes(path).decode("utf-8")


def exists(path: os.PathLike | str) -> bool:
    """``Path.exists`` for a state path.

    Unlike ``Path.exists``, a database that cannot be read raises instead of
    answering False: "absent" lets callers initialize state from scratch
    (``_init_sequence_file``), which over a live roster would be data loss.
    """
    hit = locate(path)
    if hit is None:
        return Path(path).exists()
    return hit[0].exists(hit[1])


def mtime(path: os.PathLike | str) -> float:
    """``stat().st_mtime`` for a state path: the document's ``updated_at``.

    Raises FileNotFoundError when absent, like ``stat``.
    """
    hit = locate(path)
    if hit is None:
        return Path(path).stat().st_mtime
    t = hit[0].updated_at(hit[1])
    if t is None:
        raise _missing(path)
    return t


def unlink(path: os.PathLike | str, *, missing_ok: bool = False) -> None:
    """``Path.unlink`` for a state path."""
    hit = locate(path)
    if hit is None:
        Path(path).unlink(missing_ok=missing_ok)
        return
    if not hit[0].delete(hit[1]) and not missing_ok:
        raise _missing(path)


def try_write(path: os.PathLike | str, body: bytes | str) -> bool:
    """Write a state document; False when ``path`` is a plain file.

    Writers call this first and fall through to their own atomic file write
    on False, so each keeps its file-mode, symlink and temp-file rules for
    the paths that stay files.
    """
    hit = locate(path)
    if hit is None:
        return False
    hit[0].write(hit[1], body)
    return True
