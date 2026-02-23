"""TCP client for communicating with the RenderDoc extension."""

import json
import socket


# Port range matching the extension's BridgeServer.
_PORT_RANGE      = range(19876, 19886)
_PROBE_TIMEOUT   = 0.2
_ENRICH_TIMEOUT  = 1.0
_CONNECT_TIMEOUT = 2.0
_READ_TIMEOUT    = 30.0
_WRITE_TIMEOUT   = 5.0


class RenderDocClient:
    """JSON-lines TCP client that talks to the RenderDoc bridge extension.

    Supports auto-discovery of running RenderDoc instances, auto-reconnect
    on connection failures, and optional enrichment of instance metadata.
    """

    def __init__(self):
        self._sock          : socket.socket | None = None
        self._port          : int | None           = None
        self._buffer        : str                  = ""
        self._disconnected  : bool                 = False
        self._instances     : list[dict] | None    = None
        self._conn_info     : dict | None          = None

    # --- Properties ---

    @property
    def connected_port(self) -> int | None:
        """Return the port of the currently connected instance, or None."""
        return self._port

    @property
    def is_connected(self) -> bool:
        """Return True if a connection is currently established."""
        return self._sock is not None

    # --- Connection management ---

    def connect(self, port: int):
        """Connect to a RenderDoc instance on the given port.

        Closes any existing connection first, then opens a new TCP socket
        to 127.0.0.1 on the specified port. Clears the disconnected flag
        so that auto-reconnect in ensure_connected() works again.
        """
        self.disconnect()

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(_CONNECT_TIMEOUT)
        sock.connect(("127.0.0.1", port))
        sock.settimeout(None)

        self._sock          = sock
        self._port          = port
        self._disconnected  = False

    def disconnect(self):
        """Close the current connection, if any.

        Resets internal socket, port, and read buffer state. Sets the
        disconnected flag to prevent ensure_connected() from silently
        auto-reconnecting. Does not clear cached discovery or connection
        info. Call connect() to reconnect.
        """
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock          = None
            self._port          = None
            self._buffer        = ""
            self._disconnected  = True

    def discover_instances(self, enrich: bool = False) -> list[dict]:
        """Probe the port range for running RenderDoc instances.

        Returns a list of dicts, each with at least a "port" key. When
        enrich is True, connects to each discovered instance and sends an
        instance_info command to merge richer metadata (capture path, API
        type, event count) into each entry. Enrichment failures are
        non-fatal; the instance is still included with just its port.

        enrich -- If True, query each instance for metadata.
        """
        instances = []

        for port in _PORT_RANGE:
            info = self._probe_port(port, enrich=enrich)
            if info is not None:
                instances.append(info)

        return instances

    def ensure_connected(self) -> dict:
        """Auto-connect to the first available instance if not connected.

        On first use, discovers all running instances, connects to the
        first one, queries its instance_info, and caches the results.
        Subsequent calls while connected return the cached info.

        Raises ConnectionError if a prior disconnect() has not been
        followed by an explicit connect(). This prevents send() from
        silently reconnecting after the user explicitly disconnected.

        Returns a dict with:
            port      -- The port connected to.
            info      -- instance_info data from the connected instance.
            others    -- List of other available instances (port dicts).
        """
        if self._disconnected:
            raise ConnectionError(
                "disconnected; use instance(action='connect') to reconnect"
            )

        if self._sock is not None:
            return {
                "port"   : self._port,
                "info"   : self._conn_info,
                "others" : [
                    inst for inst in (self._instances or [])
                    if inst.get("port") != self._port
                ],
            }

        instances = self.discover_instances()
        if not instances:
            raise ConnectionError("no RenderDoc instances found")

        self._instances = instances
        self.connect(instances[0]["port"])

        # Query the connected instance for metadata.
        try:
            resp            = self.send("instance_info", {})
            self._conn_info = resp.get("data") if resp.get("ok") else None
        except (ConnectionError, OSError, json.JSONDecodeError):
            self._conn_info = None

        others = [
            inst for inst in instances
            if inst.get("port") != self._port
        ]

        return {
            "port"   : self._port,
            "info"   : self._conn_info,
            "others" : others,
        }

    # --- Request / response ---

    def send(self, cmd: str, params: dict) -> dict:
        """Send a command and return the parsed response.

        Auto-connects if no connection is active. If the send or receive
        fails due to a connection error, reconnects once to the same port
        and retries the request. Raises on the second failure.

        cmd    -- Command name (e.g. "eval", "instance_info").
        params -- Command parameters dict.
        """
        self.ensure_connected()
        assert self._sock is not None

        return self._send_with_retry(cmd, params)

    def _read_response(self) -> dict:
        """Read a newline-delimited JSON response.

        Consumes data from the socket until a complete line is available
        in the internal buffer. Returns the parsed JSON object.
        """
        assert self._sock is not None

        while "\n" not in self._buffer:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise ConnectionError("connection closed by RenderDoc")
            self._buffer += chunk.decode("utf-8")

        line, self._buffer = self._buffer.split("\n", 1)
        return json.loads(line)

    # --- Internal helpers ---

    def _send_with_retry(self, cmd: str, params: dict) -> dict:
        """Execute a single send/recv, retrying once on connection failure.

        On the first connection error, disconnects, reconnects to the
        same port, and replays the request. If the retry also fails, the
        error propagates to the caller.

        cmd    -- Command name.
        params -- Command parameters dict.
        """
        assert self._sock is not None
        assert self._port is not None

        try:
            return self._do_send(cmd, params)
        except (ConnectionError, BrokenPipeError, OSError):
            # Reconnect once and retry.
            port = self._port
            self.disconnect()
            self.connect(port)
            return self._do_send(cmd, params)

    def _do_send(self, cmd: str, params: dict) -> dict:
        """Perform the raw send and receive on the current socket.

        Serializes the command as a JSON line, sends it, and reads the
        response. Does not handle reconnection.

        cmd    -- Command name.
        params -- Command parameters dict.
        """
        assert self._sock is not None

        request = json.dumps({"cmd": cmd, "params": params}) + "\n"

        self._sock.settimeout(_WRITE_TIMEOUT)
        self._sock.sendall(request.encode("utf-8"))

        self._sock.settimeout(_READ_TIMEOUT)
        return self._read_response()

    def _probe_port(self, port: int, enrich: bool = False) -> dict | None:
        """Probe a single port for a running RenderDoc instance.

        Attempts a TCP connection to 127.0.0.1 on the given port. If the
        connection succeeds and enrich is True, sends an instance_info
        command over the probe socket and merges the response data into
        the returned dict. The probe socket is always closed before
        returning.

        Returns a dict with at least {"port": port} on success, or None
        if the port is not reachable.

        port   -- TCP port to probe.
        enrich -- If True, query instance_info over the probe connection.
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(_PROBE_TIMEOUT)

        try:
            sock.connect(("127.0.0.1", port))
        except (ConnectionRefusedError, TimeoutError, OSError):
            sock.close()
            return None

        result = {"port": port}

        if enrich:
            result = self._enrich_instance(sock, result)

        sock.close()
        return result

    @staticmethod
    def _enrich_instance(sock: socket.socket, instance: dict) -> dict:
        """Query instance_info over an already-connected probe socket.

        Sends the instance_info command and merges the response data
        into the instance dict. Uses a short timeout so a slow instance
        does not block discovery. On any failure, returns the instance
        dict unchanged.

        sock     -- Connected probe socket.
        instance -- Base instance dict (must contain "port").
        """
        try:
            request = json.dumps({"cmd": "instance_info", "params": {}}) + "\n"

            sock.settimeout(_ENRICH_TIMEOUT)
            sock.sendall(request.encode("utf-8"))

            # Read until we get a complete line.
            buf = ""
            while "\n" not in buf:
                chunk = sock.recv(65536)
                if not chunk:
                    return instance
                buf += chunk.decode("utf-8")

            line = buf.split("\n", 1)[0]
            resp = json.loads(line)

            if resp.get("ok") and isinstance(resp.get("data"), dict):
                # Merge response data into the instance, preserving the
                # port from our probe (authoritative) over any port in
                # the response payload.
                merged = {**resp["data"], **instance}
                return merged
        except (ConnectionError, BrokenPipeError, OSError,
                json.JSONDecodeError, UnicodeDecodeError):
            pass

        return instance
