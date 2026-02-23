<p align="center">
  <img src="docs/agentic-renderdoc.png" alt="Agentic RenderDoc" />
</p>

<p align="center">
  AI-assisted graphics debugging for <a href="https://renderdoc.org/">RenderDoc</a> via the <a href="https://modelcontextprotocol.io/">Model Context Protocol</a>.
  <br>
  <a href="LICENSE">MIT License</a>
</p>

---

Agentic RenderDoc gives an AI agent direct access to a live RenderDoc replay session. The agent can be directed to inspect draw calls, read back buffer and texture data, query pipeline state, diff state between events, and navigate the RenderDoc UI, all through natural language.

## Why

Graphics debugging is methodical. You capture a frame, walk the event list, check pipeline state, read back buffers, compare draws. An AI agent with access to both the application source and the captured GPU state can do this faster and more thoroughly than working through either alone.

## Architecture

```
Claude (MCP client)
    |  stdio (JSON-RPC)
    v
MCP Server (Python, FastMCP)
    |  TCP socket (JSON-lines, localhost)
    v
RenderDoc Extension (Python, inside RenderDoc)
    |  renderdoc / qrenderdoc modules
    v
RenderDoc replay engine
```

Two processes connected by TCP over loopback. The MCP server is spawned by the client as a subprocess (stdio transport). The extension runs inside RenderDoc's embedded Python interpreter and hosts a TCP server on localhost (port range 19876-19885).

## Tools

| Tool | Purpose |
|---|---|
| **Eval** | Execute Python in the RenderDoc replay session. Primary interface for all inspection, analysis, and debugging. |
| **Search API** | Search the RenderDoc Python API reference. Built by introspecting the live `renderdoc` module, so it always matches the running version. |
| **Instance** | List, connect to, or disconnect from running RenderDoc instances. Auto-connects on first use. |

## Pre-loaded Utilities

Available inside Eval code as functions, not separate tools.

- `inspect(obj)` - Introspect any RenderDoc object. Methods, properties, docstrings.
- `diff_state(eid_a, eid_b)` - Diff pipeline state between two events.
- `get_resource_name(resource_id)` - Look up human-readable resource names.
- `interpret_buffer(data, fmt)` - Decode raw buffer bytes into typed values.
- `summarize_data(values)` - Min/max/mean/NaN/Inf statistics over numeric data.
- `action_flags(flags)` - Decode ActionFlags bitmask to flag names.
- `goto_event(eid)` / `view_texture(id)` / `highlight_drawcall(eid)` - Navigate the RenderDoc UI.

## Setup

### Requirements

- Python 3.10+
- [RenderDoc](https://renderdoc.org/)

### 1. Install the RenderDoc Extension

Copy `src/extension/` into RenderDoc's extensions directory as `agentic_renderdoc`:

| Platform | Extensions directory |
|---|---|
| Windows | `%APPDATA%\qrenderdoc\extensions\` |
| Linux | `~/.local/share/qrenderdoc/extensions/` |
| macOS | `~/Library/Application Support/qrenderdoc/extensions/` |

Then in RenderDoc: **Tools > Manage Extensions**, enable `agentic-renderdoc`, and restart.

### 2. Install the MCP Server

```bash
python scripts/install.py
```

This builds a wheel and installs it via pip.

### 3. Configure Your MCP Client

Add this to your MCP client config:

```json
{
  "mcpServers": {
    "renderdoc": {
      "command": "agentic-renderdoc"
    }
  }
}
```

Configuration file locations:

| Client | File |
|---|---|
| Claude Code (project) | `.mcp.json` in your project root |
| Claude Code (user) | `~/.claude.json` |
| Claude Desktop (Windows) | `%APPDATA%\Claude\claude_desktop_config.json` |
| Claude Desktop (macOS) | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Claude Desktop (Linux) | `~/.config/Claude/claude_desktop_config.json` |

## Development

### Project Structure

Two halves:

- **`src/extension/`** - Runs inside RenderDoc's embedded Python. Hosts the TCP server, handles commands, serializes RenderDoc types.
- **`src/server/`** - The MCP server, spawned by the client. Defines the three tools and connects to the extension over TCP.
- **`scripts/`** - Install, test, and packaging utilities.

### Testing

`probe.py` connects directly to the extension over TCP, bypassing the MCP server:

```bash
python scripts/probe.py          # Run health checks
python scripts/probe.py reload   # Hot-reload extension modules
```

### Hot Reload

The `reload` command reloads handler modules without restarting RenderDoc. Changes to `__init__.py` still require a restart.

### Dev Extension Install

For active development on the extension, use a symlink instead of copying so edits are picked up immediately:

**Windows (PowerShell):**
```powershell
New-Item -ItemType Junction `
  -Path "$env:APPDATA\qrenderdoc\extensions\agentic_renderdoc" `
  -Target "<repo>\src\extension"
```

**Linux/macOS:**
```bash
ln -s <repo>/src/extension ~/.local/share/qrenderdoc/extensions/agentic_renderdoc
```

## Why This Design

V1 exposed 70 tools. In practice the agent ignored most of them, went straight to the Python eval handler, and spent ~6 iterations fumbling the RenderDoc API before becoming productive. Every session. The agent also frequently made mistakes and struggled with interpreting the raw data renderdoc spat out.

V2 reduces to three tools with rich descriptions. The tool description *is* the prompt engineering. It encodes the access model, object graph, cursor semantics, and working patterns so the agent writes correct RenderDoc Python on the first call.

This approach is backed by [Zhang et al., "One Tool Is Enough"](https://arxiv.org/abs/2512.20957): fewer, semantically grounded tools outperform large tool suites, even across model size gaps. We observed the same behavior while building this tool.

## How This Was Built

Both V1 and V2 of Agentic RenderDoc were written entirely by AI (Claude). My (Techgeek1) contribution was design, architecture decisions, testing against live captures, and iterative guidance. No code was written by hand.

The process for V2 looked roughly this:

1. **Design.** I wrote the design doc with AI assistance (see `docs/DESIGN.md`). This was informed by observing V1's failure modes in several real-world debugging use cases. V1 was written with a simliar process but far less experience building AI tooling.
2. **Implementation.** AI wrote all code in both versions. the MCP server, the RenderDoc extension, serialization, utilities, the bridge protocol. I reviewed and directed.
3. **Battle testing.** I ran the tool against real GPU captures from a bindless Vulkan renderer and fed back detailed results. Each round surfaced tool description inaccuracies, API misunderstandings, deadlocks, and usability friction.
4. **Iteration.** AI fixed issues, human tested again. Five rounds of this before the tool was reliably productive on first contact with a capture.

The biggest issues with creating this tool were not in the code but in the tool descriptions. A wrong type name, a missing accessor path, or an undocumented API gap would cause the agent to write plausible but incorrect code or to take longer than necessary to find the information it needed. The descriptions went through as many revision cycles as the implementation.

This project was partly an experiment in AI-driven development workflows, and partly a practical tool we wanted to exist. Both goals were met with flying colors and the tool itself is a significant improvement over manual inspection.

My hope is that by describing the process other developers can learn from my findings and learn to build similar or better tools for common workflows.
