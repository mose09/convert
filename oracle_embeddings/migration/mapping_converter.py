"""LLM-aided converter: 임의 양식의 AS-IS↔TO-BE 매핑 .md → column_mapping.yaml.

프로젝트마다 migration 매핑 정의 문서 양식이 제각각이다. 어떤 팀은 파이프
테이블 하나에 (AS-IS 테이블, AS-IS 컬럼, TO-BE 테이블, TO-BE 컬럼, 변환 규칙) 을
한 줄씩 쓰고, 어떤 팀은 "CUST → CUSTOMER_MASTER (VARCHAR2 → DATE, TO_DATE 래핑)"
같은 자연어 설명을 섞어놓는다. 이 모듈은 그 자유로운 .md 를 LLM 으로 읽어
스펙 §4 구조의 ``column_mapping.yaml`` 로 정규화한다.

흐름:
    1. 입력 .md 텍스트 로드 (utf-8)
    2. LLM 에 원문 + 타겟 YAML 스키마 설명 + 6 가지 kind 예제 전달
    3. LLM 이 테이블/컬럼 단위 YAML 구조 (json) 반환
    4. ``mapping_loader.load_mapping_collect`` 로 구조 검증 → 에러 있으면
       출력 + 계속 (사용자가 수동 수정할 수 있게)
    5. 주석 달린 YAML 로 emit

LLM 없이도 동작하는 ``--no-llm`` 휴리스틱 경로도 제공:
    - 파이프 테이블만 인식, 5가지 고정 컬럼 세트 (AS-IS 테이블/컬럼,
      TO-BE 테이블/컬럼, 비고) 중 헤더 synonym 으로 매칭
    - 각 행을 kind 분류: type 컬럼이 있으면 type_convert, 비어있으면 rename
"""
from __future__ import annotations

import json
import logging
import os
import re
import textwrap
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def convert_mapping_md(
    md_path: Path,
    output_path: Path,
    *,
    use_llm: bool = True,
    config: Optional[Dict[str, Any]] = None,
) -> Path:
    """Convert an AS-IS↔TO-BE mapping markdown into ``column_mapping.yaml``.

    Returns the absolute output path. Raises on I/O errors; LLM / parse
    failures degrade to heuristic output instead of raising.
    """

    text = Path(md_path).read_text(encoding="utf-8")

    data: Optional[Dict[str, Any]] = None
    if use_llm:
        data = _call_llm(text, config or {})
    if data is None:
        logger.info("Falling back to heuristic table parser")
        data = _heuristic_parse(text)

    yaml_text = _format_yaml(data)
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(yaml_text, encoding="utf-8")

    # Validate — surface any errors but don't raise (user can fix)
    _print_validation(out)
    return out.resolve()


# ---------------------------------------------------------------------------
# LLM path
# ---------------------------------------------------------------------------


_SYSTEM_PROMPT = (
    "You are converting an AS-IS/TO-BE column mapping document into a "
    "structured YAML for Oracle SQL schema migration. Return JSON only — "
    "no prose, no code fences. Use UPPERCASE for Oracle identifiers."
)


