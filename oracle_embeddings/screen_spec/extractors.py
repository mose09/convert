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

# 검색 패널 컨테이너 className (한국 SI 컨벤션) — patterns.yaml 로 확장 가능.
DEFAULT_SEARCH_CONTAINERS = (
    "search-area", "search-form", "search-section",
    "filter-area", "filter-section", "criteria-area",
)

# 검색 1개 조건 단위 className — search-item 이 있으면 default input pattern
# 의존 없이 item 단위로 1 field 추출 (custom Popover 등 자동 흡수).
DEFAULT_SEARCH_ITEMS = (
    "search-item", "filter-item", "criteria-item",
    "search-row", "form-item", "form-row",
)

# 드롭다운 자식 Option 컴포넌트 이름
DEFAULT_OPTION_COMPONENTS = ("Option", "MenuItem", "SelectOption", "OptGroup",
                              "option")

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


def _extract_jsx_ancestor_condition(jsx_node, source: bytes) -> str:
    """JSX 노드의 ancestor 중 conditional (ternary / ``&&`` / ``||``) 의 test
    텍스트들 모음 → ``cond1 && cond2`` 형태로 결합. 같은 화면에 ``{tab === 'A'
    && <Grid/>}`` 처럼 분기로 render 되는 grid 의 condition 추출용.

    지원 패턴:
      - ``a && <X/>``     → ``a``
      - ``a ? <X/> : Y``  → ``a``           (then branch)
      - ``a ? Y : <X/>``  → ``!(a)``        (else branch)

    nested conditional 은 inner 부터 outer 순으로 합침. 빈 문자열이면
    top-level (무조건 render).
    """
    conditions: list[str] = []
    node = jsx_node.parent
    # parent jsx_element 노드는 같은 JSX 라 skip — 그 위의 expression 부터.
    while node is not None and node.type in ("jsx_element", "jsx_fragment"):
        node = node.parent
    while node is not None:
        if node.type in ("ternary_expression", "conditional_expression"):
            cond_node = (child_by_field(node, "condition")
                         or (node.children[0] if node.children else None))
            then_node = child_by_field(node, "consequence")
            else_node = child_by_field(node, "alternative")
            cond_text = text_of(cond_node, source).strip() if cond_node else ""
            if cond_text:
                if then_node and _node_contains(then_node, jsx_node):
                    conditions.append(cond_text)
                elif else_node and _node_contains(else_node, jsx_node):
                    conditions.append(f"!({cond_text})")
        elif node.type == "binary_expression":
            # operator 노드 (& ||) 확인
            op_text = ""
            for c in node.children:
                if c.type in ("&&", "||"):
                    op_text = c.type
                    break
            if op_text == "&&":
                left = node.children[0] if node.children else None
                right = node.children[-1] if node.children else None
                # jsx 가 right 쪽이면 left 가 condition
                if right is not None and _node_contains(right, jsx_node):
                    left_text = text_of(left, source).strip() if left else ""
                    if left_text:
                        conditions.append(left_text)
        node = node.parent
    return " && ".join(reversed(conditions))


def _node_contains(outer, inner) -> bool:
    """tree-sitter 노드 byte range 로 outer 가 inner 를 포함하는지 판정."""
    return (outer.start_byte <= inner.start_byte
            and inner.end_byte <= outer.end_byte)


# 길이 추출 — ag-grid 의 ``cellEditor: 'ResetNumber'`` 같은 커스텀 에디터를
# import 한 파일에서 ``maxLength={N}`` / ``maxLength: N`` regex.
import re as _length_re
import os as _length_os

_MAXLENGTH_RE = _length_re.compile(r"\bmaxLength\s*[:=]\s*\{?\s*(\d+)\s*\}?")


def _resolve_cell_editor_max_length(cell_editor_name: str,
                                    current_abs_path: str,
                                    closure) -> str:
    """``cellEditor: 'CustomEditor'`` 의 import 파일에서 ``maxLength`` 추출.

    1. current file 에서 ``import CustomEditor from <path>`` 매칭
    2. path resolve (relative + extension/index)
    3. 그 파일에서 ``maxLength={N}`` 또는 ``maxLength: N`` 추출
    4. closure 안 fallback (current 에서 못 찾으면 closure 내 같은 이름
       파일 검색)

    실패 시 빈 문자열.
    """
    if not cell_editor_name or not current_abs_path:
        return ""
    try:
        with open(current_abs_path, "r", encoding="utf-8", errors="ignore") as f:
            cur = f.read()
    except Exception:
        return ""
    # default 또는 named import 매칭
    import_pat = _length_re.compile(
        r"""import\s+"""
        r"""(?:"""
        r"""\{[^}]*\b""" + _length_re.escape(cell_editor_name) + r"""\b[^}]*\}"""
        r"""|"""
        + _length_re.escape(cell_editor_name) +
        r""")"""
        r"""(?:\s*,\s*\{[^}]*\})?"""
        r"""\s+from\s+["']([^"']+)["']"""
    )
    m = import_pat.search(cur)
    target_abs = ""
    if m:
        target_abs = _resolve_relative_import_abs(m.group(1), current_abs_path)
    if not target_abs:
        # closure fallback — 같은 이름의 파일
        for cf in closure.files:
            base = _length_os.path.splitext(_length_os.path.basename(str(cf.abs_path)))[0]
            if base == cell_editor_name:
                target_abs = str(cf.abs_path)
                break
    if not target_abs or not _length_os.path.isfile(target_abs):
        return ""
    try:
        with open(target_abs, "r", encoding="utf-8", errors="ignore") as f:
            editor_src = f.read()
    except Exception:
        return ""
    lm = _MAXLENGTH_RE.search(editor_src)
    return lm.group(1) if lm else ""


def _resolve_relative_import_abs(import_path: str, current_abs_path: str) -> str:
    """``./X`` / ``../Y/Z`` import path → abs path. 확장자/index 후보 시도."""
    if not import_path.startswith("."):
        return ""
    cur_dir = _length_os.path.dirname(current_abs_path)
    base = _length_os.path.normpath(_length_os.path.join(cur_dir, import_path))
    cands = [
        base,
        base + ".js", base + ".jsx", base + ".ts", base + ".tsx",
        _length_os.path.join(base, "index.js"),
        _length_os.path.join(base, "index.jsx"),
        _length_os.path.join(base, "index.ts"),
        _length_os.path.join(base, "index.tsx"),
    ]
    for c in cands:
        if _length_os.path.isfile(c):
            return c
    return ""


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


# ── 화면정의서 9컬럼 (검색영역) — UI 타입 / data type / action 휴리스틱 ──

_INPUT_TAG_KEYWORDS = ("input", "textfield", "textbox", "textarea",
                       "search", "password", "number")
_SELECT_TAG_KEYWORDS = ("select", "dropdown", "combobox", "combo")
_DATE_TAG_KEYWORDS = ("date", "calendar", "time")
_CHECKBOX_TAG_KEYWORDS = ("checkbox",)
_RADIO_TAG_KEYWORDS = ("radio",)


def _is_keyboard_input(tag: str, field_type: str) -> bool:
    """키보드로 직접 타이핑하는 입력 필드인지 — 타입/길이 칸을 채울지 결정.

    select / checkbox / radio / date / time picker / popover 류는 키보드
    타이핑이 아니므로 타입·길이 칸 비움. text / textarea / number /
    password / search / email / tel / url 류만 채움.
    """
    tag_l = (tag or "").lower()
    ft = (field_type or "").lower()
    if any(k in tag_l for k in _SELECT_TAG_KEYWORDS + _DATE_TAG_KEYWORDS
           + _CHECKBOX_TAG_KEYWORDS + _RADIO_TAG_KEYWORDS):
        return False
    if any(k in tag_l for k in ("popover", "popconfirm")):
        return False
    if ft in ("select", "checkbox", "radio", "date", "daterange", "time"):
        return False
    return True


