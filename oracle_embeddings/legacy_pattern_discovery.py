"""LLM-based project pattern discovery for analyze-legacy.

Scans a sample of Java source files, summarises their structural
metadata (class declarations, extends/implements, annotations, method
signatures, field calls, SQL calls), and asks a local LLM to identify
the project-specific patterns.  The result is saved as a YAML file
that ``analyze-legacy --patterns`` can load to customise parsing
without changing code.

Typical workflow::

    # 1) discover (needs LLM, one-time)
    python main.py discover-patterns --backend-dir /path/to/backend

    # 2) analyse (no LLM needed)
    python main.py analyze-legacy --backend-dir /path/to/backend \\
        --patterns output/legacy_analysis/patterns.yaml
"""

import json
import logging
import os
import re
import time
from collections import Counter
from datetime import datetime

import yaml

logger = logging.getLogger(__name__)

# How many classes per stereotype to include in the LLM prompt
_SAMPLE_PER_ROLE = 12
_MAX_METHODS_PER_CLASS = 6


# ── helpers ────────────────────────────────────────────────────────

def _summarise_class(cls: dict) -> dict:
    """Distill a parsed class dict into a compact summary for the LLM."""
    methods_raw = cls.get("methods") or []
    methods_summary = []
    for m in methods_raw[:_MAX_METHODS_PER_CLASS]:
        sig = m.get("signature", "")
        # Trim very long signatures
        if len(sig) > 120:
            sig = sig[:120] + "..."
        calls = []
        for fc in m.get("body_field_calls", []):
            calls.append(f"{fc['receiver']}.{fc['method']}()")
        for sc in m.get("body_sql_calls", []):
            calls.append(f"SQL:{sc['sqlid']}")
        for rc in m.get("body_rfc_calls", []):
            calls.append(f"RFC:{rc.get('name', '?')}")
        methods_summary.append({
            "signature": sig,
            "calls": calls[:8],
        })

    # Raw SQL call patterns from the file (class-level)
    sql_raw = []
    for sc in (cls.get("sql_calls") or [])[:5]:
        sql_raw.append(sc.get("raw", f"{sc.get('op','')}(\"{sc.get('sqlid','')}\")"))

    return {
        "class_name": cls.get("class_name", ""),
        "package": cls.get("package", ""),
        "extends": cls.get("extends", ""),
        "implements": cls.get("implements", []),
        "stereotype": cls.get("stereotype", ""),
        "annotations": cls.get("annotations", []),
        "autowired_fields": [
            {"type": f.get("type_simple", ""), "name": f.get("name", "")}
            for f in (cls.get("autowired_fields") or [])[:8]
        ],
        "methods": methods_summary,
        "sql_calls_raw": sql_raw,
        "rfc_calls": [r.get("name", "") for r in (cls.get("rfc_calls") or [])[:5]],
        "endpoint_count": len(cls.get("endpoints") or []),
        "endpoint_samples": [
            {"method_name": ep["method_name"], "url": ep.get("full_url", ""),
             "annotation": ep.get("annotation", "")}
            for ep in (cls.get("endpoints") or [])[:4]
        ],
    }


def _sample_classes(classes: list[dict]) -> list[dict]:
    """Pick a representative sample across stereotypes."""
    by_role = {}
    for c in classes:
        role = c.get("stereotype") or "(none)"
        by_role.setdefault(role, []).append(c)

    sample = []
    # Priority roles first
    for role in ["Controller", "RestController", "Service", "Repository",
                 "Mapper", "Component", "Verticle", "(none)"]:
        candidates = by_role.get(role, [])
        # Prefer classes with endpoints or SQL calls
        candidates.sort(key=lambda c: (
            len(c.get("endpoints") or []),
            len(c.get("sql_calls") or []),
            len(c.get("methods") or []),
        ), reverse=True)
        for c in candidates[:_SAMPLE_PER_ROLE]:
            sample.append(c)

    return sample


# ── LLM interaction ───────────────────────────────────────────────

_SYSTEM_PROMPT = """당신은 Java 엔터프라이즈 프로젝트의 프레임워크 패턴을 분석하는 전문가입니다.
주어진 클래스 구조 요약을 보고 이 프로젝트의 아키텍처 패턴을 정확히 분류합니다.
반드시 유효한 JSON만 응답하세요."""

