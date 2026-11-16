"""Cyber Verification Program (CVP) accounts: detect, label, and keep in reserve.

Anthropic publishes no API that says an account is in the CVP. The signal we
use instead (owner decision 2026-11-15): an account whose Claude Code OAuth
token is offered a Mythos model on ``GET /v1/models`` has been through
Anthropic's cyber vetting (CVP / Project Glasswing), so it is a CVP account.
The test only works one way. A missing Mythos model does not prove the account
is outside the program, so ``aswap cvp set`` can label an account by hand.

CVP accounts are scarce, so auto keeps them in reserve
(``autoswitch.cvpPolicy``):

- ``lastResort`` (default): used only when no other account can take the
  work, and left again as soon as one can.
- ``never``: never chosen automatically.
- ``normal``: treated like any other account.

A reserved account counts as a normal candidate while its weekly quota is
about to be lost: its weekly reset or plan end is within
``autoswitch.cvpReleaseHours`` (default 12, 0 = off). Unused quota there would
be wasted anyway.

Detection reads only stored access tokens and never refreshes one: the usage
poller keeps them fresh. A failed probe keeps the last answer.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from pathlib import Path

from claude_swap import endpoints, statedb
from claude_swap.locking import FileLock
from claude_swap.settings import atomic_write_json

_logger = logging.getLogger("claude-swap")

FILE_NAME = "cvp_accounts.json"
CVP_MARK = "🛡️"
# A detected answer is re-checked this often; a failed probe sooner.
RECHECK_S = 6 * 3600.0
ERROR_RECHECK_S = 3600.0
# At most this many probes per auto tick, so a tick never stalls on them.
PROBES_PER_TICK = 2
_PROBE_TIMEOUT_S = 10.0
_OAUTH_BETA = "oauth-2025-04-20"

POLICIES = ("lastResort", "never", "normal")


def is_cvp_model(model_id: str) -> bool:
    return "mythos" in model_id.lower()


def list_models(access_token: str, timeout: float = _PROBE_TIMEOUT_S) -> list[str]:
    """Model ids the token may use. Raises ``urllib.error.HTTPError`` etc."""
    req = urllib.request.Request(
        endpoints.api_base() + "/v1/models?limit=1000",
        headers={
            "Authorization": f"Bearer {access_token}",
            "anthropic-version": "2023-06-01",
            "anthropic-beta": _OAUTH_BETA,
            "User-Agent": "claude-swap/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode())
    return [m["id"] for m in data.get("data", []) if isinstance(m, dict) and isinstance(m.get("id"), str)]


def probe(access_token: str) -> tuple[list[str] | None, str | None]:
    """``(mythos model ids, None)``, or ``(None, error kind)``."""
    try:
        ids = list_models(access_token)
    except urllib.error.HTTPError as e:
        return None, f"http-{e.code}"
    except (urllib.error.URLError, TimeoutError, OSError):
        return None, "network"
    except (ValueError, KeyError):
        return None, "bad-response"
    return [i for i in ids if is_cvp_model(i)], None


def _key(email: str, org: str | None) -> str:
    return f"{email.lower()}|{org or ''}"


class CvpStore:
    """``<root>/cvp_accounts.json``: identity → detection + manual label."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.path = self.root / FILE_NAME

    def _lock(self) -> FileLock:
        return FileLock(self.root / ".cvp_accounts.lock")

    def rows(self) -> dict[str, dict]:
        try:
            raw = json.loads(statedb.read_text(self.path))
        except (OSError, ValueError, UnicodeDecodeError):
            return {}
        rows = raw.get("accounts") if isinstance(raw, dict) else None
        if not isinstance(rows, dict):
            return {}
        return {k: v for k, v in rows.items() if isinstance(v, dict)}

    def _save(self, rows: dict[str, dict]) -> None:
        atomic_write_json(self.path, {"version": 1, "accounts": rows})

    def get(self, email: str, org: str | None) -> dict:
        return self.rows().get(_key(email, org)) or {}

    def record(
        self,
        email: str,
        org: str | None,
        models: list[str] | None,
        error: str | None,
        now: float | None = None,
    ) -> None:
        """Store a probe result. A failed probe keeps the last answer."""
        now = time.time() if now is None else now
        with self._lock():
            rows = self.rows()
            row = rows.setdefault(_key(email, org), {"email": email, "organizationUuid": org or ""})
            row["checkedAt"] = now
            if error is None:
                row["detected"] = bool(models)
                row["models"] = list(models or [])
                row.pop("error", None)
            else:
                row["error"] = error
            self._save(rows)

    def set_label(self, email: str, org: str | None, value: bool | None) -> None:
        """Label by hand (True/False), or None to go back to detection."""
        with self._lock():
            rows = self.rows()
            row = rows.setdefault(_key(email, org), {"email": email, "organizationUuid": org or ""})
            if value is None:
                row.pop("label", None)
            else:
                row["label"] = bool(value)
            self._save(rows)


def row_is_cvp(row: dict | None) -> bool:
    """A hand-set label wins; otherwise the last detection."""
    if not row:
        return False
    if isinstance(row.get("label"), bool):
        return row["label"]
    return row.get("detected") is True


def probe_due(row: dict | None, now: float) -> bool:
    if not row or not isinstance(row.get("checkedAt"), (int, float)):
        return True
    wait = ERROR_RECHECK_S if row.get("error") else RECHECK_S
    return now - row["checkedAt"] >= wait


def released(perish_ts: float | None, now: float, release_hours: float) -> bool:
    """Whether a reserved account's weekly quota is about to be lost, so it
    is used like any other account (``cvpReleaseHours``; 0 = never)."""
    if release_hours <= 0 or perish_ts is None:
        return False
    return 0 < perish_ts - now <= release_hours * 3600.0
