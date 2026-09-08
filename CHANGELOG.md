# Changelog

## 0.3.0

This alpha release expands the server to 28 tools for PCB inspection, editing and manufacturing preparation. Validation in a real KiCad GUI and against an installed kicad-cli remains pending.

- Add pad, via and zone inspection, exact UUID item details, layer visibility and stackup information, and per-net pad/track/via membership. Net membership explicitly does not verify physical connectivity.
- Add selection and active/visible layer controls, through-hole via creation and transactional deletion of supported unlocked top-level items. Reject footprint children and grouped items when safe deletion cannot be established.
- Add asynchronous zone refill with an explicit pending-completion result matching KiCad 10’s API behavior.
- Add kicad-cli DRC reports and Gerber, drill, component-position and SVG exports. Commands use private copies of the last saved PCB and saved project context, without implicitly persisting changes in the original project. Reports identify that unsaved edits are excluded; DRC does not check schematic parity.
- Check CLI/editor major-version compatibility, preserve reports in unique artifact directories and clean temporary source copies after each job. Configure the executable and artifact location using KICAD_MCP_CLI and KICAD_MCP_ARTIFACT_DIR.
- Reject unconfirmed track/text creation when KiCad returns an object without an assigned UUID, and return actionable errors for stale item IDs.
- Keep 15 read tools available in read-only mode; disable editor changes and file-producing operations at both MCP and adapter boundaries.
- Extend automated coverage of the real MCP protocol, KiCad object serialization and transactions, and simulated CLI exports and failures. Update the English README with end-to-end examples and limitations.

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
