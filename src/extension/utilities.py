"""Utility functions injected into the eval handler's namespace.

Provides runtime introspection, pipeline state diffing, data interpretation,
and UI navigation helpers. Functions that need access to the replay controller
or UI thread are bound to a HandlerContext via closures.
"""

import inspect as _inspect
import math
import struct


try:
    import renderdoc as rd
except ImportError:
    rd = None


# --- Introspection ---

# Attributes to suppress from SWIG-wrapped RenderDoc types.
_SWIG_INTERNAL = frozenset({"thisown", "this"})


def inspect_obj(obj):
    """Inspect any Python object and return a structured summary.

    For classes and instances: type name, methods (with signatures and
    first-line docstrings), and properties. Filters out dunder names and
    SWIG internal attributes.

    For enums (detected by __members__ or int inheritance): type name and
    a list of name/value pairs.

    For modules: type "module" with lists of classes, functions, and
    constants.

    Returns a plain dict, not a string.
    """
    # Enum detection: has __members__ dict or inherits from int with
    # class-level named values (SWIG enum pattern).
    if hasattr(obj, "__members__"):
        return _inspect_enum(obj)

    if isinstance(obj, type) and issubclass(obj, int) and obj is not int:
        # SWIG enums are int subclasses with class-level named constants.
        members = _extract_int_enum_members(obj)
        if members:
            return {
                "type"   : obj.__name__,
                "values" : members,
            }

    # Module detection.
    if _inspect.ismodule(obj):
        return _inspect_module(obj)

    # Class or instance.
    return _inspect_object(obj)


def _inspect_enum(obj):
    """Inspect an enum type that exposes __members__."""
    type_name = getattr(obj, "__name__", type(obj).__name__)
    members   = obj.__members__

    values = []
    for name in sorted(members):
        member = members[name]
        values.append({
            "name"  : name,
            "value" : int(member) if isinstance(member, int) else str(member),
        })

    return {
        "type"   : type_name,
        "values" : values,
    }


def _extract_int_enum_members(cls):
    """Extract named constants from a SWIG int-enum class.

    Returns a list of {name, value} dicts, or an empty list if this
    doesn't look like an enum.
    """
    members = []
    for name in dir(cls):
        if name.startswith("_"):
            continue
        val = getattr(cls, name, None)
        if isinstance(val, int):
            members.append({"name": name, "value": int(val)})
    return members


def _inspect_module(mod):
    """Inspect a module, grouping contents into classes, functions, and constants."""
    classes   = []
    functions = []
    constants = []

    for name in sorted(dir(mod)):
        if name.startswith("_"):
            continue

        attr = getattr(mod, name, None)
        if attr is None:
            continue

        if _inspect.isclass(attr):
            classes.append(name)
        elif callable(attr):
            functions.append(name)
        else:
            constants.append(name)

    return {
        "type"      : "module",
        "classes"   : classes,
        "functions" : functions,
        "constants" : constants,
    }


def _inspect_object(obj):
    """Inspect a class or instance, extracting methods and properties.

    Follows the __wrapped__ protocol for proxy objects so that the
    full API surface of the wrapped target is visible.
    """
    # Follow __wrapped__ to the real object if this is a proxy.
    # This handles _TrackedController and similar wrappers.
    wrapped = getattr(obj, "__wrapped__", None)
    if wrapped is not None:
        result = _inspect_object(wrapped)
        result["type"] = f"{type(obj).__name__} (wrapping {result['type']})"
        return result

    # Resolve the type for attribute enumeration.
    if isinstance(obj, type):
        cls       = obj
        type_name = cls.__name__
    else:
        cls       = type(obj)
        type_name = cls.__name__

    methods    = []
    properties = []

    for name in sorted(dir(obj)):
        # Skip dunder names and SWIG internals.
        if name.startswith("__") and name.endswith("__"):
            continue
        if name.startswith("_") or name in _SWIG_INTERNAL:
            continue

        attr = getattr(cls, name, None)
        if attr is None:
            # Fall back to the instance if the class doesn't have it.
            attr = getattr(obj, name, None)
            if attr is None:
                continue

        if isinstance(attr, property):
            doc = _first_line(attr.fget.__doc__) if attr.fget else None
            properties.append({"name": name, "doc": doc})
        elif callable(attr):
            sig = _get_signature(attr)
            doc = _first_line(attr.__doc__)
            methods.append({"name": name, "signature": sig, "doc": doc})
        else:
            # Might be a SWIG descriptor or similar. Treat as property.
            doc = _first_line(getattr(attr, "__doc__", None))
            properties.append({"name": name, "doc": doc})

    return {
        "type"       : type_name,
        "methods"    : methods,
        "properties" : properties,
    }


