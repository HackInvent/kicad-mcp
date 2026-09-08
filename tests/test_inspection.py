"""Inspection regression tests with real KiCad IPC data wrappers."""

from __future__ import annotations

import json
import threading
from uuid import NAMESPACE_URL, uuid5

import pytest
from kipy.board import BoardStackup
from kipy.board_types import (
    ArcTrack,
    BoardCircle,
    BoardLayer,
    BoardSegment,
    BoardText,
    Field,
    FootprintInstance,
    Net,
    Pad,
    PadStackShape,
    PadType,
    Track,
    Via,
    ViaType,
    Zone,
    ZoneType,
)
from kipy.errors import ApiError
from kipy.geometry import Angle, Vector2
from kipy.proto.board import board_pb2
from kipy.proto.common.envelope_pb2 import ApiStatusCode

from kicad_mcp.bridge import KiCadBridge
from kicad_mcp.errors import BridgeError
from kicad_mcp.inspection import (
    InspectionMixin,
    item_uuid,
    parent_map,
    resolve_items,
    serialize_item,
    serialize_via,
    validate_item_ids,
)


def uid(name):
    return str(uuid5(NAMESPACE_URL, name))


def pad(name, number, net):
    item = Pad()
    item.proto.id.value = uid(name)
    item.number = number
    item.net = Net(name=net)
    item.position = Vector2.from_xy_mm(12.5, 25)
    item.padstack.angle = Angle.from_degrees(90)
    item.pad_type = PadType.PT_SMD
    item.padstack.layers = [BoardLayer.BL_F_Cu, BoardLayer.BL_F_Mask]
    item.padstack.copper_layers[0].shape = PadStackShape.PSS_RECTANGLE
    item.padstack.copper_layers[0].size = Vector2.from_xy_mm(1.2, 0.8)
    return item


def footprint(reference, pads):
    item = FootprintInstance()
    item.proto.id.value = uid(reference)
    item.reference_field.text.value = reference
    item.reference_field.text.proto.id.value = uid(reference + "-ref")
    item.value_field.text.value = "10k"
    item.definition.id.library = "Resistor_SMD"
    item.definition.id.name = "R_0603"
    item.definition.items = pads
    return item


def via(name="via", net="GND"):
    item = Via()
    item.proto.id.value = uid(name)
    item.position = Vector2.from_xy_mm(15, 26)
    item.net = Net(name=net)
    item.diameter = 600_000
    item.drill_diameter = 300_000
    return item


def track(name="track", net="GND", arc=False):
    item = ArcTrack() if arc else Track()
    item.proto.id.value = uid(name)
    item.start = Vector2.from_xy_mm(12.5, 25)
    item.end = Vector2.from_xy_mm(15, 26)
    item.width = 250_000
    item.layer = BoardLayer.BL_F_Cu
    item.net = Net(name=net)
    if arc:
        item.mid = Vector2.from_xy_mm(14, 24)
    return item


