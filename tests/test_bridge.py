"""Exercise real kipy protobuf wrappers against a simulated PCB editor."""

from __future__ import annotations

import json
from unittest.mock import Mock

import pytest
from kipy.board_types import (
    ArcTrack,
    BoardLayer,
    BoardText,
    BoardTextBox,
    Field,
    Footprint3DModel,
    FootprintInstance,
    Net,
    Pad,
    Track,
)
from kipy.errors import ConnectionError as KiCadConnectionError
from kipy.geometry import Vector2
from kipy.kicad import KiCadVersion

from kicad_mcp.bridge import BridgeError, KiCadBridge


def clone(item):
    return type(item)(item.proto)


def footprint(reference="R1", *, locked=False):
    item = FootprintInstance()
    item.proto.id.value = f"uuid-{reference}"
    item.reference_field.text.value = reference
    item.value_field.text.value = "10k"
    item.position = Vector2.from_xy_mm(10, 20)
    item.layer = BoardLayer.BL_F_Cu
    item.locked = locked
    pad = Pad()
    pad.position = Vector2.from_xy_mm(11, 20)
    item.definition.items = [pad]
    return item


class FakeBoard:
    name = "test.kicad_pcb"

    def __init__(self):
        self.footprints = [footprint()]
        self.created = []
        self.events = []
        self.selection = []
        self.fail_update = False
        self.fail_push = False
        self.fail_drop = False
        self.empty_create = False

    def get_footprints(self):
        return [clone(item) for item in self.footprints]

    def get_nets(self):
        return [Net(name="GND"), Net(name="+3V3")]

    def get_tracks(self):
        return [item for item in self.created if isinstance(item, (Track, ArcTrack))]

    def get_enabled_layers(self):
        return [
            BoardLayer.BL_F_Cu,
            BoardLayer.BL_B_Cu,
            BoardLayer.BL_F_SilkS,
            BoardLayer.BL_B_SilkS,
        ]

    def get_copper_layer_count(self):
        return 2

    def get_selection(self):
        return self.selection

    def begin_commit(self):
        self.events.append("begin")
        self.snapshot = (
            [clone(item) for item in self.footprints],
            [clone(item) for item in self.created],
        )
        return "commit-id"

    def push_commit(self, commit, message):
        assert commit == "commit-id"
        self.events.append("push")
        if self.fail_push:
            raise RuntimeError("token-secret")
        self.message = message

    def drop_commit(self, commit):
        assert commit == "commit-id"
        self.events.append("drop")
        if self.fail_drop:
            raise RuntimeError("token-secret")
        self.footprints, self.created = self.snapshot

    def update_items(self, items):
        self.events.append("update")
        self.footprints = [clone(item) for item in items]
        if self.fail_update:
            raise RuntimeError("token-secret")
        return self.get_footprints()

    def create_items(self, items):
        self.events.append("create")
        for item in items:
            item.proto.id.value = "new-item-id"
            self.created.append(clone(item))
        return [] if self.empty_create else [clone(item) for item in items]

    def save(self):
        self.events.append("save")


@pytest.fixture
def setup_bridge(monkeypatch):
    board = FakeBoard()
    client = Mock()
    client.get_version.return_value = KiCadVersion(10, 0, 0, "10.0.0")
    client.get_board.return_value = board
    constructor = Mock(return_value=client)
    monkeypatch.setattr("kicad_mcp.bridge.KiCad", constructor)
    return KiCadBridge(token="token-secret"), board, client, constructor


def test_connection_is_lazy_and_captures_launch_environment(monkeypatch, setup_bridge):
    _, _, _, constructor = setup_bridge
    monkeypatch.setenv("KICAD_API_SOCKET", "ipc:///tmp/launch.sock")
    monkeypatch.setenv("KICAD_API_TOKEN", "original-token")
    bridge = KiCadBridge()
    constructor.assert_not_called()
    monkeypatch.setenv("KICAD_API_TOKEN", "changed-token")
    monkeypatch.setenv("KICAD_API_SOCKET", "ipc:///tmp/other.sock")
    result = bridge.status()
    assert result == {
        "connected": True,
        "kicad_version": "10.0.0",
        "supported": True,
        "read_only": False,
    }
    assert constructor.call_args.kwargs["socket_path"] == "ipc:///tmp/launch.sock"
    assert constructor.call_args.kwargs["kicad_token"] == "original-token"
    assert "token" not in json.dumps(result)