def _get_signature(func):
    """Get a function's call signature as a string.

    Tries inspect.signature first. Falls back to parsing the first line
    of the docstring for SWIG-generated signatures. Returns None if
    neither works.
    """
    try:
        return str(_inspect.signature(func))
    except (ValueError, TypeError):
        pass

    # SWIG docstrings often start with "name(args) -> return_type".
    doc = getattr(func, "__doc__", None)
    if doc:
        first = doc.strip().split("\n")[0]
        if "(" in first and ")" in first:
            return first

    return None


def _first_line(doc):
    """Return the first non-empty line of a docstring, or None."""
    if not doc:
        return None
    for line in doc.strip().split("\n"):
        stripped = line.strip()
        if stripped:
            return stripped
    return None


# --- Pipeline State Diffing ---

def _deep_diff(a, b):
    """Recursively diff two dicts, returning only changed paths.

    For nested dicts, recurses and only includes keys whose subtrees
    contain actual changes. For non-dict values, compares directly and
    returns {"before": a, "after": b} when they differ.

    Returns None if the two values are equal.
    """
    if type(a) is dict and type(b) is dict:
        diff = {}
        all_keys = set(a) | set(b)

        for key in sorted(all_keys, key=str):
            val_a = a.get(key)
            val_b = b.get(key)

            if val_a is None and val_b is not None:
                diff[key] = {"before": None, "after": val_b}
            elif val_a is not None and val_b is None:
                diff[key] = {"before": val_a, "after": None}
            else:
                sub = _deep_diff(val_a, val_b)
                if sub is not None:
                    diff[key] = sub

        return diff if diff else None

    # Lists: compare element-wise. If lengths differ or any element
    # differs, report the whole list as changed.
    if type(a) is list and type(b) is list:
        if a == b:
            return None
        return {"before": a, "after": b}

    # Scalar comparison.
    if a != b:
        return {"before": a, "after": b}

    return None


def _annotate_resource_names(diff, name_map):
    """Walk a diff dict and annotate leaf values that are resource IDs.

    For each leaf {"before": x, "after": y}, if x or y is a string
    present in name_map, replaces it with {"id": x, "name": ...}.
    Recurses into nested dicts that aren't before/after leaves.

    Mutates diff in place and returns it.

    diff     -- Diff dict from _deep_diff.
    name_map -- Dict mapping serialized resource ID strings to names.
    """
    if not isinstance(diff, dict) or not name_map:
        return diff

    # Detect before/after leaf nodes.
    is_leaf = "before" in diff and "after" in diff and len(diff) == 2

    if is_leaf:
        for key in ("before", "after"):
            val = diff[key]
            if isinstance(val, str) and val in name_map:
                diff[key] = {"id": val, "name": name_map[val]}
    else:
        for val in diff.values():
            if isinstance(val, dict):
                _annotate_resource_names(val, name_map)

    return diff


