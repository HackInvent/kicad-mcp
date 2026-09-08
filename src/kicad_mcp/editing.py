"""Explicit PCB edits and editor controls using KiCad 10's official IPC API."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
import math
from typing import Any

from kipy.board_types import (
    ArcTrack,
    BoardShape,
    BoardText,
    Field,
    FootprintInstance,
    Group,
    Track,
    Via,
    Zone,
)
from kipy.geometry import Vector2
from kipy.proto.common.commands.editor_commands_pb2 import (
    DeleteItems,
    DeleteItemsResponse,
    ItemDeletionStatus,
)
from kipy.proto.common.types.base_types_pb2 import ItemRequestStatus
from kipy.proto.common.types.enums_pb2 import KiCadObjectType
from kipy.util.board_layer import canonical_name
from kipy.util.units import from_mm

from .errors import BridgeError
from .inspection import ensure_net, item_uuid, resolve_items, serialize_via, validate_item_ids


_DELETABLE = (FootprintInstance, Track, ArcTrack, Via, BoardText, BoardShape, Zone)
_TOP_LEVEL_TYPES = [
    KiCadObjectType.KOT_PCB_FOOTPRINT,
    KiCadObjectType.KOT_PCB_TRACE,
    KiCadObjectType.KOT_PCB_ARC,
    KiCadObjectType.KOT_PCB_VIA,
    KiCadObjectType.KOT_PCB_TEXT,
    KiCadObjectType.KOT_PCB_SHAPE,
    KiCadObjectType.KOT_PCB_ZONE,
    KiCadObjectType.KOT_PCB_GROUP,
]


def _distance(value: float, name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BridgeError(f"{name} must be a finite number in millimetres.")
    try:
        number = float(value)
    except OverflowError:
        raise BridgeError(f"{name} must be a finite number in millimetres.") from None
    if not math.isfinite(number) or abs(number) > 2147.483647:
        raise BridgeError(f"{name} must be finite and within KiCad's ±2147.483647 mm range.")
    if positive and (number <= 0 or from_mm(number) < 1):
        raise BridgeError(f"{name} must be positive and at least 0.000001 mm.")
    return number


@contextmanager
def _restore_on_error(restore: Callable[[], None], description: str) -> Iterator[None]:
    """Restore editor state after a failed multi-call UI operation, when possible."""
    try:
        yield
    except BaseException:
        try:
            restore()
        except Exception:
            raise BridgeError(
                f"{description} failed and its previous state could not be restored. "
                "Inspect the KiCad editor before retrying."
            ) from None
        raise


class EditingMixin:
    """Methods composed into KiCadBridge, sharing its lock and transaction helpers."""

    def set_selection(self, item_ids: list[str], mode: str = "replace") -> dict[str, Any]:
        with self._operation("Changing selection", mutation=True):
            if not isinstance(mode, str) or mode not in {"replace", "add", "remove"}:
                raise BridgeError("mode must be replace, add or remove.")
            clear_only = isinstance(item_ids, list) and not item_ids and mode == "replace"
            identifiers = [] if clear_only else validate_item_ids(item_ids)
            board = self._board()
            items = resolve_items(board, identifiers) if identifiers else []
            selectable = [item.text if isinstance(item, Field) else item for item in items]
            previous = list(board.get_selection())
            previous_ids = [item_uuid(item) for item in previous]
            target = set(identifiers)
            if mode == "add":
                target |= set(previous_ids)
            elif mode == "remove":
                target = set(previous_ids) - target

            def restore() -> None:
                board.clear_selection()
                if previous:
                    board.add_to_selection(
                        [item.text if isinstance(item, Field) else item for item in previous]
                    )
                if {item_uuid(item) for item in board.get_selection()} != set(previous_ids):
                    raise BridgeError("KiCad did not confirm restoration of the selection.")

            if target != set(previous_ids):
                with _restore_on_error(restore, "Selection change"):
                    if mode == "replace":
                        board.clear_selection()
                        if items:
                            board.add_to_selection(selectable)
                    elif mode == "add":
                        board.add_to_selection(selectable)
                    else:
                        board.remove_from_selection(selectable)
                    selected = [item_uuid(item) for item in board.get_selection()]
                    if set(selected) != target:
                        raise BridgeError("KiCad did not confirm the requested selection.")
            else:
                selected = previous_ids
            return {"mode": mode, "item_ids": selected, "selection_count": len(selected)}

    def set_active_layer(self, layer: str) -> dict[str, Any]:
        with self._operation("Changing active layer", mutation=True):
            board = self._board()
            requested = self._layer(board, layer)
            previous = board.get_active_layer()
            if requested != previous:
                with _restore_on_error(
                    lambda: board.set_active_layer(previous), "Active layer change"
                ):
                    board.set_active_layer(requested)
                    if board.get_active_layer() != requested:
                        raise BridgeError("KiCad did not confirm the requested active layer.")
            return {"active_layer": canonical_name(requested)}

    def set_visible_layers(self, layers: list[str]) -> dict[str, Any]:
        with self._operation("Changing visible layers", mutation=True):
            if not isinstance(layers, list) or any(not isinstance(layer, str) for layer in layers):
                raise BridgeError("layers must be a list of canonical KiCad layer names.")
            if len(set(layers)) != len(layers):
                raise BridgeError("layers must not contain duplicate layer names.")
            board = self._board()
            requested = [self._layer(board, layer) for layer in layers]
            previous = list(board.get_visible_layers())
            if set(requested) != set(previous):
                with _restore_on_error(
                    lambda: board.set_visible_layers(previous), "Layer visibility change"
                ):
                    board.set_visible_layers(requested)
                    visible = list(board.get_visible_layers())
                    if set(visible) != set(requested):
                        raise BridgeError("KiCad did not confirm the requested visible layers.")
            else:
                visible = previous
            return {"visible_layers": [canonical_name(layer) for layer in visible]}

    def add_via(
        self,
        x_mm: float,
        y_mm: float,
        diameter_mm: float = 0.6,
        drill_mm: float = 0.3,
        net_name: str | None = None,
    ) -> dict[str, Any]:
        with self._operation("Adding via", mutation=True):
            position = Vector2.from_xy_mm(_distance(x_mm, "x_mm"), _distance(y_mm, "y_mm"))
            diameter = from_mm(_distance(diameter_mm, "diameter_mm", positive=True))
            drill = from_mm(_distance(drill_mm, "drill_mm", positive=True))
            if drill >= diameter:
                raise BridgeError("drill_mm must be smaller than diameter_mm at KiCad's precision.")
            board = self._board()
            self._layer(board, "F.Cu", copper=True)
            self._layer(board, "B.Cu", copper=True)
            via = Via()  # VT_THROUGH with a normal padstack spanning F.Cu through B.Cu.
            via.position, via.diameter, via.drill_diameter = position, diameter, drill
            if net_name is not None:
                via.net = ensure_net(board, net_name)
            with self._commit(board, "MCP: add through via"):
                created = board.create_items([via])
                if (
                    len(created) != 1
                    or not isinstance(created[0], Via)
                    or not item_uuid(created[0])
                ):
                    raise BridgeError("KiCad did not confirm via creation; the edit was cancelled.")
                result = serialize_via(created[0])
            return {"via": result, "saved": False}

    def delete_items(self, item_ids: list[str]) -> dict[str, Any]:
        with self._operation("Deleting board items", mutation=True):
            identifiers = validate_item_ids(item_ids)
            board = self._board()
            items = resolve_items(board, identifiers)
            top_level = list(board.get_items(types=_TOP_LEVEL_TYPES))
            top_ids = {item_uuid(item) for item in top_level}
            grouped_ids = {
                identifier.value
                for group in top_level
                if isinstance(group, Group)
                for identifier in group.proto.items
            }
            for item in items:
                identifier = item_uuid(item)
                if not isinstance(item, _DELETABLE) or identifier not in top_ids:
                    raise BridgeError(
                        f"Item {identifier!r} is a footprint child or an unsupported deletion type. "
                        "Delete the whole footprint by its own ID, or edit this object in KiCad."
                    )
                if item.locked:
                    raise BridgeError(f"Item {identifier!r} is locked. Unlock it in KiCad first.")
                if identifier in grouped_ids:
                    # KiCad 10's Group protobuf omits the group's lock state.
                    raise BridgeError(
                        f"Item {identifier!r} belongs to a group whose lock cannot be checked. "
                        "Remove it from the group in KiCad before deleting it."
                    )
            command = DeleteItems()
            command.header.document.CopyFrom(board.document)
            command.item_ids.extend(item.id for item in items)
            with self._commit(board, "MCP: delete board items"):
                # remove_items_by_id discards per-item statuses in kipy 0.7.1.
                # Use the public client/document properties with official messages.
                # Deletions are staged until Push, so inspecting the board here
                # would incorrectly see the items that are about to be removed.
                response = board.client.send(command, DeleteItemsResponse)
                confirmed = [entry.id.value for entry in response.deleted_items]
                if (
                    response.status != ItemRequestStatus.IRS_OK
                    or len(confirmed) != len(identifiers)
                    or set(confirmed) != set(identifiers)
                    or any(
                        entry.status != ItemDeletionStatus.IDS_OK
                        for entry in response.deleted_items
                    )
                ):
                    raise BridgeError(
                        "KiCad did not confirm all deletions; the edit was cancelled."
                    )
            return {"deleted_ids": identifiers, "deleted_count": len(identifiers), "saved": False}

    def refill_zones(self) -> dict[str, Any]:
        with self._operation("Requesting zone refill", mutation=True):
            board = self._board()
            # The official handler schedules KiCad's native zoneFillAll action. It
            # cannot be part of our IPC commit. kipy 0.7.1's blocking poll also lacks
            # a timeout counter increment, so return an honest asynchronous result.
            board.refill_zones(block=False)
            return {
                "requested": True,
                "completed": False,
                "saved": False,
                "note": "KiCad will refill all zones asynchronously using its native action. "
                "Wait for the editor to finish before inspecting or saving. "
                "This action is outside the MCP transaction and cannot be rolled back here.",
            }
