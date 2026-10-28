"""claude.ai web sessions: exact billing data the OAuth API does not expose.

``/api/oauth/profile`` has no renewal date and no "cancels at period end"
flag. ``claude.ai/api/organizations/{org}/subscription_details`` has both,
but refuses OAuth tokens (``oauth_token_not_accepted``); it is served to a
signed-in claude.ai session (the ``sessionKey`` cookie).

Capture: aswap launches the user's real Chrome (or Edge) with a dedicated
per-account profile and a DevTools port, the user signs in normally — Google
sign-in included, which refuses automation-flagged browsers — and aswap
reads the cookie over CDP. The profile persists, so re-capturing an expired
session is usually just reopening the window.

Session keys are as sensitive as OAuth tokens: stored 0600 beside them,
never logged, never printed.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from claude_swap import endpoints, statedb
from claude_swap.locking import FileLock
from claude_swap.settings import atomic_write_json

_logger = logging.getLogger("claude-swap")

Identity = tuple[str, str]

FILE_NAME = "billing_sessions.json"
CLAUDE = "https://claude.ai"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)


class BillingSessionError(Exception):
    """Base for claude.ai session problems."""


class SessionExpired(BillingSessionError):
    """The session key no longer authenticates (signed out / rotated)."""


class SessionBlocked(BillingSessionError):
    """A transient refusal (Cloudflare challenge, 429): retry later."""


class CaptureError(BillingSessionError):
    """The browser capture could not run or finish."""


def _key(identity: Identity) -> str:
    return f"{identity[0]}|{identity[1] or ''}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- store -------------------------------------------------------------------


class SessionStore:
    """``<root>/billing_sessions.json``: identity → claude.ai session."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.path = self.root / FILE_NAME

    def _lock(self) -> FileLock:
        return FileLock(self.root / ".billing_sessions.lock")

    def rows(self) -> dict[str, dict]:
        try:
            raw = json.loads(statedb.read_text(self.path))
        except (OSError, ValueError, UnicodeDecodeError):
            return {}
        rows = raw.get("sessions") if isinstance(raw, dict) else None
        if not isinstance(rows, dict):
            return {}
        return {
            k: v
            for k, v in rows.items()
            if isinstance(v, dict) and isinstance(v.get("sessionKey"), str)
        }

    def _save(self, rows: dict[str, dict]) -> None:
        atomic_write_json(self.path, {"version": 1, "sessions": rows})

    def get(self, identity: Identity) -> dict | None:
        return self.rows().get(_key(identity))

    def put(self, identity: Identity, session_key: str) -> None:
        with self._lock():
            rows = self.rows()
            now = _now_iso()
            rows[_key(identity)] = {
                "email": identity[0],
                "organizationUuid": identity[1] or "",
                "sessionKey": session_key,
                "capturedAt": now,
                "lastOkAt": now,
                "status": "ok",
            }
            self._save(rows)

    def mark(self, identity: Identity, status: str) -> None:
        with self._lock():
            rows = self.rows()
            row = rows.get(_key(identity))
            if row is None:
                return
            if status == "ok":
                row["lastOkAt"] = _now_iso()
            elif row.get("status") == status:
                return
            row["status"] = status
            self._save(rows)

    def forget(self, identity: Identity) -> bool:
        with self._lock():
            rows = self.rows()
            if _key(identity) not in rows:
                return False
            del rows[_key(identity)]
            self._save(rows)
            return True

    def merge_rows(self, incoming: object) -> int:
        """Merge another machine's sessions; the newer capture wins."""
        if not isinstance(incoming, dict):
            return 0
        changed = 0
        with self._lock():
            rows = self.rows()
            for key, theirs in incoming.items():
                if not (
                    isinstance(key, str)
                    and isinstance(theirs, dict)
                    and isinstance(theirs.get("sessionKey"), str)
                    and isinstance(theirs.get("email"), str)
                ):
                    continue
                mine = rows.get(key)
                if mine is None or (theirs.get("capturedAt") or "") > (
                    mine.get("capturedAt") or ""
                ):
                    if mine != theirs:
                        rows[key] = dict(theirs)
                        changed += 1
            if changed:
                self._save(rows)
        return changed


# --- claude.ai HTTP ----------------------------------------------------------
#
# Requests go through the system ``curl``: Cloudflare in front of claude.ai
# answers Python's own TLS stack with a 403 challenge while curl with the
# same session key gets the data (measured 2026-10-18, Windows curl.exe and
# MinGW curl both 200, urllib 403). The session key is passed in a curl
# config read from stdin, never on the command line. Connects to claude.ai
# take 3-9 s on this network, hence the generous timeout.

