"""``aswap renumber`` and lowest-free numbering, with the real binary.

The headline scenario renumbers from a second process while ``aswap auto``
runs: the active account must stay active (by identity, whatever its number
becomes) and the engine must go on switching to the right accounts.
"""

from __future__ import annotations

import json

from tests.e2e.conftest import wait_until
from tests.e2e.test_auto import _first_poll, _quiet_for, _setup


def _numbers(box) -> dict[str, int]:
    """email -> slot number, from `aswap list --json`."""
    return {a["email"]: a["number"] for a in box.list_json()["claude"]["accounts"]}


def test_renumber_under_a_running_auto(box):
    accounts, nums = _setup(box, {
        "alice": {"five_hour": 10, "seven_day": 20},
        "bob": {"five_hour": 15, "seven_day": 25},
        "carol": {"five_hour": 12, "seven_day": 22},
        "dave": {"five_hour": 30, "seven_day": 30},
    }, active="dave")
    email = {name: acct["email"] for name, acct in accounts.items()}
    # Leave holes: 1 and 3 go, bob stays at 2 and dave (active) at 4.
    box.aswap("remove", nums["alice"], input="y\n")
    box.aswap("remove", nums["carol"], input="y\n")
    assert _numbers(box) == {email["bob"]: 2, email["dave"]: 4}

    auto = box.start_auto()
    _first_poll(auto)
    assert _quiet_for(auto, 2) == []

    # Another process renumbers while the engine runs.
    preview = box.aswap_json("renumber", "--dry-run", "--json")
    assert preview["dryRun"] is True and preview["changed"] is True
    assert [(r["from"], r["to"]) for r in preview["accounts"]] == [(2, 1), (4, 2)]
    assert _numbers(box) == {email["bob"]: 2, email["dave"]: 4}, "dry run changed something"

    done = box.aswap("renumber", "--yes")
    assert "Renumbered 2 account(s) to 1..2" in done.stdout
    assert _numbers(box) == {email["bob"]: 1, email["dave"]: 2}

    # The active account is unchanged by identity, and nothing switched.
    assert box.live_email() == email["dave"]
    status = box.aswap_json("status", "--json")
    assert status["active"]["number"] == 2
    assert status["active"]["email"] == email["dave"]
    assert _quiet_for(auto, 5) == [], "the renumber made the engine switch"
    assert auto.proc.poll() is None, "aswap auto died during the renumber"
    assert not [
        e for e in auto.events()
        if e.get("event") == "error" and not e.get("transient", True)
    ]

    # The engine still switches correctly, by the new numbers.
    box.fake.set(email["dave"], five_hour=93)
    sw = auto.wait_switch("1", timeout=60)
    assert sw["to"]["email"] == email["bob"]
    assert sw["from"] == {"number": 2, "email": email["dave"]}
    wait_until(lambda: box.live_email() == email["bob"], 20, "the live login to be bob")
    assert box.live_credentials()["refreshToken"]

    # Again a no-op now.
    assert "Already numbered 1..2" in box.aswap("renumber", "--yes").stdout


def test_renumber_by_email_is_identical_from_two_layouts(box):
    names = ["zed", "amy", "max", "kim"]
    accounts = {n: box.account(n) for n in names}
    for n in names:
        box.login(accounts[n]["email"])
        box.aswap("add")
    expected = {
        accounts[n]["email"]: i for i, n in enumerate(sorted(names), 1)
    }

    out = box.aswap_json("renumber", "--by-email", "--yes", "--json")
    assert out["renumbered"] is True and out["order"] == "email"
    assert out["snapshot"]
    first = _numbers(box)
    assert first == expected

    # Shuffle into a different layout with a hole, then renumber again.
    box.aswap("swap", "1", "4")
    box.aswap("move", "2", "7")
    box.aswap("swap", "3", "7")
    assert _numbers(box) != expected

    # --json cannot prompt: it needs --yes.
    refused = box.aswap("renumber", "--by-email", "--json", check=False)
    assert refused.returncode == 1
    assert json.loads(refused.stdout)["error"]["type"] == "ValidationError"

    box.aswap("renumber", "--by-email", "--yes")
    assert _numbers(box) == first

    # Credentials followed their accounts: each one switches to itself.
    for n in names:
        box.aswap("switch", str(expected[accounts[n]["email"]]))
        assert box.live_email() == accounts[n]["email"]


def test_new_account_fills_the_gap_without_inheriting(box):
    alice = box.account("alice", five_hour=10, seven_day=10)
    bob = box.account("bob", five_hour=77, seven_day=66)
    carol = box.account("carol", five_hour=20, seven_day=20)
    for acct in (alice, bob, carol):
        box.login(acct["email"])
        box.aswap("add")
    box.aswap("alias", "2", "bee")
    box.aswap("switch", "1")
    # Prime bob's usage cache under number 2.
    assert box.accounts_by_email()[bob["email"]]["usage"]["fiveHour"]["pct"] == 77.0

    box.aswap("remove", "2", input="y\n")

    dave = box.account("dave", five_hour=33, seven_day=44)
    box.login(dave["email"])
    assert "Added Account 2" in box.aswap("add").stdout

    listed = {a["number"]: a for a in box.list_json()["claude"]["accounts"]}
    assert sorted(listed) == [1, 2, 3]
    assert listed[2]["email"] == dave["email"]
    assert not listed[2].get("alias"), "the new account inherited bob's alias"
    assert listed[2]["usage"]["fiveHour"]["pct"] == 33.0
    assert listed[2]["usage"]["sevenDay"]["pct"] == 44.0
    # Nobody owns the removed account's alias any more.
    done = box.aswap("switch", "bee", check=False)
    assert done.returncode != 0
