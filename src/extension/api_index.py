"""API index builder for the RenderDoc Python module.

Introspects the live `renderdoc` module at startup to build a searchable
index of all classes, methods, enums, and their docstrings.
"""

import inspect
import re


# --- Synonyms ---

# Maps common search terms to related API names. When a user searches for a
# term on the left, entries matching any term on the right are also considered.
# Synonym matches are penalized slightly (-5) so direct matches rank first.
#
# Coverage targets: D3D/Vulkan/GL terminology, natural language terms LLMs
# commonly use, and common abbreviations.
_SYNONYMS = {
    # Constant buffers / uniforms.
    "constant buffer": ["constantblock", "getconstantblocks", "cbuffer"],
    "cbuffer":         ["constantblock", "getconstantblocks", "constant buffer"],
    "uniform":         ["constantblock", "getconstantblocks", "cbuffer"],

    # Textures.
    "texture":         ["gettextures", "texturedescription", "gettexturedata"],

    # Buffers (general).
    "buffer":          ["getbuffers", "bufferdescription", "getbufferdata"],

    # Render targets / color attachments / framebuffers.
    "render target":   ["getoutputtargets", "outputtarget", "colorblend"],
    "color attachment": ["getoutputtargets", "outputtarget", "colorblend"],
    "framebuffer":     ["getoutputtargets", "outputtarget", "colorblend"],

    # Depth / stencil.
    "depth":           ["getdepthtarget", "depthstencil"],
    "stencil":         ["depthstencil", "getdepthtarget", "stencilstate"],
    "depth test":      ["depthstencil", "getdepthtarget", "depthstate"],

    # Vertex / index buffers.
    "vertex buffer":   ["getvbuffers", "vbuffer"],
    "index buffer":    ["getibuffer", "ibuffer"],

    # Blend state.
    "blend":           ["getcolorblends", "colorblend"],

    # Viewport / scissor.
    "viewport":        ["getviewport"],
    "scissor":         ["getscissor"],

    # Shaders.
    "shader":          ["getshader", "getshaderreflection", "shaderreflection"],

    # Draw calls / dispatches.
    "draw":            ["drawcall", "actiondescription", "getrootactions"],
    "dispatch":        ["dispatch", "computeshader"],

    # Push constants.
    "push constant":   ["pushconsts", "pushconstant"],

    # Descriptors / resources.
    "descriptor":      ["descriptoraccess", "useddescriptor", "descriptorstore"],
    "resource":        ["getresources", "resourcedescription", "resourceid"],

    # Disassembly / debug.
    "disassembly":     ["disassembleshader", "getdisassemblytargets"],
    "debug":           ["debugpixel", "debugvertex", "shaderdebug"],

    # UAV / storage buffers / RWBuffers.
    "uav":             ["readwriteresource", "rwbuffer", "rwtexture", "unorderedaccess"],
    "storage buffer":  ["readwriteresource", "rwbuffer", "unorderedaccess"],
    "read write":      ["readwriteresource", "rwbuffer", "rwtexture", "unorderedaccess"],

    # SRV / shader resources / read-only.
    "srv":             ["shaderresource", "readonlyresource", "srvresource"],
    "shader resource": ["readonlyresource", "srvresource", "gettextures", "getbuffers"],
    "read only":       ["readonlyresource", "srvresource", "shaderresource"],

    # Samplers / filtering.
    "sampler":         ["getsampler", "getsamplers", "samplerstate", "filtering"],
    "filtering":       ["getsampler", "getsamplers", "samplerstate"],

    # Rasterizer state.
    "rasterizer":      ["rasterstate", "getrasterization", "fillmode", "cullmode"],
    "fill mode":       ["rasterstate", "getrasterization", "fillmode"],
    "cull mode":       ["rasterstate", "getrasterization", "cullmode"],

    # Topology / primitives.
    "topology":        ["primitive", "primitivetopology", "topology"],
    "primitive":       ["topology", "primitivetopology"],

    # Input layout / vertex attributes.
    "input layout":    ["vertexinput", "inputlayout", "vertexattribute", "getvbuffers"],
    "vertex input":    ["inputlayout", "vertexattribute", "getvbuffers"],
    "vertex attribute": ["inputlayout", "vertexinput", "getvbuffers"],

    # Copy / blit / transfer operations.
    "copy":            ["copyresource", "blit", "transfer", "copytexture", "copybuffer"],
    "blit":            ["copyresource", "copy", "transfer", "resolvetexture"],
    "transfer":        ["copyresource", "copy", "blit"],

    # Debug markers / annotations.
    "marker":          ["debugmarker", "debuggroup", "annotation", "pushmarker", "setmarker"],
    "debug group":     ["debugmarker", "annotation", "pushmarker", "popmarker"],
    "annotation":      ["debugmarker", "debuggroup", "pushmarker", "setmarker"],
}


