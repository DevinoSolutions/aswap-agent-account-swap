"""Tests for the `aswap profile` subcommand.

Everything here is a READ of ``<backup>/sessions/<slot>``: no subcommand in
this file's scope mutates a store, and none of them may ever put credential
material on stdout, on stderr or in a log record.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import cli, oauth
from claude_swap.session import SessionManager, session_dir_for
from claude_swap.switcher import ClaudeAccountSwitcher

ACCOUNT_EMAIL = "account2@example.com"
ACCOUNT_NUM = "2"
ORG_UUID = "org-uuid-2"

# A recognizable fake token: no test may find this string in any output.
CANARY = "canary-secret-do-not-print"
PROFILE_CREDS = json.dumps(
    {
        "claudeAiOauth": {
            "accessToken": f"access-{CANARY}",
            "refreshToken": f"refresh-{CANARY}",
            "expiresAt": 1_700_000_000_000,
        }
    }
)


def _run(argv: list[str], capsys) -> tuple[int, str, str]:
    """Run `aswap profile <argv>`; returns (exit_code, stdout, stderr).

    Drains first: a test's own setup (`set_account_disabled` and friends
    print to stdout) would otherwise land in front of the payload and make
    a `--json` assertion fail for a reason that is not the command's.
    """
    capsys.readouterr()
    with patch("os.geteuid", return_value=1000, create=True), \
         patch.object(sys, "argv", ["aswap", "profile", *argv]):
        code = 0
        try:
            cli.main()
        except SystemExit as e:
            code = e.code or 0
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.fixture
def seeded(temp_home: Path) -> ClaudeAccountSwitcher:
    """Two managed slots, no session profiles on disk yet."""
    switcher = ClaudeAccountSwitcher()
    switcher._setup_directories()
    switcher._write_json(
        switcher.sequence_file,
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
                ACCOUNT_NUM: {
                    "email": ACCOUNT_EMAIL,
                    "uuid": "uuid-2",
                    "organizationUuid": ORG_UUID,
                    "organizationName": "Org Two",
                    "added": "2024-01-02T00:00:00Z",
                },
            },
        },
    )
    return switcher


def _hydrate(switcher: ClaudeAccountSwitcher, creds: str = PROFILE_CREDS) -> Path:
    """Put a credential blob in slot 2's profile, the way `claude` would."""
    profile = session_dir_for(switcher.backup_dir, ACCOUNT_NUM, ACCOUNT_EMAIL)
    profile.mkdir(parents=True, exist_ok=True)
    (profile / ".credentials.json").write_text(creds, encoding="utf-8")
    (profile / ".claude.json").write_text(
        json.dumps(
            {
                "oauthAccount": {
                    "emailAddress": ACCOUNT_EMAIL,
                    "organizationUuid": ORG_UUID,
                }
            }
        ),
        encoding="utf-8",
    )
    return profile


class TestProfilePath:
    def test_prints_the_bare_path_and_nothing_else(self, seeded, capsys):
        code, out, _ = _run(["path", ACCOUNT_NUM], capsys)

        assert code == 0
        expected = session_dir_for(seeded.backup_dir, ACCOUNT_NUM, ACCOUNT_EMAIL)
        assert out.strip() == str(expected)
        assert len(out.strip().splitlines()) == 1

    def test_path_works_before_the_profile_exists(self, seeded, capsys):
        code, out, _ = _run(["path", ACCOUNT_NUM], capsys)

        assert code == 0
        assert not Path(out.strip()).exists()

    def test_path_resolves_an_email_too(self, seeded, capsys):
        code, out, _ = _run(["path", ACCOUNT_EMAIL], capsys)

        assert code == 0
        assert out.strip().endswith("2-account2_example.com")

    def test_json_reports_state_without_secrets(self, seeded, capsys):
        _hydrate(seeded)

        code, out, _ = _run(["path", ACCOUNT_NUM, "--json"], capsys)

        assert code == 0
        payload = json.loads(out)
        assert payload["schemaVersion"] == 1
        assert payload["number"] == 2
        assert payload["hydrated"] is True
        assert payload["liveSessions"] == 0
        assert payload["identityDrifted"] is False
        assert payload["tokenExpiresAt"] == "2023-11-14T22:13:20Z"
        assert CANARY not in out

    def test_unknown_account_exits_1(self, seeded, capsys):
        code, _, err = _run(["path", "99"], capsys)

        assert code == 1
        assert "99" in err

    def test_unknown_account_in_json_mode_emits_an_envelope(self, seeded, capsys):
        code, out, _ = _run(["path", "99", "--json"], capsys)

        assert code == 1
        assert json.loads(out)["error"]["type"] == "AccountNotFoundError"


