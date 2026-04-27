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

from .mybatis_parser import _read_file_safe

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


def build_frontend_api_index(frontend_dir: str, patterns: dict | None = None,
                               strip_patterns=None) -> tuple[dict, dict]:
    """Single-frontend helper: return (api_index, trigger_index).

    Thin wrapper that delegates to :mod:`legacy_react_api_scanner` so the
    analyzer can call one place for both single and multi-repo setups.
    """
    if not frontend_dir or not os.path.isdir(frontend_dir):
        return {}, {}
    from .legacy_react_api_scanner import build_api_url_index, extract_button_triggers
    api_idx = build_api_url_index(frontend_dir, patterns=patterns,
                                   strip_patterns=strip_patterns)
    trig = extract_button_triggers(frontend_dir, api_idx, patterns=patterns,
                                    strip_patterns=strip_patterns) if api_idx else {}
    return api_idx, trig


def build_frontend_url_map(frontend_dir: str, framework: str | None = None,
                            strip_patterns=None,
                            route_prefix: str | None = None) -> tuple[dict, str]:
    """Return ``(url_map, framework)`` for the given frontend directory.

    ``framework`` may be one of ``"react"``, ``"polymer"``, ``"auto"``, or
    ``None`` (treated as auto). Auto-detection runs ``detect_frontend_framework``
    first and dispatches to the matching parser. Unknown frontends return
    ``({}, "unknown")``.

    ``strip_patterns`` / ``route_prefix`` forwarded to the router parser
    for cross-source URL normalization.
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
        return build_url_to_component_map(
            frontend_dir, strip_patterns=strip_patterns, route_prefix=route_prefix,
        ), "react"
    if fw == "polymer":
        from .legacy_polymer_router import build_url_to_component_map
        return build_url_to_component_map(
            frontend_dir, strip_patterns=strip_patterns, route_prefix=route_prefix,
        ), "polymer"

    logger.warning("Frontend framework unknown — presentation_layer column will be empty")
    return {}, "unknown"


_NESTED_APP_CANDIDATES = (
    "src/apps", "apps", "packages", "src/pages", "projects", "src/projects",
)


def _resolve_app_buckets_root(frontends_root: str) -> str:
    """Return the directory whose immediate children should be app buckets.

    Mono-repo conventions often nest individual apps under a subpath like
    ``src/apps/<app>`` rather than having each app be a top-level sibling
    under ``frontends_root``. If the given root has too few immediate
    child directories to look like an "app container" (no app-per-dir
    structure), we probe a small set of common nested paths and use the
    first one that has >= 2 child directories.

    Returns the adjusted path, or the original ``frontends_root`` if no
    drill-down applies.
    """
    if not frontends_root or not os.path.isdir(frontends_root):
        return frontends_root

    def _app_like_children(p: str) -> int:
        try:
            return sum(
                1 for n in os.listdir(p)
                if os.path.isdir(os.path.join(p, n))
                and not n.startswith(".")
                and n != "node_modules"
            )
        except Exception:
            return 0

    # If root already has >= 3 app-like children, use it as-is.
    if _app_like_children(frontends_root) >= 3:
        return frontends_root

    for rel in _NESTED_APP_CANDIDATES:
        cand = os.path.join(frontends_root, rel)
        if os.path.isdir(cand) and _app_like_children(cand) >= 2:
            logger.info("frontends-root drill-down: %s → %s", frontends_root, cand)
            return cand

    return frontends_root


def _enumerate_buckets(frontends_root: str) -> list[tuple[str, str]]:
    """Return the list of ``(bucket_name, bucket_path)`` to treat as apps.

    Handles two common monorepo shapes:

    1. **Flat**: ``frontends_root/<app>/...`` — each immediate child is an
       app. Bucket name = child dir name.
    2. **Nested** (e.g. ``frontends_root/<repo>/src/apps/<app>/...``): the
       real apps live two levels below. This is common at SK Hynix where
       a "repo" wraps several inner apps and menu URLs reference the
       **inner** app slug, not the repo. For every child that contains
       ``src/apps/*`` or ``apps/*``, we descend and use the inner dirs
       as buckets.

    The two shapes can coexist — for a repo without nested apps the
    repo itself stays as the bucket.
    """
    out: list[tuple[str, str]] = []
    if not frontends_root or not os.path.isdir(frontends_root):
        return out
    for entry in sorted(os.listdir(frontends_root)):
        child = os.path.join(frontends_root, entry)
        if not os.path.isdir(child) or entry.startswith(".") or entry == "node_modules":
            continue
        nested_parent = None
        for rel in ("src/apps", "src/app", "apps", "app", "src/pages", "packages"):
            cand = os.path.join(child, rel)
            if os.path.isdir(cand):
                inner_dirs = [
                    d for d in sorted(os.listdir(cand))
                    if os.path.isdir(os.path.join(cand, d))
                    and not d.startswith(".") and d != "node_modules"
                ]
                if inner_dirs:
                    nested_parent = cand
                    for d in inner_dirs:
                        out.append((d, os.path.join(cand, d)))
                    break
        if nested_parent is None:
            out.append((entry, child))
    return out


# Cheap pre-pass regex: ``<Route path="/apps/<slug>"`` first segment.
# folder 이름과 menu URL slug 가 다른 케이스에서 bucket 을 살리기 위한
# alias 추출 용. 정확도보다 속도 우선 — index.* 파일만 스캔.
_QUICK_ROUTE_PATH_RE = re.compile(
    r"""path\s*=\s*["'](/apps/([^/"']+))""",
    re.IGNORECASE,
)


def _quick_bucket_route_slugs(bucket_path: str) -> set[str]:
    """Return ``/apps/<slug>`` slugs declared in ``index.{js,ts,jsx,tsx}``
    files anywhere under ``bucket_path``. Used as alias source so a
    folder whose internal name differs from the public menu URL slug
    still survives ``allowed_apps`` filtering.
    """
    aliases: set[str] = set()
    if not bucket_path or not os.path.isdir(bucket_path):
        return aliases
    for root, dirs, files in os.walk(bucket_path):
        dirs[:] = [d for d in dirs
                   if not d.startswith(".") and d != "node_modules"]
        for fn in files:
            if fn not in ("index.js", "index.jsx", "index.ts", "index.tsx"):
                continue
            try:
                content = _read_file_safe(os.path.join(root, fn))
            except Exception:
                continue
            for m in _QUICK_ROUTE_PATH_RE.finditer(content):
                slug = (m.group(2) or "").lower()
                if slug:
                    aliases.add(slug)
    return aliases


def build_frontend_url_map_multi(frontends_root: str, framework: str | None = None,
                                  strip_patterns=None,
                                  route_prefix: str | None = None,
                                  patterns: dict | None = None,
                                  allowed_apps: set[str] | None = None,
                                  ) -> tuple[dict, str, dict, dict, dict]:
    """Scan multiple frontend repos under ``frontends_root`` and merge URL maps.

    Bucket enumeration delegates to :func:`_enumerate_buckets` so both
    flat (``frontends_root/<app>``) and nested
    (``frontends_root/<repo>/src/apps/<app>``) layouts work.

    ``allowed_apps`` (optional) restricts scanning to the named buckets
    (lowercase matched). Used by the menu-driven analyze path so apps
    that aren't referenced from any menu entry are skipped entirely.

    Returns a 5-tuple
    ``(merged_map, overall_framework, by_frontend, api_by_frontend, triggers_by_frontend)``.
    Bucket keys are stored lowercase to match case-insensitive menu
    URL slugs.
    """
    if not frontends_root or not os.path.isdir(frontends_root):
        return {}, "unknown", {}, {}, {}

    buckets = _enumerate_buckets(frontends_root)
    if not buckets:
        return {}, "unknown", {}, {}, {}

    merged_map = {}
    by_frontend: dict[str, dict] = {}
    api_by_frontend: dict[str, dict] = {}
    triggers_by_frontend: dict[str, dict] = {}
    detected_frameworks = []
    skipped: list[str] = []

    from .legacy_react_api_scanner import build_api_url_index, extract_button_triggers

    allowed_lower = {a.lower() for a in allowed_apps} if allowed_apps else None

    # Pre-pass: allowed_apps 가 설정된 (보통 ``--menu-only``) 경우 folder
    # 이름이 menu URL slug 과 달라도 그 bucket 의 ``index.{js,ts,jsx,tsx}``
    # 안 ``<Route path="/apps/<slug>">`` 선언이 menu URL 과 매칭되면
    # bucket 을 살린다. 사용자 사례 (folder ``hypm_workOrderMng`` + menu
    # URL ``/apps/gipms-utl-workorder``) 핵심 해결.
    if allowed_lower is not None:
        expanded = set(allowed_lower)
        for entry, child in buckets:
            entry_lower = entry.lower()
            if entry_lower in expanded:
                continue  # folder 이름으로 이미 통과
            aliases = _quick_bucket_route_slugs(child)
            if aliases & allowed_lower:
                expanded.add(entry_lower)
        added = expanded - allowed_lower
        if added:
            logger.info("Frontend multi-repo: allowed_apps expanded by %d bucket(s) "
                        "via Route-path slug alias: %s",
                        len(added),
                        ", ".join(sorted(added)[:5])
                        + (" ..." if len(added) > 5 else ""))
        allowed_lower = expanded

    for entry, child in buckets:
        entry_lower = entry.lower()
        if allowed_lower is not None and entry_lower not in allowed_lower:
            skipped.append(entry)
            continue
        url_map, fw = build_frontend_url_map(
            child, framework=framework,
            strip_patterns=strip_patterns, route_prefix=route_prefix,
        )
        if url_map:
            # 같은 이름의 inner app 이 여러 repo 에 있을 수 있어 bucket key
            # 충돌 가능. 기존 bucket 이 있으면 url 단위로 merge (덮어쓰기 금지).
            existing = by_frontend.setdefault(entry_lower, {})
            for key, val in url_map.items():
                tagged = dict(val)
                tagged["frontend_name"] = entry
                existing.setdefault(key, tagged)
                merged_map.setdefault(key, tagged)
            detected_frameworks.append(fw)
            logger.info("Frontend sub-project %s: %s, %d routes", entry, fw, len(url_map))
        try:
            api_idx = build_api_url_index(child, patterns=patterns,
                                           strip_patterns=strip_patterns)
        except Exception as e:
            logger.warning("build_api_url_index %s 실패: %s", entry, e)
            api_idx = {}
        if api_idx:
            # Merge into existing bucket when same lowercase key already
            # has entries from another repo. Each URL's file list gets
            # extended so no call site is lost.
            bucket = api_by_frontend.setdefault(entry_lower, {})
            for url, files in api_idx.items():
                prefixed = [f"{entry}/{f}" for f in files]
                if url in bucket:
                    # preserve order while deduping
                    merged_list = list(bucket[url])
                    for p in prefixed:
                        if p not in merged_list:
                            merged_list.append(p)
                    bucket[url] = merged_list
                else:
                    bucket[url] = prefixed
            logger.info("Frontend sub-project %s: %d api urls", entry, len(api_idx))
            try:
                trig = extract_button_triggers(child, api_idx, patterns=patterns,
                                                strip_patterns=strip_patterns)
            except Exception as e:
                logger.warning("extract_button_triggers %s 실패: %s", entry, e)
                trig = {}
            if trig:
                tbucket = triggers_by_frontend.setdefault(entry_lower, {})
                for url, labels in trig.items():
                    if url in tbucket:
                        merged_labels = list(tbucket[url])
                        for lbl in labels:
                            if lbl not in merged_labels:
                                merged_labels.append(lbl)
                        tbucket[url] = merged_labels
                    else:
                        tbucket[url] = list(labels)
                logger.info("Frontend sub-project %s: %d button triggers", entry, len(trig))

    overall_fw = detected_frameworks[0] if detected_frameworks else "unknown"
    if len(set(detected_frameworks)) > 1:
        overall_fw = "mixed"

    # Route path slug 을 bucket 의 alias 로 등록. 사용자 사례: 폴더명
    # (``hypm_materialMaster``) 과 메뉴 URL slug (``gipms-materialmasternew``) 이
    # 달라 2-hop 매칭이 실패했던 문제. 각 bucket 안 Route path 가
    # ``/apps/<slug>/...`` 형태면 해당 slug 를 같은 url_map / api_idx /
    # triggers 버킷의 alias key 로 추가 등록한다. 이미 같은 slug bucket
    # 이 있으면 건드리지 않음 (first-win) — 충돌 방지.
    _APPS_SLUG_RE = re.compile(r"^/apps/([^/]+)", re.IGNORECASE)
    alias_count = 0
    for folder_slug in list(by_frontend.keys()):
        url_map_for_folder = by_frontend[folder_slug]
        for url_path in url_map_for_folder:
            m = _APPS_SLUG_RE.match(url_path)
            if not m:
                continue
            route_slug = m.group(1).lower()
            if not route_slug or route_slug == folder_slug:
                continue
            if route_slug not in by_frontend:
                by_frontend[route_slug] = url_map_for_folder
                alias_count += 1
                if folder_slug in api_by_frontend:
                    api_by_frontend.setdefault(route_slug, api_by_frontend[folder_slug])
                if folder_slug in triggers_by_frontend:
                    triggers_by_frontend.setdefault(route_slug, triggers_by_frontend[folder_slug])
    if alias_count:
        logger.info("Frontend multi-repo: %d Route-slug aliases registered "
                    "(folder name ≠ Route path slug)", alias_count)

    if skipped:
        logger.info("Frontend multi-repo: %d buckets skipped (not referenced by menu): %s",
                    len(skipped),
                    ", ".join(skipped[:10]) + (" ..." if len(skipped) > 10 else ""))
    logger.info("Frontend multi-repo: %d sub-projects scanned (of %d total), %d total routes, framework=%s",
                len(detected_frameworks), len(buckets), len(merged_map), overall_fw)
    return merged_map, overall_fw, by_frontend, api_by_frontend, triggers_by_frontend
