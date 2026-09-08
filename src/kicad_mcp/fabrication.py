"""KiCad CLI reports and exports from isolated copies of the last saved project.

KiCad 10's SaveCopyOfDocument also saves the original project settings. Do not
use Board.save_as here: copying files is what makes these tools non-saving.
CLI options are documented at https://docs.kicad.org/10.0/en/cli/cli.html.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Iterator

from .errors import BridgeError


CLI_TIMEOUT_SECONDS = 120
MAX_REPORT_BYTES = 20 * 1024 * 1024
MAX_RETURNED_VIOLATIONS = 1000
FORMATS = ("gerbers", "drill", "positions", "svg")
SVG_LAYERS = "F.Cu,F.SilkS,Edge.Cuts"
SNAPSHOT_NOTE = (
    "Uses the last saved PCB and project settings; unsaved changes are excluded. "
    "Call save_board and save project settings in KiCad before running again to include edits."
)


def artifact_directory() -> Path:
    """Use the server's state convention without importing its CLI entry point."""
    if override := os.environ.get("KICAD_MCP_ARTIFACT_DIR"):
        return Path(override).expanduser().absolute()
    if override := os.environ.get("KICAD_MCP_STATE_DIR"):
        return Path(override).expanduser().absolute() / "artifacts"
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Application Support"
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
    return base.expanduser().absolute() / "kicad-mcp" / "artifacts"


@contextmanager
def _artifact_run(prefix: str) -> Iterator[Path]:
    root = artifact_directory()
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        # mkdtemp supplies a unique directory with owner-only POSIX permissions,
        # including when the configured parent already has broader permissions.
        directory = Path(tempfile.mkdtemp(prefix=f"{prefix}-", dir=root))
    except OSError:
        raise BridgeError(
            "Cannot create the artifact directory. Check KICAD_MCP_ARTIFACT_DIR, "
            "free disk space, and directory permissions."
        ) from None
    try:
        yield directory
    except BaseException as error:
        try:
            shutil.rmtree(directory)
        except FileNotFoundError:
            pass
        except OSError:
            raise BridgeError(
                "Artifact generation failed and its temporary files could not be removed. "
                "Check permissions in the artifact directory."
            ) from None
        if isinstance(error, OSError):
            raise BridgeError(
                "Cannot read or write artifact files. Check free disk space and "
                "permissions in the artifact directory."
            ) from None
        raise