def _input_data_type(tag: str, attrs: dict[str, str], field_type: str) -> str:
    """키보드 입력 필드의 data type — String / Number / Date / "" (비입력)."""
    if not _is_keyboard_input(tag, field_type):
        return ""
    tag_l = (tag or "").lower()
    ft = (field_type or "").lower()
    type_attr = (attrs.get("type") or "").strip("'\"").lower()
    if "number" in tag_l or ft == "number" or type_attr == "number":
        return "Number"
    if "date" in tag_l or ft in ("date", "daterange") or type_attr == "date":
        return "Date"
    return "String"


# ── 화면정의서 9컬럼 (검색영역) — UI 타입 / data type / action 휴리스틱 ──

# JSX onChange={...} 의 leaf handler 이름. ``{this.handleFabChange}`` /
# ``{handleFabChange}`` / ``{(e) => this.handleFabChange(e)}`` 모두 처리.
# arrow body 안 첫 식별자 호출이 진짜 handler 인 경우가 많다.
_HANDLER_LEAF_RE = re.compile(
    r"""(?:^|[\s({,])
        (?:this\s*\.\s*)?
        (?P<name>[A-Za-z_$][\w$]*)
        \s*(?=\(|\)|,|$)""",
    re.VERBOSE,
)


def _extract_handler_leaf(attr_value: str) -> str:
    """JSX attribute value 텍스트 → leaf handler 이름.

    예: ``{this.handleFabChange}`` → ``handleFabChange``
        ``{handleFabChange}`` → ``handleFabChange``
        ``{(e) => this.handleFabChange(e)}`` → ``handleFabChange``
        ``{() => { this.setState({...}); }}`` → "" (inline body, leaf 없음)

    arrow 안에 fn 호출이 1개면 그것을 leaf, 여러 개면 보수적으로 ``""``
    (정확도 우선 — 잘못된 매칭 방지).
    """
    if not attr_value:
        return ""
    s = attr_value.strip()
    # 양 끝 {}/공백 제거
    if s.startswith("{") and s.endswith("}"):
        s = s[1:-1].strip()
    # 1) 단순 reference: ``this.X`` 또는 ``X``
    m = re.fullmatch(r"(?:this\s*\.\s*)?([A-Za-z_$][\w$]*)", s)
    if m:
        return m.group(1)
    # 2) ``X.bind(this)`` / ``X.bind(this, arg)``
    m = re.fullmatch(
        r"(?:this\s*\.\s*)?([A-Za-z_$][\w$]*)\s*\.\s*bind\s*\([^)]*\)", s)
    if m:
        return m.group(1)
    # 3) arrow: ``(args) => this.X(args)`` 또는 ``arg => X(arg)``
    arrow_m = re.search(
        r"=>\s*\{?\s*(?:return\s+)?(?:this\s*\.\s*)?([A-Za-z_$][\w$]*)\s*\(", s)
    if arrow_m:
        # arrow body 안 fn 호출 갯수 — 1개일 때만 leaf 신뢰
        body_calls = re.findall(r"\b(?:this\s*\.\s*)?([A-Za-z_$][\w$]*)\s*\(", s)
        # setState 같은 noise 는 제외
        meaningful = [c for c in body_calls
                      if c not in ("setState", "bind", "call", "apply")]
        if len(meaningful) == 1:
            return meaningful[0]
        # 여러 호출이면 첫 번째 (보통 의도된 handler 가 먼저)
        if meaningful:
            return meaningful[0]
    return ""


# ── cascading clear 검출 — onChange handler 안 setState 가 다른 field 들을
# undefined/null/''/false 로 초기화하는 패턴. 한국 SI 흔한 hierarchy
# (FAB → Team → SDPT → Model). 발견 시 parent.action 에 "변경 시 X, Y
# 초기화" + child.validation_rule 에 "{parent} 변경 시 자동 초기화" 채움.

_SETSTATE_BODY_RE = re.compile(
    r"\b(?:this\s*\.\s*)?setState\s*\(\s*\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}",
    re.DOTALL,
)
_CLEAR_KV_RE = re.compile(
    r"""(\w+)\s*:\s*
        (?:undefined|null|''|""|``|false|\[\s*\]|\{\s*\})""",
    re.VERBOSE,
)


def _detect_cascading_clears(fields, file_sources: dict) -> None:
    """fields 리스트를 mutate — onChange handler 안 setState 가 다른 field
    들을 초기화하면 ``action`` / ``validation_rule`` 에 cascading 동작 추가.

    file_sources: ``{rel_path: source_text}``. 각 field 의 ``source_file``
    이 이 dict 의 key 와 일치하면 그 파일에서 handler body 탐색.

    field 매칭은 (a) ``name`` (form key) 와 (b) ``label`` 둘 다 lowercase
    로 시도 — 일반적으로 ``name`` 이 setState key 와 일치.
    """
    if not fields:
        return
    # name / label → field 매핑 (lowercase)
    name_to_field = {}
    for f in fields:
        for key in ((f.name or "").lower(), (f.label or "").lower()):
            if key and key not in name_to_field:
                name_to_field[key] = f
    if not name_to_field:
        return

    from ..legacy_react_api_scanner import _locate_handler_body

    for f in fields:
        if not f.change_handler:
            continue
        src = file_sources.get(f.source_file or "")
        if not src:
            continue
        body = _locate_handler_body(src, f.change_handler)
        if not body:
            continue
        cleared: list[str] = []
        for ss in _SETSTATE_BODY_RE.finditer(body):
            inner = ss.group(1)
            for km in _CLEAR_KV_RE.finditer(inner):
                key = km.group(1).lower()
                # 자기 자신은 제외 (FAB 의 setState 안 fab: event 는 정상)
                if key == (f.name or "").lower():
                    continue
                if key == (f.label or "").lower():
                    continue
                if key in name_to_field and key not in cleared:
                    cleared.append(key)
        if not cleared:
            continue
        parent_label = f.label or f.name or "?"
        # cleared field 들의 사용자 가시 라벨 list
        child_labels = [
            (name_to_field[k].label or name_to_field[k].name or k)
            for k in cleared
        ]
        # parent 의 action — 기존 옵션 list 가 있으면 cascading 설명 prepend
        cascade_desc = f"변경 시 {', '.join(child_labels)} 초기화"
        if f.action:
            f.action = cascade_desc + "\n\n" + f.action
        else:
            f.action = cascade_desc
        # 각 child 의 validation_rule — parent 의존성 명시
        for k in cleared:
            child = name_to_field[k]
            note = f"{parent_label} 변경 시 자동 초기화 (의존)"
            if child.validation_rule:
                if note not in child.validation_rule:
                    child.validation_rule += "\n" + note
            else:
                child.validation_rule = note


_POPOVER_TAG_KEYWORDS = ("popover", "popconfirm", "popselect", "popoverselect")


