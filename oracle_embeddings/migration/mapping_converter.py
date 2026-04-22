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

## 9-컬럼 flat 매핑 포맷 (사용자 표준 양식)

입력이 아래 9-컬럼 파이프 테이블이면 한 행 = 한 컬럼 매핑으로 1:1 처리:
    asis_table | asis_column | asis_column_type
  | tobe_table | tobe_table_comment
  | tobe_column | tobe_column_type | tobe_column_comment
  | remark

처리 규칙:
- 같은 asis_table 의 첫 행에서 tables[] 항목 1개 생성 (type=rename, to_be 사용)
- tobe_table_comment / tobe_column_comment 는 comment 필드로 보존
  (tables[].comment, columns[].to_be.comment) — 추후 한글 주석 자동 삽입용
- type 페어가 분명한 변환 패턴이면 transform 자동 채우기:
    VARCHAR2(8)/CHAR(8) ↔ DATE   → TO_DATE/TO_CHAR with 'YYYYMMDD'
    VARCHAR2(14) → TIMESTAMP      → TO_TIMESTAMP with 'YYYYMMDDHH24MISS'
    VARCHAR2 ↔ NUMBER             → TO_NUMBER/TO_CHAR
- remark 에 split / merge / Y/N 매핑 같은 키워드가 보이면 needs_human_review
  로 분류하지 말고, kind 만 추정한 후 columns[].note 에 원문 remark 를 남겨
  사용자가 검토하도록 함

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
                    "OLD TABLE", "ASIS TABLE", "AS_IS_TABLE", "ASIS_TABLE",
                    "기존 테이블", "기존테이블", "현행 테이블", "원본 테이블"},
    "as_is_column": {"AS-IS 컬럼", "AS IS 컬럼", "AS-IS COLUMN", "ORIGINAL COLUMN",
                     "OLD COLUMN", "ASIS COLUMN", "AS_IS_COLUMN", "AS-IS 칼럼",
                     "ASIS_COLUMN", "기존 컬럼", "기존컬럼", "현행 컬럼"},
    "as_is_type": {"AS-IS 타입", "AS-IS TYPE", "OLD TYPE", "ORIGINAL TYPE",
                   "ASIS_COLUMN_TYPE", "AS_IS_COLUMN_TYPE", "기존 타입", "기존타입"},
    "as_is_table_comment": {"ASIS_TABLE_COMMENT", "AS_IS_TABLE_COMMENT",
                            "AS-IS 테이블 코멘트", "기존 테이블 설명"},
    "as_is_column_comment": {"ASIS_COLUMN_COMMENT", "AS_IS_COLUMN_COMMENT",
                             "AS-IS 컬럼 코멘트", "기존 컬럼 설명"},
    "to_be_table": {"TO-BE 테이블", "TO BE 테이블", "TO-BE TABLE", "NEW TABLE",
                    "TOBE TABLE", "TO_BE_TABLE", "TOBE_TABLE",
                    "신규 테이블", "신규테이블", "대상 테이블", "변경 테이블"},
    "to_be_column": {"TO-BE 컬럼", "TO BE 컬럼", "TO-BE COLUMN", "NEW COLUMN",
                     "TOBE COLUMN", "TO_BE_COLUMN", "TO-BE 칼럼", "TOBE_COLUMN",
                     "신규 컬럼", "신규컬럼", "대상 컬럼"},
    "to_be_type": {"TO-BE 타입", "TO-BE TYPE", "NEW TYPE", "TOBE_COLUMN_TYPE",
                   "TO_BE_COLUMN_TYPE", "신규 타입", "신규타입"},
    "to_be_table_comment": {"TOBE_TABLE_COMMENT", "TO_BE_TABLE_COMMENT",
                            "TO-BE 테이블 코멘트", "신규 테이블 설명",
                            "테이블 한글", "테이블한글", "테이블설명"},
    "to_be_column_comment": {"TOBE_COLUMN_COMMENT", "TO_BE_COLUMN_COMMENT",
                             "TO-BE 컬럼 코멘트", "신규 컬럼 설명",
                             "컬럼 한글", "컬럼한글", "컬럼설명"},
    "note": {"비고", "NOTE", "NOTES", "DESCRIPTION", "설명", "변환", "RULE",
             "변환규칙", "변환 규칙", "REMARK", "REMARKS"},
}


