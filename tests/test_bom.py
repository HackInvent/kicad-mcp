"""BOM regression tests using real kipy 0.7 protobuf-backed objects."""

from __future__ import annotations

import csv
from io import StringIO
import json
from unittest.mock import Mock

import pytest
from kipy.board_types import (
    BoardLayer,
    BoardTextBox,
    Field,
    Footprint3DModel,
    FootprintInstance,
    Pad,
)
from kipy.geometry import Vector2
from kipy.kicad import KiCadVersion

from kicad_mcp.bridge import BridgeError, KiCadBridge


def clone(item):
    return FootprintInstance(item.proto)


def make_field(name, value, field_id=4):
    item = Field()
    item.proto.id.id = field_id
    item.name = name
    item.text.value = value
    item.text.proto.id.value = f"field-{name}-{field_id}"
    item.text.position = Vector2.from_xy_mm(11, 12)
    item.text.layer = BoardLayer.BL_F_Fab
    item.text.attributes.size = Vector2.from_xy_mm(1, 1)
    item.visible = True
    return item


def make_footprint(reference="R1", value="10k", fields=None, *, dnp=False, excluded=False):
    item = FootprintInstance()
    item.proto.id.value = f"footprint-{reference}"
    for field_id, field in enumerate(
        [
            item.reference_field,
            item.value_field,
            item.datasheet_field,
            item.description_field,
        ]
    ):
        field.proto.id.id = field_id
        field.text.proto.id.value = f"{reference}-mandatory-{field_id}"
        field.text.layer = BoardLayer.BL_F_Fab
        field.text.attributes.size = Vector2.from_xy_mm(1, 1)
    item.reference_field.text.value = reference
    item.value_field.text.value = value
    item.position = Vector2.from_xy_mm(20, 30)
    item.layer = BoardLayer.BL_F_Cu
    item.definition.id.library = "Resistor_SMD"
    item.definition.id.name = "R_0603_1608Metric"
    item.attributes.do_not_populate = dnp
    item.attributes.exclude_from_bill_of_materials = excluded
    item.definition.items = [
        make_field(name, text, index + 4)
        for index, (name, text) in enumerate((fields or {}).items())
    ]
    return item


class FakeBoard:
    name = "board.kicad_pcb"

    def __init__(self, footprints):
        self.footprints = footprints
        self.events = []
        self.fail_update = False
        self.fail_push = False
        self.fail_drop = False
        self.response = "normal"

    def get_footprints(self):
        return [clone(item) for item in self.footprints]

    def begin_commit(self):
        self.events.append("begin")
        self.snapshot = self.get_footprints()
        return "bom-commit"

    def update_items(self, items):
        self.events.append("update")
        self.sent = [clone(item) for item in items]
        for index, item in enumerate(self.footprints):
            replacement = next(
                (changed for changed in items if changed.id.value == item.id.value), None
            )
            if replacement is not None:
                self.footprints[index] = clone(replacement)
                if self.fail_update:
                    raise RuntimeError("secret-api-token")
        result = [clone(item) for item in items]
        if self.response == "missing":
            return result[:-1]
        if self.response == "unchanged":
            return [
                clone(item)
                for item in self.snapshot
                if item.id.value in {fp.id.value for fp in items}
            ]
        if self.response == "wrong_id":
            result[0].proto.id.value = "wrong-id"
        elif self.response == "duplicate":
            result[-1] = clone(result[0])
        elif self.response == "wrong_type":
            result[0] = Pad()
        return result

    def push_commit(self, commit, message):
        assert commit == "bom-commit"
        self.events.append("push")
        if self.fail_push:
            raise RuntimeError("secret-api-token")

    def drop_commit(self, commit):
        assert commit == "bom-commit"
        self.events.append("drop")
        if self.fail_drop:
            raise RuntimeError("secret-api-token")
        self.footprints = [clone(item) for item in self.snapshot]


