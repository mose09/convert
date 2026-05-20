"""AST-based deterministic extractors for screen UI spec.

각 extractor 는 ScreenClosure 의 모든 파일을 입력으로 받아 패턴 매치.
LLM 0, 같은 소스 → 같은 결과.

기존 자산 재사용:
 - legacy_react_closure.build_closure   (entry + import BFS)
 - legacy_react_ast.walk / find_by_type / text_of / child_by_field
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Iterator

from ..legacy_react_ast import (
    child_by_field,
    find_by_type,
    text_of,
    walk,
)
from ..legacy_react_closure import ScreenClosure, build_closure
from .models import (
    ButtonEvent,
    FormField,
    GridColumn,
    ScreenSpec,
    Tab,
    ValidationRule,
)
from .flow_tracer import trace_button_flow

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────
# 기본 패턴 — patterns.yaml.react.screen_spec 슬롯으로 덮어쓰기 가능
# ─────────────────────────────────────────────────────────────────

# 검색 패널 = 폼 컨테이너 (search/filter/criteria 등 이름) 가 보통이지만,
# 확실한 단서가 없으므로 input 자체를 패널 안팎 가리지 않고 모두 수집.
DEFAULT_INPUT_COMPONENTS = (
    "input", "TextField", "TextInput", "Input", "Select", "Dropdown",
    "DatePicker", "RangePicker", "Checkbox", "Radio", "RadioGroup",
    "Switch", "NumberInput", "InputNumber", "TimePicker", "Combobox",
)

DEFAULT_TABLE_COMPONENTS = (
    "Table", "DataTable", "Grid", "DataGrid", "AgGridReact", "MaterialTable",
)

DEFAULT_TAB_COMPONENTS = ("Tabs", "TabList")
DEFAULT_TAB_ITEM_COMPONENTS = ("Tab", "TabPanel", "TabPane")

DEFAULT_BUTTON_COMPONENTS = ("button", "Button", "IconButton", "LinkButton")

DEFAULT_NAV_FUNCS = (
    "navigate", "push", "replace", "router.push", "history.push",
    "window.open", "location.assign", "location.replace",
)

# yup/zod/joi 등의 schema 패턴
_VALIDATION_LIBS = ("yup", "zod", "joi")


# ─────────────────────────────────────────────────────────────────
# 공통 헬퍼
# ─────────────────────────────────────────────────────────────────

def _patterns_section(patterns: dict | None) -> dict:
    """patterns.yaml 의 react.screen_spec 섹션 (없으면 빈 dict)."""
    if not isinstance(patterns, dict):
        return {}
    return ((patterns.get("react") or {}).get("screen_spec") or {})


def _comp_list(patterns: dict, key: str, default: tuple[str, ...]) -> set[str]:
    extra = patterns.get(key) or []
    if not isinstance(extra, (list, tuple)):
        extra = []
    return set(default) | {str(x) for x in extra}


def _jsx_open_elements(tree, source: bytes) -> Iterator[tuple[Any, str]]:
    """모든 JSX 열림 element (self-closing 포함) 와 tag 이름을 yield."""
    for n in find_by_type(tree.root_node,
                          {"jsx_opening_element", "jsx_self_closing_element"}):
        name_node = child_by_field(n, "name")
        if name_node is None:
            continue
        yield n, text_of(name_node, source).strip()


def _jsx_attributes(element_node, source: bytes) -> dict[str, str]:
    """JSX 노드의 attribute 들 → {name: literal_value_or_expr_text}.

    값이 string literal 이면 따옴표 제거된 텍스트, expression 이면 원본 텍스트.
    boolean prop ('required' 처럼 값 없는 것) → "true".

    tree-sitter-javascript 의 jsx_attribute 는 field name 이 없으므로
    named_children 인덱스 기반으로 처리: [0]=name, [1] (있으면)=value.
    """
    attrs: dict[str, str] = {}
    for attr in element_node.children:
        if attr.type != "jsx_attribute":
            continue
        nc = attr.named_children
        if not nc:
            continue
        attr_name = text_of(nc[0], source).strip()
        if not attr_name:
            continue
        if len(nc) < 2:
            attrs[attr_name] = "true"   # boolean prop
            continue
        val_node = nc[1]
        if val_node.type == "string":
            attrs[attr_name] = text_of(val_node, source).strip().strip("'\"`")
        elif val_node.type == "jsx_expression":
            inner = text_of(val_node, source).strip()
            if inner.startswith("{") and inner.endswith("}"):
                inner = inner[1:-1].strip()
            attrs[attr_name] = inner
        else:
            attrs[attr_name] = text_of(val_node, source).strip()
    return attrs


def _is_literal_string(s: str) -> bool:
    """단순 quoted literal 이면 그 안 텍스트만 반환에 쓰기 위한 판정."""
    if len(s) >= 2 and s[0] in "'\"`" and s[-1] == s[0]:
        return True
    return False


def _strip_quotes(s: str) -> str:
    if _is_literal_string(s):
        return s[1:-1]
    return s


def _first_text_child(node, source: bytes) -> str:
    """JSX element 의 텍스트 자식 (예: <button>조회</button> 의 '조회')."""
    if node is None:
        return ""
    # parent: jsx_element wraps opening + text/children + closing
    parent = node.parent
    if parent and parent.type == "jsx_element":
        for c in parent.children:
            if c.type == "jsx_text":
                txt = text_of(c, source).strip()
                if txt:
                    return txt
            elif c.type == "jsx_expression":
                # {label} 같은 케이스 — 원본 식 그대로
                t = text_of(c, source).strip()
                if t.startswith("{") and t.endswith("}"):
                    t = t[1:-1].strip()
                if t:
                    return t
    return ""


def _resolve_identifier_literal(name: str, tree, source: bytes
                                ) -> Any | None:
    """ ``const NAME = [...]`` 또는 ``const NAME = {...}`` 처럼 정의된
    식별자의 RHS 원본 텍스트 반환 (간단한 1-pass 해석)."""
    for decl in find_by_type(tree.root_node,
                             {"variable_declarator", "lexical_declaration"}):
        # variable_declarator: name = value
        nm = child_by_field(decl, "name") if decl.type == "variable_declarator" else None
        if nm and text_of(nm, source).strip() == name:
            val = child_by_field(decl, "value")
            if val is not None:
                return val
    return None


# ─────────────────────────────────────────────────────────────────
# 1. 검색 필드 (FormField)
# ─────────────────────────────────────────────────────────────────

def _classify_field_type(tag: str, attrs: dict[str, str]) -> str:
    """JSX tag + type attr → 필드 타입 라벨."""
    tag_l = tag.lower()
    if "date" in tag_l:
        return "date" if "range" not in tag_l else "daterange"
    if "time" in tag_l:
        return "time"
    if "checkbox" in tag_l:
        return "checkbox"
    if "radio" in tag_l:
        return "radio"
    if "select" in tag_l or "dropdown" in tag_l or "combobox" in tag_l:
        return "select"
    if "number" in tag_l:
        return "number"
    # <input type="..."> case
    t = (attrs.get("type") or "text").strip().strip("'\"")
    return t


def _format_inline_validation(attrs: dict[str, str]) -> str:
    """JSX prop 중 검증 관련만 골라 '키=값; ...' 형식."""
    parts = []
    for k in ("required", "pattern", "minLength", "maxLength",
              "min", "max", "step"):
        if k in attrs:
            v = attrs[k]
            if k == "required" and v == "true":
                parts.append("required")
            else:
                parts.append(f"{k}={v}")
    return "; ".join(parts)


def _find_label_text_in_children(parent, exclude, source: bytes) -> str:
    """parent 의 자식 element 중 ``className`` 에 'label' 포함된 element 의
    text child 반환 (exclude 노드는 제외).
    """
    for child in parent.children:
        if child is exclude:
            continue
        if child.type not in ("jsx_element", "jsx_self_closing_element"):
            continue
        if child.type == "jsx_self_closing_element":
            open_el = child
        else:
            open_el = next((c for c in child.children
                            if c.type == "jsx_opening_element"), None)
        if open_el is None:
            continue
        sib_attrs = _jsx_attributes(open_el, source)
        cls = (sib_attrs.get("className") or "").lower()
        if "label" not in cls:
            continue
        if child.type != "jsx_element":
            continue
        for c in child.children:
            if c.type == "jsx_text":
                txt = text_of(c, source).strip()
                if txt:
                    return txt
    return ""


def _sibling_label(input_node, source: bytes) -> str:
    """한국 SI 흔한 패턴: input 컴포넌트의 형제 (또는 가까운 ancestor 의
    자식) 중 ``className`` 에 'label' 포함된 element 의 text child 를
    라벨로. props 의 label/placeholder/title 가 모두 비어있을 때만 사용.

    예::

        <div className="search-item">
          <span className="search-label">FAB</span>     <- 라벨
          <span className="search-select">
            <Select .../>                                <- input 컴포넌트
          </span>
        </div>

    1-2 단계 ancestor 까지만 탐색 (너무 깊이 가면 다른 input 의 라벨을
    오인할 위험).
    """
    cur = input_node
    if cur.type == "jsx_opening_element" and cur.parent is not None:
        cur = cur.parent
    for _ in range(2):
        parent = cur.parent
        if parent is None or parent.type not in ("jsx_element", "jsx_fragment"):
            break
        text = _find_label_text_in_children(parent, exclude=cur, source=source)
        if text:
            return text
        cur = parent
    return ""


def extract_form_fields(closure: ScreenClosure,
                        patterns: dict | None = None) -> list[FormField]:
    """모든 closure 파일을 훑어 입력 컴포넌트 → FormField 리스트."""
    pat = _patterns_section(patterns)
    input_comps = _comp_list(pat, "input_components", DEFAULT_INPUT_COMPONENTS)

    fields: list[FormField] = []
    order = 0
    for f in closure.files:
        # parse_file 결과를 재사용하지 않고 closure 의 content 기반 재파싱은
        # 비효율 → build_closure 안에서 모은 source 를 다시 파싱하는 대신,
        # 화면 단위로 다시 parse_file 호출. closure.entry_file/files[i].abs_path
        # 가 실제 디스크 경로이므로 그걸로 다시 파싱.
        from ..legacy_react_ast import parse_file
        tree, source, _ = parse_file(f.abs_path)
        if tree is None:
            continue
        for el, tag in _jsx_open_elements(tree, source):
            if tag not in input_comps:
                continue
            attrs = _jsx_attributes(el, source)
            label = (attrs.get("label")
                     or attrs.get("placeholder")
                     or attrs.get("title")
                     or attrs.get("aria-label")
                     or _sibling_label(el, source)
                     or "")
            name = (attrs.get("name")
                    or attrs.get("id")
                    or attrs.get("field")
                    or "")
            order += 1
            fields.append(FormField(
                order=order,
                label=label,
                name=name,
                field_type=_classify_field_type(tag, attrs),
                required=attrs.get("required") == "true",
                default=(attrs.get("defaultValue")
                         or attrs.get("value") or ""),
                validation=_format_inline_validation(attrs),
                source_file=f.rel_path,
            ))
    return fields


# ─────────────────────────────────────────────────────────────────
# 2. 그리드 컬럼 (GridColumn)
# ─────────────────────────────────────────────────────────────────

# 컬럼 객체에서 후보 키들 (lib 별로 이름이 달라 union 으로 매치)
_COL_HEADER_KEYS = ("header", "title", "label", "headerName", "text", "headerText")
_COL_DATA_KEYS = ("dataIndex", "field", "accessor", "key", "name", "dataField")
_COL_WIDTH_KEYS = ("width", "minWidth", "maxWidth", "flex")
_COL_HIDDEN_KEYS = ("hidden", "visible", "show", "display")
_COL_TYPE_KEYS = ("type", "dataType", "renderType")
_COL_SORT_KEYS = ("sorter", "sortable", "sort", "allowSort")


def _parse_object_literal(node, source: bytes) -> dict[str, str]:
    """object literal AST → {key: value_text}. value 가 string/number/bool/identifier 인 경우만."""
    out: dict[str, str] = {}
    if node is None:
        return out
    for child in walk(node):
        if child.type != "pair":
            continue
        key_node = child_by_field(child, "key")
        val_node = child_by_field(child, "value")
        if key_node is None or val_node is None:
            continue
        key = text_of(key_node, source).strip().strip("'\"`")
        val = text_of(val_node, source).strip()
        out[key] = val
        # 1 depth 로 충분 — nested object 는 별도 처리 안 함
    return out


def _resolve_array_of_objects(value_node, tree, source: bytes
                              ) -> list[Any]:
    """array literal 노드 OR identifier 이면 그 const 의 RHS 를 다시 해석."""
    if value_node is None:
        return []
    if value_node.type == "array":
        return [c for c in value_node.children if c.type == "object"]
    # identifier → const 해석 (한 파일 내)
    if value_node.type == "identifier":
        ident = text_of(value_node, source).strip()
        resolved = _resolve_identifier_literal(ident, tree, source)
        if resolved is not None and resolved.type == "array":
            return [c for c in resolved.children if c.type == "object"]
    return []


def _member_chain(node, source: bytes) -> list[str]:
    """member_expression chain → ``['this', 'state', 'columnDefs']``.

    예: ``this.state.columnDefs`` / ``state.columnDefs`` /
    ``this.props.cols`` 모두 leaf 부터 root 까지 식별자 chain 으로 변환.
    """
    parts: list[str] = []
    cur = node
    while cur is not None and cur.type == "member_expression":
        prop = child_by_field(cur, "property")
        if prop is None:
            break
        parts.append(text_of(prop, source).strip())
        cur = child_by_field(cur, "object")
    if cur is not None:
        parts.append(text_of(cur, source).strip())
    parts.reverse()
    return parts


def _resolve_class_state_key(key_name: str, tree, source: bytes):
    """class 안 ``state = {...}`` 또는 ``constructor`` 의 ``this.state = {...}``
    안에서 ``key_name`` 키의 RHS 노드 반환. 못 찾으면 None.

    React class component 의 흔한 패턴::

        class Screen extends React.Component {
          state = { columnDefs: [...] };          ← class field
          // 또는
          constructor(props) {
            super(props);
            this.state = { columnDefs: [...] };   ← constructor assignment
          }
        }
    """
    for cls in find_by_type(tree.root_node, "class_declaration"):
        body = next((c for c in cls.children if c.type == "class_body"), None)
        if body is None:
            continue
        for member in body.children:
            # 1) class field: state = {...} 또는 public state: State = {...}
            if member.type in ("field_definition", "public_field_definition",
                               "class_property"):
                nm = child_by_field(member, "name") or child_by_field(member, "property")
                if nm is None:
                    continue
                if text_of(nm, source).strip() != "state":
                    continue
                val = child_by_field(member, "value")
                hit = _object_pair_value(val, key_name, source)
                if hit is not None:
                    return hit
            # 2) method: constructor() { this.state = {...} }
            if member.type == "method_definition":
                nm = child_by_field(member, "name")
                if nm is None or text_of(nm, source).strip() != "constructor":
                    continue
                for asn in find_by_type(member, "assignment_expression"):
                    left = child_by_field(asn, "left")
                    if left is None or left.type != "member_expression":
                        continue
                    if _member_chain(left, source)[-2:] != ["this", "state"]:
                        continue
                    right = child_by_field(asn, "right")
                    hit = _object_pair_value(right, key_name, source)
                    if hit is not None:
                        return hit
    return None


def _object_pair_value(obj_node, key_name: str, source: bytes):
    """object literal 안 ``key_name: value`` 의 value 노드 반환. 못 찾으면 None."""
    if obj_node is None or obj_node.type != "object":
        return None
    for pair in obj_node.children:
        if pair.type != "pair":
            continue
        k = child_by_field(pair, "key")
        if k is None:
            continue
        kn = text_of(k, source).strip().strip("'\"`")
        if kn == key_name:
            return child_by_field(pair, "value")
    return None


def _resolve_array_in_closure(value_node, tree, source: bytes,
                              closure: ScreenClosure, current_abs_path
                              ) -> list[tuple[Any, bytes]]:
    """``columns={X}`` 의 X 가 다른 파일에서 import 한 const 일 때, closure
    전체를 훑어 ``[export ]const X = [...]`` 정의를 찾아 array 반환.

    또한 ``columnDefs={this.state.columnDefs}`` 처럼 React class state 에서
    동적 할당된 케이스도 같은 파일 class 안에서 ``state = { columnDefs: [...] }``
    또는 ``constructor`` 의 ``this.state = {...}`` 를 찾아 해석.

    Returns list of ``(object_node, source_bytes)`` pairs — object 노드와
    그 노드가 속한 파일의 source bytes (downstream parse 에 필요).
    same-file resolution 이 성공하면 그 결과만 반환, 실패 시 closure
    전체 fallback. closure 가 None 이면 same-file 만 시도.
    """
    if value_node is None:
        return []
    if value_node.type == "array":
        return [(c, source) for c in value_node.children if c.type == "object"]
    # member_expression: this.state.X / state.X — class state 에서 해석
    if value_node.type == "member_expression":
        chain = _member_chain(value_node, source)
        # this.state.X / state.X 둘 다 지원
        if len(chain) >= 2 and chain[-2] == "state":
            key = chain[-1]
            resolved = _resolve_class_state_key(key, tree, source)
            if resolved is not None and resolved.type == "array":
                return [(c, source) for c in resolved.children if c.type == "object"]
        return []
    if value_node.type != "identifier":
        return []
    ident = text_of(value_node, source).strip()
    resolved = _resolve_identifier_literal(ident, tree, source)
    if resolved is not None and resolved.type == "array":
        return [(c, source) for c in resolved.children if c.type == "object"]
    if closure is None:
        return []
    # closure-wide fallback — 다른 파일에 정의된 const
    from ..legacy_react_ast import parse_file
    for f in closure.files:
        if str(f.abs_path) == str(current_abs_path):
            continue
        tree_x, source_x, _ = parse_file(f.abs_path)
        if tree_x is None:
            continue
        resolved_x = _resolve_identifier_literal(ident, tree_x, source_x)
        if resolved_x is not None and resolved_x.type == "array":
            return [(c, source_x) for c in resolved_x.children if c.type == "object"]
    return []


def _truthy(s: str) -> bool:
    return s.strip().lower() in ("true", "yes", "1", "y")


def extract_grid_columns(closure: ScreenClosure,
                         patterns: dict | None = None) -> list[GridColumn]:
    """모든 closure 파일에서 Table/DataGrid 의 columns prop → GridColumn 리스트."""
    pat = _patterns_section(patterns)
    table_comps = _comp_list(pat, "table_components", DEFAULT_TABLE_COMPONENTS)

    cols: list[GridColumn] = []
    order = 0
    for f in closure.files:
        from ..legacy_react_ast import parse_file
        tree, source, _ = parse_file(f.abs_path)
        if tree is None:
            continue
        for el, tag in _jsx_open_elements(tree, source):
            if tag not in table_comps:
                continue
            attrs = _jsx_attributes(el, source)
            # ag-grid: columnDefs / antd, generic: columns / RealGrid 등: schema.
            # 라이브러리별 prop 이름 union.
            cols_expr = (attrs.get("columns") or attrs.get("columnDefs")
                         or attrs.get("schema") or "")
            if not cols_expr:
                # 컬럼 정의가 children (<TableColumn ...> 형태) 인 케이스 처리
                cols.extend(_extract_table_column_children(el, source, f.rel_path,
                                                          start_order=order))
                order = len(cols)
                continue
            # columns={SOME_CONST} → tree 에서 그 const 찾아 array of objects
            # _jsx_attributes 가 value 만 string 으로 줘서 노드 손실
            # → 직접 attribute AST 에서 expression 노드 재추출
            cols_value_node = (_find_attr_expression(el, "columns", source)
                               or _find_attr_expression(el, "columnDefs", source)
                               or _find_attr_expression(el, "schema", source))
            if cols_value_node is None:
                continue
            # same-file 식별자 해석이 실패하면 closure 전체 fallback —
            # ``columns={COLUMNS}`` 의 COLUMNS 가 별도 파일에서 import 된 const
            # 인 케이스 (사용자 실무 패턴) 도 해석.
            for obj, src in _resolve_array_in_closure(
                cols_value_node, tree, source, closure, f.abs_path
            ):
                d = _parse_object_literal(obj, src)
                order += 1
                cols.append(GridColumn(
                    order=order,
                    header=_strip_quotes(_first_present(d, _COL_HEADER_KEYS) or ""),
                    data_key=_strip_quotes(_first_present(d, _COL_DATA_KEYS) or ""),
                    data_type=_strip_quotes(_first_present(d, _COL_TYPE_KEYS) or ""),
                    width=_strip_quotes(_first_present(d, _COL_WIDTH_KEYS) or ""),
                    visible=_is_visible(d),
                    sortable=_truthy(_first_present(d, _COL_SORT_KEYS) or ""),
                    source_file=f.rel_path,
                ))
    return cols


def _find_attr_expression(element_node, attr_name: str, source: bytes):
    """JSX attribute 의 expression 안 실제 AST 표현식 노드 반환.

    예: ``columns={SOME_CONST}`` → identifier 노드 ``SOME_CONST`` 반환.
    못 찾으면 None.
    """
    for attr in element_node.children:
        if attr.type != "jsx_attribute":
            continue
        nc = attr.named_children
        if len(nc) < 2:
            continue
        if text_of(nc[0], source).strip() != attr_name:
            continue
        val_node = nc[1]
        if val_node.type != "jsx_expression":
            continue
        # `{ <expr> }` → 중괄호 제외한 첫 표현식 자식 (named child 첫 항목)
        for inner in val_node.named_children:
            return inner
    return None


def _first_present(d: dict[str, str], keys) -> str | None:
    for k in keys:
        if k in d:
            return d[k]
    return None


def _is_visible(d: dict[str, str]) -> bool:
    """hidden:true / visible:false / show:false / display:'none' → False, else True."""
    if "hidden" in d and _truthy(d["hidden"]):
        return False
    if "visible" in d and not _truthy(d["visible"]):
        return False
    if "show" in d and not _truthy(d["show"]):
        return False
    if "display" in d and "none" in d["display"].lower():
        return False
    return True


def _extract_table_column_children(table_el, source: bytes,
                                   rel_path: str, start_order: int
                                   ) -> list[GridColumn]:
    """<Table><TableColumn .../></Table> 형태에서 자식 컬럼 추출."""
    out: list[GridColumn] = []
    parent = table_el.parent
    if parent is None or parent.type != "jsx_element":
        return out
    order = start_order
    for child in parent.children:
        if child.type not in ("jsx_self_closing_element", "jsx_element"):
            continue
        open_el = (child if child.type == "jsx_self_closing_element"
                   else next((c for c in child.children
                              if c.type == "jsx_opening_element"), None))
        if open_el is None:
            continue
        name_node = child_by_field(open_el, "name")
        if name_node is None:
            continue
        tag = text_of(name_node, source).strip()
        if "Column" not in tag:
            continue
        attrs = _jsx_attributes(open_el, source)
        order += 1
        out.append(GridColumn(
            order=order,
            header=(attrs.get("header") or attrs.get("title") or ""),
            data_key=(attrs.get("dataIndex") or attrs.get("field")
                      or attrs.get("name") or ""),
            data_type=(attrs.get("type") or ""),
            width=(attrs.get("width") or ""),
            visible=(attrs.get("hidden") != "true"
                     and attrs.get("visible") != "false"),
            sortable=(attrs.get("sortable") == "true"
                      or attrs.get("sorter") == "true"),
            source_file=rel_path,
        ))
    return out


# ─────────────────────────────────────────────────────────────────
# 3. 탭 (Tab)
# ─────────────────────────────────────────────────────────────────

def extract_tabs(closure: ScreenClosure,
                 patterns: dict | None = None) -> list[Tab]:
    """<Tabs><Tab label=.../></Tabs> 또는 <TabList><Tab>...</Tab>... 형태."""
    pat = _patterns_section(patterns)
    tab_item_comps = _comp_list(pat, "tab_item_components",
                                DEFAULT_TAB_ITEM_COMPONENTS)

    tabs: list[Tab] = []
    order = 0
    for f in closure.files:
        from ..legacy_react_ast import parse_file
        tree, source, _ = parse_file(f.abs_path)
        if tree is None:
            continue
        for el, tag in _jsx_open_elements(tree, source):
            if tag not in tab_item_comps:
                continue
            attrs = _jsx_attributes(el, source)
            label = (attrs.get("label") or attrs.get("title")
                     or _first_text_child(el, source) or "")
            panel = (attrs.get("component") or attrs.get("panel") or "")
            order += 1
            tabs.append(Tab(
                order=order,
                label=label,
                panel_component=panel,
                source_file=f.rel_path,
            ))
    return tabs


# ─────────────────────────────────────────────────────────────────
# 4. 버튼 + 이벤트 (ButtonEvent)
# ─────────────────────────────────────────────────────────────────

def extract_buttons(closure: ScreenClosure,
                    patterns: dict | None = None) -> list[ButtonEvent]:
    pat = _patterns_section(patterns)
    btn_comps = _comp_list(pat, "button_components", DEFAULT_BUTTON_COMPONENTS)

    out: list[ButtonEvent] = []
    for f in closure.files:
        from ..legacy_react_ast import parse_file
        tree, source, _ = parse_file(f.abs_path)
        if tree is None:
            continue
        for el, tag in _jsx_open_elements(tree, source):
            if tag not in btn_comps:
                continue
            attrs = _jsx_attributes(el, source)
            label = (attrs.get("children") or attrs.get("label")
                     or attrs.get("title")
                     or _first_text_child(el, source) or "")
            # onClick 의 expression AST 노드를 직접 가져옴
            on_click_node = (_find_attr_expression(el, "onClick", source)
                             or _find_attr_expression(el, "onSubmit", source))
            handler_name, flow = _resolve_handler_flow(
                on_click_node, tree, source)
            api_calls = [s.detail for s in flow if s.action == "api"]
            nav_calls = [s.detail for s in flow if s.action == "navigate"]
            out.append(ButtonEvent(
                trigger_label=label,
                trigger_kind="button",
                handler_name=handler_name,
                api_calls=api_calls,
                screen_calls=nav_calls,
                notes="",
                source_file=f.rel_path,
                flow=flow,
            ))
    return out


def _resolve_handler_flow(on_click_node, tree, source: bytes):
    """onClick 의 표현식 노드 → (handler_name, flow_steps).

    케이스:
      - identifier: ``onClick={handleClick}`` → handleClick 본체 추적
      - arrow_function 인데 본체가 단일 함수 호출이면 그 함수명을 가서
        추적 (``() => handleAdd(x)`` → handleAdd 본체)
      - 그 외 arrow_function: 본체 자체를 그대로 traversal
      - call_expression: 그 호출 식 자체를 1-step 으로
      - 그 외 / None: 빈
    """
    from .flow_tracer import trace_flow_in_node
    if on_click_node is None:
        return "", []

    t = on_click_node.type
    if t == "identifier":
        name = text_of(on_click_node, source).strip()
        return name, trace_button_flow(name, tree, source)
    if t == "arrow_function":
        body = child_by_field(on_click_node, "body")
        # `() => fnName(...)`  : body 가 call_expression 이고 callee 가 identifier
        if body is not None and body.type == "call_expression":
            callee = child_by_field(body, "function")
            if callee is not None and callee.type == "identifier":
                name = text_of(callee, source).strip()
                resolved = trace_button_flow(name, tree, source)
                if resolved:
                    return name, resolved
        # 그 외: arrow 본체 자체 트레이스 (anonymous handler)
        return "", trace_flow_in_node(body, source) if body else (
            "", [])
    if t == "call_expression":
        return "", trace_flow_in_node(on_click_node, source)
    return "", []


# ─────────────────────────────────────────────────────────────────
# 5. 검증 규칙 (ValidationRule)
# ─────────────────────────────────────────────────────────────────

# yup/zod chain pattern: someField.required('msg').matches(/.../, 'msg').min(3)
_VALIDATION_CHAIN_RE = re.compile(
    r"\.(?P<rule>required|matches|min|max|email|url|length|"
    r"oneOf|notOneOf|positive|negative|integer|minLength|maxLength)"
    r"\(\s*(?P<args>[^)]*?)\s*\)",
    re.DOTALL,
)


def extract_validations(closure: ScreenClosure,
                        patterns: dict | None = None) -> list[ValidationRule]:
    out: list[ValidationRule] = []
    for f in closure.files:
        from ..legacy_react_ast import parse_file
        tree, source, _ = parse_file(f.abs_path)
        if tree is None:
            continue
        # 1) JSX inline props (required/pattern/min/max)
        out.extend(_extract_inline_validations(tree, source, f.rel_path))
        # 2) yup/zod/joi schema chain — text 기반 매칭
        out.extend(_extract_schema_validations(tree, source, f.rel_path))
    return out


def _extract_inline_validations(tree, source: bytes, rel: str
                                ) -> list[ValidationRule]:
    out: list[ValidationRule] = []
    for el, tag in _jsx_open_elements(tree, source):
        attrs = _jsx_attributes(el, source)
        if not attrs:
            continue
        name = (attrs.get("name") or attrs.get("id") or attrs.get("field") or "")
        if not name:
            continue
        for prop, rule in (("required", "required"),
                           ("pattern", "pattern"),
                           ("minLength", "minLength"),
                           ("maxLength", "maxLength"),
                           ("min", "min"),
                           ("max", "max")):
            if prop in attrs:
                v = attrs[prop]
                if prop == "required" and v != "true":
                    continue
                out.append(ValidationRule(
                    field=name,
                    rule=rule,
                    detail="" if prop == "required" else v,
                    message="",
                    source="jsx_prop",
                    source_file=rel,
                ))
    return out


def _extract_schema_validations(tree, source: bytes, rel: str
                                ) -> list[ValidationRule]:
    out: list[ValidationRule] = []
    text = source.decode("utf-8", errors="replace")
    if not any(lib in text for lib in _VALIDATION_LIBS):
        return out
    # 매우 단순한 패턴: `fieldName: yup.string().required('msg').matches(/.../, 'msg2')`
    field_pattern = re.compile(
        r"(?P<field>[A-Za-z_$][\w$]*)\s*:\s*(?:yup|zod|joi|z|Y|Z)\.\w+\(\)"
        r"(?P<chain>(?:\.\w+\([^)]*\))+)",
    )
    for m in field_pattern.finditer(text):
        field_name = m.group("field")
        chain = m.group("chain")
        for cm in _VALIDATION_CHAIN_RE.finditer(chain):
            rule = cm.group("rule")
            args = (cm.group("args") or "").strip()
            # 첫 인자만 detail, 마지막 string 인자가 message 인 경우 많음
            detail, message = _split_chain_args(args)
            out.append(ValidationRule(
                field=field_name,
                rule=rule,
                detail=detail,
                message=message,
                source="yup" if "yup" in text else (
                    "zod" if "zod" in text else "joi"),
                source_file=rel,
            ))
    return out


def _split_chain_args(args: str) -> tuple[str, str]:
    """yup chain 인자 분리: 첫 (정규식/숫자/...) + 마지막 (메시지 문자열)."""
    if not args:
        return "", ""
    # 마지막 quoted string 이 message
    msg_match = re.search(r"['\"`]([^'\"`]*)['\"`]\s*$", args.strip())
    msg = msg_match.group(1) if msg_match else ""
    detail = args
    if msg_match:
        detail = args[:msg_match.start()].rstrip().rstrip(",").strip()
    return detail, msg


# ─────────────────────────────────────────────────────────────────
# Public — 한 화면 통합 추출
# ─────────────────────────────────────────────────────────────────

def extract_screen_spec(closure: ScreenClosure,
                        screen_id: str | None = None,
                        patterns: dict | None = None) -> ScreenSpec:
    """ScreenClosure → ScreenSpec (모든 추출기 통합)."""
    sid = screen_id or closure.entry_name
    return ScreenSpec(
        screen_id=sid,
        entry_file=str(closure.entry_file),
        closure_file_count=len(closure.files),
        closure_files=[f.rel_path for f in closure.files],
        closure_truncated=closure.truncated,
        closure_tokens=closure.total_tokens,
        form_fields=extract_form_fields(closure, patterns),
        grid_columns=extract_grid_columns(closure, patterns),
        tabs=extract_tabs(closure, patterns),
        buttons=extract_buttons(closure, patterns),
        validations=extract_validations(closure, patterns),
        api_calls_factual=[
            {"file": a.file, "line": a.line, "method": a.method,
             "url": a.url or "", "handler": a.handler or ""}
            for a in closure.api_calls
        ],
        popup_refs_factual=[
            {"component": p.component_name, "trigger": p.trigger,
             "from": p.invoked_from, "line": p.line}
            for p in closure.popup_refs
        ],
    )


def build_and_extract(entry_file: Path, frontend_dir: Path,
                      screen_id: str | None = None,
                      patterns: dict | None = None,
                      token_budget: int = 20000,
                      max_depth: int = 3) -> ScreenSpec:
    """편의 함수: build_closure + extract_screen_spec 한 번에."""
    closure = build_closure(
        entry_file=str(entry_file),
        repo_root=str(frontend_dir),
        patterns=patterns,
        max_depth=max_depth,
        token_budget=token_budget,
        verbose=False,
    )
    return extract_screen_spec(closure, screen_id=screen_id, patterns=patterns)