_USER_PROMPT_TEMPLATE = """## Target YAML shape
Produce JSON with exactly this shape (omit optional fields when not applicable):

{{
  "version": "1.0",
  "default_schema": {{"as_is": "LEGACY", "to_be": "NEW"}},
  "options": {{
    "emit_column_comments": false,
    "comment_scope": ["select", "update", "insert"],
    "comment_source": "terms_dictionary",
    "unknown_table_action": "warn"
  }},
  "tables": [
    {{"type": "rename", "as_is": "CUST", "to_be": "CUSTOMER_MASTER"}},
    {{"type": "split",  "as_is": "ORDER_HIST", "to_be": ["ORDER_HEADER", "ORDER_ITEM"],
      "discriminator_column": "HIST_TYPE",
      "discriminator_map": {{"H": "ORDER_HEADER", "I": "ORDER_ITEM"}}}},
    {{"type": "merge",  "as_is": ["USER_BASIC", "USER_DETAIL"], "to_be": "USER",
      "join_condition": "USER_BASIC.USER_ID = USER_DETAIL.USER_ID"}},
    {{"type": "drop",   "as_is": "OBSOLETE_TBL", "to_be": null}}
  ],
  "columns": [
    {{"kind": "rename",
      "as_is": {{"table": "CUST", "column": "CUST_NM"}},
      "to_be": {{"table": "CUSTOMER_MASTER", "column": "CUSTOMER_NAME"}}}},
    {{"kind": "type_convert",
      "as_is": {{"table": "CUST", "column": "REG_DT", "type": "VARCHAR2(8)"}},
      "to_be": {{"table": "CUSTOMER_MASTER", "column": "REGISTER_DATE", "type": "DATE"}},
      "transform": {{"read": "TO_DATE({{src}}, 'YYYYMMDD')",
                    "write": "TO_CHAR({{src}}, 'YYYYMMDD')",
                    "where": "TO_DATE({{src}}, 'YYYYMMDD')"}}}},
    {{"kind": "split",
      "as_is": {{"table": "CUST", "column": "FULL_NAME"}},
      "to_be": [
        {{"table": "CUSTOMER_MASTER", "column": "FIRST_NAME",
          "transform_select": "SUBSTR({{src}}, 1, INSTR({{src}}, ' ')-1)"}},
        {{"table": "CUSTOMER_MASTER", "column": "LAST_NAME",
          "transform_select": "SUBSTR({{src}}, INSTR({{src}}, ' ')+1)"}}
      ],
      "reverse": "{{FIRST_NAME}} || ' ' || {{LAST_NAME}}"}},
    {{"kind": "merge",
      "as_is": [
        {{"table": "EVT", "column": "YYYY"}},
        {{"table": "EVT", "column": "MM"}},
        {{"table": "EVT", "column": "DD"}}
      ],
      "to_be": {{"table": "EVENT", "column": "EVENT_DATE", "type": "DATE"}},
      "transform": {{"combine": "TO_DATE({{YYYY}}||{{MM}}||{{DD}}, 'YYYYMMDD')"}}}},
    {{"kind": "value_map",
      "as_is": {{"table": "CUST", "column": "USE_YN"}},
      "to_be": {{"table": "CUSTOMER_MASTER", "column": "IS_ACTIVE", "type": "NUMBER(1)"}},
      "value_map": {{"Y": 1, "N": 0}},
      "default_value": 0}},
    {{"kind": "drop",
      "as_is": {{"table": "CUST", "column": "OBSOLETE_FLAG"}},
      "to_be": null,
      "action": "drop_with_warning"}}
  ]
}}

## Rules
- 1:1 same-name/type change → kind="rename"
- AS-IS 타입 ≠ TO-BE 타입 → kind="type_convert" with TO_DATE/TO_CHAR/TO_NUMBER
  등 Oracle 함수 래핑 (VARCHAR2 ↔ DATE 패턴을 자주 봄)
- 여러 AS-IS 컬럼 → 단일 TO-BE 컬럼 = kind="merge" (combine 표현식 필수)
- 단일 AS-IS 컬럼 → 여러 TO-BE 컬럼 = kind="split" (reverse 표현식 권장)
- Y/N → 1/0 같은 코드표 = kind="value_map"
- TO-BE 에서 삭제 = kind="drop" (to_be: null)
- tables[] 배열은 columns[] 의 as_is.table 전부를 반드시 포함해야 함 (columns
  가 참조하는 AS-IS 테이블마다 대응 entry 필수 — 기본 rename 이면 간단 entry)
- 애매하면 kind="rename" + 주의사항을 json 루트 "notes" 필드에 남기기

## INPUT (raw markdown)
{raw_md}

## OUTPUT (JSON only, no code fences)
"""


