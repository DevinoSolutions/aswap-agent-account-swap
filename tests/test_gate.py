"""The rate-limit gate probe (gate.py): parsing, verdicts, throttling, store."""

from __future__ import annotations

import io
import json
import urllib.error
from email.message import Message
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import gate

NOW = 1_800_000_000.0


def _iso(ts: float) -> str:
    return gate._iso(ts)


def _headers(status="allowed_warning", h5=0.40, d7=0.99, s5="allowed", s7="allowed_warning",
             r5=NOW + 3600, r7=NOW + 3 * 86400) -> dict:
    return {
        "anthropic-ratelimit-unified-status": status,
        "anthropic-ratelimit-unified-5h-status": s5,
        "anthropic-ratelimit-unified-7d-status": s7,
        "anthropic-ratelimit-unified-5h-utilization": f"{h5:.2f}",
        "anthropic-ratelimit-unified-7d-utilization": f"{d7:.2f}",
        "anthropic-ratelimit-unified-5h-reset": str(int(r5)),
        "anthropic-ratelimit-unified-7d-reset": str(int(r7)),
        "anthropic-ratelimit-unified-representative-claim": "seven_day",
    }


def _usage(d7: float, h5: float = 40.0, d7_reset: float = NOW + 3 * 86400) -> dict:
    return {
        "five_hour": {"pct": h5, "resets_at": _iso(NOW + 3600)},
        "seven_day": {"pct": d7, "resets_at": _iso(d7_reset)},
    }


def _record(status="allowed_warning", d7=99.0, s7="allowed_warning", at=NOW, attempted=None) -> dict:
    return {
        "email": "a@example.com",
        "organizationUuid": "",
        "attemptedAt": at if attempted is None else attempted,
        "gate": {
            "status": status,
            "probedAt": at,
            "five_hour": {"pct": 40.0, "status": "allowed", "resets_at": _iso(NOW + 3600)},
            "seven_day": {"pct": d7, "status": s7, "resets_at": _iso(NOW + 3 * 86400)},
        },
    }


class TestParseHeaders:
    def test_reads_status_windows_and_resets(self):
        g = gate.parse_headers(_headers(), NOW)
        assert g["status"] == "allowed_warning"
        assert g["seven_day"] == {
            "pct": 99.0, "status": "allowed_warning", "resets_at": _iso(NOW + 3 * 86400),
        }
        assert g["five_hour"]["pct"] == 40.0
        assert g["probedAt"] == NOW
        assert g["claim"] == "seven_day"

    def test_utilization_over_one_is_kept(self):
        g = gate.parse_headers(_headers(status="rejected", d7=1.04, s7="rejected"), NOW)
        assert g["seven_day"]["pct"] == 104.0

    def test_no_unified_status_is_no_gate(self):
        assert gate.parse_headers({"content-type": "application/json"}, NOW) is None
        assert gate.parse_headers(None, NOW) is None


def _http_headers(values: dict) -> Message:
    msg = Message()
    for k, v in values.items():
        msg[k] = v
    return msg


class TestRequestGate:
    def test_ok_answer(self):
        class Resp(io.BytesIO):
            headers = _http_headers(_headers())

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with patch("urllib.request.urlopen", return_value=Resp(b"{}")) as op:
            out = gate.request_gate("tok", NOW)
        assert out.error is None and out.gate["status"] == "allowed_warning"
        req = op.call_args[0][0]
        body = json.loads(req.data)
        assert body["max_tokens"] == 1 and body["model"] == gate.PROBE_MODEL
        assert req.get_header("Anthropic-beta") == "oauth-2025-04-20"

    def test_429_with_headers_is_data_not_an_error(self):
        err = urllib.error.HTTPError(
            gate.GATE_URL, 429, "Too Many Requests",
            _http_headers(_headers(status="rejected", d7=1.0, s7="rejected")), None,
        )
        with patch("urllib.request.urlopen", side_effect=err):
            out = gate.request_gate("tok", NOW)
        assert out.error is None
        assert out.gate["status"] == "rejected"

    def test_429_without_headers_is_an_error(self):
        err = urllib.error.HTTPError(gate.GATE_URL, 429, "Too Many", _http_headers({}), None)
        with patch("urllib.request.urlopen", side_effect=err):
            out = gate.request_gate("tok", NOW)
        assert out == gate.GateOutcome(None, error="http-429")

    def test_network_failure(self):
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")):
            out = gate.request_gate("tok", NOW)
        assert out.gate is None and out.error == "network"


