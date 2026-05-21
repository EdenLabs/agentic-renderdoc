"""Embedded headless entry point — runs inside qrenderdoc's --script.

Used on Windows where ``renderdoc.pyd`` is not shipped as a standalone
Python module: the SWIG bindings are statically compiled into
``qrenderdoc.exe`` and only accessible from its embedded Python. We run
our headless protocol inside qrenderdoc's interpreter via ``--script``,
which runs before qrenderdoc opens its main UI: as long as this script
never returns, the UI never appears. On shutdown we call
``os._exit(0)`` so qrenderdoc skips its UI step.

Invoked via::

    qrenderdoc.exe --script <path-to-this-file>

Configuration (env vars; ``sys.argv`` is empty inside ``--script``):

    AGENTIC_EMBEDDED_CAPTURE     -- absolute path to the .rdc file.
    AGENTIC_EMBEDDED_PORT_MIN    -- start of the JSON-bridge port range.
    AGENTIC_EMBEDDED_PORT_MAX    -- end (inclusive) of that range.
    AGENTIC_EMBEDDED_PKG_PARENT  -- directory containing the
                                    ``extension`` package; prepended to
                                    sys.path so we can import from it.

Diagnostics are appended to
``%TEMP%/agentic-renderdoc-embedded-<port>.log`` because qrenderdoc as
a GUI subprocess produces no visible stdout/stderr. The port suffix
prevents collisions between concurrent workers writing to the same file.

This file is intentionally short. The actual replay context, bridge
server, and handlers live in the ``extension`` package — sharing all
the work that was already done to make those files 3.6-compatible.
"""
# NOTE: no `from __future__ import annotations` — qrenderdoc Python 3.6
# does not understand that import.

import os
import sys
import time
import traceback


_DEFAULT_PORT_MIN = 19876
_DEFAULT_PORT_MAX = 19885


def _log_path(port):
    base = os.environ.get("TEMP") or os.environ.get("TMP") or "."
    return os.path.join(base, "agentic-renderdoc-embedded-{}.log".format(port))


def _log(port, msg):
    """Append to the per-port diagnostic log. Best-effort."""
    try:
        with open(_log_path(port), "a", encoding="utf-8") as f:
            f.write("[{:.3f}] {}\n".format(time.time(), msg))
    except Exception:
        pass


def main():
    capture_path = os.environ.get("AGENTIC_EMBEDDED_CAPTURE", "")
    try:
        port_min = int(os.environ.get("AGENTIC_EMBEDDED_PORT_MIN", str(_DEFAULT_PORT_MIN)))
        port_max = int(os.environ.get("AGENTIC_EMBEDDED_PORT_MAX", str(_DEFAULT_PORT_MAX)))
    except ValueError:
        port_min, port_max = _DEFAULT_PORT_MIN, _DEFAULT_PORT_MAX

    # Diagnostic log path is port-suffixed; log to the configured range's
    # start until we have an actual bound port.
    log = lambda msg: _log(port_min, msg)
    log("=== embedded_headless start ===")
    log("python: " + sys.version)

    if not capture_path or not os.path.isfile(capture_path):
        log("AGENTIC_EMBEDDED_CAPTURE missing or not a file: " + repr(capture_path))
        os._exit(2)

    # Make the ``extension`` package importable. ``__file__`` isn't always
    # populated inside ``qrenderdoc --script``, so the spawning side
    # passes the package's parent dir via env var.
    pkg_parent = os.environ.get("AGENTIC_EMBEDDED_PKG_PARENT", "")
    if not pkg_parent:
        try:
            pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        except NameError:
            log("AGENTIC_EMBEDDED_PKG_PARENT unset and __file__ unavailable")
            os._exit(4)
    if pkg_parent and pkg_parent not in sys.path:
        sys.path.insert(0, pkg_parent)

    try:
        import renderdoc as rd
    except Exception:
        log("renderdoc import failed:\n" + traceback.format_exc())
        os._exit(3)

    try:
        rd.InitialiseReplay(rd.GlobalEnvironment(), [])
    except Exception:
        # Already initialised inside qrenderdoc — non-fatal.
        log("InitialiseReplay warning (continuing)")

    from extension.context import EmbeddedHeadlessContext
    from extension.bridge  import BridgeServer
    import extension.handlers  # noqa: F401 — registers handlers in module-level HANDLERS dict

    ctx    = None
    bridge = None

    try:
        ctx = EmbeddedHeadlessContext(capture_path=capture_path)
        log("capture opened: " + capture_path)
        ctx.on_capture_loaded()

        # force_threaded=True: we cannot drive qrenderdoc's Qt event loop
        # because that loop is what would open the UI. The threaded
        # backend uses winsock + threads, no Qt dependency.
        bridge = BridgeServer(
            ctx,
            port_range     = range(port_min, port_max + 1),
            force_threaded = True,
        )
        bridge.start()
        if bridge.port is None:
            log("bridge failed to bind in range {}-{}".format(port_min, port_max))
            os._exit(6)

        # Re-target the log to the actual bound port.
        log = lambda msg, _p=bridge.port: _log(_p, msg)
        log("bridge bound on localhost:{}".format(bridge.port))

        ctx._server_port = bridge.port
        ctx._bridge      = bridge

        # Block until the bridge stops. The shutdown handler runs
        # bridge.stop() on a background thread; we poll the threaded
        # backend's _running flag here. Sleep is fine — only fires
        # while waiting for shutdown.
        impl = getattr(bridge, "_impl", None)
        while True:
            running = getattr(impl, "_running", None) if impl is not None else None
            if not running:
                break
            time.sleep(0.5)
        log("bridge stopped; exiting")
    except Exception:
        log("startup or run failed:\n" + traceback.format_exc())
    finally:
        if bridge is not None:
            try:
                bridge.stop()
            except Exception:
                pass
        if ctx is not None:
            try:
                ctx.shutdown()
            except Exception:
                pass
        try:
            rd.ShutdownReplay()
        except Exception:
            pass

    # Bypass qrenderdoc's main() — never reach the UI launch step.
    # sys.exit() would raise SystemExit which qrenderdoc may catch; the
    # underscored variant terminates the whole process.
    os._exit(0)


main()
