"""Headless RenderDoc replay worker.

Spawns ``renderdoccmd remoteserver`` as a child process, connects to it
via ``rd.CreateRemoteServerConnection``, opens the requested capture,
and serves the same JSON-lines bridge protocol as the GUI extension on
a port in the agentic range.

Architecture rationale: in-process ``ICaptureFile.OpenCapture`` from
the SWIG Python module crashes deep inside Vulkan replay
initialization on this stack. ``renderdoccmd`` is the maintained,
tested code path RenderDoc itself uses for headless replay. We pay a
subprocess hop in exchange for stability and stronger crash isolation
(a wedged replay only kills ``renderdoccmd``, not the Python worker).

Usage:
    python -m extension.headless <path-to-capture.rdc> [options]

Options:
    --port-min P / --port-max Q
        Agentic JSON-bridge port range. Defaults match the BridgeServer
        scan range (19876-19885) so the MCP server discovers headless
        workers alongside live RenderDoc GUIs.
    --remote-port-min P / --remote-port-max Q
        renderdoccmd remoteserver port range. Defaults 39920-39929.
    --renderdoccmd PATH
        Override the renderdoccmd executable path. Defaults to
        whichever ``renderdoccmd`` is found on PATH.
"""
from __future__ import annotations

import argparse
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import renderdoc_locate
from .bridge  import BridgeServer
from .context import HeadlessHandlerContext


_DEFAULT_PORT_MIN        = 19876
_DEFAULT_PORT_MAX        = 19885
_DEFAULT_REMOTE_PORT_MIN = 39920
_DEFAULT_REMOTE_PORT_MAX = 39929


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog        = "agentic-renderdoc-headless",
        description = "Headless RenderDoc replay worker (renderdoccmd-backed).",
    )
    parser.add_argument("capture", help="Path to a .rdc capture file.")
    parser.add_argument("--port-min", type=int, default=_DEFAULT_PORT_MIN)
    parser.add_argument("--port-max", type=int, default=_DEFAULT_PORT_MAX)
    parser.add_argument("--remote-port-min", type=int, default=_DEFAULT_REMOTE_PORT_MIN)
    parser.add_argument("--remote-port-max", type=int, default=_DEFAULT_REMOTE_PORT_MAX)
    parser.add_argument("--renderdoccmd", default=None)
    args = parser.parse_args(argv)

    capture_path = Path(args.capture).resolve()
    if not capture_path.exists():
        print(f"[Agentic-Headless] capture not found: {capture_path}", file=sys.stderr)
        return 2

    renderdoccmd = args.renderdoccmd or shutil.which("renderdoccmd")
    if not renderdoccmd:
        print(
            "[Agentic-Headless] renderdoccmd not found on PATH; install "
            "RenderDoc or pass --renderdoccmd",
            file=sys.stderr,
        )
        return 7

    # Phase 1: locate and load the RenderDoc Python module.
    try:
        info = renderdoc_locate.setup()
    except renderdoc_locate.RenderDocLocateError as e:
        print(f"[Agentic-Headless] {e}", file=sys.stderr)
        return 3

    print(f"[Agentic-Headless] using renderdoc from {info['python_dir']}")

    import renderdoc as rd

    rd.InitialiseReplay(rd.GlobalEnvironment(), [])

    # Phase 2: spawn renderdoccmd remoteserver. The connection itself is
    # NOT opened here — HeadlessHandlerContext's replay thread does that
    # (and OpenCapture, and all subsequent calls), to keep the proxy
    # controller's thread affinity intact.
    proc        = None
    handler_ctx = None
    server      = None

    try:
        proc, remote_port = _spawn_remoteserver(
            renderdoccmd,
            range(args.remote_port_min, args.remote_port_max + 1),
        )
        print(f"[Agentic-Headless] renderdoccmd PID {proc.pid} on localhost:{remote_port}")

        # Phase 3: build the handler context. Constructor blocks until
        # the dedicated replay thread has connected to the remoteserver
        # and opened the capture. On failure, the constructor re-raises.
        handler_ctx = HeadlessHandlerContext(
            remote_port  = remote_port,
            capture_path = str(capture_path),
        )
        print(f"[Agentic-Headless] capture opened: {capture_path}")

        handler_ctx.on_capture_loaded()

        server = BridgeServer(
            handler_ctx,
            port_range     = range(args.port_min, args.port_max + 1),
            force_threaded = True,
        )
        server.start()

        if server.port is None:
            print("[Agentic-Headless] no port available in range", file=sys.stderr)
            return 6

        handler_ctx._server_port = server.port
        handler_ctx._bridge      = server

        print(
            f"[Agentic-Headless] serving capture on localhost:{server.port} "
            f"(renderdoccmd remote on {remote_port}, pid={proc.pid})"
        )

        # Phase 4: wait for shutdown.
        return _serve_until_done(server, proc)
    except Exception as e:
        print(f"[Agentic-Headless] startup failed: {e}", file=sys.stderr)
        return 5
    finally:
        if server is not None:
            try:
                server.stop()
            except Exception:
                pass
        # The replay thread closes the capture and remote connection
        # on its own thread inside its teardown phase — we just signal it.
        if handler_ctx is not None:
            try:
                handler_ctx.shutdown()
            except Exception:
                pass
        # Reap the renderdoccmd subprocess.
        if proc is not None:
            _reap(proc)
        try:
            rd.ShutdownReplay()
        except Exception:
            pass


