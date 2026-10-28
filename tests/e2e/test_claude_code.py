"""The real Claude Code CLI uses the account aswap switched to.

Everywhere else Claude Code is simulated. Here the real ``claude`` runs
against the fake (``ANTHROPIC_BASE_URL``, with nonessential traffic off),
reads the login aswap installed, and the fake records whose bearer token
each ``/v1/messages`` call carried.

Opt-in: runs only when ``ASWAP_E2E_REAL_CLAUDE`` names a Claude Code
executable (the ``claude-code`` job in ``.github/workflows/e2e.yml`` installs
one). Claude Code is not part of aswap, so that job is allowed to fail.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from tests.e2e.fake_claude import token_id

REAL_CLAUDE = os.environ.get("ASWAP_E2E_REAL_CLAUDE")

pytestmark = pytest.mark.skipif(
    not REAL_CLAUDE, reason="set ASWAP_E2E_REAL_CLAUDE to a Claude Code executable"
)


def _claude(box, prompt: str) -> subprocess.CompletedProcess:
    env = dict(
        box.env,
        ANTHROPIC_BASE_URL=box.fake_server.url,
        CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
        DISABLE_AUTOUPDATER="1",
    )
    done = subprocess.run(
        [REAL_CLAUDE, "-p", prompt], env=env, cwd=box.home, capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=180, input="", check=False,
    )
    box.outputs += [done.stdout, done.stderr]
    box._note(f"\n$ claude -p {prompt!r}   [exit {done.returncode}]\n{done.stdout}\n"
              f"--- stderr ---\n{done.stderr}\n")
    return done


def _claude_messages(box) -> list[dict]:
    """``/v1/messages`` calls made by Claude Code (not aswap's own probe)."""
    return [r for r in box.fake.log()
            if r["path"] == "/v1/messages" and "claude-swap" not in (r.get("userAgent") or "")]


def test_claude_code_sends_the_switched_accounts_token(box):
    alice, bob = box.account("alice"), box.account("bob")
    for acct in (alice, bob):
        box.login(acct["email"])
        box.aswap("add")

    for num, acct in (("1", alice), ("2", bob), ("1", alice)):
        box.aswap("switch", num)
        assert box.live_email() == acct["email"]
        installed = token_id(box.live_credentials()["accessToken"])
        before = len(_claude_messages(box))

        done = _claude(box, "Reply with one word.")
        assert done.returncode == 0, f"claude -p exited {done.returncode}: {done.stderr[-1500:]}"
        assert f"fake reply for {acct['email']}" in done.stdout

        sent = _claude_messages(box)[before:]
        assert sent, "Claude Code sent no /v1/messages request to the fake"
        assert {r["account"] for r in sent} == {acct["email"]}
        assert {r["bearer"] for r in sent} == {installed}