def make_diff_state(ctx):
    """Create a diff_state function bound to the given HandlerContext.

    The returned function captures ctx and manages the replay callback
    internally, so the caller just passes two event IDs.

    ctx -- HandlerContext with replay() access.
    """
    def diff_state(eid_a, eid_b):
        """Diff pipeline state between two events.

        Moves the replay cursor to each event, snapshots the full
        pipeline state via serialize.pipeline_state(), and returns a
        recursive diff containing only the keys that changed.

        Safe to call both inside and outside ctx.replay() callbacks.

        eid_a -- First event ID.
        eid_b -- Second event ID.
        """
        from . import serialize

        def _snapshot_push_constants(controller):
            """Try to capture Vulkan push constant data.

            Returns the raw bytes as a hex string, or None for
            non-Vulkan captures or if the API is unavailable.
            """
            try:
                vk_state = controller.GetVulkanPipelineState()
                data     = vk_state.pushconsts
                if data:
                    return data.hex()
            except Exception:
                pass
            return None

        def _snapshot_both(controller):
            controller.SetFrameEvent(eid_a, True)
            state_a = serialize.pipeline_state(controller.GetPipelineState())
            push_a  = _snapshot_push_constants(controller)

            controller.SetFrameEvent(eid_b, True)
            state_b = serialize.pipeline_state(controller.GetPipelineState())
            push_b  = _snapshot_push_constants(controller)

            # Attach push constants alongside pipeline state so they
            # show up in the diff when they change between events.
            if push_a is not None:
                state_a["push_constants"] = push_a
            if push_b is not None:
                state_b["push_constants"] = push_b

            # Build a mapping from serialized resource ID strings to
            # human-readable names. Only includes resources that have
            # a non-empty name.
            name_map = {}
            for res in controller.GetResources():
                key = serialize.resource_id(res.resourceId)
                if res.name:
                    name_map[key] = res.name

            return (state_a, state_b, name_map)

        # If already on the replay thread, use the active controller.
        controller = ctx._replay_controller
        if controller is not None:
            state_a, state_b, name_map = _snapshot_both(controller)
        else:
            state_a, state_b, name_map = ctx.replay(_snapshot_both)

        diff = _deep_diff(state_a, state_b)
        if diff is None:
            return {}

        _annotate_resource_names(diff, name_map)
        return diff

    return diff_state


# --- Data Interpretation ---

# Integer format codes indexed by byte width. Used when the component
# type is an integer variant and the byte width isn't the default 4.
_UINT_BY_WIDTH = {1: "B", 2: "H", 4: "I", 8: "Q"}
_SINT_BY_WIDTH = {1: "b", 2: "h", 4: "i", 8: "q"}


def interpret_buffer(data, fmt):
    """Decode raw buffer bytes into typed values.

    data -- bytes from GetBufferData.
    fmt  -- either a renderdoc.ResourceFormat object or a dict with keys:
            component_type (str), component_count (int),
            component_byte_width (int).

    Returns a list of values for single-component formats, or a list of
    tuples for multi-component formats.
    """
    if isinstance(fmt, dict):
        comp_type  = fmt.get("component_type", "Float")
        comp_count = fmt.get("component_count", 1)
        comp_width = fmt.get("component_byte_width", 4)
    else:
        # ResourceFormat object.
        comp_type  = fmt.compType.name if hasattr(fmt.compType, "name") else str(fmt.compType)
        comp_count = fmt.compCount
        comp_width = fmt.compByteWidth

    # Determine the struct format character.
    if comp_type in ("UInt", "UByte"):
        fmt_char = _UINT_BY_WIDTH.get(comp_width, "I")
    elif comp_type in ("SInt", "SByte"):
        fmt_char = _SINT_BY_WIDTH.get(comp_width, "i")
    elif comp_type == "Double":
        fmt_char = "d"
    else:
        # Float, UNorm, SNorm all decode as float.
        fmt_char = "f"

    stride      = comp_count * comp_width
    elem_count  = len(data) // stride if stride > 0 else 0
    pack_fmt    = f"<{comp_count}{fmt_char}"

    result = []
    for i in range(elem_count):
        offset = i * stride
        values = struct.unpack_from(pack_fmt, data, offset)

        if comp_count == 1:
            result.append(values[0])
        else:
            result.append(values)

    return result


