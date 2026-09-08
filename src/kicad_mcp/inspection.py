"""Read-only PCB inspection through the official KiCad 10 IPC API."""

from __future__ import annotations

from typing import Any, Sequence
from uuid import UUID

from google.protobuf.json_format import MessageToDict
from kipy.board_types import (
    ArcTrack,
    BoardArc,
    BoardCircle,
    BoardLayer,
    BoardRectangle,
    BoardSegment,
    BoardShape,
    BoardText,
    BoardTextBox,
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
from kipy.geometry import Vector2
from kipy.proto.board.board_pb2 import BoardStackupLayerType
from kipy.proto.common.envelope_pb2 import ApiStatusCode
from kipy.proto.common.types import KIID
from kipy.util.board_layer import canonical_name, is_copper_layer
from kipy.util.units import to_mm

from .bom import custom_fields, natural_key
from .errors import BridgeError


MAX_ITEM_IDS = 500


def item_uuid(item) -> str:
    """Fields carry an integer FieldId; their actual board UUID is in the text."""
    return item.text.id.value if isinstance(item, Field) else item.id.value


def validate_item_ids(item_ids: list[str]) -> list[str]:
    if not isinstance(item_ids, list) or not item_ids or len(item_ids) > MAX_ITEM_IDS:
        raise BridgeError(f"item_ids must contain between 1 and {MAX_ITEM_IDS} KiCad UUIDs.")
    normalized = []
    for value in item_ids:
        try:
            if not isinstance(value, str) or len(value) != 36:
                raise ValueError
            normalized.append(str(UUID(value)))
        except ValueError:
            raise BridgeError(
                "Each item ID must be a KiCad UUID from a list or selection tool."
            ) from None
    if len(set(normalized)) != len(normalized):
        raise BridgeError("item_ids contains duplicates. Request each item only once.")
    return normalized


def resolve_items(board, item_ids: list[str]) -> list:
    """Resolve all requested UUIDs atomically at the read level, preserving order."""
    identifiers = validate_item_ids(item_ids)
    try:
        items = board.get_items_by_id([KIID(value=value) for value in identifiers])
    except ApiError as error:
        # KiCad 10 returns AS_BAD_REQUEST, rather than an empty list, when no
        # requested UUID exists. Do not expose its raw server-provided message.
        if error.code == ApiStatusCode.AS_BAD_REQUEST:
            raise BridgeError(
                "KiCad could not resolve the requested item IDs. "
                "Refresh the item list or selection, check the open board, and retry."
            ) from None
        raise
    by_id = {item_uuid(item).lower(): item for item in items}
    missing = [value for value in identifiers if value not in by_id]
    if missing:
        raise BridgeError(
            f"KiCad could not find these item IDs: {', '.join(missing)}. "
            "Refresh the item list or selection and retry."
        )
    if len(by_id) != len(items) or set(by_id) != set(identifiers):
        raise BridgeError("KiCad returned ambiguous item IDs. Refresh the board and retry.")
    return [by_id[value] for value in identifiers]


def _validate_name(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip() or any(char in value for char in "\0\r\n"):
        raise BridgeError(f"{name} must be a nonempty exact name from the corresponding list tool.")


def ensure_net(board, net_name: str) -> Net:
    """Return one existing named net; omit this call for an unassigned net."""
    _validate_name(net_name, "net_name")
    matches = [net for net in board.get_nets() if net.name == net_name]
    if len(matches) != 1:
        raise BridgeError(
            f"Net {net_name!r} was not found uniquely. Use list_nets to choose a net."
        )
    return matches[0]


def _point(position: Vector2) -> dict[str, float]:
    return {"x_mm": to_mm(position.x), "y_mm": to_mm(position.y)}


def _size(size: Vector2) -> dict[str, float]:
    return {"x_mm": to_mm(size.x), "y_mm": to_mm(size.y)}


def _padstack(stack) -> list[dict[str, Any]]:
    return [
        {
            "layer": canonical_name(layer.layer),
            "shape": PadStackShape.Name(layer.shape),
            "size": _size(layer.size),
            "offset": _point(layer.offset),
        }
        for layer in stack.copper_layers
    ]


def parent_map(footprints: Sequence[FootprintInstance]) -> dict[str, dict[str, Any]]:
    parents = {}
    for footprint in footprints:
        parent = {
            "reference": footprint.reference_field.text.value,
            "footprint_id": footprint.id.value,
            "footprint_locked": footprint.locked,
        }
        children = [
            *footprint.definition.items,
            footprint.reference_field,
            footprint.value_field,
            footprint.datasheet_field,
            footprint.description_field,
        ]
        for child in children:
            if isinstance(child, Field):
                identifier = child.text.id.value
            else:
                identifier = getattr(getattr(child, "id", None), "value", None)
            if identifier:
                if identifier in parents and parents[identifier] != parent:
                    raise BridgeError(
                        "A board child UUID belongs to multiple footprints. Check the board in KiCad."
                    )
                parents[identifier] = parent
    return parents


def serialize_pad(item: Pad, parent: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "id": item.id.value,
        "type": "pad",
        "number": item.number,
        "net_name": item.net.name,
        "position": _point(item.position),
        "rotation_degrees": item.padstack.angle.degrees,
        "pad_type": PadType.Name(item.pad_type),
        "layers": [canonical_name(layer) for layer in item.padstack.layers],
        "copper_layers": _padstack(item.padstack),
        "drill_size": _size(item.padstack.drill.diameter),
        "reference": parent["reference"] if parent else None,
        "footprint_id": parent["footprint_id"] if parent else None,
        "footprint_locked": parent["footprint_locked"] if parent else None,
    }


def serialize_via(item: Via) -> dict[str, Any]:
    return {
        "id": item.id.value,
        "type": "via",
        "via_type": ViaType.Name(item.type),
        "position": _point(item.position),
        "net_name": item.net.name,
        "diameter_mm": to_mm(item.diameter) if item.padstack.copper_layers else None,
        "drill_diameter_mm": to_mm(item.drill_diameter),
        "start_layer": canonical_name(item.padstack.drill.start_layer),
        "end_layer": canonical_name(item.padstack.drill.end_layer),
        "copper_layers": _padstack(item.padstack),
        "locked": item.locked,
    }


def serialize_track(item: Track | ArcTrack) -> dict[str, Any]:
    result = {
        "id": item.id.value,
        "type": "arc" if isinstance(item, ArcTrack) else "track",
        "start": _point(item.start),
        "end": _point(item.end),
        "width_mm": to_mm(item.width),
        "net_name": item.net.name,
        "layer": canonical_name(item.layer),
        "locked": item.locked,
    }
    if isinstance(item, ArcTrack):
        result["mid"] = _point(item.mid)
    return result


def serialize_zone(item: Zone) -> dict[str, Any]:
    return {
        "id": item.id.value,
        "type": "zone",
        "zone_type": ZoneType.Name(item.type),
        "name": item.name,
        "net_name": item.net.name if item.net is not None else None,
        "layers": [canonical_name(layer) for layer in item.layers],
        "priority": item.priority,
        "filled": item.filled,
        "locked": item.locked,
        "clearance_mm": to_mm(item.clearance) if item.clearance is not None else None,
        "min_thickness_mm": to_mm(item.min_thickness) if item.min_thickness is not None else None,
    }


def serialize_item(item, parents: dict[str, dict] | None = None) -> dict[str, Any]:
    """Return useful scalar geometry without unbounded polygon/3D-model payloads."""
    parent = (parents or {}).get(item_uuid(item))
    if isinstance(item, Pad):
        return serialize_pad(item, parent)
    if isinstance(item, Via):
        return serialize_via(item)
    if isinstance(item, (Track, ArcTrack)):
        return serialize_track(item)
    if isinstance(item, Zone):
        return serialize_zone(item)
    if isinstance(item, FootprintInstance):
        identifier = item.definition.id
        return {
            "id": item.id.value,
            "type": "footprint",
            "reference": item.reference_field.text.value,
            "value": item.value_field.text.value,
            "footprint": str(identifier) if identifier.library else identifier.name,
            "position": _point(item.position),
            "rotation_degrees": item.orientation.degrees,
            "layer": canonical_name(item.layer),
            "locked": item.locked,
            "pad_count": len(item.definition.pads),
            "fields": custom_fields(item),
            "dnp": item.attributes.do_not_populate,
            "excluded_from_bom": item.attributes.exclude_from_bill_of_materials,
        }
    if isinstance(item, Field):
        return {
            **serialize_item(item.text, parents),
            "type": "field",
            "name": item.name,
            "field_id": item.field_id,
            "visible": item.visible,
        }
    if isinstance(item, BoardText):
        return {
            "id": item.id.value,
            "type": "text",
            "text": item.value,
            "position": _point(item.position),
            "layer": canonical_name(item.layer),
            "height_mm": to_mm(item.attributes.size.y),
            "rotation_degrees": item.attributes.angle,
            "locked": item.locked,
            "reference": parent["reference"] if parent else None,
        }
    if isinstance(item, BoardTextBox):
        return {
            "id": item.id.value,
            "type": "text_box",
            "text": item.value,
            "top_left": _point(item.top_left),
            "bottom_right": _point(item.bottom_right),
            "layer": canonical_name(item.layer),
            "locked": item.locked,
        }
    if isinstance(item, BoardShape):
        result = {
            "id": item.id.value,
            "type": "shape",
            "shape_type": type(item).__name__,
            "layer": canonical_name(item.layer),
            "locked": item.locked,
            "width_mm": to_mm(item.attributes.stroke.width),
            "net_name": item.net.name,
        }
        if isinstance(item, (BoardSegment, BoardArc)):
            result.update(start=_point(item.start), end=_point(item.end))
            if isinstance(item, BoardArc):
                result["mid"] = _point(item.mid)
        elif isinstance(item, BoardRectangle):
            result.update(top_left=_point(item.top_left), bottom_right=_point(item.bottom_right))
        elif isinstance(item, BoardCircle):
            result.update(center=_point(item.center), radius_mm=to_mm(item.radius()))
        return result
    return {"id": item_uuid(item), "type": type(item).__name__}


class InspectionMixin:
    """Read-only inspection methods for KiCadBridge's serialized connection."""

    def list_pads(
        self, reference: str | None = None, net_name: str | None = None
    ) -> dict[str, Any]:
        with self._operation("Reading pads"):
            if reference is not None:
                _validate_name(reference, "reference")
            if net_name is not None:
                _validate_name(net_name, "net_name")
            board = self._board()
            footprints = board.get_footprints()
            if reference is not None:
                matches = [fp for fp in footprints if fp.reference_field.text.value == reference]
                if len(matches) != 1:
                    raise BridgeError(
                        f"Reference {reference!r} was not found uniquely. Check list_footprints."
                    )
            if net_name is not None:
                ensure_net(board, net_name)
            parents = parent_map(footprints)
            pads = [
                serialize_pad(item, parents.get(item.id.value))
                for item in board.get_pads()
                if (net_name is None or item.net.name == net_name)
                and (
                    reference is None
                    or parents.get(item.id.value, {}).get("reference") == reference
                )
            ]
            pads.sort(
                key=lambda item: (
                    natural_key(item["reference"] or ""),
                    natural_key(item["number"]),
                    item["id"],
                )
            )
            return {"board": board.name, "count": len(pads), "pads": pads}

    def list_vias(self, net_name: str | None = None) -> dict[str, Any]:
        with self._operation("Reading vias"):
            if net_name is not None:
                _validate_name(net_name, "net_name")
            board = self._board()
            if net_name is not None:
                ensure_net(board, net_name)
            vias = [
                serialize_via(item)
                for item in board.get_vias()
                if net_name is None or item.net.name == net_name
            ]
            return {"board": board.name, "count": len(vias), "vias": vias}

    def list_zones(self) -> dict[str, Any]:
        with self._operation("Reading zones"):
            board = self._board()
            zones = [serialize_zone(item) for item in board.get_zones()]
            return {"board": board.name, "count": len(zones), "zones": zones}

    def get_item_details(self, item_ids: list[str]) -> dict[str, Any]:
        with self._operation("Reading item details"):
            identifiers = validate_item_ids(item_ids)
            board = self._board()
            items = resolve_items(board, identifiers)
            parents = parent_map(board.get_footprints())
            return {
                "board": board.name,
                "count": len(items),
                "items": [serialize_item(item, parents) for item in items],
            }

    def get_board_layers(self) -> dict[str, Any]:
        with self._operation("Reading board layers"):
            board = self._board()
            enabled = board.get_enabled_layers()
            visible = set(board.get_visible_layers())
            active = board.get_active_layer()
            return {
                "board": board.name,
                "copper_layer_count": board.get_copper_layer_count(),
                "active_layer": canonical_name(active),
                "layers": [
                    {
                        "id": layer,
                        "name": canonical_name(layer),
                        "user_name": board.get_layer_name(layer),
                        "enabled": True,
                        "visible": layer in visible,
                        "active": layer == active,
                        "copper": is_copper_layer(layer),
                    }
                    for layer in enabled
                ],
            }

    def get_board_stackup(self) -> dict[str, Any]:
        with self._operation("Reading board stackup"):
            board = self._board()
            stackup = board.get_stackup()
            layers = []
            for layer in stackup.layers:
                dielectric = layer.layer == BoardLayer.BL_UNDEFINED
                layers.append(
                    {
                        "layer": None if dielectric else canonical_name(layer.layer),
                        "name": layer.user_name,
                        "type": BoardStackupLayerType.Name(layer.type),
                        "enabled": layer.enabled,
                        "thickness_mm": to_mm(layer.thickness),
                        "material": layer.material_name,
                        "dielectric_layers": [
                            {
                                "material": sublayer.material_name,
                                "thickness_mm": to_mm(sublayer.thickness),
                                "epsilon_r": sublayer.epsilon_r,
                                "loss_tangent": sublayer.loss_tangent,
                            }
                            for sublayer in layer.dielectric.layers
                        ],
                    }
                )
            return {
                "board": board.name,
                "total_thickness_mm": sum(row["thickness_mm"] for row in layers if row["enabled"]),
                "layers": layers,
                "finish": MessageToDict(stackup.proto.finish, preserving_proto_field_name=True),
                "impedance": MessageToDict(
                    stackup.proto.impedance, preserving_proto_field_name=True
                ),
                "edge": MessageToDict(stackup.proto.edge, preserving_proto_field_name=True),
            }

    def get_net_connections(self, net_name: str) -> dict[str, Any]:
        with self._operation("Reading net assignments"):
            _validate_name(net_name, "net_name")
            board = self._board()
            ensure_net(board, net_name)
            parents = parent_map(board.get_footprints())
            pads = [
                serialize_pad(item, parents.get(item.id.value))
                for item in board.get_pads()
                if item.net.name == net_name
            ]
            tracks = [
                serialize_track(item) for item in board.get_tracks() if item.net.name == net_name
            ]
            vias = [serialize_via(item) for item in board.get_vias() if item.net.name == net_name]
            return {
                "board": board.name,
                "net_name": net_name,
                "pads": pads,
                "tracks": tracks,
                "vias": vias,
                "counts": {"pads": len(pads), "tracks": len(tracks), "vias": len(vias)},
                "connectivity_verified": False,
                "note": "Lists net assignments; does not verify copper connectivity, routing completion or DRC.",
            }
