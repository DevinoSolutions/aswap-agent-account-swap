"""Gmail and Outlook mailboxes for the login inbox, over read-only OAuth.

Each asks for the narrowest scope that still shows a message body (the
sign-in link is in the body): Gmail ``gmail.readonly``; Microsoft Graph
``Mail.Read`` plus ``offline_access`` (a refresh token) and ``openid
email`` (to learn which address signed in). Neither can send, delete or
change mail or settings.

Both providers need an OAuth client of your own, created once:

* Google: a Google Cloud project with the Gmail API enabled and an OAuth
  client of type "Desktop app"; pass its JSON with ``--client-file``.
* Microsoft: an Entra app registration with the "Mobile and desktop
  applications" platform, redirect URI ``http://localhost`` and the
  delegated Graph permission ``Mail.Read``; pass its ID with ``--client-id``.

The sign-in runs in your default browser and comes back to a one-shot
server on 127.0.0.1 (authorization code + PKCE). Later mailboxes reuse the
client of the first.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

from claude_swap.inbox import (
    SENDER_DOMAIN,
    InboxError,
    from_anthropic,
    load_sources,
    message_links,
    pick_link,
    text_links,
    update_source,
)

GOOGLE_AUTHORIZE = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"

MS_LOGIN = "https://login.microsoftonline.com"
MS_SCOPE = "offline_access openid email https://graph.microsoft.com/Mail.Read"
GRAPH = "https://graph.microsoft.com/v1.0/me"

# Recent enough to hold a link requested just now; the snapshot taken before
# Continue keeps anything older than the request out.
_GMAIL_QUERY = f'(from:{SENDER_DOMAIN} OR subject:"secure link to Claude") newer_than:1d'


# --- HTTP ----------------------------------------------------------------------


def _request(url: str, *, data: dict | None = None, token: str | None = None,
             headers: dict | None = None, timeout: float = 30.0) -> dict:
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method="POST" if body else "GET")
    req.add_header("Accept", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8", errors="replace"))
        except ValueError:
            detail = {}
        err = detail.get("error")
        if isinstance(err, dict):  # Graph / Gmail API: {"error": {"code", "message"}}
            err = err.get("code") or err.get("status") or err.get("message")
        if err == "invalid_grant":
            raise InboxError("access was revoked or has expired — connect the mailbox again") from None
        raise InboxError(f"HTTP {e.code}: {err or e.reason}") from None
    except (OSError, ValueError) as e:
        raise InboxError(f"request failed: {e}") from None


# --- sign-in -------------------------------------------------------------------


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _authorize(build_url, *, host: str, out, open_browser=webbrowser.open, timeout_s: float = 300.0):
    """Run the browser half of the code flow; returns ``(code, redirect_uri, verifier)``."""
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(24)
    got: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if params.get("state", [""])[0] != state:
                self.send_response(400)
                self.end_headers()
                return
            if "code" in params:
                got["code"] = params["code"][0]
                message = b"<h2>Mailbox connected</h2><p>You can close this tab.</p>"
            else:
                got["error"] = params.get("error_description", params.get("error", ["unknown"]))[0]
                message = b"<h2>Mailbox not connected</h2><p>See the terminal.</p>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><body>" + message + b"</body></html>")

        def log_message(self, fmt, *a) -> None:
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    srv.timeout = 1.0
    redirect_uri = f"http://{host}:{srv.server_address[1]}"
    url = build_url(redirect_uri, challenge, state)
    out("Sign in and allow read-only mail access in the browser window that opened.")
    out(f"  (if none opened: {url})")
    open_browser(url)
    deadline = time.monotonic() + timeout_s
    try:
        while not got and time.monotonic() < deadline:
            srv.handle_request()
    finally:
        srv.server_close()
    if "error" in got:
        raise InboxError(f"sign-in refused: {got['error']}")
    if "code" not in got:
        raise InboxError(f"no sign-in within {int(timeout_s)}s")
    return got["code"], redirect_uri, verifier


def _google_client(root: Path, args) -> tuple[str, str]:
    if args.client_file:
        try:
            raw = json.loads(Path(args.client_file).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise InboxError(f"cannot read {args.client_file}: {e}") from None
        block = raw.get("installed") or raw.get("web") or raw
        cid, secret = block.get("client_id"), block.get("client_secret")
    else:
        cid, secret = args.client_id, args.client_secret
    if not cid:
        prior = next((r for r in load_sources(root) if r["kind"] == "google"), None)
        if prior:
            cid, secret = prior.get("clientId"), prior.get("clientSecret")
    if not (cid and secret):
        raise InboxError(
            "a Google OAuth client is needed: create a Desktop-app client in Google Cloud "
            "(Gmail API enabled) and pass --client-file client_secret.json"
        )
    return cid, secret


def _ms_client(root: Path, args) -> tuple[str, str]:
    cid, tenant = args.client_id, args.tenant
    prior = next((r for r in load_sources(root) if r["kind"] == "outlook"), None)
    if not cid and prior:
        cid = prior.get("clientId")
        tenant = tenant or prior.get("tenant")
    if not cid:
        raise InboxError(
            "a Microsoft app registration is needed: register a public client app in "
            "Entra (redirect http://localhost, delegated Mail.Read) and pass --client-id"
        )
    return cid, tenant or "common"


def _id_token_email(id_token: str | None) -> str | None:
    try:
        payload = id_token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (AttributeError, IndexError, ValueError):
        return None
    for key in ("email", "preferred_username", "upn"):
        if isinstance(claims.get(key), str) and "@" in claims[key]:
            return claims[key]
    return None


def connect(root: Path, args, *, out=print, open_browser=webbrowser.open) -> dict:
    """Sign in to a Gmail (``args.kind == "google"``) or Outlook mailbox;
    returns the row to store."""
    hint = getattr(args, "hint", None)
    if args.kind == "google":
        cid, secret = _google_client(root, args)

        def url(redirect_uri, challenge, state):
            q = {
                "client_id": cid, "redirect_uri": redirect_uri, "response_type": "code",
                "scope": GOOGLE_SCOPE, "code_challenge": challenge,
                "code_challenge_method": "S256", "state": state,
                "access_type": "offline", "prompt": "consent",
            }
            if hint:
                q["login_hint"] = hint
            return f"{GOOGLE_AUTHORIZE}?{urllib.parse.urlencode(q)}"

        code, redirect_uri, verifier = _authorize(url, host="127.0.0.1", out=out, open_browser=open_browser)
        tokens = _request(GOOGLE_TOKEN, data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
            "client_id": cid, "client_secret": secret, "code_verifier": verifier,
        })
        if not tokens.get("refresh_token"):
            raise InboxError("Google returned no refresh token")
        address = _request(f"{GMAIL}/profile", token=tokens["access_token"]).get("emailAddress")
        if not address:
            raise InboxError("Gmail did not report the mailbox address")
        return {
            "kind": "google", "address": address, "clientId": cid, "clientSecret": secret,
            "refreshToken": tokens["refresh_token"],
        }

    cid, tenant = _ms_client(root, args)

    def url(redirect_uri, challenge, state):
        q = {
            "client_id": cid, "redirect_uri": redirect_uri, "response_type": "code",
            "response_mode": "query", "scope": MS_SCOPE, "code_challenge": challenge,
            "code_challenge_method": "S256", "state": state, "prompt": "select_account",
        }
        if hint:
            q["login_hint"] = hint
        return f"{MS_LOGIN}/{tenant}/oauth2/v2.0/authorize?{urllib.parse.urlencode(q)}"

    code, redirect_uri, verifier = _authorize(url, host="localhost", out=out, open_browser=open_browser)
    tokens = _request(f"{MS_LOGIN}/{tenant}/oauth2/v2.0/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
        "client_id": cid, "scope": MS_SCOPE, "code_verifier": verifier,
    })
    if not tokens.get("refresh_token"):
        raise InboxError("Microsoft returned no refresh token")
    address = _id_token_email(tokens.get("id_token")) or hint
    if not address:
        raise InboxError("Microsoft did not report the mailbox address; pass --hint EMAIL")
    return {
        "kind": "outlook", "address": address, "clientId": cid, "tenant": tenant,
        "refreshToken": tokens["refresh_token"],
    }


# --- sources -------------------------------------------------------------------


class _ApiSource:
    """Shared token handling: one access token per run, refreshed as needed;
    a rotated refresh token is written back at once."""

    def __init__(self, root: Path, row: dict) -> None:
        self.root, self.row, self.address = root, dict(row), row["address"]
        self._token: str | None = None
        self._expires = 0.0
        self.access_token()  # fail now, not after Continue

    def _refresh(self) -> dict:
        raise NotImplementedError

    def access_token(self) -> str:
        if self._token is None or time.monotonic() > self._expires - 60:
            tokens = self._refresh()
            self._token = tokens["access_token"]
            self._expires = time.monotonic() + float(tokens.get("expires_in") or 3600)
            rotated = tokens.get("refresh_token")
            if rotated and rotated != self.row.get("refreshToken"):
                self.row["refreshToken"] = rotated
                update_source(self.root, self.row["kind"], self.address, refreshToken=rotated)
        return self._token

    def close(self) -> None:
        pass


class GmailSource(_ApiSource):
    def _refresh(self) -> dict:
        return _request(GOOGLE_TOKEN, data={
            "grant_type": "refresh_token", "refresh_token": self.row.get("refreshToken", ""),
            "client_id": self.row.get("clientId", ""), "client_secret": self.row.get("clientSecret", ""),
        })

    def _ids(self) -> list[str]:
        q = urllib.parse.urlencode({"q": _GMAIL_QUERY, "maxResults": 20, "includeSpamTrash": "true"})
        listing = _request(f"{GMAIL}/messages?{q}", token=self.access_token())
        return [m["id"] for m in listing.get("messages") or [] if isinstance(m, dict) and m.get("id")]

    def snapshot(self) -> set[str]:
        return set(self._ids())

    def find_link(self, account_email: str, seen: set[str]) -> str | None:
        for mid in self._ids():  # newest first
            if mid in seen:
                continue
            msg = _request(f"{GMAIL}/messages/{mid}?format=raw", token=self.access_token())
            raw = msg.get("raw")
            if not isinstance(raw, str):
                continue
            data = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
            if not from_anthropic(data):
                continue
            link = pick_link(message_links(data), account_email)
            if link:
                return link
        return None


class OutlookSource(_ApiSource):
    def _refresh(self) -> dict:
        tenant = self.row.get("tenant") or "common"
        return _request(f"{MS_LOGIN}/{tenant}/oauth2/v2.0/token", data={
            "grant_type": "refresh_token", "refresh_token": self.row.get("refreshToken", ""),
            "client_id": self.row.get("clientId", ""), "scope": MS_SCOPE,
        })

    def _recent(self) -> list[dict]:
        q = urllib.parse.urlencode({
            "$top": 25, "$orderby": "receivedDateTime desc", "$select": "id,from,body",
        })
        listing = _request(
            f"{GRAPH}/messages?{q}", token=self.access_token(),
            headers={"Prefer": 'outlook.body-content-type="html"'},
        )
        return [m for m in listing.get("value") or [] if isinstance(m, dict) and m.get("id")]

    def snapshot(self) -> set[str]:
        return {m["id"] for m in self._recent()}

    def find_link(self, account_email: str, seen: set[str]) -> str | None:
        for msg in self._recent():  # newest first
            if msg["id"] in seen:
                continue
            sender = json.dumps(msg.get("from") or {})
            body = str((msg.get("body") or {}).get("content") or "")
            if not (from_anthropic(sender) or from_anthropic(body)):
                continue
            link = pick_link(text_links(body), account_email)
            if link:
                return link
        return None


def open_api_source(root: Path, row: dict):
    return GmailSource(root, row) if row["kind"] == "google" else OutlookSource(root, row)
