"""Type serialization for RenderDoc C++ types to JSON-safe Python dicts.

Converts RenderDoc's SWIG-wrapped types into plain dicts suitable for
JSON transport. Ported from orb-renderdoc v1.
"""

import renderdoc as rd


# --- Helpers ---

def _enum_name(value):
    """Extract the name string from a SWIG enum value.

    SWIG enum wrappers sometimes expose `.name` (like Python enums) and
    sometimes don't, depending on the RenderDoc build. This handles both
    cases so callers don't need to repeat the hasattr dance.
    """
    if hasattr(value, "name"):
        return value.name
    return str(value)


# --- Scalar / ID ---

def resource_id(rid: rd.ResourceId) -> str:
    """Convert a ResourceId to its string representation.

    ResourceId is an opaque 64-bit handle. The string form is the raw
    integer, which is stable for the lifetime of the capture.
    """
    return str(int(rid))


def format_description(fmt: rd.ResourceFormat) -> dict:
    """Serialize a ResourceFormat to a plain dict."""
    return {
        "name"                 : fmt.Name(),
        "type"                 : fmt.type.name,
        "component_type"       : fmt.compType.name,
        "component_count"      : fmt.compCount,
        "component_byte_width" : fmt.compByteWidth,
    }


# --- API Properties ---

def api_properties(props: rd.APIProperties) -> dict:
    """Serialize APIProperties."""
    return {
        "pipeline_type"   : props.pipelineType.name,
        "degraded"        : props.degraded,
        "shader_debugging" : props.shaderDebugging,
        "pixel_history"   : props.pixelHistory,
    }


# --- Actions ---

def action_flags(flags: rd.ActionFlags) -> list:
    """Convert an ActionFlags bitmask to a list of flag name strings."""
    result = []

    flag_map = [
        (rd.ActionFlags.Clear,             "clear"),
        (rd.ActionFlags.Drawcall,          "drawcall"),
        (rd.ActionFlags.Dispatch,          "dispatch"),
        (rd.ActionFlags.CmdList,           "cmd_list"),
        (rd.ActionFlags.SetMarker,         "set_marker"),
        (rd.ActionFlags.PushMarker,        "push_marker"),
        (rd.ActionFlags.PopMarker,         "pop_marker"),
        (rd.ActionFlags.Present,           "present"),
        (rd.ActionFlags.MultiAction,       "multi_action"),
        (rd.ActionFlags.Copy,              "copy"),
        (rd.ActionFlags.Resolve,           "resolve"),
        (rd.ActionFlags.GenMips,           "gen_mips"),
        (rd.ActionFlags.PassBoundary,      "pass_boundary"),
        (rd.ActionFlags.Indexed,           "indexed"),
        (rd.ActionFlags.Instanced,         "instanced"),
        (rd.ActionFlags.Auto,              "auto"),
        (rd.ActionFlags.Indirect,          "indirect"),
        (rd.ActionFlags.ClearColor,        "clear_color"),
        (rd.ActionFlags.ClearDepthStencil, "clear_depth_stencil"),
        (rd.ActionFlags.BeginPass,         "begin_pass"),
        (rd.ActionFlags.EndPass,           "end_pass"),
    ]

    for flag, name in flag_map:
        if flags & flag:
            result.append(name)

    return result


def action_name(action: rd.ActionDescription) -> str:
    """Derive a human-readable display name for an action.

    Prefers the custom name if one was set (e.g., user debug markers).
    Otherwise synthesizes a name from the action flags and parameters.
    """
    if action.customName:
        return action.customName

    # Build name from flags.
    flags = action.flags
    if flags & rd.ActionFlags.Drawcall:
        if flags & rd.ActionFlags.Indexed:
            return f"DrawIndexed({action.numIndices})"
        else:
            return f"Draw({action.numIndices})"
    elif flags & rd.ActionFlags.Dispatch:
        dim = action.dispatchDimension
        return f"Dispatch({dim[0]}, {dim[1]}, {dim[2]})"
    elif flags & rd.ActionFlags.Clear:
        return "Clear"
    elif flags & rd.ActionFlags.Copy:
        return "Copy"
    elif flags & rd.ActionFlags.Resolve:
        return "Resolve"
    elif flags & rd.ActionFlags.Present:
        return "Present"
    elif flags & rd.ActionFlags.PushMarker:
        return "Marker"
    elif flags & rd.ActionFlags.SetMarker:
        return "SetMarker"
    else:
        return f"Event {action.eventId}"


