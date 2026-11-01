"""Auto-add of new Claude Code logins, and the fast re-read of a lapsed plan.

Owner complaints of 2026-10-27: a new ``/login`` needed a hand-run
``aswap add``, and a re-subscribed account kept showing "subscription
canceled" while its usage poll was backed off.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import billing, live_login
from claude_swap import switcher as switcher_mod
from claude_swap.locking import FileLock
from tests.test_autoswitch import EngineHarness, _usage

FAR = 4_102_444_800_000  # 2100-01-01, ms: never expired


def _blob(access="sk-new", refresh="rt-new", expires_at=FAR, scopes=None) -> str:
    data: dict = {"accessToken": access, "refreshToken": refresh}
    if expires_at is not None:
        data["expiresAt"] = expires_at
    if scopes is not None:
        data["scopes"] = scopes
    return json.dumps({"claudeAiOauth": data})


def _login(home: Path, email: str, uuid: str, creds: str, org: str = "") -> None:
    (home / ".claude" / ".credentials.json").write_text(creds)
    account = {"emailAddress": email, "accountUuid": uuid}
    if org:
        account["organizationUuid"] = org
    (home / ".claude.json").write_text(json.dumps({"oauthAccount": account}))


def _owner(email, uuid, org=None):
    return {"uuid": uuid, "email": email, "organizationUuid": org}


@pytest.fixture
def h(temp_home):
    harness = EngineHarness(temp_home)
    harness.seed(1, "a@example.com")
    harness.seed(2, "b@example.com")
    return harness


class TestCredentialKind:
    def test_full_oauth_login(self):
        assert live_login.credential_kind(_blob()) == live_login.KIND_OAUTH

    def test_api_key(self):
        assert live_login.credential_kind("sk-ant-api03-xyz") == live_login.KIND_API_KEY

    def test_setup_token_without_refresh_or_profile_scope(self):
        assert live_login.credential_kind(_blob(refresh=None)) == live_login.KIND_SETUP_TOKEN
        assert (
            live_login.credential_kind(_blob(scopes=["user:inference"]))
            == live_login.KIND_SETUP_TOKEN
        )

    def test_wiped_or_half_written(self):
        assert live_login.credential_kind(_blob(access="", refresh="")) == live_login.KIND_PARTIAL
        assert live_login.credential_kind('{"claudeAiOau') == live_login.KIND_PARTIAL
        assert live_login.credential_kind("") == live_login.KIND_PARTIAL

    def test_observation_carries_no_token(self):
        seen = live_login.observe(
            {"emailAddress": "n@x", "accountUuid": "u"}, _blob(access="sk-secret")
        )
        assert "sk-secret" not in repr(seen) and "rt-new" not in repr(seen)


class TestAdoptNewLogin:
    def test_a_new_login_is_added_to_the_next_free_slot(self, h, temp_home, caplog):
        _login(temp_home, "new@example.com", "uuid-new", _blob())
        sw = h.switcher
        seen = sw.observe_live_login()
        assert seen.kind == live_login.KIND_OAUTH
        with (
            patch.object(sw, "_probe_live_login_owner",
                         return_value=_owner("new@example.com", "uuid-new")),
            caplog.at_level(logging.INFO, logger="claude-swap"),
        ):
            assert sw.adopt_live_login(seen) == ("added", "3")
        data = sw._get_sequence_data()
        assert data["accounts"]["3"]["email"] == "new@example.com"
        assert data["accounts"]["3"]["uuid"] == "uuid-new"
        assert data["sequence"] == [1, 2, 3]
        assert data["activeAccountNumber"] == 3
        assert sw.read_account_credentials("3", "new@example.com") == _blob()
        assert sw.current_account_number() == "3"
        assert "Auto-added account 3: new@example.com" in caplog.text
        assert "sk-new" not in caplog.text and "rt-new" not in caplog.text

    def test_a_credential_of_another_account_is_never_added(self, h, temp_home):
        """Mid-/login: oauthAccount already names the new account while the
        credential file still holds another one's tokens."""
        _login(temp_home, "new@example.com", "uuid-new", _blob())
        sw = h.switcher
        seen = sw.observe_live_login()
        with patch.object(
            sw, "_probe_live_login_owner",
            return_value=_owner("a@example.com", "uuid-1"),
        ) as probe:
            assert sw.adopt_live_login(seen) is None
            assert sw.adopt_live_login(seen) is None  # verdict memoized
        assert probe.call_count == 1
        assert sw.current_account_number() is None
        assert "3" not in sw._get_sequence_data()["accounts"]

    def test_an_unreachable_profile_adds_nothing_and_retries(self, h, temp_home):
        _login(temp_home, "new@example.com", "uuid-new", _blob())
        sw = h.switcher
        seen = sw.observe_live_login()
        with patch.object(sw, "_probe_live_login_owner", return_value=None) as probe:
            assert sw.adopt_live_login(seen) is None
            assert sw.adopt_live_login(seen) is None
        assert probe.call_count == 2
        assert sw.current_account_number() is None

    @pytest.mark.parametrize(
        "creds", ["sk-ant-api03-abc", _blob(refresh=None), _blob(access="", refresh="")]
    )
    def test_api_key_setup_token_and_husk_are_skipped(self, h, temp_home, creds):
        _login(temp_home, "new@example.com", "uuid-new", creds)
        sw = h.switcher
        seen = sw.observe_live_login()
        with patch.object(sw, "_probe_live_login_owner") as probe:
            assert sw.adopt_live_login(seen) is None
        probe.assert_not_called()

    def test_an_expired_token_waits_for_claude_code_to_refresh(self, h, temp_home):
        _login(temp_home, "new@example.com", "uuid-new", _blob(expires_at=1000))
        sw = h.switcher
        with patch.object(sw, "_probe_live_login_owner") as probe:
            assert sw.adopt_live_login(sw.observe_live_login()) is None
        probe.assert_not_called()

    def test_a_login_that_moved_since_the_observation_is_left_alone(self, h, temp_home):
        _login(temp_home, "new@example.com", "uuid-new", _blob())
        sw = h.switcher
        seen = sw.observe_live_login()
        _login(temp_home, "new@example.com", "uuid-new", _blob(refresh="rt-newer"))
        with patch.object(sw, "_probe_live_login_owner") as probe:
            assert sw.adopt_live_login(seen) is None
        probe.assert_not_called()

    def test_a_switch_landing_before_the_lock_wins(self, h, temp_home):
        """The probe runs outside the lock; a switch (or another /login)
        landing then must be seen by the re-check under the lock."""
        _login(temp_home, "new@example.com", "uuid-new", _blob())
        sw = h.switcher
        seen = sw.observe_live_login()

        def switch_meanwhile(_creds):
            h.make_live("a@example.com", 1)
            return _owner("new@example.com", "uuid-new")

        with patch.object(sw, "_probe_live_login_owner", side_effect=switch_meanwhile):
            assert sw.adopt_live_login(seen) is None
        assert "3" not in sw._get_sequence_data()["accounts"]

    def test_a_held_switch_lock_defers(self, h, temp_home, monkeypatch):
        monkeypatch.setattr(switcher_mod, "_AUTO_ADD_LOCK_TIMEOUT_S", 0.0)
        _login(temp_home, "new@example.com", "uuid-new", _blob())
        sw = h.switcher
        seen = sw.observe_live_login()
        held = FileLock(sw.lock_file)
        assert held.acquire()
        try:
            with patch.object(sw, "_probe_live_login_owner",
                              return_value=_owner("new@example.com", "uuid-new")):
                assert sw.adopt_live_login(seen) is None
        finally:
            held.release()
        assert "3" not in sw._get_sequence_data()["accounts"]


