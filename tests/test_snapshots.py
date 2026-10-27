"""Snapshots: validated, deduplicated copies of state, and crash restore."""

from __future__ import annotations

import base64
import json
import logging

from claude_swap import snapshots
from claude_swap.snapshots import SnapshotStore, maintain, valid

T0 = 1_800_000_000.0


def _seed(d):
    (d / "credentials").mkdir(parents=True)
    (d / "configs").mkdir()
    (d / "sequence.json").write_text(json.dumps({"sequence": [1, 2]}))
    (d / "weekly_anchors.json").write_text(json.dumps({"version": 1}))
    cred = base64.b64encode(json.dumps({"claudeAiOauth": {"x": 1}}).encode())
    (d / "credentials" / ".creds-1-a@x.com.enc").write_bytes(cred)
    (d / "configs" / ".claude-config-1-a@x.com.json").write_text("{}")
    (d / "cache").mkdir()
    (d / "cache" / "usage.json").write_text("{}")


def _zero(path):
    path.write_bytes(b"\x00" * len(path.read_bytes()))


class TestValid:
    def test_zero_filled_empty_and_garbage_are_invalid(self):
        assert not valid("sequence.json", b"\x00" * 50)
        assert not valid("sequence.json", b"")
        assert not valid("sequence.json", b"{trunc")
        assert not valid("credentials/.creds-1-a.enc", b"not base64!")
        assert valid("sequence.json", b"{}")
        assert valid("credentials/.creds-1-a.enc", base64.b64encode(b'{"a": 1}'))


class TestSnapshotStore:
    def test_take_records_tracked_files_only(self, tmp_path):
        _seed(tmp_path)
        store = SnapshotStore(tmp_path)
        stamp = store.take(T0)
        assert sorted(store.manifest(stamp)) == [
            "configs/.claude-config-1-a@x.com.json",
            "credentials/.creds-1-a@x.com.enc",
            "sequence.json",
            "weekly_anchors.json",
        ]

    def test_unchanged_state_takes_no_new_snapshot(self, tmp_path):
        _seed(tmp_path)
        store = SnapshotStore(tmp_path)
        assert store.take(T0)
        assert store.take(T0 + 3600) is None
        assert len(store.stamps()) == 1

    def test_damaged_file_is_not_snapshotted_and_restores_from_last_good(self, tmp_path):
        _seed(tmp_path)
        store = SnapshotStore(tmp_path)
        store.take(T0)
        good = (tmp_path / "sequence.json").read_bytes()
        _zero(tmp_path / "sequence.json")
        _zero(tmp_path / "credentials" / ".creds-1-a@x.com.enc")
        (tmp_path / "weekly_anchors.json").write_text(json.dumps({"version": 2}))
        later = store.take(T0 + 3600)
        assert "sequence.json" not in store.manifest(later)
        assert store.damaged() == ["credentials/.creds-1-a@x.com.enc", "sequence.json"]
        restored = {r.path: r.stamp for r in store.restore()}
        assert set(restored) == {"credentials/.creds-1-a@x.com.enc", "sequence.json"}
        assert (tmp_path / "sequence.json").read_bytes() == good
        # The healthy, newer file was left alone.
        assert json.loads((tmp_path / "weekly_anchors.json").read_text()) == {"version": 2}
        assert store.damaged() == []

    def test_restore_at_picks_an_older_snapshot(self, tmp_path):
        _seed(tmp_path)
        store = SnapshotStore(tmp_path)
        first = store.take(T0)
        (tmp_path / "sequence.json").write_text(json.dumps({"sequence": [1, 2, 3]}))
        store.take(T0 + 3600)
        store.restore(paths=["sequence.json"], at=first)
        assert json.loads((tmp_path / "sequence.json").read_text()) == {"sequence": [1, 2]}

    def test_prune_keeps_recent_and_daily_and_drops_orphan_objects(self, tmp_path, monkeypatch):
        monkeypatch.setattr(snapshots, "KEEP_RECENT", 2)
        _seed(tmp_path)
        store = SnapshotStore(tmp_path)
        for i in range(5):
            (tmp_path / "sequence.json").write_text(json.dumps({"n": i}))
            store.take(T0 + i * 3600)
        # Two most recent, plus the first of the day.
        assert len(store.stamps()) == 3
        live = {d for s in store.stamps() for d in store.manifest(s).values()}
        assert {p.name for p in store.objects.iterdir()} == live


class TestMaintain:
    def test_heals_damage_and_snapshots_hourly(self, tmp_path, caplog):
        _seed(tmp_path)
        log = logging.getLogger("claude-swap")
        maintain(tmp_path, log, now=T0)
        store = SnapshotStore(tmp_path)
        assert len(store.stamps()) == 1
        _zero(tmp_path / "sequence.json")
        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            maintain(tmp_path, log, now=T0 + 60)
        assert "Restored damaged sequence.json" in caplog.text
        assert store.damaged() == []
        (tmp_path / "weekly_anchors.json").write_text(json.dumps({"version": 9}))
        maintain(tmp_path, log, now=T0 + 120)
        assert len(store.stamps()) == 1  # not due yet
        maintain(tmp_path, log, now=T0 + 3700)
        assert len(store.stamps()) == 2
