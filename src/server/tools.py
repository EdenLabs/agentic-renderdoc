"""MCP tool definitions for eval, search_api, and instance."""

from server.app import mcp
from server.client import RenderDocClient

_client = RenderDocClient()


# --- eval ---

@mcp.tool(name="Eval")
def eval(code: str) -> dict:
    """Execute Python code in a live RenderDoc replay session.

    This is your primary interface for all GPU capture inspection, analysis,
    and debugging. Code runs inside RenderDoc's embedded Python interpreter
    with full access to the replay engine.

    ACCESS MODEL
    ============
    The global `ctx` (HandlerContext) provides thread-safe replay access.
    To query replay state, use ctx.replay(callback):

        def work(controller):
            controller.SetFrameEvent(eventId, True)
            state = controller.GetPipelineState()
            # ... query state ...
            return result
        ctx.replay(work)

    ctx.replay() runs your callback on the replay thread with a
    ReplayController argument, returns the callback's return value, and
    properly propagates exceptions. You MUST use this pattern for any
    ReplayController access.

    CURSOR MODEL
    ============
    The replay engine maintains a cursor position in the frame's event
    timeline. All state queries return state AT the current cursor position.

    - controller.SetFrameEvent(eventId, True) moves the cursor.
    - You MUST call SetFrameEvent before calling GetPipelineState or any
      other state query. Forgetting this is the most common mistake.
      WARNING: GetPipelineState() will NOT error without SetFrameEvent —
      it silently returns stale state from whatever event was last active.
      Always call SetFrameEvent first inside every ctx.replay() callback.
    - The second argument (True) forces full pipeline state resolution.
    - goto_event(eid) navigates the RenderDoc UI to an event. It does NOT
      move the replay cursor. Only SetFrameEvent(eventId, True) inside a
      ctx.replay() callback sets the replay cursor. Pipeline state queries
      always reflect the last SetFrameEvent call, not goto_event.

    OBJECT GRAPH
    ============
    ReplayController is the central hub. Key accessors:

    Actions (draw calls, dispatches, markers):
        controller.GetRootActions() -> list of ActionDescription
        Each action has:
            .eventId    -- unique event ID (use with SetFrameEvent)
            .actionId   -- action index
            .flags      -- ActionFlags bitmask (Drawcall, Dispatch, etc.)
            .children   -- list of child actions (markers contain children)
            .next       -- next sibling action (or None)
            .previous   -- previous sibling action (or None)
            .customName -- user-defined marker name (empty string if none)
            .GetName(ctx.structured_file) -- formatted display name
                (e.g., "vkCmdDrawIndexed(36, 1, 0, 0, 0)"). Always
                prefer this over customName for human-readable names.
            .numIndices, .numInstances, .indexOffset, .baseVertex
            .dispatchDimension -- [x, y, z] for compute dispatches

    Pipeline state (after SetFrameEvent):
        controller.GetPipelineState() -> PipeState
        PipeState is API-agnostic. Key methods:
            .GetShader(stage)                   -> ResourceId
                WARNING: GetShader(Compute) at a graphics draw call
                returns the stale shader from the last dispatch, not
                Null(). The serialize module filters this automatically,
                but raw GetShader calls will see the stale ID. Check
                the action's flags to know whether CS is relevant.
            .GetShaderReflection(stage)         -> ShaderReflection
            .GetOutputTargets()                 -> list of Descriptor (direct)
                rt.resource, rt.format, rt.firstMip, rt.numMips, etc.
            .GetDepthTarget()                   -> Descriptor (direct)
                depth.resource, depth.format, etc.
            .GetReadOnlyResources(stage)        -> list of UsedDescriptor
            .GetReadWriteResources(stage)       -> list of UsedDescriptor
            .GetConstantBlocks(stage)           -> list of UsedDescriptor
                UsedDescriptor wraps a Descriptor in .descriptor:
                    ud.descriptor.resource   -- ResourceId
                    ud.descriptor.byteOffset -- offset in buffer
                    ud.descriptor.byteSize   -- size in bytes
                Note: on Vulkan, VK_WHOLE_SIZE maps to byteSize =
                18446744073709551615 (u64::MAX). This does NOT mean the
                buffer is that large. Read the buffer's actual length
                from controller.GetBuffers() and clamp accordingly.

                UsedDescriptor also has an .access (DescriptorAccess) field:
                    ud.access.arrayElement   -- index into the descriptor array
                    ud.access.descriptorStore -- ResourceId of the backing store
                    ud.access.stage          -- ShaderStage that accessed this
                    ud.access.type           -- DescriptorType enum
                For bindless renderers, GetReadWriteResources/GetReadOnlyResources
                return only the descriptors actually accessed by the draw call.
                Use ud.access.arrayElement to map back to the original array index.

        Shader reflection containers:
            refl.constantBlocks[i] is a ConstantBlock:
                .name, .fixedBindNumber, .fixedBindSetOrSpace, .variables
            refl.readOnlyResources[i] / readWriteResources[i] is a ShaderResource:
                .name, .fixedBindNumber, .fixedBindSetOrSpace
            .GetViewport(index)                 -> viewport rect
            .GetScissor(index)                  -> scissor rect
            .GetPrimitiveTopology()             -> topology enum
            .GetColorBlends()                   -> per-target blend state
            .GetStencilFaces()                  -> (front, back) stencil state
            .GetIBuffer()                       -> index buffer binding
            .GetVBuffers()                      -> vertex buffer bindings

        Depth/stencil test configuration (enable, writes, compare function)
        is NOT available through the API-agnostic PipeState. Use the
        API-specific state object instead:
            controller.GetVulkanPipelineState().depthStencil
            controller.GetD3D11PipelineState().outputMerger.depthStencilState

        Push constant data (Vulkan only):
            controller.GetVulkanPipelineState().pushconsts -> bytes
            Decode with struct.unpack. Typically contains descriptor
            indices or buffer offsets in bindless renderers.

    Raw data access:
        controller.GetBufferData(resourceId, offset, length) -> bytes
        controller.GetTextureData(resourceId, subresource)   -> bytes
            subresource is an rd.Subresource(mip, slice, sample).
            For the base mip of the first slice: rd.Subresource(0, 0, 0).

    Resource metadata:
        controller.GetTextures()  -> list of TextureDescription
            Note: TextureDescription does not carry names. Use
            get_resource_name(resource_id) to look up human-readable names.
        controller.GetBuffers()   -> list of BufferDescription
        controller.GetResources() -> list of ResourceDescription

        Note: ResourceFormat uses .Name() (method) not .name (property)
        for the format name string. The serialize module handles this
        automatically.

        Note: ResourceId is a one-way opaque handle. You can convert to
        int via int(rid) or to string via serialize.resource_id(rid),
        but there is no way to reconstruct a ResourceId from an integer.
        Always hold onto live ResourceId objects within your ctx.replay()
        callback rather than serializing and trying to reconstruct later.

    ACTION TREE
    ===========
    The action list is hierarchical. Debug markers (PushMarker/PopMarker)
    create parent-child relationships. Actual GPU work lives in leaf nodes.

    To find all draw calls, recurse through children:

        def find_draws(actions):
            draws = []
            for a in actions:
                if a.flags & rd.ActionFlags.Drawcall:
                    draws.append(a)
                draws.extend(find_draws(a.children))
            return draws

    Use .next and .previous for sequential traversal within a level.

    KEY ENUMS
    =========
    Import as `rd.EnumName.Value` (the `rd` module is pre-loaded as
    `import renderdoc as rd`).

    ShaderStage:
        Vertex, Hull, Domain, Geometry, Pixel, Compute
        (Fragment is an alias for Pixel)

    ActionFlags (bitmask -- use & to test):
        Drawcall, Dispatch, Clear, Copy, Resolve, Present,
        PushMarker, PopMarker, SetMarker,
        Indexed, Instanced, Indirect,
        ClearColor, ClearDepthStencil,
        BeginPass, EndPass, PassBoundary
        Note: PassBoundary marks both Vulkan render pass boundaries
        AND command buffer boundaries. To distinguish, check the
        action name (e.g., "vkCmdBeginRenderPass" vs
        "vkBeginCommandBuffer").

    MeshDataStage:
        VSIn, VSOut

    SHADER REFLECTION TYPES
    =======================
    ShaderReflection.constantBlocks[i].variables[j].type is a
    ShaderConstantType with:
        .baseType   -- VarType enum (Float, Int, UInt, etc.)
        .rows       -- number of rows (1 for scalars/vectors)
        .columns    -- number of columns
        .elements   -- array length (0 if not an array)
        .members    -- list of sub-variables (for structs)

    AVAILABLE GLOBALS AND UTILITIES
    ===============================
    These are pre-loaded in the execution environment:

    Modules:
        rd           -- the renderdoc module (import renderdoc as rd)
        qrd          -- the qrenderdoc module (UI types)
        ctx          -- HandlerContext:
                        ctx.replay(callback) for replay access
                        ctx.structured_file  for ActionDescription.GetName()
        serialize    -- type serialization (see below)

    Functions:
        inspect(obj)
            Introspect any RenderDoc object to discover its methods,
            properties, and their docstrings. Use this when you are
            unsure what an object supports. Returns structured info.

        diff_state(eid_a, eid_b)
            Diff pipeline state between two events. Returns a structured
            diff showing what changed (shaders, render targets, blend,
            depth, bound resources, etc.).

        interpret_buffer(data, fmt)
            Decode raw bytes from GetBufferData into typed values.
            fmt is a ResourceFormat object or a dict with keys:
            component_type, component_count, component_byte_width.

        summarize_data(values)
            Compute min, max, mean, count, nan_count, inf_count over
            a flat list of numbers. Quick buffer/texture inspection.

        action_flags(flags)
            Decode an ActionDescription.flags int into a list of flag name strings.

        goto_event(eid)
            Navigate the RenderDoc UI to a specific event.

        view_texture(resource_id)
            Open the texture viewer for a resource.

        highlight_drawcall(eid)
            Alias for goto_event. Both call SetEventID under the hood.
            Use whichever name reads better in context.

        get_resource_name(resource_id)
            Look up the human-readable name of a resource by its ResourceId.
            Names come from ResourceDescription, not TextureDescription or
            BufferDescription.

        get_draw_calls()
            Collect all leaf draw calls in the frame. Returns a flat list
            of {"eventId": int, "name": str}. Handles the recursive action
            tree walk internally. Works both inside and outside ctx.replay().

        get_all_actions()
            Flat walk of the entire action tree (markers, draws, dispatches,
            clears, copies, etc.). Returns a list of {"eventId": int,
            "name": str, "flags": [str]}. Useful for frame structure
            exploration. Works both inside and outside ctx.replay().

        describe_draw(eventId=eid)
            One-shot comprehensive summary of a draw call. Returns event_id,
            name, shaders, render_targets, depth_target, draw_params,
            vertex_buffers, index_buffer, and push_constants in a single
            dict. Works both inside and outside ctx.replay().

        decode_push_constants(controller, stage)
            Decode Vulkan push constant bytes against shader reflection.
            Must be called inside a ctx.replay() callback. Returns a dict
            with stage name, raw_hex string, and decoded variables list.

    Serialization:
        The `serialize` module converts RenderDoc C++ types to plain
        dicts for JSON transport. Useful functions:
            serialize.pipeline_state(state)    -> dict
            serialize.action_description(act)  -> dict
            serialize.shader_reflection(refl)  -> dict
            serialize.texture_description(tex) -> dict
            serialize.buffer_description(buf)  -> dict
            serialize.format_description(fmt)  -> dict
            serialize.resource_id(rid)         -> str
            serialize.cbuffer_variables(vars, data) -> list of dicts

    RETURN CONVENTION
    =================
    - The last expression in your code block is captured and returned as
      the result. You do not need to assign it or call return.
    - Return dicts or lists for structured data.
    - print() output is also captured and included in the response.
    - ctx.replay(callback) returns the callback's return value directly:
          def work(controller):
              ...
              return data
          ctx.replay(work)  # <-- last expression, becomes the result

    EXAMPLES
    ========

    1. List all draw calls in the frame:

        get_draw_calls()

       Or manually (equivalent to what get_draw_calls does internally):

        def work(controller):
            def find_draws(actions):
                draws = []
                for a in actions:
                    if a.flags & rd.ActionFlags.Drawcall:
                        draws.append({
                            "eventId": a.eventId,
                            "name": a.GetName(ctx.structured_file),
                        })
                    draws.extend(find_draws(a.children))
                return draws
            return find_draws(controller.GetRootActions())
        ctx.replay(work)

    2. Inspect pipeline state at a specific event:

        def work(controller):
            controller.SetFrameEvent(42, True)
            state = controller.GetPipelineState()
            return serialize.pipeline_state(state)
        ctx.replay(work)

    3. Read constant buffer data for the pixel shader at event 100:

        import struct
        def work(controller):
            controller.SetFrameEvent(100, True)
            state = controller.GetPipelineState()
            cbs = state.GetConstantBlocks(rd.ShaderStage.Pixel)
            if cbs and cbs[0].descriptor.resource != rd.ResourceId.Null():
                desc = cbs[0].descriptor
                data = controller.GetBufferData(desc.resource, desc.byteOffset, desc.byteSize)
                refl = state.GetShaderReflection(rd.ShaderStage.Pixel)
                if refl and refl.constantBlocks:
                    return serialize.cbuffer_variables(
                        refl.constantBlocks[0].variables, data
                    )
            return "no constant buffers bound"
        ctx.replay(work)

    4. Discover what methods a pipeline state object has:

        def work(controller):
            controller.SetFrameEvent(42, True)
            state = controller.GetPipelineState()
            return inspect(state)
        ctx.replay(work)

    5. Summarize a specific draw call:

        describe_draw(eventId=42)

    6. Decode push constants for the vertex shader at an event:

        def work(controller):
            controller.SetFrameEvent(100, True)
            return decode_push_constants(controller, rd.ShaderStage.Vertex)
        ctx.replay(work)

    ERRORS
    ======
    On failure, the response includes:
    - traceback:    full formatted traceback
    - failing_line: the specific source line that failed
    - hints:        contextual suggestions (e.g., "did you call
                    SetFrameEvent before querying pipeline state?")

    If you get an AttributeError, use inspect(obj) to see what is
    actually available, or use the search_api tool to look up the
    correct method name.
    """
    try:
        return _client.send("eval", {"code": code})
    except (TimeoutError, OSError) as e:
        return {
            "ok"    : False,
            "error" : {
                "message" : f"Connection to RenderDoc timed out: {e}",
                "hints"   : [
                    "use instance(action='list') to check RenderDoc connectivity",
                    "RenderDoc may have closed or the capture may have changed",
                ],
            },
        }


