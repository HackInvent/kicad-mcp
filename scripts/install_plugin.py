#!/usr/bin/env python3
"""Install this checkout as a KiCad IPC plugin; KiCad installs its dependencies."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile

from build_plugin import IDENTIFIER, ROOT, plugin_files


def default_destination(version: str) -> Path:
    if not re.fullmatch(r"\d+\.\d+", version):
        raise ValueError("KiCad version must have the form 10.0")
    override = os.environ.get("KICAD_DOCUMENTS_HOME")
    if override:
        documents = Path(override).expanduser()
    elif sys.platform == "darwin" or sys.platform == "win32":
        documents = Path.home() / "Documents" / "KiCad"
    else:
        documents = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "KiCad"
    return documents / version / "plugins" / IDENTIFIER


def install(destination: Path, *, overwrite: bool = False, root: Path = ROOT) -> Path:
    payload = plugin_files(root)
    destination = destination.expanduser().absolute()
    if destination.is_symlink():
        raise ValueError(f"Refusing a symlink as plugin destination: {destination}")
    if destination.resolve().is_relative_to((root / "plugin").resolve()) or destination.resolve().is_relative_to((root / "src").resolve()):
        raise ValueError("The installation destination must not overwrite the checkout's sources")
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        if not overwrite:
            raise ValueError(f"Destination is not empty: {destination}. Use --overwrite to update this plugin.")
        manifest = destination / "plugin.json"
        if manifest.is_symlink() or not manifest.is_file():
            raise ValueError("Refusing to overwrite a directory without this plugin's manifest")
        if json.loads(manifest.read_text(encoding="utf-8")).get("identifier") != IDENTIFIER:
            raise ValueError("Refusing to overwrite a different plugin")
    # Check the entire write set before copying anything. Do not follow links in an
    # existing installation, and never remove files unrelated to this plugin.
    for name in payload:
        target = destination / name
        current = target
        while current != destination:
            if current.is_symlink():
                raise ValueError(f"Refusing to overwrite a linked path: {current}")
            current = current.parent
        if target.exists() and not target.is_file():
            raise ValueError(f"Expected a file at {target}")
        if any(parent.exists() and not parent.is_dir() for parent in target.parents if parent != destination):
            raise ValueError(f"A parent of {target} is not a directory")
    # Prepare the entire replacement before changing the installed plugin. A
    # direct write would also follow hard links and alter unrelated files.
    destination.parent.mkdir(parents=True, exist_ok=True)
    workspace = Path(tempfile.mkdtemp(prefix=f".{destination.name}.install-", dir=destination.parent))
    prepared = workspace / "prepared"
    backup = workspace / "previous"
    preserve_backup = False
    try:
        if destination.exists():
            # Retain unrelated files and links without traversing their targets.
            # copy2 creates independent files, so payload writes cannot follow
            # hard links from an existing installation into unrelated documents.
            shutil.copytree(destination, prepared, symlinks=True)
        else:
            prepared.mkdir()
        # Timestamp-based bytecode may remain valid when an update changes a
        # same-size module within one filesystem clock tick. Do not carry that
        # stale code into the replacement installation.
        for directory in {Path(name).parent for name in payload if name.endswith(".py")}:
            cache = prepared / directory / "__pycache__"
            if cache.is_symlink():
                cache.unlink()
            elif cache.is_dir():
                shutil.rmtree(cache)
        for name, data in payload.items():
            target = prepared / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        if destination.exists():
            os.replace(destination, backup)
        try:
            os.replace(prepared, destination)
        except OSError:
            if backup.exists():
                try:
                    os.replace(backup, destination)
                except OSError:
                    preserve_backup = True
                    raise OSError(
                        f"Installation failed and the original plugin could not be restored. "
                        f"Its files are preserved at {backup}."
                    ) from None
            raise
    finally:
        if not preserve_backup:
            shutil.rmtree(workspace, ignore_errors=True)
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default="10.0", help="KiCad version directory (default: 10.0)")
    parser.add_argument("--destination", type=Path, help="Override the full plugin installation directory")
    parser.add_argument("--overwrite", action="store_true", help="Update an existing installation with the same plugin ID")
    args = parser.parse_args()
    try:
        destination = args.destination or default_destination(args.version)
        installed = install(destination, overwrite=args.overwrite)
    except (ValueError, OSError) as error:
        parser.exit(1, f"Installation failed: {error}\n")
    print(f"Installed KiCad MCP in {installed}")
    print("Restart KiCad's PCB editor and enable its IPC API in Preferences > Plugins.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
