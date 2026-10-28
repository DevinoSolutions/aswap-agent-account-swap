"""Where aswap sends its HTTP requests, and the test-only clock scale.

Every Anthropic host aswap talks to is fixed by default. Two environment
variables replace them, for running aswap against a local stand-in server
(the end-to-end suite in ``tests/e2e`` does exactly this):

``ASWAP_API_BASE_URL``
    Replaces both ``https://api.anthropic.com`` (usage, profile, the
    rate-limit gate probe) and ``https://platform.claude.com`` (the OAuth
    token endpoint). One server answers all of them.
``ASWAP_CLAUDE_AI_BASE_URL``
    Replaces ``https://claude.ai`` (the ``aswap billing`` web-session
    requests).

A value must be an ``https://`` URL, or an ``http://`` URL whose host is a
loopback name (``localhost``, ``127.0.0.1``, ``::1``). These requests carry
bearer tokens and session cookies, so plain HTTP to anything that leaves the
machine is refused rather than quietly used. A refused value raises
:class:`EndpointOverrideError`; the CLI reports it and exits before any
request is made, so a typo never falls back to sending a token somewhere else.

``ASWAP_TIME_SCALE`` is test-only: a factor in ``(0, 1]`` applied to aswap's
polling and throttling intervals (usage cadence, gate probe throttle, plan
re-checks, engine ticks), so a test can watch ``aswap auto`` react in seconds
instead of minutes on a real clock. It is honoured only while
``ASWAP_API_BASE_URL`` points at a loopback host: against the real API a
smaller interval would only burn the usage endpoint's request budget.
Decision thresholds (runway, burn and recovery margins) are not scaled.
"""

from __future__ import annotations

import ipaddress
import os
import urllib.parse

API_BASE_ENV = "ASWAP_API_BASE_URL"
CLAUDE_AI_BASE_ENV = "ASWAP_CLAUDE_AI_BASE_URL"
TIME_SCALE_ENV = "ASWAP_TIME_SCALE"

DEFAULT_API_BASE = "https://api.anthropic.com"
DEFAULT_OAUTH_BASE = "https://platform.claude.com"
DEFAULT_CLAUDE_AI_BASE = "https://claude.ai"

_LOOPBACK_NAMES = frozenset({"localhost"})


class EndpointOverrideError(ValueError):
    """An endpoint override names a URL aswap refuses to send credentials to."""


def _is_loopback(host: str) -> bool:
    if host.lower() in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _parse(name: str, raw: str) -> urllib.parse.SplitResult:
    try:
        parts = urllib.parse.urlsplit(raw.strip())
        parts.port  # noqa: B018 - raises ValueError on a malformed port
    except ValueError:
        raise EndpointOverrideError(f"{name}={raw!r} is not a valid URL") from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise EndpointOverrideError(f"{name}={raw!r} is not an http(s) URL with a host")
    if parts.query or parts.fragment or parts.username or parts.password:
        raise EndpointOverrideError(
            f"{name}={raw!r} must be a base URL (no credentials, query or fragment)"
        )
    if parts.scheme == "http" and not _is_loopback(parts.hostname):
        raise EndpointOverrideError(
            f"{name}={raw!r} refused: plain http is only allowed to localhost, "
            "127.0.0.1 or ::1 (requests carry account tokens)"
        )
    return parts


def _override(name: str) -> str | None:
    """The validated base URL from ``name`` (no trailing slash), or None."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    _parse(name, raw)
    return raw.strip().rstrip("/")


def api_base() -> str:
    """Base URL for ``api.anthropic.com`` requests."""
    return _override(API_BASE_ENV) or DEFAULT_API_BASE


def oauth_base() -> str:
    """Base URL for the OAuth token endpoint (``platform.claude.com``)."""
    return _override(API_BASE_ENV) or DEFAULT_OAUTH_BASE


def claude_ai_base() -> str:
    """Base URL for ``claude.ai`` web requests."""
    return _override(CLAUDE_AI_BASE_ENV) or DEFAULT_CLAUDE_AI_BASE


def validate_environment() -> None:
    """Raise :class:`EndpointOverrideError` if any override is unusable."""
    _override(API_BASE_ENV)
    _override(CLAUDE_AI_BASE_ENV)


def _api_is_loopback() -> bool:
    raw = os.environ.get(API_BASE_ENV)
    if not raw or not raw.strip():
        return False
    try:
        host = _parse(API_BASE_ENV, raw).hostname
    except EndpointOverrideError:
        return False
    return bool(host) and _is_loopback(host)


def time_scale() -> float:
    """The test-only interval factor: 1.0 unless explicitly enabled.

    ``ASWAP_TIME_SCALE`` counts only with a loopback ``ASWAP_API_BASE_URL``,
    and only inside ``(0, 1]`` — aswap never polls the real API faster, and a
    test cannot make it poll slower than production by accident.
    """
    raw = os.environ.get(TIME_SCALE_ENV)
    if not raw or not _api_is_loopback():
        return 1.0
    try:
        value = float(raw)
    except ValueError:
        return 1.0
    if not 0.0 < value <= 1.0:
        return 1.0
    return value


def scaled(seconds: float) -> float:
    """``seconds`` times :func:`time_scale` (unchanged outside tests)."""
    return seconds * time_scale()
