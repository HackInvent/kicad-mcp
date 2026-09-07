# Changelog

## 0.2.0

This alpha release adds a bill of materials workflow for the open PCB. Validation in a real KiCad GUI is still pending.

- Add `get_bom` to read grouped or individual-component BOM rows, with per-board quantities, natural reference ordering and selectable custom fields.
- Add `export_bom` to return CSV text through MCP without writing files. Export supports custom delimiters, CSV quoting, Unicode and spreadsheet formula protection.
- Exclude DNP and “Exclude from BOM” components by default, with separate options to include them. Group components by value, complete footprint identifier and custom metadata; differing MPNs remain separate even when those fields are hidden from the output.
- Add `update_bom_fields` to add or edit custom fields, change component values and set DNP/BOM-exclusion flags in one undoable transaction. Updates do not autosave and are blocked in read-only mode.
- Expand the server to 13 tools: eight read tools and five write tools. Document BOM inspection, editing, export and explicit saving.

BOM data and updates apply to PCB footprints. This release does not provide schematic editing, schematic back-annotation or assembly-variant resolution. Updating the PCB from its schematic may overwrite PCB metadata.

## 0.1.0

Initial alpha release of the KiCad 10+ IPC plugin and MCP server.

- Add Start MCP server and Stop MCP server actions for KiCad’s PCB editor.
- Support Streamable HTTP on localhost with bearer authentication and client-managed stdio sessions.
- Provide 10 tools for connection status, board information, footprints, nets, tracks, selection, footprint movement, track/text creation and explicit board saving.
- Include a read-only mode, serialized IPC access, undoable edits and per-instance server lifecycle management.
- Provide a local installer, a KiCad Plugin and Content Manager archive, Python distributions and CI on Python 3.10, 3.12 and 3.13.
- Test MCP clients and KiCad operations with simulated editors and real `kicad-python` objects; manual validation in a real KiCad GUI remains pending.
