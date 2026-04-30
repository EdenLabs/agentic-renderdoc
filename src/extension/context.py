"""Handler context shared with every handler invocation.

The context owns capture lifecycle state and the path to the replay
controller. Two variants exist:

  GuiHandlerContext      -- runs inside qrenderdoc; replay() defers to
                            the host's replay thread via BlockInvoke,
                            and invoke_ui() schedules onto the Qt UI
                            thread via MiniQtHelper.
  HeadlessHandlerContext -- runs in a standalone worker process that
                            opened a .rdc file directly. Owns the
                            ReplayController and a dedicated replay
                            thread fed by a queue. invoke_ui() is a
                            no-op (no Qt host).

Both expose the same surface so handlers and bind_utilities() are
agnostic to which environment they're in.
"""
from __future__ import annotations

import concurrent.futures
import queue
import threading
from collections.abc import Callable
from typing          import Any

from .api_index import build_index


class _TrackedController:
    """Thin proxy around a ReplayController that tracks API call ordering.

    Delegates all attribute access to the wrapped controller. Watches for
    SetFrameEvent and GetPipelineState to detect the common mistake of
    querying pipeline state without first selecting an event.
    """

    def __init__(self, controller: Any, warnings: list[str]) -> None:
        self._controller         = controller
        self._warnings           = warnings
        self._set_frame_called   = False
        self.__wrapped__         = controller

    def SetFrameEvent(self, *args: Any, **kwargs: Any) -> Any:
        self._set_frame_called = True
        return self._controller.SetFrameEvent(*args, **kwargs)

    def GetPipelineState(self, *args: Any, **kwargs: Any) -> Any:
        if not self._set_frame_called:
            self._warnings.append(
                "GetPipelineState() called without a prior "
                "SetFrameEvent() in this replay callback. "
                "The returned state may be stale or empty."
            )
        return self._controller.GetPipelineState(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._controller, name)

    def __dir__(self) -> list[str]:
        names = set(dir(self._controller))
        names.update(super().__dir__())
        return sorted(names)


class HandlerContext:
    """Base class for handler contexts. Holds shared capture state.

    Subclasses provide the implementation of replay(), invoke_ui(),
    structured_file, and on_capture_loaded(). Handlers and utilities
    should depend only on this base interface.
    """

    # Sentinel marking a context as headless; used by handlers and
    # utilities to short-circuit UI-only operations.
    headless: bool = False

    def __init__(self) -> None:
        self._server_port       : int         = 0
        self._capture_loaded    : bool        = False
        self._api_type          : Any         = None
        self._capture_path      : str | None  = None
        self._event_count       : int         = 0
        self._api_index         : dict | None = None
        self._replay_controller : Any         = None
        self._replay_warnings   : list[str]   = []
        self._bridge            : Any         = None

    # --- Public properties ---

    @property
    def capture_loaded(self) -> bool:
        return self._capture_loaded

    @property
    def api_index(self) -> dict | None:
        return self._api_index

    @property
    def structured_file(self) -> Any:
        raise NotImplementedError

    # --- Lifecycle ---

    def on_capture_loaded(self) -> None:
        raise NotImplementedError

    def on_capture_closed(self) -> None:
        self._capture_loaded = False
        self._api_type       = None
        self._capture_path   = None
        self._event_count    = 0

    # --- Replay / UI dispatch ---

    def replay(self, callback: Callable[[Any], Any]) -> Any:
        raise NotImplementedError

    def invoke_ui(self, callback: Callable[[], None]) -> None:
        raise NotImplementedError


class GuiHandlerContext(HandlerContext):
    """Context for the qrenderdoc-hosted extension.

    Wraps the host's CaptureContext. replay() delegates to
    ctx.Replay().BlockInvoke(); invoke_ui() schedules onto the Qt UI
    thread via MiniQtHelper.
    """

    def __init__(self, ctx: Any) -> None:
        super().__init__()
        self.ctx = ctx

    @property
    def structured_file(self) -> Any:
        return self.ctx.GetStructuredFile()

    def on_capture_loaded(self) -> None:
        self._capture_loaded = True

        try:
            self._api_type     = self.ctx.APIProps().pipelineType
            self._capture_path = self.ctx.GetCaptureFilename()
            self._event_count  = self.ctx.GetLastAction().eventId + 1
        except Exception:
            pass

        if self._api_index is None:
            self._api_index = build_index()

    def replay(self, callback: Callable[[Any], Any]) -> Any:
        if not self._capture_loaded:
            raise RuntimeError("no capture loaded")

        self._replay_warnings = []

        # Re-entrant: utilities may call replay() from within a callback.
        # Reuse the active tracked controller to avoid deadlock.
        if self._replay_controller is not None:
            return callback(self._replay_controller)

        result    = [None]
        exception = [None]

        def wrapper(controller):
            tracked = _TrackedController(controller, self._replay_warnings)
            self._replay_controller = tracked
            try:
                result[0] = callback(tracked)
            except Exception as e:
                exception[0] = e
            finally:
                self._replay_controller = None

        self.ctx.Replay().BlockInvoke(wrapper)

        if exception[0]:
            raise exception[0]
        return result[0]

    def invoke_ui(self, callback: Callable[[], None]) -> None:
        exception = [None]
        done      = threading.Event()

        def wrapper():
            try:
                callback()
            except Exception as e:
                exception[0] = e
            finally:
                done.set()

        helper = self.ctx.Extensions().GetMiniQtHelper()
        helper.InvokeOntoUIThread(wrapper)
        done.wait(timeout=5.0)

        if exception[0]:
            raise exception[0]