def action_description(action: rd.ActionDescription) -> dict:
    """Serialize an ActionDescription to a plain dict.

    Always includes identity and flags. Draw and dispatch parameters are
    only present when the corresponding flag bits are set.
    """
    result = {
        "event_id"  : action.eventId,
        "action_id" : action.actionId,
        "name"      : action_name(action),
        "flags"     : action_flags(action.flags),
    }

    # Draw parameters (only for draw calls).
    if action.flags & rd.ActionFlags.Drawcall:
        result["draw"] = {
            "num_indices"     : action.numIndices,
            "num_instances"   : action.numInstances,
            "index_offset"    : action.indexOffset,
            "base_vertex"     : action.baseVertex,
            "instance_offset" : action.instanceOffset,
        }

    # Dispatch parameters (only for compute dispatches).
    if action.flags & rd.ActionFlags.Dispatch:
        result["dispatch"] = {
            "group_x" : action.dispatchDimension[0],
            "group_y" : action.dispatchDimension[1],
            "group_z" : action.dispatchDimension[2],
        }

    return result


# --- Resources ---

def texture_description(tex: rd.TextureDescription) -> dict:
    """Serialize a TextureDescription with all metadata.

    Includes dimensions, format, sample count, and creation flags.
    """
    result = {
        "resource_id"    : resource_id(tex.resourceId),
        "type"           : tex.type.name,
        "width"          : tex.width,
        "height"         : tex.height,
        "depth"          : tex.depth,
        "array_size"     : tex.arraysize,
        "mips"           : tex.mips,
        "samples"        : tex.msSamp,
        "sample_quality" : tex.msQual,
        "format"         : format_description(tex.format),
        "byte_size"      : tex.byteSize,
        "cube_map"       : tex.cubemap,
    }

    # Creation flags.
    flags = []
    if hasattr(tex, "creationFlags"):
        cf       = tex.creationFlags
        flag_map = [
            (rd.TextureCategory.ShaderRead,      "shader_read"),
            (rd.TextureCategory.ColorTarget,     "color_target"),
            (rd.TextureCategory.DepthTarget,     "depth_target"),
            (rd.TextureCategory.ShaderReadWrite, "shader_read_write"),
            (rd.TextureCategory.SwapBuffer,      "swap_buffer"),
        ]
        for flag, name in flag_map:
            if cf & flag:
                flags.append(name)
    result["creation_flags"] = flags

    return result


def buffer_description(buf: rd.BufferDescription) -> dict:
    """Serialize a BufferDescription with all metadata.

    Includes length, creation flags, and GPU virtual address if available.
    """
    result = {
        "resource_id" : resource_id(buf.resourceId),
        "length"      : buf.length,
    }

    # Creation flags.
    flags = []
    if hasattr(buf, "creationFlags"):
        cf       = buf.creationFlags
        flag_map = [
            (rd.BufferCategory.Indirect,  "indirect"),
            (rd.BufferCategory.Index,     "index"),
            (rd.BufferCategory.Vertex,    "vertex"),
            (rd.BufferCategory.Constants, "constants"),
            (rd.BufferCategory.ReadWrite, "read_write"),
        ]
        for flag, name in flag_map:
            if cf & flag:
                flags.append(name)
    result["creation_flags"] = flags

    # GPU address if available.
    if hasattr(buf, "gpuAddress") and buf.gpuAddress:
        result["gpu_address"] = hex(buf.gpuAddress)

    return result


# --- Pipeline State ---

