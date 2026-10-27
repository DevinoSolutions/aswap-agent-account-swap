"""Snapshots: copies of the state database, and crash restore from them."""

from __future__ import annotations

import base64
import errno
import hashlib
import json
import logging
import os
import sqlite3

import pytest

from claude_swap import snapshots, statedb
from claude_swap.snapshots import SnapshotStore, backup_command, maintain, valid

T0 = 1_800_000_000.0
CRED = "credentials/.creds-1-a@x.com.enc"
CONFIG = "configs/.claude-config-1-a@x.com.json"
SESSION_CRED = "sessions/1-a@x.com/.credentials.json"
SEQUENCE = {"sequence": [1], "accounts": {"1": {"email": "a@x.com"}}}


def _seed(root):
    db = statedb.open_db(root)
    db.write("sequence.json", json.dumps(SEQUENCE))
    db.write("weekly_anchors.json", json.dumps({"version": 1}))
    db.write(CRED, base64.b64encode(json.dumps({"claudeAiOauth": {"x": 1}}).encode()))
    db.write(CONFIG, "{}")
    db.write("cache/usage.json", "{}")
    session = root / SESSION_CRED
    session.parent.mkdir(parents=True)
    session.write_text(json.dumps({"claudeAiOauth": {"s": 1}}))
    return db


def _sharing_violation(path) -> OSError:
    """The OSError Windows raises moving a file another process has open."""
    e = PermissionError(errno.EACCES, "The process cannot access the file because "
                        "it is being used by another process", str(path))
    e.winerror = 32  # ERROR_SHARING_VIOLATION; settable off Windows too
    return e


def _zero(db, rel):
    db.write(rel, b"\x00" * len(db.read(rel)))


class TestValid:
    def test_zero_filled_empty_and_garbage_are_invalid(self):
        assert not valid("sequence.json", b"\x00" * 50)
        assert not valid("sequence.json", b"")
        assert not valid("sequence.json", b"{trunc")
        assert not valid("credentials/.creds-1-a.enc", b"not base64!")
        assert not valid("credentials/.creds-1-a.enc.prev", b"not base64!")
        assert valid("sequence.json", b"{}")
        assert valid("credentials/.creds-1-a.enc", base64.b64encode(b'{"a": 1}'))
        assert valid("credentials/.creds-1-a.enc.prev", base64.b64encode(b'{"a": 1}'))


