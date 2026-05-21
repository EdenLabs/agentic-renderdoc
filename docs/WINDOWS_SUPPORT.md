# Windows support

Agentic RenderDoc on Windows runs against the standard `qrenderdoc.exe`
distribution. There are three constraints in that environment that the
rest of the codebase is shaped around — this doc records them so the
choices in the source aren't mystery decisions.

## Constraint 1: qrenderdoc embeds Python 3.6

`C:\Program Files\RenderDoc\qrenderdoc.exe` (current RenderDoc 1.44)
links `python36.dll` statically and exposes `renderdoc` / `qrenderdoc`
as built-in modules of that interpreter. There is no separate
`renderdoc.pyd` shipped on disk — those bindings are only reachable
from inside `qrenderdoc.exe`.

Consequence: every `.py` file under `src/extension/` may be loaded by
that 3.6.4 interpreter (when the extension auto-loads, when
`qrenderdoc --script` runs `src/extension/embedded_headless.py`, etc.).
The source therefore uses only syntax supported by 3.6:

- No `from __future__ import annotations` — PEP 563 is 3.7+.
- No PEP-585 subscripted built-ins (`list[X]`, `dict[K, V]`, `tuple[...]`,
  `set[X]`, `type[X]`) — annotation use requires 3.9+. Use
  `List[X]`, `Dict[K, V]`, etc. from `typing`.
- No PEP-604 union syntax (`X | None`, `A | B`) in annotations — 3.10+.
  Use `Optional[X]` / `Union[A, B]` from `typing`.
- No walrus (`:=`, 3.8), no `match` statement (3.10), no positional-only
  params via `/` in signatures (3.8).

The `src/server/` half runs in the user's own Python (≥3.10 per
`pyproject.toml`) and is unconstrained — modern syntax is fine there.

## Constraint 2: qrenderdoc's Python ships without `_socket`

The bundled Python 3.6 has no `_socket` C extension. Any module that
transitively imports `socket` at module-load time will crash the
extension during import. The known offenders:

- `socket`                — direct.
- `multiprocessing`       — imports `socket`.
- `concurrent.futures`    — imports `multiprocessing`.
- `asyncio`               — imports `socket`.

`HeadlessHandlerContext` uses `concurrent.futures.Future` and
`queue.Queue`. Both are imported lazily inside `__init__` rather than
at module scope; the class itself is only instantiated by the external-
Python entry point (`src/extension/headless.py`), so the lazy imports
never fire inside qrenderdoc, and the rest of the package (`bridge`,
`handlers`, `api_index`, `serialize`, `utilities`, ...) imports
cleanly under the GUI path.

Where bridge code needs TCP, it goes through `src/extension/winsock.py`,
which wraps `ws2_32.dll` directly via `ctypes` on Windows and
delegates to stdlib `socket` on POSIX. The threaded bridge backend
(`_ThreadedBridge` in `bridge.py`) uses that wrapper; the Qt backend
uses `QTcpServer` from PySide2 (qrenderdoc bundles PySide2).

## Constraint 3: `AlwaysLoad_Extensions` name-form mismatch

qrenderdoc's `UI.config` has an `AlwaysLoad_Extensions` list that
auto-loads named extensions at startup. **On Windows, the matching is
inconsistent between the extension directory name and the
`extension.json` `name` field.** Empirically, listing only the
hyphenated JSON name (`agentic-renderdoc`) is silently ignored; only
when the underscored directory form (`agentic_renderdoc`) is also
present does auto-load fire.

The recommended setup writes both forms:

```python
import json, os
path = os.path.join(os.environ["APPDATA"], "qrenderdoc", "UI.config")
with open(path, "r", encoding="utf-8") as f:
    cfg = json.load(f)
ext_list = cfg.setdefault("AlwaysLoad_Extensions", [])
for n in ("agentic-renderdoc", "agentic_renderdoc"):
    if n not in ext_list:
        ext_list.append(n)
with open(path, "w", encoding="utf-8") as f:
    json.dump(cfg, f, indent=4)
    f.write("\n")
```

(This is likely a bug or undocumented quirk in qrenderdoc itself,
not something we can fix from the extension side.)

## `embedded_headless.py` — Windows headless replay

Because RenderDoc on Windows doesn't ship a standalone `renderdoc.pyd`,
the external-Python headless path (`extension/headless.py`) cannot
import the bindings and so cannot run. Headless mode on Windows
instead uses `qrenderdoc.exe --script <embedded_headless.py>`:

- `--script` runs the script *before* qrenderdoc opens its main UI.
- As long as the script never returns, the UI never opens — confirmed
  with a 30-second blocking probe.
