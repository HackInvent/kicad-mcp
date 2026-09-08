"""Serialized, lazy access to KiCad's official IPC API.

Public distances are millimetres and angles are degrees. Mutations are explicit
undo steps; saving is a separate operation. No pcbnew/SWIG bindings are used.
"""

from __future__ import annotations

from contextlib import contextmanager
import math
import os
import threading
from typing import Any, Iterator
from uuid import uuid4

from kipy import KiCad
from kipy.board_types import (
    ArcTrack,
    BoardLayer,
    BoardShape,
    BoardText,
    Field,
    Footprint3DModel,
    FootprintInstance,
    Pad,
    Track,
    Zone,
)
from kipy.errors import ConnectionError as KiCadConnectionError
from kipy.geometry import Angle, Vector2
from kipy.util.board_layer import (
    canonical_name,
    is_copper_layer,
    layer_from_canonical_name,
)
from kipy.util.units import from_mm, to_mm

from kicad_mcp import bom
from kicad_mcp.errors import BridgeError
from kicad_mcp.inspection import InspectionMixin
from kicad_mcp.editing import EditingMixin
from kicad_mcp.fabrication import FabricationMixin


def _finite(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BridgeError(f"{name} must be a finite number.")
    try:
        result = float(value)
    except OverflowError:
        raise BridgeError(f"{name} must be a finite number.") from None
    if not math.isfinite(result):
        raise BridgeError(f"{name} must be a finite number.")
    return result


def _distance(value: float, name: str, *, positive: bool = False) -> float:
    value = _finite(value, name)
    # KiCad's PCB geometry uses signed 32-bit nanometres internally.
    if abs(value) > 2147.483647:
        raise BridgeError(f"{name} is outside KiCad's coordinate range (±2147.483647 mm).")
    if positive and (value <= 0 or from_mm(value) < 1):
        raise BridgeError(f"{name} must be positive and at least 0.000001 mm.")
    return value


def _position(point: Vector2) -> dict[str, float]:
    return {"x_mm": to_mm(point.x), "y_mm": to_mm(point.y)}


def _footprint(item: FootprintInstance) -> dict[str, Any]:
    return {
        "id": item.id.value,
        "reference": item.reference_field.text.value,
        "value": item.value_field.text.value,
        "position": _position(item.position),
        "rotation_degrees": item.orientation.degrees,
        "layer": canonical_name(item.layer),
        "locked": item.locked,
        "pad_count": len(item.definition.pads),
    }


def _track(item: Track | ArcTrack) -> dict[str, Any]:
    result = {
        "id": item.id.value,
        "type": "arc" if isinstance(item, ArcTrack) else "track",
        "start": _position(item.start),
        "end": _position(item.end),
        "width_mm": to_mm(item.width),
        "layer": canonical_name(item.layer),
        "net_name": item.net.name,
        "locked": item.locked,
    }
    if isinstance(item, ArcTrack):
        result["mid"] = _position(item.mid)
    return result


def _text(item: BoardText) -> dict[str, Any]:
    return {
        "id": item.id.value,
        "text": item.value,
        "position": _position(item.position),
        "layer": canonical_name(item.layer),
        "height_mm": to_mm(item.attributes.size.y),
    }


def _is_polygon_child(item) -> bool:
    return isinstance(item, Zone) or (
        isinstance(item, BoardShape) and item.proto.shape.WhichOneof("geometry") == "polygon"
    )


def _transform_polygon_child(item, delta: Vector2, angle: Angle, center: Vector2) -> None:
    """Transform every contour, including arcs whose kipy getters return copies."""
    if isinstance(item, Zone):
        polygons = list(item.proto.outline.polygons)
        for fill in item.proto.filled_polygons:
            polygons.extend(fill.shapes.polygons)
    else:
        polygons = item.proto.shape.polygon.polygons
    for polygon in polygons:
        for line in [polygon.outline, *polygon.holes]:
            for node in line.nodes:
                if node.HasField("point"):
                    points = [node.point]
                elif node.HasField("arc"):
                    points = [node.arc.start, node.arc.mid, node.arc.end]
                else:
                    continue
                for point in points:
                    position = (Vector2(point) + delta).rotate(angle, center)
                    point.CopyFrom(position.proto)


def _validate_footprint_coordinates(item: FootprintInstance) -> None:
    """Prevent KiCad's int64 IPC coordinates from wrapping when read into int32 geometry."""

    def validate(message):
        if message.DESCRIPTOR.full_name == "kiapi.common.types.Vector2":
            if any(value < -(2**31) or value > 2**31 - 1 for value in (message.x_nm, message.y_nm)):
                raise BridgeError(
                    "The move would place footprint geometry outside KiCad's coordinate range. "
                    "Choose a position closer to the board origin."
                )
            return
        for descriptor, value in message.ListFields():
            if descriptor.message_type is not None:
                if descriptor.label == descriptor.LABEL_REPEATED:
                    for child in value:
                        validate(child)
                else:
                    validate(value)

    validate(item.proto)
    # Definition children are packed in Any messages; inspect their real wrappers.
    for child in item.definition.items:
        validate(child.proto)


class KiCadBridge(InspectionMixin, EditingMixin, FabricationMixin):
    """A bridge bound to one KiCad instance, with one IPC request at a time."""

    def __init__(
        self,
        socket_path: str | None = None,
        token: str | None = None,
        read_only: bool = False,
    ) -> None:
        # Capture the launch environment now, without connecting or exposing it.
        self._socket_path = (
            socket_path if socket_path is not None else os.getenv("KICAD_API_SOCKET")
        )
        self._token = token if token is not None else os.getenv("KICAD_API_TOKEN", "")
        self.read_only = read_only
        self._lock = threading.RLock()
        self._client: KiCad | None = None
        self._client_name = f"hackinvent-kicad-mcp-{uuid4().hex}"

    def _kicad(self) -> KiCad:
        if self._client is None:
            self._client = KiCad(
                socket_path=self._socket_path,
                kicad_token=self._token,
                client_name=self._client_name,
                timeout_ms=2000,
            )
        return self._client

    def _board(self):
        client = self._kicad()
        if client.get_version().major < 10:
            raise BridgeError(
                "KiCad 10.0 or newer is required. Open the board in a supported version."
            )
        return client.get_board()

    @contextmanager
    def _operation(self, action: str, *, mutation: bool = False) -> Iterator[None]:
        with self._lock:
            if mutation and self.read_only:
                raise BridgeError(
                    "This server is read-only. Restart without --read-only to allow changes."
                )
            try:
                yield
            except BridgeError:
                raise
            except bom.BOMError as error:
                raise BridgeError(str(error)) from None
            except KiCadConnectionError:
                raise BridgeError(
                    "Cannot reach KiCad. Open KiCad 10+ PCB Editor, enable the IPC API "
                    "in Preferences → Plugins, and launch the plugin again. For stdio, "
                    "check KICAD_API_SOCKET and KICAD_API_TOKEN."
                ) from None
            except Exception as error:
                # IPC errors can include server-provided text and credentials.
                # Deliberately report the type, never the raw exception message.
                raise BridgeError(
                    f"{action} failed ({type(error).__name__}). Ensure a PCB is open, "
                    "close modal dialogs in KiCad, and retry."
                ) from None

    @contextmanager
    def _commit(self, board, message: str) -> Iterator[None]:
        commit = board.begin_commit()
        try:
            yield
            board.push_commit(commit, message)
        except BaseException:
            try:
                board.drop_commit(commit)
            except Exception:
                raise BridgeError(
                    "The edit failed and KiCad could not confirm rollback. "
                    "Inspect the board and undo history before retrying or saving."
                ) from None
            raise

    @staticmethod
    def _layer(board, name: str, *, copper: bool = False) -> int:
        if not isinstance(name, str):
            raise BridgeError(
                "layer must be a canonical KiCad layer name, such as F.Cu or F.SilkS."
            )
        layer = layer_from_canonical_name(name)
        if layer in (BoardLayer.BL_UNKNOWN, BoardLayer.BL_UNDEFINED):
            raise BridgeError(
                f"Unknown layer {name!r}. Use a canonical name such as F.Cu or F.SilkS."
            )
        if copper and not is_copper_layer(layer):
            raise BridgeError("Tracks require a copper layer, such as F.Cu, B.Cu or In1.Cu.")
        if layer not in board.get_enabled_layers():
            raise BridgeError(f"Layer {name!r} is disabled. Enable it in KiCad Board Setup first.")
        return layer

    def status(self) -> dict[str, Any]:
        try:
            with self._operation("Connection check"):
                version = self._kicad().get_version()
                return {
                    "connected": True,
                    "kicad_version": version.full_version,
                    "supported": version.major >= 10,
                    "read_only": self.read_only,
                }
        except BridgeError as error:
            return {"connected": False, "read_only": self.read_only, "error": str(error)}

    def board_info(self) -> dict[str, Any]:
        with self._operation("Reading board information"):
            board = self._board()
            return {
                "name": board.name,
                "copper_layer_count": board.get_copper_layer_count(),
                "enabled_layers": [canonical_name(layer) for layer in board.get_enabled_layers()],
                "footprint_count": len(board.get_footprints()),
                "track_count": len(board.get_tracks()),
                "net_count": len(board.get_nets()),
            }

    def list_footprints(self, reference: str | None = None) -> dict[str, Any]:
        with self._operation("Reading footprints"):
            items = self._board().get_footprints()
            if reference is not None:
                items = [item for item in items if item.reference_field.text.value == reference]
            return {"footprints": [_footprint(item) for item in items]}

    def list_nets(self) -> dict[str, Any]:
        with self._operation("Reading nets"):
            return {"nets": [{"name": net.name} for net in self._board().get_nets()]}

    def list_tracks(self) -> dict[str, Any]:
        with self._operation("Reading tracks"):
            return {"tracks": [_track(item) for item in self._board().get_tracks()]}

    def get_selection(self) -> dict[str, Any]:
        with self._operation("Reading selection"):
            result = []
            for item in self._board().get_selection():
                if isinstance(item, FootprintInstance):
                    result.append({"type": "footprint", **_footprint(item)})
                elif isinstance(item, (Track, ArcTrack)):
                    result.append(_track(item))
                elif isinstance(item, BoardText):
                    result.append({"type": "text", **_text(item)})
                elif isinstance(item, Field):
                    result.append({"type": "field", "name": item.name, **_text(item.text)})
                else:
                    identifier = getattr(item, "id", None)
                    result.append(
                        {"type": type(item).__name__, "id": getattr(identifier, "value", None)}
                    )
            return {"selection": result}

    def move_footprint(
        self,
        reference: str,
        x_mm: float,
        y_mm: float,
        rotation_degrees: float | None = None,
    ) -> dict[str, Any]:
        with self._operation("Moving footprint", mutation=True):
            x_mm, y_mm = _distance(x_mm, "x_mm"), _distance(y_mm, "y_mm")
            if not isinstance(reference, str) or not reference.strip():
                raise BridgeError("reference must be an exact footprint reference, such as R1.")
            rotation = None
            if rotation_degrees is not None:
                # kipy normalizes by repeated subtraction, so bound the input first.
                rotation = math.remainder(_finite(rotation_degrees, "rotation_degrees"), 360.0)
            board = self._board()
            matches = [
                fp for fp in board.get_footprints() if fp.reference_field.text.value == reference
            ]
            if len(matches) != 1:
                raise BridgeError(
                    f"Expected exactly one footprint with reference {reference!r}; found {len(matches)}. "
                    "Check list_footprints and fix duplicate references in KiCad."
                )
            if matches[0].locked:
                raise BridgeError(
                    f"Footprint {reference!r} is locked. Unlock it in KiCad before moving it."
                )
            item = FootprintInstance(matches[0].proto)
            movable = (Field, Pad, BoardText, Zone, BoardShape)
            children = list(item.definition.items)
            if any(not isinstance(child, (*movable, Footprint3DModel)) for child in children):
                raise BridgeError(
                    "This footprint contains child items that kicad-python cannot safely transform. "
                    "Move it in KiCad instead."
                )
            # kipy 0.7 drops 3D models during rotation, changes only the first
            # zone outline, and mutates detached copies of polygon arc nodes.
            # Keep those children aside and transform their complete geometry here.
            polygons = [child for child in children if _is_polygon_child(child)]
            item.definition.items = [
                child
                for child in children
                if not isinstance(child, Footprint3DModel) and not _is_polygon_child(child)
            ]
            position = Vector2.from_xy_mm(x_mm, y_mm)
            delta = position - item.position
            previous_angle = item.orientation.degrees
            item.position = position
            if rotation is not None:
                item.orientation = Angle.from_degrees(rotation)
            angle = Angle.from_degrees(item.orientation.degrees - previous_angle)
            for child in polygons:
                _transform_polygon_child(child, delta, angle, position)
            transformed = iter(item.definition.items)
            item.definition.items = [
                child
                if isinstance(child, Footprint3DModel) or _is_polygon_child(child)
                else next(transformed)
                for child in children
            ]
            _validate_footprint_coordinates(item)
            with self._commit(board, f"MCP: move {reference}"):
                updated = board.update_items([item])
                if (
                    len(updated) != 1
                    or not isinstance(updated[0], FootprintInstance)
                    or updated[0].id.value != item.id.value
                ):
                    raise BridgeError(
                        "KiCad did not confirm the footprint update; the edit was cancelled."
                    )
                result = _footprint(updated[0])
            return {"footprint": result, "saved": False}

    def add_track(
        self,
        start_x_mm: float,
        start_y_mm: float,
        end_x_mm: float,
        end_y_mm: float,
        width_mm: float = 0.25,
        layer: str = "F.Cu",
        net_name: str | None = None,
    ) -> dict[str, Any]:
        with self._operation("Adding track", mutation=True):
            start = Vector2.from_xy_mm(
                _distance(start_x_mm, "start_x_mm"), _distance(start_y_mm, "start_y_mm")
            )
            end = Vector2.from_xy_mm(
                _distance(end_x_mm, "end_x_mm"), _distance(end_y_mm, "end_y_mm")
            )
            width = _distance(width_mm, "width_mm", positive=True)
            if start == end:
                raise BridgeError("A track must have different start and end coordinates.")
            board = self._board()
            item = Track()
            item.start, item.end, item.width = start, end, from_mm(width)
            item.layer = self._layer(board, layer, copper=True)
            if net_name is not None:
                nets = [net for net in board.get_nets() if net.name == net_name]
                if len(nets) != 1:
                    raise BridgeError(
                        f"Net {net_name!r} was not found uniquely. Use list_nets to choose a net."
                    )
                item.net = nets[0]
            with self._commit(board, "MCP: add track"):
                created = board.create_items([item])
                if (
                    len(created) != 1
                    or not isinstance(created[0], Track)
                    or not created[0].id.value
                ):
                    raise BridgeError(
                        "KiCad did not confirm track creation; the edit was cancelled."
                    )
                result = _track(created[0])
            return {"track": result, "saved": False}

    def add_text(
        self,
        text: str,
        x_mm: float,
        y_mm: float,
        layer: str = "F.SilkS",
        height_mm: float = 1.0,
    ) -> dict[str, Any]:
        with self._operation("Adding text", mutation=True):
            if not isinstance(text, str) or not text.strip():
                raise BridgeError("text must contain at least one non-whitespace character.")
            position = Vector2.from_xy_mm(_distance(x_mm, "x_mm"), _distance(y_mm, "y_mm"))
            height = _distance(height_mm, "height_mm", positive=True)
            board = self._board()
            item = BoardText()
            item.value, item.position, item.layer = text, position, self._layer(board, layer)
            item.attributes.size = Vector2.from_xy_mm(height, height)
            item.attributes.stroke_width = max(1, from_mm(height / 6))
            item.attributes.line_spacing = 1.0
            item.attributes.multiline = "\n" in text
            item.attributes.mirrored = layer.startswith("B.")
            with self._commit(board, "MCP: add text"):
                created = board.create_items([item])
                if (
                    len(created) != 1
                    or not isinstance(created[0], BoardText)
                    or not created[0].id.value
                ):
                    raise BridgeError(
                        "KiCad did not confirm text creation; the edit was cancelled."
                    )
                result = _text(created[0])
            return {"text": result, "saved": False}

    def get_bom(
        self,
        grouped: bool = True,
        include_dnp: bool = False,
        include_excluded: bool = False,
        fields: list[str] | None = None,
    ) -> dict[str, Any]:
        """Read the PCB BOM, optionally grouping parts with identical full metadata."""
        with self._operation("Reading BOM"):
            bom.validate_options(grouped, include_dnp, include_excluded, fields)
            board = self._board()
            return bom.build_bom(
                board.name, board.get_footprints(), grouped, include_dnp, include_excluded, fields
            )

    def export_bom(
        self,
        grouped: bool = True,
        include_dnp: bool = False,
        include_excluded: bool = False,
        fields: list[str] | None = None,
        delimiter: str = ",",
    ) -> dict[str, Any]:
        """Return spreadsheet-safe CSV text and metadata, without writing any file."""
        with self._operation("Exporting BOM"):
            bom.validate_delimiter(delimiter)
            return bom.render_csv(
                self.get_bom(grouped, include_dnp, include_excluded, fields), delimiter
            )

    def update_bom_fields(
        self,
        references: list[str],
        fields: dict[str, str] | None = None,
        value: str | None = None,
        dnp: bool | None = None,
        exclude_from_bom: bool | None = None,
    ) -> dict[str, Any]:
        """Change selected PCB fields/flags in one undo step, without saving."""
        with self._operation("Updating BOM fields", mutation=True):
            bom.validate_update(references, fields, value, dnp, exclude_from_bom)
            board = self._board()
            prepared = bom.prepare_updates(
                board.get_footprints(), references, fields, value, dnp, exclude_from_bom
            )
            if prepared:
                with self._commit(board, "MCP: update BOM fields"):
                    updated = board.update_items(prepared)
                    bom.verify_updates(prepared, updated)
                    components = bom.build_bom(
                        board.name, updated, grouped=False, include_dnp=True, include_excluded=True
                    )["rows"]
            else:
                components = []
            return {
                "board": board.name,
                "updated_count": len(components),
                "references": [row["references"][0] for row in components],
                "components": components,
                "saved": False,
            }

    def save_board(self) -> dict[str, Any]:
        with self._operation("Saving board", mutation=True):
            board = self._board()
            board.save()
            return {"name": board.name, "saved": True}
