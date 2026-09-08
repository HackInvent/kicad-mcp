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
    # --help does not import the adapters. Discover tools from the extracted
    # plugin too, so a missing module cannot hide behind the lazy CLI startup.
    result = subprocess.run(
        [sys.executable, "-c", (
            "import asyncio, json; from kicad_mcp import __version__; "
            "from kicad_mcp.server import create_server; "
            "print(json.dumps({'version': __version__, "
            "'tools': len(asyncio.run(create_server().list_tools()))}))"
        )], cwd=tmp_path, env=env, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"version": version["version"], "tools": 28}


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


@pytest.fixture
def packaging_modules(monkeypatch):
    import importlib

    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    return importlib.import_module("build_plugin"), importlib.import_module("install_plugin")


def test_installer_does_not_modify_external_hard_link_target(tmp_path, packaging_modules):
    _, installer = packaging_modules
    destination = installer.install(tmp_path / "installed")
    external = tmp_path / "unrelated.txt"
    external.write_text("unrelated document")
    (destination / "start.py").unlink()
    os.link(external, destination / "start.py")
    installer.install(destination, overwrite=True)
    assert external.read_text() == "unrelated document"
    assert (destination / "start.py").read_bytes() == (ROOT / "plugin" / "start.py").read_bytes()
    assert not os.path.samefile(external, destination / "start.py")


@pytest.mark.parametrize("existing", [False, True])
def test_installer_write_failure_keeps_previous_installation_intact(
    tmp_path, monkeypatch, packaging_modules, existing,
):
    _, installer = packaging_modules
    destination = tmp_path / "installed"
    if existing:
        installer.install(destination)
        (destination / "start.py").write_text("old start script")
        (destination / "notes.txt").write_text("user notes")
        previous = {
            str(path.relative_to(destination)): path.read_bytes()
            for path in destination.rglob("*") if path.is_file()
        }
    original_write = Path.write_bytes

    def fail_after_first_payload_files(path, data):
        if path.name == "stop.py":
            raise OSError("simulated full disk")
        return original_write(path, data)

    monkeypatch.setattr(Path, "write_bytes", fail_after_first_payload_files)
    with pytest.raises(OSError, match="full disk"):
        installer.install(destination, overwrite=existing)
    if existing:
        assert {
            str(path.relative_to(destination)): path.read_bytes()
            for path in destination.rglob("*") if path.is_file()
        } == previous
    else:
        assert not destination.exists()
    assert not list(tmp_path.glob(".*.install-*"))


def test_installer_activation_failure_restores_previous_directory(
    tmp_path, monkeypatch, packaging_modules,
):
    _, installer = packaging_modules
    destination = installer.install(tmp_path / "installed")
    (destination / "start.py").write_text("old start script")
    original_replace = os.replace

    def fail_activation(source, target):
        if Path(source).name == "prepared":
            raise OSError("simulated activation failure")
        return original_replace(source, target)

    monkeypatch.setattr(installer.os, "replace", fail_activation)
    with pytest.raises(OSError, match="activation failure"):
        installer.install(destination, overwrite=True)
    assert (destination / "start.py").read_text() == "old start script"
    assert not list(tmp_path.glob(".*.install-*"))


def test_installer_preserves_recovery_copy_if_activation_and_restore_fail(
    tmp_path, monkeypatch, packaging_modules,
):
    _, installer = packaging_modules
    destination = installer.install(tmp_path / "installed")
    (destination / "start.py").write_text("old start script")
    original_replace = os.replace

    def fail_activation_and_restoration(source, target):
        if Path(source).name in {"prepared", "previous"}:
            raise OSError("simulated directory failure")
        return original_replace(source, target)

    monkeypatch.setattr(installer.os, "replace", fail_activation_and_restoration)
    with pytest.raises(OSError, match="files are preserved at") as error:
        installer.install(destination, overwrite=True)
    backups = list(tmp_path.glob(".*.install-*/previous"))
    assert len(backups) == 1
    assert (backups[0] / "start.py").read_text() == "old start script"
    assert str(backups[0]) in str(error.value)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows symlink permissions vary")