class TestProfileStatus:
    def test_dry_profile_reads_as_not_hydrated(self, seeded, capsys):
        code, out, _ = _run(["status", ACCOUNT_NUM, "--json"], capsys)

        assert code == 0
        row = json.loads(out)["profiles"][0]
        assert row["exists"] is False
        assert row["hydrated"] is False
        assert row["tokenExpiresAt"] is None

    def test_a_file_without_tokens_is_not_hydrated(self, seeded, capsys):
        """The question is "would claude find a login here", not "is there a file"."""
        _hydrate(seeded, json.dumps({"claudeAiOauth": {"scopes": ["user:inference"]}}))

        code, out, _ = _run(["status", ACCOUNT_NUM, "--json"], capsys)

        row = json.loads(out)["profiles"][0]
        assert row["exists"] is True
        assert row["hydrated"] is False

    def test_an_access_token_alone_is_not_hydrated(self, seeded, capsys):
        """Without a refresh token the profile dies at the first rotation."""
        _hydrate(seeded, json.dumps({"claudeAiOauth": {"accessToken": "a"}}))

        code, out, _ = _run(["status", ACCOUNT_NUM, "--json"], capsys)

        assert json.loads(out)["profiles"][0]["hydrated"] is False

    def test_drifted_identity_is_reported(self, seeded, capsys):
        profile = _hydrate(seeded)
        (profile / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"emailAddress": "someone@else.com"}}),
            encoding="utf-8",
        )

        code, out, _ = _run(["status", ACCOUNT_NUM, "--json"], capsys)

        assert json.loads(out)["profiles"][0]["identityDrifted"] is True

    def test_all_covers_every_slot_in_list_order(self, seeded, capsys):
        code, out, _ = _run(["status", "--all", "--json"], capsys)

        assert code == 0
        assert [r["number"] for r in json.loads(out)["profiles"]] == [1, 2]

    def test_human_status_names_the_state(self, seeded, capsys):
        _hydrate(seeded)

        code, out, _ = _run(["status", "--all"], capsys)

        assert code == 0
        assert "hydrated" in out
        assert CANARY not in out

    def test_all_with_an_account_is_a_usage_error(self, seeded, capsys):
        code, _, err = _run(["status", "--all", ACCOUNT_NUM], capsys)

        assert code == 2
        assert "--all" in err

    def test_neither_account_nor_all_is_a_usage_error(self, seeded, capsys):
        code, _, err = _run(["status"], capsys)

        assert code == 2
        assert "--all" in err

    def test_a_bare_profile_verb_is_a_usage_error(self, seeded, capsys):
        code, _, err = _run([], capsys)

        assert code == 2
        assert "path" in err and "status" in err


