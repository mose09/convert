"""Button onClick handler body → ordered FlowStep list.

LLM 0, AST traversal 만으로 handler 함수의 본체를 훑어
- API 호출 (axios.get/post 등)
- 화면 호출 (navigate, window.open, router.push, Link to)
- 팝업 (legacy_react_closure 의 popup_refs 와 같은 패턴)
- if/else 조건 (직전 조건 텍스트만 첨부)

순서는 AST 트리 순회 순서 = 코드 출현 순서. 같은 소스 → 같은 결과.
"""
from __future__ import annotations

import re
from typing import Any

from ..legacy_react_ast import child_by_field, find_by_type, text_of, walk
from .models import FlowStep


# API 호출 패턴: axios/apiClient/http/fetch 등 + method (get/post/...)
_API_METHOD_RE = re.compile(
    r"^([A-Za-z_$][\w$]*?)(?:\.\w+)*\.(get|post|put|delete|patch|request)$",
    re.IGNORECASE,
)
_FETCH_RE = re.compile(r"^fetch$", re.IGNORECASE)

_NAV_FUNCS = (
    "navigate", "push", "replace",
    "history.push", "history.replace",
    "router.push", "router.replace",
    "window.open", "location.assign", "location.replace",
)


def _find_function_body(name: str, tree, source: bytes):
    """파일 내에서 ``name`` 으로 정의된 함수의 body 노드 반환."""
    target = name.split(".")[0]  # obj.method → method 가 아니라 obj 변수 등은 skip
    for n in walk(tree.root_node):
        # function declaration
        if n.type == "function_declaration":
            nm = child_by_field(n, "name")
            if nm and text_of(nm, source).strip() == target:
                return child_by_field(n, "body")
        # arrow function: const NAME = (...) => { ... } / (...) => expr
        if n.type == "variable_declarator":
            nm = child_by_field(n, "name")
            if nm and text_of(nm, source).strip() == target:
                val = child_by_field(n, "value")
                if val is None:
                    continue
                if val.type == "arrow_function":
                    return child_by_field(val, "body") or val
                if val.type == "function":
                    return child_by_field(val, "body")
        # class method: methodName(...) { ... }
        if n.type == "method_definition":
            nm = child_by_field(n, "name")
            if nm and text_of(nm, source).strip() == target:
                return child_by_field(n, "body")
    return None


def _extract_call_target(call_node, source: bytes) -> str:
    """call_expression → 호출 대상 텍스트 (e.g., 'axios.post')."""
    func = child_by_field(call_node, "function")
    return text_of(func, source).strip() if func else ""


def _extract_first_string_arg(call_node, source: bytes) -> str:
    """첫 인자가 string literal 이면 그 텍스트 (따옴표 제거), 아니면 ''."""
    args = child_by_field(call_node, "arguments")
    if args is None:
        return ""
    for c in args.children:
        if c.type == "string":
            return text_of(c, source).strip("'\"`")
        if c.type in ("template_string",):
            return text_of(c, source).strip("`")
        if c.type in ("(", ",", ")"):
            continue
        # 첫 비-구두점 자식만 본다
        return ""
    return ""


def _classify_call(target: str) -> str | None:
    """호출 대상 → 'api' / 'navigate' / None."""
    if not target:
        return None
    if target in _NAV_FUNCS:
        return "navigate"
    # 메서드 호출 형태: history.push / router.replace / window.open / location.assign
    nav_hints = ("history", "router", "navigate", "window", "location")
    if any(h in target for h in nav_hints):
        if target.endswith((".push", ".replace", ".open",
                            ".assign", ".navigate")):
            return "navigate"
    if _API_METHOD_RE.match(target):
        return "api"
    if _FETCH_RE.match(target):
        return "api"
    return None


def _api_detail(call_node, source: bytes) -> str:
    """API 호출 → 'POST /api/...' 형태 detail. 동적 URL 이면 '(dynamic)'."""
    target = _extract_call_target(call_node, source)
    m = _API_METHOD_RE.match(target)
    method = m.group(2).upper() if m else (
        "FETCH" if _FETCH_RE.match(target) else "CALL")
    url = _extract_first_string_arg(call_node, source) or "(dynamic)"
    return f"{method} {url}"


def _nav_detail(call_node, source: bytes) -> str:
    target = _extract_call_target(call_node, source)
    url = _extract_first_string_arg(call_node, source) or "(dynamic)"
    return f"{target} → {url}" if url != "(dynamic)" else f"{target}(...)"


def _condition_text_for(call_node, source: bytes) -> str:
    """call 노드를 감싸는 가장 가까운 if/else_clause/conditional 의 condition 텍스트."""
    cur = call_node.parent
    while cur is not None:
        if cur.type == "if_statement":
            cond = child_by_field(cur, "condition")
            if cond is not None:
                return text_of(cond, source).strip().strip("()").strip()
            return "if(?)"
        if cur.type == "else_clause":
            return "else"
        if cur.type == "ternary_expression":
            cond = child_by_field(cur, "condition")
            if cond is not None:
                return text_of(cond, source).strip()
        cur = cur.parent
    return ""


def trace_button_flow(handler_name: str, tree, source: bytes
                      ) -> list[FlowStep]:
    """handler_name 함수 본체에서 API/navigate call 만 코드 출현 순서로."""
    body = _find_function_body(handler_name, tree, source)
    return trace_flow_in_node(body, source)


def trace_flow_in_node(node, source: bytes) -> list[FlowStep]:
    """주어진 AST 서브트리 안의 API/navigate call 을 출현 순서로.

    `node` 가 함수 body 든 inline arrow body 든 call_expression 자체든
    공통으로 사용. None 이면 빈 리스트.
    """
    if node is None:
        return []
    out: list[FlowStep] = []
    step = 0
    # call_expression 자체가 전달된 경우 (`onClick={api.post(...)}`) 도 처리
    candidates = list(find_by_type(node, "call_expression"))
    if node.type == "call_expression" and node not in candidates:
        candidates = [node] + candidates
    for call in candidates:
        target = _extract_call_target(call, source)
        kind = _classify_call(target)
        if kind is None:
            continue
        step += 1
        if kind == "api":
            detail = _api_detail(call, source)
        else:
            detail = _nav_detail(call, source)
        out.append(FlowStep(
            step=step,
            action=kind,
            detail=detail,
            condition=_condition_text_for(call, source),
        ))
    return out
