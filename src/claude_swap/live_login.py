"""What Claude Code's live login is, for adding it to the roster unasked.

Before 2026-10-27 a new ``/login`` sat outside aswap until the owner ran
``aswap add`` by hand. ``aswap auto`` now observes the live login every poll
and adds (or re-captures) it once the observation has held for two polls in a
row. This module is the observation: an identity out of ``~/.claude.json``'s
``oauthAccount`` paired with the shape of the live credential — never the
token itself, so an observation can be compared, kept between polls and
logged without carrying a secret.

Only a complete OAuth login qualifies (``kind == "oauth"``): both tokens
present and, when the credential lists scopes, ``user:profile`` among them.
A managed API key, a setup-token (inference scope only, no refresh token) and
Claude Code's wiped ``invalid_grant`` husk each name themselves instead, and
are never added. A credential file caught mid-``/login`` (tokens of one
account, ``oauthAccount`` of another) cannot be told apart locally; the
switcher's ownership probe against the profile endpoint settles that before
anything is written.
"""

from __future__ import annotations

from dataclasses import dataclass

from claude_swap import oauth
from claude_swap.credentials import looks_like_api_key

KIND_OAUTH = "oauth"
KIND_API_KEY = "api_key"
KIND_SETUP_TOKEN = "setup_token"
KIND_PARTIAL = "partial"


@dataclass(frozen=True)
class LiveLogin:
    """One observation of the live login. Equality is "nothing moved"."""

    email: str
    org_uuid: str
    account_uuid: str
    kind: str
    # Refresh-token hash (``oauth.credential_fingerprint``), never the token.
    fingerprint: str | None
    expires_at: float | None

    @property
    def identity(self) -> tuple[str, str]:
        return (self.email, self.org_uuid)


def credential_kind(credentials: str | None) -> str:
    """Classify a live credential blob (see the module docstring)."""
    if looks_like_api_key(credentials):
        return KIND_API_KEY
    data = oauth.extract_oauth_data(credentials or "")
    if not data:
        return KIND_PARTIAL
    access, refresh = data.get("accessToken"), data.get("refreshToken")
    if not (isinstance(access, str) and access):
        return KIND_PARTIAL
    scopes = data.get("scopes")
    if isinstance(scopes, list) and "user:profile" not in scopes:
        return KIND_SETUP_TOKEN
    if not (isinstance(refresh, str) and refresh):
        return KIND_SETUP_TOKEN
    return KIND_OAUTH


def observe(oauth_account: object, credentials: str | None) -> LiveLogin | None:
    """The observation for an ``oauthAccount`` object and the live credential.

    None when there is no identity to observe (no ``oauthAccount``, no email).
    """
    if not isinstance(oauth_account, dict):
        return None
    email = oauth_account.get("emailAddress")
    if not isinstance(email, str) or not email:
        return None
    org = oauth_account.get("organizationUuid")
    account_uuid = oauth_account.get("accountUuid")
    data = oauth.extract_oauth_data(credentials or "") or {}
    expires_at = data.get("expiresAt")
    return LiveLogin(
        email=email,
        org_uuid=org if isinstance(org, str) else "",
        account_uuid=account_uuid if isinstance(account_uuid, str) else "",
        kind=credential_kind(credentials),
        fingerprint=oauth.credential_fingerprint(credentials or ""),
        expires_at=(
            float(expires_at)
            if isinstance(expires_at, (int, float)) and not isinstance(expires_at, bool)
            else None
        ),
    )


def owner_matches(resolved: dict, seen: LiveLogin) -> bool:
    """Whether the profile endpoint's answer for the live token is ``seen``.

    ``resolved`` is a ``fetch_oauth_profile`` result. The email must agree
    (case-insensitively) whenever the profile reports one; the account uuid
    and organization must agree whenever both sides carry one. A profile with
    neither an email nor a uuid that ``seen`` can be checked against proves
    nothing and does not match.
    """
    r_email = resolved.get("email")
    r_uuid = (resolved.get("uuid") or "").strip()
    r_org = resolved.get("organizationUuid")
    if isinstance(r_email, str) and r_email:
        if r_email.casefold() != seen.email.casefold():
            return False
        email_ok = True
    else:
        email_ok = False
    if r_uuid and seen.account_uuid:
        if r_uuid != seen.account_uuid:
            return False
        uuid_ok = True
    else:
        uuid_ok = False
    if isinstance(r_org, str) and r_org and seen.org_uuid and r_org != seen.org_uuid:
        return False
    return email_ok or uuid_ok
