"""`_post_backup_write`'s behaviour table: persistent x live x reason.

Two halves, deliberately:

- A CHARACTERIZATION pass with `profiles.persistent` unset, asserting the
  behaviour this repo has always had. It is what makes "the default is
  bit-for-bit unchanged" a claim the suite can refute rather than a
  sentence in a commit message.
- The new branch, which fires only for `reason="rotation"` — an ordinary
  refresh of the SAME token family. Every other reason means the identity
  or the family genuinely changed, so the profile must still be
  re-bootstrapped.

Tmp-dir stores only.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import session as session_mod
from claude_swap.session import session_dir_for, stale_marker_for
from claude_swap.settings import (
    ProfileSettings,
    load_profile_settings,
    set_setting,
    settings_path,
)
from claude_swap.switcher import ClaudeAccountSwitcher

NUM = "2"
EMAIL = "account2@example.com"
ORG_UUID = "org-uuid-2"

OLD_CREDS = json.dumps(
    {"claudeAiOauth": {"accessToken": "a1", "refreshToken": "r1", "expiresAt": 1}}
)
NEW_CREDS = json.dumps(
    {"claudeAiOauth": {"accessToken": "a2", "refreshToken": "r2", "expiresAt": 2}}
)

# Every reason a store write can carry. Only the first is eligible for the
# persistent treatment; the rest must keep today's behaviour exactly.
ROTATION = "rotation"
FAMILY_CHANGING_REASONS = ("relogin", "import", "switch-backup", "slot-move", "rollback")


@pytest.fixture
def switcher(temp_home: Path) -> ClaudeAccountSwitcher:
    sw = ClaudeAccountSwitcher()
    sw._setup_directories()
    sw._write_json(
        sw.sequence_file,
        {
            "activeAccountNumber": 1,
            "lastUpdated": "2024-01-01T00:00:00Z",
            "sequence": [2],
            "accounts": {
                NUM: {
                    "email": EMAIL,
                    "uuid": "uuid-2",
                    "organizationUuid": ORG_UUID,
                    "added": "2024-01-02T00:00:00Z",
                }
            },
        },
    )
    sw._store._write_account_credentials(NUM, EMAIL, OLD_CREDS)
    return sw


def _profile(switcher: ClaudeAccountSwitcher) -> Path:
    profile = session_dir_for(switcher.backup_dir, NUM, EMAIL)
    profile.mkdir(parents=True, exist_ok=True)
    (profile / ".credentials.json").write_text(OLD_CREDS, encoding="utf-8")
    (profile / ".claude.json").write_text(
        json.dumps(
            {"oauthAccount": {"emailAddress": EMAIL, "organizationUuid": ORG_UUID}}
        ),
        encoding="utf-8",
    )
    return profile


def _make_live(profile: Path) -> None:
    pid_dir = profile / "sessions"
    pid_dir.mkdir(parents=True, exist_ok=True)
    (pid_dir / f"{os.getpid()}.json").write_text(json.dumps({"pid": os.getpid()}))


def _persist(switcher: ClaudeAccountSwitcher) -> None:
    set_setting(switcher.backup_dir, "profiles.persistent", "true")


# --------------------------------------------------------------------------- #
# The default: today's behaviour, unchanged
# --------------------------------------------------------------------------- #
class TestDefaultIsUnchanged:
    def test_the_key_defaults_to_false(self, switcher):
        assert load_profile_settings(switcher.backup_dir) == ProfileSettings()
        assert load_profile_settings(switcher.backup_dir).persistent is False

    @pytest.mark.parametrize("reason", (ROTATION, *FAMILY_CHANGING_REASONS))
    def test_a_quiescent_profile_is_always_invalidated(self, switcher, reason):
        profile = _profile(switcher)

        switcher._write_account_credentials(NUM, EMAIL, NEW_CREDS, reason=reason)

        assert not (profile / ".credentials.json").exists()
        assert (profile / ".claude.json").exists()  # history/identity survive

    @pytest.mark.parametrize("reason", (ROTATION, *FAMILY_CHANGING_REASONS))
    def test_a_live_profile_is_always_stale_marked(self, switcher, reason):
        profile = _profile(switcher)
        _make_live(profile)

        switcher._write_account_credentials(NUM, EMAIL, NEW_CREDS, reason=reason)

        assert session_mod.is_session_stale(profile)
        # ...and its own copy is left for the running claude to manage.
        assert (profile / ".credentials.json").exists()

    def test_the_default_reason_is_rotation(self, switcher):
        """Every untouched caller keeps its old meaning."""
        profile = _profile(switcher)
        _persist(switcher)

        switcher._write_account_credentials(NUM, EMAIL, NEW_CREDS)

        assert (profile / ".credentials.json").read_text(encoding="utf-8") == (
            NEW_CREDS
        )


# --------------------------------------------------------------------------- #
# persistent=true
# --------------------------------------------------------------------------- #
class TestPersistentProfiles:
    def test_a_rotation_re_seeds_a_quiescent_profile(self, switcher):
        profile = _profile(switcher)
        _persist(switcher)

        switcher._write_account_credentials(NUM, EMAIL, NEW_CREDS, reason=ROTATION)

        assert (profile / ".credentials.json").read_text(encoding="utf-8") == (
            NEW_CREDS
        )

    def test_a_rotation_clears_an_existing_stale_marker(self, switcher):
        profile = _profile(switcher)
        stale_marker_for(profile).touch()
        _persist(switcher)

        switcher._write_account_credentials(NUM, EMAIL, NEW_CREDS, reason=ROTATION)

        assert not session_mod.is_session_stale(profile)

    def test_a_rotation_leaves_a_live_profile_completely_alone(self, switcher):
        """No stale marker, no rewrite: the running claude owns its own file."""
        profile = _profile(switcher)
        _make_live(profile)
        _persist(switcher)

        switcher._write_account_credentials(NUM, EMAIL, NEW_CREDS, reason=ROTATION)

        assert not session_mod.is_session_stale(profile)
        assert (profile / ".credentials.json").read_text(encoding="utf-8") == (
            OLD_CREDS
        )

    @pytest.mark.parametrize("reason", FAMILY_CHANGING_REASONS)
    def test_a_family_changing_reason_still_invalidates(self, switcher, reason):
        """A re-login/import/switch really did change the token family."""
        profile = _profile(switcher)
        _persist(switcher)

        switcher._write_account_credentials(NUM, EMAIL, NEW_CREDS, reason=reason)

        assert not (profile / ".credentials.json").exists()

    @pytest.mark.parametrize("reason", FAMILY_CHANGING_REASONS)
    def test_a_family_changing_reason_still_marks_a_live_profile(
        self, switcher, reason
    ):
        profile = _profile(switcher)
        _make_live(profile)
        _persist(switcher)

        switcher._write_account_credentials(NUM, EMAIL, NEW_CREDS, reason=reason)

        assert session_mod.is_session_stale(profile)

    def test_a_profile_that_does_not_exist_is_not_created(self, switcher):
        """Hydration is `setup_session`'s job; a bare creds file is not a profile."""
        _persist(switcher)

        switcher._write_account_credentials(NUM, EMAIL, NEW_CREDS, reason=ROTATION)

        assert not session_dir_for(switcher.backup_dir, NUM, EMAIL).exists()

    def test_a_failed_re_seed_falls_back_to_invalidating(self, switcher, monkeypatch):
        """Safe direction: a re-bootstrap costs a launch, a stale profile an account.

        The failure is injected at the OS seam (`replace_with_retry`), not
        by stubbing out `reseed_session_credentials` — the whole point is
        that the real function's own error handling runs.
        """
        profile = _profile(switcher)
        _persist(switcher)

        def boom(src, dst):
            raise OSError("read-only mount")

        monkeypatch.setattr(session_mod, "replace_with_retry", boom)

        switcher._write_account_credentials(NUM, EMAIL, NEW_CREDS, reason=ROTATION)

        assert not (profile / ".credentials.json").exists()

    def test_an_unreadable_session_record_is_never_written_into(self, switcher):
        """`_live_session_pids` contributes no PID for a record it cannot read.

        That let an occupied profile through the live branch as "idle", and
        the re-seed would then delete a running claude's Keychain entry and
        rewrite its credential file underneath it.
        """
        profile = _profile(switcher)
        pid_dir = profile / "sessions"
        pid_dir.mkdir(parents=True, exist_ok=True)
        (pid_dir / "9999.json").write_text("not json{{{")
        _persist(switcher)

        switcher._write_account_credentials(NUM, EMAIL, NEW_CREDS, reason=ROTATION)

        # Untouched...
        assert (profile / ".credentials.json").read_text(encoding="utf-8") == (
            OLD_CREDS
        )
        # ...and the re-bootstrap is recorded the way the live branch does.
        assert session_mod.is_session_stale(profile)

    def test_the_re_seed_is_atomic(self, switcher):
        """A concurrent reader sees the old credential or the new one."""
        profile = _profile(switcher)
        _persist(switcher)
        seen: list[str] = []
        real_replace = session_mod.replace_with_retry

        def watched(src, dst):
            seen.append((profile / ".credentials.json").read_text(encoding="utf-8"))
            return real_replace(src, dst)

        with patch.object(session_mod, "replace_with_retry", watched):
            switcher._write_account_credentials(
                NUM, EMAIL, NEW_CREDS, reason=ROTATION
            )

        assert seen == [OLD_CREDS]
        assert (profile / ".credentials.json").read_text(encoding="utf-8") == (
            NEW_CREDS
        )