def test_reads_use_actual_reference_field_and_json_units(setup_bridge):
    bridge, board, _, _ = setup_bridge
    board.footprints.append(footprint("R10"))
    result = bridge.list_footprints("R1")["footprints"]
    assert len(result) == 1
    assert result[0]["reference"] == "R1"
    assert result[0]["position"] == {"x_mm": 10.0, "y_mm": 20.0}
    assert result[0]["value"] == "10k"
    assert result[0]["pad_count"] == 1
    assert bridge.list_footprints("r1") == {"footprints": []}
    assert bridge.list_nets() == {"nets": [{"name": "GND"}, {"name": "+3V3"}]}
    assert bridge.board_info()["copper_layer_count"] == 2
    json.dumps(bridge.board_info(), allow_nan=False)


def test_move_updates_real_pad_geometry_and_preserves_models(setup_bridge):
    bridge, board, _, _ = setup_bridge
    model = Footprint3DModel()
    board.footprints[0].definition.add_item(model)
    result = bridge.move_footprint("R1", 30, 40, 450)
    item = board.footprints[0]
    assert item.position == Vector2.from_xy_mm(30, 40)
    assert item.orientation.degrees == 90
    # KiCad's screen coordinates have y pointing downwards.
    assert item.definition.pads[0].position == Vector2.from_xy_mm(30, 39)
    assert len(item.definition.models) == 1
    assert result["footprint"]["rotation_degrees"] == 90
    assert result["saved"] is False
    assert board.events == ["begin", "update", "push"]


@pytest.mark.parametrize("kind", ["missing", "duplicate", "locked", "unsupported"])
def test_move_rejects_invalid_target_without_transaction(setup_bridge, kind):
    bridge, board, _, _ = setup_bridge
    if kind == "missing":
        board.footprints.clear()
    elif kind == "duplicate":
        board.footprints.append(footprint())
    elif kind == "locked":
        board.footprints[0].locked = True
    else:
        board.footprints[0].definition.add_item(BoardTextBox())
    with pytest.raises(BridgeError):
        bridge.move_footprint("R1", 20, 30)
    assert board.events == []


@pytest.mark.parametrize("failure", ["fail_update", "fail_push"])
def test_failed_mutation_rolls_back_and_redacts_errors(setup_bridge, failure):
    bridge, board, _, _ = setup_bridge
    setattr(board, failure, True)
    with pytest.raises(BridgeError) as error:
        bridge.move_footprint("R1", 30, 40)
    assert "token-secret" not in str(error.value)
    assert board.events[-1] == "drop"
    assert board.footprints[0].position == Vector2.from_xy_mm(10, 20)
    assert "save" not in board.events


def test_failed_rollback_reports_uncertain_state(setup_bridge):
    bridge, board, _, _ = setup_bridge
    board.fail_update = board.fail_drop = True
    with pytest.raises(BridgeError, match="could not confirm rollback") as error:
        bridge.move_footprint("R1", 30, 40)
    assert "token-secret" not in str(error.value)


def test_add_track_uses_existing_net_and_nanometres(setup_bridge):
    bridge, board, _, _ = setup_bridge
    result = bridge.add_track(1.25, 2, 3.5, 4, width_mm=0.3, layer="B.Cu", net_name="GND")
    item = board.created[0]
    assert item.start.x == 1_250_000
    assert item.end.x == 3_500_000
    assert item.width == 300_000
    assert item.layer == BoardLayer.BL_B_Cu
    assert item.net.name == "GND"
    assert result["track"]["id"] == "new-item-id"
    assert result["saved"] is False
    assert bridge.list_tracks()["tracks"][0]["width_mm"] == 0.3
    assert board.events == ["begin", "create", "push"]


