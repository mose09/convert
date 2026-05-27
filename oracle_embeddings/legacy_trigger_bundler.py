"""Trigger bundle builder — 한 trigger (버튼 onClick / Select onChange 등)
의 모든 호출 chain 을 한 덩어리로 묶어 LLM 분석용 컨텍스트 생성.

설계 의도:
- 기존 ``--closure-llm`` 은 화면 file 통째 (closure import 그래프 BFS)
  를 LLM 에 던졌는데, 화면 안 trigger 마다 관심사가 분리되지 않아 LLM
  이 cascading / 분기 / setState clear 같은 trigger-specific 의미 추론
  품질이 일정하지 않음.
- 백엔드 ``--extract-biz-logic`` 은 endpoint 단위 (Controller→Service→
  Mapper→SQL) 로 묶어서 한 번 호출하는 균일한 패턴 — 프런트도 trigger
  단위 균일로 맞추기 위한 1단계.

이 모듈 (Phase 1) 은 bundle dict + 직렬화만 제공. Phase 2 에서 LLM
호출 + 캐시, Phase 3 에서 응답 머지로 이어진다.

Bundle dict 구조::

    {
      "trigger_jsx": "<Button onClick={this.search}>조회</Button>",
      "event_type": "onClick",
      "handler_name": "search",
      "label": "조회",
      "source_file": "src/.../index.js",
      "handler_chain": [
        {"name": "search", "kind": "handler", "file": "...", "body": "..."},
        {"name": "loadingResultList", "kind": "action", "file": "actions.js",
         "body": "...", "type_key": "LOAD_RESULT_LIST"},
        {"name": "loadResultListSaga", "kind": "saga", "file": "saga.js",
         "body": "..."},
      ],
      "setstate_writes": ["fab=event", "team=undefined", ...],
      "factual_urls": ["POST /api/result-list"],
    }

직렬화 결과는 LLM user-message body 로 그대로 사용 가능한 markdown.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, List, Optional

# 사용자 코드 (scanner) 의 helper 재사용 — Phase 1 은 추출 로직만, LLM
# 호출/캐시는 Phase 2.
from .legacy_react_api_scanner import (
    _DISPATCH_ACTION_RE,
    _FN_CALL_LEAF_RE,
    _MDTP_KV_RE,
    _RESERVED_NEAR_BLOCK,
    _THIS_PROPS_CALL_LEAF_RE,
    _extract_destructured_props,
    _locate_handler_body,
    _slice_function_body,
)
from .legacy_react_api_scanner import _extract_proptypes_names

try:
    from .screen_spec.extractors import (
        _CLEAR_KV_RE,
        _SETSTATE_BODY_RE,
        _extract_handler_leaf,
    )
except Exception:  # pragma: no cover — 단위 테스트 환경에서 tree-sitter 없을 때
    _SETSTATE_BODY_RE = re.compile(
        r"\b(?:this\s*\.\s*)?setState\s*\(\s*\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}",
        re.DOTALL,
    )
    _CLEAR_KV_RE = re.compile(
        r"""(\w+)\s*:\s*(?:undefined|null|''|""|``|false|\[\s*\]|\{\s*\})""",
        re.VERBOSE,
    )
    _extract_handler_leaf = None


# ─────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────


def build_trigger_bundle(
    trigger: dict,
    file_content: str,
    rel_path: str,
    *,
    fn_index: Optional[Dict[str, List[tuple]]] = None,
    mdtp_map: Optional[Dict[str, set]] = None,
    action_to_type: Optional[Dict[str, set]] = None,
    saga_urls_by_type: Optional[Dict[str, set]] = None,
    max_chain_depth: int = 3,
    max_body_chars: int = 4000,
) -> dict:
    """trigger 한 건 → bundle dict.

    Parameters
    ----------
    trigger : dict
        ``collect_event_handlers()`` 의 한 entry. 필수 키:
        ``event`` / ``handler`` / ``label`` / ``source_offset`` (+ ``body``).
    file_content : str
        trigger 가 정의된 파일의 전체 텍스트 (이미 ``_strip_comments``
        됐다고 가정).
    rel_path : str
        파일 상대 경로 (사용자 표시용).
    fn_index, mdtp_map, action_to_type, saga_urls_by_type
        scanner 가 만든 글로벌 인덱스. None 이면 chain follow 미작동.
    """
    handler_name = (trigger.get("handler") or "").strip()
    event_type = (trigger.get("event") or "").strip()
    label = (trigger.get("label") or "").strip()
    offset = int(trigger.get("source_offset", -1))
    inline_body = (trigger.get("body") or "").strip()

    bundle: dict = {
        "trigger_jsx": _slice_trigger_jsx(file_content, offset),
        "event_type": event_type,
        "handler_name": handler_name,
        "label": label,
        "source_file": rel_path,
        "handler_chain": [],
        "setstate_writes": [],
        "factual_urls": [],
    }

    # 1) handler body 확보
    handler_body = ""
    if inline_body:
        handler_body = inline_body
    elif handler_name:
        handler_body = _locate_handler_body(file_content, handler_name) or ""
        if not handler_body and fn_index:
            # 다른 파일 (helper / saga import) 일 수 있음 — 단 가장 흔한 케이스는
            # 같은 파일이므로 미발견 시 chain 일부만 분석.
            for _fp, body in (fn_index.get(handler_name) or [])[:1]:
                handler_body = body
                break
    if handler_body:
        bundle["handler_chain"].append({
            "name": handler_name or "<inline>",
            "kind": "handler",
            "file": rel_path,
            "body": _truncate(handler_body, max_body_chars),
        })

    # 2) handler body 안 setState clear 추출 (parser 가 잡는 facts)
    bundle["setstate_writes"] = _extract_setstate_writes(handler_body)

    # 3) handler body 안 helper 함수 호출 → 같은 파일 / fn_index 에서 body
    # 따라가기 (depth 제한, cycle 방지)
    if handler_body and max_chain_depth > 0:
        seen = {handler_name} if handler_name else set()
        for entry in _follow_local_helpers(
            handler_body, file_content, fn_index, seen,
            depth=max_chain_depth, max_body_chars=max_body_chars,
            current_file=rel_path,
        ):
            bundle["handler_chain"].append(entry)

    # 4) Redux/saga chain — handler body 의 dispatch / this.props.X 가
    # 가리키는 action body + 그 action 의 type 을 listen 하는 saga body
    if handler_body and action_to_type is not None and saga_urls_by_type is not None:
        for entry in _follow_action_saga_chain(
            handler_body, file_content, fn_index,
            mdtp_map=mdtp_map,
            action_to_type=action_to_type,
            saga_urls_by_type=saga_urls_by_type,
            max_body_chars=max_body_chars,
        ):
            bundle["handler_chain"].append(entry)

    # 5) factual URLs — scanner 의 chain resolver 가 이미 매칭한 URL 들
    # (LLM 환각 방지용 ground truth)
    bundle["factual_urls"] = _collect_factual_urls(
        handler_body, file_content, fn_index,
        mdtp_map=mdtp_map,
        action_to_type=action_to_type,
        saga_urls_by_type=saga_urls_by_type,
    )

    return bundle


def serialize_bundle_for_llm(bundle: dict) -> str:
    """Bundle → LLM user-message body markdown."""
    parts: list[str] = []
    parts.append(f"# Trigger: {bundle.get('label') or '(no label)'}  "
                 f"`{bundle.get('event_type')}` → `{bundle.get('handler_name')}`")
    parts.append(f"_파일_: `{bundle.get('source_file')}`")
    parts.append("")

    jsx = bundle.get("trigger_jsx") or ""
    if jsx:
        parts.append("## JSX (trigger element)")
        parts.append("```jsx")
        parts.append(jsx)
        parts.append("```")
        parts.append("")

    chain = bundle.get("handler_chain") or []
    if chain:
        parts.append("## Handler chain")
        for entry in chain:
            kind = entry.get("kind", "fn")
            name = entry.get("name", "")
            file_ = entry.get("file") or ""
            extra = ""
            if entry.get("type_key"):
                extra = f" — type=`{entry['type_key']}`"
            parts.append(f"### [{kind}] `{name}` ({file_}){extra}")
            body = entry.get("body") or ""
            if body:
                parts.append("```js")
                parts.append(body)
                parts.append("```")
        parts.append("")

    writes = bundle.get("setstate_writes") or []
    if writes:
        parts.append("## setState writes (factual)")
        for w in writes:
            parts.append(f"- {w}")
        parts.append("")

    urls = bundle.get("factual_urls") or []
    if urls:
        parts.append("## Backend URLs (factual, parser-extracted)")
        for u in urls:
            parts.append(f"- `{u}`")
        parts.append("")

    return "\n".join(parts)


def bundle_cache_key(bundle: dict) -> str:
    """캐시 키 — bundle 내용 변하지 않으면 같은 키 (LLM 응답 재사용)."""
    payload = serialize_bundle_for_llm(bundle)
    return hashlib.md5(payload.encode("utf-8")).hexdigest()


# ─────────────────────────────────────────────────────────────────
# 내부 헬퍼
# ─────────────────────────────────────────────────────────────────


def _truncate(text: str, max_chars: int) -> str:
    if not text or len(text) <= max_chars:
        return text or ""
    return text[:max_chars] + f"\n// ... ({len(text) - max_chars} chars truncated)"


def _slice_trigger_jsx(content: str, offset: int, max_chars: int = 800) -> str:
    """source_offset 위치의 JSX element 한 줄 영역 추출.

    가장 가까운 ``<`` 부터 짝 ``>`` (jsx 닫는 ``>`` — children 있는 경우
    closing tag 까지 따라가지 않고 opening element 만) 까지. 라벨이
    children 텍스트인 경우를 위해 다음 ``</`` 직전까지 같이 포함.
    """
    if offset < 0:
        return ""
    n = len(content)
    # 뒤로 이동해서 opening ``<`` 찾기
    start = content.rfind("<", max(0, offset - 600), offset + 1)
    if start < 0:
        return ""
    # ``>`` 찾기 — JSX expression 안 ``>`` 회피 위해 brace counting
    depth = 0
    i = start
    end = -1
    while i < n:
        c = content[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        elif c == ">" and depth == 0:
            end = i + 1
            break
        i += 1
    if end < 0:
        return ""
    # self-closing (``<Select ... />``) 면 closing tag 안 찾음 — outer 의
    # ``</div>`` 가 잡혀 들어오는 버그 방지.
    is_self_closing = content[end - 2:end] == "/>"
    if not is_self_closing:
        # children 있는 element — closing tag 까지 포함 (라벨 children 표시).
        after = content[end:end + 200]
        close_m = re.search(r"</[A-Za-z][\w$.]*\s*>", after)
        if close_m:
            end += close_m.end()
    snippet = content[start:end]
    if len(snippet) > max_chars:
        snippet = snippet[:max_chars] + "..."
    return snippet


def _extract_setstate_writes(body: str) -> list[str]:
    """body 안 ``setState({key: value, ...})`` 의 key=value 추출 (factual)."""
    if not body:
        return []
    out: list[str] = []
    for m in _SETSTATE_BODY_RE.finditer(body):
        inner = m.group(1)
        # 단순 ``key: value`` 추출 — value 가 무엇인지만 사람이 읽도록
        for line in re.finditer(r"(\w+)\s*:\s*([^,\n]+?)(?:,|$)", inner):
            key = line.group(1).strip()
            val = line.group(2).strip().rstrip(",")
            if key and not key.startswith("_"):
                out.append(f"{key}={val}")
    return out


def _follow_local_helpers(
    body: str,
    file_content: str,
    fn_index: Optional[dict],
    seen: set,
    *,
    depth: int,
    max_body_chars: int,
    current_file: str,
) -> list[dict]:
    """body 안 fn 호출 leaf → 같은 파일 → fn_index 순으로 body 찾아 chain."""
    if depth <= 0:
        return []
    found: list[dict] = []
    for m in _FN_CALL_LEAF_RE.finditer(body):
        fn = (m.group("fn") or "").strip()
        if not fn or fn in seen or fn in _RESERVED_NEAR_BLOCK:
            continue
        # 같은 파일 우선
        hb = _locate_handler_body(file_content, fn)
        file_ = current_file
        if not hb and fn_index:
            for _fp, sub_body in fn_index.get(fn) or []:
                hb = sub_body
                file_ = _fp
                break
        if not hb:
            continue
        seen.add(fn)
        found.append({
            "name": fn,
            "kind": "helper",
            "file": file_,
            "body": _truncate(hb, max_body_chars),
        })
        # 재귀로 깊이 follow — depth 제한
        found.extend(_follow_local_helpers(
            hb, file_content, fn_index, seen,
            depth=depth - 1, max_body_chars=max_body_chars,
            current_file=file_,
        ))
    return found


def _follow_action_saga_chain(
    handler_body: str,
    file_content: str,
    fn_index: Optional[dict],
    *,
    mdtp_map: Optional[dict],
    action_to_type: Optional[dict],
    saga_urls_by_type: Optional[dict],
    max_body_chars: int,
) -> list[dict]:
    """Redux/saga chain — handler body 의 dispatch / this.props.X →
    action body → saga body. 사용자 코드 패턴 그대로.

    action_to_type 매핑 (각 action 의 모든 type 후보 set), saga_urls_by_type
    매핑 (각 type 의 URL set) 은 scanner 가 이미 만들어서 넘김.
    """
    if not handler_body:
        return []
    out: list[dict] = []
    seen_actions: set = set()

    # (a) 직접 ``dispatch(actions.X())`` 호출
    candidate_actions: set = set()
    for m in _DISPATCH_ACTION_RE.finditer(handler_body):
        candidate_actions.add(m.group("act"))

    # (b) handler body 의 this.props.X / destructured / propTypes 호출
    # → mDTP 에서 X key 의 actions.Y
    props_keys: set = set()
    for m in _THIS_PROPS_CALL_LEAF_RE.finditer(handler_body):
        props_keys.add(m.group(1))
    prop_candidates: set = set()
    try:
        prop_candidates |= _extract_destructured_props(file_content)
        prop_candidates |= _extract_proptypes_names(file_content)
    except Exception:
        pass
    if prop_candidates:
        for m in _FN_CALL_LEAF_RE.finditer(handler_body):
            fn = (m.group("fn") or "").strip()
            if fn in prop_candidates:
                props_keys.add(fn)
    if props_keys:
        for m in _MDTP_KV_RE.finditer(file_content):
            if m.group("key") in props_keys:
                candidate_actions.add(m.group("act"))
        if mdtp_map:
            for k in props_keys:
                candidate_actions |= mdtp_map.get(k, set())

    # action body 찾아서 chain 에 추가
    for act in sorted(candidate_actions):
        if act in seen_actions or not fn_index:
            continue
        seen_actions.add(act)
        for fp, body in fn_index.get(act) or []:
            type_keys = []
            if action_to_type:
                type_keys = sorted(action_to_type.get(act) or set())
            out.append({
                "name": act,
                "kind": "action",
                "file": fp,
                "body": _truncate(body, max_body_chars),
                "type_key": type_keys[0] if type_keys else "",
            })
            # 첫 매칭만 — 같은 이름 collision 은 드물고, 너무 늘리지 않기 위해.
            break

        # 해당 action 의 type → saga URL 매핑된 saga 함수 body 도 같이.
        if action_to_type and saga_urls_by_type:
            for tk in action_to_type.get(act) or set():
                if tk in saga_urls_by_type:
                    # saga body 찾기 — saga_urls_by_type 은 URL 만 있어서
                    # body 자체는 fn_index 에서 takeLatest 의 saga_fn 이름으로
                    # 재탐색해야. 여기선 단순화 — type_key 정보만 제공.
                    pass

    return out


def _collect_factual_urls(
    handler_body: str,
    file_content: str,
    fn_index: Optional[dict],
    *,
    mdtp_map: Optional[dict],
    action_to_type: Optional[dict],
    saga_urls_by_type: Optional[dict],
) -> list[str]:
    """parser chain resolver 의 URL 결과를 그대로 사용 — LLM 환각 방지용."""
    if not handler_body or action_to_type is None or saga_urls_by_type is None:
        return []
    try:
        from .legacy_react_api_scanner import _resolve_saga_urls_for_handler
    except Exception:
        return []
    try:
        urls = _resolve_saga_urls_for_handler(
            handler_body, file_content,
            action_to_type, saga_urls_by_type,
            fn_index=fn_index, mdtp_map=mdtp_map, depth=3,
        )
    except Exception:
        return []
    return sorted(urls or set())
