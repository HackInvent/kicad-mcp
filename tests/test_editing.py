"""Exercise editor controls and undoable edits with real kipy protobuf wrappers."""

from copy import deepcopy
from uuid import UUID

from kipy.board_types import (
    BoardLayer,
    BoardSegment,
    BoardText,
    Field,
    FootprintInstance,
    Group,
    Net,
    Pad,
    Track,
    Via,
)
from kipy.proto.common.commands.editor_commands_pb2 import (
    DeleteItems,
    DeleteItemsResponse,
    ItemDeletionStatus,
)
from kipy.proto.common.types.base_types_pb2 import DocumentSpecifier, ItemRequestStatus, KIID
from kipy.util.units import from_mm
import pytest

from kicad_mcp.bridge import KiCadBridge
from kicad_mcp.editing import EditingMixin
from kicad_mcp.errors import BridgeError
from kicad_mcp.inspection import item_uuid


def uid(number):
    return str(UUID(int=number))


def identified(cls, number):
    item = cls()
    if isinstance(item, Field):
        item.text.id.value = uid(number)
    else:
        item.id.value = uid(number)
    return item


class FakeBoard:
    def __init__(self, items=(), children=()):
        self.items = {item_uuid(item): item for item in [*items, *children]}
        self.top_ids = {item_uuid(item) for item in items}
        self.selection = []
        self.active = BoardLayer.BL_F_Cu
        self.visible = [BoardLayer.BL_F_Cu, BoardLayer.BL_B_Cu]
        self.enabled = [BoardLayer.BL_F_Cu, BoardLayer.BL_B_Cu, BoardLayer.BL_F_SilkS]
        self.nets = [Net(name="GND"), Net(name="VCC")]
        self.events = []
        self.fail = None
        self.snapshot = None
        self.pending_deletions = []
        self.client = self
        self.document = DocumentSpecifier(board_filename="test.kicad_pcb")

    def _fail_once(self, stage):
        if self.fail == stage:
            self.fail = None
            raise RuntimeError("private IPC details must not escape")

    def get_items_by_id(self, identifiers):
        assert all(isinstance(identifier, KIID) for identifier in identifiers)
        # Deliberately return a different order: the shared resolver must normalize it.
        return [self.items[i.value] for i in reversed(identifiers) if i.value in self.items]

    def get_items(self, types):
        return [item for identifier, item in self.items.items() if identifier in self.top_ids]

    def get_selection(self):
        return [self.items[identifier] for identifier in self.selection]

    def clear_selection(self):
        self.events.append("clear_selection")
        self.selection = []

    def add_to_selection(self, items):
        self.events.append("add_selection")
        assert all(isinstance(item.id, KIID) for item in items)
        self.selection = list(dict.fromkeys([*self.selection, *(item.id.value for item in items)]))
        self._fail_once("add_selection")

    def remove_from_selection(self, items):
        self.events.append("remove_selection")
        assert all(isinstance(item.id, KIID) for item in items)
        removed = {item.id.value for item in items}
        self.selection = [identifier for identifier in self.selection if identifier not in removed]

    def get_enabled_layers(self):
        return self.enabled

    def get_active_layer(self):
        return self.active

    def set_active_layer(self, layer):
        self.events.append("active_layer")
        self.active = layer
        self._fail_once("active_layer")

    def get_visible_layers(self):
        return self.visible

    def set_visible_layers(self, layers):
        self.events.append("visible_layers")
        self.visible = list(layers)
        self._fail_once("visible_layers")

    def get_nets(self):
        return self.nets

    def begin_commit(self):
        self.events.append("begin")
        self.snapshot = deepcopy((self.items, self.top_ids))
        return "commit"

    def push_commit(self, commit, message):
        assert commit == "commit"
        assert message.startswith("MCP:")
        self.events.append("push")
        self._fail_once("push")
        for identifier in self.pending_deletions:
            self.items.pop(identifier)
            self.top_ids.remove(identifier)
        self.pending_deletions = []
        self.snapshot = None

    def drop_commit(self, commit):
        assert commit == "commit"
        self.events.append("drop")
        self._fail_once("drop")
        self.items, self.top_ids = self.snapshot
        self.pending_deletions = []
        self.snapshot = None

    def create_items(self, items):
        self.events.append("create")
        created = []
        for item in items:
            clone = Via(item.proto)
            clone.id.value = uid(100 + len(self.items))
            self.items[item_uuid(clone)] = clone
            self.top_ids.add(item_uuid(clone))
            created.append(clone)
        self._fail_once("create")
        if self.fail == "empty_create":
            return []
        if self.fail == "empty_id":
            created[0].id.value = ""
        return created

    def send(self, command, response_type):
        assert isinstance(command, DeleteItems)
        assert response_type is DeleteItemsResponse
        assert command.header.document == self.document
        self.events.append("remove")
        response = DeleteItemsResponse(status=ItemRequestStatus.IRS_OK)
        for index, identifier in enumerate(command.item_ids):
            # KiCad stages removals; the board remains unchanged until Push.
            assert identifier.value in self.items
            if self.fail == "partial_remove" and index:
                response.deleted_items.add(id=identifier, status=ItemDeletionStatus.IDS_IMMUTABLE)
            else:
                self.pending_deletions.append(identifier.value)
                response.deleted_items.add(id=identifier, status=ItemDeletionStatus.IDS_OK)
        self._fail_once("remove")
        if self.fail == "missing_ack":
            del response.deleted_items[-1]
        elif self.fail == "duplicate_ack":
            response.deleted_items[-1].id.CopyFrom(response.deleted_items[0].id)
        elif self.fail == "wrong_ack":
            response.deleted_items[-1].id.value = uid(999)
        elif self.fail == "bad_status":
            response.status = ItemRequestStatus.IRS_UNKNOWN
        return response

    def refill_zones(self, *, block):
        assert block is False
        self.events.append("refill")

    def save(self):
        pytest.fail("Editing methods must not save the board")