def _call_llm(raw_md: str, config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        from openai import OpenAI
    except ImportError:
        logger.warning("openai SDK unavailable — falling back to heuristic")
        return None

    llm_cfg = config.get("llm", {})
    api_key = os.environ.get("LLM_API_KEY") or llm_cfg.get("api_key", "ollama")
    api_base = (
        os.environ.get("LLM_API_BASE")
        or llm_cfg.get("api_base", "http://localhost:11434/v1")
    )
    model = (
        os.environ.get("PATTERN_LLM_MODEL")
        or os.environ.get("LLM_MODEL")
        or llm_cfg.get("model", "llama3")
    )
    client = OpenAI(api_key=api_key, base_url=api_base)

    user_prompt = _USER_PROMPT_TEMPLATE.format(raw_md=raw_md[:40000])

    for attempt in range(3):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.1,
                timeout=300,
            )
            text = (resp.choices[0].message.content or "").strip()
            return _extract_and_parse_json(text)
        except json.JSONDecodeError:
            wait = 2 ** attempt
            logger.warning("LLM returned non-JSON (attempt %d); retry in %ds", attempt + 1, wait)
            time.sleep(wait)
        except Exception as exc:
            logger.error("LLM call failed: %s", exc)
            return None
    return None


def _extract_and_parse_json(text: str) -> Dict[str, Any]:
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence:
        text = fence.group(1).strip()
    else:
        l, r = text.find("{"), text.rfind("}")
        if l >= 0 and r > l:
            text = text[l:r + 1]
    return json.loads(text)


# ---------------------------------------------------------------------------
# Heuristic fallback (no LLM)
# ---------------------------------------------------------------------------


# Header synonyms — case-insensitive, upper applied before matching.
_HEADER_SYNONYMS = {
    "as_is_table": {"AS-IS 테이블", "AS IS 테이블", "AS-IS TABLE", "ORIGINAL TABLE",
                    "OLD TABLE", "ASIS TABLE", "AS_IS_TABLE"},
    "as_is_column": {"AS-IS 컬럼", "AS IS 컬럼", "AS-IS COLUMN", "ORIGINAL COLUMN",
                     "OLD COLUMN", "ASIS COLUMN", "AS_IS_COLUMN", "AS-IS 칼럼"},
    "as_is_type": {"AS-IS 타입", "AS-IS TYPE", "OLD TYPE", "ORIGINAL TYPE"},
    "to_be_table": {"TO-BE 테이블", "TO BE 테이블", "TO-BE TABLE", "NEW TABLE",
                    "TOBE TABLE", "TO_BE_TABLE"},
    "to_be_column": {"TO-BE 컬럼", "TO BE 컬럼", "TO-BE COLUMN", "NEW COLUMN",
                     "TOBE COLUMN", "TO_BE_COLUMN", "TO-BE 칼럼"},
    "to_be_type": {"TO-BE 타입", "TO-BE TYPE", "NEW TYPE"},
    "note": {"비고", "NOTE", "NOTES", "DESCRIPTION", "설명", "변환", "RULE",
             "변환규칙", "변환 규칙"},
}


