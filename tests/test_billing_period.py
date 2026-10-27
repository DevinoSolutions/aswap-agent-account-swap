"""Billing period memory: resubscribe detection, `billing paid`, estimates."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from claude_swap import billing
from claude_swap import weekly_anchor as wa

ID = ("legal@example.com", "org-1")


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


class Clock:
    def __init__(self, s: str):
        self.t = _dt(s).timestamp()

    def __call__(self) -> float:
        return self.t


def _store(tmp_path: Path, at: str = "2026-10-10T12:00:00"):
    clock = Clock(at)
    return wa.WeeklyAnchorStore(tmp_path, clock=clock), clock


class TestLapseAndResume:
    def test_a_confirmed_403_streak_then_success_starts_a_new_period(self, tmp_path):
        store, clock = _store(tmp_path)
        store.note_forbidden(ID, slot=28)
        clock.t += 2 * 3600
        store.note_forbidden(ID, slot=28)
        assert store.get(ID)["lapse"]["confirmed"] is True
        clock.t += 86400
        assert store.note_usable(ID) is True
        row = store.get(ID)
        assert row["lapse"] is None
        assert wa.period_start(row) == ("2026-10-11T14:00:00+00:00", "resubscribed")

    def test_a_single_403_blip_is_not_a_resubscribe(self, tmp_path):
        store, clock = _store(tmp_path)
        store.note_forbidden(ID)
        clock.t += 300
        assert store.note_usable(ID) is False
        row = store.get(ID)
        assert row["lapse"] is None and wa.period_start(row) is None

    def test_profile_status_canceled_then_active_is_a_resubscribe(self, tmp_path):
        store, clock = _store(tmp_path)
        store.note_subscription(ID, "canceled", None, 28)
        clock.t += 3 * 86400
        store.note_subscription(ID, "active", None, 28)
        assert wa.period_start(store.get(ID)) == ("2026-10-13T12:00:00+00:00", "resubscribed")

    def test_removed_while_canceled_then_readded_resumes_on_first_poll(self, tmp_path):
        store, clock = _store(tmp_path)
        store.note_subscription(ID, "canceled", None, 15)
        store.mark_removed(ID, 15)
        clock.t += 9 * 86400
        assert store.note_usable(ID) is True
        row = store.get(ID)
        assert row["subscriptionStatus"] == "active"
        assert wa.period_start(row)[1] == "resubscribed"

    def test_an_active_account_never_writes_on_success(self, tmp_path):
        store, _clock = _store(tmp_path)
        store.note_subscription(ID, "active", None, 28)
        before = store.path.read_text()
        assert store.note_usable(ID) is False
        assert store.path.read_text() == before

    def test_repeated_403s_write_at_most_hourly_once_confirmed(self, tmp_path):
        store, clock = _store(tmp_path)
        store.note_subscription(ID, "canceled", None, 28)
        stamp = store.path.stat().st_mtime_ns
        before = store.path.read_text()
        clock.t += 600
        store.note_forbidden(ID)
        assert store.path.read_text() == before

    def test_manual_paid_and_clear(self, tmp_path):
        store, _clock = _store(tmp_path)
        store.set_period_start(ID, "2026-10-17T16:00:00+00:00")
        assert wa.period_start(store.get(ID)) == ("2026-10-17T16:00:00+00:00", "paid")
        store.set_period_start(ID, None)
        assert wa.period_start(store.get(ID)) is None

    def test_merge_keeps_the_latest_period_start(self, tmp_path):
        mine, _ = _store(tmp_path / "a")
        theirs, _ = _store(tmp_path / "b")
        for s in (mine, theirs):
            s.observe(ID, "2026-10-15T10:00:00Z", 28)
        mine.set_period_start(ID, "2026-10-10T00:00:00+00:00", "resubscribed")
        theirs.set_period_start(ID, "2026-10-17T16:00:00+00:00", "paid")
        mine.merge_rows(theirs.rows())
        assert wa.period_start(mine.get(ID)) == ("2026-10-17T16:00:00+00:00", "paid")


class TestEstimateUsesThePeriodStart:
    NOW = _dt("2026-10-18T12:00:00").timestamp()
    REC = {"subscriptionStatus": "active", "subscriptionCreatedAt": "2026-07-16T03:31:28Z"}

    def test_profile_anchor_alone_is_the_first_purchase(self):
        view = billing.billing_view(self.REC, self.NOW)
        assert view["nextBillingAt"].startswith("2026-11-16")
        assert view["basis"] == {"source": "subscription-created", "at": "2026-07-16T03:31:28Z"}

    def test_a_later_period_start_wins(self):
        view = billing.billing_view(self.REC, self.NOW, period_start=("2026-10-17T16:00:00+00:00", "paid"))
        assert view["nextBillingAt"].startswith("2026-11-17")
        assert view["basis"]["source"] == "paid"

    def test_an_older_period_start_is_ignored(self):
        view = billing.billing_view(self.REC, self.NOW, period_start=("2026-06-01T00:00:00+00:00", "paid"))
        assert view["nextBillingAt"].startswith("2026-11-16")

    def test_exact_data_still_wins(self):
        rec = dict(self.REC, source="exact", currentPeriodEnd="2026-11-20T00:00:00+00:00", cancelAtPeriodEnd=False)
        view = billing.billing_view(rec, self.NOW, period_start=("2026-10-17T16:00:00+00:00", "paid"))
        assert view["nextBillingAt"] == "2026-11-20T00:00:00+00:00"
        assert view["source"] == "exact"


class TestUsageStoreForbiddenHook:
    def test_403_calls_on_forbidden_and_success_calls_on_measure(self, tmp_path):
        from claude_swap.usage_store import FetchRecord, UsageStore

        seen = []
        store = UsageStore(
            tmp_path,
            on_measure=lambda n, i, u: seen.append(("ok", n)),
            on_forbidden=lambda n, i: seen.append(("403", n)),
        )
        ident = ("a@example.com", "org")
        store.record({"1": FetchRecord(usage=None, error="http-403")}, {"1": ident})
        store.record({"2": FetchRecord(usage={"five_hour": {"pct": 1.0}})}, {"2": ident})
        assert seen == [("403", "1"), ("ok", "2")]


class TestResubscribeExpiresBilling:
    def test_usable_after_a_confirmed_lapse_rechecks_billing(self, temp_home):
        from tests.test_autoswitch import EngineHarness

        h = EngineHarness(temp_home)
        h.seed(20, "pro.zero@example.com")
        ident = ("pro.zero@example.com", "")
        sw = h.switcher
        sw._billing._save({"20": {
            "email": ident[0], "organizationUuid": "", "subscriptionStatus": "canceled",
            "nextCheckAt": 9e12,
        }})
        sw._anchors.note_subscription(ident, "canceled", None, 20)
        sw._observe_usage("20", ident, {})
        assert sw._billing._load()["20"]["nextCheckAt"] == 0

    def test_a_normal_poll_leaves_billing_alone(self, temp_home):
        from tests.test_autoswitch import EngineHarness

        h = EngineHarness(temp_home)
        h.seed(20, "pro.zero@example.com")
        ident = ("pro.zero@example.com", "")
        sw = h.switcher
        sw._billing._save({"20": {
            "email": ident[0], "organizationUuid": "", "subscriptionStatus": "active",
            "nextCheckAt": 9e12,
        }})
        sw._observe_usage("20", ident, {})
        assert sw._billing._load()["20"]["nextCheckAt"] == 9e12


class TestPaidCommand:
    def test_paid_records_and_prints_the_renewal(self, temp_home, capsys):
        from unittest.mock import patch

        from claude_swap import billing_cli
        from tests.test_autoswitch import EngineHarness

        h = EngineHarness(temp_home)
        h.seed(28, "legal@example.com")
        with patch.object(billing_cli, "_switcher", return_value=h.switcher):
            billing_cli.billing_command(["paid", "28", "2026-10-17"])
        out = capsys.readouterr().out
        assert "legal@example.com" in out and "renews ~" in out
        [row] = [r for r in h.switcher._anchors.rows().values() if r["email"] == "legal@example.com"]
        assert row["periodSource"] == "paid"
        assert datetime.fromisoformat(row["periodStart"]).astimezone().strftime("%Y-%m-%d %H") == "2026-10-17 12"

    def test_paid_accepts_a_removed_identity_by_email(self, temp_home, capsys):
        from unittest.mock import patch

        from claude_swap import billing_cli
        from tests.test_autoswitch import EngineHarness

        h = EngineHarness(temp_home)
        h.switcher._anchors.observe(("gone@example.com", "org-9"), "2026-10-15T10:00:00Z", 9)
        with patch.object(billing_cli, "_switcher", return_value=h.switcher):
            billing_cli.billing_command(["paid", "gone@example.com", "2026-10-17 09:30"])
        assert h.switcher._anchors.get(("gone@example.com", "org-9"))["periodSource"] == "paid"


class TestTierChangeDetection:
    """A weekly reset off the tier's schedule waits for the profile to confirm the tier."""

    IDENT = ("up@example.com", "")

    def _setup(self, temp_home):
        from tests.test_autoswitch import EngineHarness

        h = EngineHarness(temp_home)
        h.seed(7, self.IDENT[0])
        sw = h.switcher
        sw._billing._save({"7": {
            "email": self.IDENT[0], "organizationUuid": "", "subscriptionStatus": "active",
            "rateLimitTier": "default_claude_max_5x", "fetchedAt": 1.0, "nextCheckAt": 9e12,
        }})
        sw._observe_usage("7", self.IDENT, {"seven_day": {"resets_at": "2026-10-15T10:00:00+00:00"}})
        return sw

    def test_off_schedule_reading_is_held_until_the_tier_is_confirmed(self, temp_home):
        sw = self._setup(temp_home)
        sw._observe_usage("7", self.IDENT, {"seven_day": {"resets_at": "2026-10-17T03:00:00+00:00"}})
        row = sw._anchors.get(self.IDENT)
        assert row["tiers"]["max_5x"]["anchor"] == {"weekday": 3, "hhmm": "10:00"}  # not polluted
        assert row["tiers"]["max_5x"]["moved"] is False
        assert sw._billing._load()["7"]["nextCheckAt"] == 0

    def test_confirmed_upgrade_files_the_reading_under_the_new_tier(self, temp_home):
        sw = self._setup(temp_home)
        sw._observe_usage("7", self.IDENT, {"seven_day": {"resets_at": "2026-10-17T03:00:00+00:00"}})
        rows = sw._billing._load()
        rows["7"].update(rateLimitTier="default_claude_max_20x", fetchedAt=9e12)  # re-read: 20x
        sw._billing._save(rows)
        sw._observe_usage("7", self.IDENT, {"seven_day": {"resets_at": "2026-10-17T03:00:00+00:00"}})
        row = sw._anchors.get(self.IDENT)
        assert row["tier"] == "max_20x"
        assert row["tiers"]["max_20x"]["anchor"] == {"weekday": 5, "hhmm": "03:00"}
        assert row["tiers"]["max_5x"]["moved"] is False

    def test_same_tier_after_confirmation_is_a_real_move(self, temp_home):
        sw = self._setup(temp_home)
        sw._observe_usage("7", self.IDENT, {"seven_day": {"resets_at": "2026-10-17T03:00:00+00:00"}})
        rows = sw._billing._load()
        rows["7"]["fetchedAt"] = 9e12  # re-read: still 5x
        sw._billing._save(rows)
        sw._observe_usage("7", self.IDENT, {"seven_day": {"resets_at": "2026-10-17T03:00:00+00:00"}})
        assert sw._anchors.get(self.IDENT)["tiers"]["max_5x"]["moved"] is True


