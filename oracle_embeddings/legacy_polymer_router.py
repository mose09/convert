"""Polymer source scanner that builds ``url → {component, file_path}`` maps.

Polymer apps don't have a single canonical router (unlike React Router), so
this module recognises three of the most common patterns we see in the wild:

  * ``@vaadin/router`` ``router.setRoutes([{path: '/x', component: 'x-tag'}])``
  * Object-style route configs with ``component: 'x-tag'`` (any JS router)
  * ``page.js`` style ``page('/url', () => this.page = 'name')`` paired with
    ``<x-tag name="name">`` selectors inside an ``<iron-pages>`` shell
  * Polymer ``app-route`` ``<app-route pattern="/foo/:bar">`` (best-effort,
    static prefix only)

Custom elements are indexed from any of:

  * ``customElements.define('x-tag', ClassName)`` (Polymer 3 / LitElement)
  * ``Polymer({is: 'x-tag', ...})`` (Polymer 1/2 hybrid syntax)
  * ``static get is() { return 'x-tag'; }`` (Polymer 2 class syntax)
  * ``<dom-module id="x-tag">`` inside ``.html`` files (Polymer 2 native)
  * Filename convention ``x-tag.html`` / ``x-tag.js``

The output dict has the same shape as ``legacy_react_router`` so the
analyzer can use either source interchangeably.
"""

import logging
import os
import re

from .legacy_util import normalize_url
from .mybatis_parser import _read_file_safe

logger = logging.getLogger(__name__)


SKIP_DIRS = {"node_modules", "build", "dist", "bower_components", ".git", "coverage"}
EXTENSIONS = {".js", ".html", ".ts", ".mjs"}


def scan_polymer_dir(base_dir: str) -> list[str]:
    """Return absolute paths of all Polymer source files under ``base_dir``."""
    files = []
    for root, dirs, names in os.walk(base_dir):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for n in names:
            if os.path.splitext(n)[1].lower() in EXTENSIONS:
                files.append(os.path.join(root, n))
    return files


# customElements.define('x-tag', ClassName)
_CE_DEFINE_RE = re.compile(
    r"""customElements\s*\.\s*define\s*\(\s*
        (?P<q>["'`])(?P<tag>[a-z][a-z0-9-]*-[a-z0-9-]+)(?P=q)
    """,
    re.VERBOSE,
)

# Polymer({is: 'x-tag', ...})
_POLYMER_IS_RE = re.compile(
    r"""Polymer\s*\(\s*\{\s*[^}]*?
        \bis\s*:\s*(?P<q>["'`])(?P<tag>[a-z][a-z0-9-]*-[a-z0-9-]+)(?P=q)
    """,
    re.VERBOSE | re.DOTALL,
)

# static get is() { return 'x-tag'; }
_POLYMER_STATIC_IS_RE = re.compile(
    r"""static\s+get\s+is\s*\(\s*\)\s*\{\s*return\s+
        (?P<q>["'`])(?P<tag>[a-z][a-z0-9-]*-[a-z0-9-]+)(?P=q)\s*;?\s*\}
    """,
    re.VERBOSE,
)

# <dom-module id="x-tag">
_DOM_MODULE_RE = re.compile(
    r"""<dom-module\b[^>]*\bid\s*=\s*(?P<q>["'])(?P<tag>[a-z][a-z0-9-]*-[a-z0-9-]+)(?P=q)""",
    re.VERBOSE,
)


def _is_kebab_tag(name: str) -> bool:
    """Return True if ``name`` looks like a custom element tag (foo-bar)."""
    return bool(re.match(r"^[a-z][a-z0-9-]*-[a-z0-9-]+$", name))