def _infer_form_ui_type(tag: str, attrs: dict[str, str], field_type: str,
                        options: str) -> str:
    """검색 패널 입력 컴포넌트의 UI 타입 라벨.

    예: Select(Single), Select(Multi), Text Field(Search Box), Text Field
    (Basic), DatePicker, Date Range, Checkbox, Radio Group, Number Field,
    Password, Popover. 컴포넌트 이름에 ``Popover`` / ``Popconfirm`` 키워드
    포함되면 Popover (Ant Design 등 — Select 와 비슷한 click trigger).
    """
    tag_l = (tag or "").lower()
    ft = (field_type or "").lower()
    # Popover 류 — 다른 입력 키워드보다 우선 매칭 (사용자 보고: Popover 가
    # Select 로 잘못 분류되던 케이스). children 에 Select/Option 있으면
    # 그래도 Popover 가 사용자 화면 인터랙션 타입.
    if any(k in tag_l for k in _POPOVER_TAG_KEYWORDS):
        return "Popover"
    # Select 류 — multi 인지 검사
    if any(k in tag_l for k in _SELECT_TAG_KEYWORDS) or ft == "select":
        if (attrs.get("mode") == "multiple" or attrs.get("multiple") == "true"
                or "multi" in tag_l):
            return "Select(Multi)"
        return "Select(Single)"
    if any(k in tag_l for k in _CHECKBOX_TAG_KEYWORDS) or ft == "checkbox":
        return "Checkbox"
    if any(k in tag_l for k in _RADIO_TAG_KEYWORDS) or ft == "radio":
        return "Radio Group"
    # Date 류
    if ft == "daterange" or "range" in tag_l:
        return "Date Range"
    if any(k in tag_l for k in _DATE_TAG_KEYWORDS) or ft in ("date", "time"):
        return "DatePicker"
    # Input 류
    type_attr = (attrs.get("type") or "").strip("'\"").lower()
    if "number" in tag_l or ft == "number" or type_attr == "number":
        return "Number Field"
    if type_attr == "password" or "password" in tag_l:
        return "Password"
    # search / textarea / 일반 텍스트
    if "search" in tag_l or type_attr == "search":
        return "Text Field(Search Box)"
    if "textarea" in tag_l:
        return "Text Area"
    return "Text Field(Basic)"


def _compose_form_action(field_type: str, options: str, ui_type: str) -> str:
    """동작 컬럼 기본값 — 단순 dropdown/checkbox/radio/Popover (선택형) 면
    옵션 값을 줄바꿈으로.

    예: "전체\\nY\\nN". cascading dependency 패턴이 LLM 으로 추출되면
    LLM 응답이 이 값을 덮어쓴다 (선택지가 행위가 아닌 단순 enum 일 때만
    parser 기본값 유지).
    """
    if not options:
        return ""
    ft = (field_type or "").lower()
    if (ft in ("select", "checkbox", "radio")
            or ui_type.startswith("Select")
            or ui_type in ("Checkbox", "Radio Group", "Popover")):
        items = [o.strip() for o in options.split(",") if o.strip()]
        if items:
            return "\n".join(items)
    return ""