class TestApply:
    def test_open_gate_lifts_an_endpoint_100(self):
        h, verdict = gate.apply(_usage(100.0), _record(), (), NOW)
        assert (h, verdict) == (gate.OPEN_HEADROOM, gate.OPEN)

    def test_rejected_gate_is_at_limit_even_below_100(self):
        rec = _record(status="rejected", d7=100.0, s7="rejected")
        assert gate.apply(_usage(99.0), rec, (), NOW) == (0.0, gate.BLOCKED)

    def test_stale_gate_changes_nothing(self):
        rec = _record(at=NOW - gate.FRESH_S - 1)
        assert gate.apply(_usage(100.0), rec, (), NOW) == (0.0, None)

    def test_active_freshness_is_tighter(self):
        rec = _record(at=NOW - gate.ACTIVE_FRESH_S - 1)
        assert gate.apply(_usage(100.0), rec, (), NOW, gate.ACTIVE_FRESH_S) == (0.0, None)

    def test_below_100_is_untouched_by_an_open_gate(self):
        assert gate.apply(_usage(97.0), _record(d7=96.0), (), NOW) == (3.0, None)

    def test_a_full_model_window_is_not_the_gates_to_open(self):
        usage = _usage(100.0)
        usage["scoped"] = [{"name": "Fable", "pct": 100.0}]
        assert gate.apply(usage, _record(), ("Fable",), NOW) == (0.0, None)

    def test_rejected_window_that_has_since_reset_is_stale(self):
        rec = _record(status="rejected", s7="rejected")
        rec["gate"]["seven_day"]["resets_at"] = _iso(NOW - 10)
        assert gate.apply(_usage(5.0), rec, (), NOW) == (60.0, None)


class TestDue:
    def test_endpoint_100_with_no_probe_is_due(self):
        assert gate.due(_usage(100.0), None, (), NOW)

    def test_99_is_never_probed(self):
        # The gate cannot change a reading that is already usable.
        assert not gate.due(_usage(99.0), None, (), NOW)

    def test_throttled_by_the_last_attempt(self):
        rec = _record(attempted=NOW - gate.PROBE_INTERVAL_S + 5)
        assert not gate.due(_usage(100.0), rec, (), NOW)
        assert gate.due(_usage(100.0), rec, (), NOW + 10)

    def test_ride_interval(self):
        rec = _record(attempted=NOW - gate.RIDE_REPROBE_S)
        assert gate.due(_usage(100.0), rec, (), NOW, gate.RIDE_REPROBE_S)

    def test_window_resetting_soon_is_skipped(self):
        assert not gate.due(_usage(100.0, d7_reset=NOW + 120), None, (), NOW)


class TestFromLive:
    def test_open_and_closed(self):
        reading = {"at": NOW, "seven_day": {"pct": 99.0, "resets_ts": NOW + 86400}}
        rec = gate.from_live([reading])
        assert rec["gate"]["status"] == "allowed"
        assert gate.apply(_usage(100.0), rec, (), NOW) == (gate.OPEN_HEADROOM, gate.OPEN)
        reading["seven_day"]["pct"] = 100.0
        rec = gate.from_live([reading])
        assert gate.apply(_usage(100.0), rec, (), NOW) == (0.0, gate.BLOCKED)

    def test_newest_reading_wins(self):
        old = {"at": NOW - 60, "seven_day": {"pct": 100.0, "resets_ts": None}}
        new = {"at": NOW, "seven_day": {"pct": 99.0, "resets_ts": None}}
        assert gate.from_live([old, new])["gate"]["status"] == "allowed"
        assert gate.from_live([]) is None


class TestDisplay:
    def test_annotates_full_windows_only(self):
        out = gate.annotate(_usage(100.0), _record(), NOW)
        assert out["seven_day"]["gate"]["status"] == gate.OPEN
        assert "gate" not in out["five_hour"]
        assert gate.window_note(out["seven_day"]) == "open, gate 99%"

    def test_blocked(self):
        out = gate.annotate(_usage(100.0), _record(status="rejected", d7=100.0, s7="rejected"), NOW)
        assert gate.window_note(out["seven_day"]) == "blocked"

    def test_stale_is_not_shown(self):
        usage = _usage(100.0)
        assert gate.annotate(usage, _record(at=NOW - gate.FRESH_S - 1), NOW) is usage

    def test_json(self):
        note = {"status": "open", "pct": 99.0, "probedAt": NOW}
        assert gate.gate_to_json(note) == {"status": "open", "pct": 99.0, "probedAt": _iso(NOW)}