def _heuristic_parse(text: str) -> Dict[str, Any]:
    """Parse pipe-table only, no LLM.

    Recognised shapes per row: AS-IS table | AS-IS col | TO-BE table | TO-BE col
    (+ optional types + notes). Everything else becomes kind=rename with a
    note for the user.
    """
    rows = _extract_pipe_rows(text)
    if not rows:
        return {
            "version": "1.0",
            "default_schema": {"as_is": "LEGACY", "to_be": "NEW"},
            "tables": [],
            "columns": [],
            "notes": "no recognisable pipe table found — please edit manually",
        }

    header = rows[0]
    col_map = _detect_columns(header)
    if not {"as_is_table", "as_is_column", "to_be_table", "to_be_column"} <= col_map.keys():
        return {
            "version": "1.0",
            "default_schema": {"as_is": "LEGACY", "to_be": "NEW"},
            "tables": [],
            "columns": [],
            "notes": f"missing required headers. detected {sorted(col_map)}; "
                     "need as_is_table / as_is_column / to_be_table / to_be_column",
        }

    tables: Dict[str, Dict[str, Any]] = {}
    columns: List[Dict[str, Any]] = []

    for r in rows[1:]:
        as_is_tbl = (r[col_map["as_is_table"]] or "").strip().upper()
        as_is_col = (r[col_map["as_is_column"]] or "").strip().upper()
        to_be_tbl = (r[col_map["to_be_table"]] or "").strip().upper()
        to_be_col = (r[col_map["to_be_column"]] or "").strip().upper()
        if not as_is_tbl or not as_is_col:
            continue

        # Simple table mapping
        if as_is_tbl not in tables:
            if not to_be_tbl or to_be_tbl == as_is_tbl:
                tables[as_is_tbl] = {"type": "rename", "as_is": as_is_tbl, "to_be": as_is_tbl}
            else:
                tables[as_is_tbl] = {"type": "rename", "as_is": as_is_tbl, "to_be": to_be_tbl}

        if not to_be_col or to_be_col == "-":
            columns.append({
                "kind": "drop",
                "as_is": {"table": as_is_tbl, "column": as_is_col},
                "to_be": None,
                "action": "drop_with_warning",
            })
            continue

        as_is_type = _cell(r, col_map, "as_is_type")
        to_be_type = _cell(r, col_map, "to_be_type")

        entry: Dict[str, Any] = {
            "kind": "rename",
            "as_is": {"table": as_is_tbl, "column": as_is_col},
            "to_be": {"table": to_be_tbl or as_is_tbl, "column": to_be_col},
        }
        if as_is_type:
            entry["as_is"]["type"] = as_is_type
        if to_be_type:
            entry["to_be"]["type"] = to_be_type
        if as_is_type and to_be_type and as_is_type.upper() != to_be_type.upper():
            entry["kind"] = "type_convert"
            entry["transform"] = {"read": "{src}", "where": "{src}"}

        columns.append(entry)

    return {
        "version": "1.0",
        "default_schema": {"as_is": "LEGACY", "to_be": "NEW"},
        "tables": list(tables.values()),
        "columns": columns,
        "notes": "generated by --no-llm heuristic; please review transform expressions",
    }