def summarize_data(values):
    """Summarize a flat list of numbers.

    Returns a dict with min, max, mean, count, nan_count, and inf_count.
    Useful for quick inspection of buffer or texture data.

    values -- flat list (or iterable) of numeric values.
    """
    total     = 0.0
    count     = 0
    nan_count = 0
    inf_count = 0
    lo        = float("inf")
    hi        = float("-inf")

    for v in values:
        count += 1
        fv     = float(v)

        if math.isnan(fv):
            nan_count += 1
            continue
        if math.isinf(fv):
            inf_count += 1
            continue

        total += fv
        if fv < lo:
            lo = fv
        if fv > hi:
            hi = fv

    finite_count = count - nan_count - inf_count
    mean         = (total / finite_count) if finite_count > 0 else None

    # If no finite values were seen, min/max are undefined.
    if finite_count == 0:
        lo = None
        hi = None

    return {
        "min"       : lo,
        "max"       : hi,
        "mean"      : mean,
        "count"     : count,
        "nan_count" : nan_count,
        "inf_count" : inf_count,
    }


# --- Action Flags ---

def action_flags(flags):
    """Decode an ActionDescription flags bitmask into human-readable names.

    Introspects rd.ActionFlags to discover all known flag members, then
    tests each bit against the provided value.

    flags -- Integer bitmask from ActionDescription.flags.

    Returns a list of flag name strings that are set in the value.
    """
    if rd is None:
        return []

    af = rd.ActionFlags

    # Build the member list by introspection. Prefer __members__ if the
    # SWIG wrapper exposes it, otherwise fall back to scanning class
    # attributes for int-valued constants.
    if hasattr(af, "__members__"):
        members = [(name, int(val)) for name, val in af.__members__.items()]
    else:
        members = []
        for name in dir(af):
            if name.startswith("_"):
                continue
            val = getattr(af, name, None)
            if isinstance(val, int):
                members.append((name, int(val)))

    flags = int(flags)
    return [name for name, bit in members if bit != 0 and (flags & bit) == bit]


# --- Push Constants ---

def decode_push_constants(controller, stage):
    """Decode Vulkan push constant bytes against shader reflection.

    Reads the raw push constant data from the Vulkan pipeline state and
    attempts to decode it using the shader reflection for the given stage.
    Falls back to a hex dump if reflection is unavailable or the capture
    is not Vulkan.

    Must be called inside a ctx.replay() callback with a live controller.

    controller -- ReplayController (or TrackedController proxy).
    stage      -- rd.ShaderStage value (e.g., rd.ShaderStage.Vertex).

    Returns a dict with stage name, raw hex string, and decoded variables
    (if reflection was available).
    """
    from . import serialize

    stage_name = stage.name if hasattr(stage, "name") else str(stage)
    result     = {"stage": stage_name, "raw_hex": None, "decoded": None}

    # Read push constant bytes from the Vulkan-specific state.
    try:
        vk_state = controller.GetVulkanPipelineState()
        data     = vk_state.pushconsts
    except Exception:
        # Not a Vulkan capture or API unavailable.
        return result

    if not data:
        return result

    result["raw_hex"] = data.hex()

    # Attempt reflection-based decode.
    try:
        state = controller.GetPipelineState()
        refl  = state.GetShaderReflection(stage)
        if refl and refl.constantBlocks:
            result["decoded"] = serialize.cbuffer_variables(
                refl.constantBlocks[0].variables, data
            )
    except Exception:
        pass

    return result


# --- Action Tree ---

