"""Command handlers for the RenderDoc bridge extension.

Handlers: eval, api_index, instance_info, get_texture, reload.
"""

import traceback

from .api_index import search_index

# Populated at registration time.
HANDLERS = {}

# Serializer dispatch table, keyed by RenderDoc type name.
# Built lazily on first use since the serialize module imports renderdoc,
# which may not be available during testing.
_SERIALIZER_MAP = None


def handler(name, description="", schema=None):
    """Decorator to register a command handler."""
    def decorator(func):
        HANDLERS[name] = {
            "func"        : func,
            "description" : description,
            "schema"      : schema or {},
        }
        return func
    return decorator


# --- eval ---

@handler(
    "eval",
    description="Execute Python code in the RenderDoc environment.",
    schema={
        "properties": {"code": {"type": "string", "description": "Python code to execute."}},
        "required":   ["code"],
    },
)
def handle_eval(ctx, params):
    """Execute arbitrary Python and return the result of the last expression.

    Builds a namespace with RenderDoc globals and utility modules, runs the
    code, serializes the result for JSON transport, and captures any print
    output. Returns error info with contextual hints on failure.
    """
    code = params.get("code", "")
    if not code:
        return {"error": "no code provided"}

    # Build the execution namespace with utilities and RenderDoc globals.
    captured_output = []
    namespace       = _build_namespace(ctx, captured_output)

    try:
        raw_result = _exec_with_result(code, namespace)
        result     = _serialize_result(raw_result)

        response = {"ok": True, "data": result}
        if captured_output:
            response["output"] = captured_output
        if ctx._replay_warnings:
            response["warnings"] = list(ctx._replay_warnings)
        return response
    except Exception as e:
        response = {
            "ok"    : False,
            "error" : _format_error(e, code, namespace),
        }
        if captured_output:
            response["output"] = captured_output
        if ctx._replay_warnings:
            response["warnings"] = list(ctx._replay_warnings)
        return response


# --- api_index ---

@handler(
    "api_index",
    description="Search the RenderDoc Python API reference.",
    schema={
        "properties": {
            "query" : {"type": "string", "description": "Search term."},
            "limit" : {"type": "integer", "description": "Max results to return (default 20)."},
        },
        "required": ["query"],
    },
)
def handle_api_index(ctx, params):
    """Search the cached API index for matching entries.

    Returns up to `limit` results from the pre-built API reference index.
    The index is built on first capture load by introspecting the live
    renderdoc module.
    """
    query = params.get("query", "").lower()
    if not query:
        return {"error": "no query provided"}

    if ctx.api_index is None:
        return {
            "ok"    : False,
            "error" : (
                "API index not built yet. "
                "It builds automatically when the first capture is loaded. "
                "Open a capture file and try again."
            ),
        }

    limit   = params.get("limit", 20)
    results = search_index(ctx.api_index, query)

    return {"ok": True, "data": results[:limit]}


# --- instance_info ---

@handler(
    "instance_info",
    description="Return metadata about this RenderDoc instance.",
    schema={},
)
def handle_instance_info(ctx, params):
    """Return port, capture state, API type, path, and event count."""
    # Resolve the API type name from the SWIG enum. The enum wrapper may
    # or may not expose .name depending on the RenderDoc build.
    api_type = None
    if ctx._capture_loaded and ctx._api_type is not None:
        if hasattr(ctx._api_type, "name"):
            api_type = ctx._api_type.name
        else:
            api_type = str(ctx._api_type)

    capture_path = ctx._capture_path if ctx._capture_loaded else None
    event_count  = ctx._event_count  if ctx._capture_loaded else 0

    return {
        "ok"   : True,
        "data" : {
            "port"           : ctx._server_port,
            "capture_loaded" : ctx._capture_loaded,
            "api_type"       : api_type,
            "capture_path"   : capture_path,
            "event_count"    : event_count,
        },
    }


# --- get_texture ---