def _find_label_text_in_children(parent, exclude, source: bytes
                                 ) -> tuple[str, str]:
    """parent 의 자식 element 중 ``className`` 에 'label' 포함된 element 의
    text child 반환 (exclude 노드는 제외).

    Returns ``(text, label_classname)`` — text 빈 문자열이면 매칭 없음.
    classname 은 호출자가 'required' 토큰 같은 부가 정보 추출용.
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
                    return txt, cls
    return "", ""


def _find_search_containers(tree, source: bytes,
                            container_class_tokens: set[str]) -> list:
    """``className`` 에 search-area / filter-area 등 토큰 포함된 JSX element
    노드 반환. byte offset 으로 안에 든 input 식별용 boundary."""
    out: list = []
    for n in find_by_type(tree.root_node, "jsx_element"):
        opening = next((c for c in n.children if c.type == "jsx_opening_element"), None)
        if opening is None:
            continue
        attrs = _jsx_attributes(opening, source)
        cls = (attrs.get("className") or "").lower()
        if not cls:
            continue
        if any(token.lower() in cls for token in container_class_tokens):
            out.append(n)
    return out


def _node_contains(parent, child) -> bool:
    """child 의 byte offset 이 parent 안에 들어있는지."""
    return parent.start_byte <= child.start_byte and child.end_byte <= parent.end_byte


def _has_class_token(cls: str, tokens) -> bool:
    """공백 분리 정확 토큰 매칭. ``"search-item"`` 토큰이 ``"search-item-area"``
    의 substring 매치로 잡히는 false-positive 방지."""
    parts = cls.lower().split()
    return any(t.lower() in parts for t in tokens)


def _find_search_items(tree, source: bytes, item_class_tokens) -> list:
    """``className`` 에 search-item / filter-item 토큰 포함된 jsx_element."""
    out = []
    for n in find_by_type(tree.root_node, "jsx_element"):
        opening = next((c for c in n.children if c.type == "jsx_opening_element"), None)
        if opening is None:
            continue
        attrs = _jsx_attributes(opening, source)
        cls = attrs.get("className") or ""
        if not cls:
            continue
        if _has_class_token(cls, item_class_tokens):
            out.append(n)
    return out


_INPUT_EXCLUDE_TAGS = {"Button", "IconButton", "LinkButton"}


def _find_label_in_item(item_node, source: bytes) -> tuple[str, str]:
    """search-item descendant 중 className 에 'label' substring 포함된
    element 의 text child. ``search-label`` / ``form-label`` 등 매칭.
    Returns (text, full_classname). classname split 에 'required' 있으면
    호출자가 필수 인식.
    """
    for n in walk(item_node):
        if n is item_node:
            continue
        if n.type not in ("jsx_opening_element", "jsx_self_closing_element"):
            continue
        attrs = _jsx_attributes(n, source)
        cls = (attrs.get("className") or "").lower()
        if "label" not in cls:
            continue
        parent_el = n.parent if n.type == "jsx_opening_element" else n
        if parent_el is None or parent_el.type != "jsx_element":
            continue
        for c in parent_el.children:
            if c.type == "jsx_text":
                t = text_of(c, source).strip()
                if t:
                    return t, cls
    return "", ""


def _find_input_in_item(item_node, source: bytes):
    """search-item descendant 중 첫 대문자-시작 컴포넌트 (Button / 라벨 wrap
    제외). default input pattern 의존 없음 — Popover / Custom* 등 자동
    흡수. Returns (element_node, tag) 또는 (None, "")."""
    for n in walk(item_node):
        if n is item_node:
            continue
        if n.type not in ("jsx_opening_element", "jsx_self_closing_element"):
            continue
        nm = child_by_field(n, "name")
        if nm is None:
            continue
        tag = text_of(nm, source).strip()
        if not tag or not tag[0].isupper():
            continue
        if tag in _INPUT_EXCLUDE_TAGS:
            continue
        attrs = _jsx_attributes(n, source)
        cls = (attrs.get("className") or "").lower()
        if "label" in cls:
            continue
        return n, tag
    return None, ""


def _extract_field_from_item(item_node, source: bytes,
                             option_comps,
                             rel_path: str, order: int):
    """1 search-item → 1 FormField. label 은 className 'label' 의 text,
    input 은 item 안 첫 jsx component (label wrap 제외).
    """
    label_text, label_cls = _find_label_in_item(item_node, source)
    input_el, input_tag = _find_input_in_item(item_node, source)
    if input_el is None and not label_text:
        return None
    attrs = _jsx_attributes(input_el, source) if input_el is not None else {}
    sibling_required = "required" in label_cls.split()
    field_type = _classify_field_type(input_tag, attrs) if input_tag else ""
    options = (_extract_dropdown_options(input_el, source, option_comps)
               if input_el is not None else "")
    ui_type = (_infer_form_ui_type(input_tag, attrs, field_type, options)
               if input_tag else "")
    return FormField(
        order=order,
        label=(label_text
               or attrs.get("label")
               or attrs.get("placeholder")
               or attrs.get("title")
               or attrs.get("aria-label")
               or ""),
        name=(attrs.get("id") or attrs.get("name") or attrs.get("field") or ""),
        field_type=field_type,
        required=(attrs.get("required") == "true"
                  or "required" in (attrs.get("className") or "").lower().split()
                  or sibling_required),
        default=(attrs.get("defaultValue") or attrs.get("value") or ""),
        validation=_format_inline_validation(attrs),
        source_file=rel_path,
        jsx_tag=input_tag,
        events=_extract_event_props(attrs),
        options=options,
        placeholder=attrs.get("placeholder", ""),
        max_length=attrs.get("maxLength", ""),
        input_data_type=_input_data_type(input_tag, attrs, field_type),
        ui_type=ui_type,
        action=_compose_form_action(field_type, options, ui_type),
        change_handler=_extract_handler_leaf(attrs.get("onChange", "")),
    )


def _extract_event_props(attrs: dict[str, str]) -> str:
    """``onChange`` / ``onClick`` / ``onBlur`` 등 React event prop 만 모아
    ``"onChange / onClick"`` 형식. 시스템 이벤트 (onCancel / onGridReady /
    onLoad 등 라이프사이클 콜백) 는 사용자 trigger 가 아니라 제외.
    """
    from ..legacy_react_api_scanner import _is_noise_event
    evts = [k for k in attrs
            if k.startswith("on") and len(k) > 2 and k[2].isupper()
            and not _is_noise_event(k[2:])]
    return " / ".join(sorted(evts))


# ``options={[{...}, {...}]}`` 의 각 ``{...}`` 객체 블록.
_OPTION_OBJ_BLOCK_RE = re.compile(r"\{[^{}]+?\}", re.DOTALL)
# 객체 안 표시용 라벨 키 (label/title/name/text) — 우선.
_OBJ_LABEL_KEY_RE = re.compile(
    r"""\b(?:label|title|name|text)\s*:\s*['"`]([^'"`]+)['"`]"""
)
# 객체 안 value 키 — 라벨 없으면 fallback.
_OBJ_VALUE_KEY_RE = re.compile(
    r"""\bvalue\s*:\s*['"`]([^'"`]+)['"`]"""
)


def _option_tag_matches(tag: str, option_comps) -> bool:
    """tag 가 Option 컴포넌트인지 — 단순 일치 + ``Select.Option`` 같이
    namespaced 형태도 suffix 매칭.
    """
    if tag in option_comps:
        return True
    if "." in tag:
        suffix = tag.rsplit(".", 1)[-1]
        if suffix in option_comps:
            return True
    return False


def _extract_dropdown_options(input_element_node, source: bytes,
                              option_comps) -> str:
    """드롭다운(Select 등) 의 children 중 ``<Option value=...>`` 의 value
    (또는 text child) 모아 ``"Y, N"`` 형식. children 이 없으면 ``options``
    prop 의 array literal (``[{value:'A',label:'AA'},...]``) 도 시도.
    여전히 없으면 빈 문자열.

    namespaced ``<Select.Option>`` (Ant Design 등) 도 suffix 매칭으로
    인식. ``input_element_node`` 가 jsx_opening_element 이면 wrapping
    element 까지 올라가서 children 탐색. self-closing 이면 children
    없음 → ``options`` prop array 만 확인.
    """
    values: list[str] = []
    # children 기반 (Option 컴포넌트들)
    cur = input_element_node
    if cur.type == "jsx_opening_element" and cur.parent is not None:
        cur = cur.parent
    if cur.type == "jsx_element":
        for n in walk(cur):
            if n is cur:
                continue
            if n.type not in ("jsx_opening_element", "jsx_self_closing_element"):
                continue
            nm = child_by_field(n, "name")
            if nm is None:
                continue
            tag = text_of(nm, source).strip()
            if not _option_tag_matches(tag, option_comps):
                continue
            attrs = _jsx_attributes(n, source)
            val = (attrs.get("value") or attrs.get("key") or "").strip()
            if not val:
                # text child 사용 — <Option>Yes</Option>
                if n.type == "jsx_opening_element" and n.parent is not None:
                    for c in n.parent.children:
                        if c.type == "jsx_text":
                            t = text_of(c, source).strip()
                            if t:
                                val = t
                                break
            if val and val not in values:
                values.append(val)

    # children 으로 못 잡으면 ``options`` prop 의 array literal 탐색.
    # 사용자 케이스: ``<Select options={[{value:'A',label:'전체'},...]}/>``
    # **엄격 조건**: prop 값이 inline array literal (``[``로 시작) 인 경우만.
    # ``options={someVar}`` / ``options={getList()}`` 같은 함수/변수 참조는
    # skip — false positive (다른 필드 옵션 cross-pollination) 방지.
    if not values:
        attrs = _jsx_attributes(input_element_node, source)
        opts_expr = (attrs.get("options") or attrs.get("dataSource") or "").strip()
        if opts_expr.startswith("[") and "{" in opts_expr:
            for block in _OPTION_OBJ_BLOCK_RE.finditer(opts_expr):
                blk = block.group(0)
                lm = _OBJ_LABEL_KEY_RE.search(blk)
                vm = _OBJ_VALUE_KEY_RE.search(blk)
                # 표시용 — label 우선 (사용자 가시값), 없으면 value
                shown = (lm.group(1) if lm else (vm.group(1) if vm else ""))
                shown = shown.strip()
                if shown and shown not in values:
                    values.append(shown)
    return ", ".join(values)


def _sibling_label(input_node, source: bytes) -> str:
    """한국 SI 흔한 패턴: input 컴포넌트의 형제 (또는 가까운 ancestor 의
    자식) 중 ``className`` 에 'label' 포함된 element 의 text child 를
    라벨로. props 의 label/placeholder/title 가 모두 비어있을 때만 사용.

    예::

        <div className="search-item">
          <span className="search-label required">Team</span>   ← 라벨
          <div className="search-input-wrap">
            <span className="search-select">
              <Select .../>                                      ← input 컴포넌트
            </span>
          </div>
        </div>

    ancestor 최대 5 단계까지 탐색 (사용자 회사마다 wrap 깊이 다름).
    각 단계의 direct children 만 — descendant 까지 가면 다른 input 의
    라벨 오인 위험.
    """
    text, _ = _sibling_label_info(input_node, source)
    return text


def _sibling_label_info(input_node, source: bytes) -> tuple[str, str]:
    """``_sibling_label`` 동일 로직이지만 (text, label_classname) 반환.
    classname 에 'required' 토큰 있으면 호출자가 필수 여부 추출."""
    cur = input_node
    if cur.type == "jsx_opening_element" and cur.parent is not None:
        cur = cur.parent
    for _ in range(5):
        parent = cur.parent
        if parent is None or parent.type not in ("jsx_element", "jsx_fragment"):
            break
        text, cls = _find_label_text_in_children(parent, exclude=cur, source=source)
        if text:
            return text, cls
        cur = parent
    return "", ""


def extract_form_fields(closure: ScreenClosure,
                        patterns: dict | None = None) -> list[FormField]:
    """모든 closure 파일을 훑어 입력 컴포넌트 → FormField 리스트.

    추출 우선순위:
      1. ``<section className="search-area">`` (또는 alias) 안의
         ``<div className="search-item">`` 단위로 1 item = 1 field —
         default input pattern 의존 없음 (Popover / Custom* 등 자동 흡수).
      2. search-item 없으면 search-area 안의 default input pattern.
      3. search-area 도 없으면 closure 전체의 default input pattern
         (회귀 0).
    """
    pat = _patterns_section(patterns)
    input_comps = _comp_list(pat, "input_components", DEFAULT_INPUT_COMPONENTS)
    container_tokens = _comp_list(pat, "search_containers",
                                  DEFAULT_SEARCH_CONTAINERS)
    item_tokens = _comp_list(pat, "search_items", DEFAULT_SEARCH_ITEMS)
    option_comps = _comp_list(pat, "option_components",
                              DEFAULT_OPTION_COMPONENTS)

    # Phase 1: 모든 파일 파싱 + container / item 식별
    from ..legacy_react_ast import parse_file
    file_data = []
    any_container = False
    any_item = False
    for f in closure.files:
        tree, source, _ = parse_file(f.abs_path)
        if tree is None:
            continue
        containers = _find_search_containers(tree, source, container_tokens)
        if containers:
            any_container = True
        items_all = _find_search_items(tree, source, item_tokens)
        # container 가 있으면 그 안의 item 만 (다른 영역 form-item 제외)
        if containers:
            items = [i for i in items_all
                     if any(_node_contains(c, i) for c in containers)]
        else:
            items = items_all
        if items:
            any_item = True
        file_data.append((f, tree, source, containers, items))

    # Phase 2a: search-item 우선 — 1 item = 1 field (Popover 등 자동 흡수).
    # **단 search-area container 없으면 skip** — search-item 이 search 영역
    # 밖에 있는 경우 (edit modal 등 다른 영역) 까지 search panel 로 잡히던
    # 문제 방지. container 없는 화면은 search panel 0건.
    if any_item and any_container:
        fields: list[FormField] = []
        order = 0
        for f, tree, source, containers, items in file_data:
            for item in items:
                fd = _extract_field_from_item(
                    item, source, option_comps, f.rel_path, order + 1)
                if fd is None:
                    continue
                fields.append(fd)
                order += 1
        # cascading clears 검출 — onChange handler 안 setState 가 다른 field
        # 들을 undefined 로 초기화하는 hierarchy 패턴 (FAB→Team→SDPT 등)
        _detect_cascading_clears(
            fields,
            {f.rel_path: source.decode("utf-8", errors="replace")
             for f, _t, source, _c, _i in file_data},
        )
        return fields

    # Phase 2b: search-item 없으면 search-area boundary 안 default input.
    # search-area 자체도 없으면 search_panel 빈 채로 반환 — 팝업 / 조회조건
    # 없는 화면이 closure transitive import 로 다른 input 까지 끌어오는 false
    # positive 방지. (이전 Phase C: closure 전체 default input — 제거됨)
    if not any_container:
        return []
    fields = []
    order = 0
    for f, tree, source, containers, _ in file_data:
        if not containers:
            continue
        for el, tag in _jsx_open_elements(tree, source):
            if tag not in input_comps:
                continue
            if not any(_node_contains(c, el) for c in containers):
                continue
            attrs = _jsx_attributes(el, source)
            # 우선순위: label prop > 한국 SI 형제 라벨 (className "label")
            # > placeholder / title / aria-label.
            sibling_text, sibling_cls = _sibling_label_info(el, source)
            label = (attrs.get("label")
                     or sibling_text
                     or attrs.get("placeholder")
                     or attrs.get("title")
                     or attrs.get("aria-label")
                     or "")
            # sibling label 의 className 에 'required' 토큰이 있으면 필수
            # (예: <span className="search-label required">Team</span>).
            sibling_required = "required" in sibling_cls.split()
            name = (attrs.get("name")
                    or attrs.get("id")
                    or attrs.get("field")
                    or "")
            order += 1
            field_type_ = _classify_field_type(tag, attrs)
            options_ = _extract_dropdown_options(el, source, option_comps)
            ui_type_ = _infer_form_ui_type(tag, attrs, field_type_, options_)
            fields.append(FormField(
                order=order,
                label=label,
                name=name,
                field_type=field_type_,
                required=(attrs.get("required") == "true"
                          or "required" in (attrs.get("className") or "").lower()
                          or sibling_required),
                default=(attrs.get("defaultValue")
                         or attrs.get("value") or ""),
                validation=_format_inline_validation(attrs),
                source_file=f.rel_path,
                jsx_tag=tag,
                events=_extract_event_props(attrs),
                options=options_,
                placeholder=attrs.get("placeholder", ""),
                max_length=attrs.get("maxLength", ""),
                input_data_type=_input_data_type(tag, attrs, field_type_),
                ui_type=ui_type_,
                action=_compose_form_action(field_type_, options_, ui_type_),
                change_handler=_extract_handler_leaf(attrs.get("onChange", "")),
            ))
    # cascading clears 검출 — Phase 2a 와 동일.
    _detect_cascading_clears(
        fields,
        {f.rel_path: source.decode("utf-8", errors="replace")
         for f, _t, source, _c, _i in file_data},
    )
    return fields


# ─────────────────────────────────────────────────────────────────
# 2. 그리드 컬럼 (GridColumn)
# ─────────────────────────────────────────────────────────────────

# 컬럼 객체에서 후보 키들 (lib 별로 이름이 달라 union 으로 매치)
_COL_HEADER_KEYS = ("header", "title", "label", "headerName", "text", "headerText")
_COL_DATA_KEYS = ("dataIndex", "field", "accessor", "key", "name", "dataField")
_COL_WIDTH_KEYS = ("width", "minWidth", "maxWidth", "flex")
_COL_HIDDEN_KEYS = ("hidden", "visible", "show", "display")
_COL_TYPE_KEYS = ("type", "dataType", "renderType", "cellDataType")
_COL_SORT_KEYS = ("sorter", "sortable", "sort", "allowSort")
_COL_DESC_KEYS = ("description", "tooltipField", "tooltip", "headerTooltip",
                  "comment", "note")
_COL_ACTION_KEYS = ("onCellClicked", "onCellDoubleClicked", "onClick",
                    "action", "clickAction")


def _infer_ui_type(d: dict[str, str]) -> str:
    """ag-grid columnDef object → UI 타입 라벨.

    cellRenderer / cellEditor / type / cellDataType 우선 매핑, 없으면
    "Text Field(Basic)" default.
    """
    cr = (d.get("cellRenderer") or "").strip().strip("'\"`")
    ce = (d.get("cellEditor") or "").strip().strip("'\"`")
    t = (_first_present(d, _COL_TYPE_KEYS) or "").strip().strip("'\"`").lower()
    crl = cr.lower(); cel = ce.lower()
    # cellRenderer / cellEditor 휴리스틱
    if "checkbox" in crl or "checkbox" in cel:
        return "Checkbox"
    if "select" in crl or "select" in cel or "combo" in crl or "combo" in cel:
        return "Dropdown"
    if "date" in crl or "date" in cel:
        return "DatePicker"
    if "number" in crl or "number" in cel or "numeric" in crl or "numeric" in cel:
        return "Number Field"
    if "link" in crl or "anchor" in crl or "button" in crl:
        return "Link/Button"
    # type / cellDataType 휴리스틱
    if t in ("number", "numericcolumn", "numeric"):
        return "Number Field"
    if t in ("date", "datestring", "datetime"):
        return "DatePicker"
    if t in ("boolean", "bool"):
        return "Checkbox"
    # cellRenderer / cellEditor 가 있긴 한데 매핑 안 된 케이스 — 원본 표기
    if cr:
        return cr
    if ce:
        return ce
    return "Text Field(Basic)"


def _compose_attribute(visible: bool, editable: bool) -> str:
    """필수여부/속성 — I/O/R/E/H 조합.

    hide → "H" (단독), editable=true → "O/E", default → "O/R".
    그리드는 기본 output (백엔드 데이터 표시), input 컬럼 (검색폼) 은
    별도 form_fields 트랙.
    """
    if not visible:
        return "H"
    return "O/E" if editable else "O/R"


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
    """모든 closure 파일에서 Table/DataGrid 의 columns prop → GridColumn 리스트.

    추출 후 onSave / handleSave 등 핸들러의 isNull / isNumber 등 검증
    패턴을 ``data_key`` 매칭으로 ``required`` / ``validation_rule``-
    equivalent 에 머지 (search panel 과 동일 원칙).
    """
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
            # grid 의 conditional ancestor — 같은 화면에 ``{tab === 'A' &&
            # <Grid/>}`` 처럼 분기 render 되는 경우 condition 으로 group.
            grid_condition = _extract_jsx_ancestor_condition(el, source)
            # ag-grid: columnDefs / antd, generic: columns / RealGrid 등: schema.
            # 라이브러리별 prop 이름 union.
            cols_expr = (attrs.get("columns") or attrs.get("columnDefs")
                         or attrs.get("schema") or "")
            if not cols_expr:
                # 컬럼 정의가 children (<TableColumn ...> 형태) 인 케이스 처리
                child_cols = _extract_table_column_children(el, source, f.rel_path,
                                                            start_order=order)
                # children 케이스도 grid 의 condition 부여
                if grid_condition:
                    for c in child_cols:
                        c.condition = grid_condition
                cols.extend(child_cols)
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
                # 길이 — cellEditor 가 커스텀 컴포넌트면 import 추적해서
                # maxLength regex. 사용자 보고: ag-grid 컬럼에 cellEditor:
                # 'ResetNumber' → ResetNumber.js 안 maxLength={10}.
                cell_editor_name = _strip_quotes(d.get("cellEditor", "") or "")
                length_val = _resolve_cell_editor_max_length(
                    cell_editor_name, str(f.abs_path), closure
                ) if cell_editor_name else ""
                cols.append(GridColumn(
                    order=order,
                    header=_strip_quotes(_first_present(d, _COL_HEADER_KEYS) or ""),
                    data_key=_strip_quotes(_first_present(d, _COL_DATA_KEYS) or ""),
                    data_type=_strip_quotes(_first_present(d, _COL_TYPE_KEYS) or ""),
                    width=_strip_quotes(_first_present(d, _COL_WIDTH_KEYS) or ""),
                    visible=_is_visible(d),
                    sortable=_truthy(_first_present(d, _COL_SORT_KEYS) or ""),
                    source_file=f.rel_path,
                    required=_truthy(d.get("required", "")),
                    editable=_truthy(d.get("editable", "")),
                    ui_type=_infer_ui_type(d),
                    description=_strip_quotes(
                        _first_present(d, _COL_DESC_KEYS) or ""),
                    action=_strip_quotes(_first_present(d, _COL_ACTION_KEYS) or ""),
                    condition=grid_condition,
                    length=length_val,
                ))
    # onSave 검증 결과를 grid column 에도 머지 — data_key 로 매칭.
    if cols:
        from ..legacy_react_ast import parse_file as _pf
        file_sources: dict[str, str] = {}
        for f in closure.files:
            _t, _s, _ = _pf(f.abs_path)
            if _s is not None:
                file_sources[f.rel_path] = _s.decode("utf-8", errors="replace")
        save_validations = _detect_save_validations(closure, file_sources)
        _apply_save_validations_to_grid(cols, save_validations)
    return cols


def _apply_save_validations_to_grid(cols: list[GridColumn],
                                    detected: dict) -> None:
    """onSave 검증 결과 → grid column 의 required / description 머지.

    data_key 의 leaf (``a.b.c`` → ``c``) 또는 header 로 매칭. validation
    rule 들은 GridColumn 에 별도 필드 없으므로 description 에 append
    (이미 description 있으면 ``" / "`` join).
    """
    if not detected or not cols:
        return
    norm_detected = {k.lower(): v for k, v in detected.items()}
    for c in cols:
        keys = []
        if c.data_key:
            keys.append(c.data_key.split(".")[-1].lower())
        if c.header:
            keys.append(re.sub(r"[\W_]+", "", c.header).lower())
        for k in keys:
            info = norm_detected.get(k)
            if not info:
                continue
            if info.get("required"):
                c.required = True
            rules = info.get("rules") or []
            if rules:
                text = " / ".join(rules)
                if c.description:
                    if text not in c.description:
                        c.description = c.description + " / " + text
                else:
                    c.description = text
            break


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
    """hide:true (ag-grid) / hidden:true / visible:false / show:false /
    display:'none' → False, else True."""
    if "hide" in d and _truthy(d["hide"]):
        return False
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
        name = (attrs.get("id") or attrs.get("name") or attrs.get("field") or "")
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


# ─────────────────────────────────────────────────────────────────
# 입력 영역 (input panel) — <table> 기반 입력 폼
# ─────────────────────────────────────────────────────────────────


def _find_input_tables(tree, source: bytes, input_comps,
                       container_tokens) -> list:
    """closure 안 `<table>` element 중 input panel 후보만 반환.

    필터 조건 (false positive 방지 — 사용자 보고: search-area 안 layout
    table 까지 input panel 로 잡혀 'Select 하세요' 라벨이 input panel
    에 중복 표시):
    1. ``<table>`` element 여야 함
    2. children 에 input 컴포넌트가 1개 이상
    3. **search-area / search-form / filter-area / criteria-area container
       안에 있으면 skip** — 이미 search panel 이 담당하는 영역
    4. **`<th>` element 가 1개 이상 있어야** — `<th>` 없는 table 은 보통
       display 용 (legacy layout / grid 등) 이라 input panel 아님

    legacy ``<td>`` 만으로 label 처리하는 케이스는 false negative 가능 —
    그 경우 사용자가 patterns.yaml 로 input panel 후보를 명시할 수 있게
    추후 옵션 (TODO).
    """
    # 1. search-area / 유사 container 찾기 — 그 안 table 은 input panel 에서 제외
    search_containers = _find_search_containers(tree, source, container_tokens)

    out = []
    for n in find_by_type(tree.root_node,
                          {"jsx_opening_element", "jsx_self_closing_element"}):
        nm = child_by_field(n, "name")
        if nm is None:
            continue
        tag = text_of(nm, source).strip().lower()
        if tag != "table":
            continue
        # opening element → wrapping jsx_element
        cur = n.parent if (n.type == "jsx_opening_element" and n.parent) else n
        if cur.type != "jsx_element":
            continue
        # 3. search-area container 안에 있으면 skip
        if any(_node_contains(c, cur) for c in search_containers):
            continue
        # 2 + 4. children 안 input 컴포넌트 + <th> 존재 검사
        has_input = False
        has_th = False
        for sub in walk(cur):
            if sub is cur:
                continue
            if sub.type not in ("jsx_opening_element", "jsx_self_closing_element"):
                continue
            sub_nm = child_by_field(sub, "name")
            if sub_nm is None:
                continue
            sub_tag_raw = text_of(sub_nm, source).strip()
            sub_tag_lower = sub_tag_raw.lower()
            if sub_tag_raw in input_comps:
                has_input = True
            if sub_tag_lower == "th":
                has_th = True
            if has_input and has_th:
                break
        if has_input and has_th:
            out.append(cur)
    return out


def _extract_input_panel_from_table(table_node, source: bytes,
                                    input_comps, option_comps,
                                    rel_path: str, start_order: int
                                    ) -> list[FormField]:
    """1 ``<table>`` → input panel FormField 리스트.

    각 ``<tr>`` 안에서 ``<th>`` (또는 첫 ``<td>`` 텍스트) 를 label,
    그 다음 ``<td>`` 의 input 컴포넌트를 input 으로 페어. 한 ``<tr>`` 에
    여러 label/input 페어가 있어도 순서대로 추출.
    """
    fields: list[FormField] = []
    order = start_order
    # 모든 tr 순회
    for tr in walk(table_node):
        if tr is table_node:
            continue
        if tr.type != "jsx_element":
            continue
        opening = next((c for c in tr.children if c.type == "jsx_opening_element"), None)
        if opening is None:
            continue
        tr_name = child_by_field(opening, "name")
        if tr_name is None or text_of(tr_name, source).strip().lower() != "tr":
            continue
        # tr 의 cells (th / td) 순서대로
        cells: list[tuple[str, object]] = []   # ("label_text" or "input_node", node)
        for child in tr.children:
            if child.type != "jsx_element":
                continue
            opn = next((c for c in child.children if c.type == "jsx_opening_element"), None)
            if opn is None:
                continue
            cnm = child_by_field(opn, "name")
            if cnm is None:
                continue
            ctag = text_of(cnm, source).strip().lower()
            if ctag == "th":
                txt = _find_label_text_in_children(child, exclude=None, source=source)
                if isinstance(txt, tuple):
                    txt = txt[0]
                cells.append(("label", (txt or "").strip()))
            elif ctag == "td":
                # td 안 첫 input 컴포넌트
                input_el = None
                input_tag = ""
                for sub in walk(child):
                    if sub is child:
                        continue
                    if sub.type not in ("jsx_opening_element", "jsx_self_closing_element"):
                        continue
                    sn = child_by_field(sub, "name")
                    if sn is None:
                        continue
                    t = text_of(sn, source).strip()
                    if t in input_comps:
                        input_el = sub
                        input_tag = t
                        break
                # input 없으면 label-only td (label 위치) 로 처리
                if input_el is None:
                    txt = _find_label_text_in_children(child, exclude=None, source=source)
                    if isinstance(txt, tuple):
                        txt = txt[0]
                    cells.append(("label", (txt or "").strip()))
                else:
                    cells.append(("input", (input_el, input_tag)))

        # 페어링 — label 직후 input 페어
        pending_label = ""
        for kind, val in cells:
            if kind == "label":
                if val:
                    pending_label = val
            else:  # input
                input_el, input_tag = val
                attrs = _jsx_attributes(input_el, source)
                ft = _classify_field_type(input_tag, attrs)
                opts = _extract_dropdown_options(input_el, source, option_comps)
                ui = _infer_form_ui_type(input_tag, attrs, ft, opts)
                order += 1
                fields.append(FormField(
                    order=order,
                    label=(pending_label
                           or attrs.get("label")
                           or attrs.get("placeholder")
                           or attrs.get("title")
                           or attrs.get("aria-label")
                           or ""),
                    name=(attrs.get("id") or attrs.get("name")
                          or attrs.get("field") or ""),
                    field_type=ft,
                    required=(attrs.get("required") == "true"
                              or "required" in (attrs.get("className") or "").lower()),
                    default=(attrs.get("defaultValue") or attrs.get("value") or ""),
                    validation=_format_inline_validation(attrs),
                    source_file=rel_path,
                    jsx_tag=input_tag,
                    events=_extract_event_props(attrs),
                    options=opts,
                    placeholder=attrs.get("placeholder", ""),
                    max_length=attrs.get("maxLength", ""),
                    input_data_type=_input_data_type(input_tag, attrs, ft),
                    ui_type=ui,
                    action=_compose_form_action(ft, opts, ui),
                    change_handler=_extract_handler_leaf(attrs.get("onChange", "")),
                    panel_type="input",
                ))
                pending_label = ""
    return fields


# onSave / handleSave / submit 류 핸들러 안 isNull / isNumber / isNegative
# 등 검증 패턴 추출 — input panel field 의 required / validation_rule 채움.

_SAVE_HANDLER_NAMES = (
    "onSave", "handleSave", "save", "doSave", "onSubmit", "handleSubmit",
    "submit", "doSubmit", "onApply", "handleApply", "apply",
    "onConfirm", "handleConfirm", "confirm", "onOk",
)

_ISNULL_RE = re.compile(r"\bis[Nn]ull\s*\(\s*([\w$.]+)\s*\)")

# `if (...) { errorList.push(...) }` 블록 추출 — required 추론 시 진짜
# 에러 누적 패턴인지 확인. 한국 SI 흔한 변수명: errorList / errorMsg /
# errors / errMsg / err / errArr.
_IF_ERROR_PUSH_RE = re.compile(
    r"\bif\s*\(\s*(?P<cond>[^()]*(?:\([^()]*\)[^()]*)*)\s*\)"
    r"\s*\{[^{}]*?\b(?:err(?:or)?(?:s|Msg|List|Arr|Array)?)\b"
    r"\s*\.\s*push\s*\(",
    re.DOTALL,
)

# if 조건 안 field 추출 — 빈/null/undefined check 패턴들.
_COND_FIELD_RES = [
    # X === '' / X === null / X === undefined / X === 0 (or ==, 양변 swap 가능)
    re.compile(
        r"([\w$.]+)\s*(?:===|==)\s*(?:''|\"\"|null|undefined|0)"),
    re.compile(
        r"(?:''|\"\"|null|undefined|0)\s*(?:===|==)\s*([\w$.]+)"),
    # isNull(X) / isEmpty(X) / isBlank(X) — 일반 helper
    re.compile(r"\bis(?:Null|Empty|Blank|Nil)\s*\(\s*([\w$.]+)\s*\)"),
    # !X (truthy 부정) — X 가 식별자일 때만
    re.compile(r"(?<![=!<>])!\s*([\w$.]+)(?!\s*[=!<>])"),
    # X.length === 0 / X.length <= 0
    re.compile(r"([\w$.]+)\s*\.\s*length\s*(?:===|==|<=|<)\s*0\b"),
    # X.trim() === '' / X.trim().length === 0
    re.compile(r"([\w$.]+)\s*\.\s*trim\s*\(\s*\)"),
]

# 무시할 식별자 (조건 안 등장하지만 field 가 아님)
_NOT_A_FIELD = frozenset({
    "true", "false", "null", "undefined", "0", "1",
    "node", "item", "row", "data", "params", "args", "props", "state",
    "this", "self", "obj", "el", "e", "evt", "event",
    "i", "j", "k", "idx", "index",
    # JS builtin / 일반 property — ``X.length === 0`` 의 ``length`` 같은 게
    # 단독 field 로 잡히던 false positive 방지.
    "length", "size", "value", "type", "key",
})


def _extract_fields_from_condition(cond: str) -> set:
    """if 조건 텍스트 → field 이름 set (각 식별자의 leaf segment).

    예: ``node.DATA_TYPE === '' || node.DATA_TYPE === null`` → {'DATA_TYPE'}
        ``isNull(this.state.fab)`` → {'fab'}
        ``!sdpt || sdpt.length === 0`` → {'sdpt'}
    """
    out: set = set()
    for regex in _COND_FIELD_RES:
        for m in regex.finditer(cond):
            token = m.group(1)
            leaf = token.split(".")[-1]
            if leaf and leaf.lower() not in _NOT_A_FIELD and leaf not in _NOT_A_FIELD:
                out.add(leaf)
    return out


_VALIDATION_PATTERN_RES = [
    # (regex, label-format) — label 안 ``{field}`` 는 매칭 그룹 1 로 대체
    (re.compile(r"\bis[Nn]egative\s*\(\s*([\w$.]+)\s*\)"),  "음수 불가"),
    (re.compile(r"\bisNotNumber\s*\(\s*([\w$.]+)\s*\)"),    "숫자만 허용"),
    (re.compile(r"\b(?:!\s*is[Nn]umber|isNaN)\s*\(\s*([\w$.]+)\s*\)"),
                                                            "숫자만 허용"),
    (re.compile(r"\bisNotEmail\s*\(\s*([\w$.]+)\s*\)"),     "이메일 형식"),
    (re.compile(r"([\w$.]+)\s*\.\s*length\s*[<>]=?\s*(\d+)"),
                                                            "길이 제한 ({1})"),
    (re.compile(r"([\w$.]+)\s*<\s*0\b"),                    "음수 불가"),
    (re.compile(r"([\w$.]+)\s*\.\s*test\s*\(\s*([\w$.]+)\s*\)"),
                                                            "정규식 패턴 검증"),
]


def _detect_save_validations(closure, file_sources: dict) -> dict:
    """closure 안 save 류 handler 들에서 검증 패턴 추출.

    Required 추론 — handler body 의 ``if (...) { error.push(...) }`` 패턴.
    조건 안 빈/null check (``X === '' || X === null`` / ``isNull(X)`` /
    ``!X`` / ``X.length === 0`` / ``X.trim() === ''`` 등) 의 field 가
    required.

    Validation rules — isNumber / isNegative / .length [<>] N / .test() 등.

    Returns: ``{field_name: {"required": bool, "rules": [str, ...]}}``
    """
    from ..legacy_react_api_scanner import _locate_handler_body
    out: dict = {}
    for fp, content in file_sources.items():
        for handler_name in _SAVE_HANDLER_NAMES:
            body = _locate_handler_body(content, handler_name)
            if not body:
                continue
            # required — if-block with error push
            for m in _IF_ERROR_PUSH_RE.finditer(body):
                cond = m.group("cond")
                for field in _extract_fields_from_condition(cond):
                    entry = out.setdefault(field, {"required": False, "rules": []})
                    entry["required"] = True
            # legacy isNull(X) — 직접 호출 (if-block 밖에도 있을 수 있음)
            for m in _ISNULL_RE.finditer(body):
                field = m.group(1).split(".")[-1]
                if field.lower() in _NOT_A_FIELD or field in _NOT_A_FIELD:
                    continue
                entry = out.setdefault(field, {"required": False, "rules": []})
                entry["required"] = True
            # 기타 검증 패턴 → rules
            for regex, label in _VALIDATION_PATTERN_RES:
                for m in regex.finditer(body):
                    field = m.group(1).split(".")[-1]
                    entry = out.setdefault(field, {"required": False, "rules": []})
                    # label 에 {1} 자리표시자 있으면 group(2) 로 치환
                    note = label
                    if "{1}" in note and m.lastindex and m.lastindex >= 2:
                        note = note.replace("{1}", m.group(2))
                    if note not in entry["rules"]:
                        entry["rules"].append(note)
    return out


def _apply_save_validations(fields: list[FormField], detected: dict) -> None:
    """onSave 검증 결과를 input panel field 의 required / validation_rule 에 머지."""
    if not detected or not fields:
        return
    # field name (lowercase) 기준 매칭
    for f in fields:
        keys = []
        if f.name:
            keys.append(f.name.lower())
        if f.label:
            # 'SDPT' label 도 isNull(sdpt) 와 매칭되도록
            keys.append(re.sub(r"[\W_]+", "", f.label).lower())
        for k in keys:
            if k in {kk.lower() for kk in detected}:
                # 정확 매칭 key 다시 찾기
                for orig_k, info in detected.items():
                    if orig_k.lower() != k:
                        continue
                    if info.get("required"):
                        f.required = True
                    rules = info.get("rules") or []
                    if rules:
                        text = " / ".join(rules)
                        if f.validation_rule:
                            if text not in f.validation_rule:
                                f.validation_rule = (f.validation_rule
                                                     + " / " + text)
                        else:
                            f.validation_rule = text
                break


def extract_input_panel_fields(closure: ScreenClosure,
                               patterns: dict | None = None
                               ) -> list[FormField]:
    """closure 의 **entry 파일** 안 ``<table>`` 기반 입력 폼 → FormField
    리스트 (panel_type='input'). search panel 과 parallel.

    Entry 파일만 보는 이유 (사용자 보고): closure 가 transitive import 로
    popup / 공통 컴포넌트 / 다른 화면까지 끌고 들어와서 그 안 ``<table>``
    까지 input panel 로 잡히던 false positive 차단. entry 파일에 ``<table>``
    이 없으면 input panel 0건이 정확.

    onSave / handleSave 등 핸들러 안 ``isNull(X)`` 검증 → required,
    그 외 ``isNumber`` / ``< 0`` 등 → validation_rule 머지. (handler body
    검색은 closure 전체 파일 — handler 가 helper / saga 파일에 있을 수
    있으므로.)
    """
    pat = _patterns_section(patterns)
    input_comps = _comp_list(pat, "input_components", DEFAULT_INPUT_COMPONENTS)
    option_comps = _comp_list(pat, "option_components",
                              DEFAULT_OPTION_COMPONENTS)
    container_tokens = _comp_list(pat, "search_containers",
                                  DEFAULT_SEARCH_CONTAINERS)

    from ..legacy_react_ast import parse_file
    fields: list[FormField] = []
    order = 0
    file_sources: dict[str, str] = {}
    # entry 파일만 input panel 후보. (handler 검증 추출은 closure 전체.)
    entry_rel_path = ""
    try:
        from pathlib import Path as _Path
        entry_abs = str(_Path(closure.entry_file).resolve()).replace("\\", "/")
    except Exception:
        entry_abs = ""
    for f in closure.files:
        tree, source, _ = parse_file(f.abs_path)
        if tree is None:
            continue
        file_sources[f.rel_path] = source.decode("utf-8", errors="replace")
        # entry 파일 식별 — absolute path 비교
        try:
            f_abs = str(_Path(f.abs_path).resolve()).replace("\\", "/")
        except Exception:
            f_abs = ""
        is_entry = bool(entry_abs and f_abs and entry_abs == f_abs)
        if not is_entry:
            continue
        entry_rel_path = f.rel_path
        tables = _find_input_tables(tree, source, input_comps, container_tokens)
        for tbl in tables:
            new = _extract_input_panel_from_table(
                tbl, source, input_comps, option_comps,
                f.rel_path, order)
            order += len(new)
            fields.extend(new)

    if fields:
        # cascading clears (search panel 과 동일) — input panel 도 같은 로직
        _detect_cascading_clears(fields, file_sources)
        # onSave 검증 결과 머지
        save_validations = _detect_save_validations(closure, file_sources)
        _apply_save_validations(fields, save_validations)
    return fields
