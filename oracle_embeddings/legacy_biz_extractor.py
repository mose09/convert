"""Business logic extraction (Phase A: backend ServiceImpl).

엔드포인트 체인이 도달한 ServiceImpl 메서드 body 를 LLM 에 보내 구조화된
JSON (validations / biz_rules / state_changes / calculations / summary) 으로
추출. static pre-filter 로 trivial getter/setter 스킵, disk cache 로 재실행
비용 0, fallback regex 로 LLM 없어도 summary 채움.

외부 의존:
- ``legacy_pattern_discovery._call_llm`` — retry / JSON 파싱 / raw dump 재사용
- ``method["body"]`` / ``method["name"]`` — legacy_java_parser 가 이미 보존

Scope: 사용자 결정에 따라 엔드포인트 체인이 도달한 메서드만 (scope 자동 축소
— LLM 비용 통제). row["service_methods"] 의 ``FQCN#method`` 토큰으로 메서드
지정. Frontend (Phase B) 는 별도 모듈 확장 예정.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# 프롬프트 스키마 버전 — 변경 시 bump 해서 캐시 자동 무효화
BIZ_SCHEMA_VERSION = "v1"


# 자체 default (patterns.yaml 미지정 / biz_extraction 섹션 누락 시 fallback).
# ``legacy_pattern_discovery._DEFAULT_PATTERNS['biz_extraction']`` 와 동일
# 값이어야 함 (둘 중 하나만 수정하면 동작 불일치).
_BUILTIN_DEFAULTS: Dict[str, Any] = {
    "backend_skip_methods": [
        "toString", "equals", "hashCode", "get*", "set*",
    ],
    "min_body_chars": 120,
    "biz_keyword_hints": [
        "if", "switch", "throw", "validate", "check", "status",
    ],
    "frontend_validation_props": [
        "required", "pattern", "minLength", "maxLength", "min", "max", "type",
    ],
    "llm_batch_size": 6,
    "llm_max_body_chars": 3500,
}


def _effective_config(patterns: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge user patterns.yaml 의 ``biz_extraction`` 와 built-in defaults."""
    user = (patterns or {}).get("biz_extraction") or {}
    merged = dict(_BUILTIN_DEFAULTS)
    for k, v in user.items():
        if v is not None:
            merged[k] = v
    return merged


_BACKEND_SYSTEM_PROMPT = (
    "당신은 Java 엔터프라이즈 ServiceImpl 메서드의 비즈니스 로직을 분석하는 전문가입니다.\n"
    "주어진 메서드 body 에서 validation, 분기 규칙, 상태 전환, 계산식, 외부 호출을\n"
    "추출해 구조화된 JSON 으로 요약하세요.\n"
    "반드시 유효한 JSON 만 응답하세요 (주석 금지)."
)


_BACKEND_USER_PROMPT_TEMPLATE = """아래 ServiceImpl 메서드들의 비즈니스 로직을 추출해주세요.
각 메서드에 대해 다음 필드를 채운 JSON 객체를 **배열** 형태로 반환 (top-level 은 배열):

```json
[
  {{
    "fqcn_method": "com.x.OrderServiceImpl#saveOrder",
    "validations":    [{{"field": "orderDate", "rule": "not null", "error": "필수값 누락"}}],
    "biz_rules":      [{{"when": "status == 'DRAFT'", "then": "허용"}}],
    "state_changes":  [{{"entity": "Order", "from": "DRAFT", "to": "SUBMITTED"}}],
    "calculations":   [{{"target": "totalAmount", "formula": "price * qty"}}],
    "external_calls": [{{"kind": "rfc|sql|service", "name": "Z_ORDER_SAVE"}}],
    "summary":        "주문 저장 전 상태 체크 후 SUBMITTED 로 전환"
  }}
]
```

## 규칙
- 배열 요소는 입력 메서드 순서 그대로 (index 일치 필수)
- 필드 값이 없으면 빈 배열 [] 유지 (null 금지)
- summary 는 한 문장 한국어
- 확신 없으면 해당 필드만 비우고 summary 는 반드시 채울 것
- 메서드 body 에 실제로 등장하지 않는 필드명은 절대 만들지 말 것

## 메서드 목록 (n={n_methods})
{methods_json}
"""


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------


