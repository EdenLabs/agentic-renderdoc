# Agentic RenderDoc Battle Test

You have access to a live RenderDoc instance with a GPU capture open via the `renderdoc` MCP server. Your job is to exercise the three tools (Eval, Search API, Instance) thoroughly and report failures, surprising behavior, or usability issues.

Work through the following scenarios **in order**. For each one, report: pass/fail, the actual output (abbreviated if large), and any friction you encountered.

---

## Phase 1: Connection & Orientation

### 0. Instance Discovery

- Call Instance with action "list". Note the port, API type, event count, and capture path.
- If multiple instances are listed, call Instance with action "connect" on one.

### 1. Action Tree Structure

- List all root actions. Note how many there are.
- Pick the largest marker group (one with .children) and recursively list its children. How deep does the tree go?
- Count total draw calls in the entire frame (recurse through all children, filter by ActionFlags.Drawcall).
- Count total dispatches (ActionFlags.Dispatch).
- For three draw calls spread across the frame (first, middle, last), print their display names using `GetName(ctx.structured_file)`.

### 2. Action Flags

- Pick a draw call and decode its `.flags` using `action_flags()`. What flags are set?
- Pick a marker (PushMarker) and decode its flags. Compare.
- If any draw calls have the Indexed or Instanced flags, note which ones.

---

## Phase 2: API Discovery

### 3. Search API — Exact Matches

- Search for `SetFrameEvent`. Confirm the signature and which class it belongs to.
- Search for `GetPipelineState`. What does it return?
- Search for `GetOutputTargets`. What type does it return (Descriptor vs UsedDescriptor)?

### 4. Search API — Enums

- Search for `ShaderStage`. List all enum values.
- Search for `ActionFlags`. List a few key values.
- Search for `CompareFunction`. What values are available?

### 5. Search API — Fuzzy Discovery

- Search for `"texture"`. How many results? Are they useful?
- Search for `"blend"`. Does it find blend state methods?
- Search for `"constant buffer"` or `"cbuffer"`. What comes up?

---

## Phase 3: Pipeline State Deep Dive

### 6. Full Pipeline State

- Navigate to the last draw call. Call `SetFrameEvent` then `GetPipelineState`.
- Serialize the full pipeline state with `serialize.pipeline_state()`.
- Extract and report:
  - Bound vertex and pixel/fragment shaders (resource IDs)
  - Active render target(s): resource IDs, formats
  - Depth target: resource ID, format
  - Viewport dimensions
  - Primitive topology

### 7. Resource Names

- For each shader, render target, and depth target found in scenario 6, use `get_resource_name()` to resolve human-readable names.
- Are the names informative (e.g., application-defined labels) or generic?

### 8. Shader Reflection

- For the pixel/fragment shader at the last draw call, get `ShaderReflection`.
- List all constant buffer bindings: name, bind number, bind set/space, variable count.
- For each constant buffer, list the variables: name, type (baseType, rows, columns), offset.
- List all read-only resource bindings (textures/samplers): name, bind number.

### 9. Depth/Stencil State

- The API-agnostic PipeState does not expose depth/stencil test configuration. Use the API-specific path instead:
  - For Vulkan: `controller.GetVulkanPipelineState().depthStencil`
  - For D3D11: `controller.GetD3D11PipelineState().outputMerger.depthStencilState`
- Report: depth test enabled, depth write enabled, compare function.
- Use `inspect()` on the depth/stencil state object to discover all available fields.

### 10. Blend State

- Get color blends via `state.GetColorBlends()`.
- Report blend enable/disable, source/destination factors, blend operation for the first render target.

---

## Phase 4: Data Readback

### 11. Constant Buffer Contents

- At the last draw call, read back the first constant buffer bound to the pixel shader.
- Use `serialize.cbuffer_variables()` to decode the raw bytes against the shader reflection variables.
- Report variable names and values.
- If there are multiple constant buffers, read at least two.

### 12. Vertex Buffer Inspection

- At a draw call, get the vertex buffer bindings via `state.GetVBuffers()`.
- Read back raw bytes from the first vertex buffer using `GetBufferData`.
- Use `interpret_buffer` with an appropriate format to decode the first few vertices.
- What does the vertex data look like? (positions, normals, UVs?)

### 13. Index Buffer Inspection

- At an indexed draw call (check ActionFlags.Indexed), get the index buffer via `state.GetIBuffer()`.
- Read back raw bytes and decode. What index format is used (16-bit, 32-bit)?

### 14. Resource Enumeration

