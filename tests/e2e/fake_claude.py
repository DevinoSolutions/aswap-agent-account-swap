"""A stand-in for the Claude services aswap talks to, on a real local socket.

The end-to-end suite runs the real ``aswap`` executable against this server
(via ``ASWAP_API_BASE_URL`` / ``ASWAP_CLAUDE_AI_BASE_URL``). It is the only
simulated part of those tests: the responses Claude would send. Response
shapes follow what aswap parses (``oauth.py``, ``gate.py``, ``billing.py``,
``billing_session.py``) and what the real endpoints were measured to return.

Served, per account (keyed by bearer token or session cookie):

``GET  /api/oauth/usage``       five_hour / seven_day utilization (0-100) with
                                ``resets_at``, per-model ``limits``; 401 for an
                                expired or unknown token; 429 + Retry-After
                                while the account's 429 budget lasts.
``GET  /api/oauth/profile``     account + organization (plan, rate-limit tier,
                                subscription status).
``POST /v1/oauth/token``        refresh grant with refresh-token ROTATION: the
                                presented refresh token is spent, a new pair
                                is issued; a spent or killed one gets
                                ``invalid_grant``.
``POST /v1/messages``           ``anthropic-ratelimit-unified-*`` headers
                                (status, 5h/7d utilization as two-decimal
                                fractions, resets as epoch seconds); 429 when
                                rejected. Plain JSON or SSE when ``stream``.
``GET  /api/organizations``, ``/api/account``,
``/api/organizations/{org}/subscription_details``
                                the claude.ai web endpoints (cookie
                                ``sessionKey``).

Tests drive the state from outside the aswap processes with a small control
API under ``/__control/`` (see :class:`FakeClaudeClient`). Every request is
appended to a JSON-lines request log with tokens reduced to short ids, so the
log can be uploaded as a CI artifact without carrying even a fake secret.

Run standalone (used by the Claude Code CLI job)::

    python tests/e2e/fake_claude.py --port 8765 --log requests.jsonl
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
import threading
import time
import urllib.request
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlsplit

# What Claude Code's OAuth tokens look like, so any code that sniffs the
# prefix (API key vs OAuth) sees the real shapes.
ACCESS_PREFIX = "sk-ant-oat01-"
REFRESH_PREFIX = "sk-ant-ort01-"
SESSION_PREFIX = "sk-ant-sid01-"
SCOPES = ["user:inference", "user:profile", "user:sessions:claude_code"]

DEFAULT_TIER = "default_claude_max_5x"


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).isoformat()


def token_id(token: str | None) -> str | None:
    """A short stable stand-in for a token in logs: never the token itself."""
    if not token:
        return None
    return "tok:" + hashlib.sha256(token.encode()).hexdigest()[:10]


class Account:
    """One fake Claude account and everything the server knows about it."""

    def __init__(self, email: str, **kw) -> None:
        now = time.time()
        self.uuid = kw.get("uuid") or f"acct-{secrets.token_hex(6)}"
        self.email = email
        self.org_uuid = kw.get("org_uuid") or f"org-{secrets.token_hex(6)}"
        self.org_name = kw.get("org_name") or f"{email}'s Organization"
        self.tier = kw.get("tier", DEFAULT_TIER)
        self.org_type = kw.get("org_type", "claude_max")
        self.subscription_status = kw.get("subscription_status", "active")
        # Utilization in percent, as the usage endpoint reports it.
        self.five_hour = float(kw.get("five_hour", 0.0))
        self.seven_day = float(kw.get("seven_day", 0.0))
        self.five_hour_reset = float(kw.get("five_hour_reset", now + 3 * 3600))
        self.seven_day_reset = float(kw.get("seven_day_reset", now + 4 * 86400))
        # Per-model weekly windows: [{"name": "Fable", "pct": 10, "resets": ts}]
        self.scoped: list[dict] = list(kw.get("scoped", []))
        # The /v1/messages gate. None = derived from the windows (>= 100 on
        # either is rejected); else the true fractions the gate reports, which
        # can sit under 1.0 while the endpoint rounds up to 100.
        self.gate_5h: float | None = kw.get("gate_5h")
        self.gate_7d: float | None = kw.get("gate_7d")
        self.access_ttl_s = float(kw.get("access_ttl_s", 8 * 3600))
        self.usage_429_budget = 0  # next N usage requests answer 429
        self.retry_after_s = 0.0
        self.refresh_dead = False  # every refresh grant -> invalid_grant
        # Token state. access token -> expiry epoch; live refresh tokens.
        self.access: dict[str, float] = {}
        self.refresh: set[str] = set()
        self.spent_refresh: set[str] = set()
        self.session_keys: set[str] = set()
        self.counts: dict[str, int] = {}
        # Tokens issued before this server started (a fixture's stored
        # credential): registered as live so the fixture can refresh.
        if kw.get("access_token"):
            self.access[kw["access_token"]] = float(kw.get("access_expires", 0.0))
        if kw.get("refresh_token"):
            self.refresh.add(kw["refresh_token"])

    # -- tokens ---------------------------------------------------------------

    def mint(self) -> dict:
        """A fresh token family, as a ``/login`` would get."""
        at = ACCESS_PREFIX + secrets.token_urlsafe(32)
        rt = REFRESH_PREFIX + secrets.token_urlsafe(32)
        expires = time.time() + self.access_ttl_s
        self.access[at] = expires
        self.refresh.add(rt)
        return {"accessToken": at, "refreshToken": rt, "expiresAt": int(expires * 1000)}

    def rotate(self, presented: str) -> dict:
        self.refresh.discard(presented)
        self.spent_refresh.add(presented)
        return self.mint()

    def session_key(self) -> str:
        key = SESSION_PREFIX + secrets.token_urlsafe(24)
        self.session_keys.add(key)
        return key

    # -- views ------------------------------------------------------------------

    def usage_body(self) -> dict:
        limits = [
            {
                "kind": "weekly_scoped",
                "scope": {"model": {"display_name": s["name"]}},
                "percent": float(s["pct"]),
                "resets_at": iso(float(s.get("resets", self.seven_day_reset))),
            }
            for s in self.scoped
        ]
        return {
            "five_hour": {"utilization": self.five_hour, "resets_at": iso(self.five_hour_reset)},
            "seven_day": {"utilization": self.seven_day, "resets_at": iso(self.seven_day_reset)},
            "seven_day_oauth_apps": None,
            "seven_day_opus": None,
            "extra_usage": {"is_enabled": False, "monthly_limit": None,
                            "used_credits": None, "utilization": None},
            "limits": limits,
        }

    def profile_body(self) -> dict:
        return {
            "account": {
                "uuid": self.uuid,
                "email": self.email,
                "full_name": self.email.split("@")[0],
                "display_name": self.email.split("@")[0],
                "has_claude_max": self.org_type == "claude_max",
                "has_claude_pro": self.org_type == "claude_pro",
                "created_at": "2025-01-01T00:00:00Z",
            },
            "organization": {
                "uuid": self.org_uuid,
                "name": self.org_name,
                "organization_type": self.org_type,
                "billing_type": "stripe_subscription",
                "rate_limit_tier": self.tier,
                "has_extra_usage_enabled": False,
                "subscription_status": self.subscription_status,
                "subscription_created_at": "2026-01-15T10:00:00Z",
            },
        }

    def gate_fractions(self) -> tuple[float, float]:
        h5 = self.gate_5h if self.gate_5h is not None else self.five_hour / 100.0
        d7 = self.gate_7d if self.gate_7d is not None else self.seven_day / 100.0
        return h5, d7

    def gate_headers(self) -> tuple[bool, dict[str, str]]:
        h5, d7 = self.gate_fractions()

        def status(frac: float) -> str:
            if frac >= 1.0:
                return "rejected"
            return "allowed_warning" if frac >= 0.9 else "allowed"

        s5, s7 = status(h5), status(d7)
        rejected = "rejected" in (s5, s7)
        overall = "rejected" if rejected else ("allowed_warning" if "allowed_warning" in (s5, s7) else "allowed")
        claim = "seven_day" if d7 >= h5 else "five_hour"
        headers = {
            "anthropic-ratelimit-unified-status": overall,
            "anthropic-ratelimit-unified-5h-status": s5,
            "anthropic-ratelimit-unified-5h-utilization": f"{h5:.2f}",
            "anthropic-ratelimit-unified-5h-reset": str(int(self.five_hour_reset)),
            "anthropic-ratelimit-unified-7d-status": s7,
            "anthropic-ratelimit-unified-7d-utilization": f"{d7:.2f}",
            "anthropic-ratelimit-unified-7d-reset": str(int(self.seven_day_reset)),
            "anthropic-ratelimit-unified-representative-claim": claim,
            "anthropic-ratelimit-unified-fallback-percentage": "0.5",
            "anthropic-ratelimit-unified-reset": str(int(min(self.five_hour_reset, self.seven_day_reset))),
        }
        return rejected, headers

    def subscription_details(self) -> dict:
        return {
            "status": self.subscription_status,
            "billing_interval": "monthly",
            "next_charge_at": iso(time.time() + 12 * 86400),
            "plan_ending_at": None,
            "scheduled_downgrade": None,
        }

    def state(self) -> dict:
        return {
            "uuid": self.uuid,
            "email": self.email,
            "orgUuid": self.org_uuid,
            "tier": self.tier,
            "subscriptionStatus": self.subscription_status,
            "fiveHour": self.five_hour,
            "sevenDay": self.seven_day,
            "fiveHourReset": self.five_hour_reset,
            "sevenDayReset": self.seven_day_reset,
            "gate": self.gate_fractions(),
            "liveRefreshTokens": len(self.refresh),
            "spentRefreshTokens": len(self.spent_refresh),
            "liveRefreshIds": sorted(token_id(t) for t in self.refresh),
            "spentRefreshIds": sorted(token_id(t) for t in self.spent_refresh),
            "counts": dict(self.counts),
        }

    def secrets(self) -> set[str]:
        """Every token this account was ever issued (for leak scans)."""
        return set(self.access) | self.refresh | self.spent_refresh | self.session_keys

    # -- control ------------------------------------------------------------------

    _PATCHABLE: ClassVar[dict[str, type]] = {
        "five_hour": float, "seven_day": float, "five_hour_reset": float,
        "seven_day_reset": float, "tier": str, "org_type": str,
        "subscription_status": str, "access_ttl_s": float, "scoped": list,
        "refresh_dead": bool, "usage_429_budget": int, "retry_after_s": float,
    }

    def patch(self, changes: dict) -> None:
        for key, value in changes.items():
            if key in ("gate_5h", "gate_7d"):
                setattr(self, key, None if value is None else float(value))
            elif key == "expire_access_tokens":
                if value:
                    self.access = {t: 0.0 for t in self.access}
            elif key in self._PATCHABLE:
                setattr(self, key, self._PATCHABLE[key](value))
            else:
                raise KeyError(key)


class FakeClaude:
    """The server state plus a ``ThreadingHTTPServer`` bound to 127.0.0.1."""

    def __init__(self, port: int = 0, log_path: Path | None = None) -> None:
        self.lock = threading.RLock()
        self.accounts: dict[str, Account] = {}
        self.log_path = Path(log_path) if log_path else None
        self.log: list[dict] = []
        handler = type("Handler", (_Handler,), {"fake": self})
        self.server = ThreadingHTTPServer(("127.0.0.1", port), handler)
        self.server.daemon_threads = True
        self.thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def start(self) -> FakeClaude:
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    # -- lookups (call with self.lock held) -----------------------------------

    def by_access(self, token: str | None) -> tuple[Account | None, bool]:
        """(account, expired) for a bearer token; (None, False) when unknown."""
        if not token:
            return None, False
        for acct in self.accounts.values():
            if token in acct.access:
                return acct, acct.access[token] <= time.time()
        return None, False

    def by_refresh(self, token: str) -> tuple[Account | None, bool]:
        """(account, live) for a refresh token."""
        for acct in self.accounts.values():
            if token in acct.refresh:
                return acct, True
            if token in acct.spent_refresh:
                return acct, False
        return None, False

    def by_session(self, key: str | None) -> Account | None:
        for acct in self.accounts.values():
            if key and key in acct.session_keys:
                return acct
        return None

    def record(self, entry: dict) -> None:
        entry["t"] = round(time.time(), 3)
        with self.lock:
            self.log.append(entry)
            if self.log_path is not None:
                with self.log_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(entry) + "\n")


class _Handler(BaseHTTPRequestHandler):
    fake: FakeClaude
    protocol_version = "HTTP/1.1"
    server_version = "fake-claude/1"

    def log_message(self, *args) -> None:  # the request log replaces stderr noise
        pass

    # -- plumbing ---------------------------------------------------------------

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _json(self, status: int, body: object, headers: dict | None = None) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _bearer(self) -> str | None:
        auth = self.headers.get("Authorization") or ""
        return auth[7:].strip() if auth.startswith("Bearer ") else None

    def _cookie_session(self) -> str | None:
        m = re.search(r"sessionKey=([^;\s]+)", self.headers.get("Cookie") or "")
        return m.group(1) if m else None

    def _log(self, status: int, acct: Account | None, **extra) -> None:
        if acct is not None:
            key = urlsplit(self.path).path
            acct.counts[key] = acct.counts.get(key, 0) + 1
        self.fake.record({
            "method": self.command,
            "path": urlsplit(self.path).path,
            "status": status,
            "account": acct.email if acct else None,
            "bearer": token_id(self._bearer()),
            "userAgent": self.headers.get("User-Agent"),
            **extra,
        })

    # -- routes -----------------------------------------------------------------

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path.startswith("/__control/"):
            return self._control_get(path)
        if path == "/api/oauth/usage":
            return self._usage()
        if path == "/api/oauth/profile":
            return self._profile()
        if path == "/api/organizations" or path == "/api/account" or (
            path.startswith("/api/organizations/") and path.endswith("/subscription_details")
        ):
            return self._claude_ai(path)
        self._not_found(path)

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        body = self._body()
        if path.startswith("/__control/"):
            return self._control_post(path, body)
        if path == "/v1/oauth/token":
            return self._token(body)
        if path == "/v1/messages":
            return self._messages(body)
        self._not_found(path)

    def _not_found(self, path: str) -> None:
        self._log(404, None)  # logged: shows what a client asked for that the fake lacks
        self._json(404, {"type": "error", "error": {"type": "not_found_error", "message": path}})

    def _authed(self) -> Account | None:
        """The bearer's account, or answer 401 and return None."""
        with self.fake.lock:
            acct, expired = self.fake.by_access(self._bearer())
        if acct is None or expired:
            self._log(401, acct, reason="expired" if expired else "unknown-token")
            self._json(401, {"type": "error", "error": {
                "type": "authentication_error",
                "message": "OAuth token has expired." if expired else "Invalid bearer token",
            }})
            return None
        return acct

    def _usage(self) -> None:
        acct = self._authed()
        if acct is None:
            return
        with self.fake.lock:
            if acct.usage_429_budget > 0:
                acct.usage_429_budget -= 1
                self._log(429, acct)
                return self._json(
                    429,
                    {"type": "error", "error": {"type": "rate_limit_error", "message": "Rate limited"}},
                    {"Retry-After": str(int(acct.retry_after_s))},
                )
            body = acct.usage_body()
        self._log(200, acct)
        self._json(200, body)

    def _profile(self) -> None:
        acct = self._authed()
        if acct is None:
            return
        with self.fake.lock:
            body = acct.profile_body()
        self._log(200, acct, subscription=acct.subscription_status, tier=acct.tier)
        self._json(200, body)

    def _token(self, raw: bytes) -> None:
        try:
            req = json.loads(raw or b"{}")
        except ValueError:
            req = {}
        presented = req.get("refresh_token") or ""
        if req.get("grant_type") != "refresh_token" or not presented:
            self._log(400, None, grant=req.get("grant_type"))
            return self._json(400, {"error": "invalid_request", "error_description": "bad grant"})
        with self.fake.lock:
            acct, live = self.fake.by_refresh(presented)
            if acct is None or not live or acct.refresh_dead:
                self._log(400, acct, refresh=token_id(presented), outcome="invalid_grant")
                return self._json(400, {
                    "error": "invalid_grant",
                    "error_description": "Refresh token not found or invalid",
                })
            fresh = acct.rotate(presented)
        self._log(200, acct, refresh=token_id(presented), outcome="rotated")
        self._json(200, {
            "token_type": "Bearer",
            "access_token": fresh["accessToken"],
            "refresh_token": fresh["refreshToken"],
            "expires_in": int(acct.access_ttl_s),
            "scope": " ".join(SCOPES),
            "account": {"uuid": acct.uuid, "email_address": acct.email},
            "organization": {"uuid": acct.org_uuid, "name": acct.org_name},
        })

    def _messages(self, raw: bytes) -> None:
        try:
            req = json.loads(raw or b"{}")
        except ValueError:
            req = {}
        acct = self._authed()
        if acct is None:
            return
        with self.fake.lock:
            rejected, headers = acct.gate_headers()
        self._log(429 if rejected else 200, acct, model=req.get("model"),
                  gate=headers["anthropic-ratelimit-unified-status"])
        if rejected:
            headers["Retry-After"] = "3600"
            return self._json(429, {"type": "error", "error": {
                "type": "rate_limit_error",
                "message": "This request would exceed your account's rate limit.",
            }}, headers)
        msg_id = "msg_" + secrets.token_hex(12)
        text = f"fake reply for {acct.email}"
        usage = {"input_tokens": 8, "output_tokens": 1,
                 "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
        message = {
            "id": msg_id, "type": "message", "role": "assistant",
            "model": req.get("model") or "claude-haiku-5-5",
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn", "stop_sequence": None, "usage": usage,
        }
        if not req.get("stream"):
            return self._json(200, message, headers)
        events = [
            ("message_start", {"type": "message_start", "message": dict(message, content=[], stop_reason=None)}),
            ("content_block_start", {"type": "content_block_start", "index": 0,
                                     "content_block": {"type": "text", "text": ""}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                     "delta": {"type": "text_delta", "text": text}}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            ("message_delta", {"type": "message_delta",
                               "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                               "usage": {"output_tokens": 1}}),
            ("message_stop", {"type": "message_stop"}),
        ]
        data = "".join(f"event: {name}\ndata: {json.dumps(payload)}\n\n" for name, payload in events).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(data)))
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _claude_ai(self, path: str) -> None:
        with self.fake.lock:
            acct = self.fake.by_session(self._cookie_session())
        if acct is None:
            self._log(403, None)
            return self._json(403, {"error": {"type": "permission_error", "message": "Invalid session"}})
        self._log(200, acct)
        if path == "/api/organizations":
            return self._json(200, [{"uuid": acct.org_uuid, "name": acct.org_name,
                                     "capabilities": ["chat", "claude_max"]}])
        if path == "/api/account":
            return self._json(200, {"uuid": acct.uuid, "email_address": acct.email})
        org = path.split("/")[3]
        if org != acct.org_uuid:
            return self._json(404, {"error": {"type": "not_found_error"}})
        self._json(200, acct.subscription_details())

    # -- control ------------------------------------------------------------------

    def _find(self, key: str) -> Account | None:
        for acct in self.fake.accounts.values():
            if key in (acct.uuid, acct.email):
                return acct
        return None

    def _control_get(self, path: str) -> None:
        with self.fake.lock:
            if path == "/__control/log":
                return self._json(200, list(self.fake.log))
            if path == "/__control/accounts":
                return self._json(200, [a.state() for a in self.fake.accounts.values()])
            m = re.fullmatch(r"/__control/accounts/([^/]+)", path)
            acct = self._find(m.group(1)) if m else None
            if acct is None:
                return self._json(404, {"error": "no such account"})
            return self._json(200, acct.state())

    def _control_post(self, path: str, raw: bytes) -> None:
        try:
            req = json.loads(raw or b"{}")
        except ValueError:
            return self._json(400, {"error": "bad json"})
        with self.fake.lock:
            if path == "/__control/accounts":
                email = req.pop("email")
                acct = Account(email, **req)
                self.fake.accounts[acct.uuid] = acct
                return self._json(200, acct.state())
            m = re.fullmatch(r"/__control/accounts/([^/]+)(/login|/session)?", path)
            acct = self._find(m.group(1)) if m else None
            if acct is None:
                return self._json(404, {"error": "no such account"})
            if m.group(2) == "/login":
                tokens = acct.mint()
                return self._json(200, {
                    **tokens,
                    "scopes": SCOPES,
                    "uuid": acct.uuid,
                    "email": acct.email,
                    "orgUuid": acct.org_uuid,
                    "orgName": acct.org_name,
                    "tier": acct.tier,
                    "orgType": acct.org_type,
                })
            if m.group(2) == "/session":
                return self._json(200, {"sessionKey": acct.session_key()})
            try:
                acct.patch(req)
            except (KeyError, TypeError, ValueError) as e:
                return self._json(400, {"error": f"bad field: {e}"})
            return self._json(200, acct.state())


class FakeClaudeClient:
    """Drives a FakeClaude over its control API (works across processes)."""

    def __init__(self, url: str) -> None:
        self.url = url.rstrip("/")

    def _call(self, method: str, path: str, body: dict | None = None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            self.url + path, data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())

    def add_account(self, email: str, **fields) -> dict:
        return self._call("POST", "/__control/accounts", {"email": email, **fields})

    def login_tokens(self, account: str) -> dict:
        """Mint a new token family for ``account`` (uuid or email)."""
        return self._call("POST", f"/__control/accounts/{account}/login", {})

    def session_key(self, account: str) -> str:
        return self._call("POST", f"/__control/accounts/{account}/session", {})["sessionKey"]

    def set(self, account: str, **changes) -> dict:
        return self._call("POST", f"/__control/accounts/{account}", changes)

    def state(self, account: str) -> dict:
        return self._call("GET", f"/__control/accounts/{account}")

    def log(self) -> list[dict]:
        return self._call("GET", "/__control/log")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--log", type=Path)
    args = parser.parse_args()
    fake = FakeClaude(args.port, args.log)
    print(f"fake-claude listening on {fake.url}", flush=True)
    try:
        fake.server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