@handler(
    "get_texture",
    description="Read raw texture data as base64, with format metadata.",
    schema={
        "properties": {
            "resource_id" : {"type": "string",  "description": "Texture resource ID (as returned by other commands)."},
            "event_id"    : {"type": "integer", "description": "Event ID to set the replay cursor to before reading. Required for render targets. Omit for source textures."},
            "mip"         : {"type": "integer", "description": "Mip level (default 0)."},
            "slice"       : {"type": "integer", "description": "Array slice (default 0)."},
            "sample"      : {"type": "integer", "description": "Multisample index (default 0)."},
        },
        "required": ["resource_id"],
    },
)
def handle_get_texture(ctx, params):
    """Read raw texture bytes and return them base64-encoded with format metadata.

    Uses GetTextureData for a direct memory read rather than SaveTexture,
    which triggers internal replays that can deadlock under agentic usage.
    Format conversion (HDR mapping, channel extraction, BGRA swizzle) is
    left to the MCP server where Pillow is available.
    """
    import base64

    import renderdoc as rd
    from . import serialize

    resource_id = params.get("resource_id")
    if not resource_id:
        return {"ok": False, "error": "resource_id is required"}

    event_id    = params.get("event_id")
    mip         = params.get("mip", 0)
    slice_param = params.get("slice", 0)
    sample      = params.get("sample", 0)

    def callback(controller):
        # Find the matching texture by comparing serialized resource IDs.
        tex = None
        for t in controller.GetTextures():
            if str(int(t.resourceId)) == resource_id:
                tex = t
                break

        if tex is None:
            return {"ok": False, "error": f"no texture found with resource id {resource_id}"}

        # Position the replay cursor if an event was specified.
        # Required for render targets whose contents depend on the event.
        if event_id is not None:
            controller.SetFrameEvent(event_id, True)

        raw_bytes = controller.GetTextureData(
            tex.resourceId,
            rd.Subresource(mip, slice_param, sample),
        )

        if not raw_bytes:
            return {"ok": False, "error": "GetTextureData returned empty data"}

        mip_width  = max(1, tex.width >> mip)
        mip_height = max(1, tex.height >> mip)

        return {
            "ok"   : True,
            "data" : {
                "raw"        : base64.b64encode(bytes(raw_bytes)).decode("ascii"),
                "width"      : tex.width,
                "height"     : tex.height,
                "depth"      : tex.depth,
                "format"     : serialize.format_description(tex.format),
                "mip"        : mip,
                "mip_width"  : mip_width,
                "mip_height" : mip_height,
                "slice"      : slice_param,
                "sample"     : sample,
            },
        }

    return ctx.replay(callback)


# --- reload (dev only) ---

@handler(
    "reload",
    description="Hot-reload extension modules without restarting RenderDoc.",
    schema={},
)
def handle_reload(ctx, params):
    """Reload business-logic modules in dependency order.

    Leaves winsock and bridge untouched (infrastructure). Mutates the
    HANDLERS dict in-place so the bridge's reference stays valid.
    Rebuilds the API index and clears the serializer cache.
    """
    import importlib
    from . import serialize, api_index, utilities

    # Reload in dependency order: leaves first, then this module.
    importlib.reload(serialize)
    importlib.reload(api_index)
    importlib.reload(utilities)

    # Save reference to the dict the bridge is holding.
    old_handlers = HANDLERS

    # Reload this module. This re-runs all @handler decorators into a
    # fresh HANDLERS dict inside the new module object.
    from . import handlers as _self
    importlib.reload(_self)

    # Splice the new registrations into the old dict object.
    old_handlers.clear()
    old_handlers.update(_self.HANDLERS)

    # Rebuild the API index with the reloaded api_index module.
    from .api_index import build_index
    ctx._api_index = build_index()

    # Clear the serializer cache so it picks up the reloaded serialize module.
    global _SERIALIZER_MAP
    _SERIALIZER_MAP = None

    return {
        "ok"   : True,
        "data" : {
            "reloaded"  : ["serialize", "api_index", "utilities", "handlers"],
            "handlers"  : list(old_handlers.keys()),
        },
    }


# --- Internal helpers ---

