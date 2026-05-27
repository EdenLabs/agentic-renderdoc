"""TCP client for communicating with the RenderDoc extension."""

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path


# Port range matching the extension's BridgeServer.
_PORT_RANGE         = range(19876, 19886)
_REMOTE_PORT_RANGE  = range(39920, 39930)
_PROBE_TIMEOUT      = 0.2
_ENRICH_TIMEOUT     = 1.0
_CONNECT_TIMEOUT    = 2.0
_WRITE_TIMEOUT      = 5.0
_WORKER_BIND_WAIT   = 30.0
_WORKER_GRACE_SECS  = 5.0

# Per-command read deadlines. Anything not in this map gets the default.
# eval and get_texture can drive SetFrameEvent which triggers a full
# frame replay — capture-size dependent, but rarely longer than ~60s in
# practice. Cheap metadata calls finish in milliseconds; 10s is plenty.
_DEFAULT_READ_TIMEOUT = 10.0
_READ_TIMEOUTS: dict[str, float] = {
    "eval"        : 90.0,
    "get_texture" : 90.0,
}


def _format_timeout_error(e: "WorkerTimeoutError") -> dict:
    """Convert a WorkerTimeoutError into the structured response shape."""
    if e.is_spawned:
        hints = [
            f"the headless worker on port {e.port} (pid {e.pid}) "
            f"did not respond within {e.timeout:.0f}s — likely a hung "
            "SetFrameEvent or replay-driver wait that cannot be cancelled",
            f"force-terminate it with Instance(action='close', "
            f"port={e.port}, force=True), then re-open the capture with "
            "Instance(action='open', file=...)",
        ]
    else:
        hints = [
            f"the RenderDoc instance on port {e.port} did not respond "
            f"within {e.timeout:.0f}s",
            "this is a live RenderDoc UI, not a worker spawned by this "
            "server; restart RenderDoc or use Instance(action='disconnect')",
        ]
    return {
        "ok"    : False,
        "error" : {
            "kind"     : "worker_timeout",
            "message"  : str(e),
            "port"     : e.port,
            "cmd"      : e.cmd,
            "timeout"  : e.timeout,
            "headless" : e.is_spawned,
            "pid"      : e.pid,
            "hints"    : hints,
        },
    }


def _format_dead_error(e: "WorkerDeadError") -> dict:
    """Convert a WorkerDeadError into the structured response shape."""
    if e.is_spawned:
        hints = [
            f"the headless worker on port {e.port} (pid {e.pid}) is gone "
            "— it crashed, was killed, or its renderdoccmd remote-server "
            "child exited",
            "spawn a fresh one with Instance(action='open', file=...)",
        ]
    else:
        hints = [
            f"the RenderDoc instance on port {e.port} is unreachable",
            "use Instance(action='list') to see what's currently running",
        ]
    return {
        "ok"    : False,
        "error" : {
            "kind"     : "worker_dead",
            "message"  : str(e),
            "port"     : e.port,
            "cmd"      : e.cmd,
            "headless" : e.is_spawned,
            "pid"      : e.pid,
            "hints"    : hints,
        },
    }


def _find_qrenderdoc() -> str | None:
    """Locate ``qrenderdoc.exe`` (or ``qrenderdoc`` on POSIX) for spawning.

    Tries PATH first via ``shutil.which``, then the standard Windows
    install location. Returns the resolved absolute path or None if not
    found.
    """
    import shutil
    name = "qrenderdoc.exe" if sys.platform == "win32" else "qrenderdoc"
    found = shutil.which(name)
    if found:
        return found
    if sys.platform == "win32":
        candidates = [
            r"C:\Program Files\RenderDoc\qrenderdoc.exe",
            os.path.join(
                os.environ.get("LOCALAPPDATA", ""),
                "Programs", "RenderDoc", "qrenderdoc.exe",
            ),
        ]
        for c in candidates:
            if c and os.path.isfile(c):
                return c
    return None


