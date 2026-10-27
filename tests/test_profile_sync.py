"""The profile → store adoption pass: which copy of a token family wins.

The most important file in this change. Claude Code rotates a session
profile's token family IN PLACE and nothing used to copy it back, so the
stored credential drifted into a spent grant that the next `list`/`auto`
POSTed — `invalid_grant`, one strike, `relogin_required`, and Claude Code's
own reaction (empty the token fields in place) finished the slot off.

Every cell below asserts WHICH blob the store ends up holding, because
"refused" and "adopted the wrong thing" are the two failure modes and only
one of them is visible from a return code.

Tmp-dir stores only; nothing here touches a real account store.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import cli, oauth
from claude_swap import session as session_mod
from claude_swap.session import session_dir_for, stale_marker_for
from tests import statefiles
from claude_swap.switcher import (
    PROFILE_SYNC_ADOPTED,
    PROFILE_SYNC_BUSY,
    PROFILE_SYNC_DRIFTED,
    PROFILE_SYNC_INCOMPLETE,
    PROFILE_SYNC_NO_PROFILE,
    PROFILE_SYNC_NO_STORE,
    PROFILE_SYNC_NOT_NEWER,
    PROFILE_SYNC_OUTCOMES,
    PROFILE_SYNC_STALE,
    PROFILE_SYNC_UNREADABLE,
    PROFILE_SYNC_WIPED,
    ClaudeAccountSwitcher,
)

NUM = "2"
EMAIL = "account2@example.com"
ORG_UUID = "org-uuid-2"

# A recognizable fake token: no test may find this string in any output.
CANARY = "canary-secret-do-not-print"

STORE_EXPIRES = 1_000_000_000_000
NEWER_EXPIRES = 2_000_000_000_000


def _creds(access: str, refresh: str, expires_at: int) -> str:
    return json.dumps(
        {
            "claudeAiOauth": {
                "accessToken": access,
                "refreshToken": refresh,
                "expiresAt": expires_at,
                "scopes": ["user:inference"],
            }
        }
    )


STORE_CREDS = _creds("store-access", "store-refresh", STORE_EXPIRES)
PROFILE_NEWER = _creds(f"prof-access-{CANARY}", f"prof-refresh-{CANARY}", NEWER_EXPIRES)
WIPED_CREDS = json.dumps(
    {"claudeAiOauth": {"accessToken": "", "refreshToken": "", "mcpOAuth": {"a": 1}}}
)


def _config(email: str = EMAIL, org: str = ORG_UUID) -> str:
    return json.dumps(
        {"oauthAccount": {"emailAddress": email, "organizationUuid": org}}
    )


@pytest.fixture
def switcher(temp_home: Path) -> ClaudeAccountSwitcher:
    """Slot 2 managed, with a stored credential and no profile yet."""
    sw = ClaudeAccountSwitcher()
    sw._setup_directories()
    sw._write_json(
        sw.sequence_file,
        {
            "activeAccountNumber": 1,
            "lastUpdated": "2024-01-01T00:00:00Z",
            "sequence": [1, 2],
            "accounts": {
                "1": {
                    "email": "account1@example.com",
                    "uuid": "uuid-1",
                    "organizationUuid": "",
                    "added": "2024-01-01T00:00:00Z",
                },
                NUM: {
                    "email": EMAIL,
                    "uuid": "uuid-2",
                    "organizationUuid": ORG_UUID,
                    "organizationName": "Org Two",
                    "added": "2024-01-02T00:00:00Z",
                },
            },
        },
    )
    sw._store._write_account_credentials(NUM, EMAIL, STORE_CREDS)
    sw._write_account_config(NUM, EMAIL, _config())
    return sw


def _profile(switcher: ClaudeAccountSwitcher) -> Path:
    return session_dir_for(switcher.backup_dir, NUM, EMAIL)


def _hydrate(
    switcher: ClaudeAccountSwitcher, creds: str, *, config: str | None = None
) -> Path:
    """Put a credential blob in slot 2's profile, the way `claude` would."""
    profile = _profile(switcher)
    profile.mkdir(parents=True, exist_ok=True)
    (profile / ".credentials.json").write_text(creds, encoding="utf-8")
    (profile / ".claude.json").write_text(config or _config(), encoding="utf-8")
    return profile


def _make_live(profile: Path) -> None:
    """A live claude in this profile (our own PID is always alive)."""
    pid_dir = profile / "sessions"
    pid_dir.mkdir(parents=True, exist_ok=True)
    (pid_dir / f"{os.getpid()}.json").write_text(json.dumps({"pid": os.getpid()}))


def _stored(switcher: ClaudeAccountSwitcher) -> str:
    return switcher._read_account_credentials(NUM, EMAIL)