class Harness(EditingMixin):
    _operation = KiCadBridge._operation
    _commit = KiCadBridge._commit
    _layer = staticmethod(KiCadBridge._layer)

    def __init__(self, board, *, read_only=False):
        KiCadBridge.__init__(self, read_only=read_only)
        self.board = board
        self.board_reads = 0

    def _board(self):
        self.board_reads += 1
        return self.board


@pytest.fixture
def board():
    return FakeBoard([identified(Track, 1), identified(BoardText, 2)])


def test_selection_replace_add_remove_and_empty_replace(board):
    bridge = Harness(board)
    assert bridge.set_selection([uid(1)]) == {
        "mode": "replace",
        "item_ids": [uid(1)],
        "selection_count": 1,
    }
    result = bridge.set_selection([uid(2)], mode="add")
    assert result["item_ids"] == [uid(1), uid(2)]
    result = bridge.set_selection([uid(1)], mode="remove")
    assert result["item_ids"] == [uid(2)]
    assert bridge.set_selection([])["selection_count"] == 0
    assert board.selection == []
    assert "begin" not in board.events


def test_selection_preserves_requested_order_and_noops(board):
    bridge = Harness(board)
    result = bridge.set_selection([uid(2), uid(1)])
    assert result["item_ids"] == [uid(2), uid(1)]
    board.events.clear()
    assert bridge.set_selection([uid(1)], mode="add")["selection_count"] == 2
    assert bridge.set_selection([uid(2), uid(1)])["selection_count"] == 2
    assert board.events == []


def test_selection_handles_field_text_uuid_instead_of_integer_field_id():
    field = identified(Field, 3)
    board = FakeBoard(children=[field])
    result = Harness(board).set_selection([uid(3)])
    assert result["item_ids"] == [uid(3)]
    assert Harness(board).set_selection([uid(3)], "remove")["item_ids"] == []


@pytest.mark.parametrize(
    "item_ids,mode",
    [
        ([], "add"),
        ([], "remove"),
        ([uid(1), uid(1)], "replace"),
        (["not-a-uuid"], "replace"),
        (uid(1), "replace"),
        ([42], "replace"),
        ([uid(1)], "invalid"),
        ([uid(1)], []),
        ([uid(1), uid(99)], "replace"),
    ],
)
def test_invalid_selection_is_rejected_before_changing_editor(board, item_ids, mode):
    board.selection = [uid(2)]
    with pytest.raises(BridgeError):
        Harness(board).set_selection(item_ids, mode)
    assert board.selection == [uid(2)]
    assert board.events == []


