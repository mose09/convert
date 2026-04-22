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


_FRONTEND_SYSTEM_PROMPT = (
    "당신은 React 프론트엔드 handler 코드의 validation / 조건부 로직을\n"
    "분석하는 전문가입니다. 주어진 handler body 와 그 주변 JSX slice\n"
    "를 보고 사용자 입력 검증, onClick 전 상태 체크, 조건부 API 호출,\n"
    "읽는 state 를 구조화 JSON 으로 요약하세요.\n"
    "반드시 유효한 JSON 만 응답하세요 (주석 금지)."
)


_FRONTEND_USER_PROMPT_TEMPLATE = """아래 React handler 들의 validation / 조건부 로직을 추출해주세요.
각 handler 에 대해 다음 필드를 채운 JSON 객체를 **배열** 형태로 반환:

```json
[
  {{
    "key": "src/x/Order.jsx#handleSave@/order/save",
    "field_validations": [
      {{"field": "orderDate", "required": true, "format": "YYYYMMDD",
       "range": null, "source": "prop|handler"}}
    ],
    "pre_checks":        [{{"condition": "!selectedRow", "blocks_api": true}}],
    "conditional_calls": [{{"if": "status == 'DRAFT'", "api_url": "/order/save", "method": "POST"}}],
    "state_reads":       ["selectedRow", "userRole"],
    "summary":           "선택 행 필수 + 일자 YYYYMMDD 검증 후 저장 호출"
  }}
]
```

## 규칙
- 배열 요소는 입력 handler 순서 그대로 (index 일치 필수)
- 필드 값이 없으면 빈 배열 [] 유지
- summary 는 한 문장 한국어
- JSX slice 에서 발견되는 `required` / `pattern` / `minLength` / `maxLength` /
  `min` / `max` / `type` prop 은 source="prop" 으로, handler body 의 if/throw/
  alert 검증은 source="handler" 로 분류
- 확신 없으면 해당 필드만 비우고 summary 는 반드시 채울 것

## Handlers (n={n})
{items_json}
"""


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


