"""TCP bridge server that runs inside RenderDoc's Python environment.

Accepts JSON-lines requests over TCP and dispatches to handlers.

Two implementations share a common dispatch:

  _QtBridge       -- Event-driven QTcpServer. All Python execution stays
                     on the UI thread (matching qrenderdoc's console
                     pattern), so there is no second Python thread
                     allocating objects concurrently with the replay
                     thread during BlockInvoke. Preferred path.

  _ThreadedBridge -- Socket + threads fallback. Has a known Python 3.14
                     freelist race against the replay thread (see
                     Python/generated_cases.c.h tuple_alloc). Used only
                     when no Qt bindings are importable.
"""
from __future__ import annotations

import _thread
import json
import threading
import traceback
from typing import Any

from .handlers import HANDLERS
from .          import winsock

BUFFER_SIZE = 65536


# --- Qt bindings discovery ---

def _try_import_qt():
    """Import the first available Qt-Network binding.

    Returns (QTcpServer, QTcpSocket, QHostAddress) or None.
    """
    for mod in ("PyQt6", "PySide6", "PySide2", "PyQt5"):
        try:
            net = __import__(f"{mod}.QtNetwork", fromlist=["QTcpServer"])
            return (net.QTcpServer, net.QTcpSocket, net.QHostAddress)
        except ImportError:
            continue
    return None


_QT = _try_import_qt()


# --- Shared dispatch ---

def _dispatch(ctx: Any, request: dict[str, Any]) -> dict[str, Any]:
    """Run a single request's handler.

    Caller is responsible for serializing dispatches -- the replay API
    is single-threaded.
    """
    cmd    = request.get("cmd", "")
    params = request.get("params", {})

    handler_entry = HANDLERS.get(cmd)
    if handler_entry is None:
        return {"ok": False, "error": f"unknown command: {cmd}"}

    try:
        return handler_entry["func"](ctx, params)
    except Exception as e:
        traceback.print_exc()
        return {"ok": False, "error": str(e)}


# --- Qt event-driven bridge ---

class _QtBridge:
    """TCP server driven by Qt's event loop on the UI thread.

    newConnection/readyRead/disconnected signals all fire on the UI
    thread. Handlers run inline on those signals; BlockInvoke hops to
    the replay thread exactly the way the Python console does.

    No Python threads are created by this class.
    """

    def __init__(self, ctx: Any, port_range: range) -> None:
        self._ctx        = ctx
        self._port_range = port_range
        self._port       : int | None         = None
        self._server     : Any                = None
        self._buffers    : dict[Any, bytearray] = {}

    @property
    def port(self) -> int | None:
        return self._port

    def start(self) -> None:
        """Bind to the first available port and start listening."""
        QTcpServer, _QTcpSocket, QHostAddress = _QT
        server = QTcpServer()

        for port in self._port_range:
            if server.listen(QHostAddress("127.0.0.1"), port):
                self._port   = port
                self._server = server
                server.newConnection.connect(self._on_new_connection)
                print(f"[Agentic] Listening on localhost:{port} (Qt bridge)")
                return

        start = self._port_range[0]
        end   = self._port_range[-1]
        print(f"[Agentic] Failed to start: all ports {start}-{end} in use")

    def stop(self) -> None:
        """Shut down the server and drop all connections."""
        if self._server is not None:
            self._server.close()
            self._server = None

        for sock in list(self._buffers.keys()):
            try:
                sock.disconnectFromHost()
            except Exception:
                pass
        self._buffers.clear()
        print("[Agentic] Server stopped")

    def _on_new_connection(self) -> None:
        """Accept every pending connection and wire up its slots."""
        while self._server and self._server.hasPendingConnections():
            sock = self._server.nextPendingConnection()
            self._buffers[sock] = bytearray()
            sock.readyRead.connect(lambda s=sock: self._on_ready_read(s))
            sock.disconnected.connect(lambda s=sock: self._on_disconnected(s))
            print(f"[Agentic] Connection accepted ({len(self._buffers)} active)")

    def _on_ready_read(self, sock: Any) -> None:
        """Drain newly-arrived bytes and dispatch every complete line."""
        buf = self._buffers.get(sock)
        if buf is None:
            return

        buf += bytes(sock.readAll())

        while True:
            nl = buf.find(b"\n")
            if nl < 0:
                break

            line = bytes(buf[:nl])
            del buf[:nl + 1]

            try:
                request = json.loads(line.decode("utf-8"))
            except Exception as e:
                traceback.print_exc()
                response = {"ok": False, "error": f"invalid request: {e}"}
            else:
                response = _dispatch(self._ctx, request)

            out = (json.dumps(response, separators=(",", ":")) + "\n").encode("utf-8")
            sock.write(out)
            sock.flush()

    def _on_disconnected(self, sock: Any) -> None:
        """Forget the socket and schedule it for deletion."""
        self._buffers.pop(sock, None)
        try:
            sock.deleteLater()
        except Exception:
            pass
        print(f"[Agentic] Connection closed ({len(self._buffers)} active)")