class TestSnapshotStore:
    def test_take_copies_the_database_and_session_credentials(self, tmp_path):
        _seed(tmp_path)
        store = SnapshotStore(tmp_path)
        stamp = store.take(T0)
        assert (tmp_path / "snapshots" / f"state-{stamp}.db").is_file()
        assert sorted(store.docs(stamp)) == sorted([
            CONFIG, CRED, SESSION_CRED, "cache/usage.json",
            "sequence.json", "weekly_anchors.json",
        ])
        # A snapshot is one self-contained file, not a WAL pair.
        assert not (tmp_path / "snapshots" / f"state-{stamp}.db-wal").exists()

    def test_unchanged_state_takes_no_new_snapshot(self, tmp_path):
        db = _seed(tmp_path)
        store = SnapshotStore(tmp_path)
        assert store.take(T0)
        assert store.take(T0 + 3600) is None
        db.write("settings.json", "{}")
        assert store.take(T0 + 7200)
        assert len(store.stamps()) == 2

    def test_damaged_document_restores_from_last_good(self, tmp_path):
        db = _seed(tmp_path)
        store = SnapshotStore(tmp_path)
        store.take(T0)
        good = db.read("sequence.json")
        _zero(db, "sequence.json")
        _zero(db, CRED)
        db.write("weekly_anchors.json", json.dumps({"version": 2}))
        store.take(T0 + 3600)  # the damaged copies are in this one
        assert store.damaged() == [CRED, "sequence.json"]
        restored = {r.path: r.stamp for r in store.restore()}
        assert set(restored) == {CRED, "sequence.json"}
        assert db.read("sequence.json") == good
        # The healthy, newer document was left alone.
        assert json.loads(db.read("weekly_anchors.json")) == {"version": 2}
        assert store.damaged() == []

    def test_a_damaged_session_credential_is_restored_as_a_file(self, tmp_path):
        _seed(tmp_path)
        store = SnapshotStore(tmp_path)
        store.take(T0)
        session = tmp_path / SESSION_CRED
        good = session.read_bytes()
        session.write_bytes(b"\x00" * 10)
        assert store.damaged() == [SESSION_CRED]
        store.restore()
        assert session.read_bytes() == good
        assert not statedb.open_db(tmp_path).exists(SESSION_CRED)

    def test_restore_at_picks_an_older_snapshot(self, tmp_path):
        db = _seed(tmp_path)
        store = SnapshotStore(tmp_path)
        first = store.take(T0)
        db.write("sequence.json", json.dumps({"sequence": [1, 2, 3]}))
        store.take(T0 + 3600)
        store.restore(paths=["sequence.json"], at=first)
        assert json.loads(db.read("sequence.json")) == SEQUENCE

    def test_missing_documents_come_back_only_when_asked(self, tmp_path):
        """The hourly heal must not resurrect a removed account's backups;
        a person restoring by hand after a crash asks for them."""
        db = _seed(tmp_path)
        store = SnapshotStore(tmp_path)
        store.take(T0)
        db.delete(CRED)
        assert store.restore() == []
        assert not db.exists(CRED)
        assert [r.path for r in store.restore(missing=True)] == [CRED]
        assert db.exists(CRED)

    def test_missing_documents_of_a_removed_account_stay_removed(self, tmp_path):
        """Restoring missing documents by hand must not resurrect an account
        that was removed after the snapshot; --all is the explicit way."""
        db = _seed(tmp_path)
        gone_cred = "credentials/.creds-2-b@x.com.enc"
        gone_config = "configs/.claude-config-2-b@x.com.json"
        reused = "credentials/.creds-1-old@x.com.enc"  # slot 1, earlier owner
        roster = {"sequence": [1, 2], "accounts": {
            "1": {"email": "a@x.com"}, "2": {"email": "b@x.com"}}}
        db.write("sequence.json", json.dumps(roster))
        for rel in (gone_cred, reused):
            db.write(rel, base64.b64encode(b'{"claudeAiOauth": {}}'))
        db.write(gone_config, "{}")
        store = SnapshotStore(tmp_path)
        store.take(T0)
        # Account 2 is removed; account 1 also loses a document by accident.
        db.write("sequence.json", json.dumps(SEQUENCE))
        for rel in (gone_cred, gone_config, reused, CONFIG):
            db.delete(rel)

        assert store.missing() == ([CONFIG], [gone_config, reused, gone_cred])
        assert [r.path for r in store.restore(missing=True)] == [CONFIG]
        assert not db.exists(gone_cred) and not db.exists(gone_config)
        assert not db.exists(reused)

    def test_missing_roster_is_read_from_the_snapshot(self, tmp_path):
        """With the roster itself gone, the snapshot's copy (which is about
        to be restored) decides what belongs."""
        db = _seed(tmp_path)
        store = SnapshotStore(tmp_path)
        store.take(T0)
        db.delete("sequence.json")
        db.delete(CRED)
        restored = sorted(r.path for r in store.restore(missing=True))
        assert restored == [CRED, "sequence.json"]

    def test_prune_keeps_recent_and_daily(self, tmp_path, monkeypatch):
        monkeypatch.setattr(snapshots, "KEEP_RECENT", 2)
        db = _seed(tmp_path)
        store = SnapshotStore(tmp_path)
        for i in range(5):
            db.write("sequence.json", json.dumps({"n": i}))
            store.take(T0 + i * 3600)
        # Two most recent, plus the first of the day.
        assert store.stamps() == [
            snapshots._stamp(T0),
            snapshots._stamp(T0 + 3 * 3600),
            snapshots._stamp(T0 + 4 * 3600),
        ]

    def test_export_writes_every_document_back_as_its_file(self, tmp_path):
        db = _seed(tmp_path)
        out = tmp_path / "export"
        written = SnapshotStore(tmp_path).export(out)
        assert sorted(written) == db.list()
        for rel in written:
            assert (out / rel).read_bytes() == db.read(rel)

    def test_replace_database_rebuilds_a_corrupt_one_from_a_snapshot(self, tmp_path):
        db = _seed(tmp_path)
        store = SnapshotStore(tmp_path)
        store.take(T0)
        good = db.read("sequence.json")
        statedb.close(tmp_path)
        (tmp_path / "state.db").write_bytes(b"this is not a database" * 100)
        for suffix in ("-wal", "-shm"):
            (tmp_path / f"state.db{suffix}").unlink(missing_ok=True)
        with pytest.raises(statedb.StateDBError):
            statedb.open_db(tmp_path).read("sequence.json")

        stamp = SnapshotStore(tmp_path).replace_database()

        assert stamp == snapshots._stamp(T0)
        assert statedb.open_db(tmp_path).read("sequence.json") == good
        assert list(tmp_path.glob("state.db.damaged-*")), "the damaged copy is kept"

    def test_replace_database_in_use_puts_back_what_it_moved(self, tmp_path, monkeypatch):
        """Another process holding the -wal open (Windows sharing violation)
        must not leave state.db moved aside without its -wal."""
        _seed(tmp_path)
        store = SnapshotStore(tmp_path)
        store.take(T0)
        statedb.close(tmp_path)
        db_file = tmp_path / "state.db"
        wal = tmp_path / "state.db-wal"
        db_file.write_bytes(b"this is not a database" * 100)
        wal.write_bytes(b"wal")
        real_replace = os.replace

        def replace(src, dst):
            if str(src).endswith("-wal"):
                raise _sharing_violation(src)
            real_replace(src, dst)

        monkeypatch.setattr(snapshots.os, "replace", replace)
        with pytest.raises(snapshots.DatabaseInUseError):
            SnapshotStore(tmp_path).replace_database()
        assert db_file.read_bytes() == b"this is not a database" * 100
        assert wal.read_bytes() == b"wal"
        assert not list(tmp_path.glob("state.db*.damaged-*"))

    def test_legacy_file_snapshots_still_answer_newest_valid(self, tmp_path):
        """Snapshots taken before the database stay readable, for the import."""
        body = json.dumps({"sequence": [7]}).encode()
        digest = hashlib.sha256(body).hexdigest()
        root = tmp_path / "snapshots"
        (root / "objects").mkdir(parents=True)
        (root / "objects" / digest).write_bytes(body)
        (root / "20260101T000000Z.json").write_text(
            json.dumps({"files": {"sequence.json": digest}}))
        assert SnapshotStore(tmp_path).newest_valid("sequence.json") == (
            "20260101T000000Z", body)

    def test_legacy_file_snapshots_age_out(self, tmp_path):
        body = b"{}"
        digest = hashlib.sha256(body).hexdigest()
        root = tmp_path / "snapshots"
        (root / "objects").mkdir(parents=True)
        (root / "objects" / digest).write_bytes(body)
        (root / "20200101T000000Z.json").write_text(
            json.dumps({"files": {"sequence.json": digest}}))
        _seed(tmp_path)
        SnapshotStore(tmp_path).take(T0)
        assert not (root / "20200101T000000Z.json").exists()
        assert not (root / "objects").exists()


