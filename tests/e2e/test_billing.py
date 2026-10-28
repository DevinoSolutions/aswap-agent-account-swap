"""Plan and subscription status, from the profile endpoint and from a
claude.ai web session."""

from __future__ import annotations

import json

from tests.e2e.conftest import TIME_SCALE, wait_until

LAPSED = {"subscription_status": "canceled", "tier": "default_claude_ai", "org_type": "claude_free"}


def _billing(box, email):
    return box.accounts_by_email()[email]["billing"]


def test_lapsed_then_revived_subscription_is_noticed_by_auto(box):
    alice = box.account("alice", five_hour=10, seven_day=10)
    bob = box.account("bob", five_hour=0, seven_day=0, **LAPSED)
    for acct in (bob, alice):
        box.login(acct["email"])
        box.aswap("add")
    assert box.live_email() == alice["email"]
    billing = _billing(box, bob["email"])
    assert billing["subscriptionStatus"] == "canceled"
    assert billing["rateLimitTier"] == "default_claude_ai"

    box.configure(intervalSeconds=1, cooldownSeconds=0)
    auto = box.start_auto()
    auto.wait_for(lambda ev: any(e["event"] == "poll" for e in ev), 60, "the first poll")

    # Bob buys Max again. Documented: noticed within ~5 minutes in the
    # background (LAPSED_RECHECK_S + stagger), here on the scaled clock.
    box.fake.set(bob["email"], subscription_status="active",
                 tier="default_claude_max_20x", org_type="claude_max")
    budget = (5 * 60 + 60) * TIME_SCALE + 20
    wait_until(lambda: "is subscribed again" in box.all_log_text(), budget,
               "the revived plan to be logged")
    billing = _billing(box, bob["email"])
    assert billing["subscriptionStatus"] == "active"
    assert billing["rateLimitTier"] == "default_claude_max_20x"
    profile_reads = [r for r in box.fake.log()
                     if r["path"] == "/api/oauth/profile" and r["account"] == bob["email"]]
    assert profile_reads[-1]["subscription"] == "active"


def test_switching_onto_a_lapsed_account_rereads_its_plan(box):
    alice = box.account("alice")
    bob = box.account("bob", **LAPSED)
    for acct in (bob, alice):
        box.login(acct["email"])
        box.aswap("add")
    assert _billing(box, bob["email"])["subscriptionStatus"] == "canceled"
    box.fake.set(bob["email"], subscription_status="active", tier="default_claude_max_5x",
                 org_type="claude_max")
    box.aswap("switch", "1")  # bob is slot 1
    wait_until(lambda: _billing(box, bob["email"])["subscriptionStatus"] == "active",
               30, "the plan re-read on becoming active")


def test_claude_ai_session_gives_exact_billing_dates(box):
    """A captured claude.ai session (``billing_sessions.json``, as ``aswap
    billing login`` leaves it) is used for the exact renewal date instead of
    the estimate from the profile."""
    alice = box.account("alice")
    box.login(alice["email"])
    box.aswap("add")
    # Seeded before anything lists the account: a plan estimate already
    # cached from the profile would not be due again for this run.
    stamp = "2026-10-28T00:00:00Z"
    sessions = {"version": 1, "sessions": {f"{alice['email']}|{alice['orgUuid']}": {
        "email": alice["email"], "organizationUuid": alice["orgUuid"],
        "sessionKey": box.fake.session_key(alice["email"]),
        "capturedAt": stamp, "lastOkAt": stamp, "status": "ok",
    }}}
    (box.backup_root / "billing_sessions.json").write_text(json.dumps(sessions), encoding="utf-8")

    status = box.aswap("billing", "status").stdout
    assert "session: ok" in status
    billing = _billing(box, alice["email"])
    assert billing["source"] == "exact"
    assert billing["nextBillingEstimated"] is False
    assert billing["sessionState"] == "ok"
    calls = [r for r in box.fake.log() if r["path"].endswith("/subscription_details")]
    assert calls and calls[0]["status"] == 200 and calls[0]["account"] == alice["email"]
