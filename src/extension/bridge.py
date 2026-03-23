"""TCP bridge server that runs inside RenderDoc's Python environment.

Accepts JSON-lines requests over TCP and dispatches to handlers.
Ported from orb-renderdoc v1.
"""

import json
import threading
import traceback

from .handlers import HANDLERS
from .          import winsock

BUFFER_SIZE = 65536


class JsonSocket:
    """JSON-lines protocol over a raw winsock connection.

    Buffers incoming bytes and splits on newlines. Each line is one
    JSON request or response.
    """

    def __init__(self, conn):
        """Wrap a winsock.Socket for JSON-lines I/O.

        conn -- Connected winsock.Socket instance.
        """
        self._conn   = conn
        self._buffer = b""

    def read_request(self):
        """Read a single newline-delimited JSON request.

        Blocks until a complete line arrives. Returns the parsed dict,
        or None on connection close or socket error.
        """
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

    def write_response(self, response):
        """Write a JSON response followed by a newline."""
        data = json.dumps(response, separators=(",", ":")) + "\n"
        self._conn.sendall(data.encode("utf-8"))


class BridgeServer:
    """Multi-threaded TCP server for the RenderDoc extension.

    Runs in a background thread. Serializes RenderDoc API access with a
    dispatch lock (the replay API is single-threaded).
    """

    def __init__(self, ctx, port_range=range(19876, 19886)):
        """Create a bridge server.

        ctx        -- HandlerContext shared with all handlers.
        port_range -- Range of ports to try when binding.
        """
        self._ctx              = ctx
        self._port_range       = port_range
        self._port             = None
        self._server_socket    = None
        self._running          = False
        self._thread           = None
        self._active_conns     = 0
        self._conn_lock        = threading.Lock()
        self._dispatch_lock    = threading.Lock()

    @property
    def port(self):
        """The port the server is listening on, or None if not started."""
        return self._port

    def start(self):
        """Bind to the first available port and start accepting connections.

        Tries each port in the configured range. Skips SO_REUSEADDR on
        Windows because it allows multiple binds to the same port.
        """
        if self._running:
            return

        self._running = True

        for port in self._port_range:
            try:
                self._server_socket = winsock.Socket()
                self._server_socket.bind("127.0.0.1", port)
                self._server_socket.listen(5)
                self._port = port
                print(f"[Agentic] Listening on localhost:{port}")
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

    def stop(self):
        """Shut down the server and close all connections."""
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

    def _accept_loop(self):
        """Accept connections and spawn a handler thread for each."""
        while self._running:
            try:
                conn = self._server_socket.accept()

                with self._conn_lock:
                    self._active_conns += 1
                    count = self._active_conns

                print(f"[Agentic] Connection accepted ({count} active)")

                t = threading.Thread(
                    target=self._handle_connection,
                    args=(conn,),
                    daemon=True,
                )
                t.start()
            except (winsock.SocketError, OSError):
                if self._running:
                    traceback.print_exc()
                break

    def _handle_connection(self, sock):
        """Handle a single client connection (runs in its own thread).

        Reads JSON-lines requests, dispatches each through _dispatch,
        and writes the response back. Runs until the client disconnects
        or the server shuts down.
        """
        js = JsonSocket(sock)

        try:
            while self._running:
                request = js.read_request()
                if request is None:
                    break

                response = self._dispatch(request)
                js.write_response(response)
        except Exception:
            traceback.print_exc()
        finally:
            sock.close()

            with self._conn_lock:
                self._active_conns -= 1
                count = self._active_conns

            print(f"[Agentic] Connection closed ({count} active)")

    def _dispatch(self, request):
        """Dispatch a request to the appropriate handler.

        Serializes all handler invocations through _dispatch_lock so
        that RenderDoc's single-threaded replay API is never called
        concurrently.
        """
        cmd    = request.get("cmd", "")
        params = request.get("params", {})

        handler_entry = HANDLERS.get(cmd)
        if handler_entry is None:
            return {"ok": False, "error": f"unknown command: {cmd}"}

        with self._dispatch_lock:
            try:
                return handler_entry["func"](self._ctx, params)
            except Exception as e:
                traceback.print_exc()
                return {"ok": False, "error": str(e)}
