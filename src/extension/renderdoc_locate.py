"""Locate and load the RenderDoc Python module for headless replay.

Strategy (highest priority first):

1. AGENTIC_RENDERDOC_HOME env var: explicit override. Treated as a
   RenderDoc install root; we search well-known relative locations
   (``lib``, ``python``, ``share/renderdoc/python``, etc.) for the
   ``renderdoc.so`` / ``renderdoc.pyd`` SWIG module and the matching
   ``librenderdoc.so`` / ``renderdoc.dll``.

2. Sibling of ``renderdoccmd`` / ``qrenderdoc`` on PATH: when RenderDoc
   is installed system-wide, the Python module lives in a sibling
   directory of the binaries (``../lib``, ``../share/renderdoc/python``).

3. Common install dirs: ``/usr/share/renderdoc``, ``/opt/renderdoc``,
   ``%LOCALAPPDATA%\\Programs\\RenderDoc``, ``C:\\Program Files\\RenderDoc``.

4. Common build-output locations under ``$HOME``: development builds
   often live under ``~/.local/renderdoc-build/...`` or
   ``~/dev/.../renderdoc/build/lib``. We scan a small set of these.

A located candidate is validated by attempting an ``import renderdoc``
with the candidate directory prepended to ``sys.path`` and the matching
shared library preloaded with ``RTLD_GLOBAL``. The first candidate that
imports cleanly wins.
"""
from __future__ import annotations

import ctypes
import importlib
import os
import shutil
import sys
from pathlib import Path


# --- Platform-specific names ---

if sys.platform == "win32":
    _LIB_NAME = "renderdoc.dll"
elif sys.platform.startswith("linux"):
    _LIB_NAME = "librenderdoc.so"
elif sys.platform == "darwin":
    _LIB_NAME = "librenderdoc.dylib"
else:
    _LIB_NAME = "librenderdoc.so"

# The SWIG Python module ships under several names. ``renderdoc.so``
# (Linux) or ``renderdoc.pyd`` (Windows) is the common case; some builds
# tag with the Python ABI version (``renderdoc.cpython-314-x86_64-linux-gnu.so``).
_PY_MODULE_PATTERNS = (
    "renderdoc.so",
    "renderdoc.pyd",
    "renderdoc.*.so",
    "renderdoc.*.pyd",
)


class RenderDocLocateError(RuntimeError):
    """Raised when the RenderDoc Python module cannot be located or loaded."""


def setup() -> dict:
    """Locate, preload, and import the RenderDoc Python module.

    On success, returns a dict with the resolved paths::

        {"library": "/path/to/librenderdoc.so",
         "python_dir": "/path/to/dir/with/renderdoc.so"}

    The ``renderdoc`` (and, when available, ``qrenderdoc``) modules are
    importable in the current process after this call returns.

    Raises RenderDocLocateError on failure with a message describing
    what was tried.
    """
    candidates = list(_iter_candidates())
    if not candidates:
        raise RenderDocLocateError(
            "no RenderDoc install found; set AGENTIC_RENDERDOC_HOME to a "
            "RenderDoc install root, or install RenderDoc system-wide."
        )

    tried: list[str] = []
    for python_dir, library_path in candidates:
        try:
            _try_load(python_dir, library_path)
        except Exception as e:
            tried.append(f"{python_dir} (lib={library_path}): {e}")
            continue

        return {
            "library"    : str(library_path) if library_path else None,
            "python_dir" : str(python_dir),
        }

    raise RenderDocLocateError(
        "no RenderDoc Python module candidate loaded successfully. "
        "Tried:\n  " + "\n  ".join(tried)
    )


# --- Internals ---

