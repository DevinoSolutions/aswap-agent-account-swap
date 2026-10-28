"""The SQLite state database under real processes: migration, concurrency,
hard kills, and the backup commands."""

from __future__ import annotations

import json
import random
import shutil
import sqlite3
import sys
import threading
import time
from pathlib import Path

from tests.e2e.conftest import wait_until
from tests.e2e.fake_claude import token_id

FIXTURE = Path(__file__).parent / "fixtures" / "legacy_json"
LEGACY = {
    "one": {"email": "legacy-one@e2e.test", "uuid": "acct-legacy-0001", "org_uuid": "org-legacy-0001",
            "org_name": "Legacy One Org"},
    "two": {"email": "legacy-two@e2e.test", "uuid": "acct-legacy-0002", "org_uuid": "org-legacy-0002",
            "org_name": "Legacy Two Org"},
}


def _db_check(box) -> None:
    """The database is whole, as seen by SQLite itself and by aswap."""
    with sqlite3.connect(f"file:{box.backup_root / 'state.db'}?mode=ro", uri=True) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert box.aswap("backup", "check").stdout.strip() == "No damaged documents"


def _live_token_is_current(box) -> None:
    """The live login is a slot's own, still-valid token family, and
    ~/.claude.json names that same account."""
    email = box.live_email()
    live = box.live_credentials()
    assert email and live, "no live login"
    assert token_id(live["refreshToken"]) in box.fake.state(email)["liveRefreshIds"], (
        f"the live refresh token is not {email}'s current one"
    )


def test_legacy_json_files_are_imported_into_state_db(box):
    root = box.backup_root
    shutil.copytree(FIXTURE, root, ignore=shutil.ignore_patterns("README.md"))
    for name, acct in LEGACY.items():
        box.fake.add_account(
            acct["email"], uuid=acct["uuid"], org_uuid=acct["org_uuid"], org_name=acct["org_name"],
            five_hour=12 if name == "one" else 34, seven_day=56,
            access_token=f"sk-ant-oat01-e2e-legacy-{name}-access-0000", access_expires=0,
            refresh_token=f"sk-ant-ort01-e2e-legacy-{name}-refresh-0000",
        )
    box.login(LEGACY["one"]["email"])  # the machine is logged in as slot 1
    assert not (root / "state.db").exists()

    listed = box.list_json()["claude"]
    assert (root / "state.db").exists()
    for rel in ("sequence.json", "settings.json",
                f"configs/.claude-config-2-{LEGACY['two']['email']}.json",
                f"credentials/.creds-2-{LEGACY['two']['email']}.enc"):
        assert (root / (rel + ".migrated")).is_file(), f"{rel} was not imported"
        assert not (root / rel).exists()

    assert [a["email"] for a in listed["accounts"]] == [LEGACY["one"]["email"], LEGACY["two"]["email"]]
    assert listed["activeAccountNumber"] == 1
    # Slot 2's imported credential works: its expired token was refreshed
    # with the fixture's refresh token (rotated by the fake) and usage read.
    two = listed["accounts"][1]
    assert two["usage"]["fiveHour"]["pct"] == 34.0
    assert box.fake.state(LEGACY["two"]["email"])["spentRefreshTokens"] == 1
    assert box.aswap("config", "get", "autoswitch.threshold").stdout.strip().startswith("85")

    # The rotated token went into the database, not the renamed file.
    box.aswap("switch", "2")
    _live_token_is_current(box)
    assert box.live_email() == LEGACY["two"]["email"]
    _db_check(box)


