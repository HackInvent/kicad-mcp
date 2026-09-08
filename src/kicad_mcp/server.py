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
from pydantic import StrictBool, StrictFloat
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import __version__
from .bridge import KiCadBridge
from .http_security import valid_local_host, validate_http_token


def create_server(bridge: Any = None, *, read_only: bool = False) -> FastMCP:
    """Construct a server without connecting to KiCad until a tool is called."""
    bridge = bridge if bridge is not None else KiCadBridge(read_only=read_only)
    server = FastMCP(
        "hackinvent-kicad-mcp",
        instructions=(
            "Operate on the PCB currently open in KiCad. Coordinates and sizes are in "
            "millimetres; angles are in degrees. Inspect the board before making changes. "
            "Object edits create undo steps and are not saved until save_board is called. "
            "Adding tracks does not perform routing or guarantee design-rule compliance. "
            "BOM tools use footprint data from the active PCB, not the schematic. "
            "BOM exports return CSV content; they do not write files. "
            "Net membership does not prove physical connectivity. Zone refill is asynchronous; "
            "inspect the editor before issuing dependent edits. DRC and fabrication use "
            "kicad-cli on the last saved PCB and create local artifacts. Call save_board first "
            "to include pending edits; neither tool implicitly saves the PCB or project."
        ),
        website_url="https://github.com/HackInvent/kicad-mcp",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1", "localhost", "127.0.0.1:*", "localhost:*"],
            allowed_origins=["http://127.0.0.1:*", "http://localhost:*"],
        ),
    )
    # FastMCP 1.x otherwise announces the SDK version as the application version.
    server._mcp_server.version = __version__
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                           idempotentHint=True, openWorldHint=False)
    edit = ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                           idempotentHint=False, openWorldHint=False)

    view = ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                           idempotentHint=True, openWorldHint=False)
    artifact = ToolAnnotations(readOnlyHint=False, destructiveHint=False,
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

    @server.tool(annotations=read)
    async def get_bom(
        grouped: StrictBool = True, include_dnp: StrictBool = False, include_excluded: StrictBool = False,
        fields: list[str] | None = None,
    ) -> dict[str, Any]:
        """Read the PCB bill of materials with quantities and custom fields; omit DNP/excluded parts by default."""
        return await invoke("get_bom", grouped=grouped, include_dnp=include_dnp,
                            include_excluded=include_excluded, fields=fields)

    @server.tool(annotations=read)
    async def export_bom(
        grouped: StrictBool = True, include_dnp: StrictBool = False, include_excluded: StrictBool = False,
        fields: list[str] | None = None, delimiter: str = ",",
    ) -> dict[str, Any]:
        """Return a spreadsheet-safe CSV BOM as text, without writing a file; delimiter may be comma, semicolon or tab."""
        return await invoke("export_bom", grouped=grouped, include_dnp=include_dnp,
                            include_excluded=include_excluded, fields=fields, delimiter=delimiter)

    @server.tool(annotations=read)
    async def list_pads(
        reference: str | None = None, net_name: str | None = None,
    ) -> dict[str, Any]:
        """List pad UUIDs, footprint references, numbers, positions, nets and geometry; filters are exact."""
        return await invoke("list_pads", reference=reference, net_name=net_name)

    @server.tool(annotations=read)
    async def list_vias(net_name: str | None = None) -> dict[str, Any]:
        """Read via UUIDs, positions, diameters, drills, layer spans and nets; optionally filter by net."""
        return await invoke("list_vias", net_name=net_name)

    @server.tool(annotations=read)
    async def list_zones() -> dict[str, Any]:
        """Inspect zone UUIDs, names, nets, layers, priorities and fill state."""
        return await invoke("list_zones")

    @server.tool(annotations=read)
    async def get_item_details(item_ids: list[str]) -> dict[str, Any]:
        """Read supported PCB items by 1 to 500 unique UUIDs, with type-specific details in mm."""
        return await invoke("get_item_details", item_ids=item_ids)

    @server.tool(annotations=read)
    async def get_board_layers() -> dict[str, Any]:
        """Read enabled PCB layers, their canonical/display names, visibility and active layer."""
        return await invoke("get_board_layers")

    @server.tool(annotations=read)
    async def get_board_stackup() -> dict[str, Any]:
        """Read the ordered copper/dielectric stackup, thicknesses, materials and board finish settings."""
        return await invoke("get_board_stackup")

    @server.tool(annotations=read)
    async def get_net_connections(net_name: str) -> dict[str, Any]:
        """List pads, tracks and vias assigned to an exact net; this does not verify routed connectivity or DRC."""
        return await invoke("get_net_connections", net_name=net_name)

    if not read_only:
        @server.tool(annotations=view)
        async def set_selection(item_ids: list[str], mode: str = "replace") -> dict[str, Any]:
            """Replace, add to or remove from the editor selection using UUIDs; an empty replacement clears it."""
            return await invoke("set_selection", item_ids=item_ids, mode=mode)

        @server.tool(annotations=view)
        async def set_active_layer(layer: str) -> dict[str, Any]:
            """Activate an enabled PCB layer by canonical name, for example F.Cu or B.SilkS."""
            return await invoke("set_active_layer", layer=layer)

        @server.tool(annotations=view)
        async def set_visible_layers(layers: list[str]) -> dict[str, Any]:
            """Replace the visible PCB layer set with enabled canonical layer names."""
            return await invoke("set_visible_layers", layers=layers)

        @server.tool(annotations=edit)
        async def add_via(
            x_mm: StrictFloat, y_mm: StrictFloat, diameter_mm: StrictFloat = 0.6,
            drill_mm: StrictFloat = 0.3, net_name: str | None = None,
        ) -> dict[str, Any]:
            """Add a through-hole via spanning F.Cu to B.Cu in one undo step; no autorouting, DRC or saving."""
            return await invoke("add_via", x_mm=x_mm, y_mm=y_mm,
                                diameter_mm=diameter_mm, drill_mm=drill_mm, net_name=net_name)

        @server.tool(annotations=edit)
        async def delete_items(item_ids: list[str]) -> dict[str, Any]:
            """Delete supported unlocked top-level PCB items by 1 to 500 unique UUIDs in one undo step; reject footprint children and grouped items."""
            return await invoke("delete_items", item_ids=item_ids)

        @server.tool(annotations=edit)
        async def refill_zones() -> dict[str, Any]:
            """Request an asynchronous zone refill in the editor; the response does not confirm completion. Do not save."""
            return await invoke("refill_zones")

        @server.tool(annotations=artifact)
        async def run_drc(severity: str = "all") -> dict[str, Any]:
            """Run kicad-cli DRC on the last saved PCB and write a local JSON report; no schematic parity check. Severity: all, error, warning."""
            return await invoke("run_drc", severity=severity)

        @server.tool(annotations=artifact)
        async def export_fabrication(formats: list[str] | None = None) -> dict[str, Any]:
            """Export gerbers, drill, positions or svg from the last saved PCB using kicad-cli; default gerbers, drill and positions. No implicit save."""
            return await invoke("export_fabrication", formats=formats)

        @server.tool(annotations=edit)
        async def update_bom_fields(
            references: list[str], fields: dict[str, str] | None = None,
            value: str | None = None, dnp: StrictBool | None = None,
            exclude_from_bom: StrictBool | None = None,
        ) -> dict[str, Any]:
            """Update custom BOM fields, value or assembly flags on exact PCB references in one undo step; do not save or change the schematic."""
            return await invoke("update_bom_fields", references=references, fields=fields,
                                value=value, dnp=dnp, exclude_from_bom=exclude_from_bom)

        @server.tool(annotations=edit)
        async def move_footprint(
            reference: str, x_mm: StrictFloat, y_mm: StrictFloat,
            rotation_degrees: StrictFloat | None = None,
        ) -> dict[str, Any]:
            """Move one unlocked component to absolute mm coordinates, with optional rotation; do not save."""
            return await invoke("move_footprint", reference=reference, x_mm=x_mm,
                                y_mm=y_mm, rotation_degrees=rotation_degrees)

        @server.tool(annotations=edit)
        async def add_track(
            start_x_mm: StrictFloat, start_y_mm: StrictFloat, end_x_mm: StrictFloat, end_y_mm: StrictFloat,
            width_mm: StrictFloat = 0.25, layer: str = "F.Cu", net_name: str | None = None,
        ) -> dict[str, Any]:
            """Add one straight copper track, optionally assigned to an existing net; no autorouting or DRC."""
            return await invoke("add_track", start_x_mm=start_x_mm, start_y_mm=start_y_mm,
                                end_x_mm=end_x_mm, end_y_mm=end_y_mm,
                                width_mm=width_mm, layer=layer, net_name=net_name)

        @server.tool(annotations=edit)
        async def add_text(
            text: str, x_mm: StrictFloat, y_mm: StrictFloat, layer: str = "F.SilkS",
            height_mm: StrictFloat = 1.0,
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
    validate_http_token(token)

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
        if "origin" in request.headers:
            return JSONResponse({"error": "Browser origins are not accepted"}, status_code=403)
        host = request.headers.get("host", "")
        if not valid_local_host(host):
            return JSONResponse({"error": "Invalid host"}, status_code=421)
        supplied = request.headers.get("authorization", "")
        expected = "Bearer " + token
        if not hmac.compare_digest(supplied.encode(), expected.encode()):
            return JSONResponse({"error": "A valid bearer token is required"}, status_code=401,
                                headers={"WWW-Authenticate": 'Bearer realm="kicad-mcp"'})
        # Host names are case-insensitive; normalize before the SDK's exact
        # string allowlist check on the inner MCP transport.
        request.scope["headers"] = [
            (name, value.lower() if name == b"host" else value)
            for name, value in request.scope["headers"]
        ]
        return await call_next(request)

    app.add_middleware(BaseHTTPMiddleware, dispatch=authenticate)
    return app