def make_get_draw_calls(ctx):
    """Create a get_draw_calls function bound to the given HandlerContext.

    The returned closure walks the action tree and collects all leaf draw
    calls. This is the most commonly needed boilerplate when exploring a
    capture.

    ctx -- HandlerContext with replay() access and structured_file.
    """
    def get_draw_calls(controller=None):
        """Collect all leaf draw calls in the frame.

        Recursively walks the action tree from GetRootActions(), filtering
        for actions with the Drawcall flag set. Returns a flat list of
        dicts with eventId and name.

        Safe to call both inside and outside ctx.replay() callbacks.

        controller -- Optional ReplayController. If None, dispatches via
                      ctx.replay() automatically.

        Returns a list of {"eventId": int, "name": str}.
        """
        def _collect(ctrl):
            def _recurse(actions):
                draws = []
                for action in actions:
                    if action.flags & rd.ActionFlags.Drawcall:
                        draws.append({
                            "eventId" : action.eventId,
                            "name"    : action.GetName(ctx.structured_file),
                        })
                    draws.extend(_recurse(action.children))
                return draws
            return _recurse(ctrl.GetRootActions())

        # If a controller was passed explicitly, use it directly.
        if controller is not None:
            return _collect(controller)

        # Re-entrant check: if already on the replay thread, use the
        # active controller to avoid deadlocking.
        active = ctx._replay_controller
        if active is not None:
            return _collect(active)

        return ctx.replay(_collect)

    return get_draw_calls


def make_get_all_actions(ctx):
    """Create a get_all_actions function bound to the given HandlerContext.

    The returned closure walks the entire action tree and returns every
    node (markers, draws, dispatches, clears, copies, etc.) as a flat
    list. Useful for frame structure exploration and finding non-draw
    events like clears or copies.

    ctx -- HandlerContext with replay() access and structured_file.
    """
    def get_all_actions(controller=None):
        """Collect all actions in the frame as a flat list.

        Recursively walks the action tree from GetRootActions(), emitting
        every node (not just draw calls). Each entry includes the event
        ID, name, and decoded flags.

        Safe to call both inside and outside ctx.replay() callbacks.

        controller -- Optional ReplayController. If None, dispatches via
                      ctx.replay() automatically.

        Returns a list of {"eventId": int, "name": str, "flags": [str]}.
        """
        def _collect(ctrl):
            def _recurse(actions):
                result = []
                for a in actions:
                    result.append({
                        "eventId" : a.eventId,
                        "name"    : a.GetName(ctx.structured_file),
                        "flags"   : action_flags(a.flags),
                    })
                    result.extend(_recurse(a.children))
                return result
            return _recurse(ctrl.GetRootActions())

        # If a controller was passed explicitly, use it directly.
        if controller is not None:
            return _collect(controller)

        # Re-entrant check: if already on the replay thread, use the
        # active controller to avoid deadlocking.
        active = ctx._replay_controller
        if active is not None:
            return _collect(active)

        return ctx.replay(_collect)

    return get_all_actions


# --- Draw Call Summary ---