_USER_PROMPT_TEMPLATE = """아래는 Java 프로젝트에서 추출한 {n_classes}개 클래스의 구조 요약입니다.
이 프로젝트의 프레임워크 패턴을 분석해주세요.

## 클래스 요약
{class_summaries}

## 추가 통계
- 전체 클래스: {total_classes}개
- Stereotype 분포: {stereo_dist}
- Endpoint 보유 클래스: {ep_classes}개
- SQL 호출 보유 클래스: {sql_classes}개

## 분석 요청 항목

다음 JSON 형식으로 응답하세요. 해당 사항이 없으면 빈 리스트를 쓰세요.

```json
{{
  "framework_type": "spring | vertx | nexcore | custom 중 하나",
  "controller_base_classes": ["이 프로젝트 Controller 가 상속하는 base class 의 simple name 목록"],
  "controller_annotations": ["endpoint 매핑에 사용되는 어노테이션 목록 (예: RequestMapping, GetMapping)"],
  "endpoint_param_types": ["endpoint 메서드 파라미터에서 보이는 프레임워크 타입 (예: IDataSet, HttpServletRequest)"],
  "endpoint_method_conventions": ["endpoint 메서드명의 네이밍 패턴 설명 (예: 'get*', 'find*', 'save*')"],
  "url_suffix": "URL 접미사 규칙 (예: '.do', '' 등)",
  "http_method_default": "기본 HTTP 메서드 (POST 또는 GET)",
  "sql_receivers": ["SQL 호출에 사용하는 객체명 목록 (예: sqlMapClientTemplate, commonSQL)"],
  "sql_operations": ["SQL 호출 메서드명 목록 (예: queryForList, selectList, insert)"],
  "rfc_patterns": ["RFC/SAP 호출 패턴이 있다면 메서드명 목록 (예: getFunction, getJCoFunction)"],
  "rfc_call_methods": ["RFC/인터페이스 호출에 사용하는 커스텀 메서드명 (예: execute, send, call). service.execute('IF-GERP-180', param, ZMM_FUNC.class) 같은 패턴이 있으면 해당 메서드명. 없으면 빈 리스트."],
  "service_suffixes": ["Service 클래스 네이밍 접미사 목록 (예: Service, ServiceImpl, Bo)"],
  "dao_suffixes": ["DAO 클래스 네이밍 접미사 목록 (예: Dao, DaoImpl, Repository)"],
  "di_annotations": ["의존성 주입 어노테이션 목록 (예: Autowired, Inject, Resource)"],
  "notes": "기타 특이사항 (프레임워크 버전, 특수 패턴 등)"
}}
```"""


def _build_prompt(classes: list[dict]) -> str:
    """Build the LLM prompt from the sampled classes."""
    sample = _sample_classes(classes)
    summaries = [_summarise_class(c) for c in sample]

    stereo_dist = Counter(c.get("stereotype") or "(none)" for c in classes)
    dist_str = ", ".join(f"{k}={v}" for k, v in sorted(stereo_dist.items()))
    ep_classes = sum(1 for c in classes if c.get("endpoints"))
    sql_classes = sum(1 for c in classes if c.get("sql_calls"))

    summaries_text = json.dumps(summaries, ensure_ascii=False, indent=2)

    return _USER_PROMPT_TEMPLATE.format(
        n_classes=len(summaries),
        class_summaries=summaries_text,
        total_classes=len(classes),
        stereo_dist=dist_str,
        ep_classes=ep_classes,
        sql_classes=sql_classes,
    )


# ── URL-convention sampling & prompt ─────────────────────────────

_URL_PROMPT_TEMPLATE = """

## URL 관례 분석 (추가)

아래는 이 프로젝트의 **메뉴 URL**, **프론트엔드 디렉토리 구조**, **React/Polymer 라우트** 샘플입니다.
메뉴 URL ↔ 라우트 ↔ 컨트롤러가 서로 매칭되도록 **URL 정규화 규칙**을 추출하세요.

### 메뉴 URL 샘플 ({n_menu}개)
{menu_urls}

### 프론트 하위 디렉토리명 ({n_dirs}개)
{frontend_dirs}

### React/Polymer 라우트 샘플 ({n_routes}개)
{react_routes}

### 응답 JSON 에 아래 `url` 키를 추가하세요

**엄격한 규칙**: 아래 스키마의 값은 *플레이스홀더* 입니다. 위 샘플에서 **실제로 관찰된** 형태만 채워 넣으세요. 관찰 못 했으면 빈 리스트 / null. 샘플에 없는 regex 나 prefix 를 추측해서 쓰지 마세요.

```json
{{
  "url": {{
    "url_prefix_strip":    ["<menu/react 샘플에서 실제 관찰된 공통 prefix 를 벗겨낼 정규식들. 예: ^/apps/[^/]+. 관찰되지 않으면 빈 리스트>"],
    "react_route_prefix":  "<React 라우트에 공통으로 붙는 base prefix 문자열. 없으면 null>",
    "menu_url_scheme":     "<path_only | full_url | app_prefixed 중 샘플과 일치하는 것>",
    "app_key":             {{"source": "<path_segment | query_param>", "index": <N>, "name": "<query_param 일 때 key 이름>"}}
  }}
}}
```

**`app_key.index` 계산 규칙 (혼동 주의)**:
- 프로토콜·호스트 제거 후, 남은 path 를 `/` 로 split 해서 **빈 원소를 제외한 1-based 인덱스**.
- 예: `http://host/apps/hypm_cbmModeling` → path 는 `/apps/hypm_cbmModeling` → segments `["apps", "hypm_cbmModeling"]` → `apps` 는 index **1**, `hypm_cbmModeling` 는 index **2**.
- 앱 슬러그가 `/apps/<slug>` 의 두 번째 segment 면 **`index: 2`**. `/admin/apps/<slug>` 같은 3-depth 면 `index: 3`.

- `url_prefix_strip` 은 각 항목이 **유효한 Python re 정규식** 이어야 합니다. 역슬래시는 YAML/JSON 문자열에서 두 번 씁니다 (`\\d`).
- 매칭되는 패턴이 없으면 해당 필드에 빈 리스트 / null 을 돌려주세요.
- `app_key` 가 없으면 `null` 로.
"""


def _sample_menu_urls(menu_md: str | None, limit: int = 20) -> list[str]:
    if not menu_md or not os.path.isfile(menu_md):
        return []
    try:
        from .legacy_menu_loader import load_menu_from_markdown
        programs = load_menu_from_markdown(menu_md)
    except Exception as e:
        logger.warning("menu.md 샘플 로드 실패: %s", e)
        return []
    urls = [p.get("url", "") for p in programs if p.get("url")]
    return urls[:limit]


