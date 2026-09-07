#!/usr/bin/env python3
"""Display the plugin server connection details without installing dependencies."""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kicad_mcp.__main__ import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main(["status", "--show-token", *sys.argv[1:]]))