# --- Subprocess management ---

def _die_with_parent() -> None:
    """preexec_fn: ask the kernel to SIGKILL us when our parent dies.

    Linux-only via prctl(PR_SET_PDEATHSIG, SIGKILL). Survives parent
    SIGKILL — Python finally blocks would not. Without this, a
    force-close on the worker orphans its renderdoccmd child.
    """
    import ctypes
    PR_SET_PDEATHSIG = 1
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)
    except Exception:
        # If prctl is unavailable we just lose the safety net;
        # the regular finally-block reap still runs in normal exit.
        pass


def _spawn_remoteserver(renderdoccmd: str, port_range: range) -> tuple[subprocess.Popen, int]:
    """Spawn renderdoccmd remoteserver on the first free port in range.

    Returns (Popen, port). Raises RuntimeError if every port is busy or
    the process fails to start within a short window.

    The child is configured (Linux only) so the kernel SIGKILLs it when
    this Python worker dies. Without this, a SIGKILL'd worker (force-close
    path) orphans its renderdoccmd child, leaking ports.

    Implicit Vulkan layers (RenderDoc's capture layer in particular)
    are disabled via env. They would otherwise auto-load on top of
    renderdoccmd's own librenderdoc.so, double-loading the library and
    causing the proxy connection to drop with EBADF.
    """
    env = os.environ.copy()
    env["VK_LOADER_LAYERS_DISABLE"]             = "*"
    env["DISABLE_VK_LAYER_RENDERDOC_Capture_1"] = "1"

    last_err: Exception | None = None
    for port in port_range:
        # Pre-flight: check the port is locally bindable. renderdoccmd
        # would otherwise refuse silently or print to its stderr.
        if not _port_free(port):
            continue

        proc = subprocess.Popen(
            [renderdoccmd, "remoteserver", "-h", "127.0.0.1", "-p", str(port)],
            stdin      = subprocess.DEVNULL,
            stdout     = subprocess.DEVNULL,
            stderr     = subprocess.PIPE,
            env        = env,
            preexec_fn = _die_with_parent if sys.platform.startswith("linux") else None,
        )

        # Wait for the port to come up or the process to die.
        if _wait_port_listening(port, timeout=5.0, proc=proc):
            return (proc, port)

        # Process died or never bound; try next port.
        try:
            err = (proc.stderr.read() if proc.stderr else b"").decode("utf-8", errors="replace")
        except Exception:
            err = "<no stderr>"
        last_err = RuntimeError(
            f"renderdoccmd failed to bind on {port}: {err.strip() or 'no output'}"
        )
        _reap(proc)

    raise RuntimeError(
        f"could not start renderdoccmd remoteserver in range "
        f"{port_range[0]}-{port_range[-1]}: {last_err}"
    )


def _port_free(port: int) -> bool:
    """True if the TCP port is locally free for binding."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("127.0.0.1", port))
    except OSError:
        return False
    finally:
        s.close()
    return True


def _wait_port_listening(port: int, timeout: float, proc: subprocess.Popen) -> bool:
    """Block until something is listening on port, the proc dies, or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False

        s = socket.socket()
        s.settimeout(0.2)
        try:
            s.connect(("127.0.0.1", port))
            s.close()
            return True
        except OSError:
            pass
        finally:
            try:
                s.close()
            except OSError:
                pass
        time.sleep(0.1)
    return False


def _reap(proc: subprocess.Popen) -> None:
    """Terminate a subprocess politely, escalate to SIGKILL on grace failure."""
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
    except OSError:
        pass
    try:
        proc.wait(timeout=3.0)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            pass


# --- Run loop ---

def _serve_until_done(server: BridgeServer, proc: subprocess.Popen) -> int:
    """Wait for shutdown signal, bridge stop, or renderdoccmd exit."""
    stop_event = threading.Event()

    def _on_signal(signum, _frame):
        print(f"[Agentic-Headless] caught signal {signum}; shutting down")
        stop_event.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT,  _on_signal)

    while not stop_event.is_set():
        if proc.poll() is not None:
            print(
                f"[Agentic-Headless] renderdoccmd exited (rc={proc.returncode})",
                file=sys.stderr,
            )
            return 8
        if not _bridge_alive(server):
            print("[Agentic-Headless] bridge stopped; exiting")
            return 0
        time.sleep(0.5)

    return 0


def _bridge_alive(server: BridgeServer) -> bool:
    impl = getattr(server, "_impl", None)
    if impl is None:
        return False

    running = getattr(impl, "_running", None)
    if running is False:
        return False

    thread = getattr(impl, "_thread", None)
    if thread is not None and not thread.is_alive():
        return False

    return True


if __name__ == "__main__":
    sys.exit(main())