@dataclass
class FrontendBizResult:
    """Phase B: React handler biz 추출 결과.

    key 는 ``file#handler@url`` (URL 까지 포함해야 같은 핸들러가 여러 URL 에
    묶여있을 때 구분 가능).
    """

    key: str
    file: str = ""
    handler: str = ""
    label: str = ""
    url: str = ""
    field_validations: List[Dict[str, Any]] = field(default_factory=list)
    pre_checks: List[Dict[str, Any]] = field(default_factory=list)
    conditional_calls: List[Dict[str, Any]] = field(default_factory=list)
    state_reads: List[str] = field(default_factory=list)
    summary: str = ""
    source: str = "llm"


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
    """Endpoint 체인에 걸린 메서드를 BFS 로 수집해 dedup 후 반환.

    Seeds (두 소스 합집합):
      1. ``row["service_methods"]`` — inter-class Controller→Service 호출.
         Spring 에서 ``@Autowired`` + ``svc.foo()`` 가 정상 resolve 된 경우.
      2. ``row["controller_class"] + row["method_name"]`` — endpoint 자체.
         Vert.x 처럼 Verticle handler 안에 비즈니스 로직이 인라인된 구조,
         또는 Spring 에서 커스텀 주입으로 chain resolution 실패한 경우에
         graceful degrade 로 endpoint body 를 biz 재료로 활용.

    Intra-class self-call closure (explicit ``this.X()`` + bare ``X()``) 는
    두 경우 모두 확장. false positive 위험은 ``_is_biz_candidate`` static
    filter 가 처리 (trivial delegator / getter / 짧은 body 는 걸러냄).
    """
    svc_index = indexes.get("services_by_fqcn") or {}
    ctrl_index = indexes.get("controllers_by_fqcn") or {}
    mapper_index = indexes.get("mappers_by_fqcn") or {}
    iface_to_impl = indexes.get("iface_to_impl") or {}
    seen: set = set()
    out: List[Dict[str, Any]] = []

    queue: List[tuple] = []
    seed_counts = {"service_methods": 0, "endpoint": 0}

    # Seed 1 — service_methods 가 들어있는 row (inter-class chain 정상 resolve).
    for row in rows:
        sm = row.get("service_methods") or ""
        if not sm:
            continue
        for entry in [s.strip() for s in sm.split(";") if s.strip()]:
            if "#" not in entry:
                continue
            fqcn, mname = entry.split("#", 1)
            queue.append((fqcn, mname))
            seed_counts["service_methods"] += 1

    # Seed 2 — endpoint method 자체. Vert.x Verticle 핸들러 / Spring 체인
    # 미해결 케이스를 커버한다. 중복은 BFS 의 ``seen`` 이 dedup 처리.
    for row in rows:
        ctrl = row.get("controller_class") or ""
        mname = row.get("method_name") or ""
        if ctrl and mname:
            queue.append((ctrl, mname))
            seed_counts["endpoint"] += 1

    lookup_stats = {"iface_resolved": 0, "not_found": 0}

    def _resolve(fqcn: str, mname: str) -> tuple:
        """Class/method lookup with iface→impl fallback.

        Returns ``(cls, method, resolved_fqcn)`` or ``(None, None, fqcn)``.
        iface 가 services_by_fqcn 에 등록돼 있지만 메서드 body 는 impl 에만
        있는 일반적 Spring 패턴 (``OrderService`` 인터페이스 + ``OrderServiceImpl``
        구현) 에서는 interface 쪽 cls 가 잡혀도 method 가 없음 → iface_to_impl
        로 한 번 더 시도해야 한다. 따라서 lookup 은 ``cls 없음`` 경로 뿐 아니라
        ``method 없음`` 경로에서도 impl 로 재시도.
        """
        cls = (svc_index.get(fqcn)
               or ctrl_index.get(fqcn)
               or mapper_index.get(fqcn))
        if cls is not None:
            m = _find_method_in_class(cls, mname)
            if m is not None:
                return cls, m, fqcn
        impl_fqcn = iface_to_impl.get(fqcn)
        if impl_fqcn and impl_fqcn != fqcn:
            impl_cls = (svc_index.get(impl_fqcn)
                        or ctrl_index.get(impl_fqcn)
                        or mapper_index.get(impl_fqcn))
            if impl_cls is not None:
                m = _find_method_in_class(impl_cls, mname)
                if m is not None:
                    return impl_cls, m, impl_fqcn
        return None, None, fqcn

    while queue:
        fqcn, mname = queue.pop(0)
        key = (fqcn, mname)
        if key in seen:
            continue
        seen.add(key)
        cls, method, resolved_fqcn = _resolve(fqcn, mname)
        if cls is None or method is None:
            lookup_stats["not_found"] += 1
            continue
        if resolved_fqcn != fqcn:
            lookup_stats["iface_resolved"] += 1
            # seen 키에 impl FQCN 도 추가해 BFS 중복 방지
            seen.add((resolved_fqcn, mname))
            fqcn = resolved_fqcn
        out.append({
            "fqcn": fqcn,
            "name": mname,
            "body": method.get("body", "") or "",
        })
        # Intra-class self-call 전이 closure. ``legacy_java_parser`` 가
        # bare call 도 synthetic ``receiver="this"`` 로 저장하므로 explicit
        # ``this.X()`` / bare ``X()`` 둘 다 한 번에 잡힌다.
        for fc in method.get("body_field_calls") or []:
            if fc.get("receiver") != "this":
                continue
            target = fc.get("method") or ""
            if target and (fqcn, target) not in seen:
                if _find_method_in_class(cls, target) is not None:
                    queue.append((fqcn, target))

    if out or sum(seed_counts.values()):
        extra = ""
        if lookup_stats["iface_resolved"]:
            extra += f", {lookup_stats['iface_resolved']} iface→impl 해석"
        if lookup_stats["not_found"]:
            extra += f", {lookup_stats['not_found']} 미해결 (class/method 인덱스 miss)"
        print(f"  biz extraction seeds: "
              f"{seed_counts['service_methods']} service_methods + "
              f"{seed_counts['endpoint']} endpoints → "
              f"{len(out)} unique methods (BFS closure 포함{extra})")
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
        if not target_methods:
            print("  biz extraction: no methods collected — endpoint chain "
                  "empty (check 'services=' / 'Method-scope resolution' 위 로그; "
                  "custom 주입 / 네이밍이라 체인이 끊겼을 가능성)")
        else:
            print(f"  biz extraction: 0 candidates after static filter "
                  f"({len(target_methods)} methods 전부 trivial 로 판정됨 — "
                  f"body 짧고 biz 키워드 없음)")
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


# ===========================================================================
# Phase B — Frontend React handler extraction
# ===========================================================================


def _make_frontend_key(ctx: Dict[str, Any], url: str) -> str:
    return f"{ctx.get('file','')}#{ctx.get('handler','')}@{url}"


