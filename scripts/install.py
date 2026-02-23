"""Install the agentic-renderdoc MCP server.

Installs the server wheel (builds first if needed) and prints
the MCP client configuration for Claude Code / Claude Desktop.

Usage:
    python scripts/install.py
"""

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(ROOT, "dist")


def find_or_build_wheel():
    """Find an existing wheel in dist/, or build one."""
    if os.path.isdir(DIST):
        wheels = [f for f in os.listdir(DIST) if f.endswith(".whl")]
        if wheels:
            return os.path.join(DIST, wheels[0])

    print("No wheel found, building...")
    subprocess.check_call(
        [sys.executable, os.path.join(ROOT, "package.py")],
        cwd=ROOT,
    )
    wheels = [f for f in os.listdir(DIST) if f.endswith(".whl")]
    if not wheels:
        print("Build failed: no wheel produced.")
        sys.exit(1)
    return os.path.join(DIST, wheels[0])


def install_wheel(wheel_path):
    """pip install the wheel."""
    print(f"Installing {os.path.basename(wheel_path)}...")
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "--force-reinstall", wheel_path],
    )
    print("Installed.")


def print_config():
    """Print the MCP client configuration."""
    config = {
        "mcpServers": {
            "renderdoc": {
                "command": "agentic-renderdoc",
            }
        }
    }

    print()
    print("MCP client configuration:")
    print("-" * 40)
    print(json.dumps(config, indent=2))
    print("-" * 40)
    print()
    print("Add this to your MCP client config:")
    print("  Claude Code:    ~/.claude/claude_desktop_config.json")
    print("  Claude Desktop: Settings > Developer > MCP Servers")
    print()
    print("Or run directly: agentic-renderdoc")


def main():
    wheel = find_or_build_wheel()
    install_wheel(wheel)
    print_config()


if __name__ == "__main__":
    main()
