"""Endpoint overrides (endpoints.py): validation, routing, and the test clock.

The routing tests send real requests over a real loopback socket, so they
check the URL each request actually reaches, not a patched ``urlopen``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from claude_swap import endpoints, gate, oauth


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (endpoints.API_BASE_ENV, endpoints.CLAUDE_AI_BASE_ENV, endpoints.TIME_SCALE_ENV):
        monkeypatch.delenv(name, raising=False)


class TestDefaults:
    def test_unset_means_the_real_hosts(self):
        assert endpoints.api_base() == "https://api.anthropic.com"
        assert endpoints.oauth_base() == "https://platform.claude.com"
        assert endpoints.claude_ai_base() == "https://claude.ai"

    def test_blank_means_unset(self, monkeypatch):
        monkeypatch.setenv(endpoints.API_BASE_ENV, "  ")
        assert endpoints.api_base() == endpoints.DEFAULT_API_BASE


class TestValidation:
    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1:8123",
            "http://localhost:9000/",
            "http://LOCALHOST:9000",
            "http://[::1]:8080",
            "http://127.0.0.2:8080",
            "https://proxy.example.com",
            "https://proxy.example.com/anthropic/",
        ],
    )
    def test_accepted(self, monkeypatch, url):
        monkeypatch.setenv(endpoints.API_BASE_ENV, url)
        assert endpoints.api_base() == url.strip().rstrip("/")
        assert endpoints.oauth_base() == endpoints.api_base()
        endpoints.validate_environment()

    @pytest.mark.parametrize(
        "url",
        [
            "http://example.com",  # plain http off the machine
            "http://10.0.0.5:8080",
            "http://localhost.example.com",
            "http://127.0.0.1.nip.io",
            "ftp://127.0.0.1",
            "127.0.0.1:8080",
            "https://",
            "https://user:pw@proxy.example.com",
            "https://proxy.example.com/?x=1",
            "http://127.0.0.1:notaport",
        ],
    )
    def test_refused(self, monkeypatch, url):
        monkeypatch.setenv(endpoints.API_BASE_ENV, url)
        with pytest.raises(endpoints.EndpointOverrideError):
            endpoints.api_base()
        with pytest.raises(endpoints.EndpointOverrideError):
            endpoints.validate_environment()

    def test_claude_ai_override_is_validated_too(self, monkeypatch):
        monkeypatch.setenv(endpoints.CLAUDE_AI_BASE_ENV, "http://claude.example.com")
        with pytest.raises(endpoints.EndpointOverrideError):
            endpoints.claude_ai_base()
        monkeypatch.setenv(endpoints.CLAUDE_AI_BASE_ENV, "http://127.0.0.1:1")
        assert endpoints.claude_ai_base() == "http://127.0.0.1:1"

    def test_cli_refuses_before_doing_anything(self, tmp_path):
        env = dict(os.environ)
        env[endpoints.API_BASE_ENV] = "http://example.com"
        env["HOME"] = env["USERPROFILE"] = str(tmp_path)
        done = subprocess.run(
            [sys.executable, "-m", "claude_swap", "list"],
            env=env, capture_output=True, text=True, timeout=60,
            cwd=tmp_path,
        )
        assert done.returncode == 2
        assert "plain http is only allowed" in (done.stdout + done.stderr)
        assert not any(tmp_path.iterdir()), "nothing may be written"


class TestTimeScale:
    def test_off_by_default(self, monkeypatch):
        assert endpoints.time_scale() == 1.0
        monkeypatch.setenv(endpoints.TIME_SCALE_ENV, "0.01")
        assert endpoints.time_scale() == 1.0, "needs a loopback API base"

    def test_ignored_against_a_remote_api(self, monkeypatch):
        monkeypatch.setenv(endpoints.API_BASE_ENV, "https://proxy.example.com")
        monkeypatch.setenv(endpoints.TIME_SCALE_ENV, "0.01")
        assert endpoints.time_scale() == 1.0

    def test_applies_with_a_loopback_api(self, monkeypatch):
        monkeypatch.setenv(endpoints.API_BASE_ENV, "http://127.0.0.1:1")
        monkeypatch.setenv(endpoints.TIME_SCALE_ENV, "0.01")
        assert endpoints.time_scale() == 0.01
        assert endpoints.scaled(600.0) == pytest.approx(6.0)

    @pytest.mark.parametrize("raw", ["0", "-1", "2", "nan-ish", ""])
    def test_out_of_range_is_ignored(self, monkeypatch, raw):
        monkeypatch.setenv(endpoints.API_BASE_ENV, "http://127.0.0.1:1")
        monkeypatch.setenv(endpoints.TIME_SCALE_ENV, raw)
        assert endpoints.time_scale() == 1.0


class _Recorder(BaseHTTPRequestHandler):
    seen: list[tuple[str, str, dict]] = []

    def log_message(self, *args):  # keep pytest output quiet
        pass

    def _answer(self, body: dict, headers: dict | None = None) -> None:
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        self.seen.append(("GET", self.path, dict(self.headers)))
        if self.path == "/api/oauth/usage":
            self._answer({"five_hour": {"utilization": 12.0, "resets_at": None}})
        else:
            self._answer({"account": {"uuid": "u-1", "email": "a@example.com"}})

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self.seen.append(("POST", self.path, dict(self.headers)))
        if self.path == "/v1/oauth/token":
            self._answer({"access_token": "at-2", "refresh_token": "rt-2", "expires_in": 3600})
        else:
            self._answer(
                {"type": "message"},
                {"anthropic-ratelimit-unified-status": "allowed",
                 "anthropic-ratelimit-unified-7d-utilization": "0.42"},
            )


@pytest.fixture
def loopback(monkeypatch):
    _Recorder.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv(endpoints.API_BASE_ENV, f"http://127.0.0.1:{server.server_port}")
    try:
        yield _Recorder.seen
    finally:
        server.shutdown()
        server.server_close()


class TestRouting:
    def test_every_api_request_reaches_the_override(self, loopback):
        assert oauth.request_usage_data("at-1")["five_hour"]["utilization"] == 12.0
        assert oauth.fetch_oauth_profile("at-1")["uuid"] == "u-1"
        creds = json.dumps({"claudeAiOauth": {"accessToken": "at-1", "refreshToken": "rt-1"}})
        out = oauth.try_refresh_oauth_credentials(creds)
        assert out.error is None
        assert json.loads(out.credentials)["claudeAiOauth"]["refreshToken"] == "rt-2"
        probe = gate.request_gate("at-1", 0.0)
        assert probe.gate["seven_day"]["pct"] == 42.0

        paths = [(method, path) for method, path, _ in loopback]
        assert paths == [
            ("GET", "/api/oauth/usage"),
            ("GET", "/api/oauth/profile"),
            ("POST", "/v1/oauth/token"),
            ("POST", "/v1/messages"),
        ]
        bearer = [h.get("Authorization") for m, p, h in loopback if p != "/v1/oauth/token"]
        assert bearer == ["Bearer at-1"] * 3
