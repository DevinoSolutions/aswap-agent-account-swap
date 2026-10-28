"""A stand-in for the ``claude`` CLI, put first on PATH in every e2e box.

aswap shells out to Claude Code in a few places (``claude auth status`` to
verify a session profile, ``claude`` to launch one). The e2e tier simulates
Claude Code, so the real CLI never runs against a test HOME and the tests do
not depend on whether it is installed. This answers the one question aswap
asks of it, ``auth status --json``, the way Claude Code does: from
``oauthAccount`` in ``$CLAUDE_CONFIG_DIR/.claude.json`` (or ``~/.claude.json``),
locally, with no network call. Anything else exits 2 and says so.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _config_path() -> Path:
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        return Path(config_dir) / ".claude.json"
    return Path.home() / ".claude.json"


def _auth_status() -> int:
    try:
        config = json.loads(_config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        config = {}
    account = config.get("oauthAccount") if isinstance(config, dict) else None
    if not isinstance(account, dict) or not account.get("emailAddress"):
        print(json.dumps({"loggedIn": False, "authMethod": "none"}))
        return 1
    print(json.dumps({
        "loggedIn": True,
        "authMethod": "claude.ai",
        "email": account.get("emailAddress"),
        "orgId": account.get("organizationUuid"),
        "orgName": account.get("organizationName"),
    }))
    return 0


def main(argv: list[str]) -> int:
    args = [a for a in argv if a != "--safe-mode"]
    if args[:2] == ["auth", "status"]:
        return _auth_status()
    if args[:1] == ["--version"]:
        print("0.0.0 (aswap e2e stub)")
        return 0
    print(f"aswap e2e claude stub: unsupported command {argv!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
