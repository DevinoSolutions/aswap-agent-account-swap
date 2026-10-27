"""Live usage from Claude Code's statusline: real-time, free, per response.

Every Claude Code API response carries the account's unified rate-limit
headers (5-hour and 7-day utilization + reset), and Claude Code hands them to
the statusline command as ``rate_limits``. A statusline that writes them to
``<backup_dir>/live_usage/<session_id>.json`` gives the auto engine a reading
of the ACTIVE account that is seconds old and costs no ``/api/oauth/usage``
budget — the endpoint's ~30 requests/hour is what limits polling to minutes,
and many parallel sessions can burn an account through its threshold in less
than one poll interval.

File contract (written by the statusline command, e.g. ``~/.claude/statusline.mjs``)::

    {"at": <epoch ms written>, "session": "<id>",
     "rate_limits": {"five_hour": {"used_percentage": 37, "resets_at": 1791...},
                     "seven_day": {"used_percentage": 12, "resets_at": 1792...}}}

``resets_at`` may be epoch seconds, epoch milliseconds or ISO-8601.

Which account a reading belongs to is NOT in the file: a session idle since
before a switch keeps rewriting the previous account's numbers. The weekly
reset identifies it instead — every account's 7-day window resets at its own
fixed weekday/time (see ``weekly_anchor``), so a reading is attributed to the
account whose known weekly reset it matches, and dropped when that is not
the active account or the match is ambiguous.

Readings are merged per window, never trusted blindly: within one window
instance (same ``resets_at``) utilization only rises, so the max of stored
and live is the newest truth; a live window from an older instance (an idle
session's leftovers) is ignored, and a window whose reset has passed carries
no information.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

_logger = logging.getLogger(__name__)

LIVE_DIRNAME = "live_usage"
# A file not rewritten for this long is ignored: its session is idle, so it
# says nothing the stored poll does not.
FRESH_S = 120.0
# Files older than this are deleted on read (ended sessions).
PRUNE_S = 24 * 3600.0
# Weekly resets of two readings/accounts within this distance (mod one week)
# are the same anchor. Anchors are whole hours in practice; the tolerance
# only absorbs second-level rounding between the headers and the usage API.
MATCH_TOL_S = 300.0
# Same-instance tolerance for a window's resets_at (the 5h reset is reported
# with second precision by one source and rounded by the other).
SAME_WINDOW_TOL_S = 120.0
_WEEK_S = 7 * 86400.0
_WINDOWS = ("five_hour", "seven_day")


def live_dir(root: Path) -> Path:
    return Path(root) / LIVE_DIRNAME


def to_ts(value: object) -> float | None:
    """Epoch seconds from epoch s / ms or an ISO string; None otherwise."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return v / 1000.0 if v > 1e11 else v
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    return None


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _window(raw: object) -> dict | None:
    if not isinstance(raw, dict):
        return None
    pct = raw.get("used_percentage")
    if isinstance(pct, bool) or not isinstance(pct, (int, float)):
        return None
    return {"pct": float(pct), "resets_ts": to_ts(raw.get("resets_at"))}


def read_readings(root: Path, now: float | None = None, fresh_s: float = FRESH_S) -> list[dict]:
    """Fresh readings ``{at, session, five_hour?, seven_day?}`` (never raises)."""
    now = time.time() if now is None else now
    out: list[dict] = []
    try:
        paths = list(live_dir(root).glob("*.json"))
    except OSError:
        return out
    for path in paths:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        stamp = raw.get("at") if isinstance(raw, dict) else None
        if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
            continue
        at = stamp / 1000.0  # always epoch milliseconds (JS Date.now())
        age = now - at
        if age > PRUNE_S:
            try:
                path.unlink()
            except OSError:
                pass
            continue
        if age > fresh_s:
            continue
        limits = raw.get("rate_limits")
        if not isinstance(limits, dict):
            continue
        reading: dict = {"at": at, "session": raw.get("session")}
        for key in _WINDOWS:
            window = _window(limits.get(key))
            if window is not None:
                reading[key] = window
        if "five_hour" in reading or "seven_day" in reading:
            out.append(reading)
    return out


def _weekly_distance(a: float, b: float) -> float:
    d = abs(a - b) % _WEEK_S
    return min(d, _WEEK_S - d)


def attribute(readings: list[dict], weekly_resets: dict[str, float | None], account: str) -> list[dict]:
    """The readings that belong to ``account``, by weekly-reset match.

    ``weekly_resets`` maps every known account to a timestamp of its weekly
    reset (any occurrence). A reading counts only when its 7-day reset
    matches ``account`` and no other account; without a known weekly reset
    for ``account`` nothing can be attributed.
    """
    mine = weekly_resets.get(account)
    if mine is None:
        return []
    others = [ts for num, ts in weekly_resets.items() if num != account and ts is not None]
    out = []
    for reading in readings:
        week = (reading.get("seven_day") or {}).get("resets_ts")
        if week is None or _weekly_distance(week, mine) > MATCH_TOL_S:
            continue
        if any(_weekly_distance(week, ts) <= MATCH_TOL_S for ts in others):
            continue
        out.append(reading)
    return out