def _try_load(python_dir: Path, library_path: Path | None) -> None:
    """Attempt to import the renderdoc module from python_dir.

    Preloads librenderdoc with RTLD_GLOBAL when a path is supplied so
    the SWIG module can resolve symbols against an already-loaded copy.
    Raises on any failure.
    """
    # Preload the shared library if we have a concrete path. RTLD_GLOBAL
    # makes its symbols available for subsequent imports of the SWIG
    # module that link against them.
    if library_path is not None and library_path.exists():
        flags = getattr(os, "RTLD_NOW", 2) | getattr(os, "RTLD_GLOBAL", 0x100)
        # Best-effort: ctypes.CDLL accepts mode= on POSIX; on Windows
        # the flag set differs but the same call works for our purposes.
        try:
            ctypes.CDLL(str(library_path), mode=flags)
        except TypeError:
            ctypes.CDLL(str(library_path))

    # Drop a stale renderdoc module if a previous attempt cached one.
    sys.modules.pop("renderdoc", None)
    sys.modules.pop("qrenderdoc", None)

    python_dir_s = str(python_dir)
    inserted = False
    if python_dir_s not in sys.path:
        sys.path.insert(0, python_dir_s)
        inserted = True

    try:
        importlib.import_module("renderdoc")
    except Exception:
        if inserted:
            try:
                sys.path.remove(python_dir_s)
            except ValueError:
                pass
        raise


def _iter_candidates():
    """Yield (python_dir, library_path | None) pairs to try, in priority order.

    Each tuple represents a single load attempt. The library_path is
    optional — when omitted, no preload is performed and we trust the
    OS dynamic linker.
    """
    seen: set[tuple[str, str]] = set()

    def emit(python_dir: Path, library_path: Path | None):
        key = (str(python_dir), str(library_path) if library_path else "")
        if key in seen:
            return None
        seen.add(key)
        return (python_dir, library_path)

    # 1. Explicit env override.
    env_home = os.environ.get("AGENTIC_RENDERDOC_HOME")
    if env_home:
        for cand in _candidates_under_root(Path(env_home)):
            r = emit(*cand)
            if r is not None:
                yield r

    # 2. Sibling of binaries on PATH.
    for binary in ("renderdoccmd", "qrenderdoc"):
        path = shutil.which(binary)
        if not path:
            continue
        bin_dir = Path(path).resolve().parent
        # Walk up one level to get the install root, then try standard
        # subdirectories.
        for root in (bin_dir.parent, bin_dir):
            for cand in _candidates_under_root(root):
                r = emit(*cand)
                if r is not None:
                    yield r

    # 3. Standard install dirs.
    if sys.platform.startswith("linux"):
        roots = [
            Path("/usr/share/renderdoc"),
            Path("/opt/renderdoc"),
            Path("/usr/local/share/renderdoc"),
            Path("/usr/local"),
            Path("/usr"),
        ]
    elif sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        roots = []
        if local:
            roots.append(Path(local) / "Programs" / "RenderDoc")
        roots.append(Path(r"C:\Program Files\RenderDoc"))
    else:
        roots = []

    for root in roots:
        for cand in _candidates_under_root(root):
            r = emit(*cand)
            if r is not None:
                yield r

    # 4. Common build-output locations under $HOME.
    home = Path.home()
    home_roots = [
        home / ".local" / "renderdoc",
        home / ".local" / "renderdoc-build" / "renderdoc" / "build",
    ]
    for root in home_roots:
        for cand in _candidates_under_root(root):
            r = emit(*cand)
            if r is not None:
                yield r


def _candidates_under_root(root: Path):
    """Yield (python_dir, library_path) pairs for a given install root."""
    if not root.exists():
        return

    library = _find_library_under(root)

    # Common subdirs where renderdoc.so / .pyd lives.
    py_subdirs = (
        "lib",
        "python",
        "pymodules",
        Path("share") / "renderdoc" / "python",
        Path("lib") / "python",
        ".",
    )

    for sub in py_subdirs:
        d = root / sub
        if not d.is_dir():
            continue
        if _has_python_module(d):
            yield (d, library)


def _has_python_module(d: Path) -> bool:
    """True if d contains a renderdoc Python SWIG module."""
    for pat in _PY_MODULE_PATTERNS:
        if any(d.glob(pat)):
            return True
    return False


def _find_library_under(root: Path) -> Path | None:
    """Locate the librenderdoc shared library under root, if present."""
    candidates = (
        root / "lib" / _LIB_NAME,
        root / _LIB_NAME,
        root / "bin" / _LIB_NAME,
    )
    for c in candidates:
        if c.exists():
            return c

    # As a last resort, do a shallow glob.
    matches = list(root.glob(_LIB_NAME))
    if matches:
        return matches[0]
    matches = list(root.glob(f"*/{_LIB_NAME}"))
    if matches:
        return matches[0]

    return None
