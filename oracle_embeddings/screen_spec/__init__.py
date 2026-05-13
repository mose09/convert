"""Public API for `screen_spec` — AST 기반 화면 UI 정의서 추출.

사용 예 (다른 모듈에서):
    from oracle_embeddings.screen_spec import extract_screen_spec, write_master_xlsx

    spec = extract_screen_spec(closure, patterns=None)
    write_master_xlsx([spec], output_xlsx)
"""
from .models import (
    ButtonEvent,
    FlowStep,
    FormField,
    GridColumn,
    ScreenSpec,
    Tab,
    ValidationRule,
)
from .extractors import extract_screen_spec
from .excel_writer import write_master_xlsx

__all__ = [
    "ButtonEvent",
    "FlowStep",
    "FormField",
    "GridColumn",
    "ScreenSpec",
    "Tab",
    "ValidationRule",
    "extract_screen_spec",
    "write_master_xlsx",
]