def _die_with_parent() -> None:
    """preexec_fn: ask the kernel to SIGKILL us when our parent dies.

    Same protection as in extension.headless — guards against the case
    where this MCP server itself dies while its workers are running. The
    workers would then continue running indefinitely with no controller.
    Linux-only.
    """
    import ctypes
    import signal as _sig
    PR_SET_PDEATHSIG = 1
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(PR_SET_PDEATHSIG, _sig.SIGKILL, 0, 0, 0)
    except Exception:
        pass


class WorkerTimeoutError(TimeoutError):
    """A worker did not respond to a command before its deadline.

    Distinct from ``WorkerDeadError`` — the connection is fine, but
    something on the worker side is taking too long (often a hung
    SetFrameEvent that cannot be cancelled).
    """

    def __init__(
        self,
        port       : int,
        cmd        : str,
        timeout    : float,
        is_spawned : bool,
        pid        : int | None,
    ) -> None:
        super().__init__(
            f"worker on port {port} did not respond to {cmd!r} within {timeout}s"
        )
        self.port       = port
        self.cmd        = cmd
        self.timeout    = timeout
        self.is_spawned = is_spawned
        self.pid        = pid


class WorkerDeadError(ConnectionError):
    """A worker is unreachable: refused the connection, dropped EOF,
    or its process exited.
    """

    def __init__(
        self,
        port       : int,
        cmd        : str,
        is_spawned : bool,
        pid        : int | None,
        original   : Exception | None,
    ) -> None:
        super().__init__(
            f"worker on port {port} is unreachable for {cmd!r}: {original}"
        )
        self.port       = port
        self.cmd        = cmd
        self.is_spawned = is_spawned
        self.pid        = pid
        self.original   = original


