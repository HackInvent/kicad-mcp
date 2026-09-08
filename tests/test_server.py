"""Exercise the real MCP protocol and HTTP boundary without a KiCad process."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from kicad_mcp import __version__
from kicad_mcp.bridge import BridgeError
from kicad_mcp.server import create_http_app, create_server


TOKEN = "test-secret-that-is-not-a-real-credential"
BASE_URL = "http://127.0.0.1:8765"
READ_TOOLS = {
    "kicad_status", "get_board_info", "list_footprints", "list_nets",
    "list_tracks", "get_selection", "get_bom", "export_bom",
    "list_pads", "list_vias", "list_zones", "get_item_details",
    "get_board_layers", "get_board_stackup", "get_net_connections",
}
DESTRUCTIVE_TOOLS = {
    "move_footprint", "add_track", "add_text", "save_board", "update_bom_fields",
    "add_via", "delete_items", "refill_zones",
}
VIEW_TOOLS = {"set_selection", "set_active_layer", "set_visible_layers"}
ARTIFACT_TOOLS = {"run_drc", "export_fabrication"}
WRITE_TOOLS = DESTRUCTIVE_TOOLS | VIEW_TOOLS | ARTIFACT_TOOLS


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

    def list_pads(self, reference=None, net_name=None):
        return self._record("list_pads", reference=reference, net_name=net_name)

    def list_vias(self, net_name=None):
        return self._record("list_vias", net_name=net_name)

    def list_zones(self):
        return self._record("list_zones")

    def get_item_details(self, item_ids):
        return self._record("get_item_details", item_ids=item_ids)

    def get_board_layers(self):
        return self._record("get_board_layers")

    def get_board_stackup(self):
        return self._record("get_board_stackup")

    def get_net_connections(self, net_name):
        return self._record("get_net_connections", net_name=net_name)

    def set_selection(self, item_ids, mode="replace"):
        return self._record("set_selection", item_ids=item_ids, mode=mode)

    def set_active_layer(self, layer):
        return self._record("set_active_layer", layer=layer)

    def set_visible_layers(self, layers):
        return self._record("set_visible_layers", layers=layers)

    def add_via(self, x_mm, y_mm, diameter_mm=0.6, drill_mm=0.3, net_name=None):
        return self._record("add_via", x_mm=x_mm, y_mm=y_mm,
                            diameter_mm=diameter_mm, drill_mm=drill_mm, net_name=net_name)

    def delete_items(self, item_ids):
        return self._record("delete_items", item_ids=item_ids)

    def refill_zones(self):
        return self._record("refill_zones")

    def run_drc(self, severity="all"):
        return self._record("run_drc", severity=severity)

    def export_fabrication(self, formats=None):
        return self._record("export_fabrication", formats=formats)

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

    def get_bom(self, grouped=True, include_dnp=False, include_excluded=False, fields=None):
        return self._record("get_bom", grouped=grouped, include_dnp=include_dnp,
                            include_excluded=include_excluded, fields=fields)

    def export_bom(self, grouped=True, include_dnp=False, include_excluded=False, fields=None,
                   delimiter=","):
        result = self._record("export_bom", grouped=grouped, include_dnp=include_dnp,
                              include_excluded=include_excluded, fields=fields, delimiter=delimiter)
        result["csv"] = 'References,Quantity,Value\n"R1,R2",2,10k\n'
        return result

    def update_bom_fields(self, references, fields=None, value=None, dnp=None,
                          exclude_from_bom=None):
        if "MISSING" in references:
            raise BridgeError("Footprint MISSING does not exist")
        return self._record("update_bom_fields", references=references, fields=fields,
                            value=value, dnp=dnp, exclude_from_bom=exclude_from_bom)

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
        assert initialized.serverInfo.version == __version__
        assert initialized.capabilities.tools is not None
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        assert set(tools) == READ_TOOLS | WRITE_TOOLS
        for name in READ_TOOLS:
            assert tools[name].annotations.readOnlyHint is True
            assert tools[name].annotations.destructiveHint is False
        for name in WRITE_TOOLS:
            assert tools[name].annotations.readOnlyHint is False
            assert tools[name].annotations.destructiveHint is (name in DESTRUCTIVE_TOOLS)
        for name in VIEW_TOOLS:
            assert tools[name].annotations.idempotentHint is True
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
        for name in WRITE_TOOLS:
            result = await session.call_tool(name, {})
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
            ("list_pads", "list_pads", {"reference": "U1", "net_name": "GND"}),
            ("list_vias", "list_vias", {"net_name": "GND"}),
            ("list_zones", "list_zones", {}),
            ("get_item_details", "get_item_details", {"item_ids": ["item-uuid"]}),
            ("get_board_layers", "get_board_layers", {}),
            ("get_board_stackup", "get_board_stackup", {}),
            ("get_net_connections", "get_net_connections", {"net_name": "+3V3"}),
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
@pytest.mark.parametrize("origin", ["https://attacker.example", ""])
async def test_hostile_browser_origin_is_rejected_even_with_token(path, origin):
    shutdown_calls = []
    async with running_app(on_shutdown=lambda: shutdown_calls.append(True)) as (bridge, client):
        response = await client.post(path, headers={
            "Authorization": f"Bearer {TOKEN}", "Origin": origin,
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


@pytest.mark.anyio
async def test_bom_defaults_filters_and_csv_content_over_mcp():
    async with connected_session() as (bridge, session, _):
        result = tool_data(await session.call_tool("get_bom", {}))
        assert result["arguments"] == {
            "grouped": True, "include_dnp": False, "include_excluded": False, "fields": None,
        }
        options = {"grouped": False, "include_dnp": True, "include_excluded": True,
                   "fields": ["Manufacturer", "MPN"]}
        assert tool_data(await session.call_tool("get_bom", options))["arguments"] == options
        result = tool_data(await session.call_tool("export_bom", {}))
        assert result["arguments"] == {
            "grouped": True, "include_dnp": False, "include_excluded": False,
            "fields": None, "delimiter": ",",
        }
        assert result["csv"] == 'References,Quantity,Value\n"R1,R2",2,10k\n'
        result = tool_data(await session.call_tool("export_bom", dict(options, delimiter=";")))
        assert result["arguments"] == dict(options, delimiter=";")
        assert not any(method in WRITE_TOOLS for method, _ in bridge.calls)


@pytest.mark.anyio
async def test_bom_update_preserves_false_flags_and_unicode_fields():
    async with connected_session() as (bridge, session, _):
        options = {"references": ["R2", "R10"], "value": "10k Ω",
                   "fields": {"Manufacturer": "Example", "MPN": "001234"},
                   "dnp": False, "exclude_from_bom": False}
        result = tool_data(await session.call_tool("update_bom_fields", options))
        assert result == {"method": "update_bom_fields", "arguments": options}
        assert bridge.calls == [("update_bom_fields", options)]
        result = tool_data(await session.call_tool("update_bom_fields", {
            "references": ["R2"], "fields": {"MPN": ""},
        }))
        assert result["arguments"] == {
            "references": ["R2"], "fields": {"MPN": ""}, "value": None,
            "dnp": None, "exclude_from_bom": None,
        }


@pytest.mark.anyio
async def test_read_only_bom_can_export_but_cannot_update():
    async with connected_session(read_only=True) as (bridge, session, _):
        assert not (await session.call_tool("get_bom", {})).isError
        result = tool_data(await session.call_tool("export_bom", {}))
        assert "csv" in result
        assert (await session.call_tool("update_bom_fields", {
            "references": ["R1"], "dnp": True,
        })).isError
        assert [method for method, _ in bridge.calls] == ["get_bom", "export_bom"]


@pytest.mark.anyio
async def test_invalid_bom_argument_types_never_reach_bridge():
    async with connected_session() as (bridge, session, _):
        for tool, args in [
            ("get_bom", {"fields": "MPN"}),
            ("export_bom", {"delimiter": [","]}),
            ("update_bom_fields", {"references": "R1", "dnp": True}),
            ("update_bom_fields", {"references": ["R1"], "fields": {"MPN": 123}}),
        ]:
            assert (await session.call_tool(tool, args)).isError
        assert bridge.calls == []


@pytest.mark.anyio
async def test_new_editing_and_artifact_tools_reach_bridge():
    async with connected_session() as (bridge, session, _):
        calls = [
            ("set_selection", {"item_ids": ["track-uuid"], "mode": "add"}),
            ("set_active_layer", {"layer": "B.Cu"}),
            ("set_visible_layers", {"layers": ["B.Cu", "Edge.Cuts"]}),
            ("add_via", {"x_mm": -2.5, "y_mm": 7.25, "diameter_mm": 0.8,
                         "drill_mm": 0.4, "net_name": "GND"}),
            ("delete_items", {"item_ids": ["track-uuid", "via-uuid"]}),
            ("refill_zones", {}),
            ("run_drc", {"severity": "warning"}),
            ("export_fabrication", {"formats": ["gerbers", "drill", "svg"]}),
        ]
        for tool, arguments in calls:
            assert tool_data(await session.call_tool(tool, arguments)) == {
                "method": tool, "arguments": arguments,
            }
        assert bridge.calls == calls


@pytest.mark.anyio
async def test_new_tool_defaults_and_empty_selection():
    async with connected_session() as (_, session, _):
        cases = [
            ("list_pads", {}, {"reference": None, "net_name": None}),
            ("list_vias", {}, {"net_name": None}),
            ("set_selection", {"item_ids": []}, {"item_ids": [], "mode": "replace"}),
            ("add_via", {"x_mm": 1, "y_mm": 2}, {
                "x_mm": 1, "y_mm": 2, "diameter_mm": 0.6,
                "drill_mm": 0.3, "net_name": None,
            }),
            ("run_drc", {}, {"severity": "all"}),
            ("export_fabrication", {}, {"formats": None}),
        ]
        for tool, arguments, expected in cases:
            assert tool_data(await session.call_tool(tool, arguments))["arguments"] == expected


@pytest.mark.anyio
async def test_new_tools_reject_invalid_schemas_before_bridge_calls():
    async with connected_session() as (bridge, session, _):
        for tool, arguments in [
            ("delete_items", {"item_ids": "all"}),
            ("get_item_details", {}),
            ("set_selection", {"item_ids": {"id": "item-uuid"}}),
            ("get_net_connections", {}),
            ("add_via", {"x_mm": "invalid", "y_mm": 1}),
            ("export_fabrication", {"formats": "gerbers"}),
        ]:
            assert (await session.call_tool(tool, arguments)).isError
        assert bridge.calls == []


@pytest.mark.anyio
@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "LOCALHOST"])
async def test_mcp_accepts_localhost_host_header_without_explicit_port(host):
    async with running_app() as (_, client):
        client.headers["Authorization"] = f"Bearer {TOKEN}"
        client.headers["Host"] = host
        async with streamable_http_client(f"{BASE_URL}/mcp", http_client=client) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                assert len((await session.list_tools()).tools) == 28


@pytest.mark.anyio
@pytest.mark.parametrize("host", [
    "[invalid", "user@127.0.0.1:8765", "127.0.0.1:not-a-port", "localhost:65536",
])
async def test_malformed_host_is_rejected_without_server_error(host):
    async with running_app() as (_, client):
        response = await client.get("/health", headers={
            "Authorization": f"Bearer {TOKEN}", "Host": host,
        })
        assert response.status_code == 421


@pytest.mark.parametrize("token", ["secret-\n-newline", "secret-\u00e9-nonascii", " padded "])
def test_http_token_must_be_usable_in_an_authorization_header(token):
    with pytest.raises(ValueError, match="ASCII") as error:
        create_http_app(create_server(FakeBridge()), token)
    assert token not in str(error.value)


@pytest.mark.anyio
@pytest.mark.parametrize("tool,arguments", [
    ("move_footprint", {"reference": "U1", "x_mm": True, "y_mm": 2}),
    ("add_via", {"x_mm": 1, "y_mm": 2, "drill_mm": False}),
    ("update_bom_fields", {"references": ["R1"], "dnp": 1}),
    ("update_bom_fields", {"references": ["R1"], "exclude_from_bom": "false"}),
])
async def test_mcp_does_not_coerce_boolean_and_numeric_edit_arguments(tool, arguments):
    async with connected_session() as (bridge, session, _):
        result = await session.call_tool(tool, arguments)
        assert result.isError
        assert bridge.calls == []
