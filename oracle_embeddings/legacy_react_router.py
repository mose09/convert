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


# dev/test/story 폴더 (production 빌드에 안 들어가는 Route) 도 함께 skip.
# 모두 lowercase — caller 가 ``d.lower() not in SKIP_DIRS`` 로 비교.
SKIP_DIRS = {
    "node_modules", "build", "dist", ".next", ".git", "coverage",
    "bower_components", "__pycache__", ".cache",
    "devonly", "dev-only", "dev_only",
    "__tests__", "__mocks__",
    "storybook", ".storybook", "stories",
}
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
        dirs[:] = [d for d in dirs if d.lower() not in SKIP_DIRS]
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


# `<Route path="/x" element={<X/>} />` or `component={X}`. component 값은
# 변수 reference 라 PascalCase 외에 camelCase alias 도 흔함 — `[A-Za-z_]`
# 로 완화. element 의 JSX 태그는 React 컨벤션상 PascalCase 유지.
#
# path 는 두 형태:
# 1. literal:  ``path="/list"`` 또는 ``path='/list'``
# 2. dynamic:  ``path={getRoutePath(basename, '/list')}`` 같은 JSX
#    expression. 사용자 사례: SPA 의 ``routes/index.js`` 가 basename 합성
#    함수로 path 를 만든다. 이 경우 ``{...}`` 안에서 quoted literal 을
#    별도 후처리로 추출 (:func:`_resolve_path_expr`). 중첩 brace 없는 단순
#    expression 만 — ``{...{...}...}`` 는 skip (드물고 정적 해석 어려움).
_ROUTE_JSX_RE = re.compile(
    r"""<Route
        \b[^>]*?
        \bpath\s*=\s*(?:
            (?P<quote>["'])(?P<path>[^"']*)(?P=quote)
          | \{(?P<path_expr>[^{}]+)\}
        )
        [^>]*?
        (?:
            element\s*=\s*\{\s*<\s*(?P<comp1>[A-Z]\w*)
          | component\s*=\s*\{\s*(?P<comp2>[A-Za-z_]\w*)\s*\}
        )
    """,
    re.VERBOSE,
)