def _extract_pipe_rows(text: str) -> List[List[str]]:
    """Return cleaned rows from all pipe tables, concatenated."""
    out: List[List[str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|") or not line.endswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        # Skip divider rows like |---|---|
        if all(set(c) <= {"-", ":", " ", ""} for c in cells):
            continue
        if cells and any(cells):
            out.append(cells)
    return out


def _detect_columns(header_row: List[str]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for i, cell in enumerate(header_row):
        h = (cell or "").strip().upper()
        for key, synonyms in _HEADER_SYNONYMS.items():
            if h in {s.upper() for s in synonyms}:
                out.setdefault(key, i)
                break
    return out


def _cell(row: List[str], col_map: Dict[str, int], key: str) -> str:
    idx = col_map.get(key)
    if idx is None or idx >= len(row):
        return ""
    return (row[idx] or "").strip()


# ---------------------------------------------------------------------------
# YAML emission
# ---------------------------------------------------------------------------


def _format_yaml(data: Dict[str, Any]) -> str:
    """Emit a human-readable column_mapping.yaml (comments + grouping).

    We write YAML by hand instead of ``yaml.dump`` so the output mirrors the
    template layout — comments, blank lines between sections, the kind label
    before each column entry.
    """

    lines: List[str] = []
    lines.append("# ============================================================")
    lines.append("# column_mapping.yaml — convert-mapping 자동 생성")
    lines.append("# ============================================================")
    lines.append("# 아래 내용을 검토 후 필요 시 수동 조정하세요.")
    lines.append("# - transform 표현식은 프로젝트 컨벤션에 맞게 정확한 함수로 조정")
    lines.append("# - 삭제 컬럼 (kind=drop) 은 action=drop_with_warning 이 기본")
    lines.append("# - split / merge 는 discriminator_column / join_condition 필수 확인")
    lines.append("")

    notes = data.get("notes")
    if notes:
        lines.append(f"# NOTE (from converter): {notes}")
        lines.append("")

    lines.append(f'version: "{data.get("version", "1.0")}"')
    lines.append("")

    ds = data.get("default_schema", {}) or {}
    lines.append("default_schema:")
    lines.append(f'  as_is: "{ds.get("as_is", "LEGACY")}"')
    lines.append(f'  to_be: "{ds.get("to_be", "NEW")}"')
    lines.append("")

    options = data.get("options") or {}
    if not options:
        options = {
            "emit_column_comments": False,
            "comment_scope": ["select", "update", "insert"],
            "comment_source": "terms_dictionary",
            "unknown_table_action": "warn",
        }
    lines.append("options:")
    for k, v in options.items():
        lines.append(f"  {k}: {_scalar(v)}")
    lines.append("")

    lines.append("tables:")
    for t in data.get("tables", []):
        lines.extend(_format_table(t))
    lines.append("")

    lines.append("columns:")
    for c in data.get("columns", []):
        lines.extend(_format_column(c))

    return "\n".join(lines) + "\n"


def _format_table(t: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    typ = t.get("type", "rename")
    out.append(f"  # type: {typ}")
    # as_is/to_be may be str or list
    as_is = t.get("as_is")
    to_be = t.get("to_be")
    out.append(f"  - type: {typ}")
    out.append(f"    as_is: {_scalar(as_is)}")
    out.append(f"    to_be: {_scalar(to_be)}")
    if t.get("discriminator_column"):
        out.append(f"    discriminator_column: {_scalar(t['discriminator_column'])}")
    if t.get("discriminator_map"):
        out.append("    discriminator_map:")
        for k, v in t["discriminator_map"].items():
            out.append(f'      "{k}": {_scalar(v)}')
    if t.get("join_condition"):
        out.append(f'    join_condition: "{t["join_condition"]}"')
    return out


def _format_column(c: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    kind = c.get("kind", "rename")
    out.append(f"  # kind: {kind}")
    out.append("  - as_is:")
    as_is = c.get("as_is")
    if isinstance(as_is, list):
        for r in as_is:
            out.append(f"      - {{ table: {_scalar(r.get('table'))}, "
                       f"column: {_scalar(r.get('column'))}"
                       + (f", type: {_scalar(r['type'])}" if r.get("type") else "")
                       + " }")
    else:
        out[-1] = "  - as_is: " + _inline_ref(as_is)
    to_be = c.get("to_be")
    if to_be is None:
        out.append("    to_be: null")
    elif isinstance(to_be, list):
        out.append("    to_be:")
        for tgt in to_be:
            out.append(f"      - table: {_scalar(tgt.get('table'))}")
            out.append(f"        column: {_scalar(tgt.get('column'))}")
            if tgt.get("transform_select"):
                out.append(f'        transform_select: "{tgt["transform_select"]}"')
    else:
        out.append("    to_be: " + _inline_ref(to_be))
    if c.get("transform"):
        out.append("    transform:")
        for k, v in c["transform"].items():
            out.append(f'      {k}: "{v}"')
    if c.get("reverse"):
        out.append(f'    reverse: "{c["reverse"]}"')
    if c.get("value_map"):
        out.append("    value_map: {"
                   + ", ".join(f'"{k}": {_scalar(v)}' for k, v in c["value_map"].items())
                   + "}")
    if c.get("default_value") is not None:
        out.append(f"    default_value: {_scalar(c['default_value'])}")
    if c.get("action") and c["action"] != "convert":
        out.append(f"    action: {c['action']}")
    return out


def _inline_ref(ref: Any) -> str:
    if not isinstance(ref, dict):
        return _scalar(ref)
    parts = [
        f"table: {_scalar(ref.get('table'))}",
        f"column: {_scalar(ref.get('column'))}",
    ]
    if ref.get("type"):
        parts.append(f"type: {_scalar(ref['type'])}")
    return "{ " + ", ".join(parts) + " }"


def _scalar(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_scalar(x) for x in v) + "]"
    s = str(v)
    # Quote if contains special chars or looks ambiguous
    if re.match(r"^[A-Za-z_][A-Za-z0-9_.,() -]*$", s) and ":" not in s:
        return s
    return '"' + s.replace('"', '\\"') + '"'


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _print_validation(yaml_path: Path) -> None:
    try:
        from .mapping_loader import load_mapping_collect
    except ImportError:
        return
    mapping, errs = load_mapping_collect(yaml_path)
    if not errs:
        print(f"  Validation: OK ({len(mapping.tables)} tables, "
              f"{len(mapping.columns)} columns)")
        return
    print(f"  Validation: {len(errs)} error(s) — 검토 후 수정 필요:")
    for e in errs[:10]:
        print(f"    - {e}")
    if len(errs) > 10:
        print(f"    ... ({len(errs) - 10} more)")
