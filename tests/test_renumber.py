"""Tests for `aswap renumber`, lowest-free numbering, and slot-number reuse.

Covers the pure planner (renumber.py), the switcher's locked execution
(renumber_accounts) including crash/resume, gap filling for new accounts,
the per-slot stores a reused or moved number must not leak through, and how
a running auto-switch engine copes with numbers changing under it.
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path

import pytest

from claude_swap import oauth, renumber
from claude_swap.exceptions import ConfigError, RosterChangedError
from claude_swap.settings import atomic_write_json
from claude_swap.switcher import ClaudeAccountSwitcher
from tests import statefiles


def _accounts(layout: dict[int, str]) -> dict:
    return {
        str(num): {"email": email, "organizationUuid": "", "uuid": f"u-{email}"}
        for num, email in layout.items()
    }


def _layout(accounts: dict) -> dict[int, str]:
    return {int(n): r["email"] for n, r in accounts.items()}


# -- the planner -----------------------------------------------------------


class TestPlanner:
    def test_list_order_keeps_relative_order_and_only_relocates(self):
        accounts = _accounts({3: "c@x", 13: "a@x", 19: "b@x"})
        steps = renumber.simulate(accounts)
        assert all(not s.swapped for s in steps)
        final = accounts
        for s in steps:
            final = renumber.apply_step(final, s)
        assert _layout(final) == {1: "c@x", 2: "a@x", 3: "b@x"}

    def test_list_order_plan(self):
        accounts = _accounts({3: "c@x", 13: "a@x", 19: "b@x"})
        assert renumber.plan(accounts) == {"3": "1", "13": "2", "19": "3"}

    def test_already_packed_is_a_no_op(self):
        accounts = _accounts({1: "b@x", 2: "a@x"})
        assert renumber.is_packed(accounts)
        assert renumber.next_step(accounts) is None
        assert renumber.simulate(accounts) == []
        # By email it is not packed: b sorts after a.
        assert not renumber.is_packed(accounts, by_email=True)

    @pytest.mark.parametrize(
        "layout",
        [
            {1: "c@x", 2: "a@x", 3: "b@x"},  # one 3-cycle
            {1: "b@x", 2: "a@x", 3: "d@x", 4: "c@x"},  # two 2-cycles
            {2: "b@x", 5: "a@x", 9: "c@x"},  # gaps and a swap
            {4: "a@x", 3: "b@x", 2: "c@x", 1: "d@x"},  # reversed
        ],
    )
    def test_by_email_handles_cycles(self, layout):
        """At most one step per account (N-1 when the numbers are already
        1..N): each step puts one more account in its final place."""
        accounts = _accounts(layout)
        steps = renumber.simulate(accounts, by_email=True)
        packed_numbers = sorted(layout) == list(range(1, len(layout) + 1))
        assert len(steps) <= len(layout) - (1 if packed_numbers else 0)
        final = accounts
        for s in steps:
            final = renumber.apply_step(final, s)
        emails = sorted(layout.values())
        assert _layout(final) == {i: e for i, e in enumerate(emails, 1)}

    def test_three_cycle_needs_two_swaps_and_no_free_slot(self):
        accounts = _accounts({1: "c@x", 2: "a@x", 3: "b@x"})
        steps = renumber.simulate(accounts, by_email=True)
        assert [(s.source, s.target, s.swapped) for s in steps] == [
            ("2", "1", True),
            ("3", "2", True),
        ]

    def test_by_email_is_case_insensitive_then_org(self):
        accounts = {
            "5": {"email": "B@x", "organizationUuid": ""},
            "6": {"email": "a@x", "organizationUuid": "org-2"},
            "7": {"email": "a@x", "organizationUuid": "org-1"},
        }
        assert renumber.plan(accounts, by_email=True) == {"7": "1", "6": "2", "5": "3"}

    def test_identical_identities_tie_break_on_current_number(self):
        accounts = {
            "4": {"email": "a@x", "organizationUuid": ""},
            "2": {"email": "a@x", "organizationUuid": ""},
        }
        assert renumber.plan(accounts, by_email=True) == {"2": "1", "4": "2"}
        assert len(renumber.simulate(accounts, by_email=True)) == 2

    @pytest.mark.parametrize("by_email", [False, True])
    def test_plan_from_any_intermediate_roster_reaches_the_same_layout(self, by_email):
        """Resumability: stop after k steps, recompute, finish -- the final
        layout is the one the uninterrupted run reaches."""
        layout = {2: "d@x", 4: "b@x", 7: "a@x", 8: "e@x", 11: "c@x"}
        accounts = _accounts(layout)
        steps = renumber.simulate(accounts, by_email=by_email)
        full = accounts
        for s in steps:
            full = renumber.apply_step(full, s)
        for k in range(len(steps) + 1):
            partial = accounts
            for s in steps[:k]:
                partial = renumber.apply_step(partial, s)
            resumed = partial
            for s in renumber.simulate(partial, by_email=by_email):
                resumed = renumber.apply_step(resumed, s)
            assert _layout(resumed) == _layout(full)

    def test_every_permutation_of_four_converges_by_email(self):
        emails = ["a@x", "b@x", "c@x", "d@x"]
        for perm in itertools.permutations(emails):
            accounts = _accounts(dict(enumerate(perm, 1)))
            steps = renumber.simulate(accounts, by_email=True)
            assert len(steps) <= 3
            final = accounts
            for s in steps:
                final = renumber.apply_step(final, s)
            assert _layout(final) == dict(enumerate(emails, 1))

    def test_lowest_free_number(self):
        assert renumber.lowest_free_number({}) == 1
        assert renumber.lowest_free_number(_accounts({1: "a", 2: "b"})) == 3
        assert renumber.lowest_free_number(_accounts({1: "a", 3: "b", 4: "c"})) == 2
        assert renumber.lowest_free_number(_accounts({2: "a"})) == 1


class TestEngineStateBookkeeping:
    def test_remap_follows_moves_and_keeps_types(self):
        state = {
            "lastSeenActive": "9",
            "lastSwitchTo": "7",
            "lastSwitchFrom": 9,
            "manualHold": {"account": "9", "since": 1.0},
            "quarantine": {"7": {"email": "b@x"}, "4": {"email": "z@x"}},
        }
        assert renumber.remap_engine_state(state, {"7": "2", "9": "3"})
        assert state["lastSeenActive"] == "3"
        assert state["lastSwitchTo"] == "2"
        assert state["lastSwitchFrom"] == 3
        assert state["manualHold"]["account"] == "3"
        assert state["quarantine"] == {"2": {"email": "b@x"}, "4": {"email": "z@x"}}

    def test_remap_handles_a_swap(self):
        state = {"quarantine": {"1": {"email": "a"}, "2": {"email": "b"}}}
        renumber.remap_engine_state(state, {"1": "2", "2": "1"})
        assert state["quarantine"] == {"2": {"email": "a"}, "1": {"email": "b"}}

    def test_forget_drops_every_reference_to_a_freed_slot(self):
        state = {
            "lastSeenActive": "2",
            "lastSwitchTo": "2",
            "lastSwitchFrom": 2,
            "manualHold": {"account": "2"},
            "quarantine": {"2": {"email": "b@x"}, "3": {"email": "c@x"}},
        }
        assert renumber.forget_engine_slot(state, "2", "b@x")
        assert state == {"quarantine": {"3": {"email": "c@x"}}}

    def test_forget_keeps_another_accounts_quarantine(self):
        state = {"quarantine": {"2": {"email": "new@x"}}}
        assert not renumber.forget_engine_slot(state, "2", "old@x")
        assert state == {"quarantine": {"2": {"email": "new@x"}}}

    def test_rekey_rows_carries_matching_rows_and_drops_strays(self):
        rows = {
            "7": {"email": "b@x", "organizationUuid": "", "v": 1},
            "2": {"email": "gone@x", "organizationUuid": "", "v": 2},
            "9": {"email": "someone-else@x", "organizationUuid": "", "v": 3},
            "5": {"email": "e@x", "organizationUuid": "", "v": 4},
        }
        out, changed = renumber.rekey_rows(
            rows, {"7": ("2", ("b@x", "")), "9": ("3", ("c@x", ""))}
        )
        assert changed
        assert out == {
            "2": {"email": "b@x", "organizationUuid": "", "v": 1},
            "5": {"email": "e@x", "organizationUuid": "", "v": 4},
        }


# -- executing a renumber ------------------------------------------------------


def _creds(tag: str) -> str:
    return json.dumps({
        "claudeAiOauth": {
            "accessToken": f"sk-{tag}",
            "refreshToken": f"rt-{tag}",
            "expiresAt": 9999999999000,
        }
    })


def _seed(layout: dict[int, str], active: int | None = None) -> ClaudeAccountSwitcher:
    s = ClaudeAccountSwitcher()
    s._setup_directories()
    data = {
        "activeAccountNumber": active,
        "lastUpdated": "2024-01-01T00:00:00Z",
        "sequence": sorted(layout),
        "accounts": {},
    }
    for num, email in layout.items():
        data["accounts"][str(num)] = {
            "email": email,
            "uuid": f"uuid-{email}",
            "organizationUuid": "",
            "organizationName": "",
            "added": "2024-01-01T00:00:00Z",
        }
        s._write_account_credentials(str(num), email, _creds(email))
        s._write_account_config(
            str(num), email, json.dumps({"oauthAccount": {"emailAddress": email}})
        )
    s._write_json(s.sequence_file, data)
    return s


def _assert_consistent(s: ClaudeAccountSwitcher) -> None:
    """Every record has its own credentials under its number."""
    data = s._get_sequence_data()
    assert data["sequence"] == sorted(int(n) for n in data["accounts"])
    for num, record in data["accounts"].items():
        email = record["email"]
        assert s._read_account_credentials(num, email) == _creds(email), num


class TestRenumberAccounts:
    def test_list_order_packs_and_everything_follows(self, temp_home: Path):
        s = _seed({3: "c@x", 13: "a@x", 19: "b@x"}, active=13)
        data = s._get_sequence_data()
        data["accounts"]["19"]["alias"] = "bee"
        s._write_json(s.sequence_file, data)

        result = s.renumber_accounts()

        assert result["changed"] and result["order"] == "list"
        assert result["snapshot"]
        data = s._get_sequence_data()
        assert _layout(data["accounts"]) == {1: "c@x", 2: "a@x", 3: "b@x"}
        assert data["accounts"]["3"]["alias"] == "bee"
        assert data["activeAccountNumber"] == 2
        _assert_consistent(s)

    def test_by_email_with_a_cycle(self, temp_home: Path):
        s = _seed({1: "c@x", 2: "a@x", 3: "b@x"}, active=1)
        result = s.renumber_accounts(by_email=True)
        assert [m["swapped"] for m in result["moves"]] == [True, True]
        data = s._get_sequence_data()
        assert _layout(data["accounts"]) == {1: "a@x", 2: "b@x", 3: "c@x"}
        assert data["activeAccountNumber"] == 3  # c@x stays active
        _assert_consistent(s)

    def test_two_layouts_by_email_give_identical_numbers(self, temp_home: Path, tmp_path):
        s = _seed({4: "d@x", 9: "a@x", 2: "c@x", 7: "b@x"})
        s.renumber_accounts(by_email=True, snapshot=False)
        first = _layout(s._get_sequence_data()["accounts"])
        # Same accounts, different starting numbers.
        s._write_json(s.sequence_file, {"sequence": [], "accounts": {}})
        s2 = _seed({1: "b@x", 5: "d@x", 6: "c@x", 8: "a@x"})
        s2.renumber_accounts(by_email=True, snapshot=False)
        assert _layout(s2._get_sequence_data()["accounts"]) == first
        assert first == {1: "a@x", 2: "b@x", 3: "c@x", 4: "d@x"}

    def test_no_op_changes_nothing_and_takes_no_snapshot(self, temp_home: Path):
        s = _seed({1: "a@x", 2: "b@x"})
        before = s._get_sequence_data()
        result = s.renumber_accounts()
        assert not result["changed"] and result["moves"] == []
        assert result["snapshot"] is None
        assert s._get_sequence_data() == before

    def test_preview_changes_nothing(self, temp_home: Path):
        s = _seed({5: "a@x", 9: "b@x"})
        before = s._get_sequence_data()
        preview = s.renumber_preview()
        assert preview["changed"]
        assert [(r["from"], r["to"]) for r in preview["accounts"]] == [(5, 1), (9, 2)]
        assert preview["moves"] == [
            {"from": 5, "to": 1, "swapped": False},
            {"from": 9, "to": 2, "swapped": False},
        ]
        assert s._get_sequence_data() == before

    def test_confirmed_preview_must_still_match(self, temp_home: Path):
        s = _seed({5: "a@x", 9: "b@x"})
        preview = s.renumber_preview()
        s.move_account("9", "7")  # another process moved an account
        with pytest.raises(RosterChangedError):
            s.renumber_accounts(expected=preview["accounts"])
        assert _layout(s._get_sequence_data()["accounts"]) == {5: "a@x", 7: "b@x"}

    def test_engine_state_follows(self, temp_home: Path):
        s = _seed({7: "b@x", 9: "c@x"}, active=9)
        atomic_write_json(s.backup_dir / "autoswitch_state.json", {
            "lastSeenActive": "9",
            "lastSwitchTo": "9",
            "lastSwitchFrom": 7,
            "manualHold": {"account": "9", "since": 1.0},
            "quarantine": {"7": {"email": "b@x", "reason": "invalid_grant"}},
        })
        s.renumber_accounts()
        state = statefiles.read_json(s.backup_dir / "autoswitch_state.json")
        assert state["lastSeenActive"] == "2"
        assert state["lastSwitchTo"] == "2"
        assert state["lastSwitchFrom"] == 1
        assert state["manualHold"]["account"] == "2"
        assert state["quarantine"] == {"1": {"email": "b@x", "reason": "invalid_grant"}}

    def test_usage_cache_follows_the_account(self, temp_home: Path):
        s = _seed({7: "b@x", 9: "c@x"})
        s._usage_store._write_rows({
            "7": {"email": "b@x", "organizationUuid": "", "fetchedAt": 1.0},
            "1": {"email": "removed@x", "organizationUuid": "", "fetchedAt": 2.0},
        })
        s.renumber_accounts()
        rows = s._usage_store._read_rows()
        assert rows == {"1": {"email": "b@x", "organizationUuid": "", "fetchedAt": 1.0}}


class TestCrashAndResume:
    """Each step commits on its own: a failure mid-way leaves a consistent
    roster, and running renumber again finishes the job."""

    @pytest.mark.parametrize(
        "layout, by_email, helper",
        [
            ({3: "c@x", 7: "a@x", 9: "b@x"}, False, "_relocate_locked"),
            ({1: "c@x", 2: "a@x", 3: "b@x"}, True, "_swap_accounts_locked"),
        ],
    )
    def test_failure_on_second_step_then_resume(
        self, temp_home: Path, monkeypatch, layout, by_email, helper
    ):
        s = _seed(layout)
        real = getattr(ClaudeAccountSwitcher, helper)
        calls = {"n": 0}

        def flaky(self, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("disk full (injected)")
            return real(self, *args, **kwargs)

        monkeypatch.setattr(ClaudeAccountSwitcher, helper, flaky)
        with pytest.raises(ConfigError, match="stopped after 1 of [23] move"):
            s.renumber_accounts(by_email=by_email)
        _assert_consistent(s)
        # Exactly one step landed.
        assert len(s._get_sequence_data()["accounts"]) == 3

        monkeypatch.setattr(ClaudeAccountSwitcher, helper, real)
        s.renumber_accounts(by_email=by_email)
        _assert_consistent(s)
        expected = (
            {1: "a@x", 2: "b@x", 3: "c@x"}
            if by_email
            else {i: layout[k] for i, k in enumerate(sorted(layout), 1)}
        )
        assert _layout(s._get_sequence_data()["accounts"]) == expected

    def test_stale_sequence_entry_is_normalized_first(self, temp_home: Path):
        s = _seed({4: "a@x", 6: "b@x"})
        data = s._get_sequence_data()
        data["sequence"] = [1, 4, 6]  # crash leftover: 1 has no record
        s._write_json(s.sequence_file, data)
        s.renumber_accounts()
        data = s._get_sequence_data()
        assert data["sequence"] == [1, 2]
        _assert_consistent(s)


# -- gap filling and number reuse ----------------------------------------------


class TestGapFilling:
    def test_new_accounts_take_the_lowest_free_number(self, temp_home: Path):
        s = _seed({1: "a@x", 3: "c@x", 4: "d@x"})
        assert s._get_next_account_number() == 2

    def test_removed_number_is_reused_without_inheriting_engine_state(
        self, temp_home: Path
    ):
        s = _seed({1: "a@x", 2: "b@x", 3: "c@x"}, active=1)
        atomic_write_json(s.backup_dir / "autoswitch_state.json", {
            "lastSwitchTo": "2",
            "lastSwitchFrom": 2,
            "manualHold": {"account": "2", "since": 1.0},
            "quarantine": {"2": {"email": "b@x", "reason": "invalid_grant"}},
        })
        s.remove_account("2", assume_yes=True)
        state = statefiles.read_json(s.backup_dir / "autoswitch_state.json")
        assert "quarantine" not in state or "2" not in state["quarantine"]
        assert "manualHold" not in state
        assert "lastSwitchTo" not in state and "lastSwitchFrom" not in state
        assert s._get_next_account_number() == 2

    def test_reused_number_does_not_serve_old_caches(self, temp_home: Path):
        """The usage/gate/billing caches are slot-keyed but identity-guarded:
        a row left by the removed account is invisible to the new one."""
        s = _seed({1: "a@x", 2: "b@x"})
        s._usage_store._write_rows({
            "2": {"email": "b@x", "organizationUuid": "", "fetchedAt": 1.0,
                  "lastGood": {"five_hour": {"pct": 99}}},
        })
        s.remove_account("2", assume_yes=True)
        s2 = _seed({1: "a@x", 2: "new@x"})
        entry = s2._usage_store.entries({"2": ("new@x", "")})["2"]
        assert entry.last_good is None and entry.fetched_at is None
        assert s2._gate_store.records({"2": ("new@x", "")}) == {}
        assert s2._billing.records({"2": ("new@x", "")}) == {"2": None}

    def test_token_placeholder_never_collides_after_renumber(self, temp_home: Path):
        """`setup-token-N` used to be unique because N only grew. After a
        renumber the account called setup-token-3 can sit at slot 1 and slot
        3 is free again: the next token add must not claim that identity
        (it would delete/overwrite the existing account)."""
        s = _seed({1: "setup-token-3@token.local", 2: "b@x"})
        s.add_account_from_token("fresh-token")
        data = s._get_sequence_data()
        assert data["accounts"]["1"]["email"] == "setup-token-3@token.local"
        assert s._read_account_credentials("1", "setup-token-3@token.local") == _creds(
            "setup-token-3@token.local"
        )
        assert data["accounts"]["3"]["email"] == "setup-token-4@token.local"

    def test_token_placeholder_skips_identities_with_history(self, temp_home: Path):
        s = _seed({1: "a@x"})
        s._anchors._save({"setup-token-2@token.local|": {"tiers": {}}})
        s.add_account_from_token("fresh-token")
        data = s._get_sequence_data()
        assert data["accounts"]["2"]["email"] == "setup-token-3@token.local"


# -- switching while numbers change ---------------------------------------------


class TestSwitchExpect:
    def test_switch_refuses_when_the_number_holds_someone_else(self, temp_home: Path):
        s = _seed({1: "a@x", 2: "b@x"}, active=1)
        with pytest.raises(RosterChangedError):
            s.switch_to("2", json_output=True, expect=("c@x", ""))
        assert s._get_sequence_data()["activeAccountNumber"] == 1

    def test_roster_identities(self, temp_home: Path):
        s = _seed({1: "a@x", 4: "b@x"})
        assert s.roster_identities() == {"1": ("a@x", ""), "4": ("b@x", "")}


class TestStashAdoptionAcrossRenumber:
    _OLD = _creds("old")
    _NEW = _creds("new")

    def test_stash_written_under_the_old_number_is_adopted(self, temp_home: Path):
        """A gate pass that consumed a grant and failed to persist stashes the
        successor with ``configSlot`` = the number at the time. After a
        renumber the account lives elsewhere; the consumed-generation
        fingerprint still names it, so the stash is adopted, not stranded."""
        s = _seed({1: "a@x"})
        s._write_account_credentials("1", "a@x", self._OLD)
        s._store._write_unclaimed_credential(
            self._NEW,
            {"reason": "consume-gate-persist-lock-failed",
             "configSlot": "7",
             "consumedFp": oauth.credential_fingerprint(self._OLD),
             "fingerprint": oauth.credential_fingerprint(self._NEW)},
        )
        adopted = s._adopt_stashed_successor("1", "a@x", self._OLD)
        assert adopted == self._NEW
        assert s._read_account_credentials("1", "a@x") == self._NEW

    def test_other_numbers_non_matching_stash_is_left_alone(self, temp_home: Path):
        s = _seed({1: "a@x"})
        s._write_account_credentials("1", "a@x", self._NEW)
        entry_id = s._store._write_unclaimed_credential(
            self._OLD,
            {"reason": "consume-gate-cas-conflict",
             "configSlot": "7",
             "consumedFp": "something-else",
             "fingerprint": oauth.credential_fingerprint(self._OLD)},
        )
        assert s._adopt_stashed_successor("1", "a@x", self._NEW) is None
        assert entry_id in s._store._read_stash_manifest()


# -- a running auto-switch engine -------------------------------------------------


class TestEngineAcrossRenumber:
    """Numbers change under a running ``aswap auto`` (renumber/move/swap from
    another process). The engine must never switch to whoever sits at a
    stale number, and must keep its own per-number memory on the right
    accounts."""

    def _harness(self, temp_home: Path):
        from tests.test_autoswitch import EngineHarness

        h = EngineHarness(temp_home, threshold=50.0)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.seed(3, "c@example.com")
        h.make_live("a@example.com", 1)
        return h

    def test_switch_decided_on_old_numbers_is_refused(self, temp_home: Path):
        from unittest.mock import patch

        from claude_swap.autoswitch import NoSwitchEvent, TickOutcome
        from tests.test_autoswitch import _entry_for, _usage

        h = self._harness(temp_home)
        # Usage keyed by the numbers at the start of the tick: account 2 (b)
        # has room. While the engine collects it, another process swaps 2
        # and 3 -- slot 2 is now c.
        entries = {
            n: _entry_for(_usage(v), h.clock.now)
            for n, v in {"1": 95, "2": 10, "3": 97}.items()
        }

        def collect(*_a, **_kw):
            if h.switcher.roster_identities()["2"][0] == "b@example.com":
                h.switcher.swap_accounts("2", "3")
            return entries

        with patch.object(h.switcher, "usage_entries_by_account", side_effect=collect):
            outcome = h.engine.tick()

        assert outcome is TickOutcome.NO_ACTION
        refused = [
            e for e in h.events
            if isinstance(e, NoSwitchEvent) and e.reason == "roster-changed"
        ]
        assert refused, h.kinds()
        assert h.switcher.current_account_number() == "1"

        # The next tick sees the new numbers (b is 3 now) and switches to b.
        h.events.clear()
        outcome = h.tick_with_usage({"1": _usage(95), "2": _usage(97), "3": _usage(10)})
        assert outcome is TickOutcome.SWITCHED
        current = h.switcher.current_account_number()
        assert current == "3"
        assert h.switcher.account_email(current) == "b@example.com"

    def test_engine_memory_follows_a_renumber(self, temp_home: Path):
        from tests.test_autoswitch import _usage

        h = self._harness(temp_home)
        h.tick_with_usage({"1": _usage(10), "2": _usage(10), "3": _usage(10)})
        h.engine._last_seen = "3"
        h.engine._plan_seen_active = "3"
        h.engine._gate_logged = {"3": "open", "1": "closed"}
        h.engine._burn._samples = []
        h.engine._burn.record(0.0, "3", 10.0, 1.0)
        h.engine._burn.record(1.0, "1", 10.0, 1.0)
        # Another process: move c (3) to 7, then b (2) to 3.
        h.switcher.move_account("3", "7")
        h.switcher.move_account("2", "3")
        h.engine._follow_roster()
        assert h.engine._last_seen == "7"
        assert h.engine._plan_seen_active == "7"
        assert h.engine._gate_logged == {"7": "open", "1": "closed"}
        assert [s[1] for s in h.engine._burn._samples] == ["7", "1"]

    def test_quarantine_written_on_an_old_number_follows_its_account(
        self, temp_home: Path
    ):
        from claude_swap.autoswitch import _refresh_fingerprint

        h = self._harness(temp_home)
        creds = h.switcher.read_account_credentials("2", "b@example.com")
        # An entry written for b at 2 after b had already moved to 5 (so the
        # roster commit could not remap it).
        h.switcher.move_account("2", "5")
        atomic_write_json(h.switcher.backup_dir / "autoswitch_state.json", {
            "quarantine": {"2": {
                "email": "b@example.com", "reason": "invalid_grant",
                "refreshTokenFingerprint": _refresh_fingerprint(creds),
            }},
        })
        state = h.engine._release_recovered_quarantines(h.engine._read_state())
        assert list(state["quarantine"]) == ["5"]