def pipeline_state(state: rd.PipeState) -> dict:
    """Serialize the full pipeline state snapshot.

    Covers shader bindings, output targets, viewport/scissor, topology,
    stencil, rasterizer, blend, and input assembly (index/vertex buffers).
    Each section is fault-isolated so a failure in one doesn't prevent the
    rest from being reported.
    """
    result = {}

    # Shader stages.
    stages = [
        ("vs", rd.ShaderStage.Vertex),
        ("hs", rd.ShaderStage.Hull),
        ("ds", rd.ShaderStage.Domain),
        ("gs", rd.ShaderStage.Geometry),
        ("ps", rd.ShaderStage.Pixel),
        ("cs", rd.ShaderStage.Compute),
    ]

    result["shaders"] = {}
    for name, stage in stages:
        shader = state.GetShader(stage)
        if int(shader) != 0:
            result["shaders"][name] = resource_id(shader)

    # The compute shader ID persists across events. When any graphics
    # shaders are bound, the CS is stale noise from the last dispatch.
    has_graphics = any(
        k in result["shaders"] for k in ("vs", "hs", "ds", "gs", "ps")
    )
    if has_graphics and "cs" in result["shaders"]:
        del result["shaders"]["cs"]

    # Output targets.
    try:
        om = state.GetOutputTargets()
        result["render_targets"] = []
        for i, rt in enumerate(om):
            if int(rt.resource) != 0:
                result["render_targets"].append({
                    "slot"        : i,
                    "resource"    : resource_id(rt.resource),
                    "first_mip"  : rt.firstMip,
                    "first_slice" : rt.firstSlice,
                    "num_mips"   : rt.numMips,
                    "num_slices" : rt.numSlices,
                })

        depth = state.GetDepthTarget()
        if depth and int(depth.resource) != 0:
            result["depth_target"] = {
                "resource"    : resource_id(depth.resource),
                "first_mip"  : depth.firstMip,
                "first_slice" : depth.firstSlice,
                "num_mips"   : depth.numMips,
                "num_slices" : depth.numSlices,
            }
        else:
            result["depth_target"] = None
    except Exception:
        result["render_targets"] = []
        result["depth_target"]   = None

    # Viewport (GetViewport returns a single viewport for the given index).
    try:
        vp = state.GetViewport(0)
        result["viewport"] = {
            "x"         : vp.x,
            "y"         : vp.y,
            "width"     : vp.width,
            "height"    : vp.height,
            "min_depth" : vp.minDepth,
            "max_depth" : vp.maxDepth,
        }
    except Exception as e:
        result["viewport"] = {"error": str(e)}

    # Scissor.
    try:
        sc = state.GetScissor(0)
        result["scissor"] = {
            "x"      : sc.x,
            "y"      : sc.y,
            "width"  : sc.width,
            "height" : sc.height,
        }
    except Exception as e:
        result["scissor"] = {"error": str(e)}

    # Topology.
    try:
        result["topology"] = state.GetPrimitiveTopology().name
    except Exception as e:
        result["topology"] = str(e)

    # Stencil state. GetStencilFaces returns a (front, back) tuple.
    try:
        front, back = state.GetStencilFaces()
        result["stencil"] = {
            "front": {
                "fail_op"       : _enum_name(front.failOperation),
                "depth_fail_op" : _enum_name(front.depthFailOperation),
                "pass_op"       : _enum_name(front.passOperation),
                "function"      : _enum_name(front.function),
                "reference"     : front.reference,
                "compare_mask"  : front.compareMask,
                "write_mask"    : front.writeMask,
            },
            "back": {
                "fail_op"       : _enum_name(back.failOperation),
                "depth_fail_op" : _enum_name(back.depthFailOperation),
                "pass_op"       : _enum_name(back.passOperation),
                "function"      : _enum_name(back.function),
                "reference"     : back.reference,
                "compare_mask"  : back.compareMask,
                "write_mask"    : back.writeMask,
            },
        }
    except Exception as e:
        result["stencil"] = {"error": str(e)}

    # Rasterizer state.
    try:
        restart_enabled = state.IsRestartEnabled()
        result["rasterizer"] = {
            "topology"          : state.GetPrimitiveTopology().name,
            "restart_enabled"   : restart_enabled,
            "restart_index"     : state.GetRestartIndex() if restart_enabled else None,
            "rasterized_stream" : state.GetRasterizedStream(),
        }
    except Exception as e:
        result["rasterizer"] = {"error": str(e)}

    # Blend state. GetColorBlends returns a list of per-target blend configs.
    try:
        blends = state.GetColorBlends()
        result["blend_targets"]        = []
        result["independent_blending"] = state.IsIndependentBlendingEnabled()
        for i, bt in enumerate(blends):
            target_blend = {
                "slot"             : i,
                "enabled"          : bt.enabled,
                "logic_op_enabled" : bt.logicOperationEnabled,
                "logic_op"         : _enum_name(bt.logicOperation),
                "color_blend"      : {
                    "source"      : _enum_name(bt.colorBlend.source),
                    "destination" : _enum_name(bt.colorBlend.destination),
                    "operation"   : _enum_name(bt.colorBlend.operation),
                },
                "alpha_blend"      : {
                    "source"      : _enum_name(bt.alphaBlend.source),
                    "destination" : _enum_name(bt.alphaBlend.destination),
                    "operation"   : _enum_name(bt.alphaBlend.operation),
                },
                "write_mask"       : bt.writeMask,
            }
            result["blend_targets"].append(target_blend)
    except Exception as e:
        result["blend_targets"] = {"error": str(e)}

    # Index buffer.
    try:
        ib = state.GetIBuffer()
        if int(ib.resourceId) != 0:
            result["index_buffer"] = {
                "resource" : resource_id(ib.resourceId),
                "offset"   : ib.byteOffset,
                "stride"   : ib.byteStride,
            }
    except Exception as e:
        result["index_buffer"] = {"error": str(e)}

    # Vertex buffers.
    try:
        vbs = state.GetVBuffers()
        result["vertex_buffers"] = []
        for i, vb in enumerate(vbs):
            if int(vb.resourceId) != 0:
                result["vertex_buffers"].append({
                    "slot"     : i,
                    "resource" : resource_id(vb.resourceId),
                    "offset"   : vb.byteOffset,
                    "stride"   : vb.byteStride,
                })
    except Exception as e:
        result["vertex_buffers"] = {"error": str(e)}

    return result