- List all textures: count, range of dimensions, formats seen.
- List all buffers: count, range of sizes.
- Use `summarize_data` on a set of buffer sizes or texture dimensions to get statistics.

---

## Phase 5: Comparative Analysis

### 15. State Diffing

- Use `diff_state` to compare the first and last draw calls.
- What changed? Are resource IDs annotated with human-readable names?
- How readable is the diff output?

### 16. State Diff — Adjacent Draws

- Pick two adjacent draw calls (consecutive eventIds) and diff them.
- Is the diff smaller? What typically changes between adjacent draws?

### 17. Shader Reuse Analysis

- Walk all draw calls. For each, record the bound vertex and pixel shader resource IDs.
- Which shaders are used by the most draw calls?
- Build a table: shader resource ID, name, draw call count.

---

## Phase 6: Introspection & Discovery

### 18. inspect() on Instances

- Call `inspect()` on a PipeState object. What methods and properties does it expose?
- Call `inspect()` on a ReplayController. What's available?
- Call `inspect()` on an ActionDescription. What properties does a draw call have?

### 19. inspect() on Enums and Modules

- Call `inspect(rd.ShaderStage)`. Lists values?
- Call `inspect(rd)`. What classes, functions, and constants does the renderdoc module expose?
- Call `inspect(serialize)`. What serialization functions are available?

---

## Phase 7: UI Interaction

### 20. Event Navigation

- Use `goto_event(eid)` to navigate to the first draw call. Does it return a confirmation?
- Use `highlight_drawcall(eid)` on a different draw call. Any observable difference from `goto_event`?

### 21. Texture Viewer

- Get the render target resource ID from a draw call.
- Use `view_texture(resource_id)` to open the texture viewer.

---

## Phase 8: Error Handling & Edge Cases

### 22. Syntax Errors

- Send broken Python: `eval("def foo(:")`. Does the error include the column position?
- Send a multi-line snippet with a syntax error on line 3. Does `failing_line` point to the right line?

### 23. Runtime Errors

- Reference a nonexistent variable: `eval("bogus_variable")`. Are available names listed in hints?
- Call a nonexistent method: `eval("def w(c): c.FakeMethod()\nctx.replay(w)")`. Does the hint suggest `inspect()` or `search_api`?

### 24. SetFrameEvent Warning

- Inside a ctx.replay callback, call `GetPipelineState()` without calling `SetFrameEvent()` first. Does the response include a warning?
- Then call `SetFrameEvent()` followed by `GetPipelineState()`. Confirm no warning appears.

### 25. Connection Errors

- If you can, call Instance with action "disconnect", then try to use Eval. What error do you get?
- Reconnect afterwards.

### 26. Edge Cases

- Call `get_resource_name()` with `rd.ResourceId.Null()`. What does it return?
- Call `diff_state(eid, eid)` with the same event ID twice. Does it return an empty diff?
- Pass an empty code string to Eval. What happens?
- Call `action_flags(0)`. Does it return an empty list?

---

## Phase 9: Multi-Step Workflows

### 27. Render Target Trace

- Starting from the final present/resolve action, trace backwards through the frame:
  - At each draw call, note the active render targets.
  - Build a dependency graph: which draws write to which targets, and which draws read those targets as textures.
- How many unique render targets are used across the frame?

### 28. Performance Hotspot Detection

- Walk all draw calls. For each, record the index count (numIndices) and instance count (numInstances).
- Sort by total vertex throughput (numIndices * numInstances).
- What are the top 5 most expensive draws by vertex count?

### 29. Full Frame Summary

- In a single eval call, build a comprehensive frame summary:
  - Total draw calls, dispatches, clears, copies
  - Unique shaders used (vertex + pixel)
  - Unique render targets
  - Unique textures bound as inputs
  - Total vertices processed
- Return as a structured dict.

---

## Phase 10: Bindless & Modern Rendering Patterns

The capture is from a bindless renderer. Shaders access resources through descriptor indexing rather than fixed pipeline bindings. Geometry uses vertex/index pulling from byte address buffers rather than traditional vertex input state. Push constants point at the data.

### 30. Push Constants

- At a draw call, look for push constant data. Search the API for push constants if needed.
- Read the push constant values. What do they contain? (Likely buffer addresses, descriptor indices, or offsets.)
- Can you determine what the push constants are pointing at?

### 31. Byte Address Buffer Readback