# --- Threaded fallback ---

class JsonSocket:
    """JSON-lines protocol over a raw winsock connection."""

    def __init__(self, conn: Any) -> None:
        self._conn   = conn
        self._buffer = b""

    def read_request(self) -> dict[str, Any] | None:
        """Read one newline-delimited JSON request. Blocks."""
        while b"\n" not in self._buffer:
            try:
                data = self._conn.recv(BUFFER_SIZE)
                if not data:
                    return None
                self._buffer += data
            except (winsock.SocketError, OSError):
                return None

        line, self._buffer = self._buffer.split(b"\n", 1)
        return json.loads(line.decode("utf-8"))

    def write_response(self, response: dict[str, Any]) -> None:
        """Write a JSON response followed by a newline."""
        data = json.dumps(response, separators=(",", ":")) + "\n"
        self._conn.sendall(data.encode("utf-8"))


class _ThreadedBridge:
    """Socket + threads fallback. Racy on Python 3.14 -- see module docstring."""

    def __init__(self, ctx: Any, port_range: range) -> None:
        self._ctx           = ctx
        self._port_range    = port_range
        self._port          : int | None            = None
        self._server_socket : Any                   = None
        self._running       : bool                  = False
        self._thread        : threading.Thread | None = None
        self._active_conns  : int                   = 0
        self._conn_lock                             = threading.Lock()
        self._dispatch_lock                         = threading.Lock()

    @property
    def port(self) -> int | None:
        return self._port

    def start(self) -> None:
        if self._running:
            return
        self._running = True

        for port in self._port_range:
            try:
                self._server_socket = winsock.Socket()
                self._server_socket.bind("127.0.0.1", port)
                self._server_socket.listen(5)
                self._port = port
                print(f"[Agentic] Listening on localhost:{port} (threaded fallback)")
                break
            except (winsock.SocketError, OSError):
                if self._server_socket:
                    try:
                        self._server_socket.close()
                    except Exception:
                        pass
                    self._server_socket = None

                if port == self._port_range[-1]:
                    start = self._port_range[0]
                    end   = self._port_range[-1]
                    print(f"[Agentic] Failed to start: all ports {start}-{end} in use")
                    self._running = False
                    return

        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False

        if self._server_socket is not None:
            try:
                self._server_socket.close()
            except Exception:
                pass
            self._server_socket = None

        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

        print("[Agentic] Server stopped")

    def _accept_loop(self) -> None:
        while self._running:
            try:
                conn = self._server_socket.accept()

                with self._conn_lock:
                    self._active_conns += 1
                    count = self._active_conns

                print(f"[Agentic] Connection accepted ({count} active)")

                # See module docstring re: Python 3.14 threading bug.
                _thread.start_new_thread(self._handle_connection, (conn,))
            except (winsock.SocketError, OSError):
                if self._running:
                    traceback.print_exc()
                break

    def _handle_connection(self, sock: Any) -> None:
        js = JsonSocket(sock)

        try:
            while self._running:
                request = js.read_request()
                if request is None:
                    break

                with self._dispatch_lock:
                    response = _dispatch(self._ctx, request)
                js.write_response(response)
        except Exception:
            traceback.print_exc()
        finally:
            sock.close()

            with self._conn_lock:
                self._active_conns -= 1
                count = self._active_conns

            print(f"[Agentic] Connection closed ({count} active)")


# --- Public facade ---

class BridgeServer:
    """TCP bridge server facade.

    Prefers the Qt event-driven backend. Falls back to the threaded
    backend if no Qt bindings are importable. A startup print identifies
    which path is active.
    """

    def __init__(self, ctx: Any, port_range: range = range(19876, 19886)) -> None:
        if _QT is not None:
            self._impl : Any = _QtBridge(ctx, port_range)
        else:
            print("[Agentic] Warning: no Qt bindings found (PyQt6/PySide6/"
                  "PySide2/PyQt5) -- using threaded fallback. This path has "
                  "a known Python 3.14 crash risk; install PySide6 or PyQt6 "
                  "for the stable path.")
            self._impl = _ThreadedBridge(ctx, port_range)

    @property
    def port(self) -> int | None:
        return self._impl.port

    def start(self) -> None:
        self._impl.start()

    def stop(self) -> None:
        self._impl.stop()