def test_selection_failure_restores_previous_selection(board):
    board.selection = [uid(1)]
    board.fail = "add_selection"
    with pytest.raises(BridgeError, match="Changing selection failed") as error:
        Harness(board).set_selection([uid(2)])
    assert "private" not in str(error.value)
    assert board.selection == [uid(1)]
    assert board.events == ["clear_selection", "add_selection", "clear_selection", "add_selection"]


def test_layer_changes_use_canonical_names_and_noops(board):
    bridge = Harness(board)
    assert bridge.set_active_layer("B.Cu") == {"active_layer": "B.Cu"}
    assert board.active == BoardLayer.BL_B_Cu
    assert bridge.set_visible_layers(["F.SilkS"]) == {"visible_layers": ["F.SilkS"]}
    assert board.visible == [BoardLayer.BL_F_SilkS]
    board.events.clear()
    bridge.set_active_layer("B.Cu")
    bridge.set_visible_layers(["F.SilkS"])
    assert board.events == []
    assert bridge.set_visible_layers([]) == {"visible_layers": []}


@pytest.mark.parametrize(
    "method,arguments",
    [
        ("set_active_layer", ["In1.Cu"]),
        ("set_active_layer", ["Unknown"]),
        ("set_visible_layers", [["F.Cu", "In1.Cu"]]),
        ("set_visible_layers", [["F.Cu", "F.Cu"]]),
        ("set_visible_layers", ["F.Cu"]),
        ("set_visible_layers", [[42]]),
    ],
)
def test_layer_validation_is_atomic(board, method, arguments):
    with pytest.raises(BridgeError):
        getattr(Harness(board), method)(*arguments)
    assert board.active == BoardLayer.BL_F_Cu
    assert board.visible == [BoardLayer.BL_F_Cu, BoardLayer.BL_B_Cu]
    assert board.events == []


@pytest.mark.parametrize(
    "method,argument,failure",
    [
        ("set_active_layer", "B.Cu", "active_layer"),
        ("set_visible_layers", ["F.SilkS"], "visible_layers"),
    ],
)
def test_failed_layer_change_restores_editor(board, method, argument, failure):
    board.fail = failure
    with pytest.raises(BridgeError):
        getattr(Harness(board), method)(argument)
    assert board.active == BoardLayer.BL_F_Cu
    assert board.visible == [BoardLayer.BL_F_Cu, BoardLayer.BL_B_Cu]
    assert board.events == [failure, failure]


def test_via_creation_uses_mm_through_padstack_net_and_one_commit(board):
    result = Harness(board).add_via(12.345678, -4.5, 0.7, 0.35, "GND")
    via = board.items[result["via"]["id"]]
    assert isinstance(via, Via)
    assert via.position.x == 12_345_678
    assert via.position.y == -4_500_000
    assert via.diameter == from_mm(0.7)
    assert via.drill_diameter == from_mm(0.35)
    assert via.padstack.drill.start_layer == BoardLayer.BL_F_Cu
    assert via.padstack.drill.end_layer == BoardLayer.BL_B_Cu
    assert via.net.name == "GND"
    assert result["via"]["via_type"] == "VT_THROUGH"
    assert result["via"]["diameter_mm"] == 0.7
    assert result["via"]["drill_diameter_mm"] == 0.35
    assert result["saved"] is False
    assert board.events == ["begin", "create", "push"]


def test_via_defaults_leave_net_unassigned(board):
    result = Harness(board).add_via(0, 0)
    assert result["via"]["diameter_mm"] == 0.6
    assert result["via"]["drill_diameter_mm"] == 0.3
    assert result["via"]["net_name"] == ""


@pytest.mark.parametrize(
    "arguments",
    [
        {"x_mm": float("nan")},
        {"y_mm": float("inf")},
        {"x_mm": True},
        {"x_mm": 2148},
        {"x_mm": 10**400},
        {"diameter_mm": 0},
        {"drill_mm": -0.3},
        {"drill_mm": 0.6},
        {"drill_mm": 0.8},
        {"diameter_mm": 0.0000011, "drill_mm": 0.000001},
        {"diameter_mm": 0.0000001},
        {"net_name": "missing"},
        {"net_name": ""},
    ],
)
def test_invalid_via_is_rejected_without_a_commit(board, arguments):
    with pytest.raises(BridgeError):
        Harness(board).add_via(**{"x_mm": 1, "y_mm": 2, **arguments})
    assert board.events == []
    assert len(board.items) == 2