class HeadlessHandlerContext(HandlerContext):
    """Context for a standalone worker backed by a renderdoccmd remoteserver.

    The replay thread owns the entire renderdoc lifecycle:
    ``CreateRemoteServerConnection``, ``OpenCapture``, every replay
    callback, ``CloseCapture``, and ``ShutdownServerAndConnection`` all
    run on this one thread. This is required because the local proxy
    that ``OpenCapture`` returns is bound to the thread that opened it —
    using it from any other thread leaves the proxy in a partially
    initialised state where calls "succeed" but state queries silently
    return zeros and the underlying TCP connection to renderdoccmd
    drops to RemoteServerConnectionLost.

    Construction blocks the calling thread until the replay thread
    has finished its setup phase (connection + OpenCapture). If setup
    fails, the constructor raises and the thread exits cleanly.

    remote_port  -- TCP port where renderdoccmd remoteserver is listening.
    capture_path -- Absolute path to the .rdc file to open on the remote.
    """

    headless = True

    def __init__(self, remote_port: int, capture_path: str) -> None:
        super().__init__()
        self._remote_port    = remote_port
        self._capture_path   = capture_path
        self._remote_server  : Any = None  # populated by replay thread
        self._controller_raw : Any = None  # populated by replay thread
        self._structured_file: Any = None

        self._queue: "queue.Queue[Any]" = queue.Queue()
        self._stop = threading.Event()
        # Setup completion signal. Set to True on success, set with an
        # exception on failure. Constructor blocks on it.
        self._setup_done: concurrent.futures.Future[bool] = concurrent.futures.Future()

        self._replay_thread = threading.Thread(
            target = self._replay_loop,
            name   = "agentic-replay",
            daemon = True,
        )
        self._replay_thread.start()

        # Block the constructor until setup has run on the replay thread.
        # Re-raises any setup exception in the caller's context.
        self._setup_done.result()

        # Heartbeat thread keeps the renderdoccmd remoteserver connection
        # alive. The server enforces an idle timeout (default 5s); if we
        # don't talk to it within that window the socket gets dropped and
        # the next replay call hits EBADF. The replay thread can't ping
        # itself when it's busy executing a callback, so heartbeat lives
        # on its own thread. Ping() is thread-safe with respect to other
        # operations on the same RemoteServer.
        self._heartbeat_thread = threading.Thread(
            target = self._heartbeat_loop,
            name   = "agentic-heartbeat",
            daemon = True,
        )
        self._heartbeat_thread.start()

    @property
    def structured_file(self) -> Any:
        return self._structured_file

    def on_capture_loaded(self) -> None:
        """Populate capture metadata from the controller and build the API index.

        Runs a single replay() call to read API properties and the action
        tree (for event count and structured file). Builds the API index
        once.
        """
        self._capture_loaded = True

        def _populate(controller: Any) -> tuple[Any, int, Any]:
            try:
                api_type = controller.GetAPIProperties().pipelineType
            except Exception:
                api_type = None

            try:
                actions = controller.GetRootActions()
                last_eid = _last_event_id(actions)
            except Exception:
                last_eid = -1

            try:
                sdfile = controller.GetStructuredFile()
            except Exception:
                sdfile = None

            return (api_type, last_eid + 1 if last_eid >= 0 else 0, sdfile)

        api_type, event_count, sdfile = self.replay(_populate)
        self._api_type        = api_type
        self._event_count     = event_count
        self._structured_file = sdfile

        if self._api_index is None:
            self._api_index = build_index()

    def on_capture_closed(self) -> None:
        super().on_capture_closed()
        self._structured_file = None

    def replay(self, callback: Callable[[Any], Any]) -> Any:
        if self._stop.is_set():
            raise RuntimeError("worker is shutting down")

        self._replay_warnings = []

        if self._replay_controller is not None:
            return callback(self._replay_controller)

        future: concurrent.futures.Future[Any] = concurrent.futures.Future()

        def worker(controller: Any) -> Any:
            tracked = _TrackedController(controller, self._replay_warnings)
            self._replay_controller = tracked
            try:
                return callback(tracked)
            finally:
                self._replay_controller = None

        self._queue.put((worker, future))
        return future.result()

    def invoke_ui(self, callback: Callable[[], None]) -> None:
        raise RuntimeError("UI is not available in headless mode")

    # --- Shutdown ---

    def shutdown(self) -> None:
        """Signal the replay thread to exit and reap it.

        Idempotent. Safe to call from any thread except the replay
        thread itself. Capture/connection teardown happens on the
        replay thread after the work loop drains; we don't touch
        ``_controller_raw`` or ``_remote_server`` here because they
        belong to that thread.
        """
        if self._stop.is_set():
            return

        # Order matters: set the stop flag BEFORE draining so any
        # concurrent replay() callers see the flag and raise instead of
        # putting more work onto the abandoned queue.
        self._stop.set()
        # Sentinel to unblock the replay loop's queue.get() promptly.
        self._queue.put(None)

        # Heartbeat thread wakes on _stop; just join it.
        if getattr(self, "_heartbeat_thread", None) is not None:
            if self._heartbeat_thread.is_alive():
                self._heartbeat_thread.join(timeout=5.0)

        if self._replay_thread.is_alive():
            self._replay_thread.join(timeout=15.0)

        # Drain anything queued before stop was set: the replay loop
        # exited without processing them, so their callers are still
        # blocked on future.result(). Cancel each so callers raise.
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is None:
                continue
            _callback, future = item
            if not future.done():
                future.set_exception(
                    RuntimeError("worker shut down before request completed")
                )

        self._capture_loaded = False

    def _heartbeat_loop(self) -> None:
        """Periodically Ping() the remote server to keep the connection alive.

        Runs on its own thread so it works even when the replay thread
        is busy. ~2s interval, well under the 5s default idle timeout.
        Stops when _stop is set or the connection drops irrecoverably.
        """
        import time
        while not self._stop.wait(2.0):
            server = self._remote_server
            if server is None:
                # Setup didn't complete or teardown already happened.
                return
            try:
                server.Ping()
            except Exception:
                # Connection is gone; future replay calls will surface
                # the failure. Don't spin retrying here.
                return

    # --- Internal: replay thread ---

    def _replay_loop(self) -> None:
        """Run the full renderdoc lifecycle on this thread.

        Phase 1 (setup): connect to renderdoccmd remoteserver and open
        the capture. Signal completion (or failure) via _setup_done.
        Phase 2 (work): drain the queue until stop is requested.
        Phase 3 (teardown): close capture, shut down the remote
        connection. All on this single thread — required for proxy
        thread-affinity correctness.
        """
        # --- Phase 1: setup ---
        import renderdoc as rd

        try:
            status, server = rd.CreateRemoteServerConnection(
                f"localhost:{self._remote_port}"
            )
            if status.code != rd.ResultCode.Succeeded:
                raise RuntimeError(
                    f"CreateRemoteServerConnection failed: {status.code}"
                )

            status, controller = server.OpenCapture(
                rd.RemoteServer.NoPreference,
                self._capture_path,
                rd.ReplayOptions(),
                None,
            )
            if status.code != rd.ResultCode.Succeeded:
                try:
                    server.ShutdownServerAndConnection()
                except Exception:
                    pass
                raise RuntimeError(f"remote OpenCapture failed: {status.code}")

            self._remote_server  = server
            self._controller_raw = controller
            self._setup_done.set_result(True)
        except BaseException as e:
            # Setup failed. Surface the exception to the constructor,
            # exit the thread.
            if not self._setup_done.done():
                self._setup_done.set_exception(e)
            return

        # --- Phase 2: work ---
        # The keep-alive heartbeat runs on a SEPARATE thread (started
        # in __init__) so it can ping the remoteserver even when this
        # thread is busy executing a long callback.
        try:
            while not self._stop.is_set():
                try:
                    item = self._queue.get(timeout=0.5)
                except queue.Empty:
                    continue

                if item is None:
                    break

                callback, future = item

                if future.set_running_or_notify_cancel():
                    try:
                        result = callback(self._controller_raw)
                        future.set_result(result)
                    except BaseException as e:
                        future.set_exception(e)
        finally:
            # --- Phase 3: teardown (on the same thread that opened them) ---
            # Per RenderDoc docs, a remote-proxy controller must be closed
            # via server.CloseCapture(controller), NOT controller.Shutdown().
            try:
                if self._remote_server is not None and self._controller_raw is not None:
                    self._remote_server.CloseCapture(self._controller_raw)
            except Exception:
                pass
            self._controller_raw = None

            try:
                if self._remote_server is not None:
                    self._remote_server.ShutdownServerAndConnection()
            except Exception:
                pass
            self._remote_server = None


def _last_event_id(actions: list) -> int:
    """Return the largest eventId in an action tree, or -1 if empty.

    Walks .children recursively. Used for event count discovery in
    headless mode where there is no GetLastAction() helper on the
    controller.
    """
    best = -1
    for a in actions:
        if a.eventId > best:
            best = a.eventId
        sub = _last_event_id(a.children)
        if sub > best:
            best = sub
    return best