@pytest.mark.parametrize(
    "options",
    [
        {"layer": "F.SilkS"},
        {"layer": "bad-layer"},
        {"layer": "In1.Cu"},
        {"net_name": "missing"},
        {"width_mm": 0},
        {"width_mm": -1},
        {"width_mm": 0.0000001},
        {"width_mm": float("nan")},
    ],
)
def test_invalid_track_rejected_before_commit(setup_bridge, options):
    bridge, board, _, _ = setup_bridge
    with pytest.raises(BridgeError):
        bridge.add_track(0, 0, 1, 1, **options)
    assert board.events == []


@pytest.mark.parametrize("coordinate", [float("nan"), float("inf"), -float("inf"), 1e100, True])
def test_coordinates_are_validated_before_connection(setup_bridge, coordinate):
    bridge, _, _, constructor = setup_bridge
    with pytest.raises(BridgeError):
        bridge.move_footprint("R1", coordinate, 0)
    constructor.assert_not_called()


def test_empty_create_response_is_rolled_back(setup_bridge):
    bridge, board, _, _ = setup_bridge
    board.empty_create = True
    with pytest.raises(BridgeError, match="did not confirm"):
        bridge.add_track(0, 0, 1, 1)
    assert board.events == ["begin", "create", "drop"]
    assert board.created == []


def test_text_dimensions_and_back_layer_mirroring(setup_bridge):
    bridge, board, _, _ = setup_bridge
    result = bridge.add_text("HackInvent\nKiCad", 12, 18, "B.SilkS", 1.2)
    item = board.created[0]
    assert isinstance(item, BoardText)
    assert item.position == Vector2.from_xy_mm(12, 18)
    assert item.attributes.size == Vector2.from_xy_mm(1.2, 1.2)
    assert item.attributes.stroke_width == 199_999 or item.attributes.stroke_width == 200_000
    assert item.attributes.mirrored and item.attributes.multiline
    assert result["text"]["height_mm"] == 1.2
    assert result["saved"] is False


def test_selection_includes_real_fields_and_arc_midpoints(setup_bridge):
    bridge, board, _, _ = setup_bridge
    field = Field()
    field.name = "Reference"
    field.text.value = "R1"
    field.text.layer = BoardLayer.BL_F_SilkS
    arc = ArcTrack()
    arc.mid = Vector2.from_xy_mm(2, 3)
    board.selection = [footprint(), field, arc, Pad()]
    result = bridge.get_selection()["selection"]
    assert result[0]["type"] == "footprint"
    assert result[1]["text"] == "R1"
    assert result[2]["mid"] == {"x_mm": 2, "y_mm": 3}
    assert result[3]["type"] == "Pad"
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize(
    "method,args",
    [
        ("move_footprint", ("R1", 1, 2)),
        ("add_track", (0, 0, 1, 1)),
        ("add_text", ("hello", 0, 0)),
        ("save_board", ()),
    ],
)
def test_read_only_rejects_all_mutations_without_connecting(setup_bridge, method, args):
    bridge, board, _, constructor = setup_bridge
    bridge.read_only = True
    with pytest.raises(BridgeError, match="read-only"):
        getattr(bridge, method)(*args)
    assert board.events == []
    constructor.assert_not_called()


def test_save_is_a_separate_explicit_action(setup_bridge):
    bridge, board, _, _ = setup_bridge
    assert bridge.save_board() == {"name": "test.kicad_pcb", "saved": True}
    assert board.events == ["save"]


def test_connection_failures_are_actionable_and_do_not_expose_tokens(setup_bridge):
    bridge, _, client, _ = setup_bridge
    client.get_version.side_effect = KiCadConnectionError("token-secret")
    status = bridge.status()
    assert status["connected"] is False
    assert "enable the IPC API" in status["error"]
    assert "token-secret" not in json.dumps(status)
    with pytest.raises(BridgeError, match="Cannot reach KiCad"):
        bridge.board_info()


def test_old_kicad_is_reported_and_rejected(setup_bridge):
    bridge, _, client, _ = setup_bridge
    client.get_version.return_value = KiCadVersion(9, 0, 0, "9.0.0")
    assert bridge.status()["supported"] is False
    with pytest.raises(BridgeError, match="10.0 or newer"):
        bridge.list_nets()
    client.get_board.assert_not_called()