# --------------------------------------------------------------------------- #
# Reading the setting
# --------------------------------------------------------------------------- #
class TestProfileSettingIsReadStrictly:
    @pytest.mark.parametrize("value", ["true", "false", 1, 0, None, "yes"])
    def test_only_a_real_json_boolean_moves_it(self, tmp_path: Path, value):
        """`bool("false")` is True; a string must never turn this on."""
        settings_path(tmp_path).write_text(
            json.dumps({"profiles": {"persistent": value}})
        )

        assert load_profile_settings(tmp_path).persistent is False

    def test_a_real_true_is_honoured(self, tmp_path: Path):
        settings_path(tmp_path).write_text(
            json.dumps({"profiles": {"persistent": True}})
        )

        assert load_profile_settings(tmp_path).persistent is True

    def test_a_missing_or_corrupt_file_gives_the_default(self, tmp_path: Path):
        assert load_profile_settings(tmp_path).persistent is False
        settings_path(tmp_path).write_text("{not json")
        assert load_profile_settings(tmp_path).persistent is False

    def test_config_set_round_trips_it(self, tmp_path: Path):
        assert set_setting(tmp_path, "profiles.persistent", "true") is True
        assert load_profile_settings(tmp_path).persistent is True
        assert json.loads(settings_path(tmp_path).read_text()) == {
            "schemaVersion": 1,
            "profiles": {"persistent": True},
        }

    def test_the_setting_is_re_read_per_write(self, switcher):
        """A long-running `aswap auto` must see a `config set` without a restart."""
        profile = _profile(switcher)
        switcher._write_account_credentials(NUM, EMAIL, NEW_CREDS, reason=ROTATION)
        assert not (profile / ".credentials.json").exists()

        _profile(switcher)
        _persist(switcher)
        switcher._write_account_credentials(NUM, EMAIL, NEW_CREDS, reason=ROTATION)

        assert (profile / ".credentials.json").exists()
