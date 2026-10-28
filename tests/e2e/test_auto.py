"""``aswap auto`` running in the background against the fake's live numbers.

Each test starts the real engine as a subprocess (``aswap auto --json``),
moves the fake's usage/gate from outside, and watches the JSON events and
the live credential Claude Code would read. Polling runs on the scaled clock
(``ASWAP_TIME_SCALE``); burn-rate and runway margins stay on the real one, so
these tests move usage in single steps and finish well inside the minute of
evidence the windowed burn rate needs. A single step of more than a couple of
points is evidence of a pace by itself (``BurnTracker.fast_rate``), which the
jump tests rely on.
"""

from __future__ import annotations

import itertools
import json
import os
import time

from tests.e2e.conftest import wait_until

HOUR = 3600.0
DAY = 86400.0


def _setup(box, specs, *, active, **settings):
    """Accounts from ``specs`` ({name: fake fields}), all added; ``active``
    left as the live login. Settings default to a fast, cooldown-free loop."""
    accounts = {}
    for name, fields in specs.items():
        accounts[name] = box.account(name, **fields)
        box.login(accounts[name]["email"])
        box.aswap("add")
    nums = {name: str(i + 1) for i, name in enumerate(specs)}
    box.configure(**{"intervalSeconds": 1, "cooldownSeconds": 0, **settings})
    box.aswap("switch", nums[active])
    assert box.live_email() == accounts[active]["email"]
    return accounts, nums


def _first_poll(auto):
    return auto.wait_for(lambda ev: any(e["event"] == "poll" for e in ev), 60, "the first poll")


def _quiet_for(auto, seconds):
    """Watch the engine for ``seconds`` and return the switches it made."""
    before = len(auto.switches())
    time.sleep(seconds)
    if auto.proc.poll() is not None:
        raise AssertionError(f"aswap auto exited ({auto.proc.returncode})")
    return auto.switches()[before:]


def test_switches_over_threshold_to_most_headroom_and_stays(box):
    accounts, nums = _setup(box, {
        "alice": {"five_hour": 40, "seven_day": 30},
        "bob": {"five_hour": 20, "seven_day": 70},
        "carol": {"five_hour": 10, "seven_day": 25},
    }, active="alice")
    auto = box.start_auto()
    _first_poll(auto)
    assert _quiet_for(auto, 3) == [], "nothing to do below the threshold"

    box.fake.set(accounts["alice"]["email"], five_hour=93)
    sw = auto.wait_switch(nums["carol"], timeout=45)
    assert sw["trigger"] == "proactive"
    assert sw["from"]["number"] == int(nums["alice"])
    wait_until(lambda: box.live_email() == accounts["carol"]["email"], 20, "the live login to be carol")
    assert box.live_credentials()["refreshToken"]

    # No ping-pong: carol is fine, alice is still over the line.
    assert _quiet_for(auto, 8) == []
    assert len(auto.switches()) == 1
    assert auto.stop() in (0, 130, -15, 1, 3221225786)


def test_at_limit_escape(box):
    accounts, nums = _setup(box, {
        "alice": {"five_hour": 60, "seven_day": 50},
        "bob": {"five_hour": 10, "seven_day": 20},
    }, active="alice", threshold=99)
    auto = box.start_auto()
    _first_poll(auto)
    box.fake.set(accounts["alice"]["email"], five_hour=100)  # the gate closes with it
    sw = auto.wait_switch(nums["bob"], timeout=45)
    assert sw["trigger"] == "at-limit"
    wait_until(lambda: box.live_email() == accounts["bob"]["email"], 20, "the live login to be bob")


def test_consume_first_follows_the_soonest_reset(box):
    now = time.time()
    accounts, nums = _setup(box, {
        "late": {"five_hour": 5, "seven_day": 20, "seven_day_reset": now + 6 * DAY},
        "soon": {"five_hour": 5, "seven_day": 30, "seven_day_reset": now + 1 * DAY},
        "mid": {"five_hour": 5, "seven_day": 10, "seven_day_reset": now + 3 * DAY},
    }, active="late", strategy="consume-first", manualHoldMinutes=0)
    auto = box.start_auto()
    sw = auto.wait_switch(nums["soon"], timeout=60)
    assert sw["trigger"] == "consume-first"
    wait_until(lambda: box.live_email() == accounts["soon"]["email"], 20, "the live login to be soon")

    # Spent: the at-limit escape moves on, to the next-soonest reset.
    box.fake.set(accounts["soon"]["email"], seven_day=100)
    sw = auto.wait_switch(nums["mid"], timeout=60, after=1)
    assert sw["trigger"] in ("at-limit", "consume-first")
    assert _quiet_for(auto, 6) == []


