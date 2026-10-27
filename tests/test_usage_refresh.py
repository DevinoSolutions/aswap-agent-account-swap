"""`aswap usage refresh`: the on-demand escape hatch from the poll cadence.

Zero network: the fetch is stubbed everywhere below. What is under test is
which slots are CLAIMED and which brakes hold — the cadence gate is a
measured request budget, so an escape hatch from it has to be a door and
not a hole.

Tmp-dir stores only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import cli, oauth
from claude_swap.credentials import ActiveCredentials
from claude_swap.switcher import ClaudeAccountSwitcher
from tests import statefiles
from claude_swap.usage_store import (
    AUTH_DEAD_STRIKES,
    MIN_FORCED_AGE_S,
    SERVE_TTL_S,
    _row_eligible,
)

EMAIL_1 = "account1@example.com"
EMAIL_2 = "account2@example.com"

CREDS = json.dumps(
    {
        "claudeAiOauth": {
            "accessToken": "a",
            "refreshToken": "r",
            "expiresAt": 4_000_000_000_000,
        }
    }
)
USAGE = {"five_hour": {"pct": 12.0}}


class FakeClock:
    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def switcher(temp_home: Path) -> ClaudeAccountSwitcher:
    sw = ClaudeAccountSwitcher()
    sw._setup_directories()
    sw._write_json(
        sw.sequence_file,
        {
            "activeAccountNumber": None,
            "lastUpdated": "2024-01-01T00:00:00Z",
            "sequence": [1, 2],
            "accounts": {
                "1": {"email": EMAIL_1, "uuid": "uuid-1", "organizationUuid": "",
                      "added": "2024-01-01T00:00:00Z"},
                "2": {"email": EMAIL_2, "uuid": "uuid-2", "organizationUuid": "",
                      "added": "2024-01-02T00:00:00Z"},
            },
        },
    )
    for num, email in (("1", EMAIL_1), ("2", EMAIL_2)):
        sw._store._write_account_credentials(num, email, CREDS)
    sw.clock = FakeClock()
    sw._usage_store.clock = sw.clock
    return sw


def _refresh(switcher: ClaudeAccountSwitcher, **kwargs) -> list[dict]:
    """Drive `refresh_usage` with a stubbed (zero-network) fetch."""
    with patch(
        "claude_swap.oauth.try_fetch_usage_for_account",
        return_value=oauth.UsageOutcome(USAGE),
    ), patch.object(
        switcher, "_read_active_credentials",
        return_value=ActiveCredentials("", False),
    ):
        return switcher.refresh_usage(**kwargs)


def _seed_usage(switcher: ClaudeAccountSwitcher) -> None:
    """Give both slots a stored measurement at the current fake time."""
    _refresh(switcher)


# --------------------------------------------------------------------------- #
# The brakes
# --------------------------------------------------------------------------- #
class TestMaxAgeFiltering:
    def test_a_fresh_row_is_not_re_polled(self, switcher):
        _seed_usage(switcher)
        switcher.clock.advance(MIN_FORCED_AGE_S + 1)  # past the global stamp

        rows = _refresh(switcher, max_age_s=600)

        assert [r["fetched"] for r in rows] == [False, False]
        assert {r["skippedReason"] for r in rows} == {"fresh-enough"}

    def test_a_row_past_max_age_is_re_polled_inside_the_serve_ttl(self, switcher):
        """The whole point: beat the TTL for a caller that needs a better number."""
        _seed_usage(switcher)
        switcher.clock.advance(MIN_FORCED_AGE_S + 1)
        assert MIN_FORCED_AGE_S + 1 < SERVE_TTL_S  # still "fresh" to a normal poll

        rows = _refresh(switcher, max_age_s=MIN_FORCED_AGE_S)

        assert all(r["fetched"] for r in rows)

    def test_max_age_is_floored(self, switcher):
        """Nothing may ask for a number fresher than the endpoint should give."""
        _seed_usage(switcher)
        switcher.clock.advance(MIN_FORCED_AGE_S + 1)

        rows = _refresh(switcher, max_age_s=0)

        # Floored to MIN_FORCED_AGE_S, and the rows are just past it.
        assert all(r["fetched"] for r in rows)

        switcher.clock.advance(MIN_FORCED_AGE_S + 1)
        rows = _refresh(switcher, max_age_s=-5)
        assert all(r["fetched"] for r in rows)

    def test_a_never_fetched_row_always_qualifies(self, switcher):
        rows = _refresh(switcher, max_age_s=600)

        assert all(r["fetched"] for r in rows)


class TestTheGlobalSweepStamp:
    def test_a_second_sweep_inside_the_window_is_refused(self, switcher):
        _seed_usage(switcher)

        rows = _refresh(switcher, max_age_s=MIN_FORCED_AGE_S)

        assert [r["fetched"] for r in rows] == [False, False]
        assert {r["skippedReason"] for r in rows} == {"swept-recently"}

    def test_the_window_expires(self, switcher):
        _seed_usage(switcher)
        switcher.clock.advance(MIN_FORCED_AGE_S + 1)

        rows = _refresh(switcher, max_age_s=MIN_FORCED_AGE_S)

        assert all(r["fetched"] for r in rows)

    def test_the_stamp_lives_under_this_switchers_store(self, switcher):
        """Not the import-bound cache dir, which points at the real store."""
        _seed_usage(switcher)

        stamp = switcher.backup_dir / "cache" / "forced_usage_sweep.json"
        assert statefiles.exists(stamp)

    def test_the_stamp_is_read_and_claimed_under_one_lock(self, switcher):
        """Unlocked, two callers both read 'no recent sweep' and both sweep."""
        from claude_swap.locking import FileLock

        held = FileLock(switcher.lock_file, timeout=0.1)
        assert held.acquire()
        try:
            rows = _refresh(switcher)
        finally:
            held.release()

        assert [r["skippedReason"] for r in rows] == ["lock-busy", "lock-busy"]
        assert not (
            switcher.backup_dir / "cache" / "forced_usage_sweep.json"
        ).exists()

    def test_the_lock_is_released_before_the_fetch(self, switcher):
        """`_collect_usage_entries` takes it again; FileLock is not reentrant."""
        rows = _refresh(switcher)

        assert all(r["fetched"] for r in rows)


class TestTheLibraryGuardsItsOwnArgument:
    """The CLI rejects a non-finite `--max-age`; a library caller has no CLI.

    `max(nan, 30.0)` is `nan` and every `age < nan` is False, so a `nan` that
    reaches the floor takes the brake off entirely rather than tightening it.
    """

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_a_non_finite_max_age_is_refused(self, switcher, value):
        with pytest.raises(ValueError, match="finite"):
            switcher.refresh_usage(max_age_s=value)

    def test_it_refuses_before_it_sweeps(self, switcher):
        """A refused call must not have claimed the global stamp on its way out."""
        with pytest.raises(ValueError):
            switcher.refresh_usage(max_age_s=float("nan"))

        assert not (
            switcher.backup_dir / "cache" / "forced_usage_sweep.json"
        ).exists()
        # ...and the next honest caller still gets its sweep.
        assert all(r["fetched"] for r in _refresh(switcher))

    def test_a_negative_max_age_is_clamped_not_refused(self, switcher):
        """It states an intent the floor already answers, unlike `nan`."""
        _seed_usage(switcher)
        switcher.clock.advance(MIN_FORCED_AGE_S + 1)

        assert all(r["fetched"] for r in _refresh(switcher, max_age_s=-5))


class TestACorruptSweepStampIsDistinguishableFromAnAbsentOne:
    """Both degrade to "never swept" — silently resetting a cross-process
    cooldown. Absent is the ordinary first run; unreadable every time is a
    stuck brake, and only the log can tell the two apart."""

    def _stamp(self, switcher) -> Path:
        return switcher.backup_dir / "cache" / "forced_usage_sweep.json"

    def test_an_absent_stamp_is_silent(self, switcher, caplog):
        assert not statefiles.exists(self._stamp(switcher))

        with caplog.at_level(0, logger="claude-swap"):
            rows = _refresh(switcher)

        assert all(r["fetched"] for r in rows)
        assert not [r for r in caplog.records if "sweep stamp" in r.getMessage()]

    def test_an_unreadable_stamp_warns_and_still_sweeps(self, switcher, caplog):
        stamp = self._stamp(switcher)
        stamp.parent.mkdir(parents=True, exist_ok=True)
        statefiles.write_text(stamp, "{truncated")

        with caplog.at_level(0, logger="claude-swap"):
            rows = _refresh(switcher)

        assert all(r["fetched"] for r in rows)  # degraded, never raised
        warnings = [
            r for r in caplog.records if "sweep stamp" in r.getMessage()
        ]
        assert [r.levelname for r in warnings] == ["WARNING"]
        assert str(stamp) in warnings[0].getMessage()  # names the file
        assert warnings[0].exc_info  # and carries the cause

    def test_a_stamp_holding_the_wrong_shape_warns_too(self, switcher, caplog):
        """Valid JSON, no `at`: the read succeeded and still says nothing."""
        stamp = self._stamp(switcher)
        stamp.parent.mkdir(parents=True, exist_ok=True)
        statefiles.write_text(stamp, '{"swept": true}')

        with caplog.at_level(0, logger="claude-swap"):
            _refresh(switcher)

        assert [
            r.levelname
            for r in caplog.records
            if "sweep stamp" in r.getMessage()
        ] == ["WARNING"]


class TestAccountFilter:
    def test_one_account_can_be_targeted(self, switcher):
        rows = _refresh(switcher, account="2")

        assert [r["number"] for r in rows] == [2]
        assert rows[0]["fetched"] is True

    def test_an_unknown_account_raises(self, switcher):
        from claude_swap.exceptions import AccountNotFoundError

        with pytest.raises(AccountNotFoundError):
            _refresh(switcher, account="99")


# --------------------------------------------------------------------------- #
# What `force` must NOT waive
# --------------------------------------------------------------------------- #
class TestForceOnlyWaivesTheCadence:
    """`respect_plans=False` was never a full bypass (a fresh row with no due
    plan is still refused), so a narrow `force` flag waives the cadence
    judgment — and only that."""

    def _row(self, **over) -> dict:
        row = {
            "authDeadStrikes": 0,
            "backoffUntil": None,
            "claimUntil": None,
            "lastAttemptAt": None,
            "fetchedAt": 1_000.0,
            "nextPollAt": 9_999_999.0,
            "pollIntervalS": 600.0,
        }
        row.update(over)
        return row

    def test_a_fresh_row_with_a_future_plan_is_refused_without_force(self):
        assert not _row_eligible(self._row(), 1_010.0, respect_plans=False)

    def test_force_claims_it(self):
        assert _row_eligible(self._row(), 1_010.0, respect_plans=False, force=True)

    def test_force_does_not_waive_the_dead_token_quarantine(self):
        """Forcing past a strike would POST a grant the server condemned."""
        row = self._row(authDeadStrikes=AUTH_DEAD_STRIKES)

        assert not _row_eligible(row, 1_010.0, respect_plans=False, force=True)

    def test_force_does_not_waive_a_429_backoff(self):
        row = self._row(backoffUntil=9_999_999.0)

        assert not _row_eligible(row, 1_010.0, respect_plans=False, force=True)

    def test_force_does_not_waive_another_collectors_live_claim(self):
        """Ignoring a live lease double-fetches; it does not refresh anything."""
        row = self._row(claimUntil=9_999_999.0)

        assert not _row_eligible(row, 1_010.0, respect_plans=False, force=True)


# --------------------------------------------------------------------------- #
# The command
# --------------------------------------------------------------------------- #
def _run(argv: list[str], capsys) -> tuple[int, str, str]:
    capsys.readouterr()
    with patch("os.geteuid", return_value=1000, create=True), \
         patch.object(sys, "argv", ["aswap", "usage", *argv]):
        code = 0
        try:
            cli.main()
        except SystemExit as e:
            code = e.code or 0
    captured = capsys.readouterr()
    return code, captured.out, captured.err


class TestUsageRefreshCommand:
    def test_refresh_reports_per_slot_json(self, switcher, capsys):
        with patch(
            "claude_swap.cli.ClaudeAccountSwitcher", return_value=switcher
        ), patch(
            "claude_swap.oauth.try_fetch_usage_for_account",
            return_value=oauth.UsageOutcome(USAGE),
        ), patch.object(
            switcher, "_read_active_credentials",
            return_value=ActiveCredentials("", False),
        ):
            code, out, _ = _run(["refresh", "--json"], capsys)

        assert code == 0
        payload = json.loads(out)
        assert payload["schemaVersion"] == 1
        row = payload["results"][0]
        assert set(row) >= {"number", "email", "fetched", "skippedReason"}
        assert row["fetched"] is True
        assert row["usageFetchedAt"].endswith("Z")

    def test_help_says_it_costs_no_paid_tokens(self, capsys):
        """The one thing a reader must not have to guess about this command."""
        code, out, _ = _run(["--help"], capsys)

        assert code == 0
        assert "never spends paid tokens" in out
        assert "usage-metadata endpoint" in out

    def test_a_bare_usage_verb_is_a_usage_error(self, capsys):
        code, _, err = _run([], capsys)

        assert code == 2
        assert "refresh" in err

    @pytest.mark.parametrize("value", ["nan", "inf", "-inf", "-1"])
    def test_a_non_finite_max_age_is_rejected(self, value, capsys):
        """`max(nan, 30.0)` is nan, and every `age < nan` is False — the
        floor would be off and nothing would ever count as fresh enough."""
        # `--max-age=<v>`: argparse reads a bare leading '-' as an option.
        code, _, err = _run(["refresh", f"--max-age={value}"], capsys)

        assert code == 2
        assert "finite, non-negative" in err