# --- Public API ---

def build_index():
    """Build the API reference index by introspecting the renderdoc module.

    Returns a list of entries, each with:
    - name:      Fully qualified name (e.g., "ReplayController.SetFrameEvent")
    - kind:      "class", "method", "property", "enum", "enum_value"
    - doc:       Full docstring (RST-formatted)
    - signature: Method signature string, if applicable
    """
    try:
        import renderdoc as rd
    except ImportError:
        return []

    entries = []
    _walk_module(rd, "renderdoc", entries, visited=set())
    return entries


def search_index(index, query):
    """Search the index for entries matching the query string.

    Matches against name and docstring content. Case-insensitive.
    Supports natural language queries ("pipeline state"), CamelCase
    identifiers ("GetPipelineState"), and fuzzy/truncated queries.

    Results are ranked by relevance (highest first):
    - Exact unqualified name match (100)
    - Unqualified name prefix match (80)
    - Qualified name substring match (60)
    - Token-based name match (50) / prefix token match (45)
    - Fuzzy edit-distance match (40/30/25)
    - Doc body substring match (20) / doc body token match (15)
    - Synonym expansions apply a -5 penalty to the above tiers
    """
    query_lower = query.lower()
    scored      = []

    for entry in index:
        score = _score_entry(entry, query_lower)
        if score > 0:
            scored.append((score, entry))

    # Sort by score descending, then by name for stability.
    scored.sort(key=lambda pair: (-pair[0], pair[1]["name"]))
    return [entry for _, entry in scored]


# --- Tokenization ---

# Splits on CamelCase boundaries, underscores, and digit transitions.
# "GetPipelineState" -> ["Get", "Pipeline", "State"]
# "RESOURCE"         -> ["RESOURCE"]
# "getVBuffers"      -> ["get", "V", "Buffers"]
_CAMEL_SPLIT_RE = re.compile(
    r"[A-Z]+(?=[A-Z][a-z])"   # Uppercase run before a CamelCase word.
    r"|[A-Z][a-z]+"           # CamelCase word starting with uppercase.
    r"|[A-Z]+"                # Remaining uppercase run (e.g. "RESOURCE", "SRV").
    r"|[a-z]+"                # Lowercase word.
    r"|[0-9]+"                # Digit run.
)


def _tokenize_name(name):
    """Split an API name into lowercase tokens by CamelCase and underscores.

    Handles PascalCase, camelCase, SCREAMING_SNAKE, and mixed identifiers.

    Examples:
        "GetPipelineState" -> ["get", "pipeline", "state"]
        "ShaderStage"      -> ["shader", "stage"]
        "RESOURCE"         -> ["resource"]
        "getVBuffers"      -> ["get", "v", "buffers"]
    """
    # Split on underscores first, then split each segment by CamelCase.
    tokens = []
    for segment in name.split("_"):
        tokens.extend(m.group().lower() for m in _CAMEL_SPLIT_RE.finditer(segment))
    return tokens


def _tokenize_query(query):
    """Split a search query into lowercase tokens.

    Handles both natural language ("pipeline state") and identifier-style
    ("GetPipelineState") queries. Splits on whitespace first, then applies
    CamelCase splitting to each word.
    """
    tokens = []
    for word in query.split():
        camel_tokens = _tokenize_name(word)
        if camel_tokens:
            tokens.extend(camel_tokens)
        else:
            # Fallback: the word had no alphanumeric content. Keep it as-is.
            tokens.append(word.lower())
    return tokens


# --- Edit Distance ---