# --- Shader Reflection ---

def shader_reflection(refl: rd.ShaderReflection) -> dict:
    """Serialize shader reflection data.

    Covers entry point, stage, debug info, input/output signatures,
    constant buffers (with variable layouts), read-only and read-write
    resources, and samplers.
    """
    result = {
        "entry_point" : refl.entryPoint,
        "stage"       : refl.stage.name,
    }

    # Debug info.
    if refl.debugInfo:
        result["debug_info"] = {
            "debuggable"   : refl.debugInfo.debuggable,
            "source_files" : len(refl.debugInfo.files) if refl.debugInfo.files else 0,
        }
        # Include source file names.
        if refl.debugInfo.files:
            result["debug_info"]["files"] = [
                f.filename for f in refl.debugInfo.files
            ]

    # Input signature.
    if refl.inputSignature:
        result["inputs"] = []
        for sig in refl.inputSignature:
            inp = {
                "name"           : sig.varName,
                "semantic"       : sig.semanticName,
                "semantic_index" : sig.semanticIndex,
                "type"           : _enum_name(sig.varType),
                "system_value"   : _enum_name(sig.systemValue),
                "reg_index"      : sig.regIndex,
            }
            if hasattr(sig, "regCount"):
                inp["reg_count"] = sig.regCount
            if hasattr(sig, "compCount"):
                inp["component_count"] = sig.compCount
            result["inputs"].append(inp)

    # Output signature.
    if refl.outputSignature:
        result["outputs"] = []
        for sig in refl.outputSignature:
            out = {
                "name"           : sig.varName,
                "semantic"       : sig.semanticName,
                "semantic_index" : sig.semanticIndex,
                "type"           : _enum_name(sig.varType),
                "system_value"   : _enum_name(sig.systemValue),
                "reg_index"      : sig.regIndex,
            }
            if hasattr(sig, "regCount"):
                out["reg_count"] = sig.regCount
            if hasattr(sig, "compCount"):
                out["component_count"] = sig.compCount
            result["outputs"].append(out)

    # Constant buffers with variable details.
    if refl.constantBlocks:
        result["constant_buffers"] = []
        for cb in refl.constantBlocks:
            cb_info = {
                "name"      : cb.name,
                "byte_size" : cb.byteSize,
                "binding"   : {
                    "set"        : cb.fixedBindSetOrSpace,
                    "binding"    : cb.fixedBindNumber,
                    "array_size" : cb.bindArraySize,
                },
                "variables" : [],
            }
            # Include all variables with full layout info.
            if cb.variables:
                for var in cb.variables:
                    var_info = {
                        "name"   : var.name,
                        "offset" : var.byteOffset,
                    }
                    if hasattr(var, "type") and var.type:
                        vtype                    = var.type
                        var_info["type"]         = vtype.name if hasattr(vtype, "name") else str(vtype)
                        var_info["rows"]         = getattr(vtype, "rows", 1) or 1
                        var_info["columns"]      = getattr(vtype, "columns", 1) or 1
                        var_info["elements"]     = getattr(vtype, "elements", 0)
                        var_info["array_stride"] = getattr(vtype, "arrayByteStride", 0)
                    cb_info["variables"].append(var_info)
            result["constant_buffers"].append(cb_info)

    # Read-only resources (textures, typed buffers).
    if refl.readOnlyResources:
        result["read_only_resources"] = []
        for res in refl.readOnlyResources:
            res_info = {
                "name"    : res.name,
                "binding" : {
                    "set"        : res.fixedBindSetOrSpace,
                    "binding"    : res.fixedBindNumber,
                    "array_size" : res.bindArraySize,
                },
            }
            if hasattr(res, "textureType"):
                res_info["texture_type"] = _enum_name(res.textureType)
            if hasattr(res, "variableType") and res.variableType:
                res_info["variable_type"] = _enum_name(res.variableType)
            if hasattr(res, "isTexture"):
                res_info["is_texture"] = res.isTexture
            result["read_only_resources"].append(res_info)

    # Read-write resources (UAVs, storage buffers).
    if refl.readWriteResources:
        result["read_write_resources"] = []
        for res in refl.readWriteResources:
            res_info = {
                "name"    : res.name,
                "binding" : {
                    "set"        : res.fixedBindSetOrSpace,
                    "binding"    : res.fixedBindNumber,
                    "array_size" : res.bindArraySize,
                },
            }
            if hasattr(res, "textureType"):
                res_info["texture_type"] = _enum_name(res.textureType)
            if hasattr(res, "isTexture"):
                res_info["is_texture"] = res.isTexture
            result["read_write_resources"].append(res_info)

    # Samplers.
    if refl.samplers:
        result["samplers"] = []
        for s in refl.samplers:
            sampler_info = {
                "name"    : s.name,
                "binding" : {
                    "set"        : s.fixedBindSetOrSpace,
                    "binding"    : s.fixedBindNumber,
                    "array_size" : s.bindArraySize,
                },
            }
            result["samplers"].append(sampler_info)

    return result


