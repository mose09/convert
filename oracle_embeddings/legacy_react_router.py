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


SKIP_DIRS = {"node_modules", "build", "dist", ".next", ".git", "coverage",
             "bower_components", "__pycache__", ".cache"}
EXTENSIONS = {".js", ".jsx", ".ts", ".tsx"}

# Minified / 빌드 산출 파일 패턴 — 수 MB 짜리 한 파일이 regex 폭발의
# 주범. source code 가 아니므로 분석에서 안전하게 제외.
_SKIP_FILE_INFIX = (".min.", ".bundle.", ".chunk.", ".compiled.")
# 정상 source 코드는 거의 100KB 미만. 500KB 초과는 minified / 자동생성.
_MAX_FILE_BYTES = 500_000


def _should_include_file(name: str, full_path: str) -> bool:
    """Filter out minified bundles + huge auto-generated files."""
    lname = name.lower()
    if any(s in lname for s in _SKIP_FILE_INFIX):
        return False
    try:
        if os.path.getsize(full_path) > _MAX_FILE_BYTES:
            return False
    except OSError:
        return False
    return True


def scan_react_dir(base_dir: str) -> list[str]:
    """Return absolute paths of React source files under ``base_dir``.

    Caches result when the file-content cache is enabled (analyze-legacy
    의 frontend phase) so multiple scanners (Route / import / api /
    trigger) 가 같은 디렉토리를 여러 번 walk 하지 않는다.
    """
    from .mybatis_parser import _CACHE_ENABLED, _DIR_SCAN_CACHE
    cache_key = ("react", os.path.normpath(base_dir or ""))
    if _CACHE_ENABLED:
        cached = _DIR_SCAN_CACHE.get(cache_key)
        if cached is not None:
            return cached
    files = []
    for root, dirs, names in os.walk(base_dir):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for n in names:
            if os.path.splitext(n)[1].lower() not in EXTENSIONS:
                continue
            full = os.path.join(root, n)
            if _should_include_file(n, full):
                files.append(full)
    if _CACHE_ENABLED:
        _DIR_SCAN_CACHE[cache_key] = files
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