def build_custom_element_index(files: list[str]) -> dict:
    """Return ``{tag: file_path}`` for every custom element declaration.

    Strategy:
      1) Filename convention: ``x-tag.html``/``x-tag.js`` → ``x-tag``
      2) Scan file content for ``customElements.define`` /
         ``Polymer({is: ...})`` / ``static get is()`` / ``<dom-module id>``
    """
    index = {}
    for fp in files:
        base = os.path.splitext(os.path.basename(fp))[0].lower()
        if _is_kebab_tag(base):
            index.setdefault(base, fp)
        try:
            content = _read_file_safe(fp, limit=8000)
        except Exception:
            continue
        for rx in (_CE_DEFINE_RE, _POLYMER_IS_RE, _POLYMER_STATIC_IS_RE, _DOM_MODULE_RE):
            for m in rx.finditer(content):
                tag = m.group("tag").lower()
                index.setdefault(tag, fp)
    return index


# ---------------------------------------------------------------------------
# Routing patterns
# ---------------------------------------------------------------------------

# vaadin-router / generic JS object route entry: { path: '/x', component: 'x-tag' }
_OBJ_ROUTE_RE = re.compile(
    r"""\{\s*
        (?:[^{}]*?,\s*)?
        path\s*:\s*(?P<q1>["'`])(?P<path>[^"'`]+)(?P=q1)
        [^{}]*?
        component\s*:\s*(?P<q2>["'`])(?P<tag>[a-z][a-z0-9-]*-[a-z0-9-]+)(?P=q2)
    """,
    re.VERBOSE | re.DOTALL,
)

# Same as above but reversed order: { component: 'x-tag', path: '/x' }
_OBJ_ROUTE_REV_RE = re.compile(
    r"""\{\s*
        (?:[^{}]*?,\s*)?
        component\s*:\s*(?P<q2>["'`])(?P<tag>[a-z][a-z0-9-]*-[a-z0-9-]+)(?P=q2)
        [^{}]*?
        path\s*:\s*(?P<q1>["'`])(?P<path>[^"'`]+)(?P=q1)
    """,
    re.VERBOSE | re.DOTALL,
)

# page.js style: page('/url', ...) — capture path; tag resolved separately
_PAGE_JS_RE = re.compile(
    r"""(?<![A-Za-z0-9_$.])
        page\s*\(\s*(?P<q>["'`])(?P<path>/[^"'`]*)(?P=q)
    """,
    re.VERBOSE,
)

# `this.page = 'name'` or `this.set('page', 'name')` — captures the page slug
_PAGE_ASSIGN_RE = re.compile(
    r"""this\.(?:page\s*=\s*|set\s*\(\s*(?P<q1>["'`])page(?P=q1)\s*,\s*)
        (?P<q2>["'`])(?P<name>[a-z0-9-]+)(?P=q2)
    """,
    re.VERBOSE,
)

# <x-tag name="name"> or <x-tag page-name="name"> inside iron-pages — best effort
_IRON_PAGES_CHILD_RE = re.compile(
    r"""<(?P<tag>[a-z][a-z0-9-]*-[a-z0-9-]+)\b[^>]*\bname\s*=\s*(?P<q>["'])(?P<name>[a-z0-9-]+)(?P=q)
    """,
    re.VERBOSE,
)

# <app-route pattern="/foo/:bar">
_APP_ROUTE_RE = re.compile(
    r"""<app-route\b[^>]*\bpattern\s*=\s*(?P<q>["'])(?P<path>[^"']+)(?P=q)""",
    re.VERBOSE,
)


def _extract_object_routes(content: str) -> list[dict]:
    """Return ``[{path, component}]`` from object-literal route configs."""
    out = []
    seen = set()

    def _add(path: str, tag: str):
        key = (path, tag)
        if path and tag and key not in seen:
            seen.add(key)
            out.append({"path": path, "component": tag.lower()})

    for m in _OBJ_ROUTE_RE.finditer(content):
        _add(m.group("path"), m.group("tag"))
    for m in _OBJ_ROUTE_REV_RE.finditer(content):
        _add(m.group("path"), m.group("tag"))
    return out


