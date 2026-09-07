"""PCB bill-of-materials helpers built on the official kipy wrappers.

KiCad serializes effective BOM/DNP flags on FootprintInstance.attributes.
Definition attributes are library metadata, not a second set of effective flags.
Only part metadata participates in grouping; placement and field visibility do not.
"""

from __future__ import annotations

from collections import Counter
import csv
from io import StringIO
import re
from typing import Any, Sequence
from uuid import uuid4

from kipy.board_types import BoardText, Field, FootprintInstance


class BOMError(ValueError):
    """Invalid BOM input or board metadata, safe to report to the caller."""


_RESERVED_NAMES = {
    "reference",
    "references",
    "value",
    "footprint",
    "footprintid",
    "datasheet",
    "description",
    "dnp",
    "donotpopulate",
    "excludefrombom",
    "excludedfrombom",
    "excludefrombillofmaterials",
    "quantity",
}


def natural_key(value: str) -> tuple:
    """Sort R2 before R10, with a deterministic tie-break for case/zero padding."""
    return (
        tuple(
            (1, int(part)) if part.isascii() and part.isdigit() else (0, part.casefold())
            for part in re.split(r"([0-9]+)", value)
        ),
        value,
    )


def _field_name(name: str) -> None:
    if not isinstance(name, str) or not name.strip() or name != name.strip():
        raise BOMError("Field names must be nonempty strings without surrounding whitespace.")
    if any(character in name for character in "\0\r\n\t"):
        raise BOMError("Field names cannot contain nulls, tabs or line breaks.")
    normalized = re.sub(r"[ _-]", "", name).casefold()
    if normalized in _RESERVED_NAMES:
        raise BOMError(
            f"{name!r} is a reserved field. Use the dedicated value, dnp or exclude_from_bom "
            "argument; edit other built-in fields in KiCad."
        )


def validate_options(
    grouped: bool,
    include_dnp: bool,
    include_excluded: bool,
    fields: list[str] | None,
) -> None:
    for name, value in (
        ("grouped", grouped),
        ("include_dnp", include_dnp),
        ("include_excluded", include_excluded),
    ):
        if not isinstance(value, bool):
            raise BOMError(f"{name} must be a boolean.")
    if fields is not None:
        if not isinstance(fields, list):
            raise BOMError("fields must be a list of custom field names or null.")
        for name in fields:
            _field_name(name)
        if len(set(fields)) != len(fields):
            raise BOMError("fields contains duplicate names. Select each field only once.")


def validate_delimiter(delimiter: str) -> None:
    if delimiter not in (",", ";", "\t"):
        raise BOMError("delimiter must be a comma, semicolon or tab character.")


def _valid_reference(reference: str) -> None:
    if (
        not isinstance(reference, str)
        or not reference.strip()
        or "?" in reference
        or "*" in reference
    ):
        raise BOMError(
            "The BOM contains an unannotated footprint reference. Annotate the board in "
            "KiCad before generating or updating its BOM."
        )
    if reference != reference.strip() or any(char in reference for char in "\0\r\n\t"):
        raise BOMError(
            "Footprint references cannot contain surrounding whitespace or control characters."
        )


def custom_fields(item: FootprintInstance) -> dict[str, str]:
    result: dict[str, str] = {}
    for child in item.definition.items:
        if isinstance(child, Field):
            if child.name in result:
                raise BOMError(
                    f"Footprint {item.reference_field.text.value!r} has duplicate custom field "
                    f"{child.name!r}. Fix its fields in KiCad first."
                )
            result[child.name] = child.text.value
    return result


def component(item: FootprintInstance) -> dict[str, Any]:
    identifier = item.definition.id
    footprint = f"{identifier.library}:{identifier.name}" if identifier.library else identifier.name
    return {
        "references": [item.reference_field.text.value],
        "quantity": 1,
        "value": item.value_field.text.value,
        "footprint": footprint,
        "dnp": item.attributes.do_not_populate,
        "excluded_from_bom": item.attributes.exclude_from_bill_of_materials,
        "fields": custom_fields(item),
    }