def _sample_frontend_routes(frontends_root: str | None,
                             per_dir: int = 5, total: int = 20) -> tuple[list[str], list[str]]:
    """Return ``(frontend_dir_names, sample_routes)``.

    Routes are raw (pre-normalized) when possible — we peek at the
    router parser's intermediate state via `_extract_routes_from_content`
    on a small file sample, so patterns.yaml bootstrap doesn't depend on
    an already-configured ``url_prefix_strip``.
    """
    if not frontends_root or not os.path.isdir(frontends_root):
        return [], []
    from .legacy_react_router import (
        scan_react_dir, _extract_routes_from_content,
    )
    from .mybatis_parser import _read_file_safe

    dir_names = []
    routes: list[str] = []
    for entry in sorted(os.listdir(frontends_root)):
        child = os.path.join(frontends_root, entry)
        if not os.path.isdir(child) or entry.startswith(".") or entry == "node_modules":
            continue
        dir_names.append(entry)
        # Quick raw-route probe
        try:
            files = scan_react_dir(child)[:30]
            count = 0
            for fp in files:
                try:
                    content = _read_file_safe(fp)
                except Exception:
                    continue
                for r in _extract_routes_from_content(content):
                    raw = r.get("path") or ""
                    if raw:
                        routes.append(raw)
                        count += 1
                    if count >= per_dir:
                        break
                if count >= per_dir:
                    break
        except Exception:
            continue
        if len(routes) >= total:
            break
    return dir_names, routes[:total]


def _build_url_prompt(menu_urls: list[str], dir_names: list[str],
                      react_routes: list[str]) -> str:
    return _URL_PROMPT_TEMPLATE.format(
        n_menu=len(menu_urls),
        menu_urls=json.dumps(menu_urls, ensure_ascii=False),
        n_dirs=len(dir_names),
        frontend_dirs=json.dumps(dir_names, ensure_ascii=False),
        n_routes=len(react_routes),
        react_routes=json.dumps(react_routes, ensure_ascii=False),
    )


def _longest_common_path_prefix(urls: list[str]) -> str:
    """Return the longest common path-segment prefix across ``urls``.

    Used as a heuristic fallback when no LLM is available. Only counts
    full path segments so we don't mid-segment-slice a word.
    """
    if not urls:
        return ""
    import re as _re
    # Strip protocol+host
    paths = []
    for u in urls:
        p = _re.sub(r"^https?://[^/]+", "", u.strip())
        p = p.split("?", 1)[0]
        paths.append([seg for seg in p.split("/") if seg])
    if not paths:
        return ""
    common = []
    for i in range(min(len(p) for p in paths)):
        seg = paths[0][i]
        if all(p[i] == seg for p in paths):
            common.append(seg)
        else:
            break
    return "/" + "/".join(common) if common else ""


# ── Frontend sampling & prompt ───────────────────────────────────

_FRONTEND_PROMPT = """

## 프론트엔드 구조 분석 (추가)

아래는 프론트엔드 프로젝트 샘플입니다. React / Polymer 의 **라우터 위치**, **API 호출 패턴**, **버튼 컴포넌트**를 추출해 JSON 응답에 `frontend` 키로 포함하세요.

### package.json dependencies ({n_pkg}개 앱)
{packages}

### 라우터 후보 파일 (각 앱에서 `<Route>` 나 route 배열 선언이 보이는 파일)
{router_samples}

### 대표 컴포넌트 파일 ({n_comp}개, API 호출/버튼 있는 것 위주)
{component_samples}

### 응답 형식

**엄격한 규칙**: 아래 스키마의 값은 *플레이스홀더* 입니다. 위 샘플에서 **실제로 관찰된** 값만 채워 넣으세요. 관찰 못 했으면 빈 리스트 / 빈 문자열 / null. 샘플에 없는 메서드·파일명·컴포넌트명을 추측해서 쓰지 마세요.

```json
{{
  "frontend": {{
    "router_files":        ["<실제 샘플에서 route 선언이 발견된 파일 상대경로. 없으면 빈 리스트>"],
    "route_library":       "<react-router-v5 | react-router-v6 | vaadin-router | polymer | custom | 빈 문자열>",
    "api_call_methods":    ["<대표 컴포넌트 샘플에서 HTTP 호출에 사용된 메서드 dotted name. 예: receiver.method 형태. 관찰된 것만>"],
    "api_url_const_files": ["<URL 문자열이 상수로 선언된 파일 경로. 샘플에 명시된 것만>"],
    "button_components":   ["<샘플 JSX 에서 button 역할로 쓰이는 컴포넌트 태그 이름>"],
    "button_label_props":  ["<버튼 라벨이 들어가는 prop 이름 (children / label / title 등)>"],
    "notes":               "<기타 특이사항>"
  }}
}}
```

자가점검: 채워 넣은 각 값이 위 샘플 텍스트에 실제로 등장하는지 다시 확인하세요. 등장하지 않으면 제거.
"""


def _pick_representative_frontend(frontends_root: str | None) -> str | None:
    """Return the path of the "most representative" sub-repo under
    ``frontends_root``, or ``None`` if not available.

    Heuristic: pick the immediate child with the largest count of
    ``.js/.jsx/.ts/.tsx`` files. That bucket typically holds the most
    routes / components so LLM samples from there are more useful than
    from a tiny stub app. First-wins on ties.
    """
    if not frontends_root or not os.path.isdir(frontends_root):
        return None
    best = None
    best_score = -1
    for entry in sorted(os.listdir(frontends_root)):
        child = os.path.join(frontends_root, entry)
        if not os.path.isdir(child) or entry.startswith(".") or entry == "node_modules":
            continue
        score = 0
        for root, dirs, names in os.walk(child):
            dirs[:] = [d for d in dirs if d not in {"node_modules", "build", "dist", ".git"}]
            for n in names:
                if n.endswith((".js", ".jsx", ".ts", ".tsx")):
                    score += 1
            if score > 200:  # enough signal; stop early
                break
        if score > best_score:
            best_score = score
            best = child
    return best