# --- Constant Buffer Data ---

def cbuffer_variables(variables, data: bytes) -> list:
    """Serialize constant buffer variables with their current values.

    Reads raw buffer data and unpacks each variable according to its
    type and byte offset. Falls back gracefully if type info is missing
    or the buffer is too short.
    """
    import struct

    result = []
    for var in variables:
        v = {
            "name"   : var.name,
            "offset" : var.byteOffset,
        }

        # Try to get type info safely.
        try:
            vtype     = var.type
            type_name = vtype.name if hasattr(vtype, "name") else str(vtype)
            v["type"] = type_name

            # Get dimensions. Structure varies by RenderDoc version.
            rows = getattr(vtype, "rows", 1)
            cols = getattr(vtype, "columns", 1)
            if rows == 0:
                rows = 1
            if cols == 0:
                cols = 1

            v["rows"]    = rows
            v["columns"] = cols

            # Array info from reflection.
            elements     = getattr(vtype, "elements", 0)
            array_stride = getattr(vtype, "arrayByteStride", 0)

            # Select unpack format based on the type name.
            type_lower = type_name.lower()
            if type_lower in ("uint", "uint32", "dword"):
                fmt = "I"  # unsigned 32-bit int
            elif type_lower in ("int", "int32"):
                fmt = "i"  # signed 32-bit int
            elif type_lower in ("bool",):
                fmt = "I"  # bool stored as uint
            else:
                fmt = "f"  # float (default)

            # Per-element component count (e.g., 3 for a float3).
            comp_count = rows * cols
            comp_bytes = comp_count * 4

            offset = var.byteOffset

            if elements > 0 and array_stride > 0:
                # Array variable: read each element at stride intervals.
                v["elements"]     = elements
                v["array_stride"] = array_stride
                all_values        = []
                for i in range(elements):
                    elem_offset = offset + i * array_stride
                    if elem_offset + comp_bytes <= len(data):
                        vals = struct.unpack_from(f"<{comp_count}{fmt}", data, elem_offset)
                        all_values.append(list(vals))
                    else:
                        break
                v["value"] = all_values
            elif comp_count > 0 and offset + comp_bytes <= len(data):
                # Scalar or vector/matrix variable.
                values     = struct.unpack_from(f"<{comp_count}{fmt}", data, offset)
                v["value"] = list(values)
        except Exception:
            pass

        result.append(v)

    return result
