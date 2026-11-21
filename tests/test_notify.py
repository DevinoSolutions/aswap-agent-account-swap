"""Tight-window email alerts (owner ask 2026-11-21): an account whose weekly
quota comes back only shortly before its paid period ends gets a heads-up 8
hours ahead and a "ready" alert at the reset."""

from __future__ import annotations

import json

import pytest

from claude_swap import notify
from claude_swap.notify import HEADS_UP_S, WEEK_S, Config, Notifier, TightWindow

RESET = 1_800_000_000.0  # the weekly reset
ENDS = RESET + 79 * 60  # bills 1h19m later (owner example: 22:00 / 23:19)
CONFIG = Config(("a@example.com", "b@example.com"), "nk_test")


class TestTightWindows:
    def test_before_and_after_the_reset(self):
        emails = {"7": "cozy@example.com"}
        before = notify.tight_windows({"7": {"endsAt": ENDS, "anchorNext": RESET}}, emails, RESET - 3600)
        after = notify.tight_windows(
            {"7": {"endsAt": ENDS, "anchorNext": RESET + WEEK_S}}, emails, RESET + 60
        )
        assert before == after == [TightWindow("cozy@example.com", RESET, ENDS)]

    def test_a_reset_after_the_period_end_is_not_a_window(self):
        rows = {"22": {"endsAt": RESET - 2 * 3600, "anchorNext": RESET}}
        assert notify.tight_windows(rows, {"22": "b@example.com"}, RESET - 5 * 86400) == []

    def test_a_long_window_is_not_tight(self):
        rows = {"1": {"endsAt": RESET + 2 * 86400, "anchorNext": RESET}}
        assert notify.tight_windows(rows, {"1": "x@example.com"}, RESET - 3600) == []

    def test_an_ended_period_or_unknown_data_is_skipped(self):
        rows = {
            "1": {"endsAt": ENDS, "anchorNext": RESET + WEEK_S},
            "2": {"endsAt": None, "anchorNext": RESET},
            "3": {"endsAt": ENDS, "anchorNext": None},
        }
        assert notify.tight_windows(rows, {"1": "a", "2": "b", "3": "c"}, ENDS + 1) == []


class TestMessage:
    def test_sparkle_headline_and_absolute_times_only(self):
        window = TightWindow("cozy@example.com", RESET, ENDS)
        subject, body = notify.message(window, "heads-up")
        assert subject.startswith("✨ ") and "1h 19m" in subject
        assert notify.message(window, "heads-up") == (subject, body)  # deterministic
        ready_subject, _ = notify.message(window, "ready")
        assert ready_subject.startswith("✨ cozy@example.com is ready")

    def test_one_key_per_account_reset_and_kind(self):
        w = TightWindow("Cozy@example.com", RESET, ENDS)
        assert notify.idempotency_key(w, "ready") == notify.idempotency_key(
            TightWindow("cozy@example.com", RESET, ENDS), "ready"
        )
        assert notify.idempotency_key(w, "ready") != notify.idempotency_key(w, "heads-up")


class TestConfig:
    def test_off_without_recipients(self):
        assert Config.from_env({}) is None

    def test_off_and_warns_without_a_key(self, caplog):
        assert Config.from_env({"ASWAP_NOTIFY_EMAILS": "a@example.com"}) is None
        assert "ASWAP_NOTIFY_NOTIFLY_KEY" in caplog.text

    def test_reads_recipients_and_key_with_defaults(self):
        cfg = Config.from_env({
            "ASWAP_NOTIFY_EMAILS": " a@example.com, b@example.com ,",
            "ASWAP_NOTIFY_NOTIFLY_KEY": "nk_x",
        })
        assert cfg == Config(("a@example.com", "b@example.com"), "nk_x")
        assert cfg.workflow == "aswap-tight-window"
        assert cfg.api_url == "https://api.notifly.io"

    def test_workflow_and_api_url_can_be_overridden(self):
        cfg = Config.from_env({
            "ASWAP_NOTIFY_EMAILS": "a@example.com",
            "ASWAP_NOTIFY_NOTIFLY_KEY": "nk_x",
            "ASWAP_NOTIFY_WORKFLOW": "my-flow",
            "ASWAP_NOTIFY_API_URL": "https://notifly.internal/",
        })
        assert (cfg.workflow, cfg.api_url) == ("my-flow", "https://notifly.internal")


