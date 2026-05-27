"""Dataclasses representing a screen's UI spec, extracted deterministically
from a React closure (entry + import-followed children).

각 dataclass 는 1행 ↔ 1 행위 매핑되어 Excel 시트에 그대로 떨어진다.
LLM 없이 AST 패턴만으로 채워지므로 같은 소스 → 같은 결과 보장.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional


@dataclass
class FormField:
    """검색 패널의 입력 필드 한 건."""
    order: int                        # 순번 (JSX 출현 순서, row-major)
    label: str                        # 라벨 텍스트 (jsx label/placeholder)
    name: str                         # 필드명 (name= / id=)
    field_type: str                   # text / select / date / checkbox / radio / number / ...
    required: bool                    # required prop
    default: str                      # defaultValue / value (literal 만)
    validation: str                   # required / pattern / min / max 인라인 props 요약
    source_file: str                  # closure rel_path
    jsx_tag: str = ""                 # 원본 JSX 컴포넌트 이름 (예: "Select", "DatePicker")
    events: str = ""                  # onChange / onClick / onBlur 등 (공백 구분)
    options: str = ""                 # 드롭다운 Option 자식 value 목록 (예: "Y, N")
    # 화면정의서 9컬럼 표 양식 (grid 와 parallel)
    placeholder: str = ""             # placeholder attr — 표시되면 default 보다 우선 (UI 가시값)
    max_length: str = ""              # maxLength attr — keyboard 입력 자릿수 제한
    input_data_type: str = ""         # 키보드 input 일 때만: String / Number / Date / ""
    ui_type: str = ""                 # "Select(Single)" / "Text Field(Search Box)" / "DatePicker" 등
    action: str = ""                  # 동작 — 단순 dropdown 은 옵션 값 줄바꿈 / 그 외 LLM cascading 판단
    validation_rule: str = ""         # 유효성 규칙 및 비고 — LLM 판단 (계층 cascading 등)
    change_handler: str = ""          # 내부 — onChange 의 leaf handler 이름 (cascading 분석용)


@dataclass
class GridColumn:
    """그리드 컬럼 한 건 (columns=[{...}] 배열의 element)."""
    order: int                        # 순번
    header: str                       # 표시 헤더 (= 필드설명)
    data_key: str                     # 물리명 / 매핑 (dataIndex / field / accessor)
    data_type: str                    # string / number / date / ... (있으면)
    width: str                        # px 또는 '*'
    visible: bool                     # hidden:true / visible:false / display:none
    sortable: bool                    # sorter / sortable
    source_file: str
    # 화면정의서 표 양식 (ag-grid columnDef 추가 prop 매핑)
    required: bool = False            # 필수 여부 (custom prop)
    editable: bool = False            # editable: true → 'E', false → 'R'
    ui_type: str = ""                 # cellRenderer / cellEditor → "Text Field(Basic)" 등
    description: str = ""             # description / tooltipField (반환값 설명)
    action: str = ""                  # onCellClicked → "클릭시 X 호출" 등
    # 분기 정보 — JSX conditional ancestor (예: ``tab === 'A'``, ``mode === 'edit'``).
    # 같은 화면에 조건별로 다른 grid 가 render 되는 경우 grid 별 condition 으로
    # group 가능. 빈 값이면 무조건 render (top-level).
    condition: str = ""
    # 길이 — ag-grid 의 ``cellEditor: 'CustomEditor'`` 같은 커스텀 에디터
    # import 파일에서 ``maxLength={N}`` 추출. 숫자 문자열 ("10" 등) 또는 빈 값.
    length: str = ""


@dataclass
class Tab:
    """탭 한 건."""
    order: int
    label: str                        # 탭 라벨
    panel_component: str              # 탭 내용 컴포넌트 이름 (식별 가능 시)
    source_file: str


@dataclass
class FlowStep:
    """한 이벤트 안의 step 한 건 (handler body 순회 결과)."""
    step: int                         # 1-base 순번
    action: str                       # 'api' / 'navigate' / 'popup' / 'state' / 'condition' / 'call'
    detail: str                       # 사람 읽기용 요약 ("POST /api/orders/search")
    condition: str                    # 직전 if/else 조건 (있으면)


@dataclass
class ButtonEvent:
    """버튼 + 그 onClick 핸들러의 동작."""
    trigger_label: str                # 버튼 텍스트
    trigger_kind: str                 # 'button' / 'jsx_event' (onSubmit/onChange)
    handler_name: str                 # onClick 가 가리키는 함수 이름
    api_calls: list[str] = field(default_factory=list)        # 'POST /api/...' 형태
    screen_calls: list[str] = field(default_factory=list)     # 'navigate / window.open / Link'
    notes: str = ""                   # 기타
    source_file: str = ""
    flow: list[FlowStep] = field(default_factory=list)        # handler body step list


@dataclass
class ValidationRule:
    """검증 규칙 한 건."""
    field: str                        # 필드명 또는 yup schema 키
    rule: str                         # 규칙 이름 (required / pattern / min / max / matches)
    detail: str                       # rule 인자값 (regex 패턴 / 숫자 / ...)
    message: str                      # 사용자에게 노출되는 메시지 (literal 있을 때만)
    source: str                       # 'jsx_prop' / 'yup' / 'zod' / 'joi' / 'manual'
    source_file: str


@dataclass
class ScreenSpec:
    """한 화면 (= 한 closure) 의 모든 UI 정의서 데이터."""
    screen_id: str                    # 캡처 stem 또는 entry component 이름
    entry_file: str                   # closure entry rel_path
    closure_file_count: int
    closure_files: list[str]          # rel_paths
    closure_truncated: bool
    closure_tokens: int

    form_fields: list[FormField] = field(default_factory=list)
    grid_columns: list[GridColumn] = field(default_factory=list)
    tabs: list[Tab] = field(default_factory=list)
    buttons: list[ButtonEvent] = field(default_factory=list)
    validations: list[ValidationRule] = field(default_factory=list)

    # closure 가 이미 제공하는 factual 정보 (build_closure 호출 결과 그대로)
    api_calls_factual: list[dict] = field(default_factory=list)
    popup_refs_factual: list[dict] = field(default_factory=list)

    # 진단/메타
    notes: str = ""