def _part_key(item: FootprintInstance, row: dict[str, Any]) -> tuple:
    # All custom fields participate even if the caller hides them from output.
    # Mandatory descriptive fields and non-geometric library/instance attributes
    # also distinguish parts. Geometry/UUIDs and field appearance do not.
    return (
        row["value"],
        row["footprint"],
        row["dnp"],
        row["excluded_from_bom"],
        tuple(sorted(row["fields"].items())),
        item.datasheet_field.text.value,
        item.description_field.text.value,
        item.attributes.proto.SerializeToString(deterministic=True),
        item.definition.proto.attributes.SerializeToString(deterministic=True),
    )


def build_bom(
    board: str,
    footprints: Sequence[FootprintInstance],
    grouped: bool = True,
    include_dnp: bool = False,
    include_excluded: bool = False,
    fields: list[str] | None = None,
) -> dict[str, Any]:
    validate_options(grouped, include_dnp, include_excluded, fields)
    included = [
        item
        for item in footprints
        if (include_dnp or not item.attributes.do_not_populate)
        and (include_excluded or not item.attributes.exclude_from_bill_of_materials)
    ]
    references = [item.reference_field.text.value for item in included]
    for reference in references:
        _valid_reference(reference)
    duplicates = sorted(
        (ref for ref, count in Counter(references).items() if count > 1), key=natural_key
    )
    if duplicates:
        raise BOMError(
            f"Duplicate footprint references in the BOM: {', '.join(duplicates)}. "
            "Give each component a unique reference in KiCad first."
        )
    included.sort(key=lambda item: natural_key(item.reference_field.text.value))
    parts = [(item, component(item)) for item in included]
    selected = (
        list(fields)
        if fields is not None
        else sorted({name for _, row in parts for name in row["fields"]}, key=natural_key)
    )
    rows: list[dict[str, Any]] = []
    groups: dict[tuple, dict[str, Any]] = {}
    for item, full_row in parts:
        key = _part_key(item, full_row)
        if grouped and key in groups:
            groups[key]["references"].extend(full_row["references"])
            groups[key]["quantity"] += 1
        else:
            row = {
                **full_row,
                "fields": {name: full_row["fields"].get(name, "") for name in selected},
            }
            rows.append(row)
            groups[key] = row
    return {
        "board": board,
        "grouped": grouped,
        "fields": selected,
        "rows": rows,
        "component_count": len(included),
        "line_count": len(rows),
    }


def _spreadsheet_safe(value: str) -> str:
    # Protect headers and all textual cells, including references and values.
    # Leading whitespace must not hide a formula marker from this check.
    stripped = value.lstrip()
    if value.startswith(("\t", "\r", "\n")) or stripped.startswith(
        ("=", "+", "-", "@", "＝", "＋", "－", "＠")
    ):
        return "'" + value
    return value


def render_csv(bom: dict[str, Any], delimiter: str = ",") -> dict[str, Any]:
    validate_delimiter(delimiter)
    stream = StringIO(newline="")
    writer = csv.writer(stream, delimiter=delimiter, lineterminator="\r\n")
    header = [
        "References",
        "Quantity",
        "Value",
        "Footprint",
        "DNP",
        "Excluded from BOM",
    ]
    used_headers = {name.casefold() for name in header}
    for name in bom["fields"]:
        candidate = _spreadsheet_safe(name)
        while candidate.casefold() in used_headers:
            candidate = f"Field:{candidate}"
        header.append(candidate)
        used_headers.add(candidate.casefold())
    writer.writerow(header)
    for row in bom["rows"]:
        writer.writerow(
            [
                _spreadsheet_safe(", ".join(row["references"])),
                row["quantity"],
                _spreadsheet_safe(row["value"]),
                _spreadsheet_safe(row["footprint"]),
                "yes" if row["dnp"] else "no",
                "yes" if row["excluded_from_bom"] else "no",
                *[_spreadsheet_safe(row["fields"][name]) for name in bom["fields"]],
            ]
        )
    basename = bom["board"].replace("\\", "/").rsplit("/", 1)[-1]
    stem = basename.rsplit(".", 1)[0] if "." in basename else basename
    stem = re.sub(r"[\x00-\x1f\x7f]", "_", stem).strip() or "board"
    return {
        "csv": stream.getvalue(),
        "filename": f"{stem}-bom.csv",
        "mime_type": "text/csv; charset=utf-8",
        "row_count": bom["line_count"],
        "component_count": bom["component_count"],
    }