- Identify a large storage buffer (byte address buffer) used by the frame. These will likely show up as read-write or read-only resources with large byte sizes.
- Read back a chunk of the buffer using `GetBufferData`.
- The data won't have a typed format since it's a raw byte address buffer. Try interpreting the first N bytes as:
  - float32 (interpret_buffer with component_type "Float")
  - uint32 (interpret_buffer with component_type "UInt")
- Can you infer what the data represents from the values? (vertex positions, indices, material data?)

### 32. Vertex Pulling Detection

- At a draw call, check `state.GetVBuffers()`. If the list is empty or the bindings have null resources, the shader is doing vertex pulling from a storage buffer rather than using vertex input state.
- Confirm this is the case. Then look at the vertex shader reflection: what read-only or read-write resources does it bind?
- Try to find the buffer the vertex shader pulls from and read back a few elements.

### 33. Descriptor Indexing / Bindless Resources

- At a draw call, inspect the read-only and read-write resource bindings for the pixel/fragment shader.
- In a bindless renderer, you may see large arrays of descriptors rather than individual bindings. What does the binding layout look like?
- If there are array bindings, note the array sizes. Can you tell which descriptors within the array are actually accessed by this draw call? (This may require correlating with push constant values.)

### 34. Material Data Reconstruction

- Pick a draw call and try to reconstruct what material data it uses:
  1. Read push constants to find buffer offsets / descriptor indices.
  2. Read the relevant storage buffer at those offsets.
  3. Interpret the material data (try float32, try uint32 for texture indices).
- How far can you get? Note where the tool surface runs out and you'd need manual inspection.

---

## Phase 11: Shader Debugging

### 35. Shader Source / Disassembly

- Search the API for shader debugging, disassembly, or source retrieval. What's available?
- At a draw call, try to retrieve the shader source or disassembly for the pixel/fragment shader.
- Look for `GetDisassemblyTargets` and `DisassembleShader` on ReplayController.
- What disassembly formats are available? Can you get SPIR-V, GLSL, or DXIL?

### 36. Shader Debug Trace

- Search the API for shader debug tracing. Look for `DebugVertex`, `DebugPixel`, or similar.
- If available, try to initiate a debug trace at a specific pixel or vertex for a draw call.
- What data does the trace return? Variable values? Instruction-level stepping?
- Note any friction: is the API discoverable from the tool description, or did you need multiple search_api calls?

### 37. Shader Variable Inspection

- At a draw call, examine all inputs to the pixel shader:
  - Constant buffers (already tested, but note how they interact with bindless)
  - Texture bindings
  - Push constants
- Can you build a complete picture of what data the shader has access to at this draw call?
- What's missing or hard to determine?

---

## Phase 12: Compute Dispatches

### 38. Compute Pass Inspection

- Find a compute dispatch in the frame (ActionFlags.Dispatch). Bindless renderers often use compute for culling, light binning, or post-processing.
- Set the event to the dispatch and inspect pipeline state. What compute shader is bound?
- Get the shader reflection for `rd.ShaderStage.Compute`. What resources does it bind? (storage buffers, textures, push constants?)
- Read the dispatch dimensions from the action's `.dispatchDimension` property.

### 39. Compute Buffer I/O

- For the compute dispatch, identify the read-write resources (storage buffers the shader writes to).
- Read back a chunk of the output buffer after the dispatch.
- Can you determine what the compute pass produced? (culled draw args, a light list, processed vertices?)
- If the compute output feeds into a subsequent draw call, try to trace that connection.

---

## Phase 13: Texture & Render Target Readback

### 40. Render Target Contents

- At a draw call, identify the active render target via `state.GetOutputTargets()`.
- Use `controller.GetTextureData(resourceId, subresource)` to read back the render target contents.
- The subresource index is typically `mip + (slice * mipCount)`. For the base mip of slice 0, use subresource 0.
- What format is the texture? Try interpreting the raw bytes accordingly (float16 for HDR, uint8 for LDR, etc.).
- Use `summarize_data` on the decoded values. Are there NaN or Inf values? What's the value range?

### 41. Texture Subresources

- Find a texture with multiple mip levels (check TextureDescription.mips > 1).
- Read back mip 0 and mip 1. How do the byte sizes compare?
- If any textures have array slices (arraysize > 1), read back different slices.

### 42. Depth Buffer Readback

- At a draw call with a depth target, read back the depth buffer contents.
- Depth is typically D32_FLOAT or D24_UNORM_S8_UINT. Interpret accordingly.
- What's the depth range? Does `summarize_data` show anything interesting?

---

## Phase 14: Indirect & Multi-Draw