def test_manual_hold_survives_consume_first_but_not_the_limit(box):
    now = time.time()
    accounts, nums = _setup(box, {
        "late": {"five_hour": 5, "seven_day": 20, "seven_day_reset": now + 6 * DAY},
        "soon": {"five_hour": 5, "seven_day": 30, "seven_day_reset": now + 1 * DAY},
    }, active="soon", strategy="consume-first", manualHoldMinutes=60)
    auto = box.start_auto()
    _first_poll(auto)
    assert _quiet_for(auto, 2) == [], "already on the soonest reset"

    # Pick the late account by hand: consume-first would move straight back
    # to "soon" (test_consume_first_follows_the_soonest_reset), the hold says
    # not for 60 minutes.
    box.aswap("switch", nums["late"])
    assert box.live_email() == accounts["late"]["email"]
    held = auto.wait_for(
        lambda ev: next((e for e in ev if e.get("reason") == "manual-hold"), None),
        30, "a manual-hold no-switch",
    )
    assert f"account {nums['late']} was picked by hand" in held["detail"]
    assert _quiet_for(auto, 8) == [], "consume-first overrode a manual switch"
    assert box.live_email() == accounts["late"]["email"]
    assert "Manual switch to " + nums["late"] in box.all_log_text()

    # The hold only covers optional moves: at the limit it still moves.
    box.fake.set(accounts["late"]["email"], five_hour=100)
    sw = auto.wait_switch(nums["soon"], timeout=60)
    assert sw["trigger"] == "at-limit"


def test_gate_keeps_an_open_account_then_escapes_when_it_closes(box):
    accounts, nums = _setup(box, {
        # The usage endpoint rounds up to 100; the gate says 0.99 and open.
        "alice": {"five_hour": 30, "seven_day": 100, "gate_7d": 0.99},
        "bob": {"five_hour": 20, "seven_day": 97},
    }, active="alice")
    auto = box.start_auto()

    def gate_note():
        alice = box.accounts_by_email()[accounts["alice"]["email"]]
        return alice["usage"]["sevenDay"].get("gate")

    note = wait_until(gate_note, 60, "the open gate to show in aswap list")
    assert note["status"] == "open" and note["pct"] == 99.0
    probes = [r for r in box.fake.log() if r["path"] == "/v1/messages"]
    assert probes and probes[0]["account"] == accounts["alice"]["email"]
    assert probes[0]["gate"] in ("allowed", "allowed_warning")
    assert _quiet_for(auto, 6) == [], "left an account the gate still serves"

    box.fake.set(accounts["alice"]["email"], gate_7d=1.0)
    sw = auto.wait_switch(nums["bob"], timeout=60)
    assert sw["trigger"] in ("at-limit", "proactive")
    wait_until(lambda: box.live_email() == accounts["bob"]["email"], 20, "the live login to be bob")
    assert any(r["path"] == "/v1/messages" and r["status"] == 429 for r in box.fake.log())


def test_no_ping_pong_when_everything_is_hot(box):
    now = time.time()
    _accounts, _nums = _setup(box, {
        "alice": {"five_hour": 95, "seven_day": 40, "five_hour_reset": now + 2 * HOUR},
        "bob": {"five_hour": 94, "seven_day": 40, "five_hour_reset": now + 1.5 * HOUR},
    }, active="alice")
    auto = box.start_auto()
    _first_poll(auto)
    switches = _quiet_for(auto, 15)
    assert len(switches) <= 1, switches
    assert len(auto.switches()) <= 1


def _write_live_reading(box, five_hour, seven_day, seven_day_reset, five_hour_reset):
    """What Claude Code's statusline writes after a response: the account's
    unified rate-limit numbers, under ``<backup>/live_usage/<session>.json``."""
    directory = box.backup_root / "live_usage"
    directory.mkdir(parents=True, exist_ok=True)
    tmp = directory / ".e2e-session.tmp"
    tmp.write_text(json.dumps({
        "at": time.time() * 1000,
        "session": "e2e-session",
        "rate_limits": {
            "five_hour": {"used_percentage": five_hour, "resets_at": five_hour_reset},
            "seven_day": {"used_percentage": seven_day, "resets_at": seven_day_reset},
        },
    }), encoding="utf-8")
    os.replace(tmp, directory / "e2e-session.json")