def _write_private(path: Path, data: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(data)
    path.chmod(0o600)


def _expand_saved_variables(value: str, variables: dict[str, str]) -> str:
    pattern = re.compile(r"\$\{([^}]+)\}|\$\(([^)]+)\)")
    for _ in range(20):
        expanded = pattern.sub(lambda m: variables.get(m[1] or m[2], m[0]), value)
        if expanded == value:
            if any((match[1] or match[2]) in variables for match in pattern.finditer(value)):
                raise BridgeError("The saved project's text variables contain a recursive definition.")
            return value
        value = expanded
    raise BridgeError("The saved project's text variables contain a recursive definition.")


def _rebase_project_path(value: str, project_dir: Path, variables: dict[str, str]) -> str:
    value = _expand_saved_variables(value, variables)
    path = type(project_dir)(value)
    if path.drive and not path.root:
        raise BridgeError("A project path is relative to a Windows drive. Use an absolute path in KiCad settings.")
    # A variable at the start may expand to an absolute global KiCad library
    # path. A fixed relative prefix (libs/${VENDOR}/...) remains relative even
    # when a later component is unknown, so it must use the original project.
    variable_prefix = re.match(r"^\$(?:\{|\(|[A-Za-z_])", value)
    if value and not variable_prefix and not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", value):
        if not path.is_absolute():
            value = str(project_dir / path)
    return value


def _rebase_library_table(data: bytes, project_dir: Path, variables: dict[str, str]) -> bytes:
    """Keep project libraries at their original locations when moving a snapshot."""
    text = data.decode("utf-8-sig")
    pattern = re.compile(r'(\(uri\s+)("(?:\\.|[^"\\])*")(\s*\))')

    def replace(match: re.Match) -> str:
        uri = json.loads(match[2])
        # Unknown variables may be supplied by KiCad's global path configuration.
        # Remote library URIs and those expressions should remain for KiCad to resolve.
        uri = _rebase_project_path(uri, project_dir, variables)
        return match[1] + json.dumps(uri, ensure_ascii=False) + match[3]

    return pattern.sub(replace, text).encode("utf-8")


@contextmanager
def _snapshot(board: Any, directory: Path, *, require_project: bool) -> Iterator[dict[str, Any]]:
    project = board.get_project()
    project_dir = Path(project.path).expanduser()
    if not project.path or not project_dir.is_absolute():
        raise BridgeError("Save the PCB in a KiCad project before generating reports or exports.")
    source = Path(board.name)
    if not source.is_absolute():
        source = project_dir / source
    if source.suffix != ".kicad_pcb" or not source.is_file():
        raise BridgeError("The PCB has no saved source file. Save it in KiCad first and retry.")
    project_name = project.name if project.name else source.stem
    if Path(project.name).name != project.name:
        raise BridgeError("The active project's location cannot be determined reliably.")
    project_file = project_dir / f"{project_name}.kicad_pro"
    rules_file = project_dir / f"{project_name}.kicad_dru"
    library_table = project_dir / "fp-lib-table"
    if require_project and not project_file.is_file():
        raise BridgeError("Save the project's .kicad_pro settings in KiCad before running DRC.")

    inputs = {source: source.name}
    for path, name in (
        (project_file, source.with_suffix(".kicad_pro").name),
        (rules_file, source.with_suffix(".kicad_dru").name),
    ):
        if path.exists():
            if not path.is_file():
                raise BridgeError("A saved project context path is not a regular file.")
            inputs[path] = name
    if require_project and library_table.exists():
        if not library_table.is_file():
            raise BridgeError("The project's fp-lib-table is not a regular file.")
        inputs[library_table] = "fp-lib-table"
    try:
        stamps = {path: path.stat() for path in inputs}
        contents = {path: path.read_bytes() for path in inputs}
        for path, before in stamps.items():
            after = path.stat()
            if (before.st_mtime_ns, before.st_size, before.st_ino) != (
                after.st_mtime_ns, after.st_size, after.st_ino
            ):
                raise BridgeError("A project file changed while being copied. Wait for saving to finish and retry.")
    except OSError:
        raise BridgeError(
            "Cannot read the saved PCB or project files. Check their availability and read permissions."
        ) from None

    variables: dict[str, str] = {}
    if project_file in contents:
        try:
            settings = json.loads(contents[project_file])
            configured_variables = settings.get("text_variables", {})
            if not isinstance(configured_variables, dict) or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in configured_variables.items()
            ):
                raise ValueError("Invalid project variables")
            variables.update(configured_variables)
        except (ValueError, AttributeError):
            raise BridgeError("The saved .kicad_pro settings are not valid KiCad project JSON.") from None
    variables["KIPRJMOD"] = str(project_dir)
    # KIPRJMOD is internally fixed to the CLI snapshot directory. Preserve the
    # original project location in saved text/path expressions, without copying
    # arbitrary project assets into the output bundle.
    encoded_origin = json.dumps(str(project_dir), ensure_ascii=False)[1:-1].encode("utf-8")
    for path, data in contents.items():
        contents[path] = data.replace(b"${KIPRJMOD}", encoded_origin).replace(b"$(KIPRJMOD)", encoded_origin)
    if project_file in contents:
        settings = json.loads(contents[project_file])
        pcb_settings = settings.get("pcbnew", {})
        if not isinstance(pcb_settings, dict):
            raise BridgeError("The saved project's PCB settings are invalid.")
        sheet = pcb_settings.get("page_layout_descr_file", "")
        if not isinstance(sheet, str):
            raise BridgeError("The saved project's drawing-sheet path is invalid.")
        if sheet:
            pcb_settings["page_layout_descr_file"] = _rebase_project_path(sheet, project_dir, variables)
            contents[project_file] = (json.dumps(settings, ensure_ascii=False) + "\n").encode("utf-8")
    if library_table in contents:
        contents[library_table] = _rebase_library_table(contents[library_table], project_dir, variables)

    with tempfile.TemporaryDirectory(prefix=".snapshot-", dir=directory) as temporary:
        stage = Path(temporary)
        for path, name in inputs.items():
            _write_private(stage / name, contents[path])
        yield {
            "path": stage / source.name,
            "name": source.name,
            "context": {
                "project_settings": "last_saved" if project_file in contents else "KiCad_defaults",
                "custom_rules": rules_file in contents,
                "project_footprint_libraries": library_table in contents,
            },
        }


