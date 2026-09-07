#!/usr/bin/env python3
"""Build a KiCad Plugin and Content Manager ZIP using only the standard library."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import zipfile


ROOT = Path(__file__).resolve().parents[1]
IDENTIFIER = "org.hackinvent.kicad-mcp"
PLUGIN_FILES = ("plugin.json", "requirements.txt", "start.py", "stop.py")


def project_version(root: Path = ROOT) -> str:
    """Read the single project version without requiring tomllib on Python 3.10."""
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    project = re.search(r"(?ms)^\[project\]\s*$(.*?)(?=^\[|\Z)", text)
    match = re.search(r'^version\s*=\s*"(\d+\.\d+\.\d+)"\s*$', project[1], re.M) if project else None
    if not match:
        raise ValueError("pyproject.toml must declare a project version such as 0.1.0")
    return match[1]


def plugin_files(root: Path = ROOT) -> dict[str, bytes]:
    """Return only files intended for installation, excluding caches and build files."""
    paths = [(name, root / "plugin" / name) for name in PLUGIN_FILES]
    package = root / "src" / "kicad_mcp"
    if not (package / "__init__.py").is_file():
        raise ValueError(f"Python package not found: {package}")
    paths.extend(
        (str(Path("kicad_mcp") / path.relative_to(package)).replace("\\", "/"), path)
        for path in sorted(package.rglob("*.py"))
        if "__pycache__" not in path.parts
    )
    if (package / "py.typed").is_file():
        paths.append(("kicad_mcp/py.typed", package / "py.typed"))
    paths.append(("LICENSE", root / "LICENSE"))
    result = {}
    for name, path in paths:
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError(f"Refusing to package a linked file: {path}")
        result[name] = path.read_bytes()
    manifest = json.loads(result["plugin.json"])
    if manifest.get("identifier") != IDENTIFIER:
        raise ValueError("Unexpected plugin identifier")
    for action in manifest["actions"]:
        if action["entrypoint"] not in result:
            raise ValueError(f"Missing action entrypoint: {action['entrypoint']}")
    return result


def package_metadata(version: str) -> dict:
    return {
        "$schema": "https://go.kicad.org/pcm/schemas/v2",
        "name": "HackInvent KiCad MCP",
        "description": "Connect MCP clients to KiCad's PCB editor using the official IPC API.",
        "description_full": (
            "An MCP server for a running KiCad PCB editor, with plugin actions to start and "
            "stop a local server. Requires KiCad 10.0 or later, Python 3.10 or later, and "
            "the KiCad IPC API enabled. Python dependencies are installed by KiCad."
        ),
        "identifier": IDENTIFIER,
        "type": "plugin",
        "author": {"name": "HackInvent", "contact": {"web": "https://github.com/HackInvent"}},
        "license": "MIT",
        "resources": {
            "homepage": "https://github.com/HackInvent/kicad-mcp",
            "issues": "https://github.com/HackInvent/kicad-mcp/issues",
        },
        "versions": [{
            "version": version,
            "status": "development",
            "kicad_version": "10.0",
            "runtime": "ipc",
        }],
    }


def build(output: Path | None = None, root: Path = ROOT) -> Path:
    version = project_version(root)
    destination = output or root / "dist" / f"hackinvent-kicad-mcp-{version}.zip"
    payload = {f"plugins/{name}": data for name, data in plugin_files(root).items()}
    payload["metadata.json"] = (json.dumps(package_metadata(version), indent=2) + "\n").encode()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(payload.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="ZIP output path (default: dist/<name>-<version>.zip)")
    args = parser.parse_args()
    print(build(args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