def _edit_distance(a, b):
    """Compute the Levenshtein edit distance between two strings.

    Standard dynamic programming implementation. O(len(a) * len(b)) time
    and O(min(len(a), len(b))) space (single-row optimization).
    """
    # Ensure a is the shorter string for space efficiency.
    if len(a) > len(b):
        a, b = b, a

    m = len(a)
    n = len(b)

    # Previous row of distances.
    prev = list(range(m + 1))
    curr = [0] * (m + 1)

    for j in range(1, n + 1):
        curr[0] = j
        for i in range(1, m + 1):
            cost    = 0 if a[i - 1] == b[j - 1] else 1
            curr[i] = min(
                curr[i - 1] + 1,       # Insertion.
                prev[i] + 1,           # Deletion.
                prev[i - 1] + cost,    # Substitution.
            )
        prev, curr = curr, prev

    return prev[m]


# --- Scoring ---

def _score_entry(entry, query_lower):
    """Score a single index entry against a lowercased query string.

    Returns an integer score. Zero means no match.

    Computes the base score for the original query, then checks synonym
    expansions. Synonym matches are penalized by -5 so direct matches
    are preferred. If no direct or synonym match is found, attempts
    fuzzy matching as a last resort.
    """
    best = _score_query(entry, query_lower)

    # Check synonyms.
    for syn in _SYNONYMS.get(query_lower, []):
        syn_score = _score_query(entry, syn.lower())
        if syn_score > 0:
            best = max(best, syn_score - 5)

    # Fuzzy matching: only try when nothing else matched, and the query is
    # long enough that edit distance is meaningful (> 4 chars).
    if best == 0 and len(query_lower) > 4:
        best = max(best, _score_fuzzy(entry, query_lower))

    return best


def _score_query(entry, query_lower):
    """Score a single index entry against a single lowercased search term.

    Returns an integer score. Zero means no match.

    Scoring tiers:
    - 100: exact match on unqualified name
    -  80: unqualified name starts with query
    -  60: qualified name contains query as substring
    -  50: all query tokens appear in the name tokens (token match)
    -  45: all query tokens appear in the name tokens as prefixes
    -  20: doc body contains query as substring
    -  15: all query tokens appear in the doc body
    """
    name_lower  = entry["name"].lower()
    unqualified = name_lower.rsplit(".", 1)[-1]

    # Tier 1: exact name match.
    if unqualified == query_lower:
        return 100

    # Tier 2: name prefix match.
    if unqualified.startswith(query_lower):
        return 80

    # Tier 3: qualified name substring match.
    if query_lower in name_lower:
        return 60

    # Tier 4: token-based matching on the name.
    # Tokenize from the original-cased name so CamelCase boundaries are preserved.
    query_tokens         = _tokenize_query(query_lower)
    unqualified_original = entry["name"].rsplit(".", 1)[-1]
    if len(query_tokens) > 1 or (len(query_tokens) == 1 and " " not in query_lower):
        name_tokens = _tokenize_name(unqualified_original)
        token_score = _score_tokens(query_tokens, name_tokens)
        if token_score > 0:
            return token_score

    # Tier 5: doc body substring match.
    if entry["doc"] and query_lower in entry["doc"].lower():
        return 20

    # Tier 6: doc body token match.
    if entry["doc"] and len(query_tokens) > 1:
        doc_lower = entry["doc"].lower()
        if all(qt in doc_lower for qt in query_tokens):
            return 15

    return 0


def _score_tokens(query_tokens, name_tokens):
    """Score query tokens against name tokens.

    Returns 50 if all query tokens exactly match a name token, 45 if all
    query tokens are prefixes of some name token, or 0 if any query token
    has no match.

    Each query token is matched independently. A name token can only be
    claimed by one query token (greedy, but order-independent).
    """
    if not query_tokens or not name_tokens:
        return 0

    # Try exact token matches first.
    available = list(name_tokens)
    all_exact = True
    for qt in query_tokens:
        found = False
        for i, nt in enumerate(available):
            if nt == qt:
                available.pop(i)
                found = True
                break
        if not found:
            all_exact = False
            break

    if all_exact:
        return 50

    # Try prefix token matches.
    available  = list(name_tokens)
    all_prefix = True
    for qt in query_tokens:
        found = False
        for i, nt in enumerate(available):
            if nt.startswith(qt) or qt.startswith(nt):
                available.pop(i)
                found = True
                break
        if not found:
            all_prefix = False
            break

    if all_prefix:
        return 45

    return 0


