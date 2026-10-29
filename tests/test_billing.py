"""Tests for billing-date estimation and the per-slot billing cache."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import pytest

from claude_swap import billing, oauth

# Captured at import, before conftest's autouse stub replaces it per test.
_REAL_REFRESH = billing.BillingCache.refresh


@pytest.fixture(autouse=True)
def _real_refresh(monkeypatch):
    monkeypatch.setattr(billing.BillingCache, "refresh", _REAL_REFRESH)


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


class TestNextRenewal:
    def test_same_day_next_month(self):
        anchor = _dt("2026-07-15T06:18:03")
        assert billing.next_renewal(anchor, _dt("2026-09-23T12:00:00")) == _dt(
            "2026-10-15T06:18:03"
        )

    def test_later_this_month(self):
        anchor = _dt("2026-07-15T06:18:03")
        assert billing.next_renewal(anchor, _dt("2026-09-02T00:00:00")) == _dt(
            "2026-09-15T06:18:03"
        )

    def test_before_first_renewal(self):
        anchor = _dt("2026-09-20T10:00:00")
        assert billing.next_renewal(anchor, _dt("2026-09-23T00:00:00")) == _dt(
            "2026-10-20T10:00:00"
        )

    def test_on_the_renewal_instant_moves_to_next_period(self):
        anchor = _dt("2026-07-15T06:18:03")
        assert billing.next_renewal(anchor, _dt("2026-09-15T06:18:03")) == _dt(
            "2026-10-15T06:18:03"
        )

    def test_month_end_clamps_without_drift(self):
        anchor = _dt("2026-01-31T09:00:00")
        assert billing.next_renewal(anchor, _dt("2026-02-10T00:00:00")) == _dt(
            "2026-02-28T09:00:00"
        )
        # Computed from the anchor, not chained from Feb 28.
        assert billing.next_renewal(anchor, _dt("2026-03-01T00:00:00")) == _dt(
            "2026-03-31T09:00:00"
        )

    def test_year_rollover(self):
        anchor = _dt("2025-12-05T00:00:00")
        assert billing.next_renewal(anchor, _dt("2026-12-20T00:00:00")) == _dt(
            "2027-01-05T00:00:00"
        )


PROFILE = {
    "account": {"uuid": "acc-1", "email": "a@example.com"},
    "organization": {
        "uuid": "org-1",
        "organization_type": "claude_max",
        "subscription_status": "active",
        "subscription_created_at": "2026-07-15T06:18:03.003319Z",
    },
}


class TestParseAndView:
    def test_parse_profile(self):
        assert billing.parse_profile(PROFILE) == {
            "subscriptionCreatedAt": "2026-07-15T06:18:03.003319Z",
            "subscriptionStatus": "active",
            "plan": "claude_max",
            "rateLimitTier": PROFILE["organization"].get("rate_limit_tier"),
        }

    @pytest.mark.parametrize(
        "data",
        [
            None,
            {},
            {"organization": None},
            {"organization": {"subscription_created_at": None}},
            {"organization": {"subscription_created_at": "not a date"}},
        ],
    )
    def test_parse_profile_without_anchor(self, data):
        assert billing.parse_profile(data) is None

    def test_view_active(self):
        rec = billing.parse_profile(PROFILE)
        now = _dt("2026-09-23T12:00:00").timestamp()
        view = billing.billing_view(rec, now)
        assert view["nextBillingAt"] == "2026-10-15T06:18:03.003319+00:00"
        assert view["nextBillingEstimated"] is True
        assert view["plan"] == "claude_max"

    def test_view_lapsed_has_no_next_date(self):
        rec = {**billing.parse_profile(PROFILE), "subscriptionStatus": "past_due"}
        view = billing.billing_view(rec, time.time())
        assert view["nextBillingAt"] is None
        assert billing.billing_text(view) == "subscription past due"

    def test_view_none(self):
        assert billing.billing_view(None) is None
        assert billing.billing_view({"subscriptionCreatedAt": "junk"}) is None
        assert billing.billing_text(None) is None

    def test_text_active(self):
        view = billing.billing_view(billing.parse_profile(PROFILE))
        text = billing.billing_text(view)
        assert text.startswith("renews ")
        assert " in " in text

    def test_text_carries_date_and_local_time(self):
        view = {"nextBillingAt": "2099-10-15T06:18:03+00:00"}
        local = datetime.fromisoformat(view["nextBillingAt"]).astimezone()
        expected = local.strftime(f"%b {local.day} %H:%M")
        assert billing.billing_text(view).startswith(f"renews ~{expected}")


def _creds(token="tok", expires_in_s=3600, scopes=("user:inference", "user:profile")):
    oauth_blob = {
        "accessToken": token,
        "refreshToken": "rt",
        "expiresAt": int((time.time() + expires_in_s) * 1000),
    }
    if scopes is not None:
        oauth_blob["scopes"] = list(scopes)
    return json.dumps({"claudeAiOauth": oauth_blob})


def _info(num, email="a@example.com", org="org-1", creds=None):
    return (num, email, "Org", org, False, creds if creds is not None else _creds(), "")


class _Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def no_stagger(monkeypatch):
    monkeypatch.setattr(billing, "_LAUNCH_STAGGER_S", 0)


class TestBillingCache:
    def test_refresh_fetches_and_caches(self, tmp_path, monkeypatch, no_stagger):
        calls = []
        monkeypatch.setattr(
            oauth, "request_profile_data", lambda tok: calls.append(tok) or PROFILE
        )
        clock = _Clock()
        cache = billing.BillingCache(tmp_path, clock=clock)
        cache.refresh([_info(1, creds=_creds("t1")), _info(2, email="b@x", creds=_creds("t2"))])
        assert sorted(calls) == ["t1", "t2"]
        recs = cache.records({"1": ("a@example.com", "org-1"), "2": ("b@x", "org-1")})
        assert recs["1"]["subscriptionCreatedAt"] == "2026-07-15T06:18:03.003319Z"

        # Within the TTL: no network.
        calls.clear()
        cache.refresh([_info(1, creds=_creds("t1"))])
        assert calls == []

        # After the TTL: refetched.
        clock.t += billing.SUCCESS_TTL_S + 1
        cache.refresh([_info(1, creds=_creds("t1"))])
        assert calls == ["t1"]

    def test_identity_change_hides_and_refetches(self, tmp_path, monkeypatch, no_stagger):
        calls = []
        monkeypatch.setattr(
            oauth, "request_profile_data", lambda tok: calls.append(tok) or PROFILE
        )
        cache = billing.BillingCache(tmp_path, clock=_Clock())
        cache.refresh([_info(1)])
        assert cache.records({"1": ("other@x", "org-1")})["1"] is None
        calls.clear()
        cache.refresh([_info(1, email="other@x")])
        assert calls == ["tok"]

    @pytest.mark.parametrize(
        "creds",
        [
            _creds(expires_in_s=-60),  # expired: never refreshed here
            _creds(scopes=("user:inference",)),  # setup-token
            "",  # api-key / unreadable
            json.dumps({"claudeAiOauth": {}}),
        ],
        ids=["expired", "setup-token", "no-credentials", "no-access-token"],
    )
    def test_skips_unusable_tokens(self, tmp_path, monkeypatch, creds, no_stagger):
        def boom(tok):
            raise AssertionError("should not fetch")

        monkeypatch.setattr(oauth, "request_profile_data", boom)
        cache = billing.BillingCache(tmp_path, clock=_Clock())
        cache.refresh([_info(1, creds=creds)])
        assert cache.records({"1": ("a@example.com", "org-1")})["1"] is None

    def test_failure_keeps_last_good_and_backs_off(self, tmp_path, monkeypatch, no_stagger):
        monkeypatch.setattr(oauth, "request_profile_data", lambda tok: PROFILE)
        clock = _Clock()
        cache = billing.BillingCache(tmp_path, clock=clock)
        cache.refresh([_info(1)])

        calls = []

        def fail(tok):
            calls.append(tok)
            raise OSError("network down")

        monkeypatch.setattr(oauth, "request_profile_data", fail)
        clock.t += billing.SUCCESS_TTL_S + 1
        cache.refresh([_info(1)])
        assert calls == ["tok"]
        rec = cache.records({"1": ("a@example.com", "org-1")})["1"]
        assert rec["subscriptionCreatedAt"] == "2026-07-15T06:18:03.003319Z"

        # Backed off for FAILURE_RETRY_S, not retried every pass.
        calls.clear()
        clock.t += 60
        cache.refresh([_info(1)])
        assert calls == []
        clock.t += billing.FAILURE_RETRY_S
        cache.refresh([_info(1)])
        assert calls == ["tok"]

    def test_lapsed_row_is_rechecked_soon(self, tmp_path, monkeypatch, no_stagger):
        # A canceled account can be bought again any minute; holding the row
        # for SUCCESS_TTL_S kept "subscription canceled" up for a day after.
        canceled = json.loads(json.dumps(PROFILE))
        canceled["organization"]["subscription_status"] = "canceled"
        calls = []
        monkeypatch.setattr(
            oauth, "request_profile_data", lambda tok: calls.append(tok) or canceled
        )
        clock = _Clock()
        cache = billing.BillingCache(tmp_path, clock=clock)
        cache.refresh([_info(1)])
        calls.clear()
        clock.t += billing.LAPSED_TTL_S - 1
        cache.refresh([_info(1)])
        assert calls == []
        clock.t += 2
        cache.refresh([_info(1)])
        assert calls == ["tok"]

    def test_expire_refetches_that_identity_only(self, tmp_path, monkeypatch, no_stagger):
        calls = []
        monkeypatch.setattr(
            oauth, "request_profile_data", lambda tok: calls.append(tok) or PROFILE
        )
        cache = billing.BillingCache(tmp_path, clock=_Clock())
        accounts = [_info(1, creds=_creds("t1")), _info(2, email="b@x", creds=_creds("t2"))]
        cache.refresh(accounts)
        calls.clear()
        cache.expire(("a@example.com", "org-1"))
        cache.refresh(accounts)
        assert calls == ["t1"]

    def test_expire_without_a_row_is_a_no_op(self, tmp_path):
        cache = billing.BillingCache(tmp_path, clock=_Clock())
        cache.expire(("a@example.com", "org-1"))
        assert not (tmp_path / "billing.json").exists()

    def test_confirm_tier_asks_once_then_confirms_after_a_fetch(
        self, tmp_path, monkeypatch, no_stagger
    ):
        monkeypatch.setattr(oauth, "request_profile_data", lambda tok: PROFILE)
        clock = _Clock()
        cache = billing.BillingCache(tmp_path, clock=clock)
        cache.refresh([_info(1)])
        ident = ("a@example.com", "org-1")
        clock.t += 60
        assert cache.confirm_tier(ident) is False  # profile predates the reading
        row = cache._load()["1"]
        assert row["nextCheckAt"] == 0 and row["tierCheckAskedAt"] == clock.t
        clock.t += 60
        assert cache.confirm_tier(ident) is False  # still not re-read
        assert cache._load()["1"]["tierCheckAskedAt"] == clock.t - 60
        cache.refresh([_info(1)])
        assert cache.confirm_tier(ident) is True
        assert "tierCheckAskedAt" not in cache._load()["1"]

    def test_confirm_tier_without_a_row_has_nothing_to_wait_for(self, tmp_path):
        cache = billing.BillingCache(tmp_path, clock=_Clock())
        assert cache.confirm_tier(("a@example.com", "org-1")) is True

    def test_corrupt_cache_file_is_ignored(self, tmp_path, monkeypatch, no_stagger):
        (tmp_path / "billing.json").write_text("{not json", encoding="utf-8")
        monkeypatch.setattr(oauth, "request_profile_data", lambda tok: PROFILE)
        cache = billing.BillingCache(tmp_path, clock=_Clock())
        assert cache.records({"1": ("a@example.com", "org-1")})["1"] is None
        cache.refresh([_info(1)])
        assert cache.records({"1": ("a@example.com", "org-1")})["1"] is not None


class TestExact:
    NOW = _dt("2026-10-10T12:00:00").timestamp()

    def _rec(self, **kw):
        base = {
            "source": "exact",
            "subscriptionStatus": "active",
            "subscriptionCreatedAt": "2026-07-12T14:28:34Z",
            "currentPeriodEnd": "2026-10-20T09:00:00+00:00",
            "cancelAtPeriodEnd": False,
        }
        base.update(kw)
        return base

    def test_exact_renewal_is_not_estimated(self):
        view = billing.billing_view(self._rec(), self.NOW)
        assert view["source"] == "exact"
        assert view["nextBillingAt"] == "2026-10-20T09:00:00+00:00"
        assert view["nextBillingEstimated"] is False
        assert "~" not in billing.billing_text(view)

    def test_cancelling_shows_ends(self):
        view = billing.billing_view(self._rec(cancelAtPeriodEnd=True), self.NOW)
        assert view["endsAt"] == "2026-10-20T09:00:00+00:00"
        assert view["nextBillingAt"] is None
        assert billing.billing_text(view).startswith("ends ")
        assert billing.billing_text(view).endswith("(cancelling)")
        assert billing.ends_at_ts(view) == _dt("2026-10-20T09:00:00").timestamp()

    def test_past_exact_period_falls_back_to_estimate(self):
        view = billing.billing_view(self._rec(currentPeriodEnd="2026-10-01T00:00:00+00:00"), self.NOW)
        assert view["source"] == "estimate"
        assert view["nextBillingAt"] == "2026-10-12T14:28:34+00:00"

    def test_lapsed_exact_has_no_dates(self):
        view = billing.billing_view(self._rec(subscriptionStatus="canceled", currentPeriodEnd=None), self.NOW)
        assert view["nextBillingAt"] is None and view["endsAt"] is None
        assert billing.billing_text(view) == "subscription canceled"

    def test_expired_session_note(self):
        view = billing.billing_view(self._rec(), self.NOW, session_state="expired")
        assert "billing login" in billing.billing_text(view)


class FakeSessions:
    def __init__(self, rows):
        self.rows_ = rows
        self.marks = []

    def get(self, identity):
        return self.rows_.get(identity)

    def mark(self, identity, status):
        self.marks.append((identity, status))


class TestExactRefresh:
    INFO = [(3, "a@example.com", "Org", "org-1", False, "", "")]  # no OAuth token

    def test_session_fetch_writes_exact_row(self, tmp_path, monkeypatch):
        from claude_swap import billing_session as bs

        monkeypatch.setattr(
            bs,
            "fetch_subscription_details",
            lambda key, org: {"periodEnd": "2099-01-01T00:00:00+00:00", "cancelAtPeriodEnd": True, "status": "active", "interval": "monthly", "keys": ["x"]},
        )
        sessions = FakeSessions({("a@example.com", "org-1"): {"sessionKey": "sk", "status": "ok"}})
        cache = billing.BillingCache(tmp_path, sessions=sessions)
        cache.refresh(self.INFO)
        row = cache.records({"3": ("a@example.com", "org-1")})["3"]
        assert row["source"] == "exact"
        assert row["cancelAtPeriodEnd"] is True
        assert sessions.marks == [(("a@example.com", "org-1"), "ok")]
        assert row["nextCheckAt"] - row["fetchedAt"] == billing.EXACT_TTL_S

    def test_expired_session_is_marked(self, tmp_path, monkeypatch):
        from claude_swap import billing_session as bs

        def expired(key, org):
            raise bs.SessionExpired("x")

        monkeypatch.setattr(bs, "fetch_subscription_details", expired)
        sessions = FakeSessions({("a@example.com", "org-1"): {"sessionKey": "sk", "status": "ok"}})
        cache = billing.BillingCache(tmp_path, sessions=sessions)
        cache.refresh(self.INFO)
        assert sessions.marks == [(("a@example.com", "org-1"), "expired")]
        row = cache.records({"3": ("a@example.com", "org-1")})["3"]
        assert row["nextCheckAt"] - cache.clock() <= billing.FAILURE_RETRY_S + 1


def _profile(tier, plan="claude_max", status="active", created="2026-07-15T06:18:03Z"):
    org = {
        "uuid": "org-1",
        "organization_type": plan,
        "subscription_status": status,
        "subscription_created_at": created,
        "rate_limit_tier": tier,
    }
    return {"account": {"uuid": "acc-1"}, "organization": org}


class TestCheckTiers:
    def test_reports_the_live_tier_and_the_change(self, tmp_path, monkeypatch, no_stagger):
        live = {"t1": _profile("default_claude_max_5x"), "t2": _profile("default_claude_max_5x")}
        monkeypatch.setattr(oauth, "request_profile_data", lambda tok: live[tok])
        cache = billing.BillingCache(tmp_path, clock=_Clock())
        accounts = [_info(1, creds=_creds("t1")), _info(2, email="b@x", creds=_creds("t2"))]
        cache.refresh(accounts)

        live["t2"] = _profile("default_claude_max_20x")  # upgraded on claude.ai
        out = cache.check_tiers(accounts)
        assert out["1"] == {"tier": "default_claude_max_5x", "previousTier": "default_claude_max_5x",
                            "plan": "claude_max", "status": "active", "error": None}
        assert out["2"]["tier"] == "default_claude_max_20x"
        assert out["2"]["previousTier"] == "default_claude_max_5x"
        rows = cache._load()
        assert rows["2"]["rateLimitTier"] == "default_claude_max_20x"
        assert rows["2"]["nextCheckAt"] == 0  # dates re-read on the next listing
        assert rows["1"]["nextCheckAt"] > 0

    def test_a_never_subscribed_account_still_reports_its_tier(
        self, tmp_path, monkeypatch, no_stagger
    ):
        free = _profile("default_claude_ai", plan="claude_free", status=None, created=None)
        monkeypatch.setattr(oauth, "request_profile_data", lambda tok: free)
        cache = billing.BillingCache(tmp_path, clock=_Clock())
        out = cache.check_tiers([_info(1)])
        assert out["1"]["tier"] == "default_claude_ai" and out["1"]["previousTier"] is None

    def test_unusable_token_and_fetch_failure_are_reported(
        self, tmp_path, monkeypatch, no_stagger
    ):
        def fail(tok):
            raise OSError("down")

        monkeypatch.setattr(oauth, "request_profile_data", fail)
        cache = billing.BillingCache(tmp_path, clock=_Clock())
        out = cache.check_tiers([_info(1), _info(2, email="b@x", creds=_creds(expires_in_s=-60))])
        assert out["1"]["error"].startswith("profile fetch failed")
        assert out["2"]["error"] == "no usable token"
        assert out["1"]["tier"] is None


class TestRefreshTierChange:
    def test_refresh_logs_a_tier_change(self, tmp_path, monkeypatch, no_stagger, caplog):
        live = {"p": _profile("default_claude_max_5x")}
        monkeypatch.setattr(oauth, "request_profile_data", lambda tok: live["p"])
        cache = billing.BillingCache(tmp_path, clock=_Clock())
        cache.refresh([_info(1)])
        live["p"] = _profile("default_claude_max_20x")
        with caplog.at_level("INFO", logger="claude-swap"):
            cache.refresh([_info(1)], force=True)
        assert "5x -> 20x" in caplog.text


class TestLapseVerdictFlip:
    """2026-10-27: a re-subscribed account kept showing "subscription
    canceled". The flip itself must be one INFO line, both ways."""

    def _cache_with(self, tmp_path, **row):
        cache = billing.BillingCache(tmp_path, clock=_Clock())
        cache._save({"1": {"email": "a@example.com", "organizationUuid": "org-1",
                           "nextCheckAt": 9e12, **row}})
        return cache

    def test_check_tiers_logs_a_revival(self, tmp_path, monkeypatch, no_stagger, caplog):
        monkeypatch.setattr(oauth, "request_profile_data", lambda tok: _profile("default_claude_max_5x"))
        cache = self._cache_with(tmp_path, subscriptionStatus="canceled",
                                 rateLimitTier="default_claude_ai")
        with caplog.at_level("INFO", logger="claude-swap"):
            cache.check_tiers([_info(1)])
        assert "Account 1 (a@example.com) is subscribed again: 5x" in caplog.text
        assert not billing.looks_lapsed(cache._load()["1"])

    def test_check_tiers_logs_a_lapse(self, tmp_path, monkeypatch, no_stagger, caplog):
        monkeypatch.setattr(
            oauth, "request_profile_data",
            lambda tok: _profile("default_claude_max_5x", status="canceled"),
        )
        cache = self._cache_with(tmp_path, subscriptionStatus="active",
                                 rateLimitTier="default_claude_max_5x")
        with caplog.at_level("INFO", logger="claude-swap"):
            cache.check_tiers([_info(1)])
        assert "Account 1 (a@example.com) subscription lapsed: canceled" in caplog.text

    def test_refresh_logs_a_revival(self, tmp_path, monkeypatch, no_stagger, caplog):
        monkeypatch.setattr(oauth, "request_profile_data", lambda tok: _profile("default_claude_max_20x"))
        cache = self._cache_with(tmp_path, subscriptionStatus="canceled",
                                 rateLimitTier="default_claude_max_20x")
        with caplog.at_level("INFO", logger="claude-swap"):
            cache.refresh([_info(1)], force=True)
        assert caplog.text.count("is subscribed again: 20x") == 1

    def test_no_flip_no_line(self, tmp_path, monkeypatch, no_stagger, caplog):
        monkeypatch.setattr(
            oauth, "request_profile_data",
            lambda tok: _profile("default_claude_max_5x", status="canceled"),
        )
        cache = self._cache_with(tmp_path, subscriptionStatus="canceled",
                                 rateLimitTier="default_claude_max_5x")
        with caplog.at_level("INFO", logger="claude-swap"):
            cache.check_tiers([_info(1)])
        assert "subscribed again" not in caplog.text and "lapsed" not in caplog.text


