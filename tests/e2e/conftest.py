"""Harness for the end-to-end tier: the real ``aswap`` binary, a fake Claude.

What is real here: the ``aswap`` executable (in CI the built wheel, installed
with ``uv tool install``; set ``ASWAP_E2E_BIN``), its subprocesses, the
filesystem under a throwaway HOME, SQLite, the locks, the wall clock, sockets,
and on macOS the Keychain (a temporary default keychain in CI). What is
simulated: Claude's HTTP responses (``fake_claude.py``) and Claude Code's
``/login``, which :meth:`Box.login` performs by writing the same files Claude
Code writes.

Nothing in aswap is patched. The one concession to test speed is
``ASWAP_TIME_SCALE`` (documented in ``claude_swap/endpoints.py``), which
shortens aswap's polling intervals while it talks to a loopback server.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from tests.e2e.fake_claude import FakeClaude, FakeClaudeClient

E2E_DIR = Path(__file__).resolve().parent
REPO = E2E_DIR.parent.parent
# Polling intervals x this while aswap talks to the fake (see endpoints.py):
# the 180s usage floor becomes 3.6s, the 10-minute gate throttle 12s.
TIME_SCALE = float(os.environ.get("ASWAP_E2E_TIME_SCALE", "0.02"))
KEYCHAIN_SERVICE = "Claude Code-credentials"


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items):
    for item in items:
        if E2E_DIR in Path(str(item.fspath)).resolve().parents:
            item.add_marker(pytest.mark.e2e)


def aswap_command() -> list[str]:
    """How to start aswap: the installed binary when ``ASWAP_E2E_BIN`` names
    one (CI), else this checkout's package under the current interpreter."""
    exe = os.environ.get("ASWAP_E2E_BIN")
    if exe:
        resolved = shutil.which(exe) or exe
        if not Path(resolved).is_file():
            raise pytest.UsageError(f"ASWAP_E2E_BIN={exe!r} is not an executable file")
        return [resolved]
    return [sys.executable, "-m", "claude_swap"]


def artifacts_root() -> Path:
    root = Path(os.environ.get("ASWAP_E2E_ARTIFACTS") or (REPO / ".e2e-artifacts"))
    root.mkdir(parents=True, exist_ok=True)
    return root


class AswapFailed(AssertionError):
    pass


class AutoProc:
    """A background ``aswap auto --json`` and the events it has printed."""

    def __init__(self, box: Box, args: list[str], name: str) -> None:
        self.box = box
        self.out_path = box.artifacts / f"{name}.stdout.jsonl"
        self.err_path = box.artifacts / f"{name}.stderr.log"
        self._out = self.out_path.open("wb")
        self._err = self.err_path.open("wb")
        kwargs: dict = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        self.proc = subprocess.Popen(
            [*aswap_command(), "auto", "--json", *args],
            env=box.env, cwd=box.home, stdout=self._out, stderr=self._err,
            stdin=subprocess.DEVNULL, **kwargs,
        )

    def events(self) -> list[dict]:
        out = []
        try:
            text = self.out_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return out
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass  # a line still being written
        return out

    def switches(self) -> list[dict]:
        return [e for e in self.events() if e.get("event") == "switch"]

    def wait_for(self, pred: Callable[[list[dict]], object], timeout: float = 60.0,
                 what: str = "condition") -> object:
        deadline = time.monotonic() + timeout
        while True:
            events = self.events()
            got = pred(events)
            if got:
                return got
            if self.proc.poll() is not None:
                raise AssertionError(
                    f"aswap auto exited ({self.proc.returncode}) waiting for {what}; "
                    f"stderr: {self.err_path.read_text(errors='replace')[-2000:]}"
                )
            if time.monotonic() > deadline:
                raise AssertionError(
                    f"timed out after {timeout}s waiting for {what}; last events: "
                    + json.dumps(events[-8:], indent=1)
                )
            time.sleep(0.25)

    def wait_switch(self, to: str, timeout: float = 60.0, after: int = 0) -> dict:
        def pred(events):
            sw = [e for e in events if e.get("event") == "switch"][after:]
            return next((e for e in sw if str((e.get("to") or {}).get("number")) == str(to)), None)
        return self.wait_for(pred, timeout, f"a switch to {to}")

    def stop(self) -> int:
        """Graceful stop: SIGTERM (aswap's own handler) or Ctrl-Break."""
        if self.proc.poll() is None:
            if sys.platform == "win32":
                self.proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                self.proc.terminate()
            try:
                self.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=20)
        self._close()
        return self.proc.returncode

    def kill(self) -> None:
        """Hard kill: SIGKILL / TerminateProcess, no cleanup runs."""
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=20)
        self._close()

    def _close(self) -> None:
        for f in (self._out, self._err):
            try:
                f.close()
            except OSError:
                pass