class TestMaintain:
    def test_heals_damage_and_snapshots_hourly(self, tmp_path, caplog):
        db = _seed(tmp_path)
        log = logging.getLogger("claude-swap")
        maintain(tmp_path, log, now=T0)
        store = SnapshotStore(tmp_path)
        assert len(store.stamps()) == 1
        _zero(db, "sequence.json")
        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            maintain(tmp_path, log, now=T0 + 60)
        assert "Restored damaged sequence.json" in caplog.text
        assert store.damaged() == []
        db.write("weekly_anchors.json", json.dumps({"version": 9}))
        maintain(tmp_path, log, now=T0 + 120)
        assert len(store.stamps()) == 1  # not due yet
        maintain(tmp_path, log, now=T0 + 3700)
        assert len(store.stamps()) == 2

    def test_imports_files_an_older_aswap_wrote_after_the_upgrade(self, tmp_path):
        """An engine from before the database keeps writing files until it
        is restarted; the hourly pass carries those writes over."""
        db = _seed(tmp_path)
        (tmp_path / "sequence.json").write_text(json.dumps({"sequence": [9]}))
        maintain(tmp_path, logging.getLogger("claude-swap"), now=T0)
        assert json.loads(db.read("sequence.json")) == {"sequence": [9]}
        assert not (tmp_path / "sequence.json").exists()
        assert (tmp_path / "sequence.json.migrated").exists()