class TestAdoptExistingSlot:
    def _relogin(self, h, temp_home, **blob):
        _login(temp_home, "a@example.com", "uuid-1", _blob(**blob))
        return h.switcher.observe_live_login()

    def test_same_lineage_is_a_no_op_without_network(self, h, temp_home):
        sw = h.switcher
        _login(temp_home, "a@example.com", "uuid-1",
               sw.read_account_credentials("1", "a@example.com"))
        with patch.object(sw, "_probe_live_login_owner") as probe:
            assert sw.adopt_live_login(sw.observe_live_login()) is None
        probe.assert_not_called()

    def test_a_needs_login_slot_is_refreshed_and_cleared(self, h, temp_home, caplog):
        sw = h.switcher
        data = sw._get_sequence_data()
        data["accounts"]["1"]["needsLogin"] = {"since": 1, "warnedAt": 1, "storeFp": "sha256:old"}
        sw._write_json(sw.sequence_file, data)
        assert sw.is_account_needs_login("1")
        seen = self._relogin(h, temp_home, expires_at=None)
        with (
            patch.object(sw, "_probe_live_login_owner",
                         return_value=_owner("a@example.com", "uuid-1")),
            caplog.at_level(logging.INFO, logger="claude-swap"),
        ):
            assert sw.adopt_live_login(seen) == ("refreshed", "1")
        assert not sw.is_account_needs_login("1")
        assert sw.read_account_credentials("1", "a@example.com") == _blob(expires_at=None)
        assert "Account 1 logged in again" in caplog.text

    def test_a_wiped_stored_credential_is_refreshed(self, h, temp_home):
        sw = h.switcher
        sw._write_account_credentials("1", "a@example.com", _blob(access="", refresh=""))
        seen = self._relogin(h, temp_home, expires_at=None)
        with patch.object(sw, "_probe_live_login_owner",
                          return_value=_owner("a@example.com", "uuid-1")):
            assert sw.adopt_live_login(seen) == ("refreshed", "1")
        assert sw.read_account_credentials("1", "a@example.com") == _blob(expires_at=None)

    def test_an_older_stored_credential_is_refreshed(self, h, temp_home):
        sw = h.switcher
        sw._write_account_credentials("1", "a@example.com", _blob(refresh="rt-1", expires_at=1000))
        seen = self._relogin(h, temp_home)
        with patch.object(sw, "_probe_live_login_owner",
                          return_value=_owner("a@example.com", "uuid-1")):
            assert sw.adopt_live_login(seen) == ("refreshed", "1")
        assert sw.read_account_credentials("1", "a@example.com") == _blob()

    def test_a_newer_stored_credential_is_kept(self, h, temp_home):
        """aswap rotated the backup itself: the live copy is the stale one."""
        sw = h.switcher
        stored = _blob(refresh="rt-1", expires_at=FAR + 1)
        sw._write_account_credentials("1", "a@example.com", stored)
        seen = self._relogin(h, temp_home)
        with patch.object(sw, "_probe_live_login_owner") as probe:
            assert sw.adopt_live_login(seen) is None
        probe.assert_not_called()
        assert sw.read_account_credentials("1", "a@example.com") == stored

    def test_a_foreign_credential_never_reaches_the_slot(self, h, temp_home):
        sw = h.switcher
        sw._write_account_credentials("1", "a@example.com", _blob(access="", refresh=""))
        seen = self._relogin(h, temp_home)
        with patch.object(sw, "_probe_live_login_owner",
                          return_value=_owner("b@example.com", "uuid-2")):
            assert sw.adopt_live_login(seen) is None
        assert sw.read_account_credentials("1", "a@example.com") == _blob(access="", refresh="")