def _fallback_frontend_summary(ctx: Dict[str, Any]) -> str:
    """regex 로 React handler 간단 요약 (LLM 실패 시)."""
    body = ctx.get("body", "") or ""
    norm = _normalise_body(body)
    ifs = len(re.findall(r"\bif\s*\(", norm))
    throws = len(re.findall(r"\bthrow\s+new\b", norm))
    alerts = len(re.findall(
        r"\b(?:alert|confirm|toast|message|Modal)\s*\(", norm, re.IGNORECASE
    ))
    props = len(ctx.get("validation_props") or [])
    parts = []
    if props:
        parts.append(f"jsx validation props {props}")
    if ifs:
        parts.append(f"if {ifs}")
    if throws:
        parts.append(f"throw {throws}")
    if alerts:
        parts.append(f"alert/toast {alerts}")
    if not parts:
        return "handler w/o static validation signal (heuristic)"
    return "; ".join(parts) + " (static heuristic)"


def _build_frontend_batch_prompt(batch: List[Dict[str, Any]],
                                  max_body_chars: int) -> str:
    prep = []
    for it in batch:
        ctx = it["ctx"]
        body = ctx.get("body", "") or ""
        jsx = ctx.get("jsx_slice", "") or ""
        norm_body = _normalise_body(body)[:max_body_chars]
        # JSX slice 는 더 짧게 (신호 위주, LLM 프롬프트 토큰 통제)
        norm_jsx = _normalise_body(jsx)[:1500]
        prep.append({
            "key": it["key"],
            "file": ctx.get("file", ""),
            "handler": ctx.get("handler", ""),
            "label": ctx.get("label", ""),
            "url": it["url"],
            "validation_props_hint": ctx.get("validation_props") or [],
            "jsx_slice": norm_jsx,
            "handler_body": norm_body,
        })
    return _FRONTEND_USER_PROMPT_TEMPLATE.format(
        n=len(batch),
        items_json=json.dumps(prep, ensure_ascii=False, indent=2),
    )


def _parse_frontend_batch(raw: Any,
                          expected: List[Dict[str, Any]]
                          ) -> List[FrontendBizResult]:
    items: List[Any] = []
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        for k in ("results", "data", "handlers", "items"):
            v = raw.get(k)
            if isinstance(v, list):
                items = v
                break

    def _as_list(v):
        return v if isinstance(v, list) else []

    out: List[FrontendBizResult] = []
    for i, it in enumerate(expected):
        ctx = it["ctx"]
        key = it["key"]
        r = items[i] if i < len(items) and isinstance(items[i], dict) else None
        if r is None:
            out.append(FrontendBizResult(
                key=key,
                file=ctx.get("file", ""),
                handler=ctx.get("handler", ""),
                label=ctx.get("label", ""),
                url=it["url"],
                summary=_fallback_frontend_summary(ctx),
                source="fallback",
            ))
            continue
        raw_states = r.get("state_reads")
        state_reads = [str(s) for s in raw_states if isinstance(s, str)] \
            if isinstance(raw_states, list) else []
        summary = r.get("summary", "")
        out.append(FrontendBizResult(
            key=key,
            file=ctx.get("file", ""),
            handler=ctx.get("handler", ""),
            label=ctx.get("label", ""),
            url=it["url"],
            field_validations=_as_list(r.get("field_validations")),
            pre_checks=_as_list(r.get("pre_checks")),
            conditional_calls=_as_list(r.get("conditional_calls")),
            state_reads=state_reads,
            summary=summary if isinstance(summary, str) else "",
            source="llm",
        ))
    return out