# --- search_api ---

@mcp.tool(name="Search-API")
def search_api(query: str) -> dict:
    """Search the RenderDoc Python API reference by name or concept.

    Use this tool for discovery: finding what API exists for a task,
    looking up exact method signatures, checking parameter types, or
    exploring enum values. The index is built by introspecting the live
    renderdoc module, so it always matches the running RenderDoc version.

    query: A class name, method name, enum name, or concept keyword.
           Examples: "SetFrameEvent", "ShaderStage", "GetBufferData",
                     "constant buffer", "blend".

    Returns a JSON array of matching entries ranked by relevance. Each entry:
        name:      Fully qualified name (e.g., "ReplayController.SetFrameEvent")
        kind:      "class", "method", "property", "enum", or "enum_value"
        doc:       Full RST-formatted docstring with param/type/return info
        signature: Method signature string, if applicable (e.g., "(eventId, force)")
    """
    return _client.send("api_index", {"query": query})


# --- instance ---

@mcp.tool(name="Instance")
def instance(action: str, port: int | None = None) -> dict:
    """Manage connections to running RenderDoc instances.

    Lists available instances, connects to a specific one, or disconnects.
    On first use, automatically connects to the first available instance.

    action: One of "list", "connect", "disconnect".
    port: Port to connect to. Required for "connect".
    """
    if action == "list":
        return _enrich_instances(_client.discover_instances())
    elif action == "connect":
        if port is None:
            return {"error": "port is required for connect"}
        _client.connect(port)
        info   = _client.send("instance_info", {})
        others = [
            inst for inst in _client.discover_instances()
            if inst["port"] != port
        ]
        if others:
            info["other_instances"] = _enrich_instances(others)["instances"]
        return info
    elif action == "disconnect":
        _client.disconnect()
        return {"status": "disconnected"}
    else:
        return {"error": f"unknown action: {action}"}


def _enrich_instances(instances: list[dict]) -> dict:
    """Probe each discovered instance for metadata.

    Attempts a temporary connection to each instance to fetch instance_info
    (capture state, API type, etc.). Falls back to port-only info if the
    probe fails.
    """
    enriched = []
    for inst in instances:
        port = inst["port"]
        # If we are already connected to this port, query directly.
        if _client._port == port and _client._sock is not None:
            try:
                info = _client.send("instance_info", {})
                if info.get("ok") and "data" in info:
                    enriched.append(info["data"])
                else:
                    enriched.append({"port": port})
            except Exception:
                enriched.append({"port": port})
            continue

        # Otherwise, open a temporary connection to probe.
        probe = RenderDocClient()
        try:
            probe.connect(port)
            info = probe.send("instance_info", {})
            if info.get("ok") and "data" in info:
                enriched.append(info["data"])
            else:
                enriched.append({"port": port})
        except Exception:
            enriched.append({"port": port})
        finally:
            probe.disconnect()

    return {"instances": enriched}