def _sample_frontend_for_pattern(frontends_root: str | None,
                                  frontend_dir: str | None = None,
                                  per_dir_router: int = 2,
                                  per_dir_comp: int = 3,
                                  total_comp: int = 12) -> tuple[list[dict], list[dict], list[dict]]:
    """Return ``(packages, router_samples, component_samples)`` for LLM.

    If ``frontend_dir`` is given, that **single** repo is sampled (explicit
    representative). Otherwise when ``frontends_root`` is given the helper
    auto-picks the largest sub-repo as a representative via
    :func:`_pick_representative_frontend` — scanning 29 repos blows up the
    prompt without added learning value since conventions are usually
    shared.

    * packages        : ``[{frontend_name, deps: {react-router-dom: ^6.0, ...}}]``
    * router_samples  : ``[{frontend_name, file, snippet}]`` (up to per_dir_router)
    * component_samples : ``[{frontend_name, file, snippet}]`` (up to per_dir_comp, API/버튼 있는 것 위주)

    Snippets are trimmed to ~1500 chars to keep the prompt manageable.
    """
    packages: list[dict] = []
    router_samples: list[dict] = []
    component_samples: list[dict] = []

    # Resolve which dir(s) to sample.
    target_dirs: list[tuple[str, str]] = []  # (frontend_name, abs_path)
    if frontend_dir and os.path.isdir(frontend_dir):
        name = os.path.basename(os.path.normpath(frontend_dir))
        target_dirs.append((name, frontend_dir))
    elif frontends_root and os.path.isdir(frontends_root):
        rep = _pick_representative_frontend(frontends_root)
        if rep:
            target_dirs.append((os.path.basename(rep), rep))

    if not target_dirs:
        return packages, router_samples, component_samples

    import re as _re
    from .mybatis_parser import _read_file_safe
    from .legacy_react_router import scan_react_dir

    ROUTE_HINTS = (_re.compile(r"<Route\b"), _re.compile(r"\bpath\s*:\s*['\"]"),
                    _re.compile(r"createBrowserRouter|createHashRouter|setRoutes"))
    API_HINTS = (_re.compile(r"\baxios\s*\.\s*(?:get|post|put|patch|delete)"),
                  _re.compile(r"\bfetch\s*\("),
                  _re.compile(r"\b\w+\s*\.\s*(?:get|post|put|patch|delete)\s*\("),
                  _re.compile(r"<button\b|<Button\b"))

    for entry, child in target_dirs:
        # package.json
        pkg_path = os.path.join(child, "package.json")
        if os.path.isfile(pkg_path):
            try:
                with open(pkg_path, "r", encoding="utf-8") as f:
                    pkg = json.load(f)
                deps = {}
                for k in ("dependencies", "devDependencies", "peerDependencies"):
                    deps.update(pkg.get(k, {}) or {})
                # Keep only routing/fetch-related deps to cap prompt size
                keep = {k: v for k, v in deps.items()
                        if any(t in k for t in ("react", "polymer", "vaadin", "router",
                                                 "axios", "ky", "fetch", "query", "apollo"))}
                packages.append({"frontend_name": entry, "deps": keep})
            except Exception:
                pass

        # sample files
        try:
            files = scan_react_dir(child)
        except Exception:
            files = []
        router_count = 0
        comp_count = 0
        for fp in files:
            if router_count >= per_dir_router and comp_count >= per_dir_comp:
                break
            try:
                content = _read_file_safe(fp, limit=4000)
            except Exception:
                continue
            if router_count < per_dir_router and any(r.search(content) for r in ROUTE_HINTS):
                router_samples.append({
                    "frontend_name": entry,
                    "file": os.path.relpath(fp, child),
                    "snippet": content[:1500],
                })
                router_count += 1
                continue
            if comp_count < per_dir_comp and any(r.search(content) for r in API_HINTS):
                component_samples.append({
                    "frontend_name": entry,
                    "file": os.path.relpath(fp, child),
                    "snippet": content[:1500],
                })
                comp_count += 1
        if len(component_samples) >= total_comp:
            break

    return packages, router_samples, component_samples


def _build_frontend_prompt(packages: list[dict], router_samples: list[dict],
                            component_samples: list[dict]) -> str:
    return _FRONTEND_PROMPT.format(
        n_pkg=len(packages),
        packages=json.dumps(packages, ensure_ascii=False, indent=2),
        router_samples=json.dumps(router_samples, ensure_ascii=False, indent=2),
        n_comp=len(component_samples),
        component_samples=json.dumps(component_samples, ensure_ascii=False, indent=2),
    )