class TestSurfaces:
    """`aswap list`, the TUI and the JSON output show the gate when known."""

    def _shown(self, status="allowed_warning", d7=99.0, s7="allowed_warning"):
        return gate.annotate(_usage(100.0), _record(status=status, d7=d7, s7=s7), NOW)

    def test_list_lines(self):
        from claude_swap.switcher import _format_usage_lines

        with patch("time.time", return_value=NOW):
            open_lines = _format_usage_lines(self._shown())
            blocked = _format_usage_lines(
                self._shown(status="rejected", d7=100.0, s7="rejected")
            )
        assert any(line.startswith("7d: 100% (open, gate 99%)") for line in open_lines)
        assert any(line.startswith("7d: 100% (blocked)") for line in blocked)
        assert not any("gate" in line for line in open_lines if line.startswith("5h"))

    def test_json(self):
        from claude_swap.json_output import usage_to_json

        out = usage_to_json(self._shown())
        assert out["sevenDay"]["gate"] == {
            "status": "open", "pct": 99.0, "probedAt": _iso(NOW),
        }
        assert "gate" not in out["fiveHour"]

    def test_tui_rows_and_card(self):
        from claude_swap.tui.widgets import usage_rows

        rows = {label: suffix for label, _pct, suffix, _full in usage_rows(self._shown(), NOW)}
        assert rows["7d"].startswith("(open, gate 99%)")
        blocked = usage_rows(self._shown(status="rejected", d7=100.0, s7="rejected"), NOW)
        assert {r[0]: r[2] for r in blocked}["7d"].startswith("(blocked)")

    def test_collected_entries_carry_the_gate(self, temp_home):
        from tests.test_autoswitch import EngineHarness

        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.make_live("a@example.com", 1)
        clock_now = h.clock.now
        h.switcher._gate_store.record(
            {"1": gate.GateOutcome({
                "status": "allowed_warning", "probedAt": clock_now,
                "seven_day": {"pct": 99.0, "status": "allowed_warning"},
            })},
            {"1": ("a@example.com", "")}, clock_now,
        )
        stored = {"five_hour": {"pct": 10.0}, "seven_day": {"pct": 100.0}}
        from claude_swap.usage_store import UsageEntry

        with patch.object(
            h.switcher._usage_store, "entries",
            return_value={"1": UsageEntry(last_good=stored, fetched_at=clock_now, age_s=0.0)},
        ):
            entries = h.switcher.usage_entries_by_account(fetch=set())
        assert entries["1"].last_good["seven_day"]["gate"]["status"] == "open"
        assert "gate" not in stored["seven_day"]  # the stored reading is untouched


class TestStore:
    def test_round_trip_and_identity_guard(self, tmp_path: Path):
        store = gate.GateStore(tmp_path)
        ident = {"21": ("a@example.com", "")}
        out = gate.GateOutcome(gate.parse_headers(_headers(), NOW))
        store.record({"21": out}, ident, NOW)
        assert store.records(ident)["21"]["gate"]["status"] == "allowed_warning"
        assert store.records({"21": ("other@example.com", "")}) == {}

    def test_failure_keeps_the_last_answer(self, tmp_path: Path):
        store = gate.GateStore(tmp_path)
        ident = {"21": ("a@example.com", "")}
        store.record({"21": gate.GateOutcome(gate.parse_headers(_headers(), NOW))}, ident, NOW)
        store.record({"21": gate.GateOutcome(None, error="timeout")}, ident, NOW + 100)
        row = store.records(ident)["21"]
        assert row["attemptedAt"] == NOW + 100 and row["error"] == "timeout"
        assert row["gate"]["probedAt"] == NOW


@pytest.mark.parametrize("token_text", ["sk-ant-oat01-SECRET"])
def test_probe_failure_never_carries_the_token(token_text, caplog):
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError(token_text)):
        out = gate.request_gate(token_text, NOW)
    assert token_text not in (out.error or "")
    assert token_text not in caplog.text
