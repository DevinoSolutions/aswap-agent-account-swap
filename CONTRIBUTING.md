# Contributing

## Setup

```bash
git clone https://github.com/DevinoSolutions/aswap-agent-account-swap
cd aswap-agent-account-swap
uv sync            # Python 3.12+, dev dependencies from the lockfile
uv run aswap --help
```

## Unit tests

```bash
uv run pytest
```

Runs in parallel (`pytest-xdist`, `-n auto`). The unit suite never touches your real home directory, Keychain or the network: autouse fixtures in `tests/conftest.py` redirect `HOME`, replace the Keychain with an in-memory store and stub the network calls.

## End-to-end tests

The e2e tier (`tests/e2e/`) runs the real `aswap` program as separate processes against a stand-in Claude server, so it catches what mocks cannot: the packaged entry point, real files and SQLite under concurrent processes, hard kills, real HTTP, real clocks, and on macOS the real Keychain. It is excluded from the default run.

```bash
uv run pytest -m e2e tests/e2e -n 4
```

About a minute on a laptop. Each test gets a throwaway `HOME` (and `USERPROFILE`/`APPDATA` on Windows) and its own fake server on a random loopback port, so tests run in parallel and never see your real accounts.

### What is real and what is simulated

| Real | Simulated |
|---|---|
| The `aswap` executable and its subprocesses (`aswap auto` runs in the background while the test calls the CLI) | Claude's HTTP API: `tests/e2e/fake_claude.py` serves usage, profile, OAuth token refresh (with rotation), the `/v1/messages` rate-limit probe and the claude.ai billing endpoints |
| Files under `HOME`, `state.db` (SQLite), file locks, atomic writes | Claude Code's `/login`: `Box.login` writes `~/.claude/.credentials.json` (or the macOS Keychain item) and `~/.claude.json`'s `oauthAccount` in separate writes, the way Claude Code does |
| The wall clock, sleeps, signals, `kill -9` | The `claude` CLI: `tests/e2e/claude_stub.py` is first on PATH and answers `claude auth status` from the profile's `.claude.json`, so a real Claude Code never runs against a test HOME |
| Sockets and HTTP (aswap's own client, TLS settings aside) | |
| The macOS Keychain (`security`), a temporary keychain in CI | |

Nothing inside aswap is patched. Two documented environment variables make this possible:

- `ASWAP_API_BASE_URL` / `ASWAP_CLAUDE_AI_BASE_URL` point aswap at the fake (see the README's *Endpoint overrides*). Plain `http://` is accepted only for loopback hosts.
- `ASWAP_TIME_SCALE` (test-only) multiplies aswap's polling and throttle intervals, for example `0.02` turns the 180 s usage floor into 3.6 s. It is ignored unless `ASWAP_API_BASE_URL` is a loopback address, and it never shortens the burn-rate, runway or reset arithmetic, which stays on the real clock.

### Knobs

| Variable | Default | Meaning |
|---|---|---|
| `ASWAP_E2E_BIN` | unset | The `aswap` executable to test (CI sets it to the wheel installed with `uv tool install`). Unset runs `python -m claude_swap` from this checkout. |
| `ASWAP_E2E_ARTIFACTS` | `.e2e-artifacts/` | Where each test's transcript, `aswap auto` output, aswap's logs and the fake's request log go. Kept only for failed tests. |
| `ASWAP_E2E_KEEP` | unset | Keep the artifacts of passing tests too. |
| `ASWAP_E2E_TIME_SCALE` | `0.02` | The `ASWAP_TIME_SCALE` the tests run aswap with. |

To test the packaged program the way CI does:

```bash
uv build
uv tool install --force dist/*.whl
ASWAP_E2E_BIN="$(uv tool dir --bin)/aswap" uv run pytest -m e2e tests/e2e -n 4
```

The fake's request log (`fake-requests.jsonl`) records method, path, status and which account a request was for. Tokens appear only as short hashes (`token_id`). Every passing test also checks that no token or session key the fake issued shows up in aswap's output or logs.

### macOS

On macOS, aswap and Claude Code keep credentials in the Keychain, and the e2e tests use the real one. `security` locates the user's keychains through `$HOME`, so each test creates its own unlocked keychain under its temporary HOME and registers it there as the default; every `aswap` process the test starts uses that keychain and nothing else. Your login keychain is never read or written, and the test keychain is deleted afterwards.

### Running the fake on its own

```bash
uv run python tests/e2e/fake_claude.py --port 8765 --log requests.jsonl
curl -s -X POST localhost:8765/__control/accounts -d '{"email":"me@example.test"}'
curl -s -X POST localhost:8765/__control/accounts/me@example.test/login   # tokens for a credentials file
curl -s -X POST localhost:8765/__control/accounts/me@example.test -d '{"five_hour": 95}'
ASWAP_API_BASE_URL=http://127.0.0.1:8765 aswap list
```

`/__control/...` creates accounts, issues logins, changes usage, gate, plan, token expiry and 429 budgets, and returns state (see `FakeClaude` in the file for every field).

## CI

- `CI` (`ci.yml`): unit tests on Linux, Windows and macOS × Python 3.12/3.13, plus a build-and-smoke of the wheel.
- `E2E` (`e2e.yml`): builds the wheel, installs it with `uv tool install` and runs `pytest -m e2e` against it on all three OSes, on every PR, on pushes to `main`, nightly and on demand. On failure the per-test artifacts are uploaded.
