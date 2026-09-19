"""Entry point for the agentic-renderdoc MCP server."""

from __future__ import annotations

from server.app import mcp
from server.client import sweep_worker_logs


def main() -> None:
    sweep_worker_logs()
    mcp.run()


if __name__ == "__main__":
    main()