@pytest.fixture
def setup_bom(monkeypatch):
    board = FakeBoard(
        [
            make_footprint("R1", fields={"MPN": "RC0603-10K"}),
            make_footprint("R2", fields={"MPN": "RC0603-10K"}),
        ]
    )
    client = Mock()
    client.get_version.return_value = KiCadVersion(10, 0, 0, "10.0.0")
    client.get_board.return_value = board
    constructor = Mock(return_value=client)
    monkeypatch.setattr("kicad_mcp.bridge.KiCad", constructor)
    return KiCadBridge(token="secret-api-token"), board, constructor


def test_grouping_quantities_and_natural_reference_order(setup_bom):
    bridge, board, _ = setup_bom
    board.footprints = [make_footprint(ref, fields={"MPN": "same"}) for ref in ("R10", "R2", "R1")]
    board.footprints.append(make_footprint("C1", "100nF", {"MPN": "cap"}))
    result = bridge.get_bom()
    assert result["board"] == "board.kicad_pcb"
    assert result["grouped"] is True
    assert result["fields"] == ["MPN"]
    assert result["component_count"] == 4
    assert result["line_count"] == 2
    assert result["rows"][0]["references"] == ["C1"]
    assert result["rows"][1] == {
        "references": ["R1", "R2", "R10"],
        "quantity": 3,
        "value": "10k",
        "footprint": "Resistor_SMD:R_0603_1608Metric",
        "dnp": False,
        "excluded_from_bom": False,
        "fields": {"MPN": "same"},
    }
    ungrouped = bridge.get_bom(grouped=False)
    assert ungrouped["line_count"] == ungrouped["component_count"] == 4
    assert [row["references"] for row in ungrouped["rows"]] == [["C1"], ["R1"], ["R2"], ["R10"]]
    assert board.events == []
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("selected", [None, [], ["Manufacturer"], ["Missing", "Manufacturer"]])
def test_hidden_mpn_always_distinguishes_parts(setup_bom, selected):
    bridge, board, _ = setup_bom
    board.footprints = [
        make_footprint("R1", fields={"MPN": "part-A", "Manufacturer": "Maker"}),
        make_footprint("R2", fields={"MPN": "part-B", "Manufacturer": "Maker"}),
    ]
    result = bridge.get_bom(fields=selected)
    assert result["line_count"] == 2
    assert result["fields"] == (selected if selected is not None else ["Manufacturer", "MPN"])
    if selected and "Missing" in selected:
        assert result["rows"][0]["fields"]["Missing"] == ""


@pytest.mark.parametrize(
    "difference", ["library", "datasheet", "description", "mounting", "library_metadata"]
)
def test_other_part_metadata_distinguishes_groups(setup_bom, difference):
    bridge, board, _ = setup_bom
    item = board.footprints[1]
    if difference == "library":
        item.definition.id.library = "Other_Library"
    elif difference == "datasheet":
        item.datasheet_field.text.value = "https://example.test/datasheet.pdf"
    elif difference == "description":
        item.description_field.text.value = "Precision resistor"
    elif difference == "mounting":
        item.attributes.proto.mounting_style = 1
    else:
        item.definition.proto.attributes.keywords = "precision"
    assert bridge.get_bom()["line_count"] == 2


def test_appearance_and_geometry_do_not_prevent_grouping(setup_bom):
    bridge, board, _ = setup_bom
    board.footprints[1].position = Vector2.from_xy_mm(100, 200)
    field = board.footprints[1].definition.items[0]
    field.visible = False
    field.text.position = Vector2.from_xy_mm(99, 201)
    field.text.attributes.angle = 90
    assert bridge.get_bom()["line_count"] == 1


def test_effective_instance_flags_override_stale_library_defaults(setup_bom):
    bridge, board, _ = setup_bom
    included = make_footprint("R1")
    included.definition.proto.attributes.do_not_populate = True
    included.definition.proto.attributes.exclude_from_bill_of_materials = True
    board.footprints = [
        included,
        make_footprint("R2", dnp=True),
        make_footprint("R3", excluded=True),
        make_footprint("R4", dnp=True, excluded=True),
    ]
    result = bridge.get_bom()
    assert result["component_count"] == 1
    assert result["rows"][0]["references"] == ["R1"]
    assert result["rows"][0]["dnp"] is False
    assert result["rows"][0]["excluded_from_bom"] is False
    assert bridge.get_bom(include_dnp=True)["component_count"] == 2
    assert bridge.get_bom(include_excluded=True)["component_count"] == 2
    assert bridge.get_bom(include_dnp=True, include_excluded=True)["component_count"] == 4