# --------------------------------------------------------------------------- #
# The safety matrix
# --------------------------------------------------------------------------- #
class TestWhichCopyWins:
    def test_a_strictly_newer_profile_is_adopted(self, switcher):
        _hydrate(switcher, PROFILE_NEWER)

        assert switcher.sync_profile_credential(NUM, EMAIL, ORG_UUID) == (
            PROFILE_SYNC_ADOPTED
        )
        assert _stored(switcher) == PROFILE_NEWER

    def test_an_older_profile_never_wins(self, switcher):
        _hydrate(switcher, _creds("old-a", "old-r", STORE_EXPIRES - 1))

        assert switcher.sync_profile_credential(NUM, EMAIL, ORG_UUID) == (
            PROFILE_SYNC_NOT_NEWER
        )
        assert _stored(switcher) == STORE_CREDS

    def test_an_expiry_tie_changes_nothing(self, switcher):
        """Ties are not evidence; only a strictly later expiry is."""
        _hydrate(switcher, _creds("tie-a", "tie-r", STORE_EXPIRES))

        assert switcher.sync_profile_credential(NUM, EMAIL, ORG_UUID) == (
            PROFILE_SYNC_NOT_NEWER
        )
        assert _stored(switcher) == STORE_CREDS

    def test_same_lineage_with_a_rotated_access_token_changes_nothing(
        self, switcher
    ):
        """Same refresh token = same generation, whatever the access token says."""
        same_family = _creds("rotated-access", "store-refresh", NEWER_EXPIRES)
        _hydrate(switcher, same_family)

        assert switcher.sync_profile_credential(NUM, EMAIL, ORG_UUID) == (
            PROFILE_SYNC_NOT_NEWER
        )
        assert _stored(switcher) == STORE_CREDS

    def test_a_wiped_profile_is_refused(self, switcher):
        """The documented destruction chain: never write the husk back."""
        _hydrate(switcher, WIPED_CREDS)

        assert switcher.sync_profile_credential(NUM, EMAIL, ORG_UUID) == (
            PROFILE_SYNC_WIPED
        )
        assert _stored(switcher) == STORE_CREDS

    def test_a_wiped_profile_warns_once_an_hour(self, switcher, monkeypatch, caplog):
        """The pass runs every few seconds; 3,000 identical warnings is noise."""
        import logging

        from claude_swap import switcher as sw_mod

        now = [1000.0]
        monkeypatch.setattr(sw_mod.time, "time", lambda: now[0])
        _hydrate(switcher, WIPED_CREDS)
        with caplog.at_level(logging.WARNING):
            for step in (0.0, 5.0, sw_mod._WIPED_WARN_INTERVAL_S):
                now[0] += step
                assert switcher.sync_profile_credential(NUM, EMAIL, ORG_UUID) == (
                    PROFILE_SYNC_WIPED
                )
        assert caplog.text.count("wiped credential") == 1
        assert caplog.text.count("still needs a re-login") == 1

    def test_a_wiped_profile_is_refused_even_when_it_looks_newer(self, switcher):
        """Expiry cannot buy a wiped blob its way in."""
        _hydrate(
            switcher,
            json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": "",
                        "refreshToken": "",
                        "expiresAt": NEWER_EXPIRES,
                    }
                }
            ),
        )

        assert switcher.sync_profile_credential(NUM, EMAIL, ORG_UUID) == (
            PROFILE_SYNC_WIPED
        )
        assert _stored(switcher) == STORE_CREDS

    def test_a_torn_read_changes_nothing(self, switcher):
        """A blob that never parsed is 'I could not tell', not 'nothing here'."""
        profile = _hydrate(switcher, PROFILE_NEWER)
        (profile / ".credentials.json").write_text(
            PROFILE_NEWER[: len(PROFILE_NEWER) // 2], encoding="utf-8"
        )

        assert switcher.sync_profile_credential(NUM, EMAIL, ORG_UUID) == (
            PROFILE_SYNC_UNREADABLE
        )
        assert _stored(switcher) == STORE_CREDS

    def test_a_zero_length_profile_is_unreadable_not_absent(self, switcher):
        """The likeliest torn state, and the one that used to read as absence.

        A truncate-then-write leaves zero bytes for as long as the writer
        takes. Classifying that `no-profile` means "nothing to adopt here",
        which lets the consume gate go on to POST the grant the profile
        just rotated past.
        """
        profile = _hydrate(switcher, PROFILE_NEWER)
        (profile / ".credentials.json").write_text("", encoding="utf-8")

        assert switcher.sync_profile_credential(NUM, EMAIL, ORG_UUID) == (
            PROFILE_SYNC_UNREADABLE
        )
        assert _stored(switcher) == STORE_CREDS

    def test_a_whitespace_only_profile_is_unreadable_too(self, switcher):
        profile = _hydrate(switcher, PROFILE_NEWER)
        (profile / ".credentials.json").write_text("   \n", encoding="utf-8")

        assert switcher.sync_profile_credential(NUM, EMAIL, ORG_UUID) == (
            PROFILE_SYNC_UNREADABLE
        )
        assert _stored(switcher) == STORE_CREDS

    def test_a_genuinely_absent_profile_is_still_absent(self, switcher):
        """The distinction must survive: empty is not missing, missing is not empty."""
        profile = _profile(switcher)
        profile.mkdir(parents=True, exist_ok=True)

        assert switcher.sync_profile_credential(NUM, EMAIL, ORG_UUID) == (
            PROFILE_SYNC_NO_PROFILE
        )
        assert _stored(switcher) == STORE_CREDS

    def test_the_reseed_write_is_atomic(self, switcher):
        """A reader sees the old credential or the new one, never zero bytes."""
        profile = _hydrate(switcher, STORE_CREDS)
        seen: list[str] = []
        real_replace = session_mod.replace_with_retry

        def watched(src, dst):
            # Mid-write: the destination must still hold the OLD bytes.
            seen.append((profile / ".credentials.json").read_text(encoding="utf-8"))
            return real_replace(src, dst)

        with patch.object(session_mod, "replace_with_retry", watched):
            assert session_mod.reseed_session_credentials(profile, PROFILE_NEWER)

        assert seen == [STORE_CREDS]  # never observed empty or partial
        assert (profile / ".credentials.json").read_text(encoding="utf-8") == (
            PROFILE_NEWER
        )

    def test_a_failed_reseed_leaves_the_old_credential_intact(self, switcher):
        profile = _hydrate(switcher, STORE_CREDS)

        def boom(src, dst):
            raise OSError("read-only mount")

        with patch.object(session_mod, "replace_with_retry", boom):
            assert session_mod.reseed_session_credentials(profile, PROFILE_NEWER) is (
                False
            )

        assert (profile / ".credentials.json").read_text(encoding="utf-8") == (
            STORE_CREDS
        )
        assert not list(profile.glob("*.tmp"))  # and cleaned up after itself

    def test_a_live_profile_with_an_unreadable_identity_is_refused(self, switcher):
        """An in-pane /login writes the credential and the identity separately."""
        profile = _hydrate(switcher, PROFILE_NEWER)
        _make_live(profile)
        (profile / ".claude.json").write_text("{not json", encoding="utf-8")

        assert switcher.sync_profile_credential(NUM, EMAIL, ORG_UUID) == (
            PROFILE_SYNC_DRIFTED
        )
        assert _stored(switcher) == STORE_CREDS

    def test_an_idle_profile_with_an_unreadable_identity_is_still_adopted(
        self, switcher
    ):
        """A broken .claude.json must not cost an idle slot its token family."""
        profile = _hydrate(switcher, PROFILE_NEWER)
        (profile / ".claude.json").write_text("{not json", encoding="utf-8")

        assert switcher.sync_profile_credential(NUM, EMAIL, ORG_UUID) == (
            PROFILE_SYNC_ADOPTED
        )
        assert _stored(switcher) == PROFILE_NEWER

    def test_a_torn_read_is_retried_before_it_is_believed(self, switcher):
        """A live writer caught mid-write must not cost the slot a pass."""
        _hydrate(switcher, PROFILE_NEWER)
        reads = iter(["{half", "{half", PROFILE_NEWER])
        with patch.object(
            session_mod, "read_session_credentials", lambda _d: next(reads)
        ):
            result = switcher.sync_profile_credential(NUM, EMAIL, ORG_UUID)

        assert result == PROFILE_SYNC_ADOPTED
        assert _stored(switcher) == PROFILE_NEWER

    def test_half_a_credential_is_not_an_improvement(self, switcher):
        _hydrate(switcher, json.dumps(
            {"claudeAiOauth": {"accessToken": "a", "expiresAt": NEWER_EXPIRES}}
        ))

        assert switcher.sync_profile_credential(NUM, EMAIL, ORG_UUID) == (
            PROFILE_SYNC_INCOMPLETE
        )
        assert _stored(switcher) == STORE_CREDS

    def test_a_stale_marked_profile_never_supersedes_the_store(self, switcher):
        """It is presumed stale AGAINST this store copy; it cannot outrank it."""
        profile = _hydrate(switcher, PROFILE_NEWER)
        stale_marker_for(profile).touch()

        assert switcher.sync_profile_credential(NUM, EMAIL, ORG_UUID) == (
            PROFILE_SYNC_STALE
        )
        assert _stored(switcher) == STORE_CREDS

    def test_a_drifted_identity_is_never_filed_under_this_slot(self, switcher):
        """An in-session /login points the profile at someone else's account."""
        _hydrate(switcher, PROFILE_NEWER, config=_config("someone@else.com", ""))

        assert switcher.sync_profile_credential(NUM, EMAIL, ORG_UUID) == (
            PROFILE_SYNC_DRIFTED
        )
        assert _stored(switcher) == STORE_CREDS

    def test_an_org_only_drift_is_caught_too(self, switcher):
        """Two slots can share an email across organizations."""
        _hydrate(switcher, PROFILE_NEWER, config=_config(EMAIL, "other-org"))

        assert switcher.sync_profile_credential(NUM, EMAIL, ORG_UUID) == (
            PROFILE_SYNC_DRIFTED
        )
        assert _stored(switcher) == STORE_CREDS

    def test_no_profile_is_not_a_failure(self, switcher):
        assert switcher.sync_profile_credential(NUM, EMAIL, ORG_UUID) == (
            PROFILE_SYNC_NO_PROFILE
        )
        assert _stored(switcher) == STORE_CREDS

    def test_an_empty_store_slot_is_not_an_invitation(self, switcher):
        """Mid-removal, or a locked keychain: either way, don't install one."""
        _hydrate(switcher, PROFILE_NEWER)
        switcher._store._delete_account_credentials(NUM, EMAIL)

        assert switcher.sync_profile_credential(NUM, EMAIL, ORG_UUID) == (
            PROFILE_SYNC_NO_STORE
        )
        assert _stored(switcher) == ""

    @pytest.mark.parametrize("live", [False, True])
    def test_adoption_does_not_depend_on_the_profile_being_quiescent(
        self, switcher, live
    ):
        """Adopting only READS the profile, so a running claude cannot be hurt."""
        profile = _hydrate(switcher, PROFILE_NEWER)
        if live:
            _make_live(profile)

        assert switcher.sync_profile_credential(NUM, EMAIL, ORG_UUID) == (
            PROFILE_SYNC_ADOPTED
        )
        assert _stored(switcher) == PROFILE_NEWER

    def test_a_held_lock_yields_lock_busy_and_touches_nothing(self, switcher):
        """The adoption pass runs on every ``aswap list``, so contention is
        the normal case, not the rare one. It must cost a caller its turn and
        nothing else — the store keeps its bytes, the profile keeps its own."""
        from claude_swap.locking import FileLock

        profile = _hydrate(switcher, PROFILE_NEWER)  # would otherwise be adopted
        held = FileLock(switcher.lock_file, timeout=0.1)
        assert held.acquire()
        try:
            result = switcher.sync_profile_credential(NUM, EMAIL, ORG_UUID)
        finally:
            held.release()

        assert result == PROFILE_SYNC_BUSY
        assert _stored(switcher) == STORE_CREDS
        assert (profile / ".credentials.json").read_text(encoding="utf-8") == (
            PROFILE_NEWER
        )

    def test_every_returned_reason_is_in_the_closed_vocabulary(self, switcher):
        seen = set()
        for setup in (
            lambda: None,
            lambda: _hydrate(switcher, PROFILE_NEWER),
            lambda: _hydrate(switcher, WIPED_CREDS),
        ):
            setup()
            seen.add(switcher.sync_profile_credential(NUM, EMAIL, ORG_UUID))
        assert seen <= PROFILE_SYNC_OUTCOMES


# --------------------------------------------------------------------------- #
# The deliberate chokepoint bypass
# --------------------------------------------------------------------------- #
class TestTheAdoptionWriteSkipsPostBackupWrite:
    """`_adopt_profile_credential` is the ONE write that skips the
    session-invalidation chokepoint, and the skip has to be pinned, not
    merely commented."""

    def test_adopting_leaves_the_profile_credential_in_place(self, switcher):
        profile = _hydrate(switcher, PROFILE_NEWER)

        switcher._adopt_profile_credential(NUM, EMAIL, PROFILE_NEWER)

        assert (profile / ".credentials.json").read_text(encoding="utf-8") == (
            PROFILE_NEWER
        )

    def test_an_ordinary_store_write_still_invalidates_it(self, switcher):
        """The contrast that makes the bypass meaningful rather than a leak."""
        profile = _hydrate(switcher, PROFILE_NEWER)

        switcher._write_account_credentials(NUM, EMAIL, STORE_CREDS)

        assert not (profile / ".credentials.json").exists()

    def test_adopting_does_not_stale_mark_a_live_profile(self, switcher):
        profile = _hydrate(switcher, PROFILE_NEWER)
        _make_live(profile)

        switcher._adopt_profile_credential(NUM, EMAIL, PROFILE_NEWER)

        assert not session_mod.is_session_stale(profile)

    def test_adopting_clears_a_pre_existing_stale_marker(self, switcher):
        """The marker records 'the store moved under this profile' — no longer true."""
        profile = _hydrate(switcher, PROFILE_NEWER)
        stale_marker_for(profile).touch()

        switcher._adopt_profile_credential(NUM, EMAIL, PROFILE_NEWER)

        assert not session_mod.is_session_stale(profile)


# --------------------------------------------------------------------------- #
# Where the pass runs
# --------------------------------------------------------------------------- #
class TestTheCollectPassAdoptsFirst:
    def test_usage_collection_reconciles_before_it_fetches(self, switcher):
        _hydrate(switcher, PROFILE_NEWER)

        with patch(
            "claude_swap.oauth.try_fetch_usage_for_account",
            return_value=oauth.UsageOutcome({"five_hour": {"pct": 1.0}}),
        ), patch.object(
            switcher, "_read_active_credentials",
            return_value=__import__(
                "claude_swap.credentials", fromlist=["ActiveCredentials"]
            ).ActiveCredentials("", False),
        ):
            switcher.list_accounts(json_output=True)

        assert _stored(switcher) == PROFILE_NEWER

    def test_the_active_slot_is_left_to_its_own_live_store(self, switcher):
        """A session profile is not the active account's credential truth."""
        _hydrate(switcher, PROFILE_NEWER)
        info = [(2, EMAIL, "Org Two", ORG_UUID, True, STORE_CREDS, "")]

        assert switcher.sync_profiles_into_store(info) == {}
        assert _stored(switcher) == STORE_CREDS

    def test_slots_without_a_profile_dir_cost_nothing(self, switcher):
        info = [(2, EMAIL, "Org Two", ORG_UUID, False, STORE_CREDS, "")]

        assert switcher.sync_profiles_into_store(info) == {}


class TestTheConsumeGate:
    def test_a_live_profile_is_adopted_but_its_grant_is_never_posted(
        self, switcher
    ):
        """POSTing a running claude's one-time-use grant is the destruction chain."""
        profile = _hydrate(switcher, PROFILE_NEWER)
        _make_live(profile)

        with patch("claude_swap.oauth.try_refresh_oauth_credentials") as refresh:
            outcome = switcher.consume_backup_grant(NUM, EMAIL, STORE_CREDS)

        refresh.assert_not_called()
        assert outcome.error == "profile-owns-rotation"
        assert outcome.credentials is None  # not the "stored it, but" shape
        assert _stored(switcher) == PROFILE_NEWER  # ...adoption still happened

    def test_a_live_profile_on_the_SAME_family_is_never_posted(self, switcher):
        """The commonest state, and the one that used to slip through.

        A profile freshly seeded from this very store copy has an EQUAL
        fingerprint, so there is nothing to adopt — and a resident claude is
        nonetheless holding the exact grant about to be POSTed. Deciding
        ownership inside the adoption branch answered the question only
        where it was least likely to matter.
        """
        profile = _hydrate(switcher, STORE_CREDS)  # same bytes as the store
        _make_live(profile)

        with patch("claude_swap.oauth.try_refresh_oauth_credentials") as refresh:
            outcome = switcher.consume_backup_grant(NUM, EMAIL, STORE_CREDS)

        refresh.assert_not_called()
        assert outcome.error == "profile-owns-rotation"
        assert _stored(switcher) == STORE_CREDS

    def test_a_live_profile_that_is_unreadable_counts_as_owning_it(self, switcher):
        """Not knowing is not knowing nobody needs this grant."""
        profile = _hydrate(switcher, STORE_CREDS)
        _make_live(profile)
        (profile / ".credentials.json").write_text("", encoding="utf-8")

        with patch("claude_swap.oauth.try_refresh_oauth_credentials") as refresh:
            outcome = switcher.consume_backup_grant(NUM, EMAIL, STORE_CREDS)

        refresh.assert_not_called()
        assert outcome.error == "profile-owns-rotation"

    def test_an_unreadable_session_record_counts_as_live(self, switcher):
        """`_live_session_pids` is scan-shaped; a guard must not be."""
        profile = _hydrate(switcher, STORE_CREDS)
        pid_dir = profile / "sessions"
        pid_dir.mkdir(parents=True, exist_ok=True)
        (pid_dir / "9999.json").write_text("not json{{{")

        with patch("claude_swap.oauth.try_refresh_oauth_credentials") as refresh:
            outcome = switcher.consume_backup_grant(NUM, EMAIL, STORE_CREDS)

        refresh.assert_not_called()
        assert outcome.error == "profile-owns-rotation"

    def test_a_live_profile_on_a_DIFFERENT_family_does_not_block_the_post(
        self, switcher
    ):
        """A /login'd-away pane is not holding this slot's grant."""
        profile = _hydrate(
            switcher,
            _creds("other-a", "other-r", NEWER_EXPIRES),
            config=_config("someone@else.com", ""),
        )
        _make_live(profile)
        successor = _creds("next-a", "next-r", NEWER_EXPIRES + 1)

        with patch(
            "claude_swap.oauth.try_refresh_oauth_credentials",
            return_value=oauth.RefreshOutcome(successor, None),
        ) as refresh:
            outcome = switcher.consume_backup_grant(NUM, EMAIL, STORE_CREDS)

        refresh.assert_called_once()
        assert outcome.error is None

    def test_a_quiescent_newer_profile_is_adopted_and_consumed(self, switcher):
        """Unchanged behaviour: the backup's grant is spent, so consume the live one."""
        _hydrate(switcher, PROFILE_NEWER)
        successor = _creds("next-a", "next-r", NEWER_EXPIRES + 1)

        with patch(
            "claude_swap.oauth.try_refresh_oauth_credentials",
            return_value=oauth.RefreshOutcome(successor, None),
        ) as refresh:
            # A snapshot whose access token matches the store's, so the
            # "world already moved" short-circuit cannot mask the POST.
            outcome = switcher.consume_backup_grant(NUM, EMAIL, PROFILE_NEWER)

        assert refresh.call_args[0][0] == PROFILE_NEWER  # the adopted lineage
        assert outcome.error is None
        assert _stored(switcher) == successor

    def test_a_wiped_profile_is_refused_by_the_gate_too(self, switcher):
        _hydrate(switcher, WIPED_CREDS)

        with patch(
            "claude_swap.oauth.try_refresh_oauth_credentials",
            return_value=oauth.RefreshOutcome(None, "transient"),
        ) as refresh:
            switcher.consume_backup_grant(NUM, EMAIL, STORE_CREDS)

        assert refresh.call_args[0][0] == STORE_CREDS  # never the husk
        assert _stored(switcher) == STORE_CREDS


class TestTheDeferralIsVisible:
    """A deferred poll is otherwise indistinguishable from a stuck slot.

    A resident-but-IDLE pane never rotates, its token expires, and the row
    reports `token_expired` forever with no error and no strike — because
    nothing is broken. `usageDeferredReason` is what says so out loud.
    """

    def _payload(self, switcher) -> dict:
        with patch(
            "claude_swap.oauth.try_fetch_usage_for_account",
            return_value=oauth.UsageOutcome({"five_hour": {"pct": 1.0}}),
        ), patch.object(
            switcher, "_read_active_credentials",
            return_value=__import__(
                "claude_swap.credentials", fromlist=["ActiveCredentials"]
            ).ActiveCredentials("", False),
        ):
            return switcher.list_accounts(json_output=True)

    def test_a_healthy_row_reports_no_deferral(self, switcher):
        row = next(
            a for a in self._payload(switcher)["accounts"] if a["number"] == 2
        )
        assert row["usageDeferredReason"] is None

    def test_an_expired_token_under_a_live_profile_names_the_cause(self, switcher):
        expired = _creds("exp-a", "exp-r", 1)  # long past
        profile = _hydrate(switcher, expired)
        _make_live(profile)

        row = next(
            a for a in self._payload(switcher)["accounts"] if a["number"] == 2
        )

        assert row["usageStatus"] == "token_expired"
        assert row["usageDeferredReason"] == "profile-owns-rotation"

    def test_an_expired_token_with_no_live_profile_is_not_a_deferral(
        self, switcher
    ):
        """Nothing owns it; that row really is just expired."""
        _hydrate(switcher, _creds("exp-a", "exp-r", 1))

        row = next(
            a for a in self._payload(switcher)["accounts"] if a["number"] == 2
        )

        assert row["usageDeferredReason"] is None


class TestNeedsLogin:
    """A wiped profile is a normal, explicit state: "needs login".

    The slot is kept (the owner logs in later), held out of rotation, shown
    as such, and warned about once on entry plus an hourly reminder — across
    processes, because `list`, `auto`, the TUI and the statusline each run
    the adoption pass in their own process. A fresh credential clears it.
    """

    def _wipe(self, switcher) -> None:
        _hydrate(switcher, WIPED_CREDS)
        assert switcher.sync_profile_credential(NUM, EMAIL, ORG_UUID) == (
            PROFILE_SYNC_WIPED
        )

    def test_a_wiped_profile_marks_the_slot(self, switcher):
        assert switcher.needs_login_account_numbers() == []
        self._wipe(switcher)

        assert switcher.needs_login_account_numbers() == [NUM]
        assert switcher.is_account_needs_login(NUM) is True

    def test_a_needs_login_slot_is_out_of_rotation(self, switcher):
        assert NUM in switcher.switchable_account_numbers()
        self._wipe(switcher)

        assert NUM not in switcher.switchable_account_numbers()

    def test_warned_once_across_processes(self, switcher, caplog):
        """A second process (fresh switcher, fresh module memory) must not
        repeat the warning the first one already gave."""
        import logging

        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            self._wipe(switcher)
            other = ClaudeAccountSwitcher()
            for _ in range(3):
                assert other.sync_profile_credential(NUM, EMAIL, ORG_UUID) == (
                    PROFILE_SYNC_WIPED
                )
        assert caplog.text.count("wiped credential") == 1

    def test_a_fresh_login_in_the_profile_clears_it(self, switcher, caplog):
        import logging

        self._wipe(switcher)
        caplog.clear()
        _hydrate(switcher, PROFILE_NEWER)  # the owner logged in again
        with caplog.at_level(logging.INFO, logger="claude-swap"):
            switcher.sync_profile_credential(NUM, EMAIL, ORG_UUID)
            switcher.sync_profile_credential(NUM, EMAIL, ORG_UUID)

        assert switcher.needs_login_account_numbers() == []
        assert NUM in switcher.switchable_account_numbers()
        assert caplog.text.count("logged in again") == 1
        assert all(CANARY not in r.getMessage() for r in caplog.records)
        assert CANARY not in statefiles.read_text(switcher.sequence_file)

    def test_a_re_added_store_credential_clears_it(self, switcher, caplog):
        """`aswap add --slot N` replaces the stored credential; a live pane may
        keep the husk until it restarts, but the slot itself is usable."""
        import logging

        self._wipe(switcher)
        caplog.clear()
        switcher._store._write_account_credentials(
            NUM, EMAIL, _creds("re-added-a", "re-added-r", NEWER_EXPIRES)
        )
        with caplog.at_level(logging.INFO, logger="claude-swap"):
            for _ in range(3):
                switcher.sync_profile_credential(NUM, EMAIL, ORG_UUID)

        assert switcher.needs_login_account_numbers() == []
        assert NUM in switcher.switchable_account_numbers()
        assert caplog.text.count("logged in again") == 1
        assert "wiped credential" not in caplog.text

    def test_a_removed_profile_credential_clears_it(self, switcher):
        self._wipe(switcher)
        (_profile(switcher) / ".credentials.json").unlink()

        assert switcher.sync_profile_credential(NUM, EMAIL, ORG_UUID) == (
            PROFILE_SYNC_NO_PROFILE
        )
        assert switcher.needs_login_account_numbers() == []

    def test_a_torn_read_keeps_the_state(self, switcher):
        """Unreadable is "I could not tell", never "fresh login"."""
        profile = _profile(switcher)
        self._wipe(switcher)
        (profile / ".credentials.json").write_text("{", encoding="utf-8")

        assert switcher.sync_profile_credential(NUM, EMAIL, ORG_UUID) == (
            PROFILE_SYNC_UNREADABLE
        )
        assert switcher.needs_login_account_numbers() == [NUM]

    def test_list_marks_the_slot(self, switcher, capsys):
        self._wipe(switcher)
        capsys.readouterr()
        with patch(
            "claude_swap.oauth.try_fetch_usage_for_account",
            return_value=oauth.UsageOutcome({"five_hour": {"pct": 1.0}}),
        ), patch.object(switcher, "_read_credentials", return_value=""):
            switcher.list_accounts()

        out = capsys.readouterr().out
        line = next(ln for ln in out.splitlines() if ln.strip().startswith(f"{NUM}:"))
        assert "(needs login)" in line

    def test_json_list_marks_the_slot_ineligible(self, switcher):
        self._wipe(switcher)
        with patch(
            "claude_swap.oauth.try_fetch_usage_for_account",
            return_value=oauth.UsageOutcome({"five_hour": {"pct": 1.0}}),
        ), patch.object(switcher, "_read_credentials", return_value=""):
            payload = switcher.list_accounts(json_output=True)

        row = next(a for a in payload["accounts"] if a["number"] == int(NUM))
        assert row["needsLogin"] is True
        assert row["eligible"] is False
        assert row["ineligibleReason"] == "needs_login"

    def test_the_tui_snapshot_carries_it(self, switcher):
        self._wipe(switcher)
        with patch(
            "claude_swap.oauth.try_fetch_usage_for_account",
            return_value=oauth.UsageOutcome({"five_hour": {"pct": 1.0}}),
        ), patch.object(switcher, "_read_credentials", return_value=""):
            snap = switcher.accounts_snapshot()

        acc = next(a for a in snap.accounts if a.number == NUM)
        assert acc.needs_login is True

    def test_a_vanished_profile_directory_clears_it(self, switcher):
        """The pass skips slots without a profile directory — but not a
        marked one, or the mark would outlive the profile forever."""
        import shutil

        self._wipe(switcher)
        shutil.rmtree(_profile(switcher))

        switcher.sync_profiles_into_store(
            [(int(NUM), EMAIL, "Org Two", ORG_UUID, False, STORE_CREDS, "")]
        )

        assert switcher.needs_login_account_numbers() == []

    def test_logging_in_on_the_main_login_clears_it(self, switcher):
        """The slot became the active login with real tokens: the owner
        logged in again there, not in the pane."""
        self._wipe(switcher)

        switcher.sync_profiles_into_store(
            [(int(NUM), EMAIL, "Org Two", ORG_UUID, True, PROFILE_NEWER, "")]
        )

        assert switcher.needs_login_account_numbers() == []

    def test_an_active_slot_with_a_wiped_live_login_keeps_it(self, switcher):
        self._wipe(switcher)

        switcher.sync_profiles_into_store(
            [(int(NUM), EMAIL, "Org Two", ORG_UUID, True, WIPED_CREDS, "")]
        )

        assert switcher.needs_login_account_numbers() == [NUM]


class TestWipedClassifier:
    """One definition of the husk shape, shared with the switch-time path."""

    def test_empty_tokens_are_wiped(self):
        assert oauth.is_wiped_credential(WIPED_CREDS) is True

    def test_a_real_credential_is_not(self):
        assert oauth.is_wiped_credential(STORE_CREDS) is False

    def test_an_api_key_is_not_a_wiped_oauth_credential(self):
        assert oauth.is_wiped_credential("sk-ant-api03-abc") is False
        assert oauth.is_wiped_credential(json.dumps({"other": 1})) is False

    def test_one_surviving_token_is_not_wiped(self):
        assert oauth.is_wiped_credential(
            json.dumps({"claudeAiOauth": {"accessToken": "", "refreshToken": "r"}})
        ) is False


# --------------------------------------------------------------------------- #
# `aswap profile sync`
# --------------------------------------------------------------------------- #
def _run(argv: list[str], capsys) -> tuple[int, str, str]:
    capsys.readouterr()  # drop anything the test's own setup printed
    with patch("os.geteuid", return_value=1000, create=True), \
         patch.object(sys, "argv", ["aswap", "profile", *argv]):
        code = 0
        try:
            cli.main()
        except SystemExit as e:
            code = e.code or 0
    captured = capsys.readouterr()
    return code, captured.out, captured.err


class TestProfileSyncCommand:
    def test_sync_adopts_and_reports(self, switcher, capsys):
        _hydrate(switcher, PROFILE_NEWER)

        code, out, _ = _run(["sync", NUM, "--json"], capsys)

        assert code == 0
        row = json.loads(out)["results"][0]
        assert row["result"] == PROFILE_SYNC_ADOPTED
        assert row["adopted"] is True
        assert _stored(switcher) == PROFILE_NEWER

    def test_a_refusal_is_reported_but_is_not_a_failure(self, switcher, capsys):
        """Nothing newer to adopt is the normal case, not an error."""
        code, out, _ = _run(["sync", "--all", "--json"], capsys)

        assert code == 0
        results = {r["number"]: r["result"] for r in json.loads(out)["results"]}
        assert results[2] == PROFILE_SYNC_NO_PROFILE

    def test_a_refused_wiped_profile_names_itself(self, switcher, capsys):
        _hydrate(switcher, WIPED_CREDS)

        code, out, _ = _run(["sync", NUM], capsys)

        assert code == 0
        assert PROFILE_SYNC_WIPED in out

    def test_sync_needs_an_account_or_all(self, switcher, capsys):
        code, _, err = _run(["sync"], capsys)

        assert code == 2
        assert "--all" in err

    def test_sync_never_prints_or_logs_the_token(self, switcher, caplog, capsys):
        _hydrate(switcher, PROFILE_NEWER)

        with caplog.at_level(0, logger="claude-swap"):
            code, out, err = _run(["sync", "--all", "--json"], capsys)

        assert code == 0
        assert CANARY not in out
        assert CANARY not in err
        assert all(CANARY not in r.getMessage() for r in caplog.records)