class TestProfileHydrate:
    def test_hydrate_prepares_the_profile(self, seeded, capsys):
        seeded._write_account_credentials(
            ACCOUNT_NUM, ACCOUNT_EMAIL, PROFILE_CREDS, reason="relogin"
        )
        seeded._write_account_config(
            ACCOUNT_NUM,
            ACCOUNT_EMAIL,
            json.dumps(
                {
                    "oauthAccount": {
                        "emailAddress": ACCOUNT_EMAIL,
                        "organizationUuid": ORG_UUID,
                    }
                }
            ),
        )
        with patch.object(
            SessionManager, "_session_validity", return_value="valid"
        ), patch.object(SessionManager, "_is_session_valid", return_value=False):
            code, out, _ = _run(["hydrate", ACCOUNT_NUM, "--json"], capsys)

        assert code == 0
        row = json.loads(out)["results"][0]
        assert row["hydrated"] is True
        profile = session_dir_for(seeded.backup_dir, ACCOUNT_NUM, ACCOUNT_EMAIL)
        assert (profile / ".credentials.json").exists()

    def test_an_empty_slot_is_skipped_not_failed(self, seeded, capsys):
        """Nothing to hydrate is the command doing its job, not an error."""
        code, out, _ = _run(["hydrate", "--all", "--json"], capsys)

        assert code == 0
        results = {r["number"]: r for r in json.loads(out)["results"]}
        assert results[2]["skipped"] == "no_credentials"
        assert results[2]["hydrated"] is False

    def test_a_disabled_slot_is_skipped(self, seeded, capsys):
        seeded.set_account_disabled(ACCOUNT_NUM, True)

        code, out, _ = _run(["hydrate", ACCOUNT_NUM, "--json"], capsys)

        assert code == 0
        assert json.loads(out)["results"][0]["skipped"] == "disabled"

    def test_an_api_key_slot_is_skipped(self, seeded, capsys):
        data = seeded._get_sequence_data()
        data["accounts"][ACCOUNT_NUM]["kind"] = "api_key"
        seeded._write_json(seeded.sequence_file, data)

        code, out, _ = _run(["hydrate", ACCOUNT_NUM, "--json"], capsys)

        assert code == 0
        assert json.loads(out)["results"][0]["skipped"] == "api_key"

    def test_a_live_profile_is_skipped_not_prepared(self, seeded, capsys):
        """Hydrate must never be the thing that spends a running claude's grant.

        `setup_session`'s pre-bootstrap refresh POSTs a one-time-use grant
        that process still needs, and its `_bootstrap` would delete the
        profile's Keychain entry and overwrite its credential file. A live
        profile is also, by definition, already hydrated.
        """
        seeded._store._write_account_credentials(
            ACCOUNT_NUM, ACCOUNT_EMAIL, PROFILE_CREDS
        )
        profile = _hydrate(seeded)
        pid_dir = profile / "sessions"
        pid_dir.mkdir(parents=True)
        (pid_dir / f"{os.getpid()}.json").write_text(json.dumps({"pid": os.getpid()}))

        with patch("claude_swap.oauth.try_refresh_oauth_credentials") as refresh:
            code, out, _ = _run(["hydrate", ACCOUNT_NUM, "--json"], capsys)

        refresh.assert_not_called()
        assert code == 0
        row = json.loads(out)["results"][0]
        assert row["hydrated"] is False
        assert row["skipped"] == "profile-in-use"
        assert (profile / ".credentials.json").read_text(encoding="utf-8") == (
            PROFILE_CREDS
        )

    def test_an_unreadable_session_record_also_skips(self, seeded, capsys):
        seeded._store._write_account_credentials(
            ACCOUNT_NUM, ACCOUNT_EMAIL, PROFILE_CREDS
        )
        profile = _hydrate(seeded)
        pid_dir = profile / "sessions"
        pid_dir.mkdir(parents=True)
        (pid_dir / "9999.json").write_text("not json{{{")

        code, out, _ = _run(["hydrate", ACCOUNT_NUM, "--json"], capsys)

        assert code == 0
        assert json.loads(out)["results"][0]["skipped"] == "profile-in-use"

    def test_one_failing_slot_never_aborts_the_sweep(self, seeded, capsys):
        seeded._store._write_account_credentials(
            ACCOUNT_NUM, ACCOUNT_EMAIL, PROFILE_CREDS
        )  # slot 2 has credentials but no stored config -> setup_session refuses

        code, out, _ = _run(["hydrate", "--all", "--json"], capsys)

        results = json.loads(out)["results"]
        assert code == 1  # a real failure, so the exit code says so
        assert [r["number"] for r in results] == [1, 2]  # ...and slot 1 was still tried
        assert "error" in results[1]
        assert CANARY not in out


class TestProfileDehydrate:
    def test_dehydrate_drops_credentials_and_keeps_history(self, seeded, capsys):
        profile = _hydrate(seeded)
        (profile / "projects").mkdir()

        code, out, _ = _run(["dehydrate", ACCOUNT_NUM, "--json"], capsys)

        assert code == 0
        assert json.loads(out)["results"][0]["dehydrated"] is True
        assert not (profile / ".credentials.json").exists()
        assert (profile / "projects").is_dir()

    def test_a_live_profile_is_refused(self, seeded, capsys):
        profile = _hydrate(seeded)
        pid_dir = profile / "sessions"
        pid_dir.mkdir(parents=True)
        (pid_dir / f"{os.getpid()}.json").write_text(json.dumps({"pid": os.getpid()}))

        code, out, _ = _run(["dehydrate", ACCOUNT_NUM, "--json"], capsys)

        assert code == 0  # refusing to stomp a live profile is not a failure
        row = json.loads(out)["results"][0]
        assert row["dehydrated"] is False
        assert row["skipped"] == "profile-in-use"
        assert (profile / ".credentials.json").exists()

    def test_an_unreadable_record_is_refused_too(self, seeded, capsys):
        """'I could not tell' is not 'nobody is there'."""
        profile = _hydrate(seeded)
        pid_dir = profile / "sessions"
        pid_dir.mkdir(parents=True)
        (pid_dir / "9999.json").write_text("not json{{{")

        code, out, _ = _run(["dehydrate", ACCOUNT_NUM, "--json"], capsys)

        row = json.loads(out)["results"][0]
        assert row["dehydrated"] is False
        assert row["unreadableRecords"] == 1
        assert (profile / ".credentials.json").exists()

    def test_force_overrides_a_live_profile(self, seeded, capsys):
        profile = _hydrate(seeded)
        pid_dir = profile / "sessions"
        pid_dir.mkdir(parents=True)
        (pid_dir / f"{os.getpid()}.json").write_text(json.dumps({"pid": os.getpid()}))

        code, out, err = _run(
            ["dehydrate", ACCOUNT_NUM, "--force", "--json"], capsys
        )

        assert code == 0
        assert json.loads(out)["results"][0]["forced"] is True
        assert not (profile / ".credentials.json").exists()
        assert "live" in err  # ...and it said so, loudly

    def test_dehydrating_an_absent_profile_is_harmless(self, seeded, capsys):
        code, out, _ = _run(["dehydrate", "--all", "--json"], capsys)

        assert code == 0
        assert all(r["dehydrated"] for r in json.loads(out)["results"])