@pytest.mark.parametrize("reference", ["", "R?", "REF**", " R1", "R1\nR2"])
def test_unannotated_or_invalid_references_are_rejected(setup_bom, reference):
    bridge, board, _ = setup_bom
    board.footprints = [make_footprint(reference)]
    with pytest.raises(BridgeError, match="reference"):
        bridge.get_bom()
    assert board.events == []


def test_excluded_unannotated_parts_are_ignored_until_included(setup_bom):
    bridge, board, _ = setup_bom
    board.footprints.append(make_footprint("R?", excluded=True))
    assert bridge.get_bom()["component_count"] == 2
    with pytest.raises(BridgeError, match="unannotated"):
        bridge.get_bom(include_excluded=True)


def test_duplicate_references_or_fields_do_not_silently_collapse(setup_bom):
    bridge, board, _ = setup_bom
    board.footprints.append(make_footprint("R1"))
    with pytest.raises(BridgeError, match="Duplicate footprint references"):
        bridge.get_bom()
    board.footprints.pop()
    board.footprints[0].definition.add_item(make_field("MPN", "other", 5))
    with pytest.raises(BridgeError, match="duplicate custom field"):
        bridge.get_bom()


def test_empty_board_returns_empty_bom_and_csv_header(setup_bom):
    bridge, board, _ = setup_bom
    board.footprints.clear()
    assert bridge.get_bom() == {
        "board": "board.kicad_pcb",
        "grouped": True,
        "fields": [],
        "rows": [],
        "component_count": 0,
        "line_count": 0,
    }
    result = bridge.export_bom(fields=["MPN"])
    assert result["row_count"] == result["component_count"] == 0
    assert list(csv.reader(StringIO(result["csv"]))) == [
        ["References", "Quantity", "Value", "Footprint", "DNP", "Excluded from BOM", "MPN"]
    ]


@pytest.mark.parametrize("delimiter", [",", ";", "\t"])
def test_csv_quotes_unicode_commas_newlines_and_returns_download_metadata(setup_bom, delimiter):
    bridge, board, _ = setup_bom
    board.name = r"C:\PCB\Capteur été.kicad_pcb"
    board.footprints = [
        make_footprint("R1", '10k, ±1%; "précision"', {"Supplier": "Éléments\nParis, France"})
    ]
    result = bridge.export_bom(fields=["Supplier"], delimiter=delimiter)
    assert result["filename"] == "Capteur été-bom.csv"
    assert result["mime_type"] == "text/csv; charset=utf-8"
    assert result["row_count"] == result["component_count"] == 1
    rows = list(csv.reader(StringIO(result["csv"], newline=""), delimiter=delimiter))
    assert rows[1][2] == '10k, ±1%; "précision"'
    assert rows[1][-1] == "Éléments\nParis, France"
    assert board.events == []


@pytest.mark.parametrize(
    "payload",
    ["=1+1", "+SUM(A1)", "-2+3", "@SUM(A1)", " \t=1+1", "\tplain", "\rplain", "\nplain", "＝1+1"],
)
def test_csv_formula_like_cells_are_safe_but_json_is_unchanged(setup_bom, payload):
    bridge, board, _ = setup_bom
    board.footprints = [make_footprint("R1", payload, {"=Header": payload})]
    result = bridge.export_bom()
    rows = list(csv.reader(StringIO(result["csv"], newline="")))
    assert rows[0][-1] == "'=Header"
    assert rows[1][2] == "'" + payload
    assert rows[1][-1] == "'" + payload
    assert bridge.get_bom()["rows"][0]["value"] == payload