def _heuristic_frontend_section(packages: list[dict], router_samples: list[dict],
                                  component_samples: list[dict]) -> dict:
    """Non-LLM fallback: pick up route_library from deps, common file names.

    Emits:
      - route_library: from react-router-dom major version
      - router_files : unique basenames of files where router declarations were found
      - api_call_methods : union of regex hits over sample content (case-insensitive)
      - button_components : capitalized JSX tags following the ``<X ... onClick=`` pattern
    """
    section = dict(_DEFAULT_FRONTEND_SECTION)
    # route_library from deps
    majors = []
    for pkg in packages:
        v = (pkg.get("deps") or {}).get("react-router-dom", "")
        m = re.search(r"(\d+)", v or "")
        if m:
            majors.append(int(m.group(1)))
    if majors:
        if max(majors) >= 6:
            section["route_library"] = "react-router-v6"
        elif max(majors) >= 5:
            section["route_library"] = "react-router-v5"
    # polymer / vaadin hints
    for pkg in packages:
        deps = pkg.get("deps") or {}
        if any("polymer" in k for k in deps) or any("vaadin/router" in k for k in deps):
            section["route_library"] = section["route_library"] or "polymer"

    # router_files: top-level unique basenames of router_samples
    seen: set[str] = set()
    router_files: list[str] = []
    for s in router_samples:
        bn = s.get("file", "")
        if bn and bn not in seen:
            seen.add(bn)
            router_files.append(bn)
    section["router_files"] = router_files[:6]

    # api_call_methods: scan snippets for axios.X / fetch( — case-insensitive
    # so Axios.post / AXIOS.GET / Httpclient.request all collapse to a
    # single normalized "<receiver>.<method>" entry.
    call_hits: set[str] = set()
    api_re = re.compile(r"\b(axios|fetch|api|http|request|client|httpclient|axioshelper)\s*\.\s*(\w+)\s*\(",
                         re.IGNORECASE)
    for s in component_samples:
        snippet = s.get("snippet", "") or ""
        for m in api_re.finditer(snippet):
            recv = m.group(1).lower()  # canonicalize to lowercase receiver
            method = m.group(2).lower()
            if method in {"then", "catch", "finally", "config"}:
                continue
            call_hits.add(f"{recv}.{method}")
        if re.search(r"\bfetch\s*\(", snippet, re.IGNORECASE):
            call_hits.add("fetch")
    section["api_call_methods"] = sorted(call_hits)[:20]

    # button_components: capitalized JSX tag that follows an onClick=
    # attribute. Conservative — only counts tags actually wired to a
    # handler so static decoration tags (Icon, Card) don't pollute.
    btn_hits: set[str] = set()
    btn_re = re.compile(r"<([A-Z]\w*)\b[^>]*?\bon(?:Click|Submit)\s*=", re.DOTALL)
    for s in component_samples:
        for m in btn_re.finditer(s.get("snippet", "") or ""):
            btn_hits.add(m.group(1))
    section["button_components"] = sorted(btn_hits)[:10]
    return section


def _merge_frontend_section(llm_fe: dict | None, fallback: dict) -> dict:
    """Overlay LLM-provided frontend section onto heuristic fallback.

    LLM 이 명시적으로 빈 리스트(``[]``) / 빈 문자열을 돌려주면 그건 "값
    없음" 신호이므로 fallback 의 값을 보존한다. 이전엔 LLM 의 빈 리스트가
    heuristic 결과를 덮어씌워 모처럼 찾은 axios.X 정보가 날아갔다.
    """
    merged = dict(_DEFAULT_FRONTEND_SECTION)
    for k, v in (fallback or {}).items():
        if v is not None:
            merged[k] = v
    if not isinstance(llm_fe, dict):
        return merged
    for k in ("router_files", "api_call_methods", "api_url_const_files",
              "button_components", "button_label_props"):
        v = llm_fe.get(k)
        if isinstance(v, list) and v:
            merged[k] = [str(x) for x in v if x]
    for k in ("route_library", "notes"):
        v = llm_fe.get(k)
        if isinstance(v, str) and v:
            merged[k] = v
    return merged


def _heuristic_url_section(menu_urls: list[str], dir_names: list[str]) -> dict:
    """Fallback when no LLM is available: derive naive strip prefix.

    Rules (very conservative to avoid breaking matches):
    - If every menu URL begins with the same path-segment prefix *and*
      the first segment after that prefix matches one of ``dir_names``,
      assume ``/<prefix>/<app>/...`` and emit a strip regex + app_key.
    - Otherwise leave slots empty so the analyzer behaviour is unchanged.
    """
    section = dict(_DEFAULT_URL_SECTION)
    if not menu_urls:
        return section
    prefix = _longest_common_path_prefix(menu_urls)
    if not prefix or prefix == "/":
        return section
    # How many segments in the common prefix?
    prefix_segs = [s for s in prefix.split("/") if s]
    if not prefix_segs:
        return section
    # Does the segment *after* the common prefix match a known dir name?
    app_like = False
    for u in menu_urls:
        import re as _re
        path_only = _re.sub(r"^https?://[^/]+", "", u.strip()).split("?", 1)[0]
        segs = [s for s in path_only.split("/") if s]
        if len(segs) > len(prefix_segs) and dir_names and segs[len(prefix_segs)].lower() in {
            d.lower() for d in dir_names
        }:
            app_like = True
            break
    # Emit a strip for the literal common prefix (escaped).
    import re as _re
    section["url_prefix_strip"] = [f"^{''.join(_re.escape('/' + s) for s in prefix_segs)}"]
    if app_like:
        section["app_key"] = {"source": "path_segment", "index": 1}
        section["menu_url_scheme"] = "app_prefixed"
    return section