def _run_cli(executable: str, arguments: list[str], cwd: Path, *, label: str,
             allowed_codes: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    for key in ("KICAD_API_SOCKET", "KICAD_API_TOKEN", "KICAD_MCP_TOKEN"):
        env.pop(key, None)
    try:
        result = subprocess.run(
            [executable, *arguments], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace", timeout=CLI_TIMEOUT_SECONDS, check=False,
        )
    except subprocess.TimeoutExpired:
        raise BridgeError(f"KiCad CLI {label} timed out after {CLI_TIMEOUT_SECONDS} seconds.") from None
    except OSError:
        raise BridgeError("Cannot launch kicad-cli. Check KICAD_MCP_CLI and the KiCad installation.") from None
    if result.returncode not in allowed_codes:
        # CLI diagnostics can contain project content and private environment paths.
        # Keep this error predictable; never include captured stdout/stderr.
        raise BridgeError(
            f"KiCad CLI {label} failed (exit code {result.returncode}). "
            "Check that the saved board and project load correctly in KiCad."
        )
    return result


def _find_cli(major: int, cwd: Path) -> tuple[str, str]:
    requested = os.environ.get("KICAD_MCP_CLI", "kicad-cli")
    executable = shutil.which(requested)
    if not executable and requested == "kicad-cli" and sys.platform == "darwin":
        executable = shutil.which("/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli")
    if not executable:
        raise BridgeError("kicad-cli was not found. Install KiCad and set KICAD_MCP_CLI to its executable if needed.")
    executable = str(Path(executable).absolute())
    result = _run_cli(executable, ["version"], cwd, label="version check")
    match = re.fullmatch(r"\s*(\d+)\.(\d+)(?:\.(\d+))?[^\r\n]*\s*", result.stdout)
    if not match or int(match[1]) < 10:
        raise BridgeError("Could not verify a KiCad 10+ CLI version. Check KICAD_MCP_CLI.")
    if int(match[1]) != major:
        raise BridgeError(f"kicad-cli must use the same major version as the open KiCad {major} editor.")
    return executable, ".".join(part for part in match.group(1, 2, 3) if part is not None)


def _artifact(path: Path, directory: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(directory.resolve()):
        raise BridgeError("KiCad CLI produced an invalid artifact path.")
    size = path.stat().st_size
    if size == 0:
        raise BridgeError("KiCad CLI produced an empty artifact file.")
    path.chmod(0o600)
    return {"name": path.relative_to(directory).as_posix(), "path": str(path), "size_bytes": size}


def _read_drc_report(path: Path) -> tuple[dict[str, int], list[dict[str, Any]]]:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_REPORT_BYTES:
        raise BridgeError("KiCad CLI did not produce a readable DRC JSON report within the size limit.")
    try:
        report = json.loads(path.read_text(encoding="utf-8-sig"))
        violations = []
        for category in ("violations", "unconnected_items", "schematic_parity"):
            items = report[category]
            if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
                raise ValueError("Invalid violation list")
            for item in items:
                if not isinstance(item.get("severity"), str) or not item["severity"]:
                    raise ValueError("Invalid violation severity")
                if not isinstance(item.get("excluded", False), bool):
                    raise ValueError("Invalid violation exclusion")
            violations.extend({**item, "category": category} for item in items)
    except (ValueError, KeyError, TypeError):
        raise BridgeError("KiCad CLI produced an invalid DRC JSON report.") from None
    summary = {"errors": 0, "warnings": 0, "excluded": 0, "other": 0, "total": len(violations)}
    for violation in violations:
        if violation.get("excluded", False):
            summary["excluded"] += 1
        elif violation.get("severity") == "error":
            summary["errors"] += 1
        elif violation.get("severity") == "warning":
            summary["warnings"] += 1
        else:
            summary["other"] += 1
    return summary, violations


class FabricationMixin:
    """Artifact-producing tools; require a bridge's operation, board and client helpers."""

    def run_drc(self, severity: str = "all") -> dict[str, Any]:
        with self._operation("Run DRC", mutation=True):
            if severity not in ("all", "error", "warning"):
                raise BridgeError("severity must be all, error, or warning.")
            board = self._board()
            with _artifact_run("drc") as directory:
                executable, version = _find_cli(self._kicad().get_version().major, directory)
                with _snapshot(board, directory, require_project=True) as snapshot:
                    report_path = directory / f"{Path(snapshot['name']).stem}-drc.json"
                    _run_cli(executable, [
                        "pcb", "drc", "--format", "json", "--units", "mm",
                        f"--severity-{severity}", "--exit-code-violations", "--refill-zones",
                        "--output", str(report_path), str(snapshot["path"]),
                    ], snapshot["path"].parent, label="DRC", allowed_codes=(0, 5))
                    summary, violations = _read_drc_report(report_path)
                    response = {
                        "board": snapshot["name"], "snapshot": "last_saved_board",
                        "includes_unsaved_changes": False, "note": SNAPSHOT_NOTE,
                        "context": snapshot["context"], "cli_version": version,
                        "severity": severity, "summary": summary,
                        "violations": violations[:MAX_RETURNED_VIOLATIONS],
                        "violations_truncated": len(violations) > MAX_RETURNED_VIOLATIONS,
                        "schematic_parity_checked": False, "zones_refilled_in_copy": True,
                        "report": _artifact(report_path, directory),
                    }
                return response

    def export_fabrication(self, formats: list[str] | None = None) -> dict[str, Any]:
        with self._operation("Export fabrication files", mutation=True):
            selected = ["gerbers", "drill", "positions"] if formats is None else formats
            if not isinstance(selected, list) or not selected or not all(
                isinstance(value, str) and value in FORMATS for value in selected
            ):
                raise BridgeError("formats must be a nonempty list containing gerbers, drill, positions, or svg.")
            if len(selected) != len(set(selected)):
                raise BridgeError("Each fabrication format must occur only once.")
            board = self._board()
            with _artifact_run("fabrication") as directory:
                executable, version = _find_cli(self._kicad().get_version().major, directory)
                with _snapshot(board, directory, require_project=False) as snapshot:
                    for file_format in selected:
                        output = directory / file_format
                        output.mkdir(mode=0o700)
                        stem = Path(snapshot["name"]).stem
                        if file_format == "gerbers":
                            arguments = ["gerbers", "--check-zones", "--use-drill-file-origin", "--output", str(output) + os.sep]
                        elif file_format == "drill":
                            arguments = ["drill", "--format", "excellon", "--drill-origin", "plot", "--excellon-units", "mm", "--output", str(output) + os.sep]
                        elif file_format == "positions":
                            arguments = ["pos", "--format", "csv", "--units", "mm", "--side", "both", "--exclude-dnp", "--use-drill-file-origin", "--output", str(output / f"{stem}-positions.csv")]
                        else:
                            arguments = ["svg", "--layers", SVG_LAYERS, "--mode-single", "--fit-page-to-board", "--exclude-drawing-sheet", "--check-zones", "--output", str(output / f"{stem}.svg")]
                        _run_cli(executable, ["pcb", "export", *arguments, str(snapshot["path"])], snapshot["path"].parent, label=f"{file_format} export")
                        if not any(path.is_file() for path in output.rglob("*")):
                            raise BridgeError(f"KiCad CLI completed {file_format} export without producing any files.")
                    response = {
                        "board": snapshot["name"], "snapshot": "last_saved_board",
                        "includes_unsaved_changes": False, "note": SNAPSHOT_NOTE,
                        "context": snapshot["context"], "cli_version": version,
                        "formats": list(selected), "directory": str(directory),
                        "coordinate_origins": {
                            value: "board_fit" if value == "svg" else "drill_place"
                            for value in selected
                        },
                    }
                # Enumerate only after private board/project staging has been removed.
                response["files"] = [_artifact(path, directory) for path in sorted(directory.rglob("*")) if path.is_file() or path.is_symlink()]
                if "positions" in selected:
                    response["position_units"] = "mm"
                if "svg" in selected:
                    response["svg_layers"] = SVG_LAYERS.split(",")
                return response