def test_csv_protects_reference_and_footprint_cells(setup_bom):
    bridge, board, _ = setup_bom
    item = make_footprint("=R1")
    item.definition.id.library = "@Library"
    board.footprints = [item]
    rows = list(csv.reader(StringIO(bridge.export_bom()["csv"])))
    assert rows[1][0] == "'=R1"
    assert rows[1][3].startswith("'@Library:")


@pytest.mark.parametrize(
    "options",
    [
        {"grouped": 1},
        {"include_dnp": "true"},
        {"include_excluded": None},
        {"fields": "MPN"},
        {"fields": ["MPN", "MPN"]},
        {"fields": [""]},
        {"fields": ["Value"]},
        {"fields": [123]},
    ],
)
def test_bom_option_errors_are_actionable_before_connecting(setup_bom, options):
    bridge, _, constructor = setup_bom
    with pytest.raises(BridgeError):
        bridge.get_bom(**options)
    constructor.assert_not_called()


@pytest.mark.parametrize("delimiter", ["|", "", ",;", None, 1])
def test_invalid_csv_delimiter_is_rejected_without_connecting(setup_bom, delimiter):
    bridge, _, constructor = setup_bom
    with pytest.raises(BridgeError, match="delimiter"):
        bridge.export_bom(delimiter=delimiter)
    constructor.assert_not_called()


def test_batch_update_preserves_geometry_fields_metadata_and_other_flags(setup_bom):
    bridge, board, _ = setup_bom
    original = board.footprints[0]
    pad = Pad()
    pad.position = Vector2.from_xy_mm(22, 30)
    model = Footprint3DModel()
    model.proto.filename = "model.step"
    textbox = BoardTextBox()
    textbox.value = "Assembly note"
    original.definition.items = [*original.definition.items, pad, model, textbox]
    original.attributes.not_in_schematic = True
    original.attributes.exclude_from_position_files = True
    original.attributes.proto.mounting_style = 1
    original.definition.proto.attributes.keywords = "resistor precision"
    original.definition.proto.attributes.description = "Original library description"
    original.definition.proto.attributes.do_not_populate = True
    original.proto.overrides.copper_clearance.value_nm = 170_000
    original.value_field.text.attributes.angle = 25
    baseline = original.proto.SerializeToString(deterministic=True)
    field_before = Field(original.definition.items[0].proto)
    expected = clone(original)
    expected.value_field.text.value = "22k"
    expected.definition.items[0].text.value = "new-part"
    expected.attributes.do_not_populate = False
    expected.attributes.exclude_from_bill_of_materials = True
    untouched = make_footprint("R3")
    board.footprints.append(untouched)
    result = bridge.update_bom_fields(
        ["R2", "R1"], fields={"MPN": "new-part"}, value="22k", dnp=False, exclude_from_bom=True
    )
    assert result["updated_count"] == 2
    assert result["references"] == ["R1", "R2"]
    assert result["saved"] is False
    assert len(result["components"]) == 2
    assert all(row["excluded_from_bom"] for row in result["components"])
    assert board.events == ["begin", "update", "push"]
    assert original.proto.SerializeToString(deterministic=True) == baseline
    assert board.footprints[0].proto == expected.proto
    assert board.footprints[2].proto == untouched.proto
    updated_field = Field(board.footprints[0].definition.items[0].proto)
    updated_field.text.value = field_before.text.value
    assert updated_field.proto == field_before.proto


def test_new_fields_are_nonmandatory_hidden_unique_and_at_component_position(setup_bom):
    bridge, board, _ = setup_bom
    original = board.footprints[0]
    original.definition.items[0].proto.id.id = 9
    bridge.update_bom_fields(["R1"], fields={"Manufacturer": "Maker", "Supplier": "Seller"})
    item = board.footprints[0]
    by_name = {child.name: child for child in item.definition.items if isinstance(child, Field)}
    assert by_name["Manufacturer"].field_id == 10
    assert by_name["Supplier"].field_id == 11
    ids = []
    for name in ("Manufacturer", "Supplier"):
        field = by_name[name]
        assert field.visible is False
        assert field.text.position == item.position
        assert field.text.layer == item.value_field.text.layer
        assert field.text.attributes.size == item.value_field.text.attributes.size
        ids.append(field.text.id.value)
    assert len(set(ids)) == 2
    assert item.value_field.text.id.value not in ids
    assert by_name["MPN"].visible is True
    assert item.reference_field.text.value == "R1"


