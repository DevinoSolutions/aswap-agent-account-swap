"""CVP accounts (cvp.py): labelled from a Mythos model, kept in reserve.

Owner asks 2026-11-15: an account offered a Mythos model is a Cyber
Verification Program account. Label it, and keep it for when it is really
needed (``autoswitch.cvpPolicy``), except when its weekly quota is about to be
lost anyway (``autoswitch.cvpReleaseHours``).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from claude_swap import cvp
from claude_swap.autoswitch import NoSwitchEvent, SwitchEvent, TickOutcome
from claude_swap.settings import SETTING_SPECS, AutoSwitchSettings

from tests.test_autoswitch import EngineHarness, _iso_at, _usage7, _R_LATER, _R_LATEST, _R_SOON


def _harness(temp_home: Path, *, cvp_email: str | None = "c@example.com", **settings) -> EngineHarness:
    settings.setdefault("cooldown_seconds", 0)
    h = EngineHarness(temp_home, **settings)
    h.seed(1, "a@example.com")
    h.seed(2, "b@example.com")
    h.seed(3, "c@example.com")
    h.make_live("a@example.com", 1)
    if cvp_email:
        h.switcher._cvp.set_label(cvp_email, "", True)
    return h


def _current(h: EngineHarness) -> str | None:
    return h.switcher.current_account_number()


def _switches(h: EngineHarness) -> list[SwitchEvent]:
    return [e for e in h.events if isinstance(e, SwitchEvent)]


def _reasons(h: EngineHarness) -> list[str]:
    return [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]


class TestStore:
    def test_label_beats_detection(self, temp_home):
        store = cvp.CvpStore(temp_home)
        store.record("A@x.com", "", ["claude-mythos-5-2"], None, now=10)
        assert cvp.row_is_cvp(store.get("a@x.com", ""))
        store.set_label("a@x.com", "", False)
        assert not cvp.row_is_cvp(store.get("a@x.com", ""))
        store.set_label("a@x.com", "", None)
        assert cvp.row_is_cvp(store.get("a@x.com", ""))

    def test_a_failed_probe_keeps_the_last_answer(self, temp_home):
        store = cvp.CvpStore(temp_home)
        store.record("a@x.com", "", ["claude-mythos-5-2"], None, now=10)
        store.record("a@x.com", "", None, "http-401", now=20)
        row = store.get("a@x.com", "")
        assert cvp.row_is_cvp(row) and row["error"] == "http-401"
        # Failed probes retry sooner than good ones.
        assert not cvp.probe_due(row, 20 + cvp.ERROR_RECHECK_S - 1)
        assert cvp.probe_due(row, 20 + cvp.ERROR_RECHECK_S)
        store.record("a@x.com", "", [], None, now=30)
        assert not cvp.row_is_cvp(store.get("a@x.com", ""))
        assert not cvp.probe_due(store.get("a@x.com", ""), 30 + cvp.RECHECK_S - 1)

    def test_released_window(self):
        assert cvp.released(100 + 3600, 100, 12)
        assert not cvp.released(100 + 13 * 3600, 100, 12)
        assert not cvp.released(100 + 3600, 100, 0)  # 0 = off
        assert not cvp.released(None, 100, 12)
        assert not cvp.released(50, 100, 12)  # already past

    def test_mythos_is_the_signal(self):
        assert cvp.is_cvp_model("claude-mythos-5-2")
        assert not cvp.is_cvp_model("claude-opus-5-5")


class TestDetection:
    def test_refresh_labels_mythos_accounts(self, temp_home):
        h = _harness(temp_home, cvp_email=None)

        def fake_probe(token):
            return (["claude-mythos-5-2"] if token == "sk-2" else []), None

        with patch.object(cvp, "probe", side_effect=fake_probe):
            probed = h.switcher.refresh_cvp(force=True)
        assert sorted(n for n, _e, _r in probed) == ["1", "2", "3"]
        assert h.switcher.cvp_accounts() == {"2"}
        # Not due again right away.
        with patch.object(cvp, "probe", side_effect=AssertionError("probed again")):
            assert h.switcher.refresh_cvp() == []

    def test_limit_caps_probes(self, temp_home):
        h = _harness(temp_home, cvp_email=None)
        with patch.object(cvp, "probe", return_value=([], None)):
            assert len(h.switcher.refresh_cvp(limit=2)) == 2
            assert len(h.switcher.refresh_cvp(limit=2)) == 1


class TestLastResort:
    def test_another_account_with_room_wins_over_a_fresher_cvp_one(self, temp_home):
        h = _harness(temp_home)
        usage = {
            "1": _usage7(100, 50, _R_LATER),
            "2": _usage7(60, 60, _R_LATER),
            "3": _usage7(0, 0, _R_LATER),  # most headroom, but CVP
        }
        assert h.tick_with_usage(usage) is TickOutcome.SWITCHED
        assert _current(h) == "2"

    def test_cvp_account_taken_when_nothing_else_can(self, temp_home):
        h = _harness(temp_home)
        usage = {
            "1": _usage7(100, 50, _R_LATER),
            "2": _usage7(100, 100, _R_LATER),
            "3": _usage7(10, 10, _R_LATER),
        }
        assert h.tick_with_usage(usage) is TickOutcome.SWITCHED
        assert _current(h) == "3"

    def test_over_the_threshold_but_not_spent_it_waits(self, temp_home):
        h = _harness(temp_home)
        usage = {
            "1": _usage7(95, 50, _R_LATER),
            "2": _usage7(100, 100, _R_LATER),
            "3": _usage7(10, 10, _R_LATER),
        }
        assert h.tick_with_usage(usage) is TickOutcome.NO_ACTION
        assert _current(h) == "1"
        assert "cvp-reserved" in _reasons(h)

    def test_consume_first_never_drains_onto_a_reserved_account(self, temp_home):
        h = _harness(temp_home, strategy="consume-first")
        usage = {
            "1": _usage7(20, 20, _R_LATEST),
            "2": _usage7(20, 20, _R_LATER),
            "3": _usage7(10, 10, _R_SOON),  # soonest reset, but CVP
        }
        assert h.tick_with_usage(usage) is TickOutcome.SWITCHED
        assert _current(h) == "2"

    def test_leaves_a_cvp_account_once_another_has_room(self, temp_home):
        h = _harness(temp_home, manual_hold=False)
        h.make_live("c@example.com", 3)
        usage = {
            "1": _usage7(100, 100, _R_LATER),
            "2": _usage7(20, 20, _R_LATER),
            "3": _usage7(30, 30, _R_LATER),
        }
        assert h.tick_with_usage(usage) is TickOutcome.SWITCHED
        assert _current(h) == "2"
        assert [e.trigger for e in _switches(h)] == ["cvp-exit"]

    def test_stays_on_a_cvp_account_while_nothing_else_has_room(self, temp_home):
        h = _harness(temp_home, manual_hold=False)
        h.make_live("c@example.com", 3)
        usage = {
            "1": _usage7(100, 100, _R_LATER),
            "2": _usage7(100, 100, _R_LATER),
            "3": _usage7(30, 30, _R_LATER),
        }
        assert h.tick_with_usage(usage) is TickOutcome.NO_ACTION
        assert _current(h) == "3"

    def test_kept_out_of_the_priority_list(self, temp_home):
        h = _harness(temp_home, priority="c@example.com", strategy="consume-first")
        usage = {
            "1": _usage7(20, 20, _R_SOON),
            "2": _usage7(10, 10, _R_LATER),
            "3": _usage7(10, 10, _R_LATEST),
        }
        assert h.tick_with_usage(usage) is TickOutcome.NO_ACTION
        assert _current(h) == "1"


class TestRelease:
    def _usage(self, h: EngineHarness, reset3: str) -> dict:
        return {
            "1": _usage7(100, 50, _R_LATER),
            "2": _usage7(60, 60, _R_LATER),
            "3": _usage7(10, 10, reset3),
        }

    def test_quota_about_to_be_lost_is_used_normally(self, temp_home):
        h = _harness(temp_home)
        soon = _iso_at(h.clock.now + 6 * 3600)
        assert h.tick_with_usage(self._usage(h, soon)) is TickOutcome.SWITCHED
        assert _current(h) == "3"  # most headroom, and it would be wasted

    def test_release_off(self, temp_home):
        h = _harness(temp_home, cvp_release_hours=0)
        soon = _iso_at(h.clock.now + 6 * 3600)
        assert h.tick_with_usage(self._usage(h, soon)) is TickOutcome.SWITCHED
        assert _current(h) == "2"

    def test_outside_the_window_stays_reserved(self, temp_home):
        h = _harness(temp_home, cvp_release_hours=4)
        soon = _iso_at(h.clock.now + 6 * 3600)
        assert h.tick_with_usage(self._usage(h, soon)) is TickOutcome.SWITCHED
        assert _current(h) == "2"


class TestOtherPolicies:
    def test_never(self, temp_home):
        h = _harness(temp_home, cvp_policy="never")
        usage = {
            "1": _usage7(100, 50, _R_LATER),
            "2": _usage7(100, 100, _R_LATER),
            "3": _usage7(10, 10, _R_LATER),
        }
        assert h.tick_with_usage(usage) is not TickOutcome.SWITCHED
        assert _current(h) == "1"

    def test_never_ignores_the_release_window(self, temp_home):
        h = _harness(temp_home, cvp_policy="never")
        usage = {
            "1": _usage7(100, 50, _R_LATER),
            "2": _usage7(60, 60, _R_LATER),
            "3": _usage7(10, 10, _iso_at(h.clock.now + 3600)),
        }
        assert h.tick_with_usage(usage) is TickOutcome.SWITCHED
        assert _current(h) == "2"

    def test_normal(self, temp_home):
        h = _harness(temp_home, cvp_policy="normal")
        usage = {
            "1": _usage7(100, 50, _R_LATER),
            "2": _usage7(60, 60, _R_LATER),
            "3": _usage7(0, 0, _R_LATER),
        }
        assert h.tick_with_usage(usage) is TickOutcome.SWITCHED
        assert _current(h) == "3"


class TestManualStrategies:
    def test_best_skips_a_reserved_account(self, temp_home):
        h = _harness(temp_home)
        usage = {"1": _usage7(80, 80), "2": _usage7(50, 50), "3": _usage7(0, 0)}
        assert h.switcher._select_best_switchable("1", (), usage) == ("2", "")

    def test_best_falls_back_when_spent(self, temp_home):
        h = _harness(temp_home)
        usage = {"1": _usage7(100, 100), "2": _usage7(100, 100), "3": _usage7(10, 10)}
        assert h.switcher._select_best_switchable("1", (), usage) == ("3", "")


class TestSettings:
    def test_defaults_and_specs(self):
        s = AutoSwitchSettings()
        assert s.cvp_policy == "lastResort" and s.cvp_release_hours == 12.0
        assert SETTING_SPECS["autoswitch.cvpPolicy"].choices == cvp.POLICIES
        assert SETTING_SPECS["autoswitch.cvpReleaseHours"].kind == "float"