class TestTierCommand:
    def test_tier_prints_live_tiers_and_changes(self, temp_home, capsys):
        from unittest.mock import patch

        from claude_swap import billing_cli
        from tests.test_autoswitch import EngineHarness

        h = EngineHarness(temp_home)
        h.seed(20, "pro.zero@example.com")
        h.seed(14, "gone@example.com")
        result = {
            "20": {"tier": "default_claude_max_20x", "previousTier": "default_claude_max_5x",
                   "plan": "claude_max", "status": "active", "error": None},
            "14": {"tier": "default_claude_ai", "previousTier": "default_claude_ai",
                   "plan": "claude_free", "status": "canceled", "error": None},
        }
        with patch.object(billing_cli, "_switcher", return_value=h.switcher), \
             patch.object(type(h.switcher._billing), "check_tiers", return_value=result):
            billing_cli.billing_command(["tier"])
        out = capsys.readouterr().out
        line20 = next(l for l in out.splitlines() if "pro.zero" in l)
        assert "20x" in line20 and "was 5x" in line20
        line14 = next(l for l in out.splitlines() if "gone@" in l)
        assert "free" in line14 and "canceled" in line14


class TestPlanCommand:
    def test_plan_advises_upgrade_and_lapse(self, temp_home, capsys):
        from datetime import timedelta
        from unittest.mock import patch

        from claude_swap import billing_cli
        from claude_swap.usage_store import UsageEntry
        from tests.test_autoswitch import EngineHarness

        h = EngineHarness(temp_home)
        h.seed(15, "cozy@example.com")
        now = datetime.now(timezone.utc)
        reset = (now + timedelta(hours=71)).replace(minute=0, second=0, microsecond=0)
        h.switcher._anchors.observe(
            ("cozy@example.com", ""), reset.isoformat(), slot=15, tier="default_claude_max_5x"
        )
        # Renews one hour after a weekly reset: five quotas next month.
        renews = reset + timedelta(days=7, hours=1)
        views = {"15": {"subscriptionStatus": "active", "rateLimitTier": "default_claude_max_5x",
                        "nextBillingAt": renews.isoformat()}}
        usage = {"15": UsageEntry(last_good={
            "five_hour": {"pct": 40.0},
            "seven_day": {"pct": 99.0, "resets_at": reset.isoformat()},
        })}
        with patch.object(billing_cli, "_switcher", return_value=h.switcher), \
             patch.object(h.switcher, "_billing_views", return_value=views), \
             patch.object(h.switcher, "usage_entries_by_account", return_value=usage):
            billing_cli.billing_command(["plan"])
        out = capsys.readouterr().out
        assert "15: cozy@example.com  [active, 5x]" in out
        assert "upgrade to 20x now" in out
        assert "5 quotas next month" in out and "cancel before it renews" in out

    def test_plan_leaves_out_disabled_accounts(self, temp_home, capsys):
        from datetime import timedelta
        from unittest.mock import patch

        from claude_swap import billing_cli
        from tests.test_autoswitch import EngineHarness

        h = EngineHarness(temp_home)
        h.seed(27, "banned@example.com")
        reset = (datetime.now(timezone.utc) + timedelta(hours=30)).replace(minute=0, second=0, microsecond=0)
        h.switcher._anchors.observe(("banned@example.com", ""), reset.isoformat(), slot=27)
        h.switcher.set_account_disabled("27", True)
        capsys.readouterr()
        views = {"27": {"subscriptionStatus": "canceled"}}
        with patch.object(billing_cli, "_switcher", return_value=h.switcher), \
             patch.object(h.switcher, "_billing_views", return_value=views), \
             patch.object(h.switcher, "usage_entries_by_account", return_value={}):
            billing_cli.billing_command(["plan"])
            assert "banned@example.com" not in capsys.readouterr().out
            billing_cli.billing_command(["plan", "--all"])
        out = capsys.readouterr().out
        assert "27: banned@example.com  [disabled" in out
        assert "buy now" not in out
