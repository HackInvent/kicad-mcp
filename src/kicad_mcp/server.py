"""MCP tools and authenticated, loopback-only HTTP application."""

from __future__ import annotations

import hmac
from collections.abc import Callable
from functools import partial
from typing import Any

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from .bridge import KiCadBridge


def create_server(bridge: Any = None, *, read_only: bool = False) -> FastMCP:
    """Construct a server without connecting to KiCad until a tool is called."""
    bridge = bridge if bridge is not None else KiCadBridge(read_only=read_only)
    server = FastMCP(
        "hackinvent-kicad-mcp",
        instructions=(
            "Operate on the PCB currently open in KiCad. Coordinates and sizes are in "
            "millimetres; angles are in degrees. Inspect the board before making changes. "
            "Edits are undoable in KiCad and are not saved until save_board is called. "
            "Adding tracks does not perform routing or guarantee design-rule compliance."
        ),
        website_url="https://github.com/HackInvent/kicad-mcp",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1:*", "localhost:*"],
            allowed_origins=["http://127.0.0.1:*", "http://localhost:*"],
        ),
    )
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                           idempotentHint=True, openWorldHint=False)
    edit = ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                           idempotentHint=False, openWorldHint=False)

    async def invoke(method: str, **kwargs: Any) -> dict[str, Any]:
        # The IPC client is synchronous. Its own lock serializes board transactions.
        return await anyio.to_thread.run_sync(partial(getattr(bridge, method), **kwargs))

    @server.tool(annotations=read)
    async def kicad_status() -> dict[str, Any]:
        """Check the connection to KiCad and report the editor version."""
        return await invoke("status")

    @server.tool(annotations=read)
    async def get_board_info() -> dict[str, Any]:
        """Read the active PCB's name, layers and item counts."""
        return await invoke("board_info")

    @server.tool(annotations=read)
    async def list_footprints(reference: str | None = None) -> dict[str, Any]:
        """List components, references, values and placement in mm; optionally filter by reference."""
        return await invoke("list_footprints", reference=reference)

    @server.tool(annotations=read)
    async def list_nets() -> dict[str, Any]:
        """List the electrical nets of the active PCB."""
        return await invoke("list_nets")

    @server.tool(annotations=read)
    async def list_tracks() -> dict[str, Any]:
        """Read PCB tracks, widths, positions, copper layers and nets."""
        return await invoke("list_tracks")

    @server.tool(annotations=read)
    async def get_selection() -> dict[str, Any]:
        """Read the items currently selected in the PCB editor."""
        return await invoke("get_selection")

    if not read_only:
        @server.tool(annotations=edit)
        async def move_footprint(
            reference: str, x_mm: float, y_mm: float,
            rotation_degrees: float | None = None,
        ) -> dict[str, Any]:
            """Move one unlocked component to absolute mm coordinates, with optional rotation; do not save."""
            return await invoke("move_footprint", reference=reference, x_mm=x_mm,
                                y_mm=y_mm, rotation_degrees=rotation_degrees)

        @server.tool(annotations=edit)
        async def add_track(
            start_x_mm: float, start_y_mm: float, end_x_mm: float, end_y_mm: float,
            width_mm: float = 0.25, layer: str = "F.Cu", net_name: str | None = None,
        ) -> dict[str, Any]:
            """Add one straight copper track, optionally assigned to an existing net; no autorouting or DRC."""
            return await invoke("add_track", start_x_mm=start_x_mm, start_y_mm=start_y_mm,
                                end_x_mm=end_x_mm, end_y_mm=end_y_mm,
                                width_mm=width_mm, layer=layer, net_name=net_name)

        @server.tool(annotations=edit)
        async def add_text(
            text: str, x_mm: float, y_mm: float, layer: str = "F.SilkS",
            height_mm: float = 1.0,
        ) -> dict[str, Any]:
            """Add PCB text at absolute mm coordinates on the specified layer; do not save."""
            return await invoke("add_text", text=text, x_mm=x_mm, y_mm=y_mm,
                                layer=layer, height_mm=height_mm)

        @server.tool(annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False,
        ))
        async def save_board() -> dict[str, Any]:
            """Explicitly save the active PCB, including unsaved changes made in the editor, to its current file."""
            return await invoke("save_board")

    return server


def create_http_app(
    server: FastMCP, token: str, on_shutdown: Callable[[], None] | None = None,
):
    """Protect every route with a local bearer secret, including lifecycle routes."""
    if not token:
        raise ValueError("An HTTP bearer token is required")

    @server.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"name": "hackinvent-kicad-mcp", "status": "running"})

    @server.custom_route("/shutdown", methods=["POST"])
    async def shutdown(request: Request) -> JSONResponse:
        if on_shutdown is None:
            return JSONResponse({"error": "Shutdown is unavailable"}, status_code=409)
        on_shutdown()
        return JSONResponse({"status": "stopping"})

    app = server.streamable_http_app()

    async def authenticate(request: Request, call_next):
        # Do not forward credentials from a browser origin, even on localhost.
        # Native MCP clients normally omit Origin entirely.
        if request.headers.get("origin"):
            return JSONResponse({"error": "Browser origins are not accepted"}, status_code=403)
        host = request.url.hostname
        if host not in {"127.0.0.1", "localhost"}:
            return JSONResponse({"error": "Invalid host"}, status_code=421)
        supplied = request.headers.get("authorization", "")
        expected = "Bearer " + token
        if not hmac.compare_digest(supplied.encode(), expected.encode()):
            return JSONResponse({"error": "A valid bearer token is required"}, status_code=401,
                                headers={"WWW-Authenticate": 'Bearer realm="kicad-mcp"'})
        return await call_next(request)

    app.add_middleware(BaseHTTPMiddleware, dispatch=authenticate)
    return app