def test_empty_string_clears_fields_and_value_without_deleting_them(setup_bom):
    bridge, board, _ = setup_bom
    bridge.update_bom_fields(["R1"], fields={"MPN": ""}, value="")
    item = board.footprints[0]
    assert item.value_field.text.value == ""
    assert len(item.definition.items) == 1
    assert item.definition.items[0].name == "MPN"
    assert item.definition.items[0].text.value == ""


def test_false_flags_override_effective_true_without_changing_library_metadata(setup_bom):
    bridge, board, _ = setup_bom
    item = board.footprints[0]
    item.attributes.do_not_populate = item.attributes.exclude_from_bill_of_materials = True
    item.definition.proto.attributes.do_not_populate = True
    item.definition.proto.attributes.exclude_from_bill_of_materials = True
    result = bridge.update_bom_fields(["R1"], dnp=False, exclude_from_bom=False)
    assert result["components"][0]["dnp"] is False
    assert result["components"][0]["excluded_from_bom"] is False
    updated = board.footprints[0]
    assert updated.definition.proto.attributes.do_not_populate is True
    assert updated.definition.proto.attributes.exclude_from_bill_of_materials is True
    assert bridge.get_bom()["component_count"] == 2


def test_unchanged_values_do_not_create_an_undo_entry(setup_bom):
    bridge, board, _ = setup_bom
    result = bridge.update_bom_fields(
        ["R1", "R2"], fields={"MPN": "RC0603-10K"}, value="10k", dnp=False
    )
    assert result == {
        "board": "board.kicad_pcb",
        "updated_count": 0,
        "references": [],
        "components": [],
        "saved": False,
    }
    assert board.events == []


@pytest.mark.parametrize("kind", ["missing", "ambiguous", "locked", "duplicate_field"])
def test_all_targets_are_validated_before_any_write(setup_bom, kind):
    bridge, board, _ = setup_bom
    if kind == "missing":
        board.footprints.pop()
    elif kind == "ambiguous":
        board.footprints.append(make_footprint("R2"))
    elif kind == "locked":
        board.footprints[1].locked = True
    else:
        board.footprints[1].definition.add_item(make_field("MPN", "duplicate", 8))
    before = [item.proto.SerializeToString(deterministic=True) for item in board.footprints]
    with pytest.raises(BridgeError):
        bridge.update_bom_fields(["R1", "R2"], fields={"MPN": "new"})
    assert [item.proto.SerializeToString(deterministic=True) for item in board.footprints] == before
    assert board.events == []


@pytest.mark.parametrize(
    "options",
    [
        {},
        {"fields": {}},
        {"fields": []},
        {"fields": {"": "v"}},
        {"fields": {"MPN": 123}},
        {"fields": {"MPN": "bad\0text"}},
        {"fields": {" MPN": "v"}},
        {"fields": {"M\nPN": "v"}},
        {"value": 123},
        {"value": "bad\0text"},
        {"dnp": 1},
        {"exclude_from_bom": "false"},
    ],
)
def test_invalid_updates_are_rejected_before_connecting(setup_bom, options):
    bridge, _, constructor = setup_bom
    with pytest.raises(BridgeError):
        bridge.update_bom_fields(["R1"], **options)
    constructor.assert_not_called()


@pytest.mark.parametrize(
    "reserved",
    [
        "Reference",
        "references",
        "Value",
        "Footprint",
        "Footprint ID",
        "Datasheet",
        "Description",
        "DNP",
        "Exclude from BOM",
        "quantity",
    ],
)
def test_reserved_fields_require_dedicated_arguments(setup_bom, reserved):
    bridge, _, constructor = setup_bom
    with pytest.raises(BridgeError, match="reserved"):
        bridge.update_bom_fields(["R1"], fields={reserved: "new"})
    constructor.assert_not_called()


