"""Backlot's own version, as ``backlot --version``, telemetry and the MCP ``serverInfo`` report it."""

from __future__ import annotations

from importlib import metadata


def version() -> str:
    try:
        return metadata.version("backlot")
    except metadata.PackageNotFoundError:  # a source tree that was never installed
        return "unknown"