def _build_namespace(ctx, captured_output):
    """Build the execution namespace for eval, including utilities.

    Injects RenderDoc modules (rd, qrd), the handler context, the serialize
    module, and a print override that captures output to captured_output so
    it can be included in the response.

    ctx             -- HandlerContext shared with all handlers.
    captured_output -- Mutable list; print calls append strings here.
    """
    ns = {"ctx": ctx}

    # RenderDoc modules are available globally inside the extension environment.
    # Import them into the namespace so eval code can use them directly.
    try:
        import renderdoc as rd
        ns["rd"] = rd
    except ImportError:
        pass

    try:
        import qrenderdoc as qrd
        ns["qrd"] = qrd
    except ImportError:
        pass

    # Expose the serialize module so agents can call serializers directly.
    try:
        from . import serialize
        ns["serialize"] = serialize
    except ImportError:
        pass

    # Inject utility functions (inspect, diff_state, goto_event, etc.)
    # bound to this handler context.
    try:
        from .utilities import bind_utilities
        ns.update(bind_utilities(ctx))
    except ImportError:
        pass

    # Override print to capture output alongside the eval result.
    def _capture_print(*args, **kwargs):
        """Replacement print that appends to captured_output."""
        sep = kwargs.get("sep", " ")
        end = kwargs.get("end", "\n")
        captured_output.append(sep.join(str(a) for a in args) + end)

    ns["print"] = _capture_print

    return ns


def _exec_with_result(code, namespace):
    """Execute code and return the value of the last expression, if any.

    Splits the code into statements and the final expression. Executes all
    statements, then evaluates the final expression and returns its value.
    If the final line is not an expression, returns None.
    """
    import ast

    tree = ast.parse(code)
    if not tree.body:
        return None

    last = tree.body[-1]

    # If the last node is an expression, split it off and eval it separately.
    if isinstance(last, ast.Expr):
        stmts = ast.Module(body=tree.body[:-1], type_ignores=[])
        expr  = ast.Expression(body=last.value)

        exec(compile(stmts, "<eval>", "exec"), namespace)
        return eval(compile(expr, "<eval>", "eval"), namespace)
    else:
        exec(compile(tree, "<eval>", "exec"), namespace)
        return None


def _format_error(exc, code, namespace):
    """Format an exception with stack trace, failing line, and contextual hints.

    Returns a dict with:
    - traceback:    full formatted traceback string
    - failing_line: the specific source line that failed (if extractable)
    - hints:        list of hint strings for common RenderDoc mistakes

    exc       -- The caught exception.
    code      -- The source code that was executed.
    namespace -- The execution namespace (used for NameError hints).
    """
    tb_lines  = traceback.format_exception(type(exc), exc, exc.__traceback__)
    formatted = "".join(tb_lines)
    msg       = str(exc)

    # Extract the user's failing source line from the traceback.
    failing_line = _extract_failing_line(exc, code)

    # Build contextual hints.
    hints = []

    # Missing SetFrameEvent before pipeline state queries.
    is_attr_error = isinstance(exc, AttributeError)
    if is_attr_error and "SetFrameEvent" not in code:
        hints.append(
            "did you call SetFrameEvent before querying pipeline state?"
        )

    # Threading / replay controller access.
    if "BlockInvoke" in msg or "replay" in msg.lower():
        hints.append(
            "use ctx.replay(callback) to access the replay controller"
        )

    # Generic AttributeError guidance.
    if is_attr_error:
        hints.append(
            "use inspect(obj) to see available attributes, "
            "or search_api('name') to find the right API"
        )

    # SyntaxError guidance.
    if isinstance(exc, SyntaxError):
        hint = "check Python syntax near the indicated position"
        if exc.offset is not None:
            hint += f" (column {exc.offset})"
        hints.append(hint)

    # NameError with available globals.
    if isinstance(exc, NameError):
        available = sorted(
            k for k in namespace
            if not k.startswith("_")
        )
        hints.append(
            f"available names: {', '.join(available)}"
        )

    return {
        "traceback"    : formatted,
        "failing_line" : failing_line,
        "hints"        : hints,
    }


