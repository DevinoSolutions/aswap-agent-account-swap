"""claude.ai web sessions and OAuth token minting.

``/api/oauth/profile`` has no renewal date and no "cancels at period end"
flag. ``claude.ai/api/organizations/{org}/subscription_details`` has both,
but refuses OAuth tokens (``oauth_token_not_accepted``); it is served to a
signed-in claude.ai session (the ``sessionKey`` cookie).

Capture: aswap launches the user's real Chrome (or Edge) with a dedicated
per-account profile and a DevTools port, the user signs in normally — Google
sign-in included, which refuses automation-flagged browsers — and aswap
reads the cookie over CDP. The profile persists, so re-capturing an expired
session is usually just reopening the window.

OAuth minting: once a browser session is captured, ``mint_oauth_tokens``
reuses the same browser (already authenticated on claude.ai) to drive the
standard OAuth PKCE flow — navigating to the authorize URL, catching the
redirect code on a local callback server, and exchanging it for an
access/refresh token pair. This eliminates the separate ``claude /login``
step when adding accounts.

Session keys are as sensitive as OAuth tokens: stored 0600 beside them,
never logged, never printed.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
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
# Only plan_ending_at confirms a cancellation (owner rule 2026-11-01: auto
# drains a cancelled account only once the cancellation is confirmed). The
# endpoint has never sent a Stripe-style cancel flag or cancel timestamp,
# and plan_ending_before's meaning is unknown, so neither counts.
_ENDING_KEYS = ("plan_ending_at",)
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


_CDP_CONNECT_SENTINEL = object()


class CdpBrowser:
    """Minimal browser-level Chrome DevTools Protocol client.

    Used by tests and as a fallback when nodriver is unavailable.
    """

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


_ND_LOOP = None


def _nd_run(coro):
    """Run a nodriver coroutine on the one loop every nodriver call shares.

    A browser stays bound to the loop that started it, and ``asyncio.run``
    closes its loop on return, so capture and the token mint after it would
    fail with "Event loop is closed".
    """
    import asyncio

    global _ND_LOOP
    if _ND_LOOP is None or _ND_LOOP.is_closed():
        _ND_LOOP = asyncio.new_event_loop()
    return _ND_LOOP.run_until_complete(coro)


async def _nd_stop(browser) -> None:
    """Close Chrome gracefully, then make sure it is gone.

    ``Browser.close`` lets Chrome write its cookie store, so the profile
    stays signed in for the next ``aswap login``; a bare ``stop()`` kills it
    first and the session is lost.

    nodriver's ``stop()`` never waits for Chrome nor closes its subprocess
    transport. A Chrome slower to quit than a fixed pause left that transport
    open until interpreter exit, where it printed "I/O operation on closed
    pipe" tracebacks, so this waits for the exit and closes it explicitly.
    """
    import asyncio

    from nodriver import cdp

    proc = getattr(browser, "_process", None)
    try:
        await asyncio.wait_for(browser.connection.send(cdp.browser.close()), 5)
    except Exception:
        pass
    await _nd_wait_exit(proc, 10)
    try:
        browser.stop()
    except Exception:
        pass
    await _nd_wait_exit(proc, 5)
    transport = getattr(proc, "_transport", None)
    if transport is not None:
        transport.close()  # kills Chrome if it is somehow still running
    await asyncio.sleep(0.5)  # lets stop()'s connection-close task and the pipes finish


async def _nd_wait_exit(proc, timeout_s: float) -> None:
    import asyncio

    if proc is None or proc.returncode is not None:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout_s)
    except Exception:
        pass


class NodriverBrowser:
    """nodriver-based browser that bypasses Cloudflare Turnstile.

    Holds a reference to the ``nodriver.Browser`` for ``mint_oauth_tokens``
    to navigate. Cookie reads and navigation are done via ``_nd_*`` async
    helpers inside the capture coroutines — this class is only used to
    carry the browser reference to the caller.
    """

    def __init__(self, nd_browser) -> None:
        self._browser = nd_browser  # nodriver.Browser

    def cookies(self) -> list[dict]:
        """Sync cookie read — only works if called from within asyncio.run()."""
        raise NotImplementedError(
            "NodriverBrowser.cookies() should not be called directly; "
            "use the async capture functions instead"
        )

    def navigate(self, url: str) -> None:
        """Sync navigate — only works if called from within asyncio.run()."""
        raise NotImplementedError(
            "NodriverBrowser.navigate() should not be called directly; "
            "use the async capture functions instead"
        )

    def close(self) -> None:
        if getattr(self, "_closed", False):
            return  # closing twice would only wait on a Chrome that is gone
        self._closed = True
        try:
            _nd_run(_nd_stop(self._browser))
        except Exception:
            pass


async def _nd_cookies(browser) -> list[dict]:
    """Read cookies from a nodriver Browser, returning CdpBrowser-compatible dicts."""
    raw = await browser.cookies.get_all()
    return [
        {
            "name": getattr(c, "name", ""),
            "value": getattr(c, "value", ""),
            "domain": getattr(c, "domain", ""),
            "path": getattr(c, "path", "/"),
        }
        for c in raw
    ]


async def _nd_open_signed_in(root: Path, email: str, session_key: str, chrome: str | None):
    import nodriver
    from nodriver import cdp

    exe = find_chrome(chrome)
    profile = profile_dir(root, email)
    profile.mkdir(parents=True, exist_ok=True)
    browser = await nodriver.start(
        user_data_dir=str(profile),
        browser_executable_path=str(exe),
        browser_args=["--no-first-run", "--no-default-browser-check"],
    )
    cookie = cdp.network.CookieParam(
        name="sessionKey", value=session_key, domain=".claude.ai", path="/",
        secure=True, http_only=True, same_site=cdp.network.CookieSameSite.LAX,
        expires=cdp.network.TimeSinceEpoch(time.time() + 30 * 86400),
    )
    try:
        await browser.cookies.set_all([cookie])
    except Exception as e:
        await _nd_stop(browser)
        raise CaptureError(f"could not load the saved claude.ai login into the browser: {e}") from None
    return NodriverBrowser(browser)


def open_signed_in(root: Path, email: str, session_key: str, *, chrome: str | None = None) -> NodriverBrowser:
    """Open ``email``'s browser profile signed in with a saved claude.ai session.

    For minting without a human: the saved ``sessionKey`` is set as the
    profile's cookie, so a profile whose own cookie was lost still works and
    the login page (and its sign-in email) is never involved.
    """
    return _nd_run(_nd_open_signed_in(root, email, session_key, chrome))


_EMAIL_INPUT = "input#email, input[type=email]"
_CONTINUE_BUTTON = "button[data-testid=continue], button[type=submit]"


async def _nd_prefill_email(tab, email: str, out, wait_s: float = 60.0, inbox_config=None) -> bool:
    """Type ``email`` into claude.ai's login form and press Continue.

    Best effort: a profile that is already signed in never shows the form,
    so a missing field just ends the attempt. The field is a React input, so
    the text goes in through CDP ``Input.insertText`` (keystrokes are lost).

    With a login inbox configured (``aswap inbox set``), the magic link
    claude.ai mails is read from that inbox and opened in this tab.
    """
    import asyncio
    import threading

    from nodriver import cdp

    try:
        field = await tab.select(_EMAIL_INPUT, timeout=wait_s)
    except Exception:
        return False
    inbox = await asyncio.to_thread(_open_inbox, inbox_config, out)
    # Set when this task ends (a manual sign-in cancels it), so the polling
    # thread stops instead of holding the process open until its timeout.
    stop = threading.Event()
    try:
        after_uid = await asyncio.to_thread(inbox.latest_uid) if inbox else 0
        try:
            await field.focus()
            await tab.send(cdp.input_.insert_text(email))
            button = await tab.select(_CONTINUE_BUTTON, timeout=5)
            await button.click()
        except Exception:
            return False
        if inbox is None:
            out(f"Entered {email} and pressed Continue — finish with the code or link claude.ai sends.")
            return True
        out(f"Entered {email} and pressed Continue — watching {inbox.config.user} for the sign-in link...")
        link = await asyncio.to_thread(_await_link, inbox, email, after_uid, stop)
        inbox = None  # _await_link closed it
    except Exception as e:  # inbox trouble never blocks a manual sign-in
        out(f"Login inbox: {e} — finish with the code or link claude.ai sends.")
        return True
    finally:
        stop.set()
        if inbox is not None:
            inbox.close()
    if not link:
        out("No sign-in link reached the login inbox — finish with the code or link claude.ai sends.")
        return True
    out("Sign-in link arrived; opening it.")
    await tab.get(link)
    return True


def _await_link(inbox, email: str, after_uid: int, stop) -> str | None:
    """Wait on its own thread for the link, then close the inbox there."""
    try:
        return inbox.wait_for_link(
            email, after_uid, stop=stop.is_set, sleep=stop.wait,
        )
    finally:
        inbox.close()


def _open_inbox(config, out):
    """An ``Inbox`` on ``config``, or None (unset, or unreachable: reported)."""
    from claude_swap import inbox

    if config is None:
        return None
    try:
        return inbox.Inbox(config)
    except inbox.InboxError as e:
        out(f"Login inbox unreachable ({e}) — sign in by hand.")
        return None


def _start_prefill(tab, email: str | None, out, root: Path | None = None):
    import asyncio

    from claude_swap.inbox import load_inbox

    if not email or "@" not in email:
        return None
    config = load_inbox(root) if root is not None else None
    return asyncio.ensure_future(_nd_prefill_email(tab, email, out, inbox_config=config))


def _stop_prefill(task) -> None:
    if task is not None and not task.done():
        task.cancel()


async def _nd_capture_session(
    identity: Identity,
    root: Path,
    chrome: str | None,
    timeout_s: float,
    poll_s: float,
    keep_browser: bool,
    out,
    orgs_fetch,
    email_fetch,
) -> "str | tuple[str, NodriverBrowser]":
    """Async implementation of ``capture_session`` using nodriver."""
    import asyncio

    import nodriver

    email, org_uuid = identity
    exe = find_chrome(chrome)
    profile = profile_dir(root, email)
    profile.mkdir(parents=True, exist_ok=True)

    browser = await nodriver.start(
        user_data_dir=str(profile),
        browser_executable_path=str(exe),
        browser_args=["--no-first-run", "--no-default-browser-check"],
    )
    tab = await browser.get(f"{CLAUDE}/login")
    out(f"Sign in to claude.ai as {email} in the window that opened (waiting {int(timeout_s)}s)...")
    prefill = _start_prefill(tab, email, out, root)

    rejected: set[str] = set()
    deadline = time.monotonic() + timeout_s
    try:
        while time.monotonic() < deadline:
            cookies = await _nd_cookies(browser)
            key = _session_cookie(cookies)
            if key and key not in rejected:
                try:
                    orgs = orgs_fetch(key)
                except BillingSessionError:
                    orgs = None
                if orgs is not None:
                    uuids = {o.get("uuid") for o in orgs}
                    who = email_fetch(key)
                    org_ok = not org_uuid or org_uuid in uuids
                    email_ok = who is None or who.lower() == email.lower()
                    if org_ok and email_ok:
                        if keep_browser:
                            return key, NodriverBrowser(browser)
                        await _nd_stop(browser)
                        return key
                    rejected.add(key)
                    seen = who or ", ".join(str(o.get("name")) for o in orgs) or "another account"
                    out(f"That window is signed in as {seen}, not {email} — sign out there and sign in again.")
            await asyncio.sleep(poll_s)
        raise CaptureError(f"timed out after {int(timeout_s)}s waiting for {email} to sign in")
    except CaptureError:
        await _nd_stop(browser)
        raise
    except Exception as e:
        await _nd_stop(browser)
        raise CaptureError(f"browser error: {e}") from None
    finally:
        _stop_prefill(prefill)


async def _nd_capture_any_session(
    root: Path,
    email_hint: str | None,
    chrome: str | None,
    timeout_s: float,
    poll_s: float,
    keep_browser: bool,
    out,
    orgs_fetch,
    email_fetch,
) -> CapturedLogin:
    """Async implementation of ``capture_any_session`` using nodriver."""
    import asyncio

    import nodriver

    exe = find_chrome(chrome)
    profile = profile_dir(root, email_hint or "_aswap_login")
    profile.mkdir(parents=True, exist_ok=True)

    browser = await nodriver.start(
        user_data_dir=str(profile),
        browser_executable_path=str(exe),
        browser_args=["--no-first-run", "--no-default-browser-check"],
    )
    tab = await browser.get(f"{CLAUDE}/login")
    label = email_hint or "any account"
    out(f"Sign in to claude.ai as {label} in the window that opened (waiting {int(timeout_s)}s)...")
    prefill = _start_prefill(tab, email_hint, out, root)

    seen_keys: set[str] = set()
    deadline = time.monotonic() + timeout_s
    try:
        while time.monotonic() < deadline:
            cookies = await _nd_cookies(browser)
            key = _session_cookie(cookies)
            if key and key not in seen_keys:
                seen_keys.add(key)
                try:
                    orgs = orgs_fetch(key)
                except BillingSessionError:
                    orgs = None
                if orgs is not None:
                    who = email_fetch(key)
                    if who:
                        org = next(
                            (o for o in orgs if o.get("organizationType") == "personal"),
                            orgs[0] if orgs else {},
                        )
                        nd = NodriverBrowser(browser) if keep_browser else None
                        if not keep_browser:
                            await _nd_stop(browser)
                        return CapturedLogin(
                            session_key=key,
                            email=who,
                            org_uuid=org.get("uuid", ""),
                            org_name=org.get("name", ""),
                            browser=nd,
                        )
            await asyncio.sleep(poll_s)
        raise CaptureError(f"timed out after {int(timeout_s)}s waiting for a sign-in")
    except CaptureError:
        await _nd_stop(browser)
        raise
    except Exception as e:
        await _nd_stop(browser)
        raise CaptureError(f"browser error: {e}") from None
    finally:
        _stop_prefill(prefill)


async def _nd_mint_oauth_tokens(
    nd_browser,
    timeout_s: float,
    out,
) -> dict:
    """Async implementation of ``mint_oauth_tokens`` using nodriver."""
    import asyncio
    import http.server
    import threading
    import urllib.parse

    verifier, challenge = _generate_pkce()

    auth_code: list[str] = []
    auth_error: list[str] = []

    class CallbackHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            if "code" in params:
                auth_code.append(params["code"][0])
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(
                    b"<html><body><h2>Login successful</h2>"
                    b"<p>You can close this tab.</p></body></html>"
                )
            else:
                err = params.get("error", ["unknown"])[0]
                desc = params.get("error_description", [""])[0]
                auth_error.append(f"{err}: {desc}".strip())
                self.send_response(400)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(
                    f"<html><body><h2>Login failed</h2><p>{err}: {desc}</p></body></html>".encode()
                )

        def log_message(self, fmt, *a) -> None:
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), CallbackHandler)
    port = srv.server_address[1]
    redirect_uri = f"http://localhost:{port}/callback"
    srv_thread = threading.Thread(target=srv.handle_request, daemon=True)
    srv_thread.start()

    state = secrets.token_urlsafe(32)
    authorize_url = f"{_OAUTH_AUTHORIZE}?{_authorize_query(redirect_uri, challenge, state)}"

    try:
        tab = await nd_browser.get(authorize_url, new_tab=True)
    except Exception as e:
        srv.server_close()
        raise OAuthError(f"failed to open the authorize URL: {e}")

    out("Obtaining Claude Code token (approving the consent screen for you)...")

    deadline = time.monotonic() + timeout_s
    on_login = 0
    while time.monotonic() < deadline:
        if auth_code:
            break
        try:
            await tab.evaluate(_CLICK_AUTHORIZE_JS)
            on_login = on_login + 1 if await tab.evaluate(_ON_LOGIN_PAGE_JS) else 0
        except Exception:
            pass  # page mid-navigation; try again next tick
        if auth_error:
            srv.server_close()
            raise OAuthError(f"authorize failed: {auth_error[0]}")
        if on_login >= _SIGNED_OUT_TICKS:
            srv.server_close()
            raise SignedOut("claude.ai asked to sign in again: the saved login has expired")
        await asyncio.sleep(0.5)
    else:
        srv.server_close()
        raise OAuthError(
            f"timed out after {int(timeout_s)}s waiting for the OAuth callback — "
            "the consent screen may need manual approval"
        )
    srv.server_close()

    code = auth_code[0]
    token_body = json.dumps({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": _OAUTH_CLIENT_ID,
        "code_verifier": verifier,
        "state": state,
    }).encode()

    req = urllib.request.Request(
        _OAUTH_TOKEN,
        data=token_body,
        headers={"Content-Type": "application/json", "User-Agent": _TOKEN_UA},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            token_data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:500] if hasattr(e, "read") else ""
        raise OAuthError(f"token exchange failed (HTTP {e.code}): {body}") from None
    except Exception as e:
        raise OAuthError(f"token exchange failed: {e}") from None

    if "access_token" not in token_data:
        raise OAuthError(f"token response missing access_token: {token_data}")
    return token_data


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
    timeout_s: float = 600.0,
    poll_s: float = 2.0,
    keep_browser: bool = False,
    out=print,
    launcher=subprocess.Popen,
    connect=_CDP_CONNECT_SENTINEL,
    orgs_fetch=fetch_organizations,
    email_fetch=fetch_account_email,
    clock=time.monotonic,
    sleep=time.sleep,
    _use_nodriver: bool | None = None,
) -> "str | tuple[str, NodriverBrowser]":
    """Open a dedicated browser profile on claude.ai and return its sessionKey.

    When ``keep_browser`` is True, returns ``(session_key, browser)`` so
    the caller can reuse the browser connection (e.g. for the OAuth flow).
    The caller must close the browser when done.

    Uses nodriver by default to bypass Cloudflare Turnstile. Falls back to
    raw CDP when nodriver is unavailable or ``_use_nodriver=False``. Tests
    inject their own ``launcher``/``connect`` (which forces the legacy path).

    Accepts only a session that belongs to ``identity``: its organizations
    must include the slot's org uuid, and when claude.ai reports the signed-in
    email it must match. A wrong-account session is reported once and the
    wait continues, so the user can sign out and back in in the same window.
    """
    email, org_uuid = identity
    profile = profile_dir(root, email)

    # Decide backend: nodriver unless tests injected their own launcher/connect,
    # or the caller explicitly opted out.
    use_nd = _use_nodriver
    if use_nd is None:
        use_nd = (launcher is subprocess.Popen and connect is _CDP_CONNECT_SENTINEL)

    if connect is _CDP_CONNECT_SENTINEL:
        connect = CdpBrowser.connect

    if use_nd:
        return _nd_run(_nd_capture_session(
            identity, root, chrome, timeout_s, poll_s, keep_browser,
            out, orgs_fetch, email_fetch,
        ))

    # Legacy CDP path (used by tests and when nodriver is unavailable).
    exe = find_chrome(chrome)
    profile.mkdir(parents=True, exist_ok=True)
    try:
        (profile / "DevToolsActivePort").unlink()
    except OSError:
        pass
    argv = [
        str(exe), f"--user-data-dir={profile}",
        "--remote-debugging-port=0", "--no-first-run",
        "--no-default-browser-check", "--new-window", f"{CLAUDE}/login",
    ]
    launcher(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    port_deadline = clock() + 20.0
    port = read_devtools_port(profile)
    while port is None:
        if clock() > port_deadline:
            raise CaptureError(
                "browser did not open a DevTools port — close any window "
                f"already using {profile} and retry"
            )
        sleep(0.25)
        port = read_devtools_port(profile)
    browser = connect(f"ws://127.0.0.1:{port[0]}{port[1]}")

    out(f"Sign in to claude.ai as {email} in the window that opened (waiting {int(timeout_s)}s)...")
    rejected: set[str] = set()
    deadline = clock() + timeout_s
    close_browser = True
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
                        if keep_browser:
                            close_browser = False
                            return key, browser
                        return key
                    rejected.add(key)
                    seen = who or ", ".join(str(o.get("name")) for o in orgs) or "another account"
                    out(f"That window is signed in as {seen}, not {email} — sign out there and sign in again.")
            sleep(poll_s)
        raise CaptureError(f"timed out after {int(timeout_s)}s waiting for {email} to sign in")
    finally:
        if close_browser:
            browser.close()


@dataclass
class CapturedLogin:
    """Result of ``capture_any_session``: who signed in and their session."""

    session_key: str
    email: str
    org_uuid: str
    org_name: str
    browser: NodriverBrowser | CdpBrowser | None  # non-None when ``keep_browser=True``


def capture_any_session(
    root: Path,
    *,
    email_hint: str | None = None,
    chrome: str | None = None,
    timeout_s: float = 600.0,
    poll_s: float = 2.0,
    keep_browser: bool = False,
    out=print,
    launcher=subprocess.Popen,
    connect=_CDP_CONNECT_SENTINEL,
    orgs_fetch=fetch_organizations,
    email_fetch=fetch_account_email,
    clock=time.monotonic,
    sleep=time.sleep,
    _use_nodriver: bool | None = None,
) -> CapturedLogin:
    """Open a browser on claude.ai and accept whoever signs in.

    Unlike ``capture_session``, no identity is required upfront. The caller
    gets back the email and org of whoever actually authenticated.

    ``email_hint`` is used only for the browser profile directory (so
    returning users get their persisted profile). When omitted, a generic
    ``_aswap_login`` profile is used.
    """
    profile = profile_dir(root, email_hint or "_aswap_login")

    use_nd = _use_nodriver
    if use_nd is None:
        use_nd = (launcher is subprocess.Popen and connect is _CDP_CONNECT_SENTINEL)

    if connect is _CDP_CONNECT_SENTINEL:
        connect = CdpBrowser.connect

    if use_nd:
        return _nd_run(_nd_capture_any_session(
            root, email_hint, chrome, timeout_s, poll_s, keep_browser,
            out, orgs_fetch, email_fetch,
        ))

    # Legacy CDP path (used by tests and when nodriver is unavailable).
    exe = find_chrome(chrome)
    profile.mkdir(parents=True, exist_ok=True)
    try:
        (profile / "DevToolsActivePort").unlink()
    except OSError:
        pass
    argv = [
        str(exe), f"--user-data-dir={profile}",
        "--remote-debugging-port=0", "--no-first-run",
        "--no-default-browser-check", "--new-window", f"{CLAUDE}/login",
    ]
    launcher(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    port_deadline = clock() + 20.0
    port = read_devtools_port(profile)
    while port is None:
        if clock() > port_deadline:
            raise CaptureError(
                "browser did not open a DevTools port — close any window "
                f"already using {profile} and retry"
            )
        sleep(0.25)
        port = read_devtools_port(profile)
    browser = connect(f"ws://127.0.0.1:{port[0]}{port[1]}")

    label = email_hint or "any account"
    out(f"Sign in to claude.ai as {label} in the window that opened (waiting {int(timeout_s)}s)...")
    seen_keys: set[str] = set()
    deadline = clock() + timeout_s
    close_browser = True
    try:
        while clock() < deadline:
            try:
                key = _session_cookie(browser.cookies())
            except Exception as e:
                raise CaptureError(f"browser connection lost ({e})") from None
            if key and key not in seen_keys:
                seen_keys.add(key)
                try:
                    orgs = orgs_fetch(key)
                except BillingSessionError:
                    orgs = None
                if orgs is not None:
                    who = email_fetch(key)
                    if who:
                        # Pick the first personal org, or the first org.
                        org = next(
                            (o for o in orgs if o.get("organizationType") == "personal"),
                            orgs[0] if orgs else {},
                        )
                        if keep_browser:
                            close_browser = False
                        return CapturedLogin(
                            session_key=key,
                            email=who,
                            org_uuid=org.get("uuid", ""),
                            org_name=org.get("name", ""),
                            browser=browser if keep_browser else None,
                        )
            sleep(poll_s)
        raise CaptureError(f"timed out after {int(timeout_s)}s waiting for a sign-in")
    finally:
        if close_browser:
            browser.close()


# --- OAuth PKCE token minting -----------------------------------------------
#
# Drive the standard Claude Code OAuth flow from a browser that already has
# a valid claude.ai session, producing access + refresh tokens without the
# user needing a separate ``claude /login``.

# Claude Code's own subscription (Pro/Max) login: CLAUDE_AI_AUTHORIZE_URL and
# its claude.ai scope set. platform.claude.com/oauth/authorize is the Console
# (API key) login and sends a claude.ai session to a separate sign-in page.
_OAUTH_AUTHORIZE = "https://claude.com/cai/oauth/authorize"
_OAUTH_TOKEN = "https://platform.claude.com/v1/oauth/token"
_OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
# The token endpoint answers 429 to the browser UA claude.ai needs; aswap's
# own refreshes (oauth.py) send this one and are served.
_TOKEN_UA = "claude-swap/1.0"
_OAUTH_SCOPES = "user:profile user:inference user:sessions:claude_code user:mcp_servers user:file_upload"

# The consent page has "Authorize" and "Decline"; the session is already the
# account being added, so aswap approves it instead of waiting on a click.
_CLICK_AUTHORIZE_JS = (
    "(() => { if (!location.pathname.startsWith('/oauth/authorize')) return false;"
    " const b = [...document.querySelectorAll('button')]"
    ".find(x => x.innerText.trim() === 'Authorize' && !x.disabled);"
    " if (b) b.click(); return !!b; })()"
)


# A signed-out browser is sent from the consent page to claude.ai/login. Seen
# for 3 s straight (not a redirect passing through), the mint stops waiting.
_ON_LOGIN_PAGE_JS = "location.hostname.endsWith('claude.ai') && location.pathname.startsWith('/login')"
_SIGNED_OUT_TICKS = 6


def _authorize_query(redirect_uri: str, challenge: str, state: str) -> str:
    import urllib.parse

    return urllib.parse.urlencode({
        "code": "true",
        "client_id": _OAUTH_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": _OAUTH_SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    })


class OAuthError(BillingSessionError):
    """The OAuth token minting flow failed."""


class SignedOut(OAuthError):
    """The browser's claude.ai login is gone: the consent page sent it to sign in."""