class TestEngineAutoAdd:
    def _new_login(self, temp_home, refresh="rt-new"):
        _login(temp_home, "new@example.com", "uuid-new", _blob(refresh=refresh))

    def _owner_patch(self, h):
        return patch.object(h.switcher, "_probe_live_login_owner",
                            return_value=_owner("new@example.com", "uuid-new"))

    def test_added_only_after_two_stable_polls(self, h, temp_home):
        self._new_login(temp_home)
        usage = {"1": _usage(10), "2": _usage(10)}
        with self._owner_patch(h):
            h.tick_with_usage(usage)
            assert h.switcher.current_account_number() is None
            h.tick_with_usage({**usage, "3": _usage(10)})
        assert h.switcher.current_account_number() == "3"

    def test_a_login_still_moving_is_not_added(self, h, temp_home):
        usage = {"1": _usage(10), "2": _usage(10)}
        with self._owner_patch(h):
            self._new_login(temp_home, refresh="rt-a")
            h.tick_with_usage(usage)
            self._new_login(temp_home, refresh="rt-b")
            h.tick_with_usage(usage)
        assert h.switcher.current_account_number() is None

    def test_setting_off_keeps_todays_behaviour_and_hints_once(self, temp_home, caplog):
        h = EngineHarness(temp_home, auto_add_accounts=False)
        h.seed(1, "a@example.com")
        self._new_login(temp_home)
        with (
            self._owner_patch(h) as probe,
            caplog.at_level(logging.INFO, logger="claude-swap"),
        ):
            for _ in range(3):
                h.tick_with_usage({"1": _usage(10)})
        probe.assert_not_called()
        assert h.switcher.current_account_number() is None
        assert caplog.text.count("autoswitch.autoAddAccounts is off") == 1

    def test_dry_run_never_adds(self, h, temp_home):
        h.engine = h._make_engine(dry_run=True)
        self._new_login(temp_home)
        with self._owner_patch(h) as probe:
            h.tick_with_usage({"1": _usage(10)})
            h.tick_with_usage({"1": _usage(10)})
        probe.assert_not_called()
        assert h.switcher.current_account_number() is None