class Box:
    """One isolated machine: its own HOME, a fake Claude, the aswap binary."""

    def __init__(self, root: Path, fake: FakeClaude, artifacts: Path) -> None:
        self.root = root
        self.home = root / "home"
        self.home.mkdir(parents=True)
        (self.home / ".claude").mkdir()
        self.fake_server = fake
        self.fake = FakeClaudeClient(fake.url)
        self.artifacts = artifacts
        self.tag = secrets.token_hex(3)
        self.transcript = artifacts / "transcript.log"
        self.autos: list[AutoProc] = []
        self.outputs: list[str] = []
        appdata = self.home / "AppData"
        env = {
            k: v for k, v in os.environ.items()
            if not k.startswith(("ASWAP_", "CLAUDE_", "XDG_", "ANTHROPIC_"))
        }
        env.update({
            "HOME": str(self.home),
            "USERPROFILE": str(self.home),
            "APPDATA": str(appdata / "Roaming"),
            "LOCALAPPDATA": str(appdata / "Local"),
            "ASWAP_API_BASE_URL": fake.url,
            "ASWAP_CLAUDE_AI_BASE_URL": fake.url,
            "ASWAP_TIME_SCALE": str(TIME_SCALE),
            "NO_COLOR": "1",
            "TERM": "dumb",
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            # The macOS Keychain item Claude Code writes is keyed by $USER;
            # a per-box user keeps parallel tests out of each other's item.
            "USER": f"e2e-{self.tag}",
        })
        if sys.platform != "win32":
            env.pop("APPDATA")
            env.pop("LOCALAPPDATA")
        # Claude Code is simulated: a stub `claude` shadows any real one.
        stub_bin = self._install_claude_stub(root / "bin")
        env["PATH"] = os.pathsep.join([str(stub_bin), env.get("PATH", "")])
        self.env = env
        self.keychain: Path | None = None
        if sys.platform == "darwin":
            self._create_keychain()

    @staticmethod
    def _install_claude_stub(bin_dir: Path) -> Path:
        bin_dir.mkdir()
        stub = E2E_DIR / "claude_stub.py"
        if sys.platform == "win32":
            (bin_dir / "claude.cmd").write_text(
                f'@"{sys.executable}" "{stub}" %*\r\n', encoding="utf-8")
        else:
            launcher = bin_dir / "claude"
            launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{stub}" "$@"\n',
                                encoding="utf-8")
            launcher.chmod(0o755)
        return bin_dir

    # -- paths ------------------------------------------------------------------

    @property
    def backup_root(self) -> Path:
        if sys.platform.startswith("linux"):
            return self.home / ".local" / "share" / "claude-swap"
        return self.home / ".claude-swap-backup"

    @property
    def credentials_path(self) -> Path:
        return self.home / ".claude" / ".credentials.json"

    @property
    def config_path(self) -> Path:
        return self.home / ".claude.json"

    def email(self, name: str) -> str:
        """Per-box unique address (keychain items are keyed by email)."""
        return f"{name}-{self.tag}@e2e.test"

    # -- running aswap ------------------------------------------------------------

    def _note(self, text: str) -> None:
        with self.transcript.open("a", encoding="utf-8") as f:
            f.write(text)

    def aswap(self, *args: str, check: bool = True, timeout: float = 90.0,
              input: str | None = None) -> subprocess.CompletedProcess:
        cmd = [*aswap_command(), *args]
        started = time.monotonic()
        done = subprocess.run(
            cmd, env=self.env, cwd=self.home, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
            input=input if input is not None else "", check=False,
        )
        took = time.monotonic() - started
        self.outputs += [done.stdout, done.stderr]
        self._note(
            f"\n$ aswap {' '.join(args)}   [exit {done.returncode}, {took:.1f}s]\n"
            f"{done.stdout}{('--- stderr ---' + chr(10) + done.stderr) if done.stderr else ''}"
        )
        if check and done.returncode != 0:
            raise AswapFailed(
                f"aswap {' '.join(args)} exited {done.returncode}\n"
                f"stdout:\n{done.stdout}\nstderr:\n{done.stderr}"
            )
        return done

    def aswap_json(self, *args: str, timeout: float = 90.0) -> object:
        done = self.aswap(*args, timeout=timeout)
        return json.loads(done.stdout)

    def configure(self, **settings: object) -> None:
        """``aswap config set autoswitch.<key> <value>`` for each pair."""
        for key, value in settings.items():
            if isinstance(value, bool):
                value = "true" if value else "false"
            self.aswap("config", "set", f"autoswitch.{key}", str(value))

    def start_auto(self, *args: str, name: str | None = None) -> AutoProc:
        proc = AutoProc(self, list(args), name or f"auto{len(self.autos) + 1}")
        self.autos.append(proc)
        return proc

    # -- the account as Claude knows it -----------------------------------------

    def account(self, name: str, **fields) -> dict:
        """Create an account on the fake; returns its state (uuid, email...)."""
        return self.fake.add_account(self.email(name), **fields)

    def login(self, email: str, *, pause: float = 0.0) -> dict:
        """Simulate Claude Code's ``/login`` for ``email``; returns the tokens.

        Claude Code finishes a login with two separate writes: the account
        info into ``~/.claude.json`` (``oauthAccount``, other keys kept) and
        the OAuth tokens into secure storage (``~/.claude/.credentials.json``,
        the macOS Keychain on darwin). They are not one atomic step, which is
        exactly what aswap's auto-add has to cope with; ``pause`` widens the
        gap between them.
        """
        t = self.fake.login_tokens(email)
        try:
            config = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            config = {
                "numStartups": 1,
                "installMethod": "native",
                "autoUpdates": False,
                "userID": secrets.token_hex(32),
                "hasCompletedOnboarding": True,
                "lastOnboardingVersion": "2.1.0",
                "projects": {},
            }
        config["oauthAccount"] = {
            "accountUuid": t["uuid"],
            "emailAddress": t["email"],
            "organizationUuid": t["orgUuid"],
            "hasExtraUsageEnabled": False,
            "billingType": "stripe_subscription",
            "displayName": t["email"].split("@")[0],
            "organizationRole": "admin",
            "workspaceRole": None,
            "organizationName": t["orgName"],
        }
        _atomic_write(self.config_path, json.dumps(config, indent=2))
        if pause:
            time.sleep(pause)
        blob = json.dumps({
            "claudeAiOauth": {
                "accessToken": t["accessToken"],
                "refreshToken": t["refreshToken"],
                "expiresAt": t["expiresAt"],
                "scopes": t["scopes"],
                "subscriptionType": "max",
                "rateLimitTier": t["tier"],
            }
        })
        self.write_live_credentials(blob)
        return t

    def write_live_credentials(self, blob: str, service: str = KEYCHAIN_SERVICE) -> None:
        if sys.platform == "darwin":
            self.security("add-generic-password", "-U", "-a", self.env["USER"],
                          "-s", service, "-X", blob.encode().hex())
        else:
            _atomic_write(self.credentials_path, blob)

    def security(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        """``/usr/bin/security`` as this box's user (its HOME, so its own
        keychain). Errors name the subcommand only: the arguments can hold a
        credential."""
        try:
            done = subprocess.run(["/usr/bin/security", *args], env=self.env,
                                  capture_output=True, text=True, timeout=30, check=False)
        except subprocess.TimeoutExpired:
            raise AssertionError(f"security {args[0]} timed out after 30s") from None
        if check and done.returncode != 0:
            raise AssertionError(
                f"security {args[0]} exited {done.returncode}: {done.stderr.strip()[-300:]}")
        return done

    def _create_keychain(self) -> None:
        """macOS: a fresh, unlocked default keychain for this box alone.

        ``security`` finds the user's keychain list and default keychain
        through ``$HOME``, so with the box's HOME neither exists and the first
        write would block on a dialog. Registering one under the box's HOME
        gives every box (and every aswap process it runs) its own Keychain,
        so parallel tests and a developer's login keychain never meet."""
        kc = self.home / "Library" / "Keychains" / "aswap-e2e.keychain-db"
        kc.parent.mkdir(parents=True)
        self.security("create-keychain", "-p", "", str(kc))
        self.security("set-keychain-settings", str(kc))  # no auto-lock
        self.security("unlock-keychain", "-p", "", str(kc))
        self.security("list-keychains", "-d", "user", "-s", str(kc))
        self.security("default-keychain", "-d", "user", "-s", str(kc))
        default = self.security("default-keychain").stdout.strip().strip('"')
        if Path(default).resolve() != kc.resolve():
            raise AssertionError(f"box keychain not the default for its HOME: {default!r}")
        self.keychain = kc

    def live_credentials(self) -> dict | None:
        if sys.platform == "darwin":
            done = self.security("find-generic-password", "-a", self.env["USER"],
                                 "-s", KEYCHAIN_SERVICE, "-w", check=False)
            raw = done.stdout.strip() if done.returncode == 0 else None
        else:
            try:
                raw = self.credentials_path.read_text(encoding="utf-8")
            except OSError:
                raw = None
        if not raw:
            return None
        try:
            return json.loads(raw).get("claudeAiOauth")
        except ValueError:
            # Keychain hands back hex when the data was stored as such.
            return json.loads(bytes.fromhex(raw).decode()).get("claudeAiOauth")

    def live_email(self) -> str | None:
        try:
            config = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return (config.get("oauthAccount") or {}).get("emailAddress")

    def list_json(self) -> dict:
        return self.aswap_json("list", "--json")

    def accounts_by_email(self) -> dict[str, dict]:
        return {a["email"]: a for a in self.list_json()["claude"]["accounts"]}

    # -- teardown ------------------------------------------------------------------

    def cleanup(self) -> None:
        for proc in self.autos:
            proc.kill()
        if getattr(self, "keychain", None):
            self.security("delete-keychain", str(self.keychain), check=False)

    def collect_artifacts(self) -> None:
        """Copy aswap's own logs next to the transcript and request log."""
        logs = self.artifacts / "aswap-logs"
        if self.backup_root.is_dir():
            for path in self.backup_root.rglob("*.log*"):
                if path.is_file():
                    logs.mkdir(exist_ok=True)
                    shutil.copy2(path, logs / path.name)

    def issued_secrets(self) -> set[str]:
        """Every token and session key the fake has handed out."""
        with self.fake_server.lock:
            return set().union(*(a.secrets() for a in self.fake_server.accounts.values()))

    def assert_no_secret_in_output(self) -> None:
        """No issued token appears in any aswap output or log file."""
        text = self.all_log_text()
        leaked = sorted(s[:16] + "..." for s in self.issued_secrets() if s in text)
        assert not leaked, f"tokens printed or logged: {leaked}"

    def all_log_text(self) -> str:
        parts = list(self.outputs)
        for proc in self.autos:
            for path in (proc.out_path, proc.err_path):
                try:
                    parts.append(path.read_text(encoding="utf-8", errors="replace"))
                except OSError:
                    pass
        if self.backup_root.is_dir():
            for path in self.backup_root.rglob("*.log*"):
                if path.is_file():
                    parts.append(path.read_text(encoding="utf-8", errors="replace"))
        return "\n".join(parts)


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    tmp.write_text(text, encoding="utf-8")
    for attempt in range(50):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:  # Windows: a reader holds the file open
            time.sleep(0.05)
    os.replace(tmp, path)


def _slug(nodeid: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", nodeid.split("::", 1)[-1])[:80]


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    report = yield
    item.stash.setdefault(_FAILED_KEY, False)
    if report.failed:
        item.stash[_FAILED_KEY] = True
    return report


_FAILED_KEY = pytest.StashKey[bool]()


@pytest.fixture
def box(request, tmp_path) -> Iterator[Box]:
    artifacts = artifacts_root() / _slug(request.node.nodeid)
    if artifacts.exists():
        shutil.rmtree(artifacts, ignore_errors=True)
    artifacts.mkdir(parents=True)
    fake = FakeClaude(log_path=artifacts / "fake-requests.jsonl").start()
    b = Box(tmp_path, fake, artifacts)
    b._note(f"# {request.node.nodeid}\n# aswap: {aswap_command()}\n# fake: {fake.url}\n")
    try:
        yield b
        if not request.node.stash.get(_FAILED_KEY, False):
            # Every scenario doubles as a leak check: no token or session key
            # the fake issued may show up in aswap's output or logs.
            b.assert_no_secret_in_output()
    finally:
        b.cleanup()
        b.collect_artifacts()
        fake.stop()
        if not request.node.stash.get(_FAILED_KEY, False) and not os.environ.get("ASWAP_E2E_KEEP"):
            shutil.rmtree(artifacts, ignore_errors=True)


def wait_until(pred: Callable[[], object], timeout: float = 60.0, what: str = "condition",
               interval: float = 0.25) -> object:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = pred()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"timed out after {timeout}s waiting for {what} (last: {last!r})")