_STATUS_MARK = "\n__aswap_status__:"


def _curl() -> str | None:
    if sys.platform == "win32":
        system = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "curl.exe"
        if system.is_file():
            return str(system)
    return shutil.which("curl")


def _curl_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _http_get(url: str, headers: dict[str, str], timeout: float) -> tuple[int, str]:
    curl = _curl()
    if curl is None:
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, OSError) as e:
            raise BillingSessionError(f"claude.ai unreachable: {e}") from None
    config = "".join(f"header = {_curl_quote(f'{k}: {v}')}\n" for k, v in headers.items())
    config += f"url = {_curl_quote(url)}\n"
    cmd = [curl, "-s", "-K", "-", "--max-time", str(int(timeout)), "--retry", "1",
           "-w", _STATUS_MARK.replace("\n", "\\n") + "%{http_code}"]
    try:
        done = subprocess.run(
            cmd, input=config.encode("utf-8"), capture_output=True, timeout=timeout * 2 + 5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise BillingSessionError(f"claude.ai request failed: {e}") from None
    out = done.stdout.decode("utf-8", errors="replace")
    body, _, code = out.rpartition(_STATUS_MARK)
    try:
        status = int(code.strip())
    except ValueError:
        status = 0
    if status == 0:
        raise BillingSessionError(f"claude.ai unreachable (curl exit {done.returncode})")
    return status, body


def _request(path: str, session_key: str, timeout: float = 40.0) -> object:
    base = endpoints.claude_ai_base()
    status, body = _http_get(
        base + path,
        {
            "Cookie": f"sessionKey={session_key}",
            "User-Agent": _UA,
            "Accept": "application/json",
            "Origin": base,
            "Referer": base + "/settings/billing",
            "anthropic-client-platform": "web_claude_ai",
        },
        timeout,
    )
    if status >= 400:
        if status == 429 or "cf-chl" in body or "Just a moment" in body:
            raise SessionBlocked(f"claude.ai refused ({status}); retry later")
        if status == 404:
            raise FileNotFoundError(path)
        if status in (401, 403):
            raise SessionExpired(f"claude.ai session rejected ({status})")
        raise BillingSessionError(f"claude.ai HTTP {status}")
    try:
        return json.loads(body)
    except ValueError:
        # An HTML page on a 200 is the login redirect.
        raise SessionExpired("claude.ai returned a page, not data") from None


def fetch_organizations(session_key: str) -> list[dict]:
    data = _request("/api/organizations", session_key)
    return [o for o in data if isinstance(o, dict)] if isinstance(data, list) else []


def fetch_account_email(session_key: str) -> str | None:
    """The signed-in account's email, when the endpoint says (best effort)."""
    try:
        data = _request("/api/account", session_key)
    except (BillingSessionError, FileNotFoundError):
        return None
    if isinstance(data, dict):
        for key in ("email_address", "email"):
            if isinstance(data.get(key), str):
                return data[key]
    return None


def fetch_subscription_details(session_key: str, org_uuid: str) -> dict:
    """Parsed subscription details; ``status: "none"`` when there is none."""
    try:
        data = _request(f"/api/organizations/{org_uuid}/subscription_details", session_key)
    except FileNotFoundError:
        return {"periodEnd": None, "cancelAtPeriodEnd": None, "status": "none", "interval": None, "keys": []}
    return parse_subscription_details(data)


# Measured 2026-10-18 (an active monthly Max plan): next_charge_at (exact
# instant) + next_charge_date, status "active", billing_interval "monthly",
# plan_ending_at / plan_ending_before null, scheduled_downgrade null. A
# cancelled-but-running plan sets plan_ending_at (the end).
_PERIOD_END_KEYS = (
    "next_charge_at",
    "next_charge_date",
    "current_period_end",
    "next_billing_date",
    "renews_at",
    "renewal_date",
    "period_end",
    "ends_at",
)
_CANCEL_FLAG_KEYS = ("cancel_at_period_end", "will_cancel", "is_canceling", "is_cancelling")
_CANCEL_TS_KEYS = ("cancel_at", "canceled_at", "cancelled_at", "cancels_at")
_ENDING_KEYS = ("plan_ending_at", "plan_ending_before")
_STATUS_KEYS = ("status", "subscription_status")
_INTERVAL_KEYS = ("billing_interval", "interval", "billing_period")


def _to_iso(value: object) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 1e11 else value
        return datetime.fromtimestamp(seconds, timezone.utc).isoformat()
    if isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    return None


def _scopes(payload: dict) -> list[dict]:
    """The payload, then any nested dicts one level down (``subscription``…)."""
    return [payload] + [v for v in payload.values() if isinstance(v, dict)]


def _first(payload: dict, keys: tuple[str, ...]) -> object:
    for scope in _scopes(payload):
        for key in keys:
            if scope.get(key) is not None:
                return scope[key]
    return None


def parse_subscription_details(payload: object) -> dict:
    """Tolerant parse of ``subscription_details``.

    The schema is not documented; this looks for the usual Stripe-shaped
    names at the top level and one level down, and records the key names
    (never values) so ``aswap billing status --keys`` can show what the
    endpoint actually sent.
    """
    out = {
        "periodEnd": None, "cancelAtPeriodEnd": None, "status": None, "interval": None,
        "scheduledDowngrade": None, "keys": [],
    }
    if not isinstance(payload, dict):
        return out
    out["keys"] = sorted(payload.keys())
    if payload.get("scheduled_downgrade"):
        out["scheduledDowngrade"] = payload["scheduled_downgrade"]
    ending = _to_iso(_first(payload, _ENDING_KEYS))
    if ending:
        # Set to end: that instant is the end of paid time, not a renewal.
        status = _first(payload, _STATUS_KEYS)
        out.update(periodEnd=ending, cancelAtPeriodEnd=True, status=status if isinstance(status, str) else None)
        interval = _first(payload, _INTERVAL_KEYS)
        out["interval"] = interval if isinstance(interval, str) else None
        return out
    out["periodEnd"] = _to_iso(_first(payload, _PERIOD_END_KEYS))
    status = _first(payload, _STATUS_KEYS)
    out["status"] = status if isinstance(status, str) else None
    interval = _first(payload, _INTERVAL_KEYS)
    out["interval"] = interval if isinstance(interval, str) else None
    flag = _first(payload, _CANCEL_FLAG_KEYS)
    if isinstance(flag, bool):
        out["cancelAtPeriodEnd"] = flag
    elif _first(payload, _CANCEL_TS_KEYS) is not None and out["status"] in (None, "active", "trialing"):
        out["cancelAtPeriodEnd"] = True
    elif out["status"] in ("canceling", "cancelling", "pending_cancellation"):
        out["cancelAtPeriodEnd"] = True
    else:
        out["cancelAtPeriodEnd"] = False if out["periodEnd"] else None
    return out


# --- browser capture ---------------------------------------------------------


def profile_dir(root: Path, email: str) -> Path:
    return Path(root) / "browser" / re.sub(r"[^a-z0-9._-]", "_", email.lower())


def _registry_app_path(exe: str) -> str | None:
    if sys.platform != "win32":
        return None
    import winreg

    sub = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, sub) as key:
                value, _ = winreg.QueryValueEx(key, "")
                if value and Path(value).is_file():
                    return value
        except OSError:
            continue
    return None