def _generate_pkce() -> tuple[str, str]:
    """Return ``(code_verifier, code_challenge)`` for S256 PKCE."""
    import base64
    import hashlib
    import secrets

    verifier = secrets.token_urlsafe(43)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def mint_oauth_tokens(
    browser,
    *,
    timeout_s: float = 60.0,
    out=print,
    clock=time.monotonic,
    sleep=time.sleep,
) -> dict:
    """Use an authenticated browser to obtain Claude Code OAuth tokens.

    Opens the authorize URL in a new tab and catches the redirect code on a
    local callback server. Returns the raw token-endpoint response dict
    (``access_token``, ``refresh_token``, ``expires_in``, ``scope``).

    The browser must already be signed in to claude.ai (e.g. after
    ``capture_session`` with ``keep_browser=True``).

    Works with both ``NodriverBrowser`` and ``CdpBrowser``.
    """
    if isinstance(browser, NodriverBrowser):
        return _nd_run(_nd_mint_oauth_tokens(browser._browser, timeout_s, out))
    import http.server
    import threading
    import urllib.parse

    verifier, challenge = _generate_pkce()

    # --- tiny callback server on an ephemeral port ---
    auth_code: list[str] = []  # captured from the redirect
    auth_error: list[str] = []

    class CallbackHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            if "code" in params:
                auth_code.append(params["code"][0])
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(
                    b"<html><body><h2>Login successful</h2>"
                    b"<p>You can close this tab.</p></body></html>"
                )
            else:
                err = params.get("error", ["unknown"])[0]
                desc = params.get("error_description", [""])[0]
                auth_error.append(f"{err}: {desc}".strip())
                self.send_response(400)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(
                    f"<html><body><h2>Login failed</h2><p>{err}: {desc}</p></body></html>".encode()
                )

        def log_message(self, fmt, *a) -> None:
            pass  # silence server logs

    srv = http.server.HTTPServer(("127.0.0.1", 0), CallbackHandler)
    port = srv.server_address[1]
    redirect_uri = f"http://localhost:{port}/callback"

    srv_thread = threading.Thread(target=srv.handle_request, daemon=True)
    srv_thread.start()

    # --- build authorize URL and open in a new tab ---
    state = secrets.token_urlsafe(32)
    authorize_url = f"{_OAUTH_AUTHORIZE}?{_authorize_query(redirect_uri, challenge, state)}"

    try:
        if hasattr(browser, "navigate"):
            browser.navigate(authorize_url)
        else:
            browser.call("Target.createTarget", {"url": authorize_url})
    except (CaptureError, Exception) as e:
        srv.server_close()
        raise OAuthError(f"failed to open the authorize URL in the browser: {e}")

    out("Obtaining Claude Code token (auto-granting via your session)...")

    # --- wait for the callback ---
    deadline = clock() + timeout_s
    while clock() < deadline:
        if auth_code:
            break
        if auth_error:
            srv.server_close()
            raise OAuthError(f"authorize failed: {auth_error[0]}")
        sleep(0.5)
    else:
        srv.server_close()
        raise OAuthError(
            f"timed out after {int(timeout_s)}s waiting for the OAuth callback — "
            "the consent screen may need manual approval"
        )
    srv.server_close()

    # --- exchange code for tokens ---
    code = auth_code[0]
    token_body = json.dumps({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": _OAUTH_CLIENT_ID,
        "code_verifier": verifier,
        "state": state,
    }).encode()

    req = urllib.request.Request(
        _OAUTH_TOKEN,
        data=token_body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": _TOKEN_UA,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            token_data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:500] if hasattr(e, "read") else ""
        raise OAuthError(f"token exchange failed (HTTP {e.code}): {body}") from None
    except Exception as e:
        raise OAuthError(f"token exchange failed: {e}") from None

    if "access_token" not in token_data:
        raise OAuthError(f"token response missing access_token: {token_data}")

    return token_data


_SUBSCRIPTION_TYPES = {
    "claude_max": "max",
    "claude_pro": "pro",
    "claude_team": "team",
    "claude_enterprise": "enterprise",
}


def build_credential_blob(token_data: dict, profile: dict | None = None) -> str:
    """Build the ``~/.claude.json`` credential JSON from a token-endpoint response.

    ``profile`` is ``/api/oauth/profile`` for the new token. Claude Code's own
    login stores the plan from it (``subscriptionType``, ``rateLimitTier``);
    without them the slot reads as plan-unknown.
    """
    from datetime import datetime, timezone

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    scopes = token_data.get("scope", "").split() if token_data.get("scope") else []
    oauth = {
        "accessToken": token_data["access_token"],
        "refreshToken": token_data.get("refresh_token", ""),
        "expiresAt": now_ms + token_data.get("expires_in", 3600) * 1000,
        "scopes": scopes,
    }
    org = (profile or {}).get("organization") or {}
    sub = _SUBSCRIPTION_TYPES.get(org.get("organization_type"))
    if sub:
        oauth["subscriptionType"] = sub
    if org.get("rate_limit_tier"):
        oauth["rateLimitTier"] = org["rate_limit_tier"]
    return json.dumps({"claudeAiOauth": oauth})
