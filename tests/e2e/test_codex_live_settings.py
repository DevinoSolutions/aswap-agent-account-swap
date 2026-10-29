"""``aswap config set`` reaching an ``aswap codex auto`` that is already running.

The Codex twin of ``test_live_settings.py``: one ``aswap codex auto`` process
is started once and never restarted while the settings change underneath it.
Codex's usage reads go to the fake through ``chatgpt_base_url`` in the box's
own ``$CODEX_HOME/config.toml`` -- no network, no real Codex home.
"""

from __future__ import annotations

import time

from tests.e2e.conftest import wait_until


def _reloaded(auto, pred, what: str) -> dict:
    """Wait for a ``settings-reloaded`` event that ``pred`` accepts."""
    def find(events):
        return next(
            (e for e in events if e.get("event") == "settings-reloaded" and pred(e)),
            None,
        )
    return auto.wait_for(find, 30, what)


def test_config_changes_reach_a_running_codex_auto(box):
    alice = box.account("alice", five_hour=80, seven_day=20)
    bob = box.account("bob", five_hour=10, seven_day=10)
    for acct in (alice, bob):
        box.codex_login(acct["email"])
        box.aswap("codex", "add")
    box.aswap("codex", "switch", "1")
    assert box.codex_live_email() == alice["email"]
    box.configure(intervalSeconds=1)

    # --cooldown is a flag: it stays put whatever the settings say.
    auto = box.start_codex_auto("--cooldown", "0")
    auto.wait_for(lambda ev: any(e["event"] == "poll" for e in ev), 60, "the first poll")
    time.sleep(2)
    assert auto.switches() == [], "80% is under the default 90% threshold"

    box.configure(cooldownSeconds=600)
    pinned = _reloaded(
        auto, lambda e: e.get("pinned"), "the pinned cooldown reported"
    )["pinned"]
    assert pinned == [{"key": "autoswitch.cooldownSeconds", "stored": 600.0, "kept": 0.0}]

    # Lower the threshold under alice's 80%: the same process switches.
    box.configure(threshold=70)
    changed = _reloaded(
        auto,
        lambda e: any(c["key"] == "autoswitch.threshold" for c in e.get("changes", [])),
        "the threshold change applied",
    )
    assert changed["changes"] == [{"key": "autoswitch.threshold", "old": 90.0, "new": 70.0}]
    sw = auto.wait_switch("2", timeout=45)
    assert sw["trigger"] == "proactive"
    wait_until(lambda: box.codex_live_email() == bob["email"], 20, "the Codex login to be bob")

    # And it keeps following: a later change lands in the same process.
    box.configure(threshold=75)
    _reloaded(
        auto,
        lambda e: any(c["new"] == 75.0 for c in e.get("changes", [])),
        "the threshold moved again",
    )

    assert auto.proc.poll() is None, "aswap codex auto exited"
    assert len(box.autos) == 1
    assert auto.stop() in (0, 130, -15, 1, 3221225786)