@pytest.mark.parametrize("references", [[], "R1", ["R1", "R1"], [""], ["R?"], [123]])
def test_invalid_update_references_are_rejected_before_connecting(setup_bom, references):
    bridge, _, constructor = setup_bom
    with pytest.raises(BridgeError):
        bridge.update_bom_fields(references, value="new")
    constructor.assert_not_called()


@pytest.mark.parametrize("failure", ["fail_update", "fail_push"])
def test_batch_failure_rolls_back_partial_updates_and_redacts_token(setup_bom, failure):
    bridge, board, _ = setup_bom
    setattr(board, failure, True)
    before = [item.proto.SerializeToString(deterministic=True) for item in board.footprints]
    with pytest.raises(BridgeError) as error:
        bridge.update_bom_fields(["R1", "R2"], value="new")
    assert "secret-api-token" not in str(error.value)
    assert board.events[-1] == "drop"
    assert [item.proto.SerializeToString(deterministic=True) for item in board.footprints] == before


@pytest.mark.parametrize(
    "response", ["missing", "unchanged", "wrong_id", "duplicate", "wrong_type"]
)
def test_missing_or_rejected_updates_cancel_whole_transaction(setup_bom, response):
    bridge, board, _ = setup_bom
    board.response = response
    with pytest.raises(BridgeError, match="cancelled"):
        bridge.update_bom_fields(["R1", "R2"], value="new")
    assert board.events == ["begin", "update", "drop"]
    assert all(item.value_field.text.value == "10k" for item in board.footprints)


def test_rollback_failure_reports_uncertain_state(setup_bom):
    bridge, board, _ = setup_bom
    board.fail_update = board.fail_drop = True
    with pytest.raises(BridgeError, match="could not confirm rollback") as error:
        bridge.update_bom_fields(["R1", "R2"], value="new")
    assert "secret-api-token" not in str(error.value)


def test_read_only_bom_reads_work_but_update_is_rejected_without_connecting(setup_bom):
    bridge, board, constructor = setup_bom
    bridge.read_only = True
    with pytest.raises(BridgeError, match="read-only"):
        bridge.update_bom_fields(["R1"], value="new")
    constructor.assert_not_called()
    assert bridge.get_bom()["component_count"] == 2
    assert bridge.export_bom()["component_count"] == 2
    assert board.events == []


def test_unicode_digitlike_field_names_do_not_break_natural_sort(setup_bom):
    bridge, board, _ = setup_bom
    board.footprints = [
        make_footprint(
            "R1", fields={"²": "square", "٢": "arabic", "Field10": "ten", "Field2": "two"}
        )
    ]
    result = bridge.get_bom()
    assert result["fields"] == ["Field2", "Field10", "²", "٢"]
    assert result["rows"][0]["fields"]["²"] == "square"
    assert bridge.export_bom()["component_count"] == 1


def test_csv_namespaces_colliding_existing_field_headers_without_losing_data(setup_bom):
    bridge, board, _ = setup_bom
    fields = {
        "Quantity": "supplier quantity",
        "References": "supplier references",
        "Field:Quantity": "already prefixed",
        "=Name": "formula header",
        "'=Name": "literal header",
        "quantity": "lowercase quantity",
    }
    board.footprints = [make_footprint("R1", fields=fields)]
    result = bridge.get_bom()
    assert result["rows"][0]["fields"] == fields
    rows = list(csv.reader(StringIO(bridge.export_bom()["csv"])))
    assert len(set(name.casefold() for name in rows[0])) == len(rows[0])
    assert rows[0][:6] == [
        "References",
        "Quantity",
        "Value",
        "Footprint",
        "DNP",
        "Excluded from BOM",
    ]
    assert set(rows[1][6:]) == set(fields.values())
    assert "Field:References" in rows[0]
    assert "Field:Field:Quantity" in rows[0]