def make_describe_draw(ctx):
    """Create a describe_draw function bound to the given HandlerContext.

    The returned closure provides a one-shot comprehensive summary of a
    single draw call, gathering pipeline state, bound resources, and draw
    parameters into one dict.

    ctx -- HandlerContext with replay() access and structured_file.
    """
    def describe_draw(controller=None, eventId=None):
        """Summarize pipeline state and draw parameters at an event.

        Moves the replay cursor to the given event, snapshots the full
        pipeline state, and assembles a comprehensive summary including
        bound shaders, render targets, vertex/index buffers, draw
        parameters, and push constants (Vulkan).

        Safe to call both inside and outside ctx.replay() callbacks.

        controller -- Optional ReplayController. If None, dispatches via
                      ctx.replay() automatically.
        eventId    -- Event ID to inspect. Required.

        Returns a dict with event_id, name, shaders, render_targets,
        depth_target, draw_params, vertex_buffers, index_buffer, and
        push_constants.
        """
        from . import serialize

        if eventId is None:
            return {"error": "eventId is required"}

        def _describe(ctrl):
            ctrl.SetFrameEvent(eventId, True)
            state = ctrl.GetPipelineState()

            # Find the action to get its name and draw parameters.
            action = _find_action(ctrl.GetRootActions(), eventId)
            name   = action.GetName(ctx.structured_file) if action else None

            # Bound shaders by stage.
            stages = [
                ("vs", rd.ShaderStage.Vertex),
                ("hs", rd.ShaderStage.Hull),
                ("ds", rd.ShaderStage.Domain),
                ("gs", rd.ShaderStage.Geometry),
                ("ps", rd.ShaderStage.Pixel),
                ("cs", rd.ShaderStage.Compute),
            ]
            shaders = {}
            for label, stage in stages:
                shader = state.GetShader(stage)
                if int(shader) != 0:
                    shaders[label] = serialize.resource_id(shader)

            # Render targets.
            render_targets = []
            try:
                for rt in state.GetOutputTargets():
                    if int(rt.resource) != 0:
                        render_targets.append(serialize.resource_id(rt.resource))
            except Exception:
                pass

            # Depth target.
            depth_target = None
            try:
                depth = state.GetDepthTarget()
                if depth and int(depth.resource) != 0:
                    depth_target = serialize.resource_id(depth.resource)
            except Exception:
                pass

            # Draw parameters from the action.
            draw_params = None
            if action and (action.flags & rd.ActionFlags.Drawcall):
                draw_params = {
                    "numIndices"     : action.numIndices,
                    "numInstances"   : action.numInstances,
                    "indexOffset"    : action.indexOffset,
                    "baseVertex"     : action.baseVertex,
                    "instanceOffset" : action.instanceOffset,
                }

            # Vertex buffers.
            vertex_buffers = []
            try:
                for vb in state.GetVBuffers():
                    if int(vb.resourceId) != 0:
                        vertex_buffers.append({
                            "resource" : serialize.resource_id(vb.resourceId),
                            "offset"   : vb.byteOffset,
                            "stride"   : vb.byteStride,
                        })
            except Exception:
                pass

            # Index buffer.
            index_buffer = None
            try:
                ib = state.GetIBuffer()
                if int(ib.resourceId) != 0:
                    index_buffer = {
                        "resource" : serialize.resource_id(ib.resourceId),
                        "offset"   : ib.byteOffset,
                        "stride"   : ib.byteStride,
                    }
            except Exception:
                pass

            # Push constants (Vulkan only).
            push_constants = None
            try:
                vk_state = ctrl.GetVulkanPipelineState()
                data     = vk_state.pushconsts
                if data:
                    push_constants = data.hex()
            except Exception:
                pass

            return {
                "event_id"       : eventId,
                "name"           : name,
                "shaders"        : shaders,
                "render_targets" : render_targets,
                "depth_target"   : depth_target,
                "draw_params"    : draw_params,
                "vertex_buffers" : vertex_buffers,
                "index_buffer"   : index_buffer,
                "push_constants" : push_constants,
            }

        # If a controller was passed explicitly, use it directly.
        if controller is not None:
            return _describe(controller)

        # Re-entrant check: if already on the replay thread, use the
        # active controller to avoid deadlocking.
        active = ctx._replay_controller
        if active is not None:
            return _describe(active)

        return ctx.replay(_describe)

    return describe_draw


def _find_action(actions, eventId):
    """Recursively search the action tree for an action by event ID.

    actions -- List of ActionDescription from GetRootActions() or .children.
    eventId -- Target event ID.

    Returns the matching ActionDescription, or None if not found.
    """
    for action in actions:
        if action.eventId == eventId:
            return action
        found = _find_action(action.children, eventId)
        if found is not None:
            return found
    return None


# --- Resource Lookup ---