@dataclass
class BizResult:
    fqcn_method: str
    validations: List[Dict[str, Any]] = field(default_factory=list)
    biz_rules: List[Dict[str, Any]] = field(default_factory=list)
    state_changes: List[Dict[str, Any]] = field(default_factory=list)
    calculations: List[Dict[str, Any]] = field(default_factory=list)
    external_calls: List[Dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    source: str = "llm"  # "llm" | "fallback" | "cache"


# ---------------------------------------------------------------------------
# Body hashing / normalisation
# ---------------------------------------------------------------------------


def _normalise_body(body: str) -> str:
    """Strip comments + collapse whitespace for stable hashing + smaller prompts."""
    body = re.sub(r"//[^\n]*", "", body)
    body = re.sub(r"/\*[\s\S]*?\*/", "", body)
    body = re.sub(r"\s+", " ", body)
    return body.strip()


def _hash_body(body: str, schema_version: str = BIZ_SCHEMA_VERSION) -> str:
    norm = _normalise_body(body)
    h = hashlib.sha256()
    h.update(schema_version.encode("utf-8"))
    h.update(b"\x00")
    h.update(norm.encode("utf-8"))
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Static triage
# ---------------------------------------------------------------------------


def _skip_by_method_name(name: str, skip_patterns: List[str]) -> bool:
    for p in skip_patterns:
        if "*" in p:
            if fnmatch.fnmatchcase(name, p):
                return True
        elif name == p:
            return True
    return False


def _is_biz_candidate(method: Dict[str, Any], patterns: Dict[str, Any]) -> bool:
    """body 길이 + 키워드 힌트로 LLM 대상 여부 결정."""
    bz = _effective_config(patterns)
    skip = bz.get("backend_skip_methods", []) or []
    min_chars = int(bz.get("min_body_chars", 120))
    hints = bz.get("biz_keyword_hints", []) or []

    name = method.get("name", "") or ""
    body = method.get("body", "") or ""
    if not name or not body:
        return False
    if _skip_by_method_name(name, skip):
        return False
    if len(body) >= min_chars:
        return True
    if hints:
        hint_re = re.compile(
            r"\b(?:" + "|".join(re.escape(h) for h in hints) + r")\b",
            re.IGNORECASE,
        )
        if hint_re.search(body):
            return True
    return False


def _fallback_summary(body: str) -> str:
    """regex 로 간단 요약 — LLM down 시 채움."""
    if not body:
        return ""
    norm = _normalise_body(body)
    ifs = len(re.findall(r"\bif\s*\(", norm, re.IGNORECASE))
    throws = len(re.findall(r"\bthrow\s+new\b", norm))
    switches = len(re.findall(r"\bswitch\s*\(", norm, re.IGNORECASE))
    sqls = len(re.findall(
        r"\b(?:select|insert|update|delete|selectList|queryForList)\s*\(",
        norm, re.IGNORECASE,
    ))
    parts = []
    if ifs:
        parts.append(f"if {ifs}")
    if switches:
        parts.append(f"switch {switches}")
    if throws:
        parts.append(f"throw {throws}")
    if sqls:
        parts.append(f"sql {sqls}")
    if not parts:
        return "no branching / validation found (static heuristic)"
    return "; ".join(parts) + " (static heuristic)"


def _prepare_body_for_llm(body: str, max_chars: int) -> str:
    norm = _normalise_body(body)
    if len(norm) <= max_chars:
        return norm
    return norm[: max(0, max_chars - 20)] + " ... <TRUNCATED>"


# ---------------------------------------------------------------------------
# Disk cache
# ---------------------------------------------------------------------------


def _cache_dir(base: str = "output/legacy_analysis/.biz_cache") -> Path:
    d = Path(base)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cache_get(key: str, enabled: bool) -> Optional[BizResult]:
    if not enabled:
        return None
    p = _cache_dir() / f"{key}.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    data.pop("source", None)  # cache file has no "source" marker
    try:
        return BizResult(**data, source="cache")
    except TypeError:
        return None


def _cache_put(key: str, result: BizResult, enabled: bool) -> None:
    if not enabled:
        return
    p = _cache_dir() / f"{key}.json"
    data = {k: v for k, v in asdict(result).items() if k != "source"}
    p.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Scope collection
# ---------------------------------------------------------------------------


def _find_method_in_class(cls: Dict[str, Any], name: str) -> Optional[Dict[str, Any]]:
    for m in cls.get("methods") or []:
        if m.get("name") == name:
            return m
    return None


def collect_chain_methods(rows: List[Dict[str, Any]],
                          indexes: Dict[str, Any]) -> List[Dict[str, Any]]:
    """rows 의 ``service_methods`` seed + 각 메서드의 intra-class self-call
    전이 closure 를 BFS 로 수집해 dedup 후 method dict 리스트 반환.

    service_methods 는 ``_resolve_endpoint_chain`` 이 inter-class 호출만
    append 하므로 ``this.validateDates()`` / bare ``calculateTotal()`` 같이
    같은 ServiceImpl 안 helper 는 누락됨. 체인 walker 는 이들을 방문하므로
    biz 추출 scope 도 동일한 범위로 맞춰야 "엔드포인트가 도달하는 비즈니스
    로직" 이 완전해진다.
    """
    svc_index = indexes.get("services_by_fqcn") or {}
    seen: set = set()
    out: List[Dict[str, Any]] = []

    # BFS queue of (fqcn, method_name) starting from service_methods seed
    queue: List[tuple] = []
    for row in rows:
        sm = row.get("service_methods") or ""
        if not sm:
            continue
        for entry in [s.strip() for s in sm.split(";") if s.strip()]:
            if "#" not in entry:
                continue
            fqcn, mname = entry.split("#", 1)
            queue.append((fqcn, mname))

    while queue:
        fqcn, mname = queue.pop(0)
        key = (fqcn, mname)
        if key in seen:
            continue
        seen.add(key)
        cls = svc_index.get(fqcn)
        if not cls:
            continue
        method = _find_method_in_class(cls, mname)
        if method is None:
            continue
        out.append({
            "fqcn": fqcn,
            "name": mname,
            "body": method.get("body", "") or "",
        })
        # Intra-class self-calls → 같은 클래스의 다른 메서드로 BFS 확장.
        # ``legacy_java_parser`` 가 bare call 도 synthetic ``receiver="this"``
        # 로 저장하므로 이 하나로 explicit ``this.X()`` / bare ``X()`` 둘 다 커버.
        for fc in method.get("body_field_calls") or []:
            if fc.get("receiver") != "this":
                continue
            target = fc.get("method") or ""
            if target and (fqcn, target) not in seen:
                if _find_method_in_class(cls, target) is not None:
                    queue.append((fqcn, target))
    return out


# ---------------------------------------------------------------------------
# LLM batch
# ---------------------------------------------------------------------------


def _build_batch_prompt(batch: List[Dict[str, Any]], max_body_chars: int) -> str:
    prep = []
    for m in batch:
        prep.append({
            "fqcn_method": f"{m['fqcn']}#{m['name']}",
            "body": _prepare_body_for_llm(m["body"], max_body_chars),
        })
    return _BACKEND_USER_PROMPT_TEMPLATE.format(
        n_methods=len(batch),
        methods_json=json.dumps(prep, ensure_ascii=False, indent=2),
    )


def _parse_llm_batch_response(raw: Any,
                              expected: List[Dict[str, Any]]) -> List[BizResult]:
    """LLM 응답 → ``BizResult`` 리스트. 부족/누락 건은 fallback 으로 채움."""
    items: List[Any] = []
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        for k in ("results", "data", "methods", "items"):
            v = raw.get(k)
            if isinstance(v, list):
                items = v
                break

    out: List[BizResult] = []
    for i, m in enumerate(expected):
        key = f"{m['fqcn']}#{m['name']}"
        r = items[i] if i < len(items) and isinstance(items[i], dict) else None
        if r is None:
            out.append(BizResult(
                fqcn_method=key,
                summary=_fallback_summary(m["body"]),
                source="fallback",
            ))
            continue

        def _as_list(v):
            return v if isinstance(v, list) else []

        summary_val = r.get("summary", "")
        out.append(BizResult(
            fqcn_method=key,
            validations=_as_list(r.get("validations")),
            biz_rules=_as_list(r.get("biz_rules")),
            state_changes=_as_list(r.get("state_changes")),
            calculations=_as_list(r.get("calculations")),
            external_calls=_as_list(r.get("external_calls")),
            summary=summary_val if isinstance(summary_val, str) else "",
            source="llm",
        ))
    return out


def extract_backend_biz_logic(
    target_methods: List[Dict[str, Any]],
    patterns: Dict[str, Any],
    *,
    max_methods: int = 500,
    use_cache: bool = True,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, BizResult]:
    """Main entry — dedup 된 ServiceImpl 메서드 → ``{fqcn#method: BizResult}``.

    단계:
        1. static filter (``_is_biz_candidate``)
        2. hard cap (``max_methods``)
        3. disk cache 조회
        4. 남은 건 batch LLM 호출 (prompt 스키마 v1)
        5. LLM 실패 / 포맷 불일치 → regex fallback summary
    """
    bz = _effective_config(patterns)
    batch_size = max(1, int(bz.get("llm_batch_size", 6)))
    max_body_chars = int(bz.get("llm_max_body_chars", 3500))

    candidates = [m for m in target_methods if _is_biz_candidate(m, patterns or {})]
    if not candidates:
        print("  biz extraction: no candidate methods after static filter")
        return {}

    if len(candidates) > max_methods:
        print(f"  biz extraction: {len(candidates)} candidates > cap {max_methods} — truncating")
        candidates = candidates[:max_methods]

    print(f"  biz extraction: {len(candidates)} candidate methods "
          f"(of {len(target_methods)} resolved)")

    # cache lookup
    to_llm: List[Dict[str, Any]] = []
    results: Dict[str, BizResult] = {}
    for m in candidates:
        cached = _cache_get(_hash_body(m["body"]), use_cache)
        if cached is not None:
            key = f"{m['fqcn']}#{m['name']}"
            cached.fqcn_method = key  # cache 파일 에는 원래 키가 있지만 현재 fqcn 우선
            results[key] = cached
        else:
            to_llm.append(m)

    if results:
        print(f"  biz extraction: cache hit {len(results)}/{len(candidates)}")

    if not to_llm:
        return results

    # LLM path
    try:
        from .legacy_pattern_discovery import _call_llm as llm_call
    except Exception as exc:
        logger.warning("biz extraction: LLM wrapper unavailable (%s)", exc)
        for m in to_llm:
            key = f"{m['fqcn']}#{m['name']}"
            results[key] = BizResult(
                fqcn_method=key,
                summary=_fallback_summary(m["body"]),
                source="fallback",
            )
        return results

    cfg = config or {}
    llm_ok = 0
    fallback_ct = 0
    for start in range(0, len(to_llm), batch_size):
        batch = to_llm[start:start + batch_size]
        prompt = _build_batch_prompt(batch, max_body_chars)
        label = f"biz_batch_{start // batch_size}"
        try:
            raw = llm_call(
                prompt,
                cfg,
                label=label,
                system_prompt=_BACKEND_SYSTEM_PROMPT,
            )
        except Exception as exc:
            logger.warning("biz LLM call %s failed: %s", label, exc)
            raw = {}
        parsed = _parse_llm_batch_response(raw, batch)
        for m, r in zip(batch, parsed):
            key = f"{m['fqcn']}#{m['name']}"
            results[key] = r
            if r.source == "llm":
                llm_ok += 1
                _cache_put(_hash_body(m["body"]), r, use_cache)
            else:
                fallback_ct += 1

    print(f"  biz extraction: LLM {llm_ok} ok, {fallback_ct} fallback")
    return results


# ---------------------------------------------------------------------------
# Row enrichment + sheet data
# ---------------------------------------------------------------------------


def enrich_rows_with_biz(rows: List[Dict[str, Any]],
                         biz_map: Dict[str, BizResult]) -> None:
    """In-place: row 의 ``biz_summary`` / ``biz_detail_key`` 필드 채움.

    row 당 최대 3 개 메서드의 summary 를 " | " 로 join (narrow 한 Excel 줄에
    모두 담지 않기 위해). 전체 키 리스트는 ``biz_detail_key`` 에 그대로 보존.
    """
    for row in rows:
        sm = row.get("service_methods") or ""
        if not sm:
            row.setdefault("biz_summary", "")
            row.setdefault("biz_detail_key", "")
            continue
        keys = [s.strip() for s in sm.split(";") if s.strip()]
        summaries = []
        for k in keys[:3]:
            r = biz_map.get(k)
            if r and r.summary:
                summaries.append(r.summary)
        row["biz_summary"] = " | ".join(summaries)
        row["biz_detail_key"] = "; ".join(keys)


def _format_structured_list(items: List[Dict[str, Any]]) -> str:
    """List[dict] 을 ``key=value`` / newline 형식으로 사람이 읽기 쉽게."""
    if not items:
        return ""
    lines = []
    for it in items:
        if not isinstance(it, dict):
            continue
        kv = [f"{k}={v}" for k, v in it.items() if v not in (None, "", [])]
        if kv:
            lines.append(" / ".join(kv))
    return "\n".join(lines)


def biz_detail_sheet_rows(biz_map: Dict[str, BizResult],
                          rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """``Business Logic`` 시트용 row 리스트. Programs 열은 reverse index."""
    method_to_programs: Dict[str, List[str]] = {}
    for row in rows:
        sm = row.get("service_methods") or ""
        pg = row.get("program_name") or ""
        if not sm or not pg:
            continue
        for entry in [s.strip() for s in sm.split(";") if s.strip()]:
            method_to_programs.setdefault(entry, []).append(pg)

    out = []
    for key, r in sorted(biz_map.items()):
        programs = sorted(set(method_to_programs.get(key, [])))
        out.append({
            "key": key,
            "validations":    _format_structured_list(r.validations),
            "biz_rules":      _format_structured_list(r.biz_rules),
            "state_changes":  _format_structured_list(r.state_changes),
            "calculations":   _format_structured_list(r.calculations),
            "external_calls": _format_structured_list(r.external_calls),
            "summary":        r.summary,
            "source":         r.source,
            "programs":       ", ".join(programs),
        })
    return out
