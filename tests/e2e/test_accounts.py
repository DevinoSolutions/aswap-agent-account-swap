"""Adding, listing and switching accounts with the real binary."""

from __future__ import annotations

import json


def test_add_list_switch(box):
    alice = box.account("alice", five_hour=10, seven_day=20)
    bob = box.account("bob", five_hour=50, seven_day=60)

    alice_login = box.login(alice["email"])
    assert "Added Account 1" in box.aswap("add").stdout
    bob_login = box.login(bob["email"])
    assert "Added Account 2" in box.aswap("add").stdout

    # Usage comes from the fake, per account, through the real HTTP stack.
    listed = box.list_json()["claude"]
    assert listed["activeAccountNumber"] == 2
    by_num = {a["number"]: a for a in listed["accounts"]}
    assert by_num[1]["email"] == alice["email"]
    assert by_num[1]["usage"]["sevenDay"]["pct"] == 20.0
    assert by_num[2]["usage"]["fiveHour"]["pct"] == 50.0
    assert by_num[1]["billing"]["rateLimitTier"] == "default_claude_max_5x"
    assert [a["active"] for a in listed["accounts"]] == [False, True]

    # A second `aswap add` of the live login updates the slot, no duplicate.
    box.aswap("add")
    assert len(box.list_json()["claude"]["accounts"]) == 2

    switched = box.aswap_json("switch", "1", "--json")
    assert switched["switched"] is True
    assert switched["to"] == {"number": 1, "email": alice["email"]}

    # The live login Claude Code reads is now alice's, byte for byte the
    # tokens her /login produced, and ~/.claude.json names her while keeping
    # every unrelated key.
    live = box.live_credentials()
    assert live["refreshToken"] == alice_login["refreshToken"]
    assert live["accessToken"] == alice_login["accessToken"]
    assert box.live_email() == alice["email"]
    config = json.loads(box.config_path.read_text(encoding="utf-8"))
    assert config["oauthAccount"]["accountUuid"] == alice["uuid"]
    assert config["hasCompletedOnboarding"] is True and "userID" in config

    status = box.aswap_json("status", "--json")
    assert status["active"]["number"] == 1
    assert status["totalManagedAccounts"] == 2

    # And back, by email this time.
    box.aswap("switch", bob["email"])
    live = box.live_credentials()
    assert live["refreshToken"] == bob_login["refreshToken"]
    assert box.live_email() == bob["email"]

    # Nothing here needed a token refresh: every token is still live.
    for who in (alice, bob):
        assert box.fake.state(who["email"])["spentRefreshTokens"] == 0


def test_disabled_account_is_skipped_by_bare_switch(box):
    names = ["one", "two", "three"]
    accounts = [box.account(n) for n in names]
    for acct in accounts:
        box.login(acct["email"])
        box.aswap("add")
    box.aswap("switch", "1")
    box.aswap("disable", "2")
    listed = {a["number"]: a for a in box.list_json()["claude"]["accounts"]}
    assert listed[2]["eligible"] is False

    box.aswap("switch")  # rotate: 1 -> (2 is disabled) -> 3
    assert box.live_email() == accounts[2]["email"]
    # An explicit switch to a disabled account is still allowed.
    box.aswap("switch", "2")
    assert box.live_email() == accounts[1]["email"]


def test_refused_endpoint_override_sends_nothing(box):
    """The override guard, seen from outside: a non-loopback http base makes
    aswap exit before any request leaves the machine."""
    alice = box.account("alice")
    box.login(alice["email"])
    box.env["ASWAP_API_BASE_URL"] = "http://example.com"
    done = box.aswap("list", "--json", check=False)
    assert done.returncode == 2
    assert "refused" in done.stderr + done.stdout
    assert box.fake.log() == []
    assert not box.backup_root.exists()
