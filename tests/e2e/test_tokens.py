"""OAuth refresh with refresh-token rotation, end to end through the fake."""

from __future__ import annotations

from tests.e2e.conftest import wait_until
from tests.e2e.fake_claude import token_id


def test_refresh_rotates_and_persists_and_never_prints_a_token(box):
    alice = box.account("alice", five_hour=11, seven_day=22)
    bob = box.account("bob", five_hour=5, seven_day=5)
    carol = box.account("carol", five_hour=7, seven_day=7, access_ttl_s=120)
    alice_login = box.login(alice["email"])
    box.aswap("add")
    carol_login = box.login(carol["email"])  # its access token is within the
    box.aswap("add")                         # 5-minute refresh buffer already
    box.login(bob["email"])
    box.aswap("add")

    # Server side, alice's access token dies early (revoked/rotated
    # elsewhere): her next usage read gets a 401, aswap refreshes with her
    # stored refresh token and retries.
    box.fake.set(alice["email"], expire_access_tokens=True)
    listed = box.aswap_json("list", "--json", "--debug")
    usage = {a["email"]: a.get("usage") for a in listed["claude"]["accounts"]}
    assert usage[alice["email"]]["sevenDay"]["pct"] == 22.0
    assert usage[carol["email"]]["sevenDay"]["pct"] == 7.0

    for who, login in ((alice, alice_login), (carol, carol_login)):
        state = box.fake.state(who["email"])
        # Rotation: the login's refresh token is spent, exactly one new one lives.
        assert state["spentRefreshIds"] == [token_id(login["refreshToken"])]
        assert len(state["liveRefreshIds"]) == 1

    # The rotated credential was persisted: switching to alice installs the
    # NEW refresh token (the spent one would be invalid_grant forever).
    box.aswap("switch", "1")
    live = box.live_credentials()
    assert token_id(live["refreshToken"]) == box.fake.state(alice["email"])["liveRefreshIds"][0]
    assert live["refreshToken"] != alice_login["refreshToken"]

    # And inside a running engine: carol's new access token expires too.
    box.fake.set(carol["email"], expire_access_tokens=True)
    box.configure(intervalSeconds=1, cooldownSeconds=0)
    auto = box.start_auto("--debug")
    wait_until(lambda: box.fake.state(carol["email"])["spentRefreshTokens"] >= 2, 60,
               "the engine to refresh carol")
    auto.stop()

    # Every token the fake issued, in every output and log aswap produced
    # (debug logging included): none may appear.
    box.assert_no_secret_in_output()


def test_dead_refresh_token_is_quarantined_not_retried(box):
    alice = box.account("alice")
    bob = box.account("bob")
    for acct in (alice, bob):
        box.login(acct["email"])
        box.aswap("add")
    box.fake.set(alice["email"], expire_access_tokens=True, refresh_dead=True)

    def status():
        return box.accounts_by_email()[alice["email"]]["usageStatus"]

    wait_until(lambda: status() == "relogin_required", 30, "alice to need a re-login")
    grants = [r for r in box.fake.log() if r["path"] == "/v1/oauth/token"]
    assert grants and all(r["outcome"] == "invalid_grant" for r in grants)
    # Quarantined: more listings do not keep POSTing the dead grant.
    before = len(grants)
    for _ in range(3):
        box.aswap("list", "--json")
    after = len([r for r in box.fake.log() if r["path"] == "/v1/oauth/token"])
    assert after == before
    box.assert_no_secret_in_output()