def make_get_resource_name(ctx):
    """Create a get_resource_name function bound to the given HandlerContext.

    The returned closure looks up the human-readable name of a resource
    by its ResourceId. Results are cached because the resource list is
    fixed within a single capture.

    ctx -- HandlerContext with replay() access.
    """
    cache = {}

    def _build_cache(controller):
        """Fetch all resources and populate the name cache."""
        for res in controller.GetResources():
            cache[res.resourceId] = res.name

    def get_resource_name(resource_id):
        """Return the human-readable name of a RenderDoc resource.

        Safe to call both inside and outside ctx.replay() callbacks.
        If already on the replay thread (inside a callback), uses the
        active controller directly. Otherwise dispatches via ctx.replay().

        resource_id -- ResourceId to look up.
        """
        if not cache:
            # If we're already inside a BlockInvoke callback, use the
            # active controller directly to avoid deadlocking.
            controller = ctx._replay_controller
            if controller is not None:
                _build_cache(controller)
            else:
                ctx.replay(_build_cache)

        return cache.get(resource_id, f"<unknown {resource_id}>")

    return get_resource_name


# --- UI Helpers ---

def make_goto_event(ctx):
    """Create a goto_event function bound to the given HandlerContext.

    ctx -- HandlerContext with invoke_ui() access.
    """
    def goto_event(eid):
        """Navigate the RenderDoc UI to the specified event.

        eid -- Event ID to navigate to.
        Returns a dict confirming the navigation.
        """
        def _nav():
            ctx.ctx.SetEventID([], eid, eid)

        ctx.invoke_ui(_nav)
        return {"navigated_to": eid}

    return goto_event


def make_view_texture(ctx):
    """Create a view_texture function bound to the given HandlerContext.

    ctx -- HandlerContext with invoke_ui() access.
    """
    def view_texture(resource_id):
        """Open the texture viewer for the given resource.

        resource_id -- ResourceId to display.
        """
        def _view():
            pyrenderdoc = ctx.ctx
            if hasattr(pyrenderdoc, "ViewTextureDisplay"):
                pyrenderdoc.ViewTextureDisplay(resource_id)
            elif hasattr(pyrenderdoc, "ShowTextureViewer"):
                pyrenderdoc.ShowTextureViewer()

        ctx.invoke_ui(_view)
        return {"viewing_texture": True}

    return view_texture


def make_highlight_drawcall(ctx):
    """Create a highlight_drawcall function bound to the given HandlerContext.

    ctx -- HandlerContext with invoke_ui() access.
    """
    def highlight_drawcall(eid):
        """Navigate the RenderDoc UI to highlight a draw call.

        Equivalent to goto_event. Navigates the event browser to the
        specified event ID so the draw call is selected and visible.

        eid -- Event ID of the draw call.
        """
        def _nav():
            ctx.ctx.SetEventID([], eid, eid)

        ctx.invoke_ui(_nav)
        return {"highlighted": eid}

    return highlight_drawcall


# --- Binding ---

def bind_utilities(ctx):
    """Create all utility functions bound to the given HandlerContext.

    Returns a dict suitable for merging into the eval handler's
    execution namespace. Functions that need the replay controller or
    UI thread are pre-bound to ctx via closures. Stateless helpers are
    included directly.

    ctx -- HandlerContext providing replay() and invoke_ui().
    """
    return {
        "inspect"              : inspect_obj,
        "diff_state"           : make_diff_state(ctx),
        "get_resource_name"    : make_get_resource_name(ctx),
        "get_draw_calls"       : make_get_draw_calls(ctx),
        "get_all_actions"      : make_get_all_actions(ctx),
        "describe_draw"        : make_describe_draw(ctx),
        "goto_event"           : make_goto_event(ctx),
        "view_texture"         : make_view_texture(ctx),
        "highlight_drawcall"   : make_highlight_drawcall(ctx),
        "interpret_buffer"     : interpret_buffer,
        "summarize_data"       : summarize_data,
        "action_flags"         : action_flags,
        "decode_push_constants" : decode_push_constants,
    }