class TestNeedsLoginHint:
    def test_hint_follows_the_setting(self, h):
        from claude_swap.settings import set_setting

        assert "picks the login up automatically" in h.switcher.needs_login_hint()
        set_setting(h.switcher.backup_dir, "autoswitch.autoAddAccounts", "false")
        assert h.switcher.needs_login_hint().endswith("then run: aswap add")

    def test_relogin_note_mentions_auto(self):
        from claude_swap.json_output import USAGE_RELOGIN_REQUIRED

        note = switcher_mod.SENTINEL_NOTES[USAGE_RELOGIN_REQUIRED]
        assert "aswap auto picks it up" in note

    def test_tui_card_carries_the_hint(self):
        from tests.test_tui import make_account

        from claude_swap.tui import widgets

        card = widgets.account_card_text(make_account(2, needs_login=True), 100)
        assert "auto picks it up" in card.plain


def _lapsed_row(email, status="canceled", tier="default_claude_max_5x", **extra):
    return {"email": email, "organizationUuid": "", "subscriptionStatus": status,
            "rateLimitTier": tier, "nextCheckAt": 9e12, **extra}


class TestLapsedPlanRecheck:
    def test_background_recheck_reads_only_lapsed_slots_every_few_minutes(self, h):
        sw = h.switcher
        sw._billing.clock = h.clock
        sw._billing._save({
            "1": _lapsed_row("a@example.com", fetchedAt=h.clock.now - 3600),
            "2": _lapsed_row("b@example.com", status="active", fetchedAt=h.clock.now - 3600),
        })
        with patch.object(type(sw._billing), "check_tiers", return_value={}) as check:
            sw.recheck_lapsed_plans()
            sw.recheck_lapsed_plans()
            assert check.call_count == 1
            assert [str(i[0]) for i in check.call_args.args[0]] == ["1"]
            h.clock.advance(billing.LAPSED_RECHECK_S + billing.LAPSED_STAGGER_S + 1)
            sw.recheck_lapsed_plans()
        assert check.call_count == 2

    def test_hourly_pass_reads_every_plan_not_only_lapsed_ones(self, h):
        sw = h.switcher
        sw._billing.clock = h.clock
        sw._billing._save({
            "1": _lapsed_row("a@example.com", status="active", fetchedAt=h.clock.now - 7200),
            "2": _lapsed_row("b@example.com", status="active", fetchedAt=h.clock.now - 7200),
        })
        with patch.object(type(sw._billing), "check_tiers", return_value={}) as check:
            sw.recheck_plans()
            sw.recheck_plans()
            assert check.call_count == 1
            assert sorted(str(i[0]) for i in check.call_args.args[0]) == ["1", "2"]
            h.clock.advance(billing.PLAN_REFRESH_S + billing.LAPSED_STAGGER_S + 1)
            sw.recheck_plans()
        assert check.call_count == 2

    def test_auto_rereads_session_billing_without_the_tui(self, h):
        # 2026-11-01: a confirmed cancellation steers consume-first, so the
        # loop itself re-reads it; only slots with a claude.ai session.
        sw = h.switcher
        info = {str(i[0]): i for i in sw._build_accounts_info()}
        email, org = info["2"][1], info["2"][3] or ""
        sw._sessions.put((email, org), "sk-test")
        with patch.object(type(sw._billing), "check_tiers", return_value={}),                 patch.object(type(sw._billing), "refresh") as refresh:
            sw.recheck_plans()
            sw.recheck_plans()  # inside SESSION_BILLING_SCAN_S: not looked at again
        assert refresh.call_count == 1
        assert [str(i[0]) for i in refresh.call_args.args[0]] == ["2"]

    def test_usage_falling_inside_a_window_rechecks_the_plan(self, h):
        # 2026-10-29: upgrades 5x -> 20x went unseen for hours. The usage
        # endpoint shows them at once: the same window reads a quarter.
        sw = h.switcher
        ident = ("b@example.com", "")
        reset = "2026-11-02T08:00:00Z"
        with patch.object(sw, "recheck_plan_now") as recheck:
            sw._observe_usage("2", ident, {"seven_day": {"pct": 80.0, "resets_at": reset}})
            sw._observe_usage("2", ident, {"seven_day": {"pct": 78.0, "resets_at": reset}})
            assert recheck.call_count == 0  # rounding, not a plan change
            sw._observe_usage("2", ident, {"seven_day": {"pct": 20.0, "resets_at": reset}})
            assert [c.args[0] for c in recheck.call_args_list] == ["2"]
            # A new window starting low is a reset, not an upgrade.
            sw._observe_usage("2", ident, {"seven_day": {"pct": 1.0, "resets_at": "2026-11-09T08:00:00Z"}})
        assert recheck.call_count == 1

    def test_becoming_active_rechecks_at_once(self, h):
        sw = h.switcher
        sw._billing.clock = h.clock
        sw._billing._save({"2": _lapsed_row("b@example.com", fetchedAt=h.clock.now - 120)})
        with patch.object(type(sw._billing), "check_tiers", return_value={}) as check:
            sw.recheck_plan_if_lapsed("2")
        assert [str(i[0]) for i in check.call_args.args[0]] == ["2"]

    def test_a_switch_rechecks_its_target(self, h, temp_home):
        h.make_live("a@example.com", 1)
        sw = h.switcher
        sw._billing._save({"2": _lapsed_row("b@example.com")})
        with patch.object(type(sw._billing), "check_tiers", return_value={}) as check:
            sw.switch_to("2", json_output=True)
        assert [str(i[0]) for i in check.call_args.args[0]] == ["2"]

    def test_the_engine_rechecks_in_the_background_and_on_an_active_change(self, h):
        h.make_live("a@example.com", 1)
        with (
            patch.object(h.switcher, "recheck_lapsed_plans") as background,
            patch.object(h.switcher, "recheck_plan_if_lapsed") as on_active,
        ):
            h.tick_with_usage({"1": _usage(10), "2": _usage(10)})
            h.tick_with_usage({"1": _usage(10), "2": _usage(10)})
            h.make_live("b@example.com", 2)
            h.tick_with_usage({"1": _usage(10), "2": _usage(10)})
        assert background.call_count == 3
        assert [c.args[0] for c in on_active.call_args_list] == ["1", "2"]

    def test_a_relogin_rechecks_the_plan(self, h, temp_home):
        sw = h.switcher
        sw._write_account_credentials("1", "a@example.com", _blob(access="", refresh=""))
        _login(temp_home, "a@example.com", "uuid-1", _blob())
        with (
            patch.object(sw, "_probe_live_login_owner",
                         return_value=_owner("a@example.com", "uuid-1")),
            patch.object(sw, "recheck_plan_if_lapsed") as recheck,
        ):
            sw.adopt_live_login(sw.observe_live_login())
        recheck.assert_called_once_with("1")