### 43. Indirect Draw Detection

- Walk the draw calls and check for `ActionFlags.Indirect`. Are there indirect draws?
- For an indirect draw, the draw arguments come from a buffer rather than the API call. The action's numIndices/numInstances reflect the actual values used.
- Can you find the indirect argument buffer? Search the API for indirect-related methods if needed.

### 44. Draw Call Batching

- Look at sequences of draw calls. Are there patterns? (Same shader, same render target, different push constants?)
- How many unique shader combinations (vertex + pixel pair) are used across the frame?
- Group draw calls by their shader pair. Which groups have the most draws?

---

## Phase 15: Frame Structure & Non-Draw Events

### 45. Render Pass Structure

- Walk the action tree looking for BeginPass and EndPass flags. How many render passes does the frame have?
- For each pass, list the render targets and how many draw calls it contains.
- Build a pass-level summary of the frame.

### 46. Clears and Copies

- Find all Clear actions (ActionFlags.Clear, ClearColor, ClearDepthStencil). What targets are being cleared and to what values?
- Find all Copy actions (ActionFlags.Copy). What's being copied where?
- These help understand frame setup and resource transitions.

### 47. Pipeline State at Non-Draw Events

- Set the event to a Clear or Copy operation and inspect the pipeline state. What does it look like?
- Set the event to a compute dispatch. How does the pipeline state differ from a draw call?

---

## Phase 16: Transformed Vertex Output

### 48. Vertex Shader Output

- Search the API for `MeshDataStage`, `GetPostVSData`, or related methods for reading transformed vertices.
- At a draw call, try to read the vertex shader output (post-VS positions, interpolants).
- What data is available? Positions, normals, UVs, custom interpolants?
- In a vertex-pulling renderer, the "input" assembler may be empty but the shader still produces output.

---

## Phase 17: Stress Tests

### 49. Large Data Returns

- Try returning a very large result: all draw calls with full pipeline state serialized.
- Does the response truncate? Does the MCP transport handle it? How long does it take?
- What's the practical limit on response size?

### 50. Multiple Replay Calls

- In a single eval, make multiple `ctx.replay()` calls sequentially. For example, snapshot state at 5 different events and compare them.
- Does each call work correctly? Are there any issues with sequential replay calls?

### 51. Long-Running Analysis

- Write a single eval that walks every draw call in the frame and collects detailed state for each (shaders, render targets, resource names, push constants).
- How long does it take? Does it complete or time out?
- Is there a practical limit on how much work a single eval can do?

---

## Phase 18: Real Debugging Workflows

These simulate actual debugging sessions. The goal is to test whether the tool surface supports end-to-end debugging, not just individual API calls.

### 52. "This Pixel Is Wrong"

- Pick a draw call and pretend a specific pixel in its render target is the wrong color.
- Walk through the debugging process:
  1. Identify the draw call and its render targets.
  2. Read the render target contents near the pixel of interest.
  3. Check what shader is bound and what inputs it receives (constant buffers, textures, push constants).
  4. If shader debug tracing is available, trace the pixel.
  5. Check the depth/stencil test: is the draw even reaching the pixel?
  6. Check blend state: is the output being blended incorrectly?
- How far can you get? Where does the tool run out?

### 53. "Draw Call Is Missing"

- Pretend a draw call that should be rendering isn't producing visible output.
- Debug it:
  1. Find the draw call by name or event ID.
  2. Check if it has a valid shader bound.
  3. Check the render target: is it writing to the right target?
  4. Check the viewport and scissor: is the draw inside the visible area?
  5. Check depth/stencil: is it being depth-culled?
  6. Check the vertex count: is it drawing zero vertices?
  7. Diff against a draw call that IS working: what's different?

### 54. "Performance Regression"

- Analyze the frame for performance characteristics:
  1. Identify the largest draw calls by vertex/index count.
  2. Look for redundant state changes (draws with identical shaders and render targets but different push constants could be batched).
  3. Check for overdraw: how many draws target the same render target?
  4. Identify the most expensive render passes by total draw call count and vertex throughput.
  5. Look for unnecessary clears or copies.
- Produce a performance report with actionable findings.

---

**After each scenario**, note:
1. Did the tool description give you enough information to write correct code on the first try?
2. Were there surprises in the return format?
3. Did error messages help you self-correct?

**At the end**, write a summary covering:
- What worked well
- What was confusing or could bite someone
- Specific suggestions for improving tool descriptions, error messages, or utilities
- Any new utility functions that would reduce friction
