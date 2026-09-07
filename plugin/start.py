"""KiCad IPC action: serve the active PCB through a local HTTP MCP endpoint."""

from kicad_mcp.__main__ import main


if __name__ == "__main__":
    raise SystemExit(main(["serve", "--transport", "streamable-http"]))