def _merge_url_section(llm_url: dict | None, fallback: dict) -> dict:
    """Overlay LLM-provided url section onto a safe default.

    Invalid regexes are dropped; non-dict or empty LLM output falls back
    entirely to ``fallback``. Returns a fresh dict.
    """
    merged = dict(_DEFAULT_URL_SECTION)
    # Apply heuristic fallback first
    for k, v in (fallback or {}).items():
        if v is not None:
            merged[k] = v
    if not isinstance(llm_url, dict):
        return merged
    # Accept known keys only. LLM 이 빈 리스트/빈 문자열을 돌려줘도
    # 이는 "관찰 못 함" 신호이므로 fallback 값을 보존한다 (덮어쓰지 않음).
    if isinstance(llm_url.get("url_prefix_strip"), list) and llm_url["url_prefix_strip"]:
        good = []
        for pat in llm_url["url_prefix_strip"]:
            if not pat:
                continue
            try:
                re.compile(pat)
                good.append(pat)
            except re.error as e:
                logger.warning("LLM이 반환한 url_prefix_strip 정규식 무효 (%r): %s", pat, e)
        if good:
            merged["url_prefix_strip"] = good
    if "react_route_prefix" in llm_url:
        v = llm_url["react_route_prefix"]
        if isinstance(v, str) and v:
            merged["react_route_prefix"] = v
    if "menu_url_scheme" in llm_url:
        v = llm_url["menu_url_scheme"]
        if v in ("path_only", "full_url", "app_prefixed"):
            merged["menu_url_scheme"] = v
    if "app_key" in llm_url:
        v = llm_url["app_key"]
        if isinstance(v, dict) and v.get("source") in ("path_segment", "query_param"):
            merged["app_key"] = v
        elif v is None:
            merged["app_key"] = None
    return merged


def _call_llm(prompt: str, config: dict, max_retries: int = 2,
              label: str = "patterns") -> dict:
    """Send prompt to the LLM and parse the JSON response.

    Uses ``PATTERN_LLM_*`` env vars first (coding-model recommended),
    falls back to generic ``LLM_*`` / ``config.yaml`` ``llm`` section.

    On JSON parse failure the raw response is written to
    ``output/legacy_analysis/pattern_llm_raw_<label>.txt`` so the
    operator can inspect what the model actually returned (truncation,
    prose leakage, invalid syntax, etc.).
    """
    from openai import OpenAI

    llm_config = config.get("llm", {})
    # PATTERN_LLM_* > LLM_* > config.yaml
    api_key = (os.environ.get("PATTERN_LLM_API_KEY")
               or os.environ.get("LLM_API_KEY")
               or llm_config.get("api_key", "ollama"))
    api_base = (os.environ.get("PATTERN_LLM_API_BASE")
                or os.environ.get("LLM_API_BASE")
                or llm_config.get("api_base", "http://localhost:11434/v1"))
    model = (os.environ.get("PATTERN_LLM_MODEL")
             or os.environ.get("LLM_MODEL")
             or llm_config.get("model", "llama3"))
    client = OpenAI(api_key=api_key, base_url=api_base)

    print(f"  LLM model: {model} (PATTERN_LLM_MODEL 또는 LLM_MODEL)")
    print(f"  LLM endpoint: {api_base}")
    print(f"  LLM request: {label} (prompt {len(prompt)} chars)")

    last_text = ""
    for attempt in range(max_retries + 1):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
                timeout=300,
            )
            last_text = response.choices[0].message.content.strip()
            text = last_text

            # Extract first fenced JSON block if present
            json_match = re.search(r'```(?:json)?\s*([\s\S]*?)```', text)
            if json_match:
                text = json_match.group(1).strip()
            else:
                # No fence — try to slice from first `{` to last `}`.
                first = text.find("{")
                last = text.rfind("}")
                if first != -1 and last != -1 and last > first:
                    text = text[first:last + 1]

            return json.loads(text)
        except json.JSONDecodeError as je:
            wait = 2 ** (attempt + 1)
            if attempt < max_retries:
                logger.warning("LLM returned invalid JSON for %s (attempt %d: %s), retrying in %ds...",
                             label, attempt + 1, je, wait)
                time.sleep(wait)
            else:
                logger.error("Failed to parse LLM response for %s after %d attempts",
                             label, max_retries + 1)
                _dump_raw_response(last_text, label)
        except Exception as e:
            logger.error("LLM call failed for %s: %s", label, e)
            if last_text:
                _dump_raw_response(last_text, label)
            break

    return {}


