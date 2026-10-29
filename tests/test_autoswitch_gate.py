"""The auto engine with the rate-limit gate (gate.py) and the runway rule."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import edge, gate
from claude_swap.autoswitch import (
    ENDGAME_TICK_MAX_S,
    ENDGAME_TICK_MIN_S,
    LIMIT_GUARD_S,
    NoSwitchEvent,
    SwitchEvent,
    TickOutcome,
)
from tests.test_autoswitch import EngineHarness, _usage

TIERS = {"5x": "default_claude_max_5x", "20x": "default_claude_max_20x"}


def _harness(temp_home: Path, n: int = 3, **settings) -> EngineHarness:
    h = EngineHarness(temp_home, **settings)
    for i in range(1, n + 1):
        h.seed(i, f"{chr(96 + i)}@example.com")
    h.make_live("a@example.com", 1)
    return h


@pytest.fixture(autouse=True)
def _stop_patches():
    yield
    patch.stopall()


class FakeGate:
    """Scripted probe answers keyed by the access token the probe carries
    (``sk-live`` is the active login, ``sk-N`` slot N's backup)."""

    def __init__(self, answers: dict[str, tuple[str, float]]):
        self.answers = dict(answers)
        self.calls: list[str] = []

    def __call__(self, token: str, now: float) -> gate.GateOutcome:
        self.calls.append(token)
        status, d7 = self.answers[token]
        seven = {"pct": d7, "status": "rejected" if status == "rejected" else status}
        return gate.GateOutcome({
            "status": status,
            "probedAt": now,
            "five_hour": {"pct": 10.0, "status": "allowed"},
            "seven_day": seven,
        })


def _full(pct7: float = 100.0, pct5: float = 10.0) -> dict:
    return {"five_hour": {"pct": pct5}, "seven_day": {"pct": pct7}}


def _probing(answers: dict[str, tuple[str, float]]) -> FakeGate:
    fake = FakeGate(answers)
    patch("claude_swap.gate.request_gate", side_effect=fake).start()
    return fake


def _reasons(h: EngineHarness) -> list[str]:
    return [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]


def _seen_at_98(h: EngineHarness, ago: float = 2.0, account: str = "1") -> None:
    """The active account was read at 98% a moment ago, so the tick to 99
    that follows starts a last-point clock (edge.py) instead of reading as
    a last point whose start was never seen."""
    now = h.clock.now
    h.engine._edge.observe(
        account, {"probedAt": now - ago, "seven_day": {"pct": 98.0}}, now
    )


class TestActiveRidesAnOpenGate:
    def test_endpoint_100_with_open_gate_is_not_escaped(self, temp_home, caplog):
        h = _harness(temp_home, threshold=99.9)
        _seen_at_98(h)
        fake = _probing({"sk-live": ("allowed_warning", 99.0)})
        with caplog.at_level(logging.INFO, logger="claude-swap"):
            outcome = h.tick_with_usage({"1": _full(), "2": _usage(50), "3": _usage(60)})
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 1
        assert fake.calls == ["sk-live"]
        assert any(
            "Account 1: usage API says 100% but the gate is open (99%)" in r.getMessage()
            for r in caplog.records
        )

    def test_rejected_gate_escapes_at_once(self, temp_home):
        h = _harness(temp_home, threshold=99.9)
        _probing({"sk-live": ("rejected", 100.0)})
        outcome = h.tick_with_usage({"1": _full(), "2": _usage(50), "3": _usage(60)})
        assert outcome is TickOutcome.SWITCHED
        assert next(e for e in h.events if isinstance(e, SwitchEvent)).trigger == "at-limit"
        assert h.active_number() == 2

    def test_a_timed_last_point_is_reprobed_and_escapes_when_it_closes(self, temp_home):
        h = _harness(temp_home, threshold=99.9)
        _seen_at_98(h)
        fake = _probing({"sk-live": ("allowed_warning", 99.0)})
        usage = {"1": _full(), "2": _usage(50), "3": _usage(60)}
        h.tick_with_usage(usage)
        assert h.engine._edge.running()
        h.clock.advance(edge.EDGE_REPROBE_S / 2)
        assert h.tick_with_usage(usage) is TickOutcome.NO_ACTION
        assert fake.calls == ["sk-live"]  # not yet due
        h.clock.advance(edge.EDGE_REPROBE_S / 2)
        fake.answers["sk-live"] = ("rejected", 100.0)
        assert h.tick_with_usage(usage) is TickOutcome.SWITCHED
        assert fake.calls == ["sk-live", "sk-live"]

    def test_moves_when_the_gate_reads_100_before_any_refusal(self, temp_home):
        h = _harness(temp_home, threshold=99.9)
        _seen_at_98(h)
        fake = _probing({"sk-live": ("allowed_warning", 99.0)})
        usage = {"1": _full(), "2": _usage(50), "3": _usage(60)}
        assert h.tick_with_usage(usage) is TickOutcome.NO_ACTION
        # Still allowed, but the window now reads 100%: inside the grace.
        fake.answers["sk-live"] = ("allowed_warning", 100.0)
        h.clock.advance(edge.EDGE_REPROBE_S)
        assert h.tick_with_usage(usage) is TickOutcome.SWITCHED
        assert next(e for e in h.events if isinstance(e, SwitchEvent)).trigger == "at-limit"
        assert h.active_number() == 2

    def test_last_stretch_probes_below_100_every_few_seconds(self, temp_home):
        h = _harness(temp_home, threshold=99.9)
        fake = _probing({"sk-live": ("allowed", 97.0)})
        others = {"2": _usage(50), "3": _usage(60)}
        assert h.tick_with_usage({"1": _usage(94), **others}) is TickOutcome.NO_ACTION
        h.clock.advance(60)
        # Three points a minute, three left: a minute out, the last stretch.
        assert h.tick_with_usage({"1": _usage(97), **others}) is TickOutcome.NO_ACTION
        assert h.engine._endgame
        assert fake.calls == []  # the endpoint's 97% is not a probe trigger
        for _ in range(20):
            delay = h.engine._next_delay(TickOutcome.NO_ACTION)
            assert ENDGAME_TICK_MIN_S <= delay <= ENDGAME_TICK_MAX_S
        h.clock.advance(ENDGAME_TICK_MIN_S)
        assert h.tick_with_usage({"1": _usage(97), **others}) is TickOutcome.NO_ACTION
        assert fake.calls == ["sk-live"]

    def test_failed_reprobes_fall_back_to_the_endpoint(self, temp_home):
        h = _harness(temp_home, threshold=99.9)
        _seen_at_98(h)
        _probing({"sk-live": ("allowed_warning", 99.0)})
        usage = {"1": _full(), "2": _usage(50), "3": _usage(60)}
        h.tick_with_usage(usage)
        patch(
            "claude_swap.gate.request_gate",
            return_value=gate.GateOutcome(None, error="timeout"),
        ).start()
        h.clock.advance(gate.ACTIVE_FRESH_S + 1)
        assert h.tick_with_usage(usage) is TickOutcome.SWITCHED

    def test_no_limit_guard_while_riding(self, temp_home):
        # 0.5 estimated points burning at a point a minute would trip the
        # guard; the gate is the signal instead.
        h = _harness(temp_home, threshold=99.9)
        _seen_at_98(h)
        _probing({"sk-live": ("allowed_warning", 99.0)})
        h.tick_with_usage({"1": _full(99.0), "2": _usage(50), "3": _usage(60)})
        h.clock.advance(20)
        outcome = h.tick_with_usage({"1": _full(100.0), "2": _usage(50), "3": _usage(60)})
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 1

    def test_fresh_statusline_is_a_free_gate(self, temp_home):
        from claude_swap import live_usage

        h = _harness(temp_home, threshold=99.9)
        _seen_at_98(h)
        fake = _probing({})
        live_usage.write_reading(
            h.switcher.backup_dir, "s1",
            {"five_hour": {"used_percentage": 10}, "seven_day": {"used_percentage": 99}},
            now=h.clock.now,
        )
        with patch.object(
            h.engine, "_live_readings",
            return_value=[{"at": h.clock.now, "seven_day": {"pct": 99.0, "resets_ts": None}}],
        ):
            outcome = h.tick_with_usage({"1": _full(), "2": _usage(50), "3": _usage(60)})
        assert outcome is TickOutcome.NO_ACTION
        assert fake.calls == []

    def test_a_last_point_that_started_unseen_is_left_at_once(self, temp_home, caplog):
        # 2026-10-29 desktop: landed on an account already at 99 and rode it
        # into a refusal 68 s later. Nothing inside the last point is
        # visible, so with no 98 reading seen there is nothing to time.
        h = _harness(temp_home, threshold=99.9)
        _probing({"sk-live": ("allowed_warning", 99.0)})
        with caplog.at_level(logging.INFO, logger="claude-swap"):
            outcome = h.tick_with_usage({"1": _full(), "2": _usage(50), "3": _usage(60)})
        assert outcome is TickOutcome.SWITCHED
        assert next(e for e in h.events if isinstance(e, SwitchEvent)).trigger == "at-limit"
        assert any("start unseen" in r.getMessage() for r in caplog.records)

    def test_the_clock_leaves_before_the_point_runs_out_probing_sparingly(self, temp_home):
        h = _harness(temp_home, threshold=99.9)
        fake = _probing({"sk-live": ("allowed_warning", 99.0)})
        usage = {"1": _full(), "2": _usage(50), "3": _usage(60)}
        _seen_at_98(h, ago=1.0)
        start = h.clock.now - 1.0
        assert h.tick_with_usage(usage) is TickOutcome.NO_ACTION
        assert h.engine._edge.running()
        deadline = h.engine._edge_deadline
        # A point a minute (the default burn, 5x): 80% of it, minus margin.
        assert deadline == pytest.approx(start + 60 * edge.EDGE_SHARE - edge.EDGE_MARGIN_S)
        delay = h.engine._next_delay(TickOutcome.NO_ACTION)
        assert delay <= deadline - h.clock.now
        while h.clock.now + ENDGAME_TICK_MIN_S < deadline:
            h.clock.advance(ENDGAME_TICK_MIN_S)
            assert h.tick_with_usage(usage) is TickOutcome.NO_ACTION
        # Timed, not polled: a re-probe every EDGE_REPROBE_S, not every 3-5 s.
        assert len(fake.calls) <= 1 + (deadline - start) / edge.EDGE_REPROBE_S
        h.clock.advance(ENDGAME_TICK_MIN_S)
        assert h.tick_with_usage(usage) is TickOutcome.SWITCHED
        assert h.active_number() == 2

    def test_the_clock_never_trades_for_another_unknown_last_point(self, temp_home):
        h = _harness(temp_home, n=2, threshold=99.9)
        _probing({"sk-live": ("allowed_warning", 99.0), "sk-2": ("allowed_warning", 99.0)})
        outcome = h.tick_with_usage({"1": _full(), "2": _full()})
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 1
        assert "last-point" in _reasons(h)

    def test_dry_run_never_probes(self, temp_home):
        h = _harness(temp_home, threshold=99.9)
        h.engine = h._make_engine(dry_run=True)
        fake = _probing({"sk-live": ("allowed_warning", 99.0)})
        h.tick_with_usage({"1": _full(), "2": _usage(50), "3": _usage(60)})
        assert fake.calls == []


class TestFullCandidates:
    def test_open_full_candidate_is_a_target_when_nothing_else_is(self, temp_home, caplog):
        h = _harness(temp_home, threshold=99.9)
        fake = _probing({
            "sk-live": ("rejected", 100.0),
            "sk-2": ("rejected", 100.0),
            "sk-3": ("allowed_warning", 99.0),
        })
        with caplog.at_level(logging.INFO, logger="claude-swap"):
            outcome = h.tick_with_usage({"1": _full(), "2": _full(), "3": _full()})
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 3
        assert sorted(fake.calls) == ["sk-2", "sk-3", "sk-live"]
        assert any("Account 3: usage API says 100% but the gate is open" in r.getMessage()
                   for r in caplog.records)

    def test_ranked_behind_measured_headroom_and_not_probed_then(self, temp_home):
        h = _harness(temp_home, threshold=99.9)
        fake = _probing({"sk-live": ("rejected", 100.0), "sk-3": ("allowed_warning", 99.0)})
        outcome = h.tick_with_usage({"1": _full(), "2": _full(99.0), "3": _full()})
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2
        assert fake.calls == ["sk-live"]  # 2 has a real point; 3 needs no probe

    def test_without_probes_endpoint_100_stays_at_limit(self, temp_home):
        h = _harness(temp_home, threshold=99.9)
        _probing({
            "sk-live": ("rejected", 100.0),
            "sk-2": ("rejected", 101.0),
            "sk-3": ("rejected", 100.0),
        })
        outcome = h.tick_with_usage({"1": _full(), "2": _full(), "3": _full()})
        assert outcome is TickOutcome.BLOCKED
        assert h.active_number() == 1

    def test_probes_are_capped_per_tick_and_throttled(self, temp_home):
        h = _harness(temp_home, n=6, threshold=99.9)
        fake = _probing({
            "sk-live": ("rejected", 100.0),
            **{f"sk-{i}": ("rejected", 100.0) for i in range(2, 7)},
        })
        usage = {str(i): _full() for i in range(1, 7)}
        h.tick_with_usage(usage)
        assert len(fake.calls) == gate.MAX_PROBES_PER_TICK
        h.clock.advance(60)
        h.tick_with_usage(usage)
        # the active (blocked) is throttled; the unprobed candidates go next
        assert len(fake.calls) == 2 * gate.MAX_PROBES_PER_TICK
        assert len(set(fake.calls)) == 6
        h.clock.advance(60)
        h.tick_with_usage(usage)
        assert len(fake.calls) == 6  # everyone probed within the interval

    def test_window_resetting_within_minutes_is_not_probed(self, temp_home):
        from tests.test_autoswitch import _iso_at

        h = _harness(temp_home, threshold=99.9)
        fake = _probing({"sk-live": ("allowed_warning", 99.0)})
        soon = _iso_at(h.clock.now + 120)
        usage = {"1": {"five_hour": {"pct": 100.0, "resets_at": soon}, "seven_day": {"pct": 50.0}},
                 "2": _usage(50), "3": _usage(60)}
        h.tick_with_usage(usage)
        assert fake.calls == []


class TestRunwayRule:
    def test_limit_guard_does_not_ping_pong_between_spent_accounts(self, temp_home):
        """2026-10-27 desktop: 1 -> 14 -> 23 -> 1 every 10-60 s, each move
        "target runway ~1 min; limit projected in 0.7 min" — the guard left an
        account for one with no more runway than it had, partly on a stale
        reading of the account it had just left."""
        h = _harness(temp_home, threshold=99, strategy="consume-first")
        patch.object(
            h.switcher, "perish_inputs",
            return_value={n: {"endsAt": None, "anchorNext": None, "tier": TIERS["5x"]}
                          for n in ("1", "2", "3")},
        ).start()
        _probing({})
        # a minute of burn evidence before the active nears its limit
        readings = {"1": 85.0, "2": 97.0, "3": 96.5}
        switches = []
        for _ in range(12):
            usage = {num: _usage(pct) for num, pct in readings.items()}
            h.tick_with_usage(usage)
            new = [e for e in h.events if isinstance(e, SwitchEvent)]
            if len(new) > len(switches):
                switches = new
            # the active burns 3 points every 20 s (a burst the limit guard
            # sees coming); the others hold still
            active = str(h.active_number())
            readings[active] = min(readings[active] + 3.0, 100.0)
            h.clock.advance(20)
        moves = [(e.from_ref["number"], e.to_ref["number"], e.trigger) for e in switches]
        # one at-limit escape at most once the active really is spent; never
        # a proactive move to an account that runs out as fast
        assert all(trigger == "at-limit" for _, _, trigger in moves), moves
        assert len(moves) <= 2, moves
        assert "riding-active" in _reasons(h)

    def test_a_target_with_real_runway_is_still_taken(self, temp_home):
        h = _harness(temp_home, threshold=99, strategy="consume-first")
        patch.object(
            h.switcher, "perish_inputs",
            return_value={n: {"endsAt": None, "anchorNext": None, "tier": TIERS["5x"]}
                          for n in ("1", "2", "3")},
        ).start()
        _probing({})
        h.tick_with_usage({"1": _usage(91), "2": _usage(60), "3": _usage(97)})
        h.clock.advance(60)
        outcome = h.tick_with_usage({"1": _usage(97), "2": _usage(60), "3": _usage(97)})
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2
        assert h.engine._time_to_limit <= LIMIT_GUARD_S