def test_installer_keeps_unrelated_symlinks_without_copying_their_targets(
    tmp_path, packaging_modules,
):
    _, installer = packaging_modules
    destination = installer.install(tmp_path / "installed")
    external = tmp_path / "external"
    external.mkdir()
    (external / "notes.txt").write_text("personal notes")
    (destination / "notes").symlink_to(external, target_is_directory=True)
    installer.install(destination, overwrite=True)
    assert (destination / "notes").is_symlink()
    assert (destination / "notes" / "notes.txt").read_text() == "personal notes"


def test_interrupted_archive_build_keeps_last_complete_archive(
    tmp_path, monkeypatch, packaging_modules,
):
    builder, _ = packaging_modules
    output = builder.build(tmp_path / "plugin.zip")
    previous = output.read_bytes()
    original_write = zipfile.ZipFile.writestr
    written = []

    def fail_second_file(archive, *args, **kwargs):
        written.append(True)
        if len(written) == 2:
            raise OSError("simulated archive failure")
        return original_write(archive, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "writestr", fail_second_file)
    with pytest.raises(OSError, match="archive failure"):
        builder.build(output)
    assert output.read_bytes() == previous
    assert list(tmp_path.iterdir()) == [output]


def test_archive_build_does_not_overwrite_hard_link_target(tmp_path, packaging_modules):
    builder, _ = packaging_modules
    external = tmp_path / "unrelated.txt"
    external.write_text("unrelated document")
    output = tmp_path / "plugin.zip"
    os.link(external, output)
    builder.build(output)
    assert external.read_text() == "unrelated document"
    assert zipfile.is_zipfile(output)
    assert not os.path.samefile(external, output)


@pytest.mark.parametrize("relative_path", ["LICENSE", "pyproject.toml", "src/kicad_mcp/new.zip"])
def test_archive_destination_cannot_overwrite_checkout_sources(
    tmp_path, packaging_modules, relative_path,
):
    import shutil

    builder, _ = packaging_modules
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    for name in ("LICENSE", "pyproject.toml"):
        shutil.copyfile(ROOT / name, checkout / name)
    shutil.copytree(ROOT / "plugin", checkout / "plugin")
    package = checkout / "src" / "kicad_mcp"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("# test package\n")
    output = checkout / relative_path
    previous = output.read_bytes() if output.exists() else None
    with pytest.raises(ValueError, match="checkout's sources"):
        builder.build(output, root=checkout)
    if previous is None:
        assert not output.exists()
    else:
        assert output.read_bytes() == previous


def test_installer_invalidates_stale_bytecode_for_same_size_same_timestamp_update(
    tmp_path, monkeypatch, packaging_modules,
):
    builder, installer = packaging_modules
    destination = installer.install(tmp_path / "installed")
    module = destination / "kicad_mcp" / "__init__.py"
    version = builder.project_version()
    old_version = ("1" if version[0] != "1" else "2") + version[1:]
    module.write_bytes(module.read_bytes().replace(version.encode(), old_version.encode(), 1))
    os.utime(module, (1234567890, 1234567890))
    env = dict(os.environ, PYTHONPATH=str(destination))
    command = [sys.executable, "-c", "from kicad_mcp import __version__; print(__version__)"]
    assert subprocess.check_output(command, cwd=tmp_path, env=env, text=True).strip() == old_version
    original_write = Path.write_bytes

    def write_with_same_timestamp(path, data):
        result = original_write(path, data)
        os.utime(path, (1234567890, 1234567890))
        return result

    monkeypatch.setattr(Path, "write_bytes", write_with_same_timestamp)
    installer.install(destination, overwrite=True)
    assert subprocess.check_output(command, cwd=tmp_path, env=env, text=True).strip() == version