def extract_frontend_biz_logic(
    handlers_by_url: Dict[str, List[Dict[str, Any]]],
    patterns: Dict[str, Any],
    *,
    max_handlers: int = 300,
    use_cache: bool = True,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, FrontendBizResult]:
    """Main entry for Phase B.

    입력: ``collect_handler_contexts`` 가 준 ``{url: [ctx, ...]}``.
    반환: ``{key: FrontendBizResult}`` where ``key = file#handler@url``.
    """
    bz = _effective_config(patterns)
    batch_size = max(1, int(bz.get("llm_batch_size", 6)))
    max_body_chars = int(bz.get("llm_max_body_chars", 3500))

    # 평탄화 + dedup by key (같은 handler 가 여러 URL 에 바인딩돼도 per-URL
    # 단위로 분석 — 조건부 API 호출 맥락 파악에 도움).
    items: List[Dict[str, Any]] = []
    seen_keys: set = set()
    for url, ctx_list in handlers_by_url.items():
        for ctx in ctx_list or []:
            key = _make_frontend_key(ctx, url)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            items.append({"key": key, "url": url, "ctx": ctx})

    if not items:
        print("  frontend biz: no handler contexts collected")
        return {}

    if len(items) > max_handlers:
        print(f"  frontend biz: {len(items)} handlers > cap {max_handlers} — truncating")
        items = items[:max_handlers]

    print(f"  frontend biz: {len(items)} handler×url pairs")

    # cache lookup: 프론트는 body + jsx + validation props 의 hash 를 사용
    results: Dict[str, FrontendBizResult] = {}
    to_llm: List[Dict[str, Any]] = []
    for it in items:
        ctx = it["ctx"]
        blob = ctx.get("body", "") + "\n---JSX---\n" + (ctx.get("jsx_slice", "") or "")
        cache_key = _hash_body(blob)
        cached = _cache_get_fe(cache_key, use_cache)
        if cached is not None:
            # Refresh identity fields from current context (file/handler/url).
            cached.key = it["key"]
            cached.file = ctx.get("file", "")
            cached.handler = ctx.get("handler", "")
            cached.label = ctx.get("label", "")
            cached.url = it["url"]
            results[it["key"]] = cached
        else:
            it["_cache_key"] = cache_key
            to_llm.append(it)

    if results:
        print(f"  frontend biz: cache hit {len(results)}/{len(items)}")

    if not to_llm:
        return results

    try:
        from .legacy_pattern_discovery import _call_llm as llm_call
    except Exception as exc:
        logger.warning("frontend biz: LLM wrapper unavailable (%s)", exc)
        for it in to_llm:
            ctx = it["ctx"]
            results[it["key"]] = FrontendBizResult(
                key=it["key"],
                file=ctx.get("file", ""),
                handler=ctx.get("handler", ""),
                label=ctx.get("label", ""),
                url=it["url"],
                summary=_fallback_frontend_summary(ctx),
                source="fallback",
            )
        return results

    cfg = config or {}
    llm_ok = 0
    fb = 0
    for start in range(0, len(to_llm), batch_size):
        batch = to_llm[start:start + batch_size]
        prompt = _build_frontend_batch_prompt(batch, max_body_chars)
        label = f"biz_fe_batch_{start // batch_size}"
        try:
            raw = llm_call(
                prompt,
                cfg,
                label=label,
                system_prompt=_FRONTEND_SYSTEM_PROMPT,
            )
        except Exception as exc:
            logger.warning("frontend biz LLM call %s failed: %s", label, exc)
            raw = {}
        parsed = _parse_frontend_batch(raw, batch)
        for it, r in zip(batch, parsed):
            results[it["key"]] = r
            if r.source == "llm":
                llm_ok += 1
                _cache_put_fe(it["_cache_key"], r, use_cache)
            else:
                fb += 1

    print(f"  frontend biz: LLM {llm_ok} ok, {fb} fallback")
    return results


def _cache_get_fe(key: str, enabled: bool) -> Optional[FrontendBizResult]:
    if not enabled:
        return None
    p = _cache_dir() / f"fe_{key}.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    data.pop("source", None)
    try:
        return FrontendBizResult(**data, source="cache")
    except TypeError:
        return None


def _cache_put_fe(key: str, result: FrontendBizResult, enabled: bool) -> None:
    if not enabled:
        return
    p = _cache_dir() / f"fe_{key}.json"
    data = {k: v for k, v in asdict(result).items() if k != "source"}
    p.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8",
    )


def enrich_rows_with_frontend_biz(
    rows: List[Dict[str, Any]],
    fe_map: Dict[str, FrontendBizResult],
) -> None:
    """row 의 ``frontend_validation_summary`` 필드 채움.

    매칭 기준: row 의 `url` + `presentation_layer` (파일 경로) 가 fe_map
    엔트리의 url + file 과 맞으면 summary 를 "; " 로 join. 여러 handler 가
    걸려있으면 상위 3개만.
    """
    # Index by (url_lower, file) for fast lookup
    by_url_file: Dict[tuple, List[FrontendBizResult]] = {}
    for r in fe_map.values():
        by_url_file.setdefault((r.url.lower(), r.file), []).append(r)

    for row in rows:
        summaries: List[str] = []
        url = (row.get("url") or "").lower()
        files = (row.get("presentation_layer") or "").split(";")
        for f in files:
            f = f.strip()
            if not f:
                continue
            matches = by_url_file.get((url, f), [])
            for r in matches[:3]:
                if r.summary:
                    summaries.append(r.summary)
        row.setdefault("frontend_validation_summary", "")
        if summaries:
            row["frontend_validation_summary"] = " | ".join(summaries[:3])


def frontend_biz_sheet_rows(
    fe_map: Dict[str, FrontendBizResult],
) -> List[Dict[str, str]]:
    out = []
    for key, r in sorted(fe_map.items()):
        out.append({
            "key": key,
            "screen":            r.file,
            "button":            r.label,
            "handler":           r.handler,
            "url":               r.url,
            "field_validations": _format_structured_list(r.field_validations),
            "pre_checks":        _format_structured_list(r.pre_checks),
            "conditional_calls": _format_structured_list(r.conditional_calls),
            "state_reads":       ", ".join(r.state_reads),
            "summary":           r.summary,
            "source":            r.source,
        })
    return out
