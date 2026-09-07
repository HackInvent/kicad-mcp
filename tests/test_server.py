"""Exercise the real MCP protocol and HTTP boundary without a KiCad process."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from kicad_mcp.bridge import BridgeError
from kicad_mcp.server import create_http_app, create_server


TOKEN = "test-secret-that-is-not-a-real-credential"
BASE_URL = "http://127.0.0.1:8765"
READ_TOOLS = {
    "kicad_status", "get_board_info", "list_footprints", "list_nets",
    "list_tracks", "get_selection",
}
WRITE_TOOLS = {"move_footprint", "add_track", "add_text", "save_board"}


class FakeBridge:
    def __init__(self):
        self.calls = []

    def _record(self, method, **arguments):
        self.calls.append((method, arguments))
        return {"method": method, "arguments": arguments}

    def status(self):
        return self._record("status")

    def board_info(self):
        return self._record("board_info")

    def list_footprints(self, reference=None):
        return self._record("list_footprints", reference=reference)

    def list_nets(self):
        return self._record("list_nets")

    def list_tracks(self):
        return self._record("list_tracks")

    def get_selection(self):
        return self._record("get_selection")

    def move_footprint(self, reference, x_mm, y_mm, rotation_degrees=None):
        if reference == "MISSING":
            raise BridgeError("Footprint MISSING does not exist")
        return self._record(
            "move_footprint", reference=reference, x_mm=x_mm, y_mm=y_mm,
            rotation_degrees=rotation_degrees,
        )

    def add_track(self, start_x_mm, start_y_mm, end_x_mm, end_y_mm,
                  width_mm=0.25, layer="F.Cu", net_name=None):
        return self._record(
            "add_track", start_x_mm=start_x_mm, start_y_mm=start_y_mm,
            end_x_mm=end_x_mm, end_y_mm=end_y_mm, width_mm=width_mm,
            layer=layer, net_name=net_name,
        )

    def add_text(self, text, x_mm, y_mm, layer="F.SilkS", height_mm=1.0):
        return self._record(
            "add_text", text=text, x_mm=x_mm, y_mm=y_mm,
            layer=layer, height_mm=height_mm,
        )

    def save_board(self):
        return self._record("save_board")


@pytest.fixture
def anyio_backend():
    return "asyncio"


@asynccontextmanager
async def running_app(*, read_only=False, on_shutdown=None):
    bridge = FakeBridge()
    server = create_server(bridge, read_only=read_only)
    app = create_http_app(server, TOKEN, on_shutdown=on_shutdown)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=BASE_URL,
        ) as client:
            yield bridge, client


@asynccontextmanager
async def connected_session(*, read_only=False):
    async with running_app(read_only=read_only) as (bridge, client):
        client.headers["Authorization"] = f"Bearer {TOKEN}"
        async with streamable_http_client(
            f"{BASE_URL}/mcp", http_client=client,
        ) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                initialized = await session.initialize()
                yield bridge, session, initialized


def tool_data(result):
    assert not result.isError, result.content
    return json.loads(result.content[0].text)


@pytest.mark.anyio
async def test_initialization_tool_discovery_and_safety_annotations():
    async with connected_session() as (bridge, session, initialized):
        assert initialized.serverInfo.name == "hackinvent-kicad-mcp"
        assert initialized.capabilities.tools is not None
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        assert set(tools) == READ_TOOLS | WRITE_TOOLS
        for name in READ_TOOLS:
            assert tools[name].annotations.readOnlyHint is True
            assert tools[name].annotations.destructiveHint is False
        for name in WRITE_TOOLS:
            assert tools[name].annotations.readOnlyHint is False
            assert tools[name].annotations.destructiveHint is True
        assert tools["save_board"].annotations.idempotentHint is True
        assert set(tools["move_footprint"].inputSchema["required"]) == {
            "reference", "x_mm", "y_mm",
        }
        # Discovery must work even before KiCad is connected.
        assert bridge.calls == []


@pytest.mark.anyio
async def test_read_only_server_hides_and_rejects_mutations():
    async with connected_session(read_only=True) as (bridge, session, _):
        names = {tool.name for tool in (await session.list_tools()).tools}
        assert names == READ_TOOLS
        result = await session.call_tool("save_board", {})
        assert result.isError
        assert bridge.calls == []
        assert tool_data(await session.call_tool("get_board_info", {})) == {
            "method": "board_info", "arguments": {},
        }


@pytest.mark.anyio
async def test_read_tools_reach_bridge_and_return_json():
    async with connected_session() as (bridge, session, _):
        calls = [
            ("kicad_status", "status", {}),
            ("get_board_info", "board_info", {}),
            ("list_footprints", "list_footprints", {"reference": "R12"}),
            ("list_nets", "list_nets", {}),
            ("list_tracks", "list_tracks", {}),
            ("get_selection", "get_selection", {}),
        ]
        for tool, method, arguments in calls:
            result = await session.call_tool(tool, arguments)
            assert tool_data(result) == {"method": method, "arguments": arguments}
        assert bridge.calls == [(method, arguments) for _, method, arguments in calls]


@pytest.mark.anyio
async def test_mutations_preserve_coordinates_layers_nets_and_explicit_save():
    async with connected_session() as (bridge, session, _):
        calls = [
            ("move_footprint", {
                "reference": "U1", "x_mm": 12.5, "y_mm": -7.25,
                "rotation_degrees": 90.0,
            }),
            ("add_track", {
                "start_x_mm": 1.5, "start_y_mm": 2.0,
                "end_x_mm": 3.0, "end_y_mm": 4.5,
                "width_mm": 0.4, "layer": "B.Cu", "net_name": "GND",
            }),
            ("add_text", {
                "text": "Entrée Ω", "x_mm": 25.0, "y_mm": 30.0,
                "layer": "B.SilkS", "height_mm": 1.5,
            }),
        ]
        for tool, arguments in calls:
            result = await session.call_tool(tool, arguments)
            assert tool_data(result) == {"method": tool, "arguments": arguments}
        assert bridge.calls == calls
        assert tool_data(await session.call_tool("save_board", {})) == {
            "method": "save_board", "arguments": {},
        }
        assert bridge.calls[-1] == ("save_board", {})


@pytest.mark.anyio
async def test_tool_defaults_are_forwarded_to_bridge():
    async with connected_session() as (bridge, session, _):
        result = await session.call_tool("add_track", {
            "start_x_mm": 1, "start_y_mm": 2, "end_x_mm": 3, "end_y_mm": 4,
        })
        arguments = tool_data(result)["arguments"]
        assert arguments["width_mm"] == 0.25
        assert arguments["layer"] == "F.Cu"
        assert arguments["net_name"] is None
        result = await session.call_tool("move_footprint", {
            "reference": "R1", "x_mm": 1, "y_mm": 2,
        })
        assert tool_data(result)["arguments"]["rotation_degrees"] is None
        result = await session.call_tool("list_footprints", {})
        assert tool_data(result)["arguments"] == {"reference": None}


@pytest.mark.anyio
async def test_bridge_error_is_tool_error_and_session_remains_usable():
    async with connected_session() as (bridge, session, _):
        result = await session.call_tool("move_footprint", {
            "reference": "MISSING", "x_mm": 1, "y_mm": 2,
        })
        assert result.isError
        assert "Footprint MISSING does not exist" in result.content[0].text
        assert bridge.calls == []
        assert tool_data(await session.call_tool("kicad_status", {}))["method"] == "status"


@pytest.mark.anyio
async def test_invalid_tool_arguments_do_not_reach_bridge():
    async with connected_session() as (bridge, session, _):
        for arguments in ({"reference": "R1"}, {
            "reference": "R1", "x_mm": {"invalid": "number"}, "y_mm": 2,
        }):
            result = await session.call_tool("move_footprint", arguments)
            assert result.isError
        assert bridge.calls == []


@pytest.mark.anyio
@pytest.mark.parametrize("method,path", [
    ("GET", "/health"), ("POST", "/shutdown"), ("POST", "/mcp"),
])
@pytest.mark.parametrize("authorization", [None, "Bearer wrong-secret", f"Basic {TOKEN}"])
async def test_every_http_route_requires_bearer_auth(method, path, authorization):
    shutdown_calls = []
    async with running_app(on_shutdown=lambda: shutdown_calls.append(True)) as (bridge, client):
        headers = {} if authorization is None else {"Authorization": authorization}
        response = await client.request(method, path, headers=headers)
        assert response.status_code == 401
        assert response.headers["www-authenticate"].startswith("Bearer")
        assert shutdown_calls == []
        assert bridge.calls == []


@pytest.mark.anyio
async def test_authenticated_health_and_shutdown():
    shutdown_calls = []
    async with running_app(on_shutdown=lambda: shutdown_calls.append(True)) as (_, client):
        client.headers["Authorization"] = f"Bearer {TOKEN}"
        response = await client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"name": "hackinvent-kicad-mcp", "status": "running"}
        assert shutdown_calls == []
        response = await client.post("/shutdown")
        assert response.status_code == 200
        assert response.json()["status"] == "stopping"
        assert shutdown_calls == [True]


@pytest.mark.anyio
async def test_shutdown_unavailable_without_callback():
    async with running_app() as (_, client):
        response = await client.post("/shutdown", headers={"Authorization": f"Bearer {TOKEN}"})
        assert response.status_code == 409


@pytest.mark.anyio
@pytest.mark.parametrize("path", ["/health", "/mcp", "/shutdown"])
async def test_hostile_browser_origin_is_rejected_even_with_token(path):
    shutdown_calls = []
    async with running_app(on_shutdown=lambda: shutdown_calls.append(True)) as (bridge, client):
        response = await client.post(path, headers={
            "Authorization": f"Bearer {TOKEN}", "Origin": "https://attacker.example",
        })
        assert response.status_code == 403
        assert shutdown_calls == []
        assert bridge.calls == []


@pytest.mark.anyio
async def test_dns_rebinding_host_is_rejected_even_with_token():
    async with running_app() as (bridge, client):
        response = await client.post("/mcp", headers={
            "Authorization": f"Bearer {TOKEN}", "Host": "attacker.example:8765",
        })
        assert response.status_code == 421
        assert bridge.calls == []


def test_empty_http_token_is_rejected():
    with pytest.raises(ValueError, match="token"):
        create_http_app(create_server(FakeBridge()), "")