def _score_fuzzy(entry, query_lower):
    """Score an entry using edit distance as a last resort.

    Only called when no other scoring method produced a match. Compares
    the query against the unqualified name. For queries longer than 4
    characters:
    - Edit distance 1 scores 40.
    - Edit distance 2 scores 25.
    - Greater distances score 0.
    """
    unqualified = entry["name"].rsplit(".", 1)[-1].lower()

    # Compare against full unqualified name.
    dist = _edit_distance(query_lower, unqualified)
    if dist <= 1:
        return 40
    if dist <= 2:
        return 25

    # Also try matching query as a prefix of the name (for truncated queries
    # like "GetPipelineStat" matching "GetPipelineState").
    if len(query_lower) >= 5 and len(unqualified) > len(query_lower):
        prefix_dist = _edit_distance(query_lower, unqualified[:len(query_lower)])
        if prefix_dist == 0:
            # Exact prefix of the name, just truncated.
            return 40
        if prefix_dist <= 1:
            return 30

    return 0


# --- Module Walker ---

def _walk_module(obj, prefix, entries, visited):
    """Recursively enumerate members of a module or class.

    Walks public attributes of obj, classifying each as a class, enum,
    method, or property and appending index entries. Recurses into classes
    and enums.
    """
    obj_id = id(obj)
    if obj_id in visited:
        return
    visited.add(obj_id)

    for name in sorted(dir(obj)):
        if name.startswith("_"):
            continue

        try:
            member = getattr(obj, name)
        except Exception:
            continue

        qualified = f"{prefix}.{name}"
        doc       = getattr(member, "__doc__", None) or ""

        kind = _classify(member)

        if kind == "enum":
            # Record the enum type itself.
            entries.append({
                "name":      qualified,
                "kind":      "enum",
                "doc":       doc,
                "signature": None,
            })
            # Extract individual enum values from __members__.
            _walk_enum_members(member, qualified, entries)
            continue

        if kind is not None:
            entries.append({
                "name":      qualified,
                "kind":      kind,
                "doc":       doc,
                "signature": _get_signature(member) if kind == "method" else None,
            })

        # Recurse into class types to pick up methods and properties.
        if kind == "class":
            _walk_class(member, qualified, entries, visited)


def _walk_class(cls, prefix, entries, visited):
    """Walk the members of a class, emitting methods and properties.

    Unlike _walk_module, this checks for property descriptors using
    inspect.getattr_static to avoid invoking descriptors.
    """
    cls_id = id(cls)
    if cls_id in visited:
        return
    visited.add(cls_id)

    for name in sorted(dir(cls)):
        if name.startswith("_"):
            continue

        # Use getattr_static to inspect descriptors without triggering them.
        try:
            static_attr = inspect.getattr_static(cls, name)
        except Exception:
            continue

        qualified = f"{prefix}.{name}"

        # Check for property descriptors (both builtin property and
        # SWIG-style descriptors that have fget or __doc__).
        if _is_property_descriptor(static_attr):
            doc = getattr(static_attr, "__doc__", None) or ""
            entries.append({
                "name":      qualified,
                "kind":      "property",
                "doc":       doc,
                "signature": None,
            })
            continue

        # Fall back to regular attribute access for non-descriptors.
        try:
            member = getattr(cls, name)
        except Exception:
            continue

        doc  = getattr(member, "__doc__", None) or ""
        kind = _classify(member)

        if kind == "enum":
            entries.append({
                "name":      qualified,
                "kind":      "enum",
                "doc":       doc,
                "signature": None,
            })
            _walk_enum_members(member, qualified, entries)
        elif kind == "class":
            entries.append({
                "name":      qualified,
                "kind":      "class",
                "doc":       doc,
                "signature": None,
            })
            _walk_class(member, qualified, entries, visited)
        elif kind == "method":
            entries.append({
                "name":      qualified,
                "kind":      "method",
                "doc":       doc,
                "signature": _get_signature(member),
            })