def _resolve_path_expr(expr: str) -> str | None:
    """JSX expression ``{...}`` 안에서 path literal 을 추출.

    지원 패턴:
    - ``getRoutePath(basename, '/list')`` → ``'/list'`` (마지막 path-like quoted)
    - ``getRoutePath(basename, '/')`` → ``'/'``
    - `` `/list/${id}` `` → ``'/list/{p}'`` (template literal, dynamic 치환)
    - ``URLS.LIST`` / 변수 reference → None (정적 해석 불가)

    path-like 우선순위는 ``/`` 시작 토큰. 같은 expression 에 여러 quoted
    string 이 있으면 마지막 path-like 토큰 선택. 함수 호출 첫 인자가
    변수, 뒤가 path literal 인 일반적 케이스 매칭.
    """
    if not expr:
        return None
    # 1. Template literal (back-tick) — ``${...}`` 는 ``{p}`` 토큰으로 치환
    m = re.search(r"`([^`]+)`", expr)
    if m:
        return re.sub(r"\$\{[^}]+\}", "{p}", m.group(1))
    # 2. quoted string literal — / 시작 우선
    matches = re.findall(r"""['"]([^'"]*)['"]""", expr)
    if matches:
        path_like = [s for s in matches if s.startswith("/")]
        if path_like:
            return path_like[-1]
        return matches[-1]
    return None


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
            \bpath\s*=\s*(?:
                (?P<quote>["'])(?P<path>[^"']*)(?P=quote)
              | \{{(?P<path_expr>[^{{}}]+)\}}
            )
            [^>]*?
            (?:
                element\s*=\s*\{{\s*<\s*(?P<comp1>[A-Z]\w*)
              | component\s*=\s*\{{\s*(?P<comp2>[A-Za-z_]\w*)\s*\}}
            )
        """,
        re.VERBOSE,
    )

# Object-style route config — component 값은 변수 reference 라 camelCase
# alias 도 허용 (element JSX 태그만 PascalCase 유지).
_ROUTE_OBJ_RE = re.compile(
    r"""\{\s*
        path\s*:\s*(?P<quote>["'`])(?P<path>[^"'`]+)(?P=quote)
        [^}]*?
        (?:element\s*:\s*<\s*(?P<comp1>[A-Z]\w*)
          | component\s*:\s*(?P<comp2>[A-Za-z_]\w*)
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


# ``import App from 'apps/<slug>'`` — 사용자 사례: routes/index.js 가
# Route path 를 명시적으로 박지 않고 sub-app 컴포넌트만 import 해서
# ``<Route component={App}/>`` 로 위임. 그러면 메뉴 URL 의 진짜 slug
# 정보는 ``Route path`` 가 아니라 **import path 의 마지막 segment** 에
# 들어있다. 이 패턴을 추출해서 ``/apps/<normalized-slug>`` alias 를
# url_map 에 등록 (file_path = routes 선언 파일 자체).
#
# 표기 변환: ``hypm_interlockRule`` (underscore + camelCase) →
# ``hypm-interlockrule`` (메뉴 URL 의 kebab-case). underscore → dash +
# lowercase 만 적용 — normalize_url 이 lowercase 적용하니 실질적으로는
# underscore → dash 만 필요하지만 명시적으로 처리.
_APP_IMPORT_RE = re.compile(
    r"""\bimport\s+
        (?:\{[^}]*\}|\*\s+as\s+\w+|\w+(?:\s*,\s*\{[^}]*\})?)
        \s+from\s+
        ["'](?:\.{1,2}/)*apps/(?P<slug>[A-Za-z0-9_][\w-]*)
    """,
    re.VERBOSE,
)


def _apps_import_aliases(content: str) -> list[str]:
    """``import X from 'apps/<slug>'`` 라인들에서 ``apps/<slug>`` 의 ``<slug>``
    를 추출해 kebab-case 정규화한 리스트 반환. 중복 제거.
    """
    out: list[str] = []
    seen: set[str] = set()
    for m in _APP_IMPORT_RE.finditer(content):
        raw = m.group("slug")
        norm = raw.replace("_", "-").lower()
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


# Balanced-brace / JSX walker primitives used by the ``render=`` handler.
# `_ROUTE_JSX_RE` 는 attrs 를 `[^>]*?` 로 훑어 `(props) => <Foo/>` 같은
# HOC render prop 에서 `=>` 의 `>` 에 막혀 매칭이 중단된다. 이 경로는
# 사용자 실 프로젝트에서 가장 흔한 Route 선언 패턴 (Router v5 HOC) 이라
# 별도 balanced walker 로 정확히 처리한다.
_ROUTE_OPEN_RE = re.compile(r"<Route\b")
# path 는 두 형태 모두 지원: literal (group 1) 또는 dynamic JSX expression
# (group 2 — caller 가 :func:`_resolve_path_expr` 로 후처리).
_PATH_ATTR_RE = re.compile(
    r"""\bpath\s*=\s*(?:["']([^"']+)["']|\{([^{}]+)\})"""
)
_RENDER_ATTR_RE = re.compile(r"""\brender\s*=\s*\{""")
_JSX_CAPITAL_COMP_RE = re.compile(r"<\s*([A-Z]\w+)")


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
        # path 가 literal (group 1) 또는 dynamic expression (group 2).
        # PR #201 의 _resolve_path_expr 재사용 — 함수 호출의 마지막 path-like
        # quoted / template literal 처리.
        raw_path = path_m.group(1) or _resolve_path_expr(path_m.group(2) or "")
        if not raw_path:
            continue
        render_m = _RENDER_ATTR_RE.search(tag)
        if not render_m:
            continue
        open_brace = tag.find("{", render_m.end() - 1)
        if open_brace < 0:
            continue
        body = _extract_brace_body(tag, open_brace)
        comp_m = _JSX_CAPITAL_COMP_RE.search(body)
        comp = comp_m.group(1) if comp_m else ""
        routes.append({"path": raw_path, "component": comp})
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
        raw_path = m.group("path")
        if raw_path is None:
            # ``path={...}`` JSX expression — extract quoted literal.
            raw_path = _resolve_path_expr(m.group("path_expr") or "")
            if not raw_path:
                continue
        _add(raw_path, m.group("comp1") or m.group("comp2"))

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

    # Path-only fallback — component / element / render 모두 없는 ``<Route
    # path={...}>...</Route>`` (children pattern). url_map 의 base 는
    # 등록되어야 메뉴 URL prefix 매칭 (PR #202) 이 동작. file_path 는
    # caller 가 declared_in 으로 fallback. 사용자 사례: 메인 레포의
    # routes/index.js 가 catch-all path 만 있고 화면 렌더는 children 안
    # 다른 라우터로 위임.
    seen_paths = {r["path"] for r in routes}
    for m in _ROUTE_OPEN_RE.finditer(content):
        tag_end = _walk_to_tag_end(content, m.start())
        tag = content[m.start():tag_end]
        path_m = _PATH_ATTR_RE.search(tag)
        if not path_m:
            continue
        raw_path = path_m.group(1) or _resolve_path_expr(path_m.group(2) or "")
        if not raw_path or raw_path in seen_paths:
            continue
        seen_paths.add(raw_path)
        routes.append({"path": raw_path, "component": ""})

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

        # ``import App from 'apps/<slug>'`` alias — Route path 가 dynamic /
        # 없거나 컴포넌트 안에서 처리될 때, import 경로의 slug 가 진짜
        # 메뉴 URL slug 인 사용자 사례 (hypm_interlockRule → hypm-interlockrule).
        # 같은 routes 파일에 등록 — file_path 는 그 파일 자체.
        for slug in _apps_import_aliases(content):
            alias_key = normalize_url(f"/apps/{slug}", strip_patterns)
            if not alias_key:
                continue
            url_map.setdefault(alias_key, {
                "component": "",
                "file_path": fp,
                "declared_in": fp,
            })

    logger.info("React URL map: %d entries from %d files", len(url_map), len(files))
    return url_map