- The script opens the capture in-process via `rd.OpenCaptureFile()`
  / `OpenCapture()`, runs the existing `BridgeServer` from
  `src/extension/bridge.py` (with `force_threaded=True`, since a Qt
  event loop would hijack qrenderdoc's), and blocks until the bridge
  receives a `shutdown` command.
- On shutdown the script calls `os._exit(0)` — `sys.exit()` would
  raise `SystemExit`, which qrenderdoc may catch and then proceed
  to the UI-launch step.

The new code paths are intentionally small:

- `src/extension/embedded_headless.py` — the entry script. ~130 lines.
  Reads env-var config, sets up `sys.path`, instantiates
  `EmbeddedHeadlessContext`, starts the existing `BridgeServer`
  (`force_threaded=True`), blocks on the bridge's `_running` flag,
  then `os._exit(0)`. Uses the upstream `HANDLERS` from
  `extension/handlers.py` for `eval` / `instance_info` / `shutdown`
  etc., so Windows users get the same rich eval namespace
  (`bind_utilities`, `inspect`, `diff_state`, `goto_event`, …) that
  Linux gets.
- `EmbeddedHeadlessContext` in `src/extension/context.py` — sibling
  of `HeadlessHandlerContext`. Same single-replay-thread discipline,
  but opens the capture in-process via `rd.OpenCaptureFile()` rather
  than `CreateRemoteServerConnection()`, and uses `threading.Event`
  for setup signalling instead of `concurrent.futures.Future`
  (because `concurrent.futures` transitively imports `socket`, which
  qrenderdoc's Python lacks — see Constraint 2).
- The `sys.platform == "win32"` branch in
  `src/server/client.py`:`spawn_headless_worker`, which launches
  `qrenderdoc.exe --script embedded_headless.py` with config supplied
  via env vars (qrenderdoc does not populate `sys.argv` inside
  `--script`). Env vars used:
  - `AGENTIC_EMBEDDED_CAPTURE`     — absolute `.rdc` path.
  - `AGENTIC_EMBEDDED_PORT_MIN/MAX`— bridge port range.
  - `AGENTIC_EMBEDDED_PKG_PARENT`  — extension package's parent dir,
    prepended to `sys.path` by the script so it can import from the
    `extension` package without depending on `__file__`.
  - `AGENTIC_DISABLE_AUTOLOAD`     — set to `1` so qrenderdoc's
    `AlwaysLoad_Extensions` doesn't trigger the GUI-context bridge to
    bind a competing socket on the same port. Without this, on
    Windows with `SO_REUSEADDR`, two bridges coexist as listeners and
    incoming connections routinely hit the GUI bridge instead of ours.
    The extension's `register()` checks this env var and no-ops.

Diagnostics from the embedded script land in
`%TEMP%/agentic-renderdoc-embedded-<port>.log` (port-suffixed to
prevent concurrent workers racing on a shared file).

## Summary of changes specifically for Windows support

| File | Change |
|---|---|
| `src/extension/__init__.py` | Replaced `from __future__ import annotations` + PEP-604 syntax with `typing.Optional`. |
| `src/extension/api_index.py` | Same — typing.* in place of PEP-585 subscripted generics. |
| `src/extension/bridge.py` | Same, plus `SO_REUSEADDR` on the threaded listener (see note below). |
| `src/extension/context.py` | Same, plus lazy `import concurrent.futures` / `queue` inside `HeadlessHandlerContext.__init__` so importing the module doesn't pull `concurrent.futures` (which transitively requires `socket`). Plus a new `EmbeddedHeadlessContext` class for in-process replay inside qrenderdoc. |
| `src/extension/handlers.py` | Same. `collections.abc.Callable` → `typing.Callable` (subscripted `Callable[[...], ...]` annotations require `typing.Callable` on 3.6). |
| `src/extension/serialize.py` | Syntax-only. |
| `src/extension/utilities.py` | Same as handlers.py. |
| `src/extension/headless.py` | Syntax-only. Runs in external Python only, but kept consistent. |
| `src/extension/renderdoc_locate.py` | Syntax-only. |
| `src/extension/winsock.py` | Single type-comment fix for a 3.6-incompatible annotation in the Windows branch. |
| `src/extension/embedded_headless.py` | **New.** Thin Windows headless entry point (~130 lines). Imports `EmbeddedHeadlessContext`, `BridgeServer`, and the upstream `HANDLERS` from the package — no logic duplicated. |
| `src/server/client.py` | Windows branch in `spawn_headless_worker` to launch `qrenderdoc --script embedded_headless.py`. `_find_qrenderdoc()` helper. Bind-failure error message reads `%TEMP%/agentic-renderdoc-embedded-<port>.log` when the embedded path failed (qrenderdoc as GUI doesn't surface stderr). Skips the `renderdoccmd remote-server` port allocation on Windows since the embedded path doesn't use one. |

Linux Python 3.10+ is unaffected — all replaced syntax is valid in
every Python ≥3.5, and the Windows-specific branches in `client.py`
are guarded by `sys.platform == "win32"`.

## Side note: `SO_REUSEADDR` on the threaded bridge

`_ThreadedBridge.start()` in `src/extension/bridge.py` now calls
`setsockopt_reuse()` on the listener socket before `bind()`. This is a
cross-platform improvement, not Windows-specific:

- On any platform, the MCP server's port-discovery probe in
  `src/server/client.py:_first_free_port` binds a port with
  `SO_REUSEADDR` and closes it immediately before spawning the worker.
  Without `SO_REUSEADDR` on the worker's listener, the worker can fail
  to rebind the same port during the brief window the OS holds it.
- On Linux/macOS, `SO_REUSEADDR` just allows rebinding through TIME_WAIT
  — standard, safe behaviour.
- On Windows specifically, `SO_REUSEADDR` has hijacking semantics
  (multiple listeners can coexist on the same port), which is what
  makes the `AGENTIC_DISABLE_AUTOLOAD` env var (above) necessary —
  without it, both the auto-loaded GUI bridge and the embedded bridge
  would bind the same port and connections would race.