def merge(stored: dict | None, readings: list[dict], now: float) -> tuple[dict | None, bool]:
    """``stored`` usage with each live window folded in; ``(usage, changed)``.

    Per window, live wins only with information the stored value lacks: the
    max utilization within the same window instance, or a newer instance
    once the stored one has reset. Other keys (scoped, spend) are untouched.
    """
    result = dict(stored) if isinstance(stored, dict) else {}
    changed = False
    for key in _WINDOWS:
        best: dict | None = None
        for reading in readings:
            window = reading.get(key)
            if window is None:
                continue
            resets = window["resets_ts"]
            if resets is not None and resets <= now:
                continue  # that window already rolled over: stale number
            if best is None:
                best = window
                continue
            b = best["resets_ts"]
            if resets is not None and (b is None or resets > b + SAME_WINDOW_TOL_S):
                best = window  # a newer window instance
            elif (resets is None or b is None or abs(resets - b) <= SAME_WINDOW_TOL_S) and window["pct"] > best["pct"]:
                best = window
        if best is None:
            continue
        old = result.get(key) if isinstance(result.get(key), dict) else None
        old_reset = to_ts(old.get("resets_at")) if old else None
        old_pct = old.get("pct") if old else None
        live_reset = best["resets_ts"]
        if old is None or not isinstance(old_pct, (int, float)):
            take = True
        elif old_reset is None or live_reset is None:
            take = best["pct"] > old_pct
        elif abs(live_reset - old_reset) <= SAME_WINDOW_TOL_S:
            take = best["pct"] > old_pct
        elif live_reset > old_reset:
            take = old_reset <= now  # stored instance has reset; live is the new one
        else:
            take = False  # live is from an older instance
        if not take:
            continue
        window = dict(old or {})
        window["pct"] = best["pct"]
        if live_reset is not None:
            window["resets_at"] = _iso(live_reset)
            window.pop("countdown", None)
            window.pop("clock", None)
        window["live"] = True
        result[key] = window
        changed = True
    return (result or None), changed


def write_reading(root: Path, session: str, rate_limits: dict, now: float | None = None) -> Path:
    """Write one reading in the statusline contract (tests and tooling)."""
    directory = live_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session}.json"
    tmp = directory / f".{session}.tmp"
    stamp = (time.time() if now is None else now) * 1000
    tmp.write_text(json.dumps({"at": stamp, "session": session, "rate_limits": rate_limits}), encoding="utf-8")
    os.replace(tmp, path)
    return path


# How far apart a live reading and an API poll may be in time and still be
# compared (usage keeps rising in between, so wider windows mean noisier
# diffs), and the disagreement that is worth a warning.
COMPARE_WINDOW_S = 90.0
DISAGREE_PCT = 3.0


def compare(api_usage: dict | None, readings: list[dict], fetched_at: float) -> list[dict]:
    """Per-window comparison of a fresh API poll with live readings.

    Only same-instance windows (``resets_at`` within ``SAME_WINDOW_TOL_S``)
    from readings within ``COMPARE_WINDOW_S`` of the fetch count; per window
    the reading closest in time is used. ``diff`` is API minus live.
    """
    if not isinstance(api_usage, dict):
        return []
    out = []
    for key in _WINDOWS:
        api = api_usage.get(key)
        if not isinstance(api, dict) or not isinstance(api.get("pct"), (int, float)):
            continue
        api_reset = to_ts(api.get("resets_at"))
        best = None
        for reading in readings:
            window = reading.get(key)
            if window is None or abs(reading["at"] - fetched_at) > COMPARE_WINDOW_S:
                continue
            live_reset = window["resets_ts"]
            if api_reset is not None and live_reset is not None and abs(api_reset - live_reset) > SAME_WINDOW_TOL_S:
                continue
            if best is None or abs(reading["at"] - fetched_at) < abs(best[0]["at"] - fetched_at):
                best = (reading, window)
        if best is None:
            continue
        reading, window = best
        out.append({
            "window": key,
            "api": float(api["pct"]),
            "live": window["pct"],
            "diff": float(api["pct"]) - window["pct"],
            "skew_s": fetched_at - reading["at"],
        })
    return out


def same_week(readings: list[dict], api_usage: dict | None) -> list[dict]:
    """Readings whose 7-day window is the same instance as the API's."""
    seven = api_usage.get("seven_day") if isinstance(api_usage, dict) else None
    reset = to_ts(seven.get("resets_at")) if isinstance(seven, dict) else None
    if reset is None:
        return []
    return [
        r for r in readings
        if (r.get("seven_day") or {}).get("resets_ts") is not None
        and abs(r["seven_day"]["resets_ts"] - reset) <= SAME_WINDOW_TOL_S
    ]