def test_via_requires_both_outer_copper_layers(board):
    board.enabled.remove(BoardLayer.BL_B_Cu)
    with pytest.raises(BridgeError, match="disabled"):
        Harness(board).add_via(1, 2)
    assert board.events == []


@pytest.mark.parametrize("failure", ["create", "empty_create", "empty_id", "push"])
def test_failed_via_creation_rolls_back(board, failure):
    board.fail = failure
    with pytest.raises(BridgeError):
        Harness(board).add_via(1, 2)
    assert set(board.items) == {uid(1), uid(2)}
    assert board.events[-1] == "drop"


def test_delete_all_requested_items_in_one_undo_step(board):
    result = Harness(board).delete_items([uid(2), uid(1)])
    assert result == {"deleted_ids": [uid(2), uid(1)], "deleted_count": 2, "saved": False}
    assert board.items == {}
    assert board.events == ["begin", "remove", "push"]


@pytest.mark.parametrize("identifiers", [[], [uid(1), uid(1)], [uid(1), uid(99)], ["R1"]])
def test_invalid_deletion_does_not_remove_anything(board, identifiers):
    with pytest.raises(BridgeError):
        Harness(board).delete_items(identifiers)
    assert set(board.items) == {uid(1), uid(2)}
    assert board.events == []


@pytest.mark.parametrize("cls", [Track, BoardText, FootprintInstance, Via, BoardSegment])
def test_locked_objects_cannot_be_deleted(cls):
    item = identified(cls, 1)
    item.locked = True
    board = FakeBoard([item])
    with pytest.raises(BridgeError, match="locked"):
        Harness(board).delete_items([uid(1)])
    assert board.events == []


@pytest.mark.parametrize("cls", [Pad, Field, BoardText, BoardSegment])
def test_footprint_children_cannot_be_deleted_directly(cls):
    footprint = identified(FootprintInstance, 1)
    footprint.locked = True
    child = identified(cls, 2)
    board = FakeBoard([footprint], children=[child])
    with pytest.raises(BridgeError, match="child|unsupported"):
        Harness(board).delete_items([uid(2)])
    assert board.events == []


def test_unlocked_whole_footprint_can_be_deleted():
    board = FakeBoard([identified(FootprintInstance, 1)])
    assert Harness(board).delete_items([uid(1)])["deleted_count"] == 1


def test_group_members_are_protected_when_group_lock_is_not_exposed():
    track = identified(Track, 1)
    group = identified(Group, 2)
    group.items = [track]
    board = FakeBoard([track, group])
    with pytest.raises(BridgeError, match="group whose lock cannot be checked"):
        Harness(board).delete_items([uid(1)])
    assert board.events == []


@pytest.mark.parametrize(
    "failure",
    [
        "remove",
        "partial_remove",
        "missing_ack",
        "duplicate_ack",
        "wrong_ack",
        "bad_status",
        "push",
    ],
)
def test_delete_failure_and_partial_acknowledgement_restore_all_items(board, failure):
    board.fail = failure
    with pytest.raises(BridgeError):
        Harness(board).delete_items([uid(1), uid(2)])
    assert set(board.items) == {uid(1), uid(2)}
    assert board.events[-1] == "drop"


def test_refill_reports_async_request_without_claiming_completion_or_transaction(board):
    result = Harness(board).refill_zones()
    assert result["requested"] is True
    assert result["completed"] is False
    assert result["saved"] is False
    assert "outside the MCP transaction" in result["note"]
    assert board.events == ["refill"]


@pytest.mark.parametrize(
    "method,arguments",
    [
        ("set_selection", [[uid(1)]]),
        ("set_active_layer", ["B.Cu"]),
        ("set_visible_layers", [["B.Cu"]]),
        ("add_via", [1, 2]),
        ("delete_items", [[uid(1)]]),
        ("refill_zones", []),
    ],
)
def test_read_only_blocks_all_editor_changes_before_ipc(board, method, arguments):
    bridge = Harness(board, read_only=True)
    with pytest.raises(BridgeError, match="read-only"):
        getattr(bridge, method)(*arguments)
    assert bridge.board_reads == 0
    assert board.events == []