def test_auto_and_cli_processes_share_the_database(box):
    names = ["a", "b", "c"]
    accts = {n: box.account(n, five_hour=10 + i, seven_day=20 + i) for i, n in enumerate(names)}
    for n in names:
        box.login(accts[n]["email"])
        box.aswap("add")
    box.configure(intervalSeconds=1, cooldownSeconds=0)
    auto = box.start_auto()
    auto.wait_for(lambda ev: any(e["event"] == "poll" for e in ev), 60, "the first poll")

    failures: list[str] = []

    def hammer(seed: int) -> None:
        rng = random.Random(seed)
        for _ in range(8):
            args = rng.choice([
                ("switch", str(rng.randint(1, 3))),
                ("list", "--json"),
                ("status", "--json"),
                ("config", "set", "autoswitch.hysteresisPct", str(rng.randint(5, 15))),
            ])
            done = box.aswap(*args, check=False, timeout=120)
            if done.returncode != 0:
                failures.append(f"{args}: exit {done.returncode}: {done.stderr[-500:]}")

    threads = [threading.Thread(target=hammer, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not failures, "\n".join(failures)
    assert auto.proc.poll() is None, "aswap auto died under concurrent CLI use"
    auto.stop()

    _db_check(box)
    listed = box.list_json()["claude"]
    assert sorted(a["email"] for a in listed["accounts"]) == sorted(a["email"] for a in accts.values())
    active = next(a for a in listed["accounts"] if a["active"])
    assert active["email"] == box.live_email()
    _live_token_is_current(box)
    # Every slot still holds its own working credential.
    for num in ("1", "2", "3"):
        box.aswap("switch", num)
        _live_token_is_current(box)


def test_kill_dash_nine_mid_run_then_restart(box):
    alice = box.account("alice", five_hour=10, seven_day=10)
    bob = box.account("bob", five_hour=10, seven_day=10)
    for acct in (alice, bob):
        box.login(acct["email"])
        box.aswap("add")
    box.configure(intervalSeconds=1, cooldownSeconds=0, threshold=80)

    hot = alice
    for round_ in range(3):
        auto = box.start_auto(name=f"auto-kill{round_}")
        auto.wait_for(lambda ev: any(e["event"] == "poll" for e in ev), 60, "the first poll")
        # Make the active account hot so the engine is busy switching (writes
        # in flight) when it is killed.
        active_email = box.live_email()
        cold = bob if active_email == alice["email"] else alice
        hot = alice if cold is bob else bob
        box.fake.set(hot["email"], five_hour=95)
        box.fake.set(cold["email"], five_hour=10)
        time.sleep(random.uniform(0.5, 3.0))
        auto.kill()
        _db_check(box)
        _live_token_is_current(box)

    # A fresh engine picks up where the killed one left off and still works.
    box.fake.set(alice["email"], five_hour=10)
    box.fake.set(bob["email"], five_hour=10)
    auto = box.start_auto(name="auto-final")
    auto.wait_for(lambda ev: any(e["event"] == "poll" for e in ev), 60, "the first poll")
    current = box.live_email()
    other = bob if current == alice["email"] else alice
    other_num = "1" if other is alice else "2"
    box.fake.set(current, five_hour=96)
    auto.wait_switch(other_num, timeout=60)
    wait_until(lambda: box.live_email() == other["email"], 20, "the switch to land")
    auto.stop()
    _db_check(box)
    _live_token_is_current(box)


def test_backup_take_check_restore_export(box):
    alice, bob = box.account("alice"), box.account("bob")
    for acct in (alice, bob):
        box.login(acct["email"])
        box.aswap("add")
    box.aswap("config", "set", "autoswitch.threshold", "70")

    taken = box.aswap("backup", "take").stdout.strip()
    assert taken.startswith("Snapshot ") and taken.endswith(" taken"), taken
    stamp = taken.split()[1]
    assert stamp in box.aswap("backup", "list").stdout
    _db_check(box)

    # Damage the roster the way the 2026-10-26 crash did (zero-filled), and
    # change a setting after the snapshot.
    box.aswap("config", "set", "autoswitch.threshold", "80")
    with sqlite3.connect(box.backup_root / "state.db") as conn:
        conn.execute("UPDATE docs SET body = ? WHERE path = 'sequence.json'", (b"\0" * 64,))
    assert "sequence.json" in box.aswap("backup", "check").stdout.split()

    restored = box.aswap("backup", "restore").stdout
    assert f"Restored sequence.json from {stamp}" in restored
    _db_check(box)
    assert [a["email"] for a in box.list_json()["claude"]["accounts"]] == [alice["email"], bob["email"]]
    assert box.aswap("config", "get", "autoswitch.threshold").stdout.strip().startswith("80")

    assert "settings.json" in box.aswap("backup", "restore", "--all").stdout
    assert box.aswap("config", "get", "autoswitch.threshold").stdout.strip().startswith("70")

    out = box.root / "exported"
    assert "Exported" in box.aswap("backup", "export", str(out)).stdout
    seq = json.loads((out / "sequence.json").read_text(encoding="utf-8"))
    assert sorted(a["email"] for a in seq["accounts"].values()) == sorted([alice["email"], bob["email"]])
    assert json.loads((out / "settings.json").read_text(encoding="utf-8"))["autoswitch"]["threshold"] == 70
    if sys.platform != "darwin":  # macOS keeps slot credentials in the Keychain
        assert len(list((out / "credentials").glob(".creds-*.enc"))) == 2
