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

# Phase II endpoint narrative 스키마 버전 — Phase A/B 와 독립 캐시.
# 프롬프트 / EndpointSpec dataclass / 후처리 규칙 바뀔 때 bump.
ENDPOINT_SPEC_SCHEMA_VERSION = "v1"


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
        # interface→impl 치환 시 양쪽 키를 알 수 있게 원본 fqcn 을 메서드
        # dict 에 보존. extract_backend_biz_logic 가 dual-key 로 biz_map 에
        # 등록 → enrich_rows_with_biz 가 row.service_methods (interface
        # 원본) 로도, Business Logic 시트 출력 (impl 정규화) 로도 매칭됨.
        original_fqcn = fqcn
        if resolved_fqcn != fqcn:
            lookup_stats["iface_resolved"] += 1
            seen.add((resolved_fqcn, mname))
            fqcn = resolved_fqcn
        out.append({
            "fqcn": fqcn,
            "name": mname,
            "body": method.get("body", "") or "",
            "original_fqcn": original_fqcn,  # interface FQCN (있을 경우)
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
            cached.fqcn_method = key
            results[key] = cached
            orig = m.get("original_fqcn")
            if orig and orig != m["fqcn"]:
                results[f"{orig}#{m['name']}"] = cached
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
            # iface→impl 로 치환된 경우 원본 interface 키로도 등록해
            # row.service_methods (interface FQCN 사용) 에서 enrich 매칭 성공.
            orig = m.get("original_fqcn")
            if orig and orig != m["fqcn"]:
                results[f"{orig}#{m['name']}"] = r
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
    """``Business Logic`` 시트용 row 리스트. Programs 열은 reverse index.

    biz_map 에는 iface→impl 매칭을 위해 같은 BizResult 가 interface 키 +
    impl 키로 이중 등록돼 있을 수 있음. 시트에는 한 메서드 당 한 줄만
    필요하므로 BizResult 객체 id 로 dedup + 두 키 중 ``fqcn_method`` 값과
    일치하는 (= impl 쪽) 키를 대표로 선택.
    """
    method_to_programs: Dict[str, List[str]] = {}
    for row in rows:
        sm = row.get("service_methods") or ""
        pg = row.get("program_name") or ""
        if not sm or not pg:
            continue
        for entry in [s.strip() for s in sm.split(";") if s.strip()]:
            method_to_programs.setdefault(entry, []).append(pg)

    # Dedup: 같은 BizResult 객체는 한 번만. 대표 키는 r.fqcn_method (impl).
    seen_ids: set = set()
    primary: Dict[str, BizResult] = {}
    for key, r in biz_map.items():
        if id(r) in seen_ids:
            continue
        seen_ids.add(id(r))
        primary_key = r.fqcn_method if r.fqcn_method else key
        primary[primary_key] = r

    out = []
    for key, r in sorted(primary.items()):
        # Programs 역인덱스: biz_map 에 등록된 모든 alias 키로 찾은 프로그램
        # 합집합. 특히 interface 키로 저장된 row.service_methods 와도 매칭.
        aliases = {k for k, v in biz_map.items() if v is r}
        prog_set = set()
        for alias in aliases:
            for p in method_to_programs.get(alias, []):
                prog_set.add(p)
        out.append({
            "key": key,
            "validations":    _format_structured_list(r.validations),
            "biz_rules":      _format_structured_list(r.biz_rules),
            "state_changes":  _format_structured_list(r.state_changes),
            "calculations":   _format_structured_list(r.calculations),
            "external_calls": _format_structured_list(r.external_calls),
            "summary":        r.summary,
            "source":         r.source,
            "programs":       ", ".join(sorted(prog_set)),
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


# ===========================================================================
# Phase II — endpoint-level narrative (Program Specification)
# ===========================================================================
#
# "프론트 버튼 → validation → 비즈니스 로직 → DML 컬럼" 한 줄 narrative.
# Phase A 의 per-method summary + Phase B 의 per-handler summary + 체인
# 워커가 이미 수집한 column_crud / table_crud / frontend_trigger 를
# 재조립해 LLM 에 **원본 body 없이** 요약만 전달 → 토큰 절감 + 중복
# LLM 호출 회피. opt-in 플래그 ``--extract-program-spec`` (Phase A/B
# 없이는 자동 skip).


@dataclass
class EndpointSpec:
    """Phase II 출력 스키마. endpoint 당 한 행."""

    key: str  # controller_fqcn#method_name (SHA cache key의 baseline)
    trigger_type: str = ""       # READ / CREATE / UPDATE / DELETE / COMPOSITE / OTHER
    input_fields: str = ""       # 버튼 → 수집 field + 검증 간단 설명
    validations: str = ""        # 프론트 validation / 백엔드 pre-check 병합
    business_flow: str = ""      # 서비스 체인 narrative (한 단락)
    read_targets: str = ""       # TABLE.col(R) 목록 (column_crud 에서 R)
    write_targets: str = ""      # TABLE.col(C/U/D) 목록
    purpose_ko: str = ""         # 한 문장 목적 (사용자 요구: "기능이 무엇을 하는지")
    source: str = "llm"          # "llm" | "fallback" | "cache"


# 프롬프트. Phase A 의 `_BACKEND_SYSTEM_PROMPT` 패턴 따라 짧고 명확하게.
_ENDPOINT_SPEC_SYSTEM_PROMPT = (
    "당신은 레거시 Java + MyBatis 기반 엔터프라이즈 애플리케이션의 "
    "endpoint 를 분석해 '프로그램 명세서' 를 생성하는 도우미입니다. "
    "각 endpoint 에 대해 아래 JSON 스키마 그대로 응답하세요. "
    "주석/설명/코드펜스 없이 순수 JSON 배열만. "
    "write_targets 는 제공된 column_crud 의 C/U/D 컬럼만 사용하고 "
    "임의 컬럼을 생성하지 마세요. read_targets 도 column_crud 의 "
    "R 컬럼만 사용."
)

_ENDPOINT_SPEC_USER_PROMPT_TEMPLATE = """아래 {n} 개의 endpoint 에 대해 각각 JSON 객체 하나씩 반환하세요.

입력 정보 (원본 코드 아님, 이미 추출된 요약):
{endpoints_json}

응답 스키마 (배열 길이는 입력과 동일):
[
  {{
    "key": "<입력의 key 그대로>",
    "trigger_type": "READ | CREATE | UPDATE | DELETE | COMPOSITE | OTHER",
    "input_fields": "프론트에서 수집하는 필드와 간단 설명 (한 단락)",
    "validations": "프론트 validation + 백엔드 pre-check 요약 (한 단락)",
    "business_flow": "endpoint 실행 흐름을 3~5 문장으로 narrative. 서비스 chain 순서대로",
    "read_targets": "읽는 테이블.컬럼 나열 (column_crud 의 R)",
    "write_targets": "쓰는 테이블.컬럼 나열 (column_crud 의 C/U/D)",
    "purpose_ko": "이 endpoint 가 무엇을 하는지 한 문장"
  }},
  ...
]"""


def _make_spec_key(row: Dict[str, Any]) -> str:
    """Stable cache key — controller#method + sql_ids + trigger fingerprint."""
    parts = [
        row.get("controller_class", "") or "",
        row.get("program_name", "") or "",
        row.get("url", "") or "",
        row.get("service_methods", "") or "",
        row.get("sql_ids", "") or "",
        row.get("related_columns", "") or "",
        row.get("biz_summary", "") or "",
        row.get("frontend_validation_summary", "") or "",
        row.get("frontend_trigger", "") or "",
    ]
    h = hashlib.sha256()
    h.update(ENDPOINT_SPEC_SCHEMA_VERSION.encode("utf-8"))
    for p in parts:
        h.update(b"\x00")
        h.update(p.encode("utf-8"))
    return h.hexdigest()


def _parse_column_crud_cell(cell: str) -> Dict[str, set]:
    """``TBL.col[한글](CRUD),\nTBL.col2(U)`` → ``{TBL.col: {C,R,U,D}}``.

    Phase I 의 ``_format_column_crud`` 역파싱. LLM 후처리에서 write_targets
    가 실제 CRUD 컬럼 부분집합인지 확인할 때 사용.
    """
    out: Dict[str, set] = {}
    if not cell:
        return out
    for raw in re.split(r",\s*\n?|\n", cell):
        raw = raw.strip()
        if not raw:
            continue
        m = re.match(r"([A-Za-z_][\w.]*?)(?:\[[^\]]*\])?\s*\(([CRUD]+)\)\s*$", raw)
        if not m:
            continue
        key = m.group(1).upper()
        letters = set(m.group(2).upper())
        out.setdefault(key, set()).update(letters)
    return out


def _collect_spec_inputs(row: Dict[str, Any]) -> Dict[str, Any]:
    """Assemble the compact LLM input payload for a single endpoint.

    No original source code — just summaries that Phase A / Phase B
    already produced, plus the column_crud string from Phase I. Keeps
    prompts small and avoids leaking sensitive code to the LLM.
    """
    return {
        "key": _make_spec_key(row),
        "http_method": row.get("http_method", "") or "",
        "url": row.get("url", "") or "",
        "program_name": row.get("program_name", "") or "",
        "controller": row.get("controller_class", "") or "",
        "service_methods": row.get("service_methods", "") or "",
        "sql_ids": row.get("sql_ids", "") or "",
        "tables": row.get("related_tables", "") or "",
        "column_crud": row.get("related_columns", "") or "",
        "procedures": row.get("procedures", "") or "",
        "rfc": row.get("rfc", "") or "",
        "backend_biz_summary": row.get("biz_summary", "") or "",
        "frontend_trigger": row.get("frontend_trigger", "") or "",
        "frontend_validation": row.get("frontend_validation_summary", "") or "",
    }


def _build_spec_batch_prompt(batch: List[Dict[str, Any]]) -> str:
    payload = [_collect_spec_inputs(r) for r in batch]
    return _ENDPOINT_SPEC_USER_PROMPT_TEMPLATE.format(
        n=len(batch),
        endpoints_json=json.dumps(payload, ensure_ascii=False, indent=2),
    )


def _filter_spec_targets(spec_str: str, allowed: Dict[str, set],
                          letters: str) -> str:
    """Drop any ``TBL.col`` in ``spec_str`` not present in ``allowed`` for
    the given ``letters`` (e.g. "CUD" for write_targets, "R" for read).

    ``allowed`` comes from ``_parse_column_crud_cell(row["related_columns"])``
    so the LLM can't hallucinate columns outside what the static AST
    walker actually found. Preserves order + separators.
    """
    if not spec_str:
        return ""
    allowed_letters = set(letters)
    parts = re.split(r",\s*\n?|\n", spec_str)
    kept: List[str] = []
    dropped: List[str] = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        # Accept either bare "TBL.col" or with suffix like "(R)" / "[한글](U)"
        m = re.match(r"([A-Za-z_][\w.]*)(?:\[[^\]]*\])?(?:\([CRUD]+\))?", p)
        if not m:
            continue
        key = m.group(1).upper()
        cand = allowed.get(key, set())
        if cand & allowed_letters:
            kept.append(p)
        else:
            dropped.append(p)
    if dropped:
        logger.warning(
            "EndpointSpec: filtered %d targets outside column_crud: %s",
            len(dropped), dropped[:5],
        )
    return ", ".join(kept)


def _parse_spec_batch(raw: Any, batch: List[Dict[str, Any]]) -> List[EndpointSpec]:
    """LLM 응답 → EndpointSpec 리스트. Phase I column_crud 기반 후처리."""
    items: List[Any] = []
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        for k in ("results", "data", "endpoints", "items"):
            v = raw.get(k)
            if isinstance(v, list):
                items = v
                break
    out: List[EndpointSpec] = []
    for i, row in enumerate(batch):
        key = _make_spec_key(row)
        allowed = _parse_column_crud_cell(row.get("related_columns", "") or "")
        item = items[i] if i < len(items) and isinstance(items[i], dict) else {}
        trig = str(item.get("trigger_type", "") or "").upper().strip()
        spec = EndpointSpec(
            key=key,
            trigger_type=trig or _infer_trigger_type_from_row(row),
            input_fields=str(item.get("input_fields", "") or "").strip(),
            validations=str(item.get("validations", "") or "").strip(),
            business_flow=str(item.get("business_flow", "") or "").strip(),
            read_targets=_filter_spec_targets(
                str(item.get("read_targets", "") or ""), allowed, "R",
            ),
            write_targets=_filter_spec_targets(
                str(item.get("write_targets", "") or ""), allowed, "CUD",
            ),
            purpose_ko=str(item.get("purpose_ko", "") or "").strip(),
            source="llm" if item else "fallback",
        )
        # Fill in trivial targets from column_crud if LLM returned empty
        # (common for tiny endpoints where body_flow is bare CRUD).
        if not spec.read_targets:
            spec.read_targets = _format_targets(allowed, "R")
        if not spec.write_targets:
            spec.write_targets = _format_targets(allowed, "CUD")
        out.append(spec)
    return out


def _format_targets(allowed: Dict[str, set], letters: str) -> str:
    """Render ``{TBL.col: letters}`` as ``TBL.col(R), TBL.col2(U)``."""
    allowed_letters = set(letters)
    canonical = ("C", "R", "U", "D")
    parts: List[str] = []
    for key in sorted(allowed):
        cands = allowed[key] & allowed_letters
        if not cands:
            continue
        ordered = "".join(ch for ch in canonical if ch in cands)
        parts.append(f"{key}({ordered})")
    return ", ".join(parts)


def _infer_trigger_type_from_row(row: Dict[str, Any]) -> str:
    """Cheap deterministic classification used when LLM is unavailable.

    Heuristic: aggregate the CRUD letters present in the row's
    column_crud / related_tables string. If only R → READ, mostly C → CREATE,
    etc. HTTP method is a weaker hint (POST can be any DML).
    """
    cells = (row.get("related_columns", "") or "") + ";" + (row.get("related_tables", "") or "")
    letters = set(re.findall(r"\(([CRUD]+)\)", cells))
    flat = "".join(letters)
    has_c = "C" in flat
    has_r = "R" in flat
    has_u = "U" in flat
    has_d = "D" in flat
    mutations = sum([has_c, has_u, has_d])
    if mutations == 0 and has_r:
        return "READ"
    if mutations > 1:
        return "COMPOSITE"
    if has_c:
        return "CREATE"
    if has_u:
        return "UPDATE"
    if has_d:
        return "DELETE"
    return "OTHER"


def _spec_cache_dir(base: str = "output/legacy_analysis/.spec_cache") -> Path:
    d = Path(base)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _spec_cache_get(key: str, enabled: bool) -> Optional[EndpointSpec]:
    if not enabled:
        return None
    path = _spec_cache_dir() / f"{key}.json"
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
        if raw.get("schema_version") != ENDPOINT_SPEC_SCHEMA_VERSION:
            return None
        spec = EndpointSpec(**{k: v for k, v in raw.items() if k in EndpointSpec.__annotations__})
        spec.source = "cache"
        return spec
    except Exception:
        return None


def _spec_cache_put(key: str, spec: EndpointSpec, enabled: bool) -> None:
    if not enabled:
        return
    path = _spec_cache_dir() / f"{key}.json"
    data = asdict(spec)
    data["schema_version"] = ENDPOINT_SPEC_SCHEMA_VERSION
    try:
        with path.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.debug("spec cache put failed: %s", e)


def extract_endpoint_narrative(
    rows: List[Dict[str, Any]],
    patterns: Optional[Dict[str, Any]] = None,
    *,
    use_cache: bool = True,
    batch_size: int = 10,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, EndpointSpec]:
    """Generate a per-endpoint specification narrative using LLM.

    Relies on Phase A (``biz_summary``) and Phase B (``frontend_*``)
    having already populated the row dicts. For an endpoint without any
    biz summary the resulting spec is fallback-only: trigger_type is
    inferred from CRUD letters, purpose/flow fields stay empty.

    LLM endpoint is the same one configured via ``PATTERN_LLM_*`` / ``LLM_*``
    env (``legacy_pattern_discovery._call_llm``). If the call fails the
    batch silently falls back to the static inference path.
    """
    result: Dict[str, EndpointSpec] = {}
    if not rows:
        return result

    # Collect fallback-only specs up front so cache / LLM only needs the
    # diff. Also serves as the guaranteed non-empty return for each row.
    pending: List[Dict[str, Any]] = []
    for row in rows:
        if not row.get("matched") and not row.get("service_methods") and not row.get("related_tables"):
            # menu-only stub rows with nothing to describe
            continue
        key = _make_spec_key(row)
        cached = _spec_cache_get(key, use_cache)
        if cached is not None:
            result[key] = cached
            continue
        pending.append(row)

    if not pending:
        return result

    # Attempt LLM batch. On any failure fall back to the deterministic
    # empty spec so the user at least gets trigger_type + targets.
    try:
        from .legacy_pattern_discovery import _call_llm as llm_call
    except Exception:
        llm_call = None
    cfg = config or {}

    for start in range(0, len(pending), batch_size):
        batch = pending[start:start + batch_size]
        prompt = _build_spec_batch_prompt(batch)
        raw: Any = None
        if llm_call is not None:
            try:
                raw = llm_call(
                    prompt,
                    cfg,
                    label=f"endpoint_spec_batch_{start // batch_size}",
                    system_prompt=_ENDPOINT_SPEC_SYSTEM_PROMPT,
                )
            except Exception as e:
                logger.warning("EndpointSpec LLM call failed: %s", e)
                raw = None
        specs = _parse_spec_batch(raw or [], batch)
        for spec, row in zip(specs, batch):
            result[spec.key] = spec
            _spec_cache_put(spec.key, spec, use_cache)
    return result


def enrich_rows_with_endpoint_spec(
    rows: List[Dict[str, Any]],
    spec_map: Dict[str, EndpointSpec],
) -> None:
    """Attach ``program_spec_key`` / ``purpose_ko`` to each row in place."""
    for row in rows:
        key = _make_spec_key(row)
        spec = spec_map.get(key)
        if spec is None:
            continue
        row["program_spec_key"] = key
        row.setdefault("purpose_ko", spec.purpose_ko)


def program_spec_sheet_rows(
    spec_map: Dict[str, EndpointSpec],
    rows: List[Dict[str, Any]],
) -> List[Dict[str, str]]:
    """Payload for the ``Program Specification`` sheet.

    Joins each row in ``rows`` with its EndpointSpec (by spec key). Rows
    without a spec are skipped — caller decides whether to still write
    the sheet for consistency.
    """
    out: List[Dict[str, str]] = []
    for row in rows:
        key = _make_spec_key(row)
        spec = spec_map.get(key)
        if spec is None:
            continue
        out.append({
            "main_menu": row.get("main_menu", "") or "",
            "sub_menu": row.get("sub_menu", "") or "",
            "tab": row.get("tab", "") or "",
            "program_name": row.get("program_name", "") or "",
            "http_method": row.get("http_method", "") or "",
            "url": row.get("url", "") or "",
            "trigger_label": row.get("frontend_trigger", "") or "",
            "trigger_type": spec.trigger_type,
            "input_fields": spec.input_fields,
            "validations": spec.validations,
            "business_flow": spec.business_flow,
            "read_targets": spec.read_targets,
            "write_targets": spec.write_targets,
            "purpose_ko": spec.purpose_ko,
            "spec_source": spec.source,
        })
    return out
