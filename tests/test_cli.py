"""Exercise actual server processes, session lifecycle and the stdio wire protocol."""

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import pytest

from kicad_mcp import __version__
from kicad_mcp.__main__ import main, request_session, session_lock, session_path, write_session


ROOT = Path(__file__).resolve().parents[1]


def process_environment(tmp_path):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    env["KICAD_MCP_STATE_DIR"] = str(tmp_path)
    env["KICAD_API_SOCKET"] = str(tmp_path / "unavailable-kicad.sock")
    env.pop("KICAD_API_TOKEN", None)
    env.pop("KICAD_MCP_TOKEN", None)
    return env


def test_http_process_status_stop_and_session_cleanup(tmp_path, monkeypatch, capsys):
    env = process_environment(tmp_path)
    monkeypatch.setenv("KICAD_MCP_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("KICAD_API_SOCKET", env["KICAD_API_SOCKET"])
    with (tmp_path / "server.log").open("w") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "kicad_mcp", "serve", "--transport", "streamable-http", "--port", "0"],
            env=env, stdout=log, stderr=log,
        )
        try:
            path = session_path(tmp_path, env["KICAD_API_SOCKET"])
            deadline = time.monotonic() + 15
            while True:
                try:
                    session = json.loads(path.read_text())
                    if request_session(session, "/health")["status"] == "running":
                        break
                except (OSError, ValueError):
                    pass
                if process.poll() is not None or time.monotonic() > deadline:
                    pytest.fail((tmp_path / "server.log").read_text())
                time.sleep(0.05)

            assert "token" in session and len(session["token"]) >= 32
            assert "KICAD_API_TOKEN" not in path.read_text()
            if os.name != "nt":
                assert path.stat().st_mode & 0o777 == 0o600
            assert main(["status"]) == 0
            output = capsys.readouterr().out
            assert session["token"] not in output
            assert json.loads(output)[0]["url"] == session["url"]
            assert main(["status", "--show-token"]) == 0
            assert json.loads(capsys.readouterr().out)[0]["token"] == session["token"]

            # A second plugin click does not start another server or change credentials.
            duplicate = subprocess.run(
                [sys.executable, "-m", "kicad_mcp", "serve", "--transport", "streamable-http", "--port", "0"],
                env=env, capture_output=True, text=True, timeout=10,
            )
            assert duplicate.returncode == 0, duplicate.stderr
            assert "already running" in duplicate.stderr
            assert json.loads(path.read_text())["token"] == session["token"]
            assert main(["stop"]) == 0
            assert process.wait(timeout=10) == 0
            assert not path.exists()
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)


def test_real_stdio_client_initialization_and_disconnected_kicad(tmp_path):
    async def exercise():
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "kicad_mcp", "serve", "--transport", "stdio", "--read-only"],
            env=process_environment(tmp_path),
        )
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                info = await session.initialize()
                assert info.serverInfo.name == "hackinvent-kicad-mcp"
                assert info.serverInfo.version == __version__
                tools = await session.list_tools()
                assert len(tools.tools) == 15
                result = await session.call_tool("kicad_status", {})
                assert not result.isError
                assert result.structuredContent["connected"] is False
                assert "KiCad" in result.structuredContent["error"]
    asyncio.run(asyncio.wait_for(exercise(), timeout=20))


@pytest.mark.parametrize("url", [
    "https://127.0.0.1:8765/mcp", "http://example.com:8765/mcp",
    "http://127.0.0.1:8765/other", "http://user@127.0.0.1:8765/mcp",
    "http://127.0.0.1:8765/mcp?redirect=example.com",
])
def test_session_file_cannot_redirect_credentials(url):
    with pytest.raises(ValueError, match="Invalid local server URL"):
        request_session({"url": url, "token": "secret"}, "/health")


def test_replacing_session_keeps_private_permissions(tmp_path):
    path = tmp_path / "session.json"
    path.write_text("{}")
    path.chmod(0o644)
    write_session(path, {"token": "replacement"})
    assert json.loads(path.read_text())["token"] == "replacement"
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def test_session_lock_excludes_simultaneous_startups_and_releases(tmp_path):
    path = tmp_path / "session.json"
    with session_lock(path):
        with pytest.raises(ValueError, match="already running or starting"):
            with session_lock(path):
                pytest.fail("A second startup acquired the same session lock")
    with session_lock(path):
        pass


@pytest.mark.parametrize("data", [
    {"url": 123, "token": "test-token"},
    {"url": ["http://127.0.0.1:8765/mcp"], "token": "test-token"},
])
def test_malformed_session_records_do_not_break_discovery(tmp_path, data):
    from kicad_mcp.__main__ import active_sessions
    write_session(tmp_path / "broken.json", data)
    assert active_sessions(tmp_path) == []


@pytest.mark.parametrize("payload", [[], None, "running"])
def test_non_object_health_responses_are_rejected(monkeypatch, payload):
    from unittest.mock import Mock
    connection = Mock()
    response = connection.getresponse.return_value
    response.status = 200
    response.read.return_value = json.dumps(payload).encode()
    monkeypatch.setattr("kicad_mcp.__main__.http.client.HTTPConnection", lambda *a, **k: connection)
    with pytest.raises(ValueError, match="response"):
        request_session({"url": "http://127.0.0.1:8765/mcp", "token": "test-token"}, "/health")
    connection.close.assert_called_once()


@pytest.mark.parametrize("read_only", [True, False])
def test_repeated_start_cannot_silently_ignore_requested_access_mode(tmp_path, monkeypatch, read_only):
    from argparse import Namespace
    from kicad_mcp.__main__ import serve_http
    args = Namespace(state_dir=tmp_path, socket="kicad.sock", read_only=read_only)
    record = {"url": "http://127.0.0.1:8765/mcp", "read_only": not read_only}
    monkeypatch.setattr("kicad_mcp.__main__.active_sessions", lambda *a: [(session_path(tmp_path, args.socket), record)])
    with pytest.raises(ValueError, match="Stop"):
        serve_http(args, None)


@pytest.mark.parametrize("token", ["secret-\n-newline", "secret-\u00e9-nonascii"])
def test_invalid_fixed_token_fails_without_publishing_session_or_secret(tmp_path, token):
    env = process_environment(tmp_path)
    env["KICAD_MCP_TOKEN"] = token
    result = subprocess.run(
        [sys.executable, "-m", "kicad_mcp", "serve", "--transport", "streamable-http", "--port", "0"],
        env=env, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 1
    assert "token" in result.stderr and "ASCII" in result.stderr
    assert "secret-" not in result.stderr
    assert not list(tmp_path.glob("*.json"))