def _build_route_jsx_re(extra_tags: list[str] | None = None) -> re.Pattern:
    """Build ``_ROUTE_JSX_RE`` 와 동일 shape 의 regex 인데 ``<Route>`` 외에
    extra wrapper tag (사용자 사례: ``<PropsRouter path=...>`` 같은 custom
    component) 도 매칭. extra_tags 가 비어있으면 default ``<Route>`` 와
    동일.
    """
    tags = ["Route"] + [t for t in (extra_tags or []) if t and t != "Route"]
    alt = "|".join(re.escape(t) for t in tags)
    return re.compile(
        rf"""<(?:{alt})
            \b[^>]*?
            \bpath\s*=\s*(?P<quote>["'])(?P<path>[^"']*)(?P=quote)
            [^>]*?
            (?:
                element\s*=\s*\{{\s*<\s*(?P<comp1>[A-Z]\w*)
              | component\s*=\s*\{{\s*(?P<comp2>[A-Z]\w*)\s*\}}
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


# Balanced-brace / JSX walker primitives used by the ``render=`` handler.
# `_ROUTE_JSX_RE` 는 attrs 를 `[^>]*?` 로 훑어 `(props) => <Foo/>` 같은
# HOC render prop 에서 `=>` 의 `>` 에 막혀 매칭이 중단된다. 이 경로는
# 사용자 실 프로젝트에서 가장 흔한 Route 선언 패턴 (Router v5 HOC) 이라
# 별도 balanced walker 로 정확히 처리한다.
_ROUTE_OPEN_RE = re.compile(r"<Route\b")
_PATH_ATTR_RE = re.compile(r"""\bpath\s*=\s*["']([^"']+)["']""")
_RENDER_ATTR_RE = re.compile(r"""\brender\s*=\s*\{""")
# JSX 컴포넌트 참조. PascalCase 가 표준인데 사용자 프로젝트 (Korean
# enterprise) 가 ``<jCheckCmpheadManage>`` 같이 소문자 prefix +
# camelCase 컨벤션도 사용. 이 경우 React 표준상 HTML element 로
# 해석되지만 분석기는 component 로 인식해야 함. 소문자 시작도 허용
# 하되 HTML primitive (div / span / a / p / ...) 는 ``_HTML_PRIMITIVES``
# 로 post-filter.
_JSX_COMP_RE = re.compile(r"<\s*([A-Za-z][\w$]+)")
_HTML_PRIMITIVES = frozenset({
    "div", "span", "a", "p", "i", "b", "em", "strong", "small", "code", "pre",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "button", "input", "form", "label", "textarea", "select", "option",
    "ul", "ol", "li", "dl", "dt", "dd",
    "table", "thead", "tbody", "tfoot", "tr", "td", "th", "caption", "colgroup", "col",
    "img", "video", "audio", "source", "track", "iframe", "br", "hr",
    "section", "article", "header", "footer", "main", "nav", "aside",
    "figure", "figcaption", "blockquote", "q", "cite", "abbr", "address",
    "fieldset", "legend", "datalist", "details", "summary", "dialog",
    "canvas", "svg", "path", "g", "rect", "circle", "ellipse", "line", "polygon",
    "polyline", "text", "tspan", "title", "desc", "use", "symbol", "defs",
    "head", "body", "html", "meta", "link", "script", "style", "noscript",
    "fragment",
})


def _find_first_component_in_jsx(text: str) -> str:
    """Return the first non-HTML-primitive JSX tag name in ``text``."""
    for m in _JSX_COMP_RE.finditer(text):
        name = m.group(1)
        if name.lower() in _HTML_PRIMITIVES:
            continue
        return name
    return ""


def _walk_to_tag_end(content: str, start: int) -> int:
    """Return the position just after the matching ``>`` of the JSX tag
    opening at ``content[start] == '<'``. Skips over attribute braces
    (``{...}``) and string literals so a ``>`` inside ``(props) => <Foo/>``
    or ``element={<Foo/>}`` doesn't close the outer Route tag prematurely.
    """
    i = start
    n = len(content)
    depth = 0
    in_str = None
    while i < n:
        c = content[i]
        if in_str:
            if c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == in_str:
                in_str = None
            i += 1
            continue
        if c in ("'", '"', "`"):
            in_str = c
            i += 1
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        elif c == ">" and depth == 0:
            return i + 1
        i += 1
    return n


def _extract_brace_body(text: str, open_pos: int) -> str:
    """Return the substring inside the balanced ``{...}`` starting at
    ``text[open_pos] == '{'`` (excluding outer braces). String-literal
    aware so inner ``}`` in quotes don't close the brace early.
    """
    if open_pos >= len(text) or text[open_pos] != "{":
        return ""
    i = open_pos + 1
    n = len(text)
    depth = 1
    in_str = None
    start = i
    while i < n:
        c = text[i]
        if in_str:
            if c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == in_str:
                in_str = None
            i += 1
            continue
        if c in ("'", '"', "`"):
            in_str = c
            i += 1
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:i]
        i += 1
    return text[start:]


def _extract_render_routes(content: str) -> list[dict]:
    """Extract ``{path, component}`` from ``<Route render={...}/>`` tags.

    React Router v5 HOC 패턴 (``render={(props) => <Foo {...props}/>}``)
    은 `_ROUTE_JSX_RE` 의 `[^>]*?` 어태가 `=>` 의 `>` 에 막혀 매칭 실패한다.
    여기서는 ``<Route`` 오픈 위치부터 balanced walker 로 정확한 tag 범위를
    잡고, render={ 블록 안에서 첫 번째 ``<CapitalizedComponent`` 를 꺼낸다.
    컴포넌트 해석 실패 시에도 caller (``build_url_to_component_map``) 가
    ``declared_in`` 을 file_path fallback 으로 쓸 수 있도록 빈 component
    로 entry 를 emit 한다.
    """
    routes: list[dict] = []
    for m in _ROUTE_OPEN_RE.finditer(content):
        tag_end = _walk_to_tag_end(content, m.start())
        tag = content[m.start():tag_end]
        path_m = _PATH_ATTR_RE.search(tag)
        if not path_m:
            continue
        render_m = _RENDER_ATTR_RE.search(tag)
        if not render_m:
            continue
        open_brace = tag.find("{", render_m.end() - 1)
        if open_brace < 0:
            continue
        body = _extract_brace_body(tag, open_brace)
        comp = _find_first_component_in_jsx(body)
        routes.append({"path": path_m.group(1), "component": comp})
    return routes


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


# Component declaration whose body contains ``<Route``. 사용자 사례:
# ``const PropsRouter = (...) => { return <Route ...>...</Route>; }``
# 같이 ``<Route>`` 를 감싼 wrapper component 가 사용처에서
# ``<PropsRouter path="/x" component={X}/>`` 형태로 호출되는데, default
# ``<Route\b`` regex 가 못 잡아 Route extraction 0건. wrapper 이름을
# 모아서 dynamic regex alt 로 추가하면 매칭 성립.
_WRAPPER_DECL_RE = re.compile(
    r"""(?:const|let|var|function|class)\s+
        (?P<name>[A-Z]\w*)
        \s*(?:=|extends\s+\w+|\(|<)
    """,
    re.VERBOSE,
)


def detect_route_wrapper_components(content: str) -> set[str]:
    """파일 안에서 ``<Route>`` 를 감싸는 wrapper component 이름 추출.

    각 component 선언 (const/let/function/class) 의 시작 위치에서 일정
    window (3000자) 안에 ``<Route\\b`` 가 등장하면 wrapper 후보로 등록.
    PascalCase 이름만 (Component 컨벤션). 기본 ``Route`` 자체는 제외.
    """
    wrappers: set[str] = set()
    if "<Route" not in content:
        return wrappers
    for m in _WRAPPER_DECL_RE.finditer(content):
        name = m.group("name")
        if not name or name == "Route":
            continue
        window = content[m.start():m.start() + 3000]
        if "<Route" in window:
            wrappers.add(name)
    return wrappers


def _extract_routes_from_content(content: str,
                                  route_jsx_re: re.Pattern | None = None) -> list[dict]:
    """Extract ``{path, component}`` pairs from one file's source.

    Collapses duplicates across JSX and object patterns; returns entries in
    source order (important for nested-path propagation).

    ``route_jsx_re`` 가 주어지면 ``<Route>`` 외에 wrapper component 도
    매칭하는 dynamic regex 사용 (사용자 사례: ``<PropsRouter path=...>``).
    None 이면 module-level ``_ROUTE_JSX_RE`` 사용.
    """
    routes = []
    seen = set()
    jsx_re = route_jsx_re or _ROUTE_JSX_RE

    def _add(path: str, comp: str):
        key = (path, comp)
        if key not in seen and path and comp:
            seen.add(key)
            routes.append({"path": path, "component": comp})

    for m in jsx_re.finditer(content):
        _add(m.group("path"), m.group("comp1") or m.group("comp2"))

    for m in _ROUTE_OBJ_RE.finditer(content):
        _add(m.group("path"), m.group("comp1") or m.group("comp2"))

    # render={(props) => <Foo .../>} — Router v5 HOC 패턴.
    # 컴포넌트 해석 실패 (render 본문이 custom logic) 해도 path 는 유효
    # 하므로 빈 comp 로 emit — caller 가 declared_in 을 file_path
    # fallback 으로 쓸 수 있게 한다.
    for r in _extract_render_routes(content):
        path, comp = r["path"], r["component"]
        key = (path, comp)
        if path and key not in seen:
            seen.add(key)
            routes.append({"path": path, "component": comp})

    return routes


# ---------------------------------------------------------------------------
# Import graph — menu URL ↔ Route 선언 파일 기반 "실제 참조" 추적
# ---------------------------------------------------------------------------

# Import / require / dynamic-import. 상대경로 (./, ../) 만 관심 —
# node_modules 패키지는 frontend 소스 트리 밖이라 graph 에서 제외.
_IMPORT_FROM_RE = re.compile(
    r"""(?:^|\s|;)
        import\s+
        (?:.+?\s+from\s+)?
        ["'](?P<path>\.{1,2}/[^"']+)["']
    """,
    re.VERBOSE | re.MULTILINE | re.DOTALL,
)
_DYNAMIC_IMPORT_RE = re.compile(
    r"""(?:^|[^.\w])import\s*\(\s*["'](?P<path>\.{1,2}/[^"']+)["']\s*\)"""
)
_REQUIRE_RE = re.compile(
    r"""\brequire\s*\(\s*["'](?P<path>\.{1,2}/[^"']+)["']\s*\)"""
)


def _extract_imports_in_content(content: str) -> list[str]:
    """Relative import paths referenced in content.

    Covers ``import ... from "./x"`` / ``import "./x"`` / dynamic
    ``import("./x")`` / CommonJS ``require("./x")``. Bare package imports
    (``import x from "react"``) are skipped.
    """
    found: list[str] = []
    seen: set[str] = set()
    for pat in (_IMPORT_FROM_RE, _DYNAMIC_IMPORT_RE, _REQUIRE_RE):
        for m in pat.finditer(content):
            p = m.group("path")
            if p and p not in seen:
                seen.add(p)
                found.append(p)
    return found


def build_import_graph(react_dir: str) -> dict[str, set[str]]:
    """Return ``{abs_file_path: set(abs_imported_paths)}``.

    Used by :func:`collect_menu_scope_files`. Starting from the Route
    declaration file of a menu URL, BFS this graph to find every screen
    / helper actually referenced — **import chain** replaces folder-name
    proximity as the unit of menu→code attribution. 사용자 환경에선
    folder 이름 (``hypm_materialMaster``) 과 public 메뉴 URL slug
    (``gipms-materialmasternew``) 가 달라 folder 기준 휴리스틱은 부정확.
    """
    graph: dict[str, set[str]] = {}
    if not react_dir or not os.path.isdir(react_dir):
        return graph
    files = scan_react_dir(react_dir)
    for fp in files:
        try:
            content = _read_file_safe(fp)
        except Exception:
            continue
        from_dir = os.path.dirname(fp)
        resolved: set[str] = set()
        for rel in _extract_imports_in_content(content):
            abs_path = _resolve_import_path(rel, from_dir)
            if abs_path:
                resolved.add(abs_path)
        graph[fp] = resolved
    return graph


def collect_menu_scope_files(menu_url_norm: str, url_map: dict,
                              import_graph: dict[str, set[str]],
                              max_depth: int = 8) -> set[str]:
    """Return absolute files reachable from ``menu_url_norm`` via imports.

    Seeds = ``url_map[menu_url_norm].declared_in`` + ``file_path``.
    BFS forward up to ``max_depth`` hops, cycle-safe. Empty set if the
    URL is not in ``url_map`` (caller can treat as "no Route match").
    """
    if not menu_url_norm:
        return set()
    entry = url_map.get(menu_url_norm)
    if not entry:
        return set()
    seeds: list[str] = []
    for key in ("declared_in", "file_path"):
        p = entry.get(key) or ""
        if p and p not in seeds:
            seeds.append(p)
    if not seeds:
        return set()
    visited: set[str] = set(seeds)
    frontier = list(seeds)
    for _ in range(max_depth):
        next_frontier: list[str] = []
        for f in frontier:
            for dep in import_graph.get(f, ()):
                if dep not in visited:
                    visited.add(dep)
                    next_frontier.append(dep)
        if not next_frontier:
            break
        frontier = next_frontier
    return visited


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

    # Project-wide Route wrapper component scan. 사용자 사례:
    # ``const PropsRouter = (...) => <Route ...>`` 같은 wrapper 가 사용처
    # 에서 ``<PropsRouter path="/x" component={X}/>`` 로 호출 — default
    # ``<Route\b`` regex 가 못 잡아 Route extraction 0건. 모든 file 에서
    # wrapper 이름을 모아 dynamic regex alt 로 추가.
    wrappers: set[str] = set()
    for fp in files:
        try:
            wrappers.update(detect_route_wrapper_components(_read_file_safe(fp)))
        except Exception:
            continue
    if wrappers:
        logger.info("React Route wrappers detected: %s", sorted(wrappers))
    route_jsx_re = _build_route_jsx_re(sorted(wrappers)) if wrappers else _ROUTE_JSX_RE

    url_map = {}
    for fp in files:
        try:
            content = _read_file_safe(fp)
        except Exception:
            continue
        # wrapper 가 있으면 사용처에서 ``<{Wrapper}\b`` 매칭이 되므로
        # ``Route`` substring 이 없는 파일도 후보. 보수적으로 wrapper 의
        # 첫 alt 만 검사.
        has_route_keyword = "Route" in content or "path:" in content
        if not has_route_keyword:
            if not any(f"<{w}" in content for w in wrappers):
                continue
        lazy_map = _extract_lazy_imports(content, os.path.dirname(fp))
        routes = _extract_routes_from_content(content, route_jsx_re=route_jsx_re)
        for r in routes:
            comp = r["component"]
            # comp 해석 실패 (render HOC / dynamic import 등) 하거나 lazy
            # map / 전체 파일 색인에도 없으면 Route 가 **선언된 파일**
            # (fp) 자체를 file_path 로 사용한다. 사용자 환경에서 폴더명
            # (hypm_materialMaster) 과 메뉴 URL (gipms-materialmasternew)
            # 이 달라도 Route 선언 위치만 있으면 URL → 파일 매핑이 유효.
            file_path = lazy_map.get(comp) or component_index.get(comp) or fp
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