def _debug_pipe_is_open() -> bool:
    """Did the run's ``--debug`` actually reach ``setup_logging``?

    The canaries below assert a token is absent from the log. Absence is
    also what a *closed* pipe produces, so every one of them pairs the
    check with this: the "claude-swap" logger left at DEBUG, propagating,
    with the file handler that would have carried a leak to disk.
    """
    logger = logging.getLogger("claude-swap")
    return (
        logger.level == logging.DEBUG
        and logger.propagate
        and any(h.level <= logging.DEBUG for h in logger.handlers)
    )


class TestCredentialsNeverLeak:
    """Cross-cutting: a seeded token string appears in NO output, NO log.

    Every command runs with ``--debug``. Without it this proves much less
    than it looks like: ``switcher.__init__`` calls ``setup_logging``, which
    sets the "claude-swap" logger to INFO and so drops every DEBUG record
    before ``caplog`` (or the file handler) can ever see it — a
    ``caplog.at_level(0)`` set beforehand is simply overwritten. ``--debug``
    puts the logger at DEBUG for real, so the assertions cover the level
    where a careless ``exc_info``/payload dump would actually land.

    The on-disk ``claude-swap.log`` is checked too: it is the durable
    artifact a user would hand to someone, and it has its own handler.
    """

    ARGV = (
        ["path", ACCOUNT_NUM],
        ["path", ACCOUNT_NUM, "--json"],
        ["status", ACCOUNT_NUM],
        ["status", "--all", "--json"],
        ["sync", "--all"],
        ["sync", "--all", "--json"],
        ["hydrate", "--all", "--json"],
        ["dehydrate", "--all", "--json"],
    )

    def test_no_profile_command_prints_or_logs_the_token(
        self, seeded, caplog, capsys
    ):
        seeded._store._write_account_credentials(
            ACCOUNT_NUM, ACCOUNT_EMAIL, PROFILE_CREDS
        )
        _hydrate(seeded)

        with caplog.at_level(0):
            for argv in self.ARGV:
                _hydrate(seeded)  # re-seed: dehydrate removes it
                # `--debug` belongs to the `profile` parser, BEFORE the verb
                # (same shape as `aswap config`). After it, argparse rejects
                # it as unrecognized — which is exit 2, no command run, and a
                # canary check that proves nothing.
                code, out, err = _run(["--debug", *argv], capsys)
                assert code != 2, (argv, err)
                assert CANARY not in out, argv
                assert CANARY not in err, argv

        assert all(CANARY not in r.getMessage() for r in caplog.records)
        assert caplog.records, "nothing was logged at all — the check is vacuous"
        assert _debug_pipe_is_open()
        log_file = seeded.backup_dir / "claude-swap.log"
        if log_file.exists():
            assert CANARY not in log_file.read_text(
                encoding="utf-8", errors="replace"
            )

    def test_list_profiles_and_usage_refresh_do_not_leak_either(
        self, seeded, caplog, capsys
    ):
        seeded._store._write_account_credentials(
            ACCOUNT_NUM, ACCOUNT_EMAIL, PROFILE_CREDS
        )
        _hydrate(seeded)

        with caplog.at_level(0):
            for argv in (
                ["aswap", "list", "--json", "--provider", "claude",
                 "--profiles", "--debug"],
                # `--debug` is on the `usage` parser, not on `refresh`.
                ["aswap", "usage", "--debug", "refresh", "--json"],
            ):
                capsys.readouterr()
                code = 0
                with patch("os.geteuid", return_value=1000, create=True), \
                     patch("claude_swap.oauth.try_fetch_usage_for_account",
                           return_value=oauth.UsageOutcome(None, "transient")), \
                     patch("claude_swap.update_check.check_for_update",
                           return_value=None), \
                     patch.object(sys, "argv", argv):
                    try:
                        cli.main()
                    except SystemExit as exc:
                        code = exc.code or 0
                captured = capsys.readouterr()
                assert code != 2, (argv, captured.err)
                assert CANARY not in captured.out, argv
                assert CANARY not in captured.err, argv

        assert all(CANARY not in r.getMessage() for r in caplog.records)
        # Unlike the profile verbs, these two emit no records on the happy
        # path, so `caplog.records` is legitimately empty — assert the pipe
        # instead, or the canary above is satisfied by silence.
        assert _debug_pipe_is_open()
        log_file = seeded.backup_dir / "claude-swap.log"
        if log_file.exists():
            assert CANARY not in log_file.read_text(
                encoding="utf-8", errors="replace"
            )
