"""Exercise the distributed plugin layout and non-destructive local installer."""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import re
import runpy
import subprocess
import sys
import types
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
IDENTIFIER = "org.hackinvent.kicad-mcp"


def run_script(name: str, *arguments: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / name), *arguments],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_manifest_uses_ipc_python_and_resolvable_pcb_actions():
    manifest = json.loads((ROOT / "plugin" / "plugin.json").read_text())
    assert manifest["identifier"] == IDENTIFIER
    assert manifest["runtime"] == {"type": "python", "min_version": "3.10"}
    assert manifest["$schema"] == "https://go.kicad.org/api/schemas/v1"
    assert manifest["name"] and manifest["description"]
    assert len({action["identifier"] for action in manifest["actions"]}) == 2
    for action in manifest["actions"]:
        assert action["name"] and action["description"]
        assert action["scopes"] == ["pcb"]
        assert action["show-button"] is True
        assert (ROOT / "plugin" / action["entrypoint"]).is_file()


def test_plugin_and_python_distribution_install_the_same_dependencies():
    pyproject = (ROOT / "pyproject.toml").read_text()
    dependencies = re.search(r"(?ms)^dependencies\s*=\s*(\[.*?\])", pyproject)
    assert dependencies is not None
    plugin_dependencies = {
        line.strip() for line in (ROOT / "plugin" / "requirements.txt").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }
    assert set(ast.literal_eval(dependencies[1])) == plugin_dependencies


@pytest.mark.parametrize("filename,arguments", [
    ("start.py", ["serve", "--transport", "streamable-http"]),
    ("stop.py", ["stop"]),
])
def test_plugin_actions_invoke_cli(monkeypatch, filename, arguments):
    calls = []
    cli = types.ModuleType("kicad_mcp.__main__")
    cli.main = lambda args: calls.append(args) or 0
    monkeypatch.setitem(sys.modules, "kicad_mcp.__main__", cli)
    with pytest.raises(SystemExit) as raised:
        runpy.run_path(str(ROOT / "plugin" / filename), run_name="__main__")
    assert raised.value.code == 0
    assert calls == [arguments]


def test_pcm_archive_is_reproducible_and_runs_without_installing_project(tmp_path):
    outputs = [tmp_path / "first.zip", tmp_path / "second.zip"]
    for output in outputs:
        result = run_script("build_plugin.py", "--output", str(output))
        assert result.returncode == 0, result.stderr
    assert outputs[0].read_bytes() == outputs[1].read_bytes()
    extracted = tmp_path / "extracted"
    with zipfile.ZipFile(outputs[0]) as archive:
        names = set(archive.namelist())
        assert "metadata.json" in names
        assert "plugins/plugin.json" in names
        assert "plugins/requirements.txt" in names
        assert "plugins/LICENSE" in names
        assert "plugins/kicad_mcp/__init__.py" in names
        assert "plugins/kicad_mcp/__main__.py" in names
        assert all(name == "metadata.json" or name.startswith("plugins/") for name in names)
        assert not any("__pycache__" in name or name.endswith(".pyc") for name in names)
        metadata = json.loads(archive.read("metadata.json"))
        assert metadata["identifier"] == IDENTIFIER
        assert metadata["type"] == "plugin"
        assert metadata["license"] == "MIT"
        assert metadata["author"]["contact"]["web"] == "https://github.com/HackInvent"
        version = metadata["versions"][0]
        assert version["runtime"] == "ipc"
        assert version["kicad_version"] == "10.0"
        assert not any(key.startswith("download_") for key in version)
        archive.extractall(extracted)
    env = dict(os.environ, PYTHONPATH=str(extracted / "plugins"))
    result = subprocess.run(
        [sys.executable, "-m", "kicad_mcp", "--help"], cwd=tmp_path, env=env,
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "serve" in result.stdout


def test_installer_respects_documents_home_and_version(tmp_path):
    env = dict(os.environ, KICAD_DOCUMENTS_HOME=str(tmp_path))
    result = run_script("install_plugin.py", "--version", "10.0", env=env)
    assert result.returncode == 0, result.stderr
    destination = tmp_path / "10.0" / "plugins" / IDENTIFIER
    assert json.loads((destination / "plugin.json").read_text())["identifier"] == IDENTIFIER
    assert (destination / "kicad_mcp" / "__main__.py").is_file()
    assert (destination / "requirements.txt").is_file()


def test_installer_refuses_unknown_directory_even_with_overwrite(tmp_path):
    destination = tmp_path / "existing"
    destination.mkdir()
    sentinel = destination / "keep.txt"
    sentinel.write_text("user data")
    result = run_script("install_plugin.py", "--destination", str(destination), "--overwrite")
    assert result.returncode != 0
    assert sentinel.read_text() == "user data"
    assert not (destination / "plugin.json").exists()


def test_installer_update_preserves_unrelated_files_and_requires_flag(tmp_path):
    destination = tmp_path / "plugin"
    result = run_script("install_plugin.py", "--destination", str(destination))
    assert result.returncode == 0, result.stderr
    (destination / "notes.txt").write_text("personal notes")
    (destination / "start.py").write_text("# old version\n")
    result = run_script("install_plugin.py", "--destination", str(destination))
    assert result.returncode != 0
    assert (destination / "start.py").read_text() == "# old version\n"
    result = run_script("install_plugin.py", "--destination", str(destination), "--overwrite")
    assert result.returncode == 0, result.stderr
    assert (destination / "start.py").read_bytes() == (ROOT / "plugin" / "start.py").read_bytes()
    assert (destination / "notes.txt").read_text() == "personal notes"


@pytest.mark.skipif(sys.platform == "win32", reason="Windows symlink permissions vary")
def test_installer_rejects_symlinks_before_overwriting_any_file(tmp_path):
    destination = tmp_path / "plugin"
    result = run_script("install_plugin.py", "--destination", str(destination))
    assert result.returncode == 0, result.stderr
    outside = tmp_path / "outside.py"
    outside.write_text("outside data")
    (destination / "start.py").unlink()
    (destination / "start.py").symlink_to(outside)
    (destination / "stop.py").write_text("unchanged")
    result = run_script("install_plugin.py", "--destination", str(destination), "--overwrite")
    assert result.returncode != 0
    assert outside.read_text() == "outside data"
    assert (destination / "stop.py").read_text() == "unchanged"


def test_installer_rejects_version_path_traversal(tmp_path):
    env = dict(os.environ, KICAD_DOCUMENTS_HOME=str(tmp_path))
    result = run_script("install_plugin.py", "--version", "../other", env=env)
    assert result.returncode != 0
    assert not list(tmp_path.iterdir())