class FakeBoard:
    name = "inspection.kicad_pcb"

    def __init__(self):
        self.footprints = [
            footprint("R10", [pad("R10-1", "1", "GND")]),
            footprint("R2", [pad("R2-2", "2", "+3V3"), pad("R2-1", "1", "GND")]),
        ]
        self.pads = [child for item in self.footprints for child in item.definition.pads]
        self.vias = [via(), via("other-via", "+3V3")]
        self.tracks = [track(), track("arc", arc=True), track("other-track", "+3V3")]
        copper = Zone()
        copper.proto.id.value = uid("zone")
        copper.net = Net(name="GND")
        copper.name = "Ground plane"
        copper.layers = [BoardLayer.BL_F_Cu, BoardLayer.BL_B_Cu]
        copper.priority = 2
        copper.proto.filled = True
        rule = Zone()
        rule.proto.id.value = uid("rule-area")
        rule.type = ZoneType.ZT_RULE_AREA
        rule.layers = [BoardLayer.BL_F_Cu]
        self.zones = [copper, rule]
        self.extra = []
        self.lookup_response = None
        self.calls = []

    def get_footprints(self):
        return self.footprints

    def get_pads(self):
        return self.pads

    def get_vias(self):
        return self.vias

    def get_tracks(self):
        return self.tracks

    def get_zones(self):
        return self.zones

    def get_nets(self):
        return [Net(name="GND"), Net(name="+3V3"), Net(name="unused")]

    def get_items_by_id(self, identifiers):
        self.calls.append([identifier.value for identifier in identifiers])
        if self.lookup_response is not None:
            return self.lookup_response
        items = [*self.footprints, *self.pads, *self.vias, *self.tracks, *self.zones, *self.extra]
        return [item for item in reversed(items) if item_uuid(item) in self.calls[-1]]

    def get_enabled_layers(self):
        return [BoardLayer.BL_F_Cu, BoardLayer.BL_B_Cu, BoardLayer.BL_F_SilkS]

    def get_visible_layers(self):
        return [BoardLayer.BL_F_Cu, BoardLayer.BL_F_SilkS]

    def get_active_layer(self):
        return BoardLayer.BL_F_Cu

    def get_copper_layer_count(self):
        return 2

    def get_layer_name(self, layer):
        return {
            BoardLayer.BL_F_Cu: "Signal top",
            BoardLayer.BL_B_Cu: "B.Cu",
            BoardLayer.BL_F_SilkS: "Legend",
        }[layer]

    def get_stackup(self):
        proto = board_pb2.BoardStackup()
        proto.finish.type_name = "ENIG"
        proto.impedance.is_controlled = True
        proto.edge.castellation.has_castellated_pads = True
        for layer_id, kind, thickness in [
            (BoardLayer.BL_F_Cu, board_pb2.BSLT_COPPER, 35_000),
            (BoardLayer.BL_UNDEFINED, board_pb2.BSLT_DIELECTRIC, 1_530_000),
            (BoardLayer.BL_B_Cu, board_pb2.BSLT_COPPER, 35_000),
        ]:
            layer = proto.layers.add()
            layer.layer, layer.type, layer.enabled = layer_id, kind, True
            layer.thickness.value_nm = thickness
            if kind == board_pb2.BSLT_DIELECTRIC:
                sublayer = layer.dielectric.layer.add()
                sublayer.material_name = "FR4"
                sublayer.thickness.value_nm = thickness
                sublayer.epsilon_r = 4.5
                sublayer.loss_tangent = 0.02
        return BoardStackup(proto)


class InspectionHarness(InspectionMixin):
    # Reuse the real bridge's serialization, read-only and error boundary.
    _operation = KiCadBridge._operation

    def __init__(self, board):
        self.board = board
        self._lock = threading.RLock()
        self.read_only = True
        self.connection_count = 0

    def _board(self):
        self.connection_count += 1
        return self.board


@pytest.fixture
def setup_inspection():
    board = FakeBoard()
    return InspectionHarness(board), board


def test_pads_include_absolute_units_parent_reference_and_layer_geometry(setup_inspection):
    bridge, board = setup_inspection
    board.footprints[1].locked = True
    result = bridge.list_pads()
    assert result["count"] == 3
    assert [item["reference"] for item in result["pads"]] == ["R2", "R2", "R10"]
    first = result["pads"][0]
    assert first["number"] == "1"
    assert first["position"] == {"x_mm": 12.5, "y_mm": 25}
    assert first["rotation_degrees"] == 90
    assert first["layers"] == ["F.Cu", "F.Mask"]
    assert first["copper_layers"][0]["size"] == {"x_mm": 1.2, "y_mm": 0.8}
    assert first["pad_type"] == "PT_SMD"
    assert first["footprint_id"] == uid("R2")
    assert first["footprint_locked"] is True
    assert bridge.list_pads(reference="R2", net_name="GND")["count"] == 1
    assert bridge.list_pads(net_name="unused")["count"] == 0
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("options", [{"reference": "missing"}, {"net_name": "missing"}])
def test_pad_filters_reject_unknown_reference_or_net(setup_inspection, options):
    bridge, _ = setup_inspection
    with pytest.raises(BridgeError, match="not found uniquely"):
        bridge.list_pads(**options)


def test_pad_filter_rejects_ambiguous_reference_and_missing_parent_stays_explicit(setup_inspection):
    bridge, board = setup_inspection
    board.footprints.append(footprint("R2", []))
    with pytest.raises(BridgeError, match="not found uniquely"):
        bridge.list_pads(reference="R2")
    board.footprints.clear()
    assert all(item["reference"] is None for item in bridge.list_pads()["pads"])