class RenderDocClient:
    """JSON-lines TCP client that talks to the RenderDoc bridge extension.

    Supports auto-discovery of running RenderDoc instances, auto-reconnect
    on connection failures, and optional enrichment of instance metadata.
    Also owns the subprocess handles for any headless workers it spawned
    so they can be terminated cleanly.
    """

    def __init__(self):
        self._sock          : socket.socket | None = None
        self._port          : int | None           = None
        self._buffer        : str                  = ""
        self._disconnected  : bool                 = False
        self._instances     : list[dict] | None    = None
        self._conn_info     : dict | None          = None
        # port -> subprocess.Popen for headless workers we spawned.
        self._spawned       : dict[int, subprocess.Popen] = {}

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

        Auto-connects if no connection is active. Connection-level
        failures during send produce a structured error dict
        (``{"ok": False, "error": {...}}``) rather than raising, so the
        agent can react without a try/except in every tool.

        Behaviors by failure mode:
          * Read timeout (worker didn't respond before the per-command
            deadline) — single attempt, no retry. Returns a
            ``worker_timeout`` error with hints to ``Instance(action=
            'close', force=True)`` for spawned workers.
          * Connection refused / EOF — checks subprocess liveness for
            spawned workers. If dead, untracks and returns
            ``worker_dead``. If live (or unknown), reconnects once and
            retries. If retry also fails, returns ``worker_dead``.

        Setup-level failures (no instances found, explicit disconnect)
        still raise ``ConnectionError`` from ``ensure_connected``.

        cmd    -- Command name (e.g. "eval", "instance_info").
        params -- Command parameters dict.
        """
        self.ensure_connected()
        assert self._sock is not None

        try:
            return self._send_with_retry(cmd, params)
        except WorkerTimeoutError as e:
            return _format_timeout_error(e)
        except WorkerDeadError as e:
            return _format_dead_error(e)

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
        """Execute a single send/recv with deadline + liveness-aware retry.

        Read-timeout is final (no retry); the worker is just slow or
        wedged, and a retry will hang again. Connection-level failures
        retry once if the worker is still alive; otherwise the dead
        worker is untracked and ``WorkerDeadError`` is raised.

        cmd    -- Command name.
        params -- Command parameters dict.
        """
        assert self._sock is not None
        assert self._port is not None

        port    = self._port
        timeout = _READ_TIMEOUTS.get(cmd, _DEFAULT_READ_TIMEOUT)

        try:
            return self._do_send(cmd, params, read_timeout=timeout)
        except TimeoutError:
            # Worker is unresponsive but the connection is still open.
            # No retry — a second attempt would just hang again.
            raise WorkerTimeoutError(
                port       = port,
                cmd        = cmd,
                timeout    = timeout,
                is_spawned = port in self._spawned,
                pid        = self._spawned_pid(port),
            )
        except (ConnectionError, BrokenPipeError, OSError) as first_err:
            # Connection-level failure. If the worker process is dead,
            # don't bother retrying.
            if not self._is_worker_alive(port):
                pid = self._spawned_pid(port)
                # Untrack a dead spawned worker so list/close behave
                # consistently with reality.
                self._spawned.pop(port, None)
                self.disconnect()
                raise WorkerDeadError(
                    port       = port,
                    cmd        = cmd,
                    is_spawned = pid is not None,
                    pid        = pid,
                    original   = first_err,
                )

            # Worker still appears alive; reconnect and retry once.
            self.disconnect()
            try:
                self.connect(port)
                return self._do_send(cmd, params, read_timeout=timeout)
            except (ConnectionError, BrokenPipeError, OSError) as second_err:
                pid = self._spawned_pid(port)
                if pid is not None and not self._is_worker_alive(port):
                    self._spawned.pop(port, None)
                raise WorkerDeadError(
                    port       = port,
                    cmd        = cmd,
                    is_spawned = pid is not None,
                    pid        = pid,
                    original   = second_err,
                )
            except TimeoutError:
                raise WorkerTimeoutError(
                    port       = port,
                    cmd        = cmd,
                    timeout    = timeout,
                    is_spawned = port in self._spawned,
                    pid        = self._spawned_pid(port),
                )

    def _do_send(self, cmd: str, params: dict, read_timeout: float) -> dict:
        """Perform the raw send and receive on the current socket.

        cmd          -- Command name.
        params       -- Command parameters dict.
        read_timeout -- Seconds to wait for the response (per-command).
        """
        assert self._sock is not None

        request = json.dumps({"cmd": cmd, "params": params}) + "\n"

        self._sock.settimeout(_WRITE_TIMEOUT)
        self._sock.sendall(request.encode("utf-8"))

        self._sock.settimeout(read_timeout)
        return self._read_response()

    def _is_worker_alive(self, port: int) -> bool:
        """True if the spawned worker on port is still alive (or unknown).

        For workers we spawned, probes the subprocess via
        ``os.kill(pid, 0)``. For unknown ports (e.g. a live RenderDoc
        UI) we cannot tell, so default to True and let the retry path
        decide.
        """
        proc = self._spawned.get(port)
        if proc is None:
            return True

        if proc.poll() is not None:
            return False

        try:
            os.kill(proc.pid, 0)
        except OSError:
            return False
        return True

    def _spawned_pid(self, port: int) -> int | None:
        """PID of the spawned worker on port, or None if not spawned by us."""
        proc = self._spawned.get(port)
        return proc.pid if proc is not None else None

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

    # --- Headless worker management ---

    def spawn_headless_worker(self, capture_path: str) -> dict:
        """Spawn a headless worker for a .rdc file.

        Picks a free port in the agentic range and a free port in the
        renderdoccmd remote-server range, launches the worker as a
        subprocess, and waits for the bridge port to come up. Tracks
        the Popen handle so close_headless_worker() can terminate it.

        Returns the bound port, the captured PID, and the metadata
        from a probe of the new instance.

        Raises RuntimeError on any failure; the subprocess is reaped if
        it was started.
        """
        capture = Path(capture_path)
        if not capture.exists():
            raise RuntimeError(f"capture not found: {capture}")

        # Reserve ports — pick free, pass exact-port range to worker.
        bridge_port = self._first_free_port(_PORT_RANGE, exclude=set(self._spawned))
        if bridge_port is None:
            raise RuntimeError(
                f"no free port in agentic range "
                f"{_PORT_RANGE.start}-{_PORT_RANGE.stop - 1}"
            )

        # On Windows the standard RenderDoc distribution does not ship
        # ``renderdoc.pyd`` for external Python — the SWIG bindings are
        # compiled into ``qrenderdoc.exe``. Run headless logic inside
        # qrenderdoc's embedded Python via ``--script``; it exits via
        # ``os._exit(0)`` before qrenderdoc ever opens its main UI, and
        # uses in-process ``rd.OpenCaptureFile`` rather than
        # ``renderdoccmd remoteserver``. No remote port needed.
        windows_embedded = sys.platform == "win32"

        if windows_embedded:
            remote_port = None
            qrd_path = _find_qrenderdoc()
            if qrd_path is None:
                raise RuntimeError(
                    "could not locate qrenderdoc.exe (looked on PATH and "
                    "%ProgramFiles%\\RenderDoc); install RenderDoc system-wide"
                )
            embedded_script = (
                Path(__file__).resolve().parent.parent / "extension" / "embedded_headless.py"
            )
            cmd = [qrd_path, "--script", str(embedded_script)]
        else:
            remote_port = self._first_free_port(_REMOTE_PORT_RANGE)
            if remote_port is None:
                raise RuntimeError(
                    f"no free port in remote range "
                    f"{_REMOTE_PORT_RANGE.start}-{_REMOTE_PORT_RANGE.stop - 1}"
                )
            cmd = [
                sys.executable,
                "-u",
                "-m", "extension.headless",
                str(capture.resolve()),
                "--port-min",        str(bridge_port),
                "--port-max",        str(bridge_port),
                "--remote-port-min", str(remote_port),
                "--remote-port-max", str(remote_port),
            ]

        env = os.environ.copy()
        # Make sure the extension/ package is importable.
        src_dir = Path(__file__).resolve().parent.parent
        existing_pp = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (
            f"{src_dir}{os.pathsep}{existing_pp}" if existing_pp else str(src_dir)
        )
        # Disable Vulkan implicit layers in the worker. The system has
        # the RenderDoc capture layer registered globally
        # (/etc/vulkan/implicit_layer.d/renderdoc_capture.json), and the
        # Vulkan loader will auto-inject it into any process that
        # initialises Vulkan. That conflicts with the replay role of the
        # same library — librenderdoc.so loaded twice with different
        # roles asserts and eventually drops the proxy socket with
        # EBADF. We set both the all-layers disable and the specific
        # layer disable for belt-and-suspenders coverage across loader
        # versions.
        env["VK_LOADER_LAYERS_DISABLE"]                = "*"
        env["DISABLE_VK_LAYER_RENDERDOC_Capture_1"]    = "1"

        if windows_embedded:
            # qrenderdoc does not populate sys.argv inside --script, so
            # the embedded script reads its config from env vars instead.
            # PKG_PARENT is the directory the script prepends to sys.path
            # so it can ``from extension.context import ...`` etc.
            # (relying on __file__ would be flaky — qrenderdoc doesn't
            # always populate it under --script).
            env["AGENTIC_EMBEDDED_CAPTURE"]    = str(capture.resolve())
            env["AGENTIC_EMBEDDED_PORT_MIN"]   = str(bridge_port)
            env["AGENTIC_EMBEDDED_PORT_MAX"]   = str(bridge_port)
            env["AGENTIC_EMBEDDED_PKG_PARENT"] = str(src_dir)
            # Prevent qrenderdoc's AlwaysLoad_Extensions auto-load from
            # binding a competing bridge on the same port. With Windows'
            # SO_REUSEADDR semantics both bridges would coexist as
            # listeners and incoming connections would routinely hit the
            # GuiHandlerContext-backed one instead of ours. The
            # extension's register() checks this var and no-ops.
            env["AGENTIC_DISABLE_AUTOLOAD"]    = "1"

        popen_kwargs = {
            "stdin"  : subprocess.DEVNULL,
            "stdout" : subprocess.DEVNULL,
            "stderr" : subprocess.PIPE,
            "env"    : env,
        }
        if sys.platform.startswith("linux"):
            popen_kwargs["preexec_fn"] = _die_with_parent

        proc = subprocess.Popen(cmd, **popen_kwargs)
        # Why stdin=DEVNULL: when this MCP server is launched via stdio
        # (Claude Code's default), our own stdin is the JSON-RPC socket.
        # If the worker inherits that fd, renderdoc/renderdoccmd may read
        # bytes off it during startup and corrupt our protocol stream
        # — manifests as the proxy socket dropping with EBADF a few
        # seconds after OpenCapture.

        # Capture early stderr in a buffer for diagnostic on bind failure,
        # then once the worker is healthy keep draining (to DEVNULL) so the
        # pipe can never fill and block the worker on a stderr write.
        stderr_buffer: list[bytes] = []
        stderr_drain  = threading.Event()

        def _drain_stderr() -> None:
            stream = proc.stderr
            if stream is None:
                return
            try:
                while True:
                    chunk = stream.read(4096)
                    if not chunk:
                        return
                    if not stderr_drain.is_set():
                        stderr_buffer.append(chunk)
                    # else: discard
            except Exception:
                return

        threading.Thread(
            target = _drain_stderr,
            name   = f"agentic-stderr-{bridge_port}",
            daemon = True,
        ).start()

        # Poll for the bridge port to come up.
        info = self._wait_for_worker(proc, bridge_port, timeout=_WORKER_BIND_WAIT)
        if info is None:
            self._reap_proc(proc)
            err_bytes = b"".join(stderr_buffer)
            err_text  = err_bytes.decode("utf-8", errors="replace").strip()

            # Embedded-headless mode (qrenderdoc --script) doesn't write
            # diagnostics to stderr — qrenderdoc is a GUI subprocess. Read
            # the per-port log file the script writes instead. The path
            # must match the one embedded_headless.py writes.
            log_text = ""
            if windows_embedded:
                base = os.environ.get("TEMP") or os.environ.get("TMP") or "."
                log_path = os.path.join(
                    base, f"agentic-renderdoc-embedded-{bridge_port}.log"
                )
                try:
                    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                        log_text = f.read().strip()
                except OSError:
                    pass

            if windows_embedded:
                diag  = log_text or err_text or "<empty>"
                label = "embedded-log"
            else:
                diag  = err_text or "<empty>"
                label = "stderr"
            raise RuntimeError(
                f"headless worker did not bind on {bridge_port} within "
                f"{_WORKER_BIND_WAIT:.0f}s; {label}: {diag}"
            )

        # Worker is healthy. Stop buffering — drain thread keeps reading
        # but discards.
        stderr_drain.set()
        stderr_buffer.clear()

        self._spawned[bridge_port] = proc

        return {
            "port"        : bridge_port,
            "remote_port" : remote_port,
            "pid"         : proc.pid,
            "info"        : info,
        }

    def close_headless_worker(self, port: int, force: bool = False) -> dict:
        """Stop a headless worker. Returns a status dict.

        First tries a graceful shutdown via the bridge's ``shutdown``
        command. Then SIGTERMs the subprocess; escalates to SIGKILL on
        ``force=True`` or after a grace period.

        Returns ``{"closed": True, ...}`` on success; ``{"closed": False,
        "reason": ...}`` if the port isn't a worker we own.
        """
        proc = self._spawned.get(port)
        if proc is None:
            return {
                "closed" : False,
                "reason" : (
                    f"port {port} is not a worker spawned by this server; "
                    "use Instance(action='disconnect') for live RenderDoc UIs"
                ),
            }

        # Step 1: ask the bridge to shut down via the protocol if it's
        # still responsive. Best-effort; a wedged worker may not reply.
        # Then give it a chance to exit on its own — the worker has to
        # tear down its renderdoccmd remoteserver child too, which can
        # take a moment.
        if not force:
            try:
                self._send_shutdown_to(port)
            except Exception:
                pass
            try:
                proc.wait(timeout=_WORKER_GRACE_SECS)
            except subprocess.TimeoutExpired:
                pass

        # Step 2: SIGTERM and wait briefly.
        if proc.poll() is None and not force:
            try:
                proc.terminate()
            except OSError:
                pass
            try:
                proc.wait(timeout=_WORKER_GRACE_SECS)
            except subprocess.TimeoutExpired:
                pass

        # Step 3: SIGKILL on force or if still alive.
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=_WORKER_GRACE_SECS)
            except subprocess.TimeoutExpired:
                pass

        rc = proc.returncode

        # If we're connected to this worker, drop the connection.
        if self._port == port:
            self.disconnect()

        del self._spawned[port]

        return {
            "closed"      : True,
            "port"        : port,
            "exit_code"   : rc,
            "force"       : force,
        }

    def reap_dead_workers(self) -> list[int]:
        """Clean up any tracked workers whose process exited unexpectedly.

        Returns a list of ports whose workers had died. Useful for
        liveness checks before reporting instance lists.
        """
        dead: list[int] = []
        for port, proc in list(self._spawned.items()):
            if proc.poll() is not None:
                dead.append(port)
                if self._port == port:
                    self.disconnect()
                del self._spawned[port]
        return dead

    @property
    def spawned_ports(self) -> list[int]:
        """Ports of headless workers we currently track."""
        return list(self._spawned.keys())

    # --- Internal: worker spawn helpers ---

    def _first_free_port(self, port_range: range, exclude: set[int] | None = None) -> int | None:
        """Return the first locally-bindable port in range, skipping exclude.

        A bind probe with ``SO_REUSEADDR`` is not enough on Windows: the
        extension's bridge listener also sets ``SO_REUSEADDR`` (see
        ``bridge.py:_ThreadedBridge.start``), so two listeners can coexist
        on the same port. ``bind()`` would silently succeed against a port
        that already has a running bridge, and the worker we then spawn
        would race the existing bridge for incoming connections.
        Connect-probe first to detect a live bridge.
        """
        excl = exclude or set()
        for port in port_range:
            if port in excl:
                continue
            if self._probe_port(port) is not None:
                # Something is already listening — could be a GUI bridge
                # on the same port that we'd otherwise hijack via
                # SO_REUSEADDR.
                continue
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            finally:
                s.close()
            return port
        return None

    def _wait_for_worker(self, proc: subprocess.Popen, port: int, timeout: float) -> dict | None:
        """Block until the worker binds on port and answers instance_info.

        Returns the instance_info data dict, or None on timeout / proc death.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if proc.poll() is not None:
                return None
            info = self._probe_port(port, enrich=True)
            if info is not None and info.get("capture_loaded"):
                return info
            time.sleep(0.25)
        return None

    def _send_shutdown_to(self, port: int) -> None:
        """Send a one-shot shutdown command to a worker on a specific port."""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(_CONNECT_TIMEOUT)
        s.connect(("127.0.0.1", port))
        s.settimeout(_WRITE_TIMEOUT)
        s.sendall(b'{"cmd":"shutdown","params":{}}\n')
        # Best-effort response read; ignore content.
        s.settimeout(2.0)
        try:
            s.recv(1024)
        except OSError:
            pass
        finally:
            s.close()

    def _reap_proc(self, proc: subprocess.Popen) -> None:
        """Politely terminate a Popen we don't intend to track."""
        if proc.poll() is not None:
            return
        try:
            proc.terminate()
        except OSError:
            pass
        try:
            proc.wait(timeout=_WORKER_GRACE_SECS)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=_WORKER_GRACE_SECS)
            except subprocess.TimeoutExpired:
                pass

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
