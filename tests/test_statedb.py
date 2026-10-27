"""The state database: the store, the path layer, the import of old files,
and the helpers that were switched over to it."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from claude_swap import statedb
from claude_swap.exceptions import ConfigError
from claude_swap.statedb import StateDB, StateDBError, is_doc
from tests import statefiles

SRC = str(Path(__file__).resolve().parents[1] / "src")


def _b64(obj) -> bytes:
    return base64.b64encode(json.dumps(obj).encode())


# -- the store ----------------------------------------------------------------


class TestDocPatterns:
    @pytest.mark.parametrize("rel", [
        "sequence.json", "weekly_anchors.json", "billing_sessions.json",
        "settings.json", "autoswitch_state.json", "warmup_state.json",
        "mappings.json", "credentials/.creds-3-a@b.com.enc",
        "credentials/.creds-3-a@b.com.enc.prev",
        "configs/.claude-config-3-a@b.com.json", "cache/usage.json",
        "cache/billing.json", "codex/sequence.json",
        "codex/credentials/account-1.json",
    ])
    def test_state_documents(self, rel):
        assert is_doc(rel)

    @pytest.mark.parametrize("rel", [
        "live_usage/s1.json", "claude-swap.log", ".lock", "credentials/.consume-1.lock",
        "sessions/1-a@b.com/.credentials.json", "credentials/.unclaimed-manifest.json",
        "credentials/.swap-staging-creds-1.json", "sequence.json.migrated",
        "credentials/.creds-1-a.enc.migrated", "cache/sub/x.json", "nested/sequence.json",
        ".migrations.json", "state.db",
    ])
    def test_not_state_documents(self, rel):
        assert not is_doc(rel)


class TestStateDB:
    def test_round_trip_and_listing(self, tmp_path):
        db = StateDB(tmp_path)
        db.write("sequence.json", '{"a": 1}')
        db.write("credentials/.creds-1-a.enc", b"Zm9v")
        assert db.read("sequence.json") == b'{"a": 1}'
        assert db.exists("credentials/.creds-1-a.enc")
        assert db.list() == ["credentials/.creds-1-a.enc", "sequence.json"]
        assert db.list("credentials/") == ["credentials/.creds-1-a.enc"]
        assert db.delete("sequence.json") is True
        assert db.delete("sequence.json") is False
        assert db.read("sequence.json") is None

    def test_reading_creates_nothing(self, tmp_path):
        """A no-op command on a fresh install must not lay down the backup dir."""
        root = tmp_path / "backup"
        db = StateDB(root)
        assert db.read("sequence.json") is None
        assert not db.exists("sequence.json")
        assert db.list() == []
        assert db.delete("sequence.json") is False
        assert not root.exists()

    def test_wal_and_full_sync(self, tmp_path):
        db = StateDB(tmp_path)
        db.write("sequence.json", "{}")
        conn = db._conn(create=False)
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 5000

    def test_updated_at_strictly_increases(self, tmp_path, monkeypatch):
        """It stands in for an mtime that callers compare to spot changes."""
        db = StateDB(tmp_path)
        monkeypatch.setattr(statedb.time, "time", lambda: 1000.0)
        db.write("settings.json", "{}")
        first = db.updated_at("settings.json")
        db.write("settings.json", '{"a": 1}')
        assert db.updated_at("settings.json") > first

    def test_transaction_is_atomic(self, tmp_path):
        db = StateDB(tmp_path)
        db.write("sequence.json", "old")
        with pytest.raises(RuntimeError):
            with db.transaction():
                db.write("sequence.json", "new")
                db.write("mappings.json", "{}")
                raise RuntimeError("boom")
        assert db.read("sequence.json") == b"old"
        assert not db.exists("mappings.json")

        with db.transaction():
            with db.transaction():  # nested joins the outer one
                db.write("sequence.json", "new")
            db.write("mappings.json", "{}")
        assert db.read("sequence.json") == b"new"
        assert db.exists("mappings.json")

    def test_threads_get_their_own_connections(self, tmp_path):
        db = StateDB(tmp_path)
        db.write("sequence.json", "{}")
        errors = []

        def worker(n):
            try:
                for i in range(20):
                    db.write(f"cache/t{n}.json", json.dumps({"i": i}))
                    assert json.loads(db.read(f"cache/t{n}.json"))["i"] == i
            except Exception as e:  # pragma: no cover - reported below
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        assert len(db.list("cache/")) == 4

    def test_waits_out_a_writer_holding_the_lock(self, tmp_path):
        db = StateDB(tmp_path)
        db.write("sequence.json", "{}")
        other = sqlite3.connect(
            tmp_path / "state.db", isolation_level=None, check_same_thread=False
        )
        other.execute("BEGIN IMMEDIATE")
        other.execute("UPDATE docs SET body=? WHERE path='sequence.json'", (b'{"held": 1}',))
        threading.Timer(0.5, lambda: (other.execute("COMMIT"), other.close())).start()
        start = time.monotonic()
        with db.transaction():
            current = json.loads(db.read("sequence.json"))
            db.write("sequence.json", json.dumps({**current, "mine": 1}))
        assert time.monotonic() - start >= 0.3
        assert json.loads(db.read("sequence.json")) == {"held": 1, "mine": 1}

    def test_a_busy_error_is_retried(self, tmp_path, monkeypatch):
        """Some busy results come back at once instead of waiting (a WAL
        database being recovered by another connection)."""
        db = StateDB(tmp_path)
        db.write("sequence.json", "{}")
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise sqlite3.OperationalError("database is locked")
            return "ok"

        assert db._retry(flaky) == "ok"
        assert len(calls) == 3

    def test_errors_surface_as_oserror(self, tmp_path):
        (tmp_path / "state.db").write_bytes(b"definitely not sqlite" * 64)
        db = StateDB(tmp_path)
        with pytest.raises(StateDBError) as excinfo:
            db.read("sequence.json")
        assert isinstance(excinfo.value, OSError)

    def test_a_corrupt_row_reads_back_as_the_damaged_bytes(self, tmp_path):
        """A row holding zeros reads like the zero-filled file did, so the
        callers' existing "unparseable" handling (and the snapshot heal)
        still applies."""
        db = StateDB(tmp_path)
        db.write("sequence.json", "{}")
        raw = sqlite3.connect(tmp_path / "state.db")
        with raw:
            raw.execute("UPDATE docs SET body=? WHERE path='sequence.json'", (b"\x00" * 64,))
        raw.close()
        assert db.read("sequence.json") == b"\x00" * 64

    def test_integrity_check_and_backup(self, tmp_path):
        db = StateDB(tmp_path)
        db.write("sequence.json", "{}")
        assert db.integrity_check() == ["ok"]
        dest = tmp_path / "copy.db"
        db.backup_to(dest)
        conn = sqlite3.connect(dest)
        try:
            assert conn.execute("SELECT body FROM docs").fetchone()[0] == b"{}"
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        finally:
            conn.close()


_WRITER = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from claude_swap.statedb import StateDB
db = StateDB(Path(sys.argv[2]))
name = sys.argv[3]
for i in range(int(sys.argv[4])):
    db.write(f"cache/{name}-{i}.json", json.dumps({"i": i}))
    with db.transaction():
        counter = json.loads(db.read("cache/counter.json") or b'{"n": 0}')
        counter["n"] += 1
        db.write("cache/counter.json", json.dumps(counter))
"""


