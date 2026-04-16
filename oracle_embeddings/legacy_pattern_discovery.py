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


def _call_llm(prompt: str, config: dict, max_retries: int = 2) -> dict:
    """Send prompt to the LLM and parse the JSON response."""
    from openai import OpenAI

    llm_config = config.get("llm", {})
    api_key = os.environ.get("LLM_API_KEY") or llm_config.get("api_key", "ollama")
    api_base = os.environ.get("LLM_API_BASE") or llm_config.get("api_base", "http://localhost:11434/v1")
    model = os.environ.get("LLM_MODEL") or llm_config.get("model", "llama3")
    client = OpenAI(api_key=api_key, base_url=api_base)

    print(f"  LLM model: {model}")
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
    "service_suffixes": [
        "Service", "ServiceImpl", "Bo", "BoImpl",
        "Biz", "BizImpl", "Manager", "ManagerImpl",
        "Facade", "FacadeImpl",
    ],
    "dao_suffixes": ["Dao", "DaoImpl", "Repository"],
    "di_annotations": ["Autowired", "Inject", "Resource"],
    "notes": "",
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
    """Load patterns from YAML and merge with defaults."""
    with open(yaml_path, "r", encoding="utf-8") as f:
        loaded = yaml.safe_load(f) or {}

    # Merge: loaded values override defaults; lists are replaced, not appended
    merged = dict(_DEFAULT_PATTERNS)
    for key, value in loaded.items():
        if value is not None:
            merged[key] = value

    logger.info("Loaded patterns from %s: framework=%s, %d controller_base_classes, "
                "%d sql_receivers, %d service_suffixes",
                yaml_path,
                merged.get("framework_type", "?"),
                len(merged.get("controller_base_classes", [])),
                len(merged.get("sql_receivers", [])),
                len(merged.get("service_suffixes", [])))
    return merged


# ── Main entry point ──────────────────────────────────────────────

def discover_patterns(backend_dir: str, config: dict) -> dict:
    """Scan backend sources and use LLM to discover project patterns.

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

    print("\n=== Step 3: Calling LLM ===")
    llm_result = _call_llm(prompt, config)

    if not llm_result:
        print("  LLM call failed. Returning defaults.")
        return dict(_DEFAULT_PATTERNS)

    # Merge LLM result with defaults
    patterns = dict(_DEFAULT_PATTERNS)
    for key, value in llm_result.items():
        if key in patterns and value is not None:
            patterns[key] = value

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

    return patterns
