"""CLI command, snapshot isolation and failure checks without a KiCad installation."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from kipy.kicad import KiCadVersion

from kicad_mcp.bridge import KiCadBridge
from kicad_mcp.errors import BridgeError
from kicad_mcp.fabrication import (
    FabricationMixin,
    _rebase_library_table,
    artifact_directory,
)
import kicad_mcp.fabrication as fabrication


class FakeCLI:
    def __init__(self):
        self.calls = []
        self.snapshots = []
        self.version = "10.0.5\n"
        self.exit_code = 0
        self.exception = None
        self.no_output = False
        self.report = {
            "coordinate_units": "mm",
            "violations": [
                {"type": "clearance", "severity": "error", "description": "Copper clearance", "excluded": False},
                {"type": "silk_overlap", "severity": "warning", "description": "Text overlap", "excluded": False},
                {"type": "hole_clearance", "severity": "error", "description": "Excluded clearance", "excluded": True},
            ],
            "unconnected_items": [
                {"type": "unconnected_items", "severity": "error", "description": "Unconnected pads", "excluded": False}
            ],
            "schematic_parity": [],
        }
        self.report_bytes = None
        self.fail_format = None

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        assert isinstance(command, list)
        assert "shell" not in kwargs or kwargs["shell"] is False
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["timeout"] == fabrication.CLI_TIMEOUT_SECONDS
        if command[1:] == ["version"]:
            return subprocess.CompletedProcess(command, 0, self.version, "")
        if self.exception:
            raise self.exception
        snapshot = Path(command[-1])
        self.snapshots.append({p.name: p.read_bytes() for p in snapshot.parent.iterdir()})
        assert snapshot.read_text() == "(kicad_pcb saved-board-state)\n"
        assert kwargs["cwd"] == snapshot.parent
        if self.fail_format and command[3] == self.fail_format:
            return subprocess.CompletedProcess(command, 3, "secret source", "secret token")
        output = Path(command[command.index("--output") + 1])
        if not self.no_output and self.exit_code in (0, 5):
            if command[1:3] == ["pcb", "drc"]:
                output.write_bytes(self.report_bytes if self.report_bytes is not None else json.dumps(self.report).encode())
            elif command[3] == "gerbers":
                (output / "board-F_Cu.gbr").write_text("gerber content")
                (output / "board-Edge_Cuts.gbr").write_text("outline content")
            elif command[3] == "drill":
                (output / "board-PTH.drl").write_text("drill content")
            else:
                output.write_text("export content")
        return subprocess.CompletedProcess(command, self.exit_code, "private board output", "private API token")


@pytest.fixture
def environment(tmp_path, monkeypatch):
    project_dir = tmp_path / "my project"
    project_dir.mkdir()
    (project_dir / "board.kicad_pcb").write_text("(kicad_pcb saved-board-state)\n")
    (project_dir / "board.kicad_pro").write_text(json.dumps({
        "text_variables": {"LIBS": "${KIPRJMOD}/footprints"},
        "board": {"design_settings": {"rules": {"min_clearance": 0.2}}},
    }))
    (project_dir / "board.kicad_dru").write_text('(version 1)\n(rule "saved clearance")\n')
    (project_dir / "fp-lib-table").write_text('(fp_lib_table (lib (name "local") (uri "${LIBS}/Local.pretty")))\n')
    (project_dir / "private.env").write_text("secret never copied")
    board = Mock()
    board.name = "board.kicad_pcb"
    board.get_project.return_value = SimpleNamespace(path=str(project_dir), name="board")
    board.save.side_effect = AssertionError("Never save the original board")
    board.save_as.side_effect = AssertionError("SaveCopyOfDocument also saves original project settings")
    bridge = KiCadBridge()
    client = Mock()
    client.get_version.return_value = KiCadVersion(10, 0, 5, "10.0.5")
    client.get_board.return_value = board
    bridge._client = client
    artifacts = tmp_path / "artifacts"
    monkeypatch.setenv("KICAD_MCP_ARTIFACT_DIR", str(artifacts))
    monkeypatch.setenv("KICAD_API_TOKEN", "ipc-secret")
    monkeypatch.setenv("KICAD_MCP_TOKEN", "http-secret")
    monkeypatch.setenv("KICAD_API_SOCKET", "/private/ipc.sock")
    monkeypatch.setenv("KICAD_MCP_CLI", str(tmp_path / "kicad-cli"))
    monkeypatch.setattr(fabrication.shutil, "which", lambda name: str(tmp_path / "kicad-cli"))
    cli = FakeCLI()
    monkeypatch.setattr(fabrication.subprocess, "run", cli)
    return SimpleNamespace(bridge=bridge, board=board, client=client, cli=cli,
                           artifacts=artifacts, project=project_dir)


def run_drc(env, **kwargs):
    return FabricationMixin.run_drc(env.bridge, **kwargs)


def export(env, **kwargs):
    return FabricationMixin.export_fabrication(env.bridge, **kwargs)


def test_drc_copies_saved_context_without_saving_original_or_retaining_sources(environment):
    env = environment
    originals = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in env.project.iterdir()}
    env.cli.exit_code = 5
    result = run_drc(env)
    assert result["snapshot"] == "last_saved_board"
    assert result["includes_unsaved_changes"] is False
    assert "save_board" in result["note"]
    assert result["schematic_parity_checked"] is False
    assert result["zones_refilled_in_copy"] is True
    assert result["summary"] == {"errors": 2, "warnings": 1, "excluded": 1, "other": 0, "total": 4}
    assert result["violations"][-1]["category"] == "unconnected_items"
    assert result["context"] == {"project_settings": "last_saved", "custom_rules": True, "project_footprint_libraries": True}
    staged = env.cli.snapshots[0]
    assert set(staged) == {"board.kicad_pcb", "board.kicad_pro", "board.kicad_dru", "fp-lib-table"}
    staged_project = json.loads(staged["board.kicad_pro"])
    assert staged_project["board"] == json.loads(originals["board.kicad_pro"][0])["board"]
    assert staged_project["text_variables"]["LIBS"] == str(env.project / "footprints")
    assert staged["board.kicad_dru"] == originals["board.kicad_dru"][0]
    assert str(env.project / "footprints/Local.pretty").encode() in staged["fp-lib-table"]
    report = Path(result["report"]["path"])
    assert report.exists()
    assert list(report.parent.iterdir()) == [report]
    assert report.stat().st_size == result["report"]["size_bytes"]
    assert {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in env.project.iterdir()} == originals
    env.board.save.assert_not_called()
    env.board.save_as.assert_not_called()
    assert env.board.get_project.return_value.__dict__ == {"path": str(env.project), "name": "board"}
    if os.name == "posix":
        assert stat.S_IMODE(report.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(report.stat().st_mode) == 0o600


@pytest.mark.parametrize("severity", ["all", "error", "warning"])
def test_drc_flags_and_exclusion_of_ipc_credentials(environment, severity):
    run_drc(environment, severity=severity)
    command, options = environment.cli.calls[-1]
    assert command[1:3] == ["pcb", "drc"]
    assert command[3:9] == ["--format", "json", "--units", "mm", f"--severity-{severity}", "--exit-code-violations"]
    assert "--refill-zones" in command
    assert "--save-board" not in command
    assert "--schematic-parity" not in command
    for key in ("KICAD_API_TOKEN", "KICAD_MCP_TOKEN", "KICAD_API_SOCKET"):
        assert key not in options["env"]


def test_default_exports_use_official_flags_and_only_return_finished_outputs(environment):
    result = export(environment)
    assert result["formats"] == ["gerbers", "drill", "positions"]
    assert result["includes_unsaved_changes"] is False
    assert result["coordinate_origins"] == {"gerbers": "drill_place", "drill": "drill_place", "positions": "drill_place"}
    assert result["position_units"] == "mm"
    assert len(result["files"]) == 4
    commands = [command for command, _ in environment.cli.calls[1:]]
    assert [command[3] for command in commands] == ["gerbers", "drill", "pos"]
    assert "--check-zones" in commands[0] and "--use-drill-file-origin" in commands[0]
    assert commands[1][4:10] == ["--format", "excellon", "--drill-origin", "plot", "--excellon-units", "mm"]
    assert commands[2][4:10] == ["--format", "csv", "--units", "mm", "--side", "both"]
    assert "--exclude-dnp" in commands[2] and "--use-drill-file-origin" in commands[2]
    assert not any("--save-board" in command or "--force" in command for command in commands)
    assert "fp-lib-table" not in environment.cli.snapshots[0]
    for artifact in result["files"]:
        path = Path(artifact["path"])
        assert path.is_file()
        assert artifact["name"] == path.relative_to(result["directory"]).as_posix()
    assert not list(Path(result["directory"]).glob(".snapshot-*"))
    assert not list(Path(result["directory"]).rglob("*.kicad_pro"))
    assert not list(Path(result["directory"]).rglob("*.kicad_pcb"))


def test_svg_is_explicit_top_view_and_new_runs_do_not_overwrite(environment):
    first = export(environment, formats=["svg"])
    first_file = Path(first["files"][0]["path"])
    first_bytes = first_file.read_bytes()
    second = export(environment, formats=["svg"])
    assert first["directory"] != second["directory"]
    assert first_file.read_bytes() == first_bytes
    assert first["svg_layers"] == ["F.Cu", "F.SilkS", "Edge.Cuts"]
    assert first["coordinate_origins"] == {"svg": "board_fit"}
    assert "position_units" not in first
    command = environment.cli.calls[-1][0]
    assert command[3] == "svg"
    assert command[command.index("--layers") + 1] == "F.Cu,F.SilkS,Edge.Cuts"
    for option in ("--mode-single", "--fit-page-to-board", "--exclude-drawing-sheet", "--check-zones"):
        assert option in command


@pytest.mark.parametrize("formats", [[], "gerbers", ["step"], ["gerbers", "gerbers"], [1], ["../outside"], ["gerbers; touch bad"]])
def test_invalid_formats_never_invoke_cli(environment, formats):
    with pytest.raises(BridgeError, match="format"):
        export(environment, formats=formats)
    assert environment.cli.calls == []
    assert not environment.artifacts.exists()


@pytest.mark.parametrize("severity", ["errors", "exclusion", "all --save-board", "", None])
def test_invalid_severity_never_invoke_cli(environment, severity):
    with pytest.raises(BridgeError, match="severity"):
        run_drc(environment, severity=severity)
    assert environment.cli.calls == []


@pytest.mark.parametrize("method", [run_drc, export])
def test_artifact_tools_are_blocked_in_read_only_mode(environment, method):
    environment.bridge.read_only = True
    with pytest.raises(BridgeError, match="read-only"):
        method(environment)
    assert not environment.artifacts.exists()
    assert environment.cli.calls == []


@pytest.mark.parametrize("method", [run_drc, export])
def test_missing_cli_is_actionable_and_cleans_new_run(environment, monkeypatch, method):
    monkeypatch.setattr(fabrication.shutil, "which", lambda name: None)
    with pytest.raises(BridgeError, match="KICAD_MCP_CLI"):
        method(environment)
    assert list(environment.artifacts.iterdir()) == []


@pytest.mark.parametrize("version", ["9.0.6\n", "11.0.0\n", "not a KiCad version\n"])
def test_cli_version_must_match_supported_editor(environment, version):
    environment.cli.version = version
    with pytest.raises(BridgeError, match="version"):
        run_drc(environment)
    assert len(environment.cli.calls) == 1
    assert list(environment.artifacts.iterdir()) == []


@pytest.mark.parametrize("method", [run_drc, export])
def test_unsaved_board_is_rejected_before_export(environment, method):
    (environment.project / "board.kicad_pcb").unlink()
    with pytest.raises(BridgeError, match="Save it in KiCad"):
        method(environment)
    assert len(environment.cli.calls) == 1  # Version check only.
    assert list(environment.artifacts.iterdir()) == []


def test_drc_requires_saved_project_settings_but_standalone_exports_are_explicit(environment):
    (environment.project / "board.kicad_pro").unlink()
    with pytest.raises(BridgeError, match="kicad_pro"):
        run_drc(environment)
    result = export(environment, formats=["svg"])
    assert result["context"]["project_settings"] == "KiCad_defaults"


def test_board_and_project_with_different_stems_keep_correct_saved_project(environment):
    (environment.project / "board.kicad_pro").rename(environment.project / "project.kicad_pro")
    (environment.project / "board.kicad_dru").rename(environment.project / "project.kicad_dru")
    environment.board.get_project.return_value.name = "project"
    run_drc(environment)
    assert "board.kicad_pro" in environment.cli.snapshots[0]
    assert "board.kicad_dru" in environment.cli.snapshots[0]


@pytest.mark.parametrize("method", [run_drc, export])
def test_cli_failure_is_sanitized_and_cleans_staging_and_outputs(environment, method):
    environment.cli.exit_code = 3
    with pytest.raises(BridgeError, match="exit code 3") as error:
        method(environment)
    assert "private" not in str(error.value)
    assert "token" not in str(error.value)
    assert list(environment.artifacts.iterdir()) == []


def test_later_failed_export_does_not_leave_an_incomplete_bundle(environment):
    environment.cli.fail_format = "drill"
    with pytest.raises(BridgeError, match="drill export"):
        export(environment)
    assert len(environment.cli.snapshots) == 2
    assert list(environment.artifacts.iterdir()) == []


@pytest.mark.parametrize("exception,expected", [
    (subprocess.TimeoutExpired("private command", 120, output="secret"), "timed out"),
    (PermissionError("private path"), "Cannot launch"),
])
def test_timeout_and_launch_failure_are_sanitized(environment, exception, expected):
    environment.cli.exception = exception
    with pytest.raises(BridgeError, match=expected) as error:
        run_drc(environment)
    assert "private" not in str(error.value) and "secret" not in str(error.value)
    assert list(environment.artifacts.iterdir()) == []


@pytest.mark.parametrize("method", [run_drc, export])
def test_cli_success_without_output_is_not_reported_as_success(environment, method):
    environment.cli.no_output = True
    with pytest.raises(BridgeError, match="report|without producing"):
        method(environment)
    assert list(environment.artifacts.iterdir()) == []


@pytest.mark.parametrize("data", [b"invalid", b"{}", b'"wrong type"', b'{"violations":{},"unconnected_items":[],"schematic_parity":[]}'])
def test_invalid_drc_json_is_rejected(environment, data):
    environment.cli.report_bytes = data
    with pytest.raises(BridgeError, match="invalid DRC JSON"):
        run_drc(environment)
    assert list(environment.artifacts.iterdir()) == []


def test_large_drc_response_keeps_full_report_and_limits_inline_results(environment, monkeypatch):
    monkeypatch.setattr(fabrication, "MAX_RETURNED_VIOLATIONS", 2)
    result = run_drc(environment)
    assert len(result["violations"]) == 2
    assert result["violations_truncated"] is True
    assert result["summary"]["total"] == 4
    report = json.loads(Path(result["report"]["path"]).read_text())
    assert len(report["violations"]) == 3


def test_oversized_drc_report_is_rejected(environment, monkeypatch):
    monkeypatch.setattr(fabrication, "MAX_REPORT_BYTES", 5)
    with pytest.raises(BridgeError, match="size limit"):
        run_drc(environment)


def test_saved_library_paths_use_saved_project_variables_and_preserve_remote_uris(tmp_path):
    table = b'(fp_lib_table (lib (uri "${LIBS}/a.pretty")) (lib (uri "local/b.pretty")) (lib (uri "https://example.test/lib")) (lib (uri "${KICAD10_FOOTPRINT_DIR}/Global.pretty")))'
    result = _rebase_library_table(table, tmp_path, {"LIBS": "${KIPRJMOD}/libs", "KIPRJMOD": str(tmp_path)}).decode()
    assert str(tmp_path / "libs/a.pretty") in result
    assert str(tmp_path / "local/b.pretty") in result
    assert "https://example.test/lib" in result
    assert "${KICAD10_FOOTPRINT_DIR}/Global.pretty" in result


def test_recursive_project_variables_are_rejected_before_cli_execution(environment):
    project = environment.project / "board.kicad_pro"
    project.write_text(json.dumps({"text_variables": {"LIBS": "${OTHER}", "OTHER": "${LIBS}"}}))
    with pytest.raises(BridgeError, match="recursive"):
        run_drc(environment)
    assert len(environment.cli.calls) == 1


def test_artifact_directory_respects_overrides_and_platform_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("KICAD_MCP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("KICAD_MCP_ARTIFACT_DIR", str(tmp_path / "custom"))
    assert artifact_directory() == tmp_path / "custom"
    monkeypatch.delenv("KICAD_MCP_ARTIFACT_DIR")
    assert artifact_directory() == tmp_path / "state/artifacts"
    monkeypatch.delenv("KICAD_MCP_STATE_DIR")
    monkeypatch.setattr(fabrication.sys, "platform", "linux")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    assert artifact_directory() == tmp_path / "xdg/kicad-mcp/artifacts"
    monkeypatch.setattr(fabrication.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert artifact_directory() == tmp_path / "local/kicad-mcp/artifacts"
    monkeypatch.setattr(fabrication.sys, "platform", "darwin")
    assert artifact_directory() == Path.home() / "Library/Application Support/kicad-mcp/artifacts"


def test_dotted_project_names_are_not_truncated(environment):
    (environment.project / "board.kicad_pro").rename(environment.project / "board.rev2.kicad_pro")
    (environment.project / "board.kicad_dru").rename(environment.project / "board.rev2.kicad_dru")
    environment.board.get_project.return_value.name = "board.rev2"
    result = run_drc(environment)
    assert result["context"]["custom_rules"] is True
    assert "board.kicad_pro" in environment.cli.snapshots[0]


def test_custom_drawing_sheet_paths_resolve_against_original_saved_project(environment):
    path = environment.project / "board.kicad_pro"
    settings = json.loads(path.read_text())
    settings["pcbnew"] = {"page_layout_descr_file": "templates/custom.kicad_wks"}
    settings["text_variables"]["HERE"] = "$(KIPRJMOD)"
    path.write_text(json.dumps(settings))
    original = path.read_bytes()
    run_drc(environment)
    copied = json.loads(environment.cli.snapshots[0]["board.kicad_pro"])
    assert copied["pcbnew"]["page_layout_descr_file"] == str(environment.project / "templates/custom.kicad_wks")
    assert copied["text_variables"]["HERE"] == str(environment.project)
    assert path.read_bytes() == original


@pytest.mark.skipif(os.name == "nt", reason="Windows symlink permissions vary")
def test_cli_symlink_artifacts_are_rejected_without_touching_external_files(environment, monkeypatch, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("outside data")
    runner = environment.cli

    def linked_output(command, **kwargs):
        result = runner(command, **kwargs)
        if command[1:4] == ["pcb", "export", "svg"]:
            output = Path(command[command.index("--output") + 1])
            output.unlink()
            output.symlink_to(outside)
        return result

    monkeypatch.setattr(fabrication.subprocess, "run", linked_output)
    with pytest.raises(BridgeError, match="invalid artifact path"):
        export(environment, formats=["svg"])
    assert outside.read_text() == "outside data"
    assert list(environment.artifacts.iterdir()) == []