class TestBackupCommand:
    """``aswap backup`` against a switcher's real (temp) backup root."""

    @pytest.fixture
    def root(self, temp_home):
        from claude_swap.switcher import ClaudeAccountSwitcher

        root = ClaudeAccountSwitcher().backup_dir
        _seed(root)
        return root

    def test_take_list_check(self, root, capsys):
        backup_command(["take"])
        assert "taken" in capsys.readouterr().out
        backup_command(["list"])
        assert "6 documents" in capsys.readouterr().out
        backup_command(["check"])
        assert capsys.readouterr().out.strip() == "No damaged documents"

    def test_check_names_damaged_documents_and_restore_fixes_them(self, root, capsys):
        backup_command(["take"])
        db = statedb.open_db(root)
        _zero(db, CRED)
        capsys.readouterr()
        backup_command(["check"])
        assert capsys.readouterr().out.strip() == CRED
        backup_command(["restore", "--dry-run"])
        assert f"Would restore {CRED}" in capsys.readouterr().out
        assert not valid(CRED, db.read(CRED))
        backup_command(["restore"])
        assert f"Restored {CRED}" in capsys.readouterr().out
        assert valid(CRED, db.read(CRED))

    def test_restore_leaves_out_a_removed_account_unless_all(self, root, capsys):
        db = statedb.open_db(root)
        backup_command(["take"])
        db.write("sequence.json", json.dumps({"sequence": [], "accounts": {}}))
        db.delete(CRED)
        capsys.readouterr()
        backup_command(["restore"])
        out = capsys.readouterr().out
        assert "Nothing to restore" in out
        assert "Left out 1 documents of accounts no longer in the roster" in out
        assert not db.exists(CRED)
        backup_command(["restore", "--all"])
        assert f"Restored {CRED}" in capsys.readouterr().out
        assert db.exists(CRED)

    def test_restore_of_a_database_in_use_says_what_to_stop(self, root, capsys, monkeypatch):
        backup_command(["take"])
        capsys.readouterr()
        monkeypatch.setattr(statedb.StateDB, "integrity_check",
                            lambda self: ["*** in database main ***"])

        def replace(src, dst):
            raise _sharing_violation(src)

        monkeypatch.setattr(snapshots.os, "replace", replace)
        with pytest.raises(SystemExit) as exc:
            backup_command(["restore"])
        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "Stop aswap auto and the dashboard, then retry." in captured.err
        assert "Traceback" not in captured.err + captured.out
        assert (root / "state.db").exists()

    def test_check_reports_a_failed_integrity_check(self, root, capsys, monkeypatch):
        monkeypatch.setattr(statedb.StateDB, "integrity_check",
                            lambda self: ["*** in database main ***", "page 3 is never used"])
        backup_command(["check"])
        out = capsys.readouterr().out
        assert "failed its integrity check" in out
        assert "page 3 is never used" in out

    def test_export_writes_files_and_prints_no_credential(self, root, tmp_path, capsys):
        out_dir = tmp_path / "out"
        backup_command(["export", str(out_dir)])
        printed = capsys.readouterr().out
        assert "Exported 5 documents" in printed
        db = statedb.open_db(root)
        assert (out_dir / CRED).read_bytes() == db.read(CRED)
        assert db.read(CRED).decode() not in printed

    def test_snapshot_file_is_readable_by_plain_sqlite(self, root):
        """The escape hatch needs nothing but sqlite3."""
        backup_command(["take"])
        snap = next((root / "snapshots").glob("state-*.db"))
        conn = sqlite3.connect(snap)
        try:
            paths = {p for (p,) in conn.execute("SELECT path FROM docs")}
        finally:
            conn.close()
        assert "sequence.json" in paths
