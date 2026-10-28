"""``aswap config set`` reaching an ``aswap auto`` that is already running.

The owner leaves ``aswap auto`` running for days; a settings change that only
lands after a restart is a change that does not land. Here one engine process
is started once and never restarted, while the settings change underneath it.
"""

from __future__ import annotations

import time

from tests.e2e.conftest import wait_until


def _reload_of(auto, key: str, new: object) -> dict:
    """Wait for the ``settings-reloaded`` event that moved ``key`` to ``new``."""
    def pred(events):
        for e in events:
            if e.get("event") != "settings-reloaded":
                continue
            for change in e.get("changes", []):
                if change["key"] == key and change["new"] == new:
                    return change
        return None
    return auto.wait_for(pred, 30, f"a settings-reloaded event: {key} -> {new}")


def _slots(box):
    return {a["email"]: a for a in box.list_json()["claude"]["accounts"]}


def test_config_changes_reach_a_running_auto(box):
    alice = box.account("alice", five_hour=80, seven_day=20)
    bob = box.account("bob", five_hour=10, seven_day=10)
    for acct in (alice, bob):
        box.login(acct["email"])
        box.aswap("add")
    box.configure(intervalSeconds=1, cooldownSeconds=0)
    box.aswap("switch", "1")
    assert box.live_email() == alice["email"]

    auto = box.start_auto()
    auto.wait_for(lambda ev: any(e["event"] == "poll" for e in ev), 60, "the first poll")
    time.sleep(3)
    assert auto.switches() == [], "80% is under the default 90% threshold"

    # Lower the threshold under alice's 80%: the same process switches.
    box.configure(threshold=70)
    assert _reload_of(auto, "autoswitch.threshold", 70.0)["old"] == 90.0
    sw = auto.wait_switch("2", timeout=45)
    assert sw["trigger"] == "proactive"
    wait_until(lambda: box.live_email() == bob["email"], 20, "the live login to be bob")
    assert "settings reloaded: autoswitch.threshold 90 -> 70" in box.all_log_text()

    # Turn auto-add off: a new login is left alone by that same process.
    box.configure(autoAddAccounts=False)
    assert _reload_of(auto, "autoswitch.autoAddAccounts", False)["old"] is True
    carol = box.account("carol", five_hour=5, seven_day=5)
    box.login(carol["email"])
    time.sleep(10)  # dozens of polls on the scaled clock
    assert carol["email"] not in _slots(box)
    assert "Auto-added" not in box.all_log_text()

    # And back on: still no restart, and now it is picked up.
    box.configure(autoAddAccounts=True)
    _reload_of(auto, "autoswitch.autoAddAccounts", True)
    wait_until(lambda: carol["email"] in _slots(box), 60, "carol to be auto-added")

    assert auto.proc.poll() is None, "aswap auto exited"
    assert len(box.autos) == 1
    assert auto.stop() in (0, 130, -15, 1, 3221225786)
