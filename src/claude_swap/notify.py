"""Email alerts for a weekly quota that can only be used for a few hours.

An account whose weekly limit resets shortly before its paid period ends
(owner example 2026-11-21: weekly back Nov 25 22:00, bills Nov 25 23:19)
gets a whole fresh week of quota for that short stretch only; whatever is
left at the period end is lost (accounts are cancelled before they renew).
Auto already drains such an account first, but one person's chats rarely
spend a week in an hour or two. These alerts give people time to line up
work:

- a heads-up ``HEADS_UP_S`` before the weekly reset, and
- a "ready" alert at the reset, while the quota is there to use.

Configuration is environment-only, so no address or key is ever stored in
source or settings (all three are required; nothing is sent otherwise):

- ``ASWAP_NOTIFY_EMAILS``: comma-separated recipients.
- ``ASWAP_NOTIFY_FROM``: the sender, on a domain verified in Sendly.
- ``ASWAP_NOTIFY_SENDLY_KEY``: a Sendly API key with ``emails:send``.

Every machine running ``aswap auto`` may send the same alert: each send
carries an ``Idempotency-Key`` derived from the account, the reset and the
alert kind, and the body only states absolute times, so Sendly delivers one
email however many machines ask (its key lives 24 hours).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Callable, Mapping

_logger = logging.getLogger(__name__)

WEEK_S = 7 * 86400.0
# A fresh week usable for at most this long before the paid period ends is
# "tight": no single person spends a 20x week in half a day.
TIGHT_WINDOW_S = 12 * 3600.0
# The heads-up goes out this long before the weekly reset (owner ask).
HEADS_UP_S = 8 * 3600.0
# How often the engine looks; alerts are about hours, not seconds.
CHECK_INTERVAL_S = 300.0
STATE_FILENAME = "notify_state.json"
SENDLY_URL = "https://api.sendly.now/api/emails"
SEND_TIMEOUT_S = 20.0
SPARKLE = "✨"


@dataclass(frozen=True)
class TightWindow:
    email: str
    reset_at: float  # the weekly reset that brings the fresh quota
    ends_at: float  # when the paid period ends and the quota is lost

    @property
    def length_s(self) -> float:
        return self.ends_at - self.reset_at


@dataclass(frozen=True)
class Config:
    recipients: tuple[str, ...]
    sender: str
    api_key: str

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Config | None":
        env = os.environ if env is None else env
        recipients = tuple(
            r.strip() for r in env.get("ASWAP_NOTIFY_EMAILS", "").split(",") if r.strip()
        )
        sender = env.get("ASWAP_NOTIFY_FROM", "").strip()
        key = env.get("ASWAP_NOTIFY_SENDLY_KEY", "").strip()
        if not recipients:
            return None
        if not sender or not key:
            _logger.warning(
                "⚠️ ASWAP_NOTIFY_EMAILS is set but ASWAP_NOTIFY_FROM or "
                "ASWAP_NOTIFY_SENDLY_KEY is missing; tight-window alerts are off"
            )
            return None
        return cls(recipients, sender, key)


def tight_windows(
    inputs: Mapping[str, dict], emails: Mapping[str, str], now: float
) -> list[TightWindow]:
    """Accounts whose last weekly reset before the period end leaves at most
    ``TIGHT_WINDOW_S`` to use it, and whose period has not ended yet.

    ``inputs`` is ``switcher.perish_inputs``: per slot ``endsAt`` and
    ``anchorNext`` (the next weekly reset from now). Once that reset has
    passed, ``anchorNext`` is the one after it, so the reset in question is
    found by stepping back a week.
    """
    out: list[TightWindow] = []
    for num, row in inputs.items():
        ends, nxt = row.get("endsAt"), row.get("anchorNext")
        email = emails.get(num)
        if ends is None or nxt is None or not email or ends <= now:
            continue
        for reset in (nxt - WEEK_S, nxt, nxt + WEEK_S):
            if 0 < ends - reset <= TIGHT_WINDOW_S:
                out.append(TightWindow(email, float(reset), float(ends)))
                break
    return out


def _clock(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().strftime("%a %b %d %H:%M %Z").strip()


def _span(seconds: float) -> str:
    minutes = int(seconds // 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"


def message(window: TightWindow, kind: str) -> tuple[str, str]:
    """``(subject, html body)``; absolute times only, so every machine
    composes the identical message for the same alert."""
    span = _span(window.length_s)
    reset, ends = _clock(window.reset_at), _clock(window.ends_at)
    if kind == "heads-up":
        subject = (
            f"{SPARKLE} {window.email}: a fresh week of Claude quota for only {span}"
        )
        lead = (
            f"In about {_span(HEADS_UP_S)}, at <b>{escape(reset)}</b>, the weekly limit "
            f"of <b>{escape(window.email)}</b> resets, and its paid period ends at "
            f"<b>{escape(ends)}</b>. That leaves <b>{span}</b> to use a whole week "
            "of quota; whatever is left then is lost. Line up work now."
        )
    else:
        subject = f"{SPARKLE} {window.email} is ready: use it hard until {ends}"
        lead = (
            f"The weekly limit of <b>{escape(window.email)}</b> reset at "
            f"<b>{escape(reset)}</b>: a whole week of quota is there to use until "
            f"<b>{escape(ends)}</b> ({span}), when its paid period ends. aswap auto "
            "is already moving work onto it; start more chats to spend it."
        )
    body = f"<p>{lead}</p><p style=\"color:#888\">aswap tight-window alert</p>"
    return subject, body


def idempotency_key(window: TightWindow, kind: str) -> str:
    raw = f"aswap|{window.email.lower()}|{int(window.reset_at)}|{kind}"
    return hashlib.sha256(raw.encode()).hexdigest()[:40]


def send_sendly(
    config: Config, subject: str, body: str, key: str, *, opener=urllib.request.urlopen
) -> None:
    """One email to every recipient. Raises on failure (caller retries)."""
    payload = json.dumps({
        "to": list(config.recipients),
        "from": config.sender,
        "name": "aswap",
        "subject": subject,
        "body": body,
    }).encode()
    request = urllib.request.Request(
        SENDLY_URL,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
            "Idempotency-Key": key,
        },
    )
    with opener(request, timeout=SEND_TIMEOUT_S) as response:
        status = getattr(response, "status", 200)
        if status >= 300:
            raise OSError(f"Sendly answered {status}")


class Notifier:
    """Looks every ``CHECK_INTERVAL_S`` and sends each due alert once."""

    def __init__(
        self,
        backup_dir: Path,
        config: Config,
        *,
        inputs: Callable[[float], Mapping[str, dict]],
        emails: Callable[[], Mapping[str, str]],
        send: Callable[[Config, str, str, str], None] = send_sendly,
        clock: Callable[[], float] = time.time,
    ):
        self.state_path = Path(backup_dir) / STATE_FILENAME
        self.config = config
        self._inputs = inputs
        self._emails = emails
        self._send = send
        self.clock = clock
        self._next_at = 0.0

    def maybe_run(self) -> int:
        """Send what is due; the number of alerts sent. Never raises."""
        now = self.clock()
        if now < self._next_at:
            return 0
        self._next_at = now + CHECK_INTERVAL_S
        try:
            return self._run(now)
        except Exception:
            _logger.warning("⚠️ tight-window alerts failed", exc_info=True)
            return 0

    def _load(self) -> dict:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, state: dict) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def _run(self, now: float) -> int:
        windows = tight_windows(self._inputs(now), self._emails(), now)
        state = self._load()
        sent = 0
        for window in windows:
            if now >= window.reset_at:
                due = ["ready"] if now < window.ends_at else []
            elif now >= window.reset_at - HEADS_UP_S:
                due = ["heads-up"]
            else:
                due = []
            for kind in due:
                key = idempotency_key(window, kind)
                if key in state:
                    continue
                subject, body = message(window, kind)
                try:
                    self._send(self.config, subject, body, key)
                except (OSError, urllib.error.URLError) as exc:
                    _logger.warning("⚠️ tight-window alert for %s not sent: %s", window.email, exc)
                    continue
                state[key] = {"email": window.email, "kind": kind, "sentAt": now}
                sent += 1
                _logger.info("%s Sent the %s alert for %s", SPARKLE, kind, window.email)
        # Forget alerts whose window is long over.
        state = {
            k: v for k, v in state.items()
            if isinstance(v, dict) and now - float(v.get("sentAt", now)) < 2 * WEEK_S
        }
        if sent or state != self._load():
            self._save(state)
        return sent