def _dump_raw_response(text: str, label: str) -> None:
    """Write the raw LLM response to ``output/legacy_analysis/pattern_llm_raw_<label>.txt``.

    Silently skipped if the target dir can't be created — this is a best-
    effort debug aid, not a hard requirement.
    """
    try:
        outdir = os.path.join("output", "legacy_analysis")
        os.makedirs(outdir, exist_ok=True)
        path = os.path.join(outdir, f"pattern_llm_raw_{label}.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text or "(empty response)")
        logger.error("  원본 LLM 응답 저장: %s (JSON 파싱 실패 원인 확인용)", path)
    except Exception:
        pass


# ── Pattern file I/O ──────────────────────────────────────────────

_DEFAULT_URL_SECTION = {
    "url_prefix_strip": [],            # list[str] — regexes applied by normalize_url
    "react_route_prefix": None,        # str | None — prepend to React routes before normalize
    "menu_url_scheme": "path_only",    # path_only | full_url | app_prefixed
    "app_key": None,                   # {source: path_segment, index: N} | {source: query_param, name: X}
}


_DEFAULT_FRONTEND_SECTION = {
    # where <Route> declarations live; empty → scan whole tree
    "router_files": [],
    # react-router-v5 | react-router-v6 | vaadin-router | polymer | custom
    "route_library": "",
    # API 호출 메서드 (커스텀 래퍼 포함). 예: axios.get, httpClient.post
    "api_call_methods": [],
    # URL 상수 정의 파일 (2-pass 해석용). 예: src/constants/urls.ts
    "api_url_const_files": [],
    # 버튼으로 추정할 컴포넌트 이름 목록 — 기본 empty. discover-patterns 가
    # 실제 샘플에서 관찰된 값으로 채우거나, 분석기의 내장 `Button`/`button`
    # 기본 감지만으로 동작한다. 여기 가짜 네이밍을 넣어두면 LLM fallback 과
    # 헷갈리기 쉬워 기본값을 비워 둔다.
    "button_components": [],
    # 버튼 라벨이 들어갈 prop 이름 후보
    "button_label_props": ["children", "label", "title"],
    "notes": "",
}


_DEFAULT_PATTERNS = {
    "framework_type": "spring",
    "controller_base_classes": [],
    "controller_annotations": [
        "RequestMapping", "GetMapping", "PostMapping",
        "PutMapping", "DeleteMapping", "PatchMapping",
    ],
    "endpoint_param_types": [],
    "endpoint_method_conventions": [],
    "url_suffix": "",
    "http_method_default": "GET",
    "sql_receivers": [],
    "sql_operations": [],
    "rfc_patterns": [],
    "rfc_call_methods": [],
    "service_suffixes": [
        "Service", "ServiceImpl", "Bo", "BoImpl",
        "Biz", "BizImpl", "Manager", "ManagerImpl",
        "Facade", "FacadeImpl",
    ],
    "dao_suffixes": ["Dao", "DaoImpl", "Repository"],
    "di_annotations": ["Autowired", "Inject", "Resource"],
    "notes": "",
    "url": dict(_DEFAULT_URL_SECTION),
    "frontend": dict(_DEFAULT_FRONTEND_SECTION),
}


def save_patterns(patterns: dict, output_path: str) -> str:
    """Write the pattern dict to a YAML file."""
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    header = (
        f"# Auto-generated by: python main.py discover-patterns\n"
        f"# Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"#\n"
        f"# 이 파일은 LLM 이 프로젝트 소스를 분석해서 생성한 패턴 정의입니다.\n"
        f"# 필요 시 수동으로 수정한 뒤 analyze-legacy --patterns 로 사용하세요.\n"
        f"#\n"
        f"# 사용법:\n"
        f"#   python main.py analyze-legacy \\\n"
        f"#     --backend-dir /path/to/backend \\\n"
        f"#     --patterns {output_path}\n\n"
    )

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(header)
        yaml.dump(patterns, f, allow_unicode=True, default_flow_style=False,
                  sort_keys=False, width=120)

    logger.info("Patterns saved: %s", output_path)
    return output_path


def load_patterns(yaml_path: str) -> dict:
    """Load patterns from YAML and merge with defaults.

    The ``url`` sub-section is deep-merged so older ``patterns.yaml`` files
    without it continue to load with safe defaults.
    """
    with open(yaml_path, "r", encoding="utf-8") as f:
        loaded = yaml.safe_load(f) or {}

    # Merge: loaded values override defaults; lists are replaced, not appended
    merged = dict(_DEFAULT_PATTERNS)
    merged["url"] = dict(_DEFAULT_URL_SECTION)
    merged["frontend"] = dict(_DEFAULT_FRONTEND_SECTION)
    for key, value in loaded.items():
        if value is None:
            continue
        if key == "url" and isinstance(value, dict):
            # deep-merge, keep defaults for missing keys
            url_merged = dict(_DEFAULT_URL_SECTION)
            for uk, uv in value.items():
                if uv is not None:
                    url_merged[uk] = uv
            merged["url"] = url_merged
        elif key == "frontend" and isinstance(value, dict):
            fe_merged = dict(_DEFAULT_FRONTEND_SECTION)
            for uk, uv in value.items():
                if uv is not None:
                    fe_merged[uk] = uv
            merged["frontend"] = fe_merged
        else:
            merged[key] = value

    url_section = merged.get("url") or {}
    fe_section = merged.get("frontend") or {}
    logger.info("Loaded patterns from %s: framework=%s, %d controller_base_classes, "
                "%d sql_receivers, %d service_suffixes, "
                "%d url_prefix_strip, app_key=%s, "
                "%d api_call_methods, route_library=%s",
                yaml_path,
                merged.get("framework_type", "?"),
                len(merged.get("controller_base_classes", [])),
                len(merged.get("sql_receivers", [])),
                len(merged.get("service_suffixes", [])),
                len(url_section.get("url_prefix_strip", []) or []),
                bool(url_section.get("app_key")),
                len(fe_section.get("api_call_methods", []) or []),
                fe_section.get("route_library") or "unknown")
    return merged


# ── Main entry point ──────────────────────────────────────────────

def discover_patterns(backend_dir: str, config: dict,
                       menu_md: str | None = None,
                       frontends_root: str | None = None,
                       frontend_dir: str | None = None) -> dict:
    """Scan backend sources and use LLM to discover project patterns.

    When ``menu_md`` and/or ``frontends_root`` / ``frontend_dir`` are
    provided, also samples menu URLs + frontend sources for URL and
    frontend conventions (emitted under ``url`` and ``frontend`` keys).

    For monorepos the ``frontend_dir`` (single representative repo) is
    preferred — scanning every sub-project blows up the LLM prompt
    without additional learning value since conventions are shared. If
    only ``frontends_root`` is given, the largest sub-repo is auto-
    picked as representative.

    The LLM is called **twice**: once for backend patterns, once for
    url + frontend. This keeps each request's prompt and response under
    typical model context limits and makes partial success useful (e.g.
    backend learned but frontend failed).

    Returns the patterns dict (also suitable for ``save_patterns``).
    """
    from .legacy_java_parser import parse_all_java

    print("=== Step 1: Parsing Java sources ===")
    classes = parse_all_java(backend_dir)
    print(f"  Classes parsed: {len(classes)}")

    if not classes:
        print("  No classes found. Returning defaults.")
        return dict(_DEFAULT_PATTERNS)

    print("\n=== Step 2: Building LLM prompts ===")
    backend_prompt = _build_prompt(classes)
    sample_count = len(_sample_classes(classes))
    print(f"  Sampled {sample_count} classes for backend analysis")

    # URL-convention sampling (optional)
    menu_urls = _sample_menu_urls(menu_md)
    dir_names, react_routes = _sample_frontend_routes(frontends_root)
    want_url = bool(menu_urls or dir_names or react_routes)
    heuristic_url = _heuristic_url_section(menu_urls, dir_names) if want_url else {}
    if want_url:
        print(f"  URL samples: menu={len(menu_urls)} dirs={len(dir_names)} "
              f"routes={len(react_routes)}")

    # Frontend-pattern sampling (optional). Single representative repo —
    # explicit `frontend_dir` wins; otherwise pick largest child of root.
    fe_packages, fe_routers, fe_components = _sample_frontend_for_pattern(
        frontends_root=frontends_root, frontend_dir=frontend_dir,
    )
    want_fe = bool(fe_packages or fe_routers or fe_components)
    heuristic_fe = _heuristic_frontend_section(fe_packages, fe_routers, fe_components) if want_fe else {}
    if want_fe:
        reps = sorted({p["frontend_name"] for p in fe_packages} |
                      {s["frontend_name"] for s in fe_routers} |
                      {s["frontend_name"] for s in fe_components})
        print(f"  Frontend representative repo(s): {reps}")
        print(f"  Frontend samples: packages={len(fe_packages)} routers={len(fe_routers)} "
              f"components={len(fe_components)}")

    # ── LLM call 1: backend patterns ──
    print("\n=== Step 3: Calling LLM — backend patterns ===")
    llm_backend = _call_llm(backend_prompt, config, label="backend")

    # ── LLM call 2: url + frontend (optional) ──
    llm_url_fe: dict = {}
    if want_url or want_fe:
        print("\n=== Step 4: Calling LLM — url / frontend patterns ===")
        # Build a focused prompt that contains *only* the URL + FE
        # sections. Omit backend samples to keep tokens tight.
        secondary = "프로젝트의 URL 관례와 프론트엔드 패턴을 추출해 주세요. 응답에는 `url` 과 `frontend` 두 키만 포함합니다.\n"
        if want_url:
            secondary += _build_url_prompt(menu_urls, dir_names, react_routes)
        if want_fe:
            secondary += _build_frontend_prompt(fe_packages, fe_routers, fe_components)
        llm_url_fe = _call_llm(secondary, config, label="url_frontend")

    if not llm_backend:
        print("  Backend LLM call failed. Returning backend defaults; url/frontend continue if available.")
        patterns = dict(_DEFAULT_PATTERNS)
    else:
        patterns = dict(_DEFAULT_PATTERNS)
        for key, value in llm_backend.items():
            if value is None:
                continue
            if key in patterns and key not in ("url", "frontend"):
                patterns[key] = value

    # URL / frontend merge (uses llm_url_fe + heuristic fallback).
    patterns["url"] = dict(_DEFAULT_URL_SECTION)
    patterns["frontend"] = dict(_DEFAULT_FRONTEND_SECTION)
    if want_url:
        patterns["url"] = _merge_url_section(
            (llm_url_fe or {}).get("url") if isinstance(llm_url_fe, dict) else None,
            heuristic_url,
        )
    if want_fe:
        patterns["frontend"] = _merge_frontend_section(
            (llm_url_fe or {}).get("frontend") if isinstance(llm_url_fe, dict) else None,
            heuristic_fe,
        )

    print(f"\n=== Discovered patterns ===")
    print(f"  Framework type:          {patterns['framework_type']}")
    print(f"  Controller base classes: {patterns['controller_base_classes']}")
    print(f"  Endpoint param types:    {patterns['endpoint_param_types']}")
    print(f"  URL suffix:              '{patterns['url_suffix']}'")
    print(f"  SQL receivers:           {patterns['sql_receivers']}")
    print(f"  SQL operations:          {patterns['sql_operations']}")
    print(f"  Service suffixes:        {patterns['service_suffixes']}")
    print(f"  DAO suffixes:            {patterns['dao_suffixes']}")
    print(f"  RFC patterns:            {patterns['rfc_patterns']}")
    if patterns.get("notes"):
        print(f"  Notes:                   {patterns['notes']}")

    if want_url:
        print(f"\n=== Step 5: URL conventions ===")
        url = patterns["url"]
        print(f"  url_prefix_strip:        {url.get('url_prefix_strip')}")
        print(f"  react_route_prefix:      {url.get('react_route_prefix')}")
        print(f"  menu_url_scheme:         {url.get('menu_url_scheme')}")
        print(f"  app_key:                 {url.get('app_key')}")

    if want_fe:
        print(f"\n=== Step 6: Frontend conventions ===")
        fe = patterns["frontend"]
        print(f"  route_library:           {fe.get('route_library')}")
        print(f"  router_files:            {fe.get('router_files')}")
        print(f"  api_call_methods:        {fe.get('api_call_methods')}")
        print(f"  api_url_const_files:     {fe.get('api_url_const_files')}")
        print(f"  button_components:       {fe.get('button_components')}")

    return patterns