def _walk_enum_members(enum_cls, prefix, entries):
    """Extract individual values from a SWIG-generated enum type.

    SWIG enums expose their members via a __members__ dict mapping
    name strings to enum values. Each member becomes an "enum_value"
    entry with the integer value included in the doc field.
    """
    members = getattr(enum_cls, "__members__", None)
    if not isinstance(members, dict):
        return

    for member_name, member_value in sorted(members.items()):
        qualified  = f"{prefix}.{member_name}"
        member_doc = getattr(member_value, "__doc__", None) or ""

        # Include the integer value for quick reference.
        try:
            int_val   = int(member_value)
            value_str = f"Integer value: {int_val}"
            if member_doc:
                doc = f"{member_doc}\n\n{value_str}"
            else:
                doc = value_str
        except (ValueError, TypeError):
            doc = member_doc

        entries.append({
            "name":      qualified,
            "kind":      "enum_value",
            "doc":       doc,
            "signature": None,
        })


# --- Classification ---

def _classify(obj):
    """Classify a Python object as class, method, enum, or None.

    RenderDoc enums are SWIG-generated IntEnum-like types. They are
    distinguished from regular classes by the presence of a __members__
    attribute (SWIG enum pattern) or by inheriting from int.

    Properties are not detected here. They require descriptor-level
    inspection via getattr_static, which is handled in _walk_class.
    """
    if isinstance(obj, type):
        if _is_swig_enum(obj):
            return "enum"
        return "class"
    elif callable(obj):
        return "method"
    else:
        return None


def _is_swig_enum(cls):
    """Check if a type is a SWIG-generated enum.

    SWIG enums are identified by either:
    - Having a __members__ dict attribute (SWIG enum convention).
    - Inheriting from int (IntEnum-like pattern).
    """
    has_members    = isinstance(getattr(cls, "__members__", None), dict)
    inherits_int   = issubclass(cls, int)
    return has_members or inherits_int


def _is_property_descriptor(obj):
    """Check if an object is a property descriptor.

    Detects both builtin property objects and SWIG-style descriptors
    that expose fget or are otherwise non-callable descriptors with
    __doc__.
    """
    if isinstance(obj, property):
        return True

    # SWIG property descriptors: have __get__ (descriptor protocol) but
    # are not regular functions or types.
    obj_type = type(obj)
    if obj_type.__name__ in ("getset_descriptor", "member_descriptor"):
        return True

    # Generic descriptor with fget (some SWIG versions).
    if hasattr(obj, "fget") and not isinstance(obj, type) and not callable(obj):
        return True

    return False


# --- Signature Extraction ---

def _get_signature(obj):
    """Extract a signature string from a callable.

    Tries inspect.signature first. When that fails (common for
    SWIG-wrapped methods), falls back to parsing the first line
    of the docstring, which SWIG conventionally formats as:
        method_name(arg1, arg2) -> ReturnType
    """
    if not callable(obj):
        return None

    # Preferred path: inspect.signature works on native Python callables.
    try:
        return str(inspect.signature(obj))
    except (ValueError, TypeError):
        pass

    # Fallback: parse SWIG-style docstring signature.
    return _parse_docstring_signature(obj)


# Pattern for SWIG docstring signatures.
# Matches lines like: "method_name(arg1, arg2) -> ReturnType"
# or just: "method_name(arg1, arg2)"
_SWIG_SIG_RE = re.compile(
    r"^\s*\w+\s*\(([^)]*)\)(?:\s*->\s*(.+))?\s*$"
)


def _parse_docstring_signature(obj):
    """Parse a signature from the first line of a SWIG-style docstring.

    Returns a signature string like "(arg1, arg2) -> ReturnType", or
    None if the docstring does not contain a recognizable signature.
    """
    doc = getattr(obj, "__doc__", None)
    if not doc:
        return None

    first_line = doc.strip().split("\n", 1)[0]
    match      = _SWIG_SIG_RE.match(first_line)
    if not match:
        return None

    args       = match.group(1).strip()
    ret        = match.group(2)
    sig        = f"({args})"
    if ret:
        sig = f"{sig} -> {ret.strip()}"

    return sig