def find_chrome(explicit: str | None = None) -> Path:
    """Chrome (or Edge) to launch: --chrome > ASWAP_CHROME > installed."""
    for given in (explicit, os.environ.get("ASWAP_CHROME")):
        if given:
            if Path(given).is_file():
                return Path(given)
            raise CaptureError(f"browser not found at {given}")
    candidates: list[str | None] = []
    if sys.platform == "win32":
        candidates.append(_registry_app_path("chrome.exe"))
        for base in (
            os.environ.get("ProgramFiles"),
            os.environ.get("ProgramFiles(x86)"),
            os.environ.get("LOCALAPPDATA"),
        ):
            if base:
                candidates.append(str(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"))
        candidates.append(_registry_app_path("msedge.exe"))
        for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
            if base:
                candidates.append(str(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"))
    elif sys.platform == "darwin":
        candidates.append("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        candidates.append("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")
    else:
        for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge"):
            candidates.append(shutil.which(name))
    for c in candidates:
        if c and Path(c).is_file():
            return Path(c)
    raise CaptureError("Chrome/Edge not found; pass --chrome PATH or set ASWAP_CHROME")


def read_devtools_port(profile: Path) -> tuple[int, str] | None:
    """``DevToolsActivePort``: line 1 the port, line 2 the browser ws path."""
    try:
        lines = (Path(profile) / "DevToolsActivePort").read_text().splitlines()
        port = int(lines[0].strip())
        path = lines[1].strip()
    except (OSError, ValueError, IndexError):
        return None
    return (port, path) if port > 0 and path.startswith("/devtools/") else None


class CdpBrowser:
    """Minimal browser-level Chrome DevTools Protocol client."""

    def __init__(self, ws) -> None:
        self._ws = ws
        self._id = 0

    @classmethod
    def connect(cls, ws_url: str) -> "CdpBrowser":
        import websocket  # websocket-client

        # suppress_origin: Chrome rejects DevTools sockets that send an Origin
        # unless launched with --remote-allow-origins.
        return cls(websocket.create_connection(ws_url, timeout=10, suppress_origin=True))

    def call(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        msg_id = self._id
        self._ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        while True:
            reply = json.loads(self._ws.recv())
            if reply.get("id") == msg_id:
                if "error" in reply:
                    raise CaptureError(f"CDP {method}: {reply['error'].get('message')}")
                return reply.get("result") or {}

    def cookies(self) -> list[dict]:
        return self.call("Storage.getCookies").get("cookies") or []

    def close(self) -> None:
        try:
            self.call("Browser.close")
        except Exception:
            pass
        try:
            self._ws.close()
        except Exception:
            pass


def _session_cookie(cookies: list[dict]) -> str | None:
    for c in cookies:
        if c.get("name") == "sessionKey" and str(c.get("domain", "")).lstrip(".").endswith("claude.ai"):
            value = c.get("value")
            if isinstance(value, str) and value:
                return value
    return None


def capture_session(
    identity: Identity,
    root: Path,
    *,
    chrome: str | None = None,
    timeout_s: float = 300.0,
    poll_s: float = 2.0,
    out=print,
    launcher=subprocess.Popen,
    connect=CdpBrowser.connect,
    orgs_fetch=fetch_organizations,
    email_fetch=fetch_account_email,
    clock=time.monotonic,
    sleep=time.sleep,
) -> str:
    """Open a dedicated browser profile on claude.ai and return its sessionKey.

    Accepts only a session that belongs to ``identity``: its organizations
    must include the slot's org uuid, and when claude.ai reports the signed-in
    email it must match. A wrong-account session is reported once and the
    wait continues, so the user can sign out and back in in the same window.
    """
    email, org_uuid = identity
    exe = find_chrome(chrome)
    profile = profile_dir(root, email)
    profile.mkdir(parents=True, exist_ok=True)
    try:
        (profile / "DevToolsActivePort").unlink()
    except OSError:
        pass
    argv = [
        str(exe),
        f"--user-data-dir={profile}",
        "--remote-debugging-port=0",
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window",
        f"{CLAUDE}/login",
    ]
    launcher(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    port_deadline = clock() + 20.0
    port = read_devtools_port(profile)
    while port is None:
        if clock() > port_deadline:
            raise CaptureError(
                "browser did not open a DevTools port — close any window already "
                f"using {profile} and retry"
            )
        sleep(0.25)
        port = read_devtools_port(profile)
    browser = connect(f"ws://127.0.0.1:{port[0]}{port[1]}")
    out(f"Sign in to claude.ai as {email} in the window that opened (waiting {int(timeout_s)}s)...")
    rejected: set[str] = set()
    deadline = clock() + timeout_s
    try:
        while clock() < deadline:
            try:
                key = _session_cookie(browser.cookies())
            except Exception as e:  # window closed by the user
                raise CaptureError(f"browser connection lost ({e})") from None
            if key and key not in rejected:
                try:
                    orgs = orgs_fetch(key)
                except BillingSessionError:
                    orgs = None  # half-finished login; poll again
                if orgs is not None:
                    uuids = {o.get("uuid") for o in orgs}
                    who = email_fetch(key)
                    org_ok = not org_uuid or org_uuid in uuids
                    email_ok = who is None or who.lower() == email.lower()
                    if org_ok and email_ok:
                        return key
                    rejected.add(key)
                    seen = who or ", ".join(str(o.get("name")) for o in orgs) or "another account"
                    out(f"That window is signed in as {seen}, not {email} — sign out there and sign in again.")
            sleep(poll_s)
        raise CaptureError(f"timed out after {int(timeout_s)}s waiting for {email} to sign in")
    finally:
        browser.close()