def test_vias_report_units_and_span_without_mutating(setup_inspection):
    bridge, board = setup_inspection
    before = board.vias[0].proto.SerializeToString()
    result = bridge.list_vias(net_name="GND")
    assert result["count"] == 1
    item = result["vias"][0]
    assert item["via_type"] == "VT_THROUGH"
    assert item["diameter_mm"] == 0.6
    assert item["drill_diameter_mm"] == 0.3
    assert item["start_layer"] == "F.Cu" and item["end_layer"] == "B.Cu"
    assert item["position"] == {"x_mm": 15, "y_mm": 26}
    assert board.vias[0].proto.SerializeToString() == before
    board.vias[0].type = ViaType.VT_BLIND_BURIED
    board.vias[0].padstack.drill.end_layer = BoardLayer.BL_In1_Cu
    assert serialize_via(board.vias[0])["end_layer"] == "In1.Cu"


def test_zones_distinguish_copper_and_rule_areas_without_dumping_polygons(setup_inspection):
    bridge, _ = setup_inspection
    result = bridge.list_zones()
    assert result["count"] == 2
    copper, rule = result["zones"]
    assert copper["net_name"] == "GND" and copper["filled"] is True
    assert copper["priority"] == 2 and copper["layers"] == ["F.Cu", "B.Cu"]
    assert rule["zone_type"] == "ZT_RULE_AREA"
    assert rule["net_name"] is None and rule["clearance_mm"] is None
    assert "outline" not in copper and "filled_polygons" not in copper
    json.dumps(result, allow_nan=False)


def test_item_lookup_uses_real_kiid_wrappers_and_preserves_requested_order(setup_inspection):
    bridge, board = setup_inspection
    ids = [uid("R2-1").upper(), uid("via"), uid("arc"), uid("R10")]
    result = bridge.get_item_details(ids)
    assert board.calls == [[identifier.lower() for identifier in ids]]
    assert [item["id"] for item in result["items"]] == [identifier.lower() for identifier in ids]
    assert [item["type"] for item in result["items"]] == ["pad", "via", "arc", "footprint"]
    assert result["items"][0]["reference"] == "R2"
    assert result["items"][2]["mid"] == {"x_mm": 14, "y_mm": 24}
    assert result["items"][3]["footprint"] == "Resistor_SMD:R_0603"
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize(
    "ids",
    [[], "not-a-list", ["not-a-uuid"], [123], [uid("via"), uid("via").upper()], [uid("via")] * 501],
)
def test_item_id_validation_precedes_connection(setup_inspection, ids):
    bridge, _ = setup_inspection
    with pytest.raises(BridgeError):
        bridge.get_item_details(ids)
    assert bridge.connection_count == 0


def test_missing_or_ambiguous_lookup_is_not_silently_omitted(setup_inspection):
    bridge, board = setup_inspection
    with pytest.raises(BridgeError, match="could not find"):
        bridge.get_item_details([uid("via"), uid("missing")])
    board.lookup_response = [board.vias[0], board.vias[0]]
    with pytest.raises(BridgeError, match="ambiguous"):
        bridge.get_item_details([uid("via")])


def test_field_identity_uses_text_uuid_and_keeps_parent(setup_inspection):
    bridge, board = setup_inspection
    field = Field()
    field.proto.id.id = 7
    field.name = "MPN"
    field.text.proto.id.value = uid("mpn")
    field.text.value = "RC0603"
    field.visible = False
    board.extra.append(field)
    board.footprints[0].definition.add_item(field)
    result = bridge.get_item_details([uid("mpn")])["items"][0]
    assert result["type"] == "field" and result["id"] == uid("mpn")
    assert result["field_id"] == 7 and result["text"] == "RC0603"
    assert result["reference"] == "R10" and result["visible"] is False


def test_known_shape_serializers_are_compact_and_use_mm():
    segment = BoardSegment()
    segment.start, segment.end = Vector2.from_xy_mm(1, 2), Vector2.from_xy_mm(3, 4)
    segment.attributes.stroke.width = 100_000
    assert serialize_item(segment)["start"] == {"x_mm": 1, "y_mm": 2}
    assert serialize_item(segment)["width_mm"] == 0.1
    circle = BoardCircle()
    circle.center = Vector2.from_xy_mm(10, 10)
    circle.radius_point = Vector2.from_xy_mm(11, 10)
    assert serialize_item(circle)["radius_mm"] == 1
    text = BoardText()
    text.value = "KiCad"
    text.position = Vector2.from_xy_mm(20, 30)
    assert serialize_item(text)["text"] == "KiCad"


def test_layer_state_retains_canonical_names_custom_names_and_visibility(setup_inspection):
    bridge, _ = setup_inspection
    result = bridge.get_board_layers()
    assert result["active_layer"] == "F.Cu" and result["copper_layer_count"] == 2
    assert result["layers"][0]["name"] == "F.Cu"
    assert result["layers"][0]["user_name"] == "Signal top"
    assert result["layers"][0]["active"] is True
    assert result["layers"][1]["visible"] is False
    assert result["layers"][2]["copper"] is False


