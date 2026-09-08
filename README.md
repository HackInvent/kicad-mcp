# KiCad MCP — HackInvent

[![CI](https://github.com/HackInvent/kicad-mcp/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/HackInvent/kicad-mcp/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/HackInvent/kicad-mcp?include_prereleases&sort=semver)](https://github.com/HackInvent/kicad-mcp/releases)
[![Status: alpha](https://img.shields.io/badge/status-alpha-orange)](#verification)
[![KiCad 10+](https://img.shields.io/badge/KiCad-10%2B-314CB0?logo=kicad&logoColor=white)](#install-in-kicad)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](#install-in-kicad)
[![License: MIT](https://img.shields.io/github/license/HackInvent/kicad-mcp)](LICENSE)
[![Downloads](https://img.shields.io/github/downloads/HackInvent/kicad-mcp/total)](https://github.com/HackInvent/kicad-mcp/releases)

A **KiCad 10+** plugin that starts a [Model Context Protocol](https://modelcontextprotocol.io/) server, letting MCP-compatible assistants work with the open PCB.

The plugin uses KiCad’s official IPC API and the `kicad-python` library, without `pcbnew`/SWIG bindings. It provides two actions: **Start MCP server** and **Stop MCP server**. A **stdio** mode also lets an MCP client launch the server directly.

**Version 0.3.1, alpha.** The MCP protocol, server lifecycle and operations are covered by automated tests. The KiCad tests use real `kicad-python` objects with a simulated editor; validation in a real KiCad interface is still pending. This project targets the PCB editor and requires a running GUI instance.

## Available tools

| MCP tool | Function |
|---|---|
| `kicad_status` | Check the connection and KiCad version |
| `get_board_info` | Read the board name, layers and item counts |
| `list_footprints` | Read references, values, positions, rotations and lock status; optional exact reference filter |
| `list_nets` | List electrical nets |
| `list_tracks` | Read straight and curved tracks, their dimensions and nets |
| `get_selection` | Read the items selected in the editor |
| `list_pads` | Inspect pad numbers, parent references, positions, nets and geometry; filter by reference or net |
| `list_vias` | Inspect via dimensions, layer spans and nets |
| `list_zones` | Inspect zone names, nets, layers, priorities and fill state |
| `get_item_details` | Retrieve type-specific details for exact item UUIDs |
| `get_board_layers` | Inspect enabled layers, display names, visibility and the active layer |
| `get_board_stackup` | Read ordered copper/dielectric layers, thicknesses, materials and finish settings |
| `get_net_connections` | List the pads, tracks and vias assigned to one net |
| `get_bom` | Read a grouped or individual-component BOM from the open PCB |
| `export_bom` | Return the PCB BOM as CSV text to the MCP client |
| `move_footprint` | Move a component by its reference and optionally rotate it |
| `add_track` | Add a straight track on an enabled copper layer |
| `add_text` | Add text to the board |
| `update_bom_fields` | Update component values, custom BOM fields and assembly flags |
| `set_selection` | Replace, add to or remove from the editor selection using item UUIDs |
| `set_active_layer` | Activate an enabled layer by its canonical name |
| `set_visible_layers` | Set which enabled layers are visible |
| `add_via` | Add a through-hole via spanning F.Cu to B.Cu |
| `delete_items` | Delete supported, unlocked top-level items by UUID in one undo step |
| `refill_zones` | Request an asynchronous zone refill in the editor |
| `run_drc` | Check the last saved PCB with kicad-cli and create a local JSON report |
| `export_fabrication` | Export Gerbers, drill files, component positions or SVG from the last saved PCB |
| `save_board` | Explicitly save the PCB to its current file |

There are **28 tools: 15 read tools and 13 tools that change the editor, save the PCB or create artifacts**. BOM export reads the PCB and returns text; it does not write a file.

Distances are in **millimetres**, positions are absolute and angles are in **degrees**. MCP numeric arguments must be JSON numbers and flags must be JSON booleans; implicit conversion between these types is rejected. Object creation, movement, deletion and BOM updates create an undo step in KiCad. Selection and layer visibility change the editor view. Zone refill is a separate asynchronous KiCad action. These operations never trigger an automatic save. `save_board` also saves any other unsaved changes currently in the editor.

`add_track` creates a segment and `add_via` creates a through-hole via; neither performs autorouting or design-rule checks. Locked components and ambiguous references are rejected. Footprints containing items that `kicad-python` cannot safely transform are also rejected; pads, standard geometry and 3D models are supported.

## Inspect and edit a PCB

Use `get_board_layers` to discover canonical layer names such as `F.Cu`, `B.Cu` and `Edge.Cuts`. `get_board_stackup` reports board thickness and material information from Board Setup.

Call `list_pads` with `{"reference": "U1"}` to inspect a component, or `get_net_connections` with `{"net_name": "GND"}` to inspect all pads, tracks and vias assigned to a net. These lists describe **net membership**. They do not prove that the copper is physically connected or that the design passes DRC; the net response explicitly reports `connectivity_verified: false`.

Use UUIDs returned by inspection tools to request `get_item_details` or control selection. Item requests accept up to 500 unique UUIDs at a time:

```json
{
  "item_ids": ["REPLACE_WITH_AN_ITEM_UUID"],
  "mode": "replace"
}
```

`set_selection` also accepts `"add"` and `"remove"`; `{"item_ids": [], "mode": "replace"}` clears the selection. Layer tools accept canonical names: for example, call `set_visible_layers` with `{"layers": ["F.Cu", "Edge.Cuts"]}`.

To create a through-hole via assigned to an existing net, call `add_via`:

```json
{
  "x_mm": 25.0,
  "y_mm": 30.0,
  "diameter_mm": 0.6,
  "drill_mm": 0.3,
  "net_name": "GND"
}
```

The drill must be smaller than the copper diameter. This tool supports through-hole vias only. Inspect the result and run DRC before using the board for manufacturing.

`delete_items` validates every requested UUID before deleting anything. It refuses locked items, footprint children such as pads and fields, and grouped items whose effective lock cannot be established through the API. Delete the parent footprint when appropriate, or ungroup items in KiCad first. A failed edit rolls back its transaction; an unconfirmed rollback is reported explicitly.

`refill_zones` schedules KiCad’s refill action and returns `completed: false`. Wait for the editor to finish before dependent edits or saving. The MCP response confirms that the action was requested, not that the fill finished.

## Check DRC and export manufacturing files

These tools require **kicad-cli from the same major KiCad version as the running editor**. Put it on `PATH` or set `KICAD_MCP_CLI` to the executable’s full path before starting the server. They create local files and are unavailable in read-only mode.

Both tools use a private copy of the **last saved PCB**, along with saved project settings and custom design rules when available. Their responses identify this as `snapshot: "last_saved_board"` and `includes_unsaved_changes: false`. They never implicitly save the open PCB or project. Call `save_board` first to include pending PCB edits, and save changed project settings in KiCad before checking them. A never-saved PCB must first be saved in KiCad.

This choice avoids an effect of KiCad 10’s IPC Save Copy command, which can persist settings in the original project even when creating a copy.

Call `run_drc` with `{"severity": "all"}`. You can filter to `"error"` or `"warning"`. A saved `.kicad_pro` is required so the check uses the project’s saved rules. The response includes a summary, up to 1,000 findings and the path to the complete JSON report; violations are returned as findings rather than a server failure. The check does **not** compare the PCB with its schematic. Filtered or excluded findings must not be interpreted as a complete clean-design result.

Call `export_fabrication` with these arguments for common manufacturing outputs:

```json
{
  "formats": ["gerbers", "drill", "positions"]
}
```

These are also the defaults when `formats` is omitted. Gerbers, drill and position files share KiCad’s drill/place origin. Drill and position coordinates use millimetres; position export includes both sides and excludes DNP components. Gerber and SVG exports check zone fills and refill the private copy when necessary. `"svg"` exports a board drawing containing F.Cu, F.SilkS and Edge.Cuts. STEP export is not provided because a copied project can change how project-relative 3D model paths resolve. Export does not constitute a passing DRC check or manufacturing approval.

Results list generated files with paths and sizes on the **machine running the MCP server**. Outputs go into unique job directories under `artifacts` in the server’s state directory, or under `KICAD_MCP_ARTIFACT_DIR` when configured. Existing jobs are not overwritten. Temporary source copies are removed; successful reports and exports remain until you delete them. A remote MCP client may need a separate way to retrieve these local files.

## Work with a bill of materials

The BOM is derived from the **open PCB**, including its current unsaved changes. It uses footprint values, library identifiers, custom fields and assembly flags available through KiCad’s PCB API. It does not read the schematic or resolve assembly variants.

`get_bom` and `export_bom` share these options:

| Argument | Default | Behavior |
|---|---|---|
| `grouped` | `true` | Combine matching components into rows with references and a quantity per board; use `false` for one component per row |
| `include_dnp` | `false` | Include components marked “Do not populate” when enabled |
| `include_excluded` | `false` | Include components marked “Exclude from BOM” when enabled |
| `fields` | `null` | Include all custom fields; provide a list to select particular custom fields |

Grouping compares values, complete library footprint identifiers and custom metadata, including manufacturer part numbers. Selecting fewer output fields does not merge components with different MPNs or other hidden metadata. References use natural ordering, such as `R2` before `R10`. Quantities describe one copy of the board. Included components must have unique, annotated references; ambiguous or unannotated references produce an actionable error. Requested custom fields that are absent from a component are returned as empty strings.

A typical workflow is to inspect the BOM, update selected components, export it, and explicitly save the board if you want to keep the edits.

1. Call `get_bom` with these MCP arguments to inspect purchasing fields:

```json
{
  "grouped": true,
  "include_dnp": false,
  "include_excluded": false,
  "fields": ["Manufacturer", "MPN"]
}
```

2. Call `update_bom_fields` to set the value, custom fields and assembly flags for selected references:

```json
{
  "references": ["R1", "R2"],
  "value": "10k",
  "fields": {
    "Manufacturer": "Example Components",
    "MPN": "EXAMPLE-10K-0603"
  },
  "dnp": false,
  "exclude_from_bom": false
}
```

Provide `references` and at least one change; omit options you do not want to change. Use `value` for the built-in component value and `fields` to add or edit custom metadata such as Manufacturer or MPN. The update uses a single undoable transaction and does not save the board. It is unavailable in read-only mode. Use an empty string to clear a custom field’s value. Reapplying identical values makes no changes and returns `updated_count: 0`.

3. Call `export_bom` with these arguments to retrieve CSV text:

```json
{
  "grouped": true,
  "fields": ["Manufacturer", "MPN"],
  "delimiter": ","
}
```

The MCP response contains `csv`, a suggested `filename`, `mime_type`, `row_count` and `component_count` for your client to display or save. Choose comma (`","`), semicolon (`";"`) or tab (`"\t"`) as the delimiter. The server does not create a CSV file on disk. CSV quoting handles commas, newlines and Unicode; spreadsheet formula prefixes are escaped to prevent them from being evaluated as formulas. Custom column names that would collide with another CSV header are prefixed with `Field:`; the original names remain unchanged in `get_bom`.

4. Inspect the board and call `save_board` with `{}` only when you want to save the result. This also saves any other pending PCB changes. You can undo the metadata update in KiCad before saving.

BOM updates modify **PCB footprint data only**. They do not edit or back-annotate the schematic. A later **Update PCB from Schematic** may overwrite these fields, so maintain the corresponding schematic metadata separately when it is the source of truth.

## Install in KiCad

Requirements: KiCad 10.0 or later, Python 3.10+ with `venv`/`pip` support, and network access when dependencies are first installed. On Debian/Ubuntu, you may need the `python3-venv` package.

### Using the Plugin and Content Manager

1. Download `hackinvent-kicad-mcp-0.3.1.zip` from the [releases page](https://github.com/HackInvent/kicad-mcp/releases).
2. In the KiCad project manager, open **Plugin and Content Manager**, choose **Install from File** and select the ZIP.
3. Enable the KiCad API in the plugin preferences, then open a PCB and reload the plugins or restart the editor.
4. Wait for the plugin’s Python environment to be created, then run **Start MCP server**.

The package is not yet listed in KiCad’s official plugin catalogue. Install it from the ZIP file.

### From the repository

```bash
git clone https://github.com/HackInvent/kicad-mcp.git
cd kicad-mcp
python3 scripts/install_plugin.py --version 10.0
```

The installer copies the plugin into KiCad’s user directory. It does not modify KiCad’s program files. Typical paths are:

- Linux: `~/.local/share/KiCad/10.0/plugins/org.hackinvent.kicad-mcp`
- macOS: `~/Documents/KiCad/10.0/plugins/org.hackinvent.kicad-mcp`
- Windows: `%USERPROFILE%\Documents\KiCad\10.0\plugins\org.hackinvent.kicad-mcp`

Use `--destination /exact/path/to/plugin` to choose another location, for example when your Documents folder is redirected. To update this installation, add `--overwrite`; the installer first checks the existing plugin’s identifier. Avoid installing both the ZIP and a manual copy of the same plugin.

On Windows, use `py` or `python` instead of `python3`, depending on your installation.

## Connect an MCP client over HTTP

After running **Start MCP server**, the server listens at this address by default:

```text
http://127.0.0.1:8765/mcp
```

A server-specific token protects every route. From the repository, retrieve the connection settings for running servers:

```bash
python3 scripts/connection_info.py
```

This command uses only the Python standard library. It displays the `url` and `token` fields to provide to your client:

```text
Transport : Streamable HTTP
URL       : http://127.0.0.1:8765/mcp
Header    : Authorization: Bearer <value of the token field>
```

The configuration format depends on your client. It must support setting this HTTP header. Authentication uses a shared local token, without an OAuth server; clients that require OAuth cannot use this mode directly. Browser clients that send an `Origin` header are rejected; use a native MCP client or stdio mode.

The token changes on each startup unless `KICAD_MCP_TOKEN` is set. A fixed token must contain visible ASCII characters without whitespace; invalid values are rejected before the server starts. It is separate from KiCad’s internal IPC token. Do not include either token in issues or commits.

Without a copy of the repository, you can also find the connection settings in a session JSON file:

| System | Session directory |
|---|---|
| Linux | `$XDG_STATE_HOME/kicad-mcp`, or `~/.local/state/kicad-mcp` if unset |
| macOS | `~/Library/Application Support/kicad-mcp` |
| Windows | `%LOCALAPPDATA%\kicad-mcp` |

These files contain a secret; POSIX permissions restrict access to their owner. **Stop MCP server** stops the server associated with the KiCad instance that launches the action. Session locks prevent concurrent startups for the same instance.

## Stdio mode and development

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/kicad-mcp serve --transport stdio
```

On Windows, the environment’s executables are in `.venv\Scripts\`.

For a client that accepts the `mcpServers` format, adapt the paths in this example:

```json
{
  "mcpServers": {
    "kicad": {
      "command": "/ABSOLUTE/PATH/kicad-mcp/.venv/bin/python",
      "args": ["-m", "kicad_mcp", "serve", "--transport", "stdio"]
    }
  }
}
```

In this mode, the MCP client manages the server’s lifecycle; you do not need to click **Start MCP server**. The KiCad editor must remain open with its API enabled. With a single instance, `kicad-python` looks for the default KiCad socket. Use `--socket` or `KICAD_API_SOCKET` to target another instance. After restarting KiCad, restart the server to connect to the correct session.

Additional commands after installing the Python package:

```bash
kicad-mcp serve --transport streamable-http --port 8765
kicad-mcp serve --transport stdio --read-only
kicad-mcp status
kicad-mcp status --show-token
kicad-mcp stop
```

`--read-only` exposes only the 15 inspection/BOM read tools and also blocks the other operations in the KiCad adapter. Selection, layer changes, DRC reports and fabrication exports are disabled because they change editor state or create files. `export_bom` remains available because it returns CSV text without creating a file. An already running server cannot change its read-only setting through a second start command: stop it first, then restart with the intended setting. `status` hides the token by default. When multiple servers are running, use `stop --socket /path/to/socket` to select the instance to stop.

### Configure startup from KiCad

Set these variables **before starting KiCad**:

| Variable | Effect |
|---|---|
| `KICAD_MCP_PORT` | HTTP port, `8765` by default; `0` selects an available port |
| `KICAD_MCP_TOKEN` | Optional fixed HTTP token to keep the client configuration unchanged |
| `KICAD_MCP_READ_ONLY=1` | Enable read-only mode |
| `KICAD_MCP_STATE_DIR` | Override the session directory |
| `KICAD_MCP_CLI` | Full path to kicad-cli for DRC and fabrication tools; otherwise search PATH |
| `KICAD_MCP_ARTIFACT_DIR` | Override the directory that stores DRC reports and manufacturing exports |

KiCad automatically passes `KICAD_API_SOCKET` and `KICAD_API_TOKEN` to plugin actions. For multiple simultaneous instances, use different ports or `KICAD_MCP_PORT=0`, then retrieve their URLs with `connection_info.py`.

## Verification

```bash
.venv/bin/ruff check .
.venv/bin/python -m pytest -q
.venv/bin/python -m build
python3 scripts/build_plugin.py
```

CI runs these checks on Python 3.10, 3.12 and 3.13. Tests cover real HTTP/stdio MCP clients, errors, access restrictions, repeated startups, shutdown, KiCad units, transaction rollback, BOM grouping and CSV export, metadata updates, pad/via/zone inspection, stackup, selection and deletion, DRC/export command handling, artifact cleanup, and PCM archives. The tests use simulated editor and CLI boundaries; they do not require a KiCad GUI or kicad-cli.

For a manual check on a **copy of a test PCB**:

1. Install the plugin and start the server from KiCad.
2. Connect a client and call `kicad_status`, `get_board_info` and `list_footprints`.
3. Move an unlocked component and check its coordinates, then undo the change in KiCad.
4. Add text and a track on enabled layers; inspect the result and run DRC in the editor.
5. Read a BOM, update Manufacturer/MPN on test components, export the CSV and verify grouping, quantities and DNP filtering. Undo the metadata update in KiCad.
6. Inspect pads, zones, stackup and a net; select an item by UUID and check layer visibility.
7. Create and delete a test via, verify undo behavior, then request zone refill and wait for KiCad to finish.
8. Call `save_board` only if the result is as expected. Run `run_drc`, compare the report with the editor’s DRC, and check Gerbers, drill files and positions in appropriate viewers before manufacturing.
9. Stop the server using the KiCad action and confirm that the client disconnects.

If the plugin does not appear, check the Python environment and KiCad’s messages. If the server cannot connect, check that the API is enabled, a PCB is open and the correct socket is selected. If the port is occupied, choose another `KICAD_MCP_PORT`.

## Architecture and references

```mermaid
flowchart LR
    A[MCP client] -->|Local Streamable HTTP or stdio| B[Python MCP server]
    B --> C[KiCad adapter with serialized access]
    C -->|IPC Protobuf / NNG| D[KiCad PCB editor]
    P[KiCad plugin action] -->|Start / stop| B
    C -->|Copy saved PCB and project context| S[Private job directory]
    S --> K[kicad-cli DRC and exports]
    K --> F[Local reports and manufacturing files]
```

- `src/kicad_mcp/server.py`: MCP tools and HTTP authentication.
- `src/kicad_mcp/bridge.py`: IPC access, conversions and edit transactions.
- `src/kicad_mcp/inspection.py`: pads, vias, zones, item details, nets and stackup.
- `src/kicad_mcp/editing.py`: selection, layer controls, vias, deletion and zone refill.
- `src/kicad_mcp/fabrication.py`: saved-board snapshots, kicad-cli and artifact management.
- `src/kicad_mcp/bom.py`: BOM grouping, CSV generation and metadata validation.
- `src/kicad_mcp/__main__.py`: transports, sessions and control commands.
- `plugin/`: IPC manifest, dependencies and KiCad actions.
- `scripts/`: local installation, connection settings and PCM packaging.

Official references: [KiCad IPC API](https://dev-docs.kicad.org/en/apis-and-binding/ipc-api/for-addon-developers/), [kicad-python library](https://gitlab.com/kicad/code/kicad-python), [KiCad 10 CLI](https://docs.kicad.org/10.0/en/cli/cli.html), [KiCad package format](https://dev-docs.kicad.org/en/addons/), [MCP Python SDK, 1.x branch](https://github.com/modelcontextprotocol/python-sdk/tree/v1.x).

See [CHANGELOG.md](CHANGELOG.md) for release history.

[MIT](LICENSE) license.