class _Response:
    status = 201

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_send_notifly_triggers_the_workflow_once_for_every_recipient():
    seen = {}

    def opener(request, timeout):
        seen["url"] = request.full_url
        seen["headers"] = dict(request.header_items())
        seen["body"] = json.loads(request.data)
        return _Response()

    data = notify.payload(TightWindow("cozy@example.com", RESET, ENDS), "ready")
    notify.send_notifly(CONFIG, data, "key-1", opener=opener)
    assert seen["url"] == "https://api.notifly.io/v1/events/trigger"
    assert seen["headers"]["Authorization"] == "ApiKey nk_test"
    body = seen["body"]
    assert body["name"] == "aswap-tight-window"
    assert body["transactionId"] == "key-1"
    assert [t["email"] for t in body["to"]] == ["a@example.com", "b@example.com"]
    assert all(t["subscriberId"].startswith("aswap-") for t in body["to"])
    assert body["payload"]["subject"].startswith("✨ cozy@example.com is ready")
    assert body["payload"]["kind"] == "ready" and body["payload"]["span"] == "1h 19m"


def test_a_duplicate_transaction_counts_as_sent_and_other_errors_raise():
    import urllib.error

    def refuse(code):
        def opener(request, timeout):
            raise urllib.error.HTTPError(request.full_url, code, "x", {}, None)
        return opener

    data = notify.payload(TightWindow("cozy@example.com", RESET, ENDS), "ready")
    notify.send_notifly(CONFIG, data, "k", opener=refuse(409))
    with pytest.raises(OSError, match="401"):
        notify.send_notifly(CONFIG, data, "k", opener=refuse(401))


def test_subscriber_ids_are_stable_and_case_blind():
    assert notify.subscriber_id("A@Example.com ") == notify.subscriber_id("a@example.com")
    assert notify.subscriber_id("a@example.com") != notify.subscriber_id("b@example.com")


class _Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def _notifier(tmp_path, clock, sends, fail=False):
    def send(config, data, key):
        if fail:
            raise OSError("network down")
        sends.append((data["subject"], key))

    def inputs(now):
        nxt = RESET if now < RESET else RESET + WEEK_S
        return {"7": {"endsAt": ENDS, "anchorNext": nxt}}

    return Notifier(
        tmp_path, CONFIG, inputs=inputs, emails=lambda: {"7": "cozy@example.com"},
        send=send, clock=clock,
    )


class TestNotifier:
    def test_heads_up_eight_hours_ahead_then_ready_at_the_reset_once_each(self, tmp_path):
        clock, sends = _Clock(RESET - HEADS_UP_S - 60), []
        n = _notifier(tmp_path, clock, sends)
        assert n.maybe_run() == 0  # too early

        clock.now = RESET - HEADS_UP_S + 1
        n._next_at = 0
        assert n.maybe_run() == 1
        assert "a fresh week" in sends[-1][0]

        clock.now += 600
        n._next_at = 0
        assert n.maybe_run() == 0  # not twice

        clock.now = RESET + 30
        n2 = _notifier(tmp_path, clock, sends)  # a restarted auto remembers
        assert n2.maybe_run() == 1
        assert "is ready" in sends[-1][0]
        n2._next_at = 0
        assert n2.maybe_run() == 0
        assert len(sends) == 2

    def test_nothing_after_the_period_ends(self, tmp_path):
        clock, sends = _Clock(ENDS + 60), []
        assert _notifier(tmp_path, clock, sends).maybe_run() == 0

    def test_a_failed_send_is_retried_next_check(self, tmp_path):
        clock, sends = _Clock(RESET + 30), []
        assert _notifier(tmp_path, clock, sends, fail=True).maybe_run() == 0
        assert _notifier(tmp_path, clock, sends).maybe_run() == 1

    def test_checks_at_most_every_five_minutes(self, tmp_path):
        clock, sends = _Clock(RESET - 100), []
        n = _notifier(tmp_path, clock, sends)
        assert n.maybe_run() == 1
        state = json.loads((tmp_path / notify.STATE_FILENAME).read_text())
        assert [v["kind"] for v in state.values()] == ["heads-up"]
        clock.now = RESET + 10  # "ready" is due, but inside the check interval
        assert n.maybe_run() == 0
        clock.now = RESET - 100 + notify.CHECK_INTERVAL_S
        assert n.maybe_run() == 1


@pytest.mark.parametrize("dry_run, env_on, expected", [
    (False, True, True), (True, True, False), (False, False, False),
])
def test_engine_enables_alerts_from_the_environment(temp_home, monkeypatch, dry_run, env_on, expected):
    from tests.test_autoswitch import EngineHarness

    for key in ("ASWAP_NOTIFY_EMAILS", "ASWAP_NOTIFY_NOTIFLY_KEY"):
        monkeypatch.delenv(key, raising=False)
    if env_on:
        monkeypatch.setenv("ASWAP_NOTIFY_EMAILS", "a@example.com")
        monkeypatch.setenv("ASWAP_NOTIFY_NOTIFLY_KEY", "nk_x")
    h = EngineHarness(temp_home)
    engine = h._make_engine(dry_run=dry_run)
    assert (engine._notifier is not None) is expected
