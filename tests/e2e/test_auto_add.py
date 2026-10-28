"""New Claude Code logins picked up by a running ``aswap auto``."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
import unicodedata

from tests.e2e.conftest import KEYCHAIN_SERVICE, wait_until


def _start(box, *names, auto_add=True):
    accounts = [box.account(n, five_hour=10, seven_day=10) for n in names]
    for acct in accounts:
        box.login(acct["email"])
        box.aswap("add")
    box.configure(intervalSeconds=1, cooldownSeconds=0, autoAddAccounts=auto_add)
    auto = box.start_auto()
    auto.wait_for(lambda ev: any(e["event"] == "poll" for e in ev), 60, "the first poll")
    return accounts, auto


def _slots(box):
    return {a["email"]: a for a in box.list_json()["claude"]["accounts"]}


def test_a_new_login_is_added_without_aswap_add(box):
    (alice,), auto = _start(box, "alice")
    bob = box.account("bob", five_hour=33, seven_day=44)
    bob_tokens = box.login(bob["email"])

    wait_until(lambda: bob["email"] in _slots(box), 60, "bob to be auto-added")
    slot = _slots(box)[bob["email"]]
    assert slot["number"] == 2
    assert "Auto-added account 2" in box.all_log_text()

    # It is a working slot holding bob's own credential: switching away and
    # back restores exactly the tokens bob's /login wrote.
    box.aswap("switch", "1")
    box.aswap("switch", "2")
    assert box.live_credentials()["refreshToken"] == bob_tokens["refreshToken"]
    wait_until(
        lambda: (_slots(box)[bob["email"]].get("usage") or {}).get("sevenDay", {}).get("pct") == 44.0,
        30, "bob's usage from the fake",
    )


def test_a_half_written_login_is_never_added_under_the_wrong_identity(box):
    """``/login`` writes ``~/.claude.json`` and the credential separately.
    Between the two, the config names bob while the credential is still
    alice's; auto-add must not file alice's tokens under bob."""
    (alice,), auto = _start(box, "alice")
    alice_rt = box.live_credentials()["refreshToken"]
    bob = box.account("bob")
    bob_tokens = box.login(bob["email"], pause=8.0)

    wait_until(lambda: bob["email"] in _slots(box), 60, "bob to be auto-added")
    assert "its credential belongs to another account" in box.all_log_text()
    box.aswap("switch", "1")
    assert box.live_credentials()["refreshToken"] == alice_rt
    box.aswap("switch", "2")
    assert box.live_credentials()["refreshToken"] == bob_tokens["refreshToken"]
    # The profile endpoint was asked who the mid-login credential belonged to.
    owners = [r["account"] for r in box.fake.log() if r["path"] == "/api/oauth/profile"]
    assert alice["email"] in owners and bob["email"] in owners


def test_auto_add_off_leaves_new_logins_alone(box):
    (alice,), auto = _start(box, "alice", auto_add=False)
    bob = box.account("bob")
    box.login(bob["email"])
    time.sleep(10)  # many polls on the scaled clock
    assert list(_slots(box)) == [alice["email"]]
    assert "Auto-added" not in box.all_log_text()
    # A manual add still works.
    box.aswap("add")
    assert bob["email"] in _slots(box)


def _profile_credentials_writer(box, profile_dir: str):
    """Write a session profile's credential the way Claude Code would."""
    if sys.platform == "darwin":
        digest = hashlib.sha256(unicodedata.normalize("NFC", profile_dir).encode()).hexdigest()[:8]
        service = f"{KEYCHAIN_SERVICE}-{digest}"

        def write(blob: str) -> None:
            subprocess.run(
                ["/usr/bin/security", "add-generic-password", "-U",
                 "-a", box.env["USER"], "-s", service, "-X", blob.encode().hex()],
                check=True, capture_output=True, timeout=30,
            )
        return write

    from pathlib import Path

    def write(blob: str) -> None:
        (Path(profile_dir) / ".credentials.json").write_text(blob, encoding="utf-8")
    return write


def test_relogin_clears_a_needs_login_slot(box):
    alice, bob = (box.account(n, five_hour=10, seven_day=10) for n in ("alice", "bob"))
    for acct in (alice, bob):
        box.login(acct["email"])
        box.aswap("add")
    box.aswap("switch", "1")

    # Account 2 gets a session profile (as `aswap run 2` would), then Claude
    # Code, running in it, meets invalid_grant and empties the tokens in place.
    box.aswap("profile", "hydrate", "2")
    profile = box.aswap("profile", "path", "2").stdout.strip()
    husk = {"claudeAiOauth": {
        "accessToken": "", "refreshToken": "", "expiresAt": 0,
        "scopes": ["user:inference", "user:profile"], "subscriptionType": "max",
    }}
    _profile_credentials_writer(box, profile)(json.dumps(husk))
    box.fake.set(bob["email"], refresh_dead=True)

    def needs_login():
        slot = _slots(box)[bob["email"]]
        return slot["ineligibleReason"] == "needs_login"

    wait_until(needs_login, 30, "slot 2 to be marked needs login")
    assert "needs login" in box.aswap("list").stdout

    # The owner logs in again as bob on the main login; a running auto picks
    # it up and the slot is back in rotation.
    box.configure(intervalSeconds=1, cooldownSeconds=0)
    auto = box.start_auto()
    auto.wait_for(lambda ev: any(e["event"] == "poll" for e in ev), 60, "the first poll")
    box.fake.set(bob["email"], refresh_dead=False)
    fresh = box.login(bob["email"])
    wait_until(lambda: not needs_login(), 60, "the needs-login mark to clear")
    assert "logged in again" in box.all_log_text()
    box.aswap("switch", "1")
    box.aswap("switch", "2")
    assert box.live_credentials()["refreshToken"] == fresh["refreshToken"]
