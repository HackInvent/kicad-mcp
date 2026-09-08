"""Errors shared by KiCad adapters and safe to return through MCP."""


class BridgeError(RuntimeError):
    """An actionable error safe to expose to MCP clients."""
