"""Command line and KiCad start/stop action entry points."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import http.client
import json
import os
from pathlib import Path
import secrets
import socket
import sys
import tempfile
from urllib.parse import urlsplit

from . import __version__


def state_directory() -> Path:
    if override := os.environ.get("KICAD_MCP_STATE_DIR"):
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Application Support"
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
    return base / "kicad-mcp"


def session_path(directory: Path, socket_path: str | None) -> Path:
    key = hashlib.sha256((socket_path or "default").encode()).hexdigest()[:16]
    return directory / f"{key}.json"


def write_session(path: Path, data: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".session-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def request_session(data: dict, route: str, method: str = "GET") -> dict:
    """Contact only a validated local endpoint, without proxies or redirects."""
    url = urlsplit(data["url"])
    if (url.scheme != "http" or url.hostname != "127.0.0.1" or not url.port
            or url.username or url.password or url.path != "/mcp" or url.query or url.fragment):
        raise ValueError("Invalid local server URL in session file")
    connection = http.client.HTTPConnection("127.0.0.1", url.port, timeout=2)
    try:
        connection.request(method, route, headers={"Authorization": "Bearer " + data["token"]})
        response = connection.getresponse()
        payload = response.read(65536)
        if response.status != 200:
            raise ValueError(f"Local server returned HTTP {response.status}")
        return json.loads(payload)
    finally:
        connection.close()


def active_sessions(directory: Path, socket_path: str | None = None) -> list[tuple[Path, dict]]:
    candidates = [session_path(directory, socket_path)] if socket_path else sorted(directory.glob("*.json"))
    sessions = []
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            health = request_session(data, "/health")
            if health.get("name") == "hackinvent-kicad-mcp" and health.get("status") == "running":
                sessions.append((path, data))
        except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException):
            # Stale records are ignored, never used to signal a potentially reused PID.
            continue
    return sessions


@contextmanager
def session_lock(path: Path):
    """Hold an OS lock for the server lifetime; crashes release it automatically."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    try:
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"0")
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            raise ValueError("A server for this KiCad instance is already running or starting; use status") from None
        yield
    finally:
        if acquired:
            if sys.platform == "win32":
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def serve_http(args: argparse.Namespace, bridge) -> int:
    path = session_path(args.state_dir, args.socket)
    for existing_path, data in active_sessions(args.state_dir, args.socket):
        if existing_path == path:
            print(f"KiCad MCP is already running at {data['url']}", file=sys.stderr)
            return 0
    with session_lock(path):
        return _serve_http_locked(args, bridge)


def _serve_http_locked(args: argparse.Namespace, bridge) -> int:
    import uvicorn

    from .server import create_http_app, create_server

    directory = args.state_dir
    path = session_path(directory, args.socket)
    for existing_path, data in active_sessions(directory, args.socket):
        if existing_path == path:
            print(f"KiCad MCP is already running at {data['url']}", file=sys.stderr)
            return 0

    # Bind before writing any state; a second click cannot replace the active token.
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if sys.platform == "win32":
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind(("127.0.0.1", args.port))
        listener.listen(128)
    except OSError:
        listener.close()
        raise ValueError(f"Cannot listen on 127.0.0.1:{args.port}; choose another KICAD_MCP_PORT") from None

    token = os.environ.get("KICAD_MCP_TOKEN") or secrets.token_urlsafe(32)
    port = listener.getsockname()[1]
    data = {"url": f"http://127.0.0.1:{port}/mcp", "token": token,
            "pid": os.getpid(), "socket": args.socket, "read_only": args.read_only,
            "version": __version__}
    server = create_server(bridge, read_only=args.read_only)
    runner = None

    def shutdown() -> None:
        if runner is not None:
            runner.should_exit = True

    app = create_http_app(server, token, on_shutdown=shutdown)
    runner = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                         log_level="warning", access_log=False))
    try:
        write_session(path, data)
        print(f"KiCad MCP: {data['url']}\nSession: {path}\n"
              "Run kicad-mcp status --show-token for client connection settings.", file=sys.stderr)
        runner.run(sockets=[listener])
    finally:
        listener.close()
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
            if current.get("token") == token:
                path.unlink()
        except (OSError, ValueError):
            pass
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kicad-mcp", description="Connect MCP clients to the KiCad PCB editor.")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="Run the MCP server")
    serve.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    serve.add_argument("--port", type=int, default=os.environ.get("KICAD_MCP_PORT", "8765"))
    serve.add_argument("--read-only", action="store_true",
                       default=os.environ.get("KICAD_MCP_READ_ONLY", "").lower() in {"1", "true", "yes"})
    status = commands.add_parser("status", help="List running local HTTP servers")
    status.add_argument("--show-token", action="store_true", help="Include the local bearer secret in the output")
    stop = commands.add_parser("stop", help="Stop a local HTTP server through its authenticated endpoint")
    for command in (serve, status, stop):
        command.add_argument("--socket", default=os.environ.get("KICAD_API_SOCKET"),
                             help="Select a KiCad IPC socket (defaults to KiCad's environment)")
        command.add_argument("--state-dir", type=Path, default=state_directory(),
                             help="Directory containing local server session files")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if not arguments:
        arguments = ["serve"]
    args = build_parser().parse_args(arguments)
    try:
        if args.command == "status":
            result = []
            for path, data in active_sessions(args.state_dir, args.socket):
                visible = {k: v for k, v in data.items() if k != "token" or args.show_token}
                visible["session_file"] = str(path)
                result.append(visible)
            print(json.dumps(result, indent=2))
            return 0
        if args.command == "stop":
            sessions = active_sessions(args.state_dir, args.socket)
            if not sessions:
                print("No running KiCad MCP server found.", file=sys.stderr)
                return 0
            if len(sessions) > 1:
                raise ValueError("Several servers are running; select one with --socket")
            request_session(sessions[0][1], "/shutdown", "POST")
            print("KiCad MCP server is stopping.", file=sys.stderr)
            return 0

        if not 0 <= args.port <= 65535:
            raise ValueError("Port must be between 0 and 65535")
        from .bridge import KiCadBridge
        from .server import create_server

        bridge = KiCadBridge(socket_path=args.socket, token=os.environ.get("KICAD_API_TOKEN"),
                             read_only=args.read_only)
        if args.transport == "stdio":
            create_server(bridge, read_only=args.read_only).run(transport="stdio")
            return 0
        return serve_http(args, bridge)
    except (OSError, ValueError, http.client.HTTPException) as exc:
        print(f"kicad-mcp: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