# Measured before the fast burn pace: a jump from 50% to 99% under a 99.9%
# threshold waited out the minute of evidence the windowed burn rate needs
# (~60 s of real time from the first reading), whatever the tick cadence.
# Now the first reading that shows the jump fires the limit guard.
JUMP_SWITCH_BOUND_S = 15.0


def test_a_jump_toward_the_limit_switches_on_the_next_usage_poll(box):
    now = time.time()
    accounts, nums = _setup(box, {
        "alice": {"five_hour": 50, "seven_day": 20},
        "bob": {"five_hour": 10, "seven_day": 20, "seven_day_reset": now + 2 * DAY},
    }, active="alice", threshold=99.9)
    auto = box.start_auto()
    _first_poll(auto)
    assert _quiet_for(auto, 2) == []

    jumped = time.monotonic()
    box.fake.set(accounts["alice"]["email"], five_hour=99)
    sw = auto.wait_switch(nums["bob"], timeout=90)
    took = time.monotonic() - jumped
    print(f"jump to switch (usage API): {took:.1f}s")
    assert sw["trigger"] == "proactive"
    assert took < JUMP_SWITCH_BOUND_S, f"switched {took:.1f}s after the jump"
    wait_until(lambda: box.live_email() == accounts["bob"]["email"], 20, "the live login to be bob")


def test_a_live_statusline_jump_switches_within_a_tick_or_two(box):
    now = time.time()
    alice_week = now + 3 * DAY
    alice_5h = now + 2 * HOUR
    accounts, nums = _setup(box, {
        "alice": {"five_hour": 50, "seven_day": 20,
                  "seven_day_reset": alice_week, "five_hour_reset": alice_5h},
        "bob": {"five_hour": 10, "seven_day": 20, "seven_day_reset": now + 5 * DAY},
    }, active="alice", threshold=99.9)
    auto = box.start_auto()
    _first_poll(auto)
    assert _quiet_for(auto, 2) == []

    # The usage endpoint still says 50%; only the statusline sees the burn.
    jumped = time.monotonic()
    _write_live_reading(box, 99, 20, alice_week, alice_5h)
    sw = auto.wait_switch(nums["bob"], timeout=90)
    took = time.monotonic() - jumped
    print(f"jump to switch (live statusline): {took:.1f}s")
    assert sw["trigger"] == "proactive"
    assert took < JUMP_SWITCH_BOUND_S, f"switched {took:.1f}s after the jump"
    assert box.fake.state(accounts["alice"]["email"])["fiveHour"] == 50.0


def test_usage_429_backs_off_and_keeps_the_last_known_numbers(box):
    accounts, _nums = _setup(box, {
        "alice": {"five_hour": 10, "seven_day": 10},
        "bob": {"five_hour": 20, "seven_day": 30},
    }, active="alice")
    bob = accounts["bob"]["email"]
    auto = box.start_auto()
    _first_poll(auto)

    def bob_usage_requests():
        return [r for r in box.fake.log()
                if r["path"] == "/api/oauth/usage" and r["account"] == bob]

    # The usage endpoint starts refusing bob (its request budget, not his
    # quota) and asks for a 3-second wait (Retry-After is real seconds; a
    # short ask is honoured as given).
    box.fake.set(bob, usage_429_budget=1000, retry_after_s=3, seven_day=45)
    wait_until(
        lambda: next((r for r in bob_usage_requests() if r["status"] == 429), None),
        60, "a 429 for bob",
    )
    time.sleep(10)
    refused = [r["t"] for r in bob_usage_requests() if r["status"] == 429]
    gaps = [b - a for a, b in itertools.pairwise(refused)]
    assert gaps, "no retry at all within 10s"
    assert min(gaps) >= 2.9, f"retried before Retry-After elapsed: gaps {gaps}"
    # The last-known numbers are still what aswap shows and decides on.
    assert box.accounts_by_email()[bob]["usage"]["sevenDay"]["pct"] == 30.0

    box.fake.set(bob, usage_429_budget=0)
    wait_until(
        lambda: box.accounts_by_email()[bob]["usage"]["sevenDay"]["pct"] == 45.0,
        90, "bob's usage to recover after the back-off",
    )
    assert not auto.switches()