def _extract_pagejs_pairs(content: str) -> list[dict]:
    """Pair ``page('/url')`` calls with the ``this.page = 'name'`` slug.

    Walks the source linearly: when a ``page('/url', ...)`` call is found,
    we look ahead a few hundred characters for the slug assignment within
    the same callback. Returns ``[{url, slug}]``.
    """
    out = []
    for m in _PAGE_JS_RE.finditer(content):
        url = m.group("path")
        # Look ahead in the same statement for a slug assignment (callback body).
        window = content[m.end(): m.end() + 400]
        slug_match = _PAGE_ASSIGN_RE.search(window)
        slug = slug_match.group("name") if slug_match else ""
        out.append({"url": url, "slug": slug})
    return out


def _extract_iron_pages_children(content: str) -> dict:
    """Return ``{slug: tag}`` from ``<x-tag name="slug">`` children of iron-pages.

    We don't actually require the parent to be ``<iron-pages>`` since some
    apps use ``<neon-animated-pages>`` etc.; the ``name="..."`` attribute is
    the connection.
    """
    out = {}
    for m in _IRON_PAGES_CHILD_RE.finditer(content):
        out.setdefault(m.group("name"), m.group("tag").lower())
    return out


def _extract_app_routes(content: str) -> list[str]:
    """Return raw ``pattern`` strings from ``<app-route pattern="...">``."""
    return [m.group("path") for m in _APP_ROUTE_RE.finditer(content)]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_url_to_component_map(polymer_dir: str, strip_patterns=None,
                                route_prefix: str | None = None) -> dict:
    """Scan ``polymer_dir`` and return ``{normalized_url: {component, file_path, declared_in}}``.

    Walks every file; for each detected route entry we resolve the custom
    element tag back to its declaring file via ``build_custom_element_index``.
    page.js + iron-pages routes are paired by slug.

    ``strip_patterns`` / ``route_prefix`` forwarded to
    :func:`legacy_util.normalize_url` for URL-convention alignment with
    the menu and controller sides.
    """
    if not polymer_dir or not os.path.isdir(polymer_dir):
        return {}

    files = scan_polymer_dir(polymer_dir)
    if not files:
        logger.info("Polymer scan: no source files in %s", polymer_dir)
        return {}

    tag_index = build_custom_element_index(files)
    logger.info("Polymer custom-element index: %d tags", len(tag_index))

    prefix = route_prefix or ""

    url_map = {}

    def _record(url: str, tag: str, declared_in: str):
        key = normalize_url(prefix + url, strip_patterns)
        if not key or not tag:
            return
        file_path = tag_index.get(tag.lower(), "")
        url_map.setdefault(key, {
            "component": tag,
            "file_path": file_path,
            "declared_in": declared_in,
        })

    for fp in files:
        try:
            content = _read_file_safe(fp)
        except Exception:
            continue

        # 1) Object-literal route configs (vaadin-router, generic)
        for r in _extract_object_routes(content):
            _record(r["path"], r["component"], fp)

        # 2) page.js + iron-pages slug pairing — within the SAME file the
        #    slug → tag map usually lives next to the page() calls.
        if "page(" in content:
            slug_to_tag = _extract_iron_pages_children(content)
            for pair in _extract_pagejs_pairs(content):
                tag = slug_to_tag.get(pair["slug"], "")
                if tag:
                    _record(pair["url"], tag, fp)
                else:
                    # No slug pairing — try to guess by URL-derived tag
                    # (e.g., '/order/list' → 'x-order-list' if exists)
                    pass

        # 3) <app-route pattern="..."> — Polymer 1/2 routing
        for path in _extract_app_routes(content):
            # The actual page tag is selected separately via slug, so we
            # mainly use this to register URL prefixes. We pair with any
            # slug map present in the same file.
            if "page(" not in content:
                slug_to_tag = _extract_iron_pages_children(content)
                # If only one child, use that as the resolved tag (best effort)
                if len(slug_to_tag) == 1:
                    tag = next(iter(slug_to_tag.values()))
                    _record(path, tag, fp)

    logger.info("Polymer URL map: %d entries from %d files", len(url_map), len(files))
    return url_map