def _heuristic_parse(text: str) -> Dict[str, Any]:
    """Parse pipe-table only, no LLM.

    Recognised shapes per row: AS-IS table | AS-IS col | TO-BE table | TO-BE col
    (+ optional types, comments, notes). Everything else becomes kind=rename
    with a note for the user.

    9-컬럼 flat 매핑 포맷 (사용자 워크플로우):
        | asis_table | asis_column | asis_column_type
        | tobe_table | tobe_table_comment
        | tobe_column | tobe_column_type | tobe_column_comment
        | remark |
    type 페어가 잘 알려진 변환 패턴 (VARCHAR2(8) → DATE 등) 이면 transform
    템플릿 자동 추론. 코멘트 필드는 yaml 의 ``comment`` 로 보존되어 추후
    comment_injector 가 한글 주석 소스로 사용 가능 (Phase 2).
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

        to_be_table_comment = _cell(r, col_map, "to_be_table_comment")

        # Simple table mapping (한 번만 등록, table_comment 가 있으면 첨부)
        if as_is_tbl not in tables:
            tm: Dict[str, Any] = {
                "type": "rename",
                "as_is": as_is_tbl,
                "to_be": (to_be_tbl or as_is_tbl),
            }
            if to_be_table_comment:
                tm["comment"] = to_be_table_comment
            tables[as_is_tbl] = tm
        else:
            # 이후 행에서 같은 테이블의 코멘트가 비어있지 않게 등장하면 보강
            if to_be_table_comment and "comment" not in tables[as_is_tbl]:
                tables[as_is_tbl]["comment"] = to_be_table_comment

        # drop 행
        if not to_be_col or to_be_col in {"-", "DROP", "삭제"}:
            entry_drop: Dict[str, Any] = {
                "kind": "drop",
                "as_is": {"table": as_is_tbl, "column": as_is_col},
                "to_be": None,
                "action": "drop_with_warning",
            }
            note = _cell(r, col_map, "note")
            if note:
                entry_drop["note"] = note
            columns.append(entry_drop)
            continue

        as_is_type = _cell(r, col_map, "as_is_type")
        to_be_type = _cell(r, col_map, "to_be_type")
        to_be_column_comment = _cell(r, col_map, "to_be_column_comment")
        note = _cell(r, col_map, "note")

        entry: Dict[str, Any] = {
            "kind": "rename",
            "as_is": {"table": as_is_tbl, "column": as_is_col},
            "to_be": {"table": (to_be_tbl or as_is_tbl), "column": to_be_col},
        }
        if as_is_type:
            entry["as_is"]["type"] = as_is_type
        if to_be_type:
            entry["to_be"]["type"] = to_be_type
        if to_be_column_comment:
            entry["to_be"]["comment"] = to_be_column_comment

        if as_is_type and to_be_type and as_is_type.upper() != to_be_type.upper():
            a_kind, _ = _simplify_type(as_is_type)
            b_kind, _ = _simplify_type(to_be_type)
            if a_kind == b_kind:
                # 같은 base 타입 (예: VARCHAR2(100) → VARCHAR2(200)) 은 길이/
                # precision 만 다른 케이스이므로 transform 불필요. rename 유지.
                pass
            else:
                entry["kind"] = "type_convert"
                tx = _infer_type_transform(as_is_type, to_be_type)
                if tx:
                    entry["transform"] = tx
                else:
                    # 미지원 type 페어. ⚠ 마커로 사용자 검토 필요 표시 +
                    # 기본 no-op transform 으로 시작 (loader 통과시키기 위함).
                    entry["transform"] = {
                        "read":  "{src}",
                        "write": "{src}",
                        "where": "{src}",
                    }
                    hint = (f"⚠ unrecognised type pair {as_is_type} → {to_be_type}; "
                            "please supply transform.read / write / where manually.")
                    note = (note + " | " + hint) if note else hint

        if note:
            entry["note"] = note

        columns.append(entry)

    return {
        "version": "1.0",
        "default_schema": {"as_is": "LEGACY", "to_be": "NEW"},
        "tables": list(tables.values()),
        "columns": columns,
        "notes": "generated by --no-llm heuristic; please review transform expressions"
                 " and any rows tagged with ⚠",
    }


# ---------------------------------------------------------------------------
# Type-pair → transform template heuristic
# ---------------------------------------------------------------------------
#
# 잘 알려진 Oracle 타입 페어에 대한 기본 변환 템플릿. 폐쇄망 LLM 가
# 없거나 LLM 응답이 신뢰 어려울 때 fallback. 매칭 실패 시 None 을 반환해
# 호출 측이 ``⚠`` 마커 단 후 사용자 수동 수정을 요청.
#
# 형식:  (as_is_kind, to_be_kind) → transform spec
#   as_is_kind / to_be_kind 는 ``_simplify_type`` 으로 정규화 (괄호 안의
#   precision 은 별도 매개로 처리).


_TYPE_TRANSFORMS: Dict[Tuple[str, str], Dict[str, str]] = {
    # 8자리 문자열 (YYYYMMDD) → DATE
    ("VARCHAR2_8", "DATE"): {
        "read":  "TO_DATE({src}, 'YYYYMMDD')",
        "write": "TO_DATE({src}, 'YYYYMMDD')",
        "where": "TO_CHAR({src}, 'YYYYMMDD')",
    },
    ("CHAR_8", "DATE"): {
        "read":  "TO_DATE({src}, 'YYYYMMDD')",
        "write": "TO_DATE({src}, 'YYYYMMDD')",
        "where": "TO_CHAR({src}, 'YYYYMMDD')",
    },
    # 14자리 문자열 (YYYYMMDDHHMISS) → TIMESTAMP
    ("VARCHAR2_14", "TIMESTAMP"): {
        "read":  "TO_TIMESTAMP({src}, 'YYYYMMDDHH24MISS')",
        "write": "TO_TIMESTAMP({src}, 'YYYYMMDDHH24MISS')",
        "where": "TO_CHAR({src}, 'YYYYMMDDHH24MISS')",
    },
    # DATE → TIMESTAMP
    ("DATE", "TIMESTAMP"): {
        "read":  "CAST({src} AS TIMESTAMP)",
        "write": "CAST({src} AS TIMESTAMP)",
        "where": "CAST({src} AS TIMESTAMP)",
    },
    # 문자 ↔ 숫자
    ("VARCHAR2", "NUMBER"): {
        "read":  "TO_NUMBER({src})",
        "write": "TO_CHAR({src})",
        "where": "TO_CHAR({src})",
    },
    ("NUMBER", "VARCHAR2"): {
        "read":  "TO_CHAR({src})",
        "write": "TO_NUMBER({src})",
        "where": "TO_NUMBER({src})",
    },
    # CHAR ↔ VARCHAR2 는 사실상 no-op — transform 없이 rename 처리하는 게
    # 깔끔하므로 Empty 가 아닌 None 신호를 위해 여기 등록하지 않음.
}


_TYPE_LEN_RE = re.compile(r"^([A-Z][A-Z0-9_]*)\s*\(\s*(\d+)", re.IGNORECASE)


def _simplify_type(t: str) -> Tuple[str, Optional[int]]:
    """``VARCHAR2(8)`` → ``("VARCHAR2", 8)``, ``DATE`` → ``("DATE", None)``."""
    if not t:
        return ("", None)
    m = _TYPE_LEN_RE.match(t.strip())
    if m:
        return (m.group(1).upper(), int(m.group(2)))
    return (t.strip().upper(), None)


def _infer_type_transform(as_is_type: str, to_be_type: str) -> Optional[Dict[str, str]]:
    """Look up a default transform spec for a type pair, or None on miss."""
    a_kind, a_len = _simplify_type(as_is_type)
    b_kind, _ = _simplify_type(to_be_type)
    # length-aware 키 우선 시도
    if a_len is not None:
        keyed = _TYPE_TRANSFORMS.get((f"{a_kind}_{a_len}", b_kind))
        if keyed:
            return dict(keyed)
    # length-agnostic
    return dict(_TYPE_TRANSFORMS.get((a_kind, b_kind))) if (a_kind, b_kind) in _TYPE_TRANSFORMS else None


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
    if t.get("comment"):
        out.append(f"    comment: {_scalar(t['comment'])}")
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
    if c.get("note"):
        # 줄바꿈 / 따옴표 정제 후 한 줄 코멘트로 저장
        note = " ".join(str(c["note"]).split())
        note = note.replace('"', "'")
        out.append(f'    note: "{note}"')
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
    if ref.get("comment"):
        parts.append(f"comment: {_scalar(ref['comment'])}")
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
