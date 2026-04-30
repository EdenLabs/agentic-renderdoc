"""Agentic RenderDoc extension entry point.

Registers the CaptureViewer and starts the TCP bridge server.
RenderDoc calls register() on load and unregister() on shutdown.
"""
from __future__ import annotations

from typing import Any

# qrenderdoc / renderdoc are only available when this package is loaded
# inside the RenderDoc GUI. The headless worker entry point imports
# this package's submodules (context, bridge, handlers, ...) without
# needing the GUI bindings, so those imports must stay at function
# scope rather than module scope.

from .bridge  import BridgeServer
from .context import GuiHandlerContext, HandlerContext


# --- Module state ---

_extension : Any           = None
_server    : BridgeServer | None = None


def register(version: str, ctx: Any) -> None:
    """Called by RenderDoc when the extension is loaded.

    Sets up the handler context, registers the CaptureViewer, and
    starts the TCP bridge server.
    """
    global _extension, _server

    print(f"[Agentic] Registering (RenderDoc {version})")

    import qrenderdoc as qrd

    class AgenticExtension(qrd.CaptureViewer):
        """CaptureViewer that forwards capture open/close events to the context."""

        def __init__(self, ctx: Any, handler_ctx: HandlerContext) -> None:
            super().__init__()
            self._ctx         = ctx
            self._handler_ctx = handler_ctx

        def OnCaptureLoaded(self) -> None:
            self._handler_ctx.on_capture_loaded()
            print("[Agentic] Capture loaded")

        def OnCaptureClosed(self) -> None:
            self._handler_ctx.on_capture_closed()
            print("[Agentic] Capture closed")

        def OnSelectedEventChanged(self, event: int) -> None:
            pass

        def OnEventChanged(self, event: int) -> None:
            pass

    handler_ctx = GuiHandlerContext(ctx)

    _extension = AgenticExtension(ctx, handler_ctx)
    ctx.AddCaptureViewer(_extension)

    _server = BridgeServer(handler_ctx)
    _server.start()

    if _server.port is not None:
        handler_ctx._server_port = _server.port

    handler_ctx._bridge = _server


def unregister() -> None:
    """Called by RenderDoc when the extension is unloaded."""
    global _extension, _server

    print("[Agentic] Unregistering")

    if _server is not None:
        _server.stop()
        _server = None

    _extension = None