def validate_update(
    references: list[str],
    fields: dict[str, str] | None,
    value: str | None,
    dnp: bool | None,
    exclude_from_bom: bool | None,
) -> None:
    if not isinstance(references, list) or not references:
        raise BOMError("references must be a nonempty list of exact footprint references.")
    for reference in references:
        _valid_reference(reference)
    if len(set(references)) != len(references):
        raise BOMError("references contains duplicates. Request each footprint only once.")
    if fields is not None:
        if not isinstance(fields, dict):
            raise BOMError("fields must map custom field names to string values.")
        for name, text in fields.items():
            _field_name(name)
            if not isinstance(text, str) or "\0" in text:
                raise BOMError(
                    "Custom field values must be strings without null characters; use '' to clear a field."
                )
    if value is not None and (not isinstance(value, str) or "\0" in value):
        raise BOMError("value must be a string without null characters; use '' to clear it.")
    for name, flag in (("dnp", dnp), ("exclude_from_bom", exclude_from_bom)):
        if flag is not None and not isinstance(flag, bool):
            raise BOMError(f"{name} must be a boolean or null.")
    if not fields and value is None and dnp is None and exclude_from_bom is None:
        raise BOMError("Provide at least one custom field, value, dnp or exclude_from_bom change.")


def prepare_updates(
    footprints: Sequence[FootprintInstance],
    references: list[str],
    fields: dict[str, str] | None = None,
    value: str | None = None,
    dnp: bool | None = None,
    exclude_from_bom: bool | None = None,
) -> list[FootprintInstance]:
    validate_update(references, fields, value, dnp, exclude_from_bom)
    targets: list[FootprintInstance] = []
    for reference in sorted(references, key=natural_key):
        matches = [item for item in footprints if item.reference_field.text.value == reference]
        if len(matches) != 1:
            raise BOMError(
                f"Expected exactly one footprint with reference {reference!r}; found {len(matches)}. "
                "Check list_footprints and fix missing or duplicate references in KiCad."
            )
        if matches[0].locked:
            raise BOMError(
                f"Footprint {reference!r} is locked. Unlock it in KiCad before changing BOM data."
            )
        custom_fields(matches[0])  # Reject ambiguous field names before preparing any edits.
        targets.append(matches[0])

    updated: list[FootprintInstance] = []
    for original in targets:
        # FootprintInstance copies the complete protobuf, retaining child geometry,
        # mandatory-field presentation, library metadata and unrelated attributes.
        item = FootprintInstance(original.proto)
        existing = {
            child.name: child for child in item.definition.items if isinstance(child, Field)
        }
        for name, text in (fields or {}).items():
            field = existing.get(name)
            if field is None:
                field = Field()
                # IDs 0..3 are KiCad's mandatory Reference/Value/Datasheet/Description.
                field.proto.id.id = max([3, *(entry.field_id for entry in existing.values())]) + 1
                field.name = name
                field.text = BoardText(item.value_field.text.proto)
                field.text.proto.id.value = str(uuid4())
                field.text.position = item.position
                field.visible = False
                item.definition.add_item(field)
                existing[name] = field
            field.text.value = text
        if value is not None:
            item.value_field.text.value = value
        if dnp is not None:
            item.attributes.do_not_populate = dnp
        if exclude_from_bom is not None:
            item.attributes.exclude_from_bill_of_materials = exclude_from_bom
        if item.proto != original.proto:
            updated.append(item)
    return updated


def verify_updates(
    expected: Sequence[FootprintInstance], returned: Sequence[FootprintInstance]
) -> None:
    expected_ids = {item.id.value for item in expected}
    if (
        len(returned) != len(expected)
        or any(not isinstance(item, FootprintInstance) for item in returned)
        or {item.id.value for item in returned} != expected_ids
        or len(expected_ids) != len(expected)
    ):
        raise BOMError("KiCad did not confirm every footprint update; the BOM edit was cancelled.")
    actual = {item.id.value: component(item) for item in returned}
    if any(actual[item.id.value] != component(item) for item in expected):
        raise BOMError("KiCad did not apply all requested BOM values; the BOM edit was cancelled.")