class TestTwoProcesses:
    def test_concurrent_writers_lose_nothing(self, tmp_path):
        """Each increment is a read-modify-write inside one transaction; a
        lost update or a busy failure would leave the counter short."""
        StateDB(tmp_path).write("cache/counter.json", '{"n": 0}')
        n = 60
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", _WRITER, SRC, str(tmp_path), name, str(n)],
                stderr=subprocess.PIPE, text=True,
            )
            for name in ("a", "b")
        ]
        for p in procs:
            _out, err = p.communicate(timeout=120)
            assert p.returncode == 0, err
        db = StateDB(tmp_path)
        assert json.loads(db.read("cache/counter.json")) == {"n": 2 * n}
        assert len(db.list("cache/a-")) == n
        assert len(db.list("cache/b-")) == n


# -- the path layer --------------------------------------------------------------


@pytest.fixture
def root(temp_home):
    from claude_swap.paths import get_backup_root

    return get_backup_root()


class TestPathLayer:
    def test_state_paths_go_to_the_database(self, root):
        path = root / "sequence.json"
        assert statedb.try_write(path, '{"a": 1}') is True
        assert statedb.read_text(path) == '{"a": 1}'
        assert statedb.exists(path)
        assert not path.exists(), "no file is written"
        assert statedb.open_db(root).read("sequence.json") == b'{"a": 1}'
        statedb.unlink(path)
        assert not statedb.exists(path)
        with pytest.raises(FileNotFoundError):
            statedb.read_text(path)
        with pytest.raises(FileNotFoundError):
            statedb.mtime(path)
        with pytest.raises(FileNotFoundError):
            statedb.unlink(path)
        statedb.unlink(path, missing_ok=True)

    def test_other_paths_stay_files(self, root, temp_home):
        for path in (
            temp_home / ".claude.json",
            root / "live_usage" / "s1.json",
            root / "sessions" / "1-a@b.com" / ".credentials.json",
            root / "credentials" / ".unclaimed-manifest.json",
        ):
            assert statedb.locate(path) is None
            assert statedb.try_write(path, "{}") is False
        path = temp_home / ".claude.json"
        path.write_text("{}")
        assert statedb.read_text(path) == "{}"
        assert statedb.exists(path)
        assert statedb.mtime(path) == path.stat().st_mtime

    def test_a_symlinked_state_file_stays_a_file(self, root, tmp_path):
        target = tmp_path / "dotfiles-settings.json"
        target.write_text('{"autoswitch": {}}')
        link = root / "settings.json"
        root.mkdir(parents=True, exist_ok=True)
        try:
            link.symlink_to(target)
        except OSError:
            pytest.skip("symlinks need privileges here")
        assert statedb.locate(link) is None
        assert statedb.open_db(root).state_files() == []

    def test_mtime_is_updated_at(self, root):
        path = root / "settings.json"
        statedb.try_write(path, "{}")
        first = statedb.mtime(path)
        statedb.try_write(path, '{"a": 1}')
        assert statedb.mtime(path) > first

    def test_a_managed_root_routes_even_when_home_moves(self, tmp_path):
        other = tmp_path / "elsewhere"
        assert statedb.locate(other / "sequence.json") is None
        statedb.manage(other)
        db, rel = statedb.locate(other / "sequence.json")
        assert (db.root, rel) == (other, "sequence.json")

    def test_an_unreadable_database_raises_from_exists(self, root):
        """``exists`` answering False on a broken database would let
        ``_init_sequence_file`` write a fresh roster over the real one."""
        statedb.try_write(root / "sequence.json", "{}")
        statedb.close_all()
        (root / "state.db").write_bytes(b"garbage" * 200)
        for suffix in ("-wal", "-shm"):
            (root / f"state.db{suffix}").unlink(missing_ok=True)
        with pytest.raises(StateDBError):
            statedb.exists(root / "sequence.json")


