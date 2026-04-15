"""Frontend dispatcher: auto-detect React vs Polymer and build the URL map.

The legacy analyzer used to call ``legacy_react_router`` directly. Real
projects ship with different SPA stacks though, so this module sniffs the
frontend directory and forwards to the correct router parser.

Detection signals (highest priority first):

  1. ``package.json`` dependencies — ``react`` / ``react-dom`` → React,
     ``@polymer/*`` / ``polymer`` / ``@vaadin/router`` / ``lit-element`` /
     ``@webcomponents/*`` → Polymer.
  2. File extension distribution under ``src/`` — many ``.tsx/.jsx`` →
     React; many ``.html`` files containing ``<dom-module>`` or
     ``customElements.define`` → Polymer.
  3. Content sampling on a handful of files — ``import React`` /
     ``from 'react'`` → React; ``Polymer({`` / ``customElements.define`` /
     ``extends PolymerElement`` / ``extends LitElement`` → Polymer.

Returns one of ``"react"``, ``"polymer"``, or ``"unknown"``. The caller
can override detection via the ``framework`` parameter.
"""

import json
import logging
import os
import re

logger = logging.getLogger(__name__)


REACT_DEP_KEYS = ("react", "react-dom", "react-router", "react-router-dom", "next")
POLYMER_DEP_KEYS = (
    "@polymer/", "polymer", "@vaadin/router", "lit-element", "lit",
    "@webcomponents/", "@lit/",
)


def _read_package_json(frontend_dir: str) -> dict | None:
    path = os.path.join(frontend_dir, "package.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("package.json parse failed: %s", e)
        return None


def _check_deps(pkg: dict) -> str:
    """Return ``"react"`` / ``"polymer"`` / ``""`` based on package.json deps."""
    if not pkg:
        return ""
    deps = {}
    for k in ("dependencies", "devDependencies", "peerDependencies"):
        d = pkg.get(k) or {}
        if isinstance(d, dict):
            deps.update(d)
    keys = list(deps.keys())
    react_hit = any(k in keys for k in REACT_DEP_KEYS)
    polymer_hit = any(
        any(k.startswith(prefix) if prefix.endswith("/") else k == prefix
            for prefix in POLYMER_DEP_KEYS)
        for k in keys
    )
    if react_hit and not polymer_hit:
        return "react"
    if polymer_hit and not react_hit:
        return "polymer"
    if react_hit and polymer_hit:
        # Mixed project — fall back to file/content sampling to break the tie
        return ""
    return ""


_REACT_IMPORT_RE = re.compile(
    r"""(?:import\s+[^;\n]*\bfrom\s+["']react(?:-dom|-router(?:-dom)?)?["']
        | import\s+React\b
        | from\s+["']next/)""",
    re.VERBOSE,
)
_POLYMER_HINT_RE = re.compile(
    r"""(?:customElements\s*\.\s*define
        | Polymer\s*\(\s*\{
        | extends\s+PolymerElement
        | extends\s+LitElement
        | from\s+["']@polymer/
        | from\s+["']@vaadin/router["']
        | <dom-module\b)
    """,
    re.VERBOSE,
)


def _content_sample_score(frontend_dir: str, max_files: int = 40) -> tuple[int, int]:
    """Return ``(react_hits, polymer_hits)`` from a small file sample."""
    react_hits = 0
    polymer_hits = 0
    seen = 0
    for root, dirs, names in os.walk(frontend_dir):
        dirs[:] = [d for d in dirs
                   if d not in {"node_modules", "build", "dist", ".git", "bower_components"}]
        for n in names:
            ext = os.path.splitext(n)[1].lower()
            if ext not in {".js", ".jsx", ".ts", ".tsx", ".html", ".mjs"}:
                continue
            path = os.path.join(root, n)
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    chunk = f.read(4000)
            except Exception:
                continue
            if _REACT_IMPORT_RE.search(chunk):
                react_hits += 1
            if _POLYMER_HINT_RE.search(chunk):
                polymer_hits += 1
            seen += 1
            if seen >= max_files:
                return react_hits, polymer_hits
    return react_hits, polymer_hits


def detect_frontend_framework(frontend_dir: str) -> str:
    """Return ``"react"`` / ``"polymer"`` / ``"unknown"`` for ``frontend_dir``."""
    if not frontend_dir or not os.path.isdir(frontend_dir):
        return "unknown"

    # Signal 1: package.json dependencies
    pkg = _read_package_json(frontend_dir)
    via_deps = _check_deps(pkg) if pkg else ""
    if via_deps:
        logger.info("Frontend framework detected via package.json: %s", via_deps)
        return via_deps

    # Signal 2 + 3: content sampling
    react_hits, polymer_hits = _content_sample_score(frontend_dir)
    if react_hits == 0 and polymer_hits == 0:
        logger.info("Frontend framework: no React/Polymer signals found")
        return "unknown"
    if react_hits >= polymer_hits * 2:
        logger.info("Frontend framework detected via content sampling: react "
                    "(react=%d polymer=%d)", react_hits, polymer_hits)
        return "react"
    if polymer_hits >= react_hits * 2:
        logger.info("Frontend framework detected via content sampling: polymer "
                    "(react=%d polymer=%d)", react_hits, polymer_hits)
        return "polymer"
    # Tie or near-tie: prefer the larger by 1 hit margin, else react default
    chosen = "react" if react_hits >= polymer_hits else "polymer"
    logger.info("Frontend framework: ambiguous, defaulting to %s "
                "(react=%d polymer=%d)", chosen, react_hits, polymer_hits)
    return chosen


def build_frontend_url_map(frontend_dir: str, framework: str | None = None) -> tuple[dict, str]:
    """Return ``(url_map, framework)`` for the given frontend directory.

    ``framework`` may be one of ``"react"``, ``"polymer"``, ``"auto"``, or
    ``None`` (treated as auto). Auto-detection runs ``detect_frontend_framework``
    first and dispatches to the matching parser. Unknown frontends return
    ``({}, "unknown")``.
    """
    if not frontend_dir:
        return {}, "unknown"
    if not os.path.isdir(frontend_dir):
        logger.warning("Frontend dir not found: %s", frontend_dir)
        return {}, "unknown"

    fw = (framework or "auto").lower()
    if fw == "auto":
        fw = detect_frontend_framework(frontend_dir)

    if fw == "react":
        from .legacy_react_router import build_url_to_component_map
        return build_url_to_component_map(frontend_dir), "react"
    if fw == "polymer":
        from .legacy_polymer_router import build_url_to_component_map
        return build_url_to_component_map(frontend_dir), "polymer"

    logger.warning("Frontend framework unknown — presentation_layer column will be empty")
    return {}, "unknown"
