"""The priority list (autoswitch.priority): accounts auto uses up first.

Asked 2026-10-29: pick accounts to burn through before anything else (e.g.
the 5x plans, to upgrade them to 20x), over every other rule. Only an
account's limit moves auto off a priority account, and then onto the next one
on the list with room.
"""

from __future__ import annotations

from pathlib import Path

from claude_swap import cli
from claude_swap.autoswitch import NoSwitchEvent, SwitchEvent, TickOutcome
from claude_swap.settings import load_settings, parse_priority

from tests.test_autoswitch import EngineHarness, _R_LATER, _R_LATEST, _R_SOON, _usage7


def _harness(temp_home: Path, **settings) -> EngineHarness:
    settings.setdefault("strategy", "consume-first")
    settings.setdefault("cooldown_seconds", 0)
    settings.setdefault("threshold", 99.9)
    h = EngineHarness(temp_home, **settings)
    h.seed(1, "a@example.com")
    h.seed(2, "b@example.com")
    h.seed(3, "c@example.com")
    h.make_live("a@example.com", 1)
    return h


def _current(h: EngineHarness) -> str | None:
    return h.switcher.current_account_number()


def _reasons(h: EngineHarness) -> list[str]:
    return [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]


def _switches(h: EngineHarness) -> list[SwitchEvent]:
    return [e for e in h.events if isinstance(e, SwitchEvent)]


# Account 1 resets soonest: consume-first alone would stay on it.
_SETTLED_ON_1 = {
    "1": _usage7(20, 20, _R_SOON),
    "2": _usage7(10, 10, _R_LATER),
    "3": _usage7(10, 10, _R_LATEST),
}


class TestParse:
    def test_order_kept_case_deduped_spaces_or_commas(self):
        assert parse_priority("c@x.com, 2  A@x.com,a@x.com") == ("c@x.com", "2", "A@x.com")
        assert parse_priority(None) == ()
        assert parse_priority("") == ()


class TestPriority:
    def test_moves_onto_a_priority_account_over_consume_first(self, temp_home):
        h = _harness(temp_home, priority="c@example.com")
        assert h.tick_with_usage(_SETTLED_ON_1) is TickOutcome.SWITCHED
        assert _current(h) == "3"
        assert [e.trigger for e in _switches(h)] == ["priority"]

    def test_a_slot_number_works_too(self, temp_home):
        h = _harness(temp_home, priority="3")
        assert h.tick_with_usage(_SETTLED_ON_1) is TickOutcome.SWITCHED
        assert _current(h) == "3"

    def test_stays_on_a_priority_account_until_its_limit(self, temp_home):
        h = _harness(temp_home, priority="c@example.com")
        h.make_live("c@example.com", 3)
        usage = {
            "1": _usage7(10, 10, _R_LATEST),
            "2": _usage7(10, 10, _R_SOON),  # consume-first would move here
            "3": _usage7(90, 90, _R_LATER),
        }
        assert h.tick_with_usage(usage) is TickOutcome.NO_ACTION
        assert _current(h) == "3"
        assert "priority" in _reasons(h)

    def test_overrides_the_manual_hold(self, temp_home):
        h = _harness(temp_home, priority="c@example.com")
        h.tick_with_usage({**_SETTLED_ON_1, "3": _usage7(100, 100, _R_LATEST)})
        assert _current(h) == "1"  # 3 is spent: nowhere to go
        h.make_live("b@example.com", 2)  # a hand switch starts a hold on 2
        h.events.clear()
        assert h.tick_with_usage(_SETTLED_ON_1) is TickOutcome.SWITCHED
        assert _current(h) == "3"

    def test_a_spent_priority_account_hands_over_to_the_next_one(self, temp_home):
        h = _harness(temp_home, priority="c@example.com,b@example.com")
        h.make_live("c@example.com", 3)
        usage = {
            "1": _usage7(10, 10, _R_SOON),  # the ranking alone would pick 1
            "2": _usage7(50, 50, _R_LATEST),
            "3": _usage7(100, 100, _R_LATER),
        }
        assert h.tick_with_usage(usage) is TickOutcome.SWITCHED
        assert _current(h) == "2"

    def test_a_priority_account_in_its_last_point_is_no_target(self, temp_home):
        h = _harness(temp_home, priority="c@example.com")
        usage = {**_SETTLED_ON_1, "3": _usage7(99, 99, _R_LATEST)}
        assert h.tick_with_usage(usage) is TickOutcome.NO_ACTION
        assert _current(h) == "1"
        assert not _switches(h)

    def test_unknown_entries_are_skipped(self, temp_home):
        h = _harness(temp_home, priority="gone@example.com, 9, c@example.com")
        assert h.tick_with_usage(_SETTLED_ON_1) is TickOutcome.SWITCHED
        assert _current(h) == "3"

    def test_no_list_changes_nothing(self, temp_home):
        h = _harness(temp_home)
        assert h.tick_with_usage(_SETTLED_ON_1) is TickOutcome.NO_ACTION
        assert _current(h) == "1"


class TestPriorityCommand:
    def _run(self, argv, capsys):
        code = 0
        try:
            cli._priority_command(argv)
        except SystemExit as e:
            code = e.code or 0
        out = capsys.readouterr()
        return code, out.out, out.err

    def test_set_show_clear(self, temp_home, capsys):
        h = _harness(temp_home)
        root = h.switcher.backup_dir
        code, out, _ = self._run(["3", "b@example.com", "3"], capsys)
        assert code == 0
        assert "Priority set" in out
        # Stored as emails: survives renumbering and reads the same elsewhere.
        assert load_settings(root).priority == "c@example.com,b@example.com"

        code, out, _ = self._run([], capsys)
        assert code == 0
        assert "1. Account 3" in out and "2. Account 2" in out

        code, out, _ = self._run(["--clear"], capsys)
        assert code == 0
        assert load_settings(root).priority is None

    def test_an_unknown_account_is_an_error(self, temp_home, capsys):
        _harness(temp_home)
        code, _, err = self._run(["nobody@example.com"], capsys)
        assert code == 1
        assert "nobody@example.com" in err