class TestClaimPlanChecks:
    """Which lapsed rows are due a cheap profile re-read, claimed across
    processes through the cache row itself (``planCheckedAt``)."""

    IDS = {"1": ("a@example.com", "org-1"), "2": ("b@x", "org-1")}

    def _cache(self, tmp_path, clock, status1="canceled", status2="active"):
        cache = billing.BillingCache(tmp_path, clock=clock)
        cache._save({
            "1": {"email": "a@example.com", "organizationUuid": "org-1",
                  "subscriptionStatus": status1, "fetchedAt": clock.t - 3600},
            "2": {"email": "b@x", "organizationUuid": "org-1",
                  "subscriptionStatus": status2, "fetchedAt": clock.t - 3600},
        })
        return cache

    def test_only_lapsed_rows_are_claimed_and_then_held_off(self, tmp_path):
        clock = _Clock()
        cache = self._cache(tmp_path, clock)
        assert cache.claim_plan_checks(self.IDS, min_age=60) == ["1"]
        # Claimed: a second surface asking right after gets nothing.
        assert cache.claim_plan_checks(self.IDS, min_age=60) == []
        clock.t += 61
        assert cache.claim_plan_checks(self.IDS, min_age=60) == ["1"]

    def test_every_row_is_claimed_when_not_only_lapsed(self, tmp_path):
        clock = _Clock()
        cache = self._cache(tmp_path, clock)
        assert cache.claim_plan_checks(self.IDS, min_age=60, lapsed_only=False) == ["1", "2"]
        assert cache.claim_plan_checks(self.IDS, min_age=60, lapsed_only=False) == []

    def test_a_recent_fetch_counts_as_a_check(self, tmp_path):
        clock = _Clock()
        cache = self._cache(tmp_path, clock)
        rows = cache._load()
        rows["1"]["fetchedAt"] = clock.t - 10
        cache._save(rows)
        assert cache.claim_plan_checks(self.IDS, min_age=60) == []

    def test_a_changed_identity_is_not_claimed(self, tmp_path):
        cache = self._cache(tmp_path, _Clock())
        assert cache.claim_plan_checks({"1": ("other@x", "org-1")}, min_age=60) == []

    def test_stagger_spreads_slots_but_stays_bounded(self, tmp_path):
        clock = _Clock()
        cache = self._cache(tmp_path, clock, status2="canceled")
        rows = cache._load()
        for row in rows.values():
            row["fetchedAt"] = clock.t
        cache._save(rows)
        clock.t += billing.LAPSED_RECHECK_S - 1
        assert cache.claim_plan_checks(
            self.IDS, min_age=billing.LAPSED_RECHECK_S, stagger=True
        ) == []
        clock.t += 1 + billing.LAPSED_STAGGER_S
        claimed = cache.claim_plan_checks(
            self.IDS, min_age=billing.LAPSED_RECHECK_S, stagger=True
        )
        assert sorted(claimed) == ["1", "2"]
