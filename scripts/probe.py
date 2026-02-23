"""Direct TCP probe for the RenderDoc bridge extension.

Connects to the bridge server and sends commands without needing
the MCP server. Useful for testing the extension in isolation.

Usage:
    python scripts/probe.py                    # auto-discover and run all checks
    python scripts/probe.py eval "1 + 1"       # run a single eval
    python scripts/probe.py api_index "SetFrameEvent"
    python scripts/probe.py instance_info
    python scripts/probe.py reload          # hot-reload extension modules
"""

import json
import socket
import sys

PORT_RANGE      = range(19876, 19886)
PROBE_TIMEOUT   = 0.3
CONNECT_TIMEOUT = 2.0
READ_TIMEOUT    = 30.0


def discover():
    """Find the first listening bridge server port."""
    for port in PORT_RANGE:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(PROBE_TIMEOUT)
            s.connect(("127.0.0.1", port))
            s.close()
            return port
        except (ConnectionRefusedError, TimeoutError, OSError):
            continue
    return None


def send(port, cmd, params=None):
    """Send a JSON-lines command and return the parsed response."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(CONNECT_TIMEOUT)
    s.connect(("127.0.0.1", port))

    request = json.dumps({"cmd": cmd, "params": params or {}}) + "\n"
    s.sendall(request.encode("utf-8"))

    s.settimeout(READ_TIMEOUT)
    buf = ""
    while "\n" not in buf:
        chunk = s.recv(65536)
        if not chunk:
            break
        buf += chunk.decode("utf-8")

    s.close()
    return json.loads(buf.split("\n", 1)[0])


def run_checks(port):
    """Run a basic smoke test against all three handlers."""
    print(f"Connected to bridge on port {port}\n")

    # instance_info
    print("--- instance_info ---")
    resp = send(port, "instance_info")
    print(json.dumps(resp, indent=2))
    print()

    # eval: simple math
    print("--- eval: 1 + 1 ---")
    resp = send(port, "eval", {"code": "1 + 1"})
    print(json.dumps(resp, indent=2))
    print()

    # eval: list draw calls
    print("--- eval: list root actions ---")
    code = """\
results = []
def work(controller):
    for a in controller.GetRootActions():
        results.append({"eventId": a.eventId, "name": a.customName or str(a.eventId)})
pyrenderdoc.Replay().BlockInvoke(work)
results[:10]
"""
    resp = send(port, "eval", {"code": code})
    print(json.dumps(resp, indent=2))
    print()

    # api_index
    print("--- api_index: SetFrameEvent ---")
    resp = send(port, "api_index", {"query": "SetFrameEvent"})
    data = resp.get("data", [])
    for entry in data[:3]:
        print(f"  {entry['kind']:12s} {entry['name']}")
        if entry.get("signature"):
            print(f"               sig: {entry['signature']}")
    print()

    # eval: inspect utility
    print("--- eval: inspect(rd.ShaderStage) ---")
    resp = send(port, "eval", {"code": "inspect(rd.ShaderStage)"})
    print(json.dumps(resp, indent=2))
    print()

    # eval: error formatting
    print("--- eval: trigger NameError ---")
    resp = send(port, "eval", {"code": "nonexistent_var"})
    err = resp.get("error", {})
    if isinstance(err, dict):
        print(f"  failing_line: {err.get('failing_line')}")
        print(f"  hints: {err.get('hints')}")
    else:
        print(f"  {err}")
    print()

    print("All checks complete.")


def main():
    port = discover()
    if port is None:
        print("No bridge server found. Is the extension loaded in RenderDoc?")
        sys.exit(1)

    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "eval" and len(sys.argv) > 2:
            resp = send(port, "eval", {"code": sys.argv[2]})
        elif cmd == "api_index" and len(sys.argv) > 2:
            resp = send(port, "api_index", {"query": sys.argv[2]})
        elif cmd == "instance_info":
            resp = send(port, "instance_info")
        elif cmd == "reload":
            resp = send(port, "reload")
        else:
            print(f"Usage: {sys.argv[0]} [eval <code> | api_index <query> | instance_info | reload]")
            sys.exit(1)
        print(json.dumps(resp, indent=2))
    else:
        run_checks(port)


if __name__ == "__main__":
    main()
