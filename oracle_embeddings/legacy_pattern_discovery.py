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

```json
{{
  "url": {{
    "url_prefix_strip": ["정규화 직전 메뉴/라우트 URL 에서 제거할 정규식들. 예: '^/apps/[^/]+', '^/api/v\\\\d+'"],
    "react_route_prefix": "React 라우트에만 앞에 붙여야 맞는 공통 prefix. 없으면 null",
    "menu_url_scheme": "path_only | full_url | app_prefixed 중 하나. 메뉴 URL이 어떤 형태인지.",
    "app_key": {{"source": "path_segment", "index": N}} 또는 {{"source": "query_param", "name": "app"}} 또는 null
  }}
}}
```

주의:
- `url_prefix_strip` 은 각 항목이 **유효한 Python re 정규식** 이어야 합니다. 역슬래시는 YAML/JSON 문자열에서 두 번 씁니다 (`\\d`).
- 매칭되는 패턴이 없으면 해당 필드에 빈 리스트/null 을 돌려주세요.
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
    # Accept known keys only
    if isinstance(llm_url.get("url_prefix_strip"), list):
        good = []
        for pat in llm_url["url_prefix_strip"]:
            if not pat:
                continue
            try:
                re.compile(pat)
                good.append(pat)
            except re.error as e:
                logger.warning("LLM이 반환한 url_prefix_strip 정규식 무효 (%r): %s", pat, e)
        merged["url_prefix_strip"] = good
    if "react_route_prefix" in llm_url:
        v = llm_url["react_route_prefix"]
        merged["react_route_prefix"] = v if isinstance(v, str) and v else None
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


def _call_llm(prompt: str, config: dict, max_retries: int = 2) -> dict:
    """Send prompt to the LLM and parse the JSON response.

    Uses ``PATTERN_LLM_*`` env vars first (coding-model recommended),
    falls back to generic ``LLM_*`` / ``config.yaml`` ``llm`` section.
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

    for attempt in range(max_retries + 1):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
                timeout=180,
            )
            text = response.choices[0].message.content.strip()

            # Extract JSON from markdown code block if present
            json_match = re.search(r'```(?:json)?\s*([\s\S]*?)```', text)
            if json_match:
                text = json_match.group(1).strip()

            return json.loads(text)
        except json.JSONDecodeError:
            wait = 2 ** (attempt + 1)
            if attempt < max_retries:
                logger.warning("LLM returned invalid JSON (attempt %d), retrying in %ds...",
                             attempt + 1, wait)
                time.sleep(wait)
            else:
                logger.error("Failed to parse LLM response after %d attempts", max_retries + 1)
        except Exception as e:
            logger.error("LLM call failed: %s", e)
            break

    return {}


# ── Pattern file I/O ──────────────────────────────────────────────

_DEFAULT_URL_SECTION = {
    "url_prefix_strip": [],            # list[str] — regexes applied by normalize_url
    "react_route_prefix": None,        # str | None — prepend to React routes before normalize
    "menu_url_scheme": "path_only",    # path_only | full_url | app_prefixed
    "app_key": None,                   # {source: path_segment, index: N} | {source: query_param, name: X}
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
        else:
            merged[key] = value

    url_section = merged.get("url") or {}
    logger.info("Loaded patterns from %s: framework=%s, %d controller_base_classes, "
                "%d sql_receivers, %d service_suffixes, "
                "%d url_prefix_strip, app_key=%s",
                yaml_path,
                merged.get("framework_type", "?"),
                len(merged.get("controller_base_classes", [])),
                len(merged.get("sql_receivers", [])),
                len(merged.get("service_suffixes", [])),
                len(url_section.get("url_prefix_strip", []) or []),
                bool(url_section.get("app_key")))
    return merged


# ── Main entry point ──────────────────────────────────────────────

def discover_patterns(backend_dir: str, config: dict,
                       menu_md: str | None = None,
                       frontends_root: str | None = None) -> dict:
    """Scan backend sources and use LLM to discover project patterns.

    When ``menu_md`` and/or ``frontends_root`` are provided, also samples
    menu URLs + frontend directory names + React/Polymer raw routes,
    appends a URL-convention section to the LLM prompt, and emits the
    result under the top-level ``url`` key in the returned patterns dict.

    Returns the patterns dict (also suitable for ``save_patterns``).
    """
    from .legacy_java_parser import parse_all_java

    print("=== Step 1: Parsing Java sources ===")
    classes = parse_all_java(backend_dir)
    print(f"  Classes parsed: {len(classes)}")

    if not classes:
        print("  No classes found. Returning defaults.")
        return dict(_DEFAULT_PATTERNS)

    print("\n=== Step 2: Building LLM prompt ===")
    prompt = _build_prompt(classes)
    sample_count = len(_sample_classes(classes))
    print(f"  Sampled {sample_count} classes for LLM analysis")

    # URL-convention sampling (optional)
    menu_urls = _sample_menu_urls(menu_md)
    dir_names, react_routes = _sample_frontend_routes(frontends_root)
    want_url = bool(menu_urls or dir_names or react_routes)
    heuristic_url = _heuristic_url_section(menu_urls, dir_names) if want_url else {}
    if want_url:
        prompt = prompt + _build_url_prompt(menu_urls, dir_names, react_routes)
        print(f"  URL samples: menu={len(menu_urls)} dirs={len(dir_names)} "
              f"routes={len(react_routes)}")

    print("\n=== Step 3: Calling LLM ===")
    llm_result = _call_llm(prompt, config)

    if not llm_result:
        print("  LLM call failed. Returning defaults.")
        patterns = dict(_DEFAULT_PATTERNS)
        if want_url:
            patterns["url"] = _merge_url_section(None, heuristic_url)
            print(f"  URL 섹션 heuristic fallback 적용")
        return patterns

    # Merge LLM result with defaults
    patterns = dict(_DEFAULT_PATTERNS)
    patterns["url"] = dict(_DEFAULT_URL_SECTION)
    for key, value in llm_result.items():
        if value is None:
            continue
        if key == "url" and isinstance(value, dict):
            patterns["url"] = _merge_url_section(value, heuristic_url)
        elif key in patterns:
            patterns[key] = value
    if want_url and patterns["url"] == _DEFAULT_URL_SECTION:
        # LLM returned no url section; fall back to heuristic.
        patterns["url"] = _merge_url_section(None, heuristic_url)

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
        print(f"\n=== Step 4: URL conventions ===")
        url = patterns["url"]
        print(f"  url_prefix_strip:        {url.get('url_prefix_strip')}")
        print(f"  react_route_prefix:      {url.get('react_route_prefix')}")
        print(f"  menu_url_scheme:         {url.get('menu_url_scheme')}")
        print(f"  app_key:                 {url.get('app_key')}")

    return patterns
