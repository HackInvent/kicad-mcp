"""KiCad IPC action: stop the server associated with this editor instance."""

from kicad_mcp.__main__ import main


if __name__ == "__main__":
    raise SystemExit(main(["stop"]))