def _extract_failing_line(exc, code=""):
    """Extract the source line that caused the exception.

    Prefers frames from the user's eval code (filename ``<eval>``). Falls
    back to the last traceback frame if no eval frame is found. For syntax
    errors, the offending line is pulled from the exception itself.

    The ``<eval>`` frames have no backing source file, so linecache can't
    populate frame_summary.line. Instead we use the frame's lineno to
    index into the original source code.

    exc  -- The caught exception.
    code -- The original source code submitted by the user.
    """
    # SyntaxError carries the offending source line directly.
    if isinstance(exc, SyntaxError) and exc.text:
        return exc.text.strip()

    tb = exc.__traceback__
    if tb is None:
        return None

    code_lines = code.splitlines() if code else []

    # Walk frames, preferring <eval> frames over internal ones.
    best = None
    for frame_summary in traceback.extract_tb(tb):
        if frame_summary.filename == "<eval>":
            # Extract from original source since linecache can't find <eval>.
            lineno = frame_summary.lineno
            if code_lines and 1 <= lineno <= len(code_lines):
                best = code_lines[lineno - 1].strip()
            elif frame_summary.line:
                best = frame_summary.line
        elif best is None and frame_summary.line:
            best = frame_summary.line

    return best


def _get_serializer_map():
    """Lazily build the type-name-to-serializer dispatch table.

    Deferred because the serialize module imports renderdoc at the top
    level, which may not be available during testing.
    """
    global _SERIALIZER_MAP
    if _SERIALIZER_MAP is not None:
        return _SERIALIZER_MAP

    try:
        from . import serialize
        _SERIALIZER_MAP = {
            "ResourceId"         : serialize.resource_id,
            "ActionDescription"  : serialize.action_description,
            "TextureDescription" : serialize.texture_description,
            "BufferDescription"  : serialize.buffer_description,
            "ShaderReflection"   : serialize.shader_reflection,
            "ResourceFormat"     : serialize.format_description,
            "PipeState"          : serialize.pipeline_state,
            "APIProperties"      : serialize.api_properties,
        }
    except ImportError:
        _SERIALIZER_MAP = {}

    return _SERIALIZER_MAP


def _serialize_result(value):
    """Convert an eval result to a JSON-serializable value.

    Handles basic JSON types, RenderDoc SWIG types (via the serialize
    module), SWIG enums, and falls back to repr() for anything else.
    """
    # None passes through.
    if value is None:
        return value

    # SWIG enums: inherit from int but also expose .name. Must be checked
    # before the basic int check, since isinstance(swig_enum, int) is True.
    if isinstance(value, int) and hasattr(value, "name"):
        return {"name": value.name, "value": int(value)}

    # Basic JSON scalars pass through.
    if isinstance(value, (bool, int, float, str)):
        return value

    # Recursively serialize lists.
    if isinstance(value, list):
        return [_serialize_result(v) for v in value]

    # Recursively serialize dicts.
    if isinstance(value, dict):
        return {
            _serialize_result(k): _serialize_result(v)
            for k, v in value.items()
        }

    # Known RenderDoc types: dispatch to the matching serializer.
    type_name      = type(value).__name__
    serializer_map = _get_serializer_map()
    serializer     = serializer_map.get(type_name)
    if serializer is not None:
        try:
            return serializer(value)
        except Exception:
            # Serialization failed; fall through to repr.
            pass

    # Try attribute-based serialization for unknown SWIG types.
    # SWIG objects can throw on attribute access, so be defensive.
    try:
        attrs = {}
        for name in dir(value):
            if name.startswith("_"):
                continue
            if name in ("thisown", "this"):
                continue
            try:
                attr_val = getattr(value, name)
                if not callable(attr_val):
                    attrs[name] = _serialize_result(attr_val)
            except Exception:
                continue
        if attrs:
            return {"__type__": type_name, **attrs}
    except Exception:
        pass

    # Fallback for anything we don't recognize.
    return repr(value)
