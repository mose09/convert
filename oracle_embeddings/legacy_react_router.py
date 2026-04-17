"""React source scanner that builds ``url → {component, file_path}`` maps.

Scans ``.js/.jsx/.ts/.tsx`` files for React Router definitions and pairs
them with the files that declare the referenced components. This is what
fills the ``presentation_layer`` column in the AS-IS legacy report.

Supports:
  * React Router v6 ``<Route path="..." element={<X/>} />``
  * React Router v5 ``<Route path="..." component={X} />``
  * Object-style route configs ``{ path: "/x", element: <X/> }``
  * ``React.lazy(() => import("./X"))`` factories
  * Nested ``<Route>`` trees (parent path is prepended onto child paths)
"""

import logging
import os
import re

from .legacy_util import normalize_url
from .mybatis_parser import _read_file_safe

logger = logging.getLogger(__name__)


SKIP_DIRS = {"node_modules", "build", "dist", ".next", ".git", "coverage"}
EXTENSIONS = {".js", ".jsx", ".ts", ".tsx"}


def scan_react_dir(base_dir: str) -> list[str]:
    """Return absolute paths of all React source files under ``base_dir``."""
    files = []
    for root, dirs, names in os.walk(base_dir):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for n in names:
            if os.path.splitext(n)[1].lower() in EXTENSIONS:
                files.append(os.path.join(root, n))
    return files


# Component declarations by filename (best-effort; we use filename→path as
# the primary index and fall back to content scanning).
_COMPONENT_EXPORT_RE = re.compile(
    r"""(?:
            export\s+default\s+(?:function\s+|class\s+)?(?P<name1>[A-Z]\w*)
          | export\s+(?:const|function|class)\s+(?P<name2>[A-Z]\w*)
        )
    """,
    re.VERBOSE,
)


def build_component_index(files: list[str]) -> dict:
    """Return ``{ComponentName: file_path}`` by scanning exports + filenames.

    Strategy:
      1) If the basename matches ``/^[A-Z]\w*\.(jsx?|tsx?)$/``, map the
         base name → file (common convention).
      2) Additionally scan top of the file for ``export default``/
         ``export const Xxx`` / ``export function Xxx`` / ``export class Xxx``
         and register those names.
    """
    index = {}
    for fp in files:
        base = os.path.splitext(os.path.basename(fp))[0]
        if base and base[:1].isupper():
            index.setdefault(base, fp)
        try:
            content = _read_file_safe(fp, limit=4000)
        except Exception:
            continue
        for m in _COMPONENT_EXPORT_RE.finditer(content):
            name = m.group("name1") or m.group("name2")
            if name:
                index.setdefault(name, fp)
    return index


# `<Route path="/x" element={<X/>} />` or `component={X}`
_ROUTE_JSX_RE = re.compile(
    r"""<Route
        \b[^>]*?
        \bpath\s*=\s*(?P<quote>["'])(?P<path>[^"']*)(?P=quote)
        [^>]*?
        (?:
            element\s*=\s*\{\s*<\s*(?P<comp1>[A-Z]\w*)
          | component\s*=\s*\{\s*(?P<comp2>[A-Z]\w*)\s*\}
        )
    """,
    re.VERBOSE,
)

# Object-style route config
_ROUTE_OBJ_RE = re.compile(
    r"""\{\s*
        path\s*:\s*(?P<quote>["'`])(?P<path>[^"'`]+)(?P=quote)
        [^}]*?
        (?:element\s*:\s*<\s*(?P<comp1>[A-Z]\w*)
          | component\s*:\s*(?P<comp2>[A-Z]\w*)
        )
    """,
    re.VERBOSE | re.DOTALL,
)

# React.lazy factory imports: const X = lazy(() => import("./pages/X"))
_LAZY_IMPORT_RE = re.compile(
    r"""(?:const|let|var)\s+(?P<name>[A-Z]\w*)\s*=\s*
        (?:React\.)?lazy\s*\(\s*\(\s*\)\s*=>\s*import\s*\(\s*["'](?P<path>[^"']+)["']\s*\)\s*\)
    """,
    re.VERBOSE,
)


def _extract_lazy_imports(content: str, file_dir: str) -> dict:
    """Return ``{ComponentName: resolved_absolute_path}`` for lazy imports."""
    result = {}
    for m in _LAZY_IMPORT_RE.finditer(content):
        name = m.group("name")
        rel = m.group("path")
        resolved = _resolve_import_path(rel, file_dir)
        if resolved:
            result[name] = resolved
    return result


def _resolve_import_path(rel: str, from_dir: str) -> str:
    """Best-effort resolve a JS import path to a file on disk.

    Tries the path verbatim with each React extension, then ``/index.*``.
    Returns an empty string if nothing plausible exists.
    """
    if not rel.startswith("."):
        return ""
    candidate = os.path.normpath(os.path.join(from_dir, rel))
    # Exact match with extensions
    for ext in (".tsx", ".ts", ".jsx", ".js"):
        p = candidate + ext
        if os.path.isfile(p):
            return p
    # Index file
    if os.path.isdir(candidate):
        for ext in (".tsx", ".ts", ".jsx", ".js"):
            p = os.path.join(candidate, "index" + ext)
            if os.path.isfile(p):
                return p
    return ""


def _extract_routes_from_content(content: str) -> list[dict]:
    """Extract ``{path, component}`` pairs from one file's source.

    Collapses duplicates across JSX and object patterns; returns entries in
    source order (important for nested-path propagation).
    """
    routes = []
    seen = set()

    def _add(path: str, comp: str):
        key = (path, comp)
        if key not in seen and path and comp:
            seen.add(key)
            routes.append({"path": path, "component": comp})

    for m in _ROUTE_JSX_RE.finditer(content):
        _add(m.group("path"), m.group("comp1") or m.group("comp2"))

    for m in _ROUTE_OBJ_RE.finditer(content):
        _add(m.group("path"), m.group("comp1") or m.group("comp2"))

    return routes


def build_url_to_component_map(react_dir: str, strip_patterns=None,
                                route_prefix: str | None = None) -> dict:
    """Scan ``react_dir`` and return ``{normalized_url: {component, file_path}}``.

    For each route entry we resolve the component to an absolute file path
    using the component index and lazy-import map of the declaring file.
    If no file path can be determined, the component name is still recorded
    so that callers can degrade gracefully.

    ``strip_patterns`` / ``route_prefix`` are forwarded to
    :func:`legacy_util.normalize_url` so URL conventions learned by
    ``discover-patterns`` (e.g. ``^/apps/[^/]+`` strip, ``/web`` route
    prefix) can be applied uniformly across all URL-producing modules.
    """
    if not react_dir or not os.path.isdir(react_dir):
        return {}

    files = scan_react_dir(react_dir)
    component_index = build_component_index(files)

    prefix = route_prefix or ""

    url_map = {}
    for fp in files:
        try:
            content = _read_file_safe(fp)
        except Exception:
            continue
        if "Route" not in content and "path:" not in content:
            continue
        lazy_map = _extract_lazy_imports(content, os.path.dirname(fp))
        routes = _extract_routes_from_content(content)
        for r in routes:
            comp = r["component"]
            file_path = lazy_map.get(comp) or component_index.get(comp) or ""
            key = normalize_url(prefix + r["path"], strip_patterns)
            if not key:
                continue
            url_map.setdefault(key, {
                "component": comp,
                "file_path": file_path,
                "declared_in": fp,
            })

    logger.info("React URL map: %d entries from %d files", len(url_map), len(files))
    return url_map