def test_stackup_preserves_order_dielectric_properties_and_converts_thickness(setup_inspection):
    bridge, _ = setup_inspection
    result = bridge.get_board_stackup()
    assert result["total_thickness_mm"] == pytest.approx(1.6)
    assert [layer["layer"] for layer in result["layers"]] == ["F.Cu", None, "B.Cu"]
    assert result["layers"][0]["thickness_mm"] == 0.035
    assert result["layers"][1]["dielectric_layers"] == [
        {"material": "FR4", "thickness_mm": 1.53, "epsilon_r": 4.5, "loss_tangent": 0.02}
    ]
    assert result["finish"] == {"type_name": "ENIG"}
    assert result["impedance"] == {"is_controlled": True}
    assert result["edge"]["castellation"]["has_castellated_pads"] is True
    json.dumps(result, allow_nan=False)


def test_net_summary_filters_assignments_without_claiming_electrical_connectivity(setup_inspection):
    bridge, _ = setup_inspection
    result = bridge.get_net_connections("GND")
    assert result["counts"] == {"pads": 2, "tracks": 2, "vias": 1}
    assert result["connectivity_verified"] is False
    assert "does not verify" in result["note"]
    assert all(
        item["net_name"] == "GND" for kind in ("pads", "tracks", "vias") for item in result[kind]
    )
    assert bridge.get_net_connections("unused")["counts"] == {"pads": 0, "tracks": 0, "vias": 0}
    with pytest.raises(BridgeError, match="not found uniquely"):
        bridge.get_net_connections("missing")


@pytest.mark.parametrize(
    "method,args",
    [
        ("list_pads", {"reference": ""}),
        ("list_vias", {"net_name": ""}),
        ("get_net_connections", {"net_name": None}),
    ],
)
def test_invalid_names_are_rejected_before_connection(setup_inspection, method, args):
    bridge, _ = setup_inspection
    with pytest.raises(BridgeError):
        getattr(bridge, method)(**args)
    assert bridge.connection_count == 0


def test_parent_map_rejects_shared_child_uuid():
    first = footprint("R1", [pad("shared", "1", "GND")])
    second = footprint("R2", [pad("shared", "1", "GND")])
    with pytest.raises(BridgeError, match="multiple footprints"):
        parent_map([first, second])


def test_validated_ids_are_normalized_without_mutating_input():
    ids = [uid("one").upper()]
    assert validate_item_ids(ids) == [uid("one")]
    assert ids == [uid("one").upper()]


def test_all_stale_ids_report_actionable_error_without_leaking_server_text(
    setup_inspection, monkeypatch
):
    bridge, board = setup_inspection

    def missing(_identifiers):
        raise ApiError("server error containing private-token", code=ApiStatusCode.AS_BAD_REQUEST)

    monkeypatch.setattr(board, "get_items_by_id", missing)
    with pytest.raises(BridgeError, match="Refresh the item list") as error:
        bridge.get_item_details([uid("missing")])
    assert "private-token" not in str(error.value)


def test_busy_lookup_error_is_not_misreported_as_stale_ids(setup_inspection, monkeypatch):
    _, board = setup_inspection
    busy = ApiError("busy", code=ApiStatusCode.AS_BUSY)

    def fail(_identifiers):
        raise busy

    monkeypatch.setattr(board, "get_items_by_id", fail)
    with pytest.raises(ApiError) as error:
        resolve_items(board, [uid("via")])
    assert error.value is busy


def test_stackup_sums_every_dielectric_sublayer(setup_inspection, monkeypatch):
    bridge, board = setup_inspection
    stackup = board.get_stackup()
    dielectric = stackup.proto.layers[1]
    # KiCad serializes only the first sublayer into the outer thickness field.
    dielectric.thickness.value_nm = 530_000
    dielectric.dielectric.layer[0].thickness.value_nm = 530_000
    extra = dielectric.dielectric.layer.add()
    extra.material_name = "Prepreg"
    extra.thickness.value_nm = 1_000_000
    monkeypatch.setattr(board, "get_stackup", lambda: stackup)
    result = bridge.get_board_stackup()
    assert result["layers"][1]["thickness_mm"] == pytest.approx(1.53)
    assert result["total_thickness_mm"] == pytest.approx(1.6)