# -- import of the pre-database files -------------------------------------------


def _seed_files(root: Path) -> dict[str, bytes]:
    files = {
        "sequence.json": json.dumps({"accounts": {"1": {"email": "a@x.com"}}}).encode(),
        "weekly_anchors.json": b'{"version": 1}',
        "settings.json": b'{"autoswitch": {"threshold": 80}}',
        "credentials/.creds-1-a@x.com.enc": _b64({"claudeAiOauth": {"t": 1}}),
        "credentials/.creds-1-a@x.com.enc.prev": _b64({"claudeAiOauth": {"t": 0}}),
        "configs/.claude-config-1-a@x.com.json": b'{"oauthAccount": {}}',
        "cache/usage.json": b"{}",
        "codex/sequence.json": b'{"accounts": {}}',
        "codex/credentials/account-1.json": b'{"tokens": {}}',
    }
    for rel, body in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    # Never imported: not state documents.
    (root / "live_usage").mkdir()
    (root / "live_usage" / "s1.json").write_text("{}")
    (root / "credentials" / ".unclaimed-manifest.json").write_text("{}")
    return files


class TestImport:
    def test_imports_valid_files_and_renames_them(self, tmp_path):
        files = _seed_files(tmp_path)
        db = StateDB(tmp_path)
        assert sorted(db.import_files()) == sorted(files)
        for rel, body in files.items():
            assert db.read(rel) == body
            assert not (tmp_path / rel).exists()
            assert (tmp_path / (rel + ".migrated")).read_bytes() == body
        assert (tmp_path / "live_usage" / "s1.json").exists()
        assert (tmp_path / "credentials" / ".unclaimed-manifest.json").exists()

    def test_first_access_imports_before_reading(self, tmp_path):
        files = _seed_files(tmp_path)
        assert StateDB(tmp_path).read("sequence.json") == files["sequence.json"]

    def test_a_zero_filled_roster_is_skipped_while_the_rest_import(self, tmp_path, caplog):
        files = _seed_files(tmp_path)
        (tmp_path / "sequence.json").write_bytes(b"\x00" * 300)
        db = StateDB(tmp_path)
        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            imported = db.import_files()
        assert "sequence.json" not in imported
        assert not db.exists("sequence.json")
        assert sorted(imported) == sorted(set(files) - {"sequence.json"})
        assert "skipped damaged sequence.json" in caplog.text
        # Left in place for a person to look at, not renamed or deleted.
        assert (tmp_path / "sequence.json").read_bytes() == b"\x00" * 300

    def test_a_damaged_file_imports_its_copy_from_an_old_snapshot(self, tmp_path, caplog):
        """The 2026-10-26 crash, replayed: the file snapshots taken before
        the database still hold the good roster."""
        files = _seed_files(tmp_path)
        good = files["sequence.json"]
        digest = hashlib.sha256(good).hexdigest()
        snaps = tmp_path / "snapshots"
        (snaps / "objects").mkdir(parents=True)
        (snaps / "objects" / digest).write_bytes(good)
        (snaps / "20261026T050000Z.json").write_text(
            json.dumps({"files": {"sequence.json": digest}}))
        (tmp_path / "sequence.json").write_bytes(b"\x00" * len(good))
        db = StateDB(tmp_path)
        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            db.import_files()
        assert db.read("sequence.json") == good
        assert "from snapshot 20261026T050000Z" in caplog.text

    def test_is_idempotent(self, tmp_path):
        _seed_files(tmp_path)
        db = StateDB(tmp_path)
        db.import_files()
        before = db.items()
        assert db.import_files() == []
        assert db.items() == before

    def test_a_newer_file_wins_and_an_older_one_does_not(self, tmp_path):
        """An aswap from before the database, still running, writes files."""
        db = StateDB(tmp_path)
        db.write("sequence.json", '{"from": "db"}')
        stale = tmp_path / "sequence.json"
        stale.write_text('{"from": "old file"}')
        os.utime(stale, (time.time() - 3600, time.time() - 3600))
        db.import_files()
        assert db.read("sequence.json") == b'{"from": "db"}'
        assert (tmp_path / "sequence.json.migrated").exists()

        fresh = tmp_path / "sequence.json"
        fresh.write_text('{"from": "new file"}')
        os.utime(fresh, (time.time() + 60, time.time() + 60))
        db.import_files()
        assert db.read("sequence.json") == b'{"from": "new file"}'

    def test_an_unreadable_file_fails_the_import_instead_of_reading_as_absent(
        self, tmp_path, monkeypatch
    ):
        _seed_files(tmp_path)
        real = statedb.read_bytes_with_retry

        def deny(path, **kw):
            if Path(path).name == "sequence.json":
                raise PermissionError(13, "Permission denied")
            return real(path, **kw)

        monkeypatch.setattr(statedb, "read_bytes_with_retry", deny)
        db = StateDB(tmp_path)
        with pytest.raises(PermissionError):
            db.read("settings.json")
        # Nothing committed, nothing renamed, and the next access retries.
        assert (tmp_path / "settings.json").exists()
        monkeypatch.setattr(statedb, "read_bytes_with_retry", real)
        assert db.read("sequence.json") is not None

    def test_two_processes_starting_at_once(self, tmp_path):
        files = _seed_files(tmp_path)
        code = (
            "import sys; sys.path.insert(0, sys.argv[1]);"
            "from pathlib import Path; from claude_swap.statedb import StateDB;"
            "print(StateDB(Path(sys.argv[2])).read('sequence.json') is not None)"
        )
        procs = [
            subprocess.Popen([sys.executable, "-c", code, SRC, str(tmp_path)],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for _ in range(2)
        ]
        for p in procs:
            out, err = p.communicate(timeout=120)
            assert p.returncode == 0, err
            assert out.strip() == "True"
        db = StateDB(tmp_path)
        assert sorted(db.list()) == sorted(files)
        assert not any((tmp_path / rel).exists() for rel in files)

    def test_the_migration_is_recorded(self, temp_home):
        from claude_swap.switcher import ClaudeAccountSwitcher
        from claude_swap.paths import get_backup_root

        _seed_files(get_backup_root())
        switcher = ClaudeAccountSwitcher()
        applied = json.loads((switcher.backup_dir / ".migrations.json").read_text())["applied"]
        assert "state_files_to_db" in applied
        assert switcher._get_sequence_data()["accounts"]["1"]["email"] == "a@x.com"


# -- the switched-over helpers ---------------------------------------------------


@pytest.fixture
def switcher(temp_home):
    from claude_swap.switcher import ClaudeAccountSwitcher

    s = ClaudeAccountSwitcher()
    s._setup_directories()
    s._init_sequence_file()
    return s


class TestSwitchedHelpers:
    def test_roster_credentials_and_config_live_in_the_database(self, switcher):
        db = statedb.open_db(switcher.backup_dir)
        switcher._write_account_credentials("1", "a@x.com", '{"claudeAiOauth": {"t": 1}}')
        switcher._write_account_config("1", "a@x.com", '{"oauthAccount": {}}')
        assert db.exists("sequence.json")
        assert db.exists("credentials/.creds-1-a@x.com.enc")
        assert db.exists("configs/.claude-config-1-a@x.com.json")
        assert not switcher.sequence_file.exists()
        assert not list(switcher.credentials_dir.glob(".creds-*"))
        assert not list(switcher.configs_dir.glob("*.json"))
        # The .enc body is the same base64 the file held.
        assert base64.b64decode(db.read("credentials/.creds-1-a@x.com.enc")) == (
            b'{"claudeAiOauth": {"t": 1}}')
        assert switcher._read_account_credentials("1", "a@x.com") == '{"claudeAiOauth": {"t": 1}}'

        switcher._write_account_credentials("1", "a@x.com", '{"claudeAiOauth": {"t": 2}}')
        assert db.exists("credentials/.creds-1-a@x.com.enc.prev")
        switcher._delete_account_credentials("1", "a@x.com")
        assert not db.exists("credentials/.creds-1-a@x.com.enc")
        assert not db.exists("credentials/.creds-1-a@x.com.enc.prev")

    def test_a_torn_roster_row_still_refuses_to_be_read_as_empty(self, switcher):
        statefiles.write_text(switcher.sequence_file, '{"accounts": {"1"')
        with pytest.raises(ConfigError):
            switcher._get_sequence_data()

    def test_settings_changes_are_seen_through_updated_at(self, switcher):
        from claude_swap.settings import set_setting

        assert switcher._poll_policy_inputs()[0] != 77.0
        set_setting(switcher.backup_dir, "autoswitch.threshold", "77")
        assert switcher._poll_policy_inputs()[0] == 77.0
        assert not (switcher.backup_dir / "settings.json").exists()

    def test_stores_and_cache(self, switcher):
        from claude_swap import cache
        from claude_swap.mappings import MappingStore
        from claude_swap.weekly_anchor import WeeklyAnchorStore

        db = statedb.open_db(switcher.backup_dir)
        store = MappingStore(switcher.backup_dir)
        store.set(str(switcher.home / "work"), "a@x.com", "")
        assert store.get(str(switcher.home / "work")) is not None
        assert db.exists("mappings.json")

        anchors = WeeklyAnchorStore(switcher.backup_dir)
        anchors._save({"a@x.com|": {"anchor": "Mon 00:00"}})
        assert db.exists("weekly_anchors.json")
        assert "a@x.com|" in anchors.rows()

        path = switcher.backup_dir / "cache" / "update_check.json"
        cache.write_cache(path, "1.2.3")
        assert cache.read_cache(path, ttl=60) == "1.2.3"
        assert db.exists("cache/update_check.json")
        assert not path.exists()

    def test_codex_roster_and_auth_backups(self, temp_home):
        from claude_swap.codex import CodexAccountSwitcher

        codex = CodexAccountSwitcher()
        codex._write_sequence({"activeAccountNumber": None, "accounts": {}})
        codex._write_json(codex._credential_path("1"), {"tokens": {"a": 1}})
        db = statedb.open_db(codex.backup_dir)
        assert db.exists("codex/sequence.json")
        assert db.exists("codex/credentials/account-1.json")
        assert codex._read_account_auth("1") == {"tokens": {"a": 1}}
        assert not codex.sequence_file.exists()

    def test_purge_closes_the_database_before_removing_it(self, switcher, monkeypatch):
        switcher._write_account_credentials("1", "a@x.com", "{}")
        monkeypatch.setattr("builtins.input", lambda _prompt: "y")
        switcher.purge()
        assert not switcher.backup_dir.exists()
