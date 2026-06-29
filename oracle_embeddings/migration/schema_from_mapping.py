"""매핑 yaml 의 TO-BE 측 정보로 TO-BE 스키마를 파생.

TO-BE DB 접속 / 스키마 .md 가 아직 없을 때 ``migrate-sql`` 을 돌릴 수
있도록, ``column_mapping.yaml`` 에 이미 담긴 TO-BE 테이블/컬럼/타입/주석을
모아 ``{TABLE: {COLUMN, ...}}`` 인덱스 (Stage A 검증용) + ``schema`` 커맨드
호환 ``.md`` 텍스트 (사용자 검토/보강용) 로 변환한다.

⚠ 한계: 매핑에 등장하지 않는 **pass-through 컬럼** (AS-IS 이름 그대로
TO-BE 에 존재) 은 포함되지 않는다. 따라서 Stage A 검증은 "매핑이 바꾼
컬럼" 범위에서만 정확하고, 안 바뀐 컬럼은 오탐(없는 컬럼)으로 잡힐 수
있다. 변환 산출물 (converted XML) 자체는 영향 없음. 정확한 검증이
필요하면 실제 TO-BE 스키마 ``.md`` 를 ``--to-be-schema`` 로 넣는다.
"""
from __future__ import annotations

import re
from typing import Dict, Set, Tuple

from .mapping_model import Mapping


def _safe_type(t: str | None) -> str:
    """``parse_schema_md`` 의 타입 셀은 공백 없는 단일 토큰(``\\S+``)이어야
    한다. 비면 ``VARCHAR2`` placeholder, 내부 공백은 제거 — 타입 문자열은
    검증 컬럼셋 계산에 쓰이지 않으므로 가독성보다 파싱 안정성 우선."""
    s = (t or "").strip()
    if not s:
        return "VARCHAR2"
    return re.sub(r"\s+", "", s)


def _collect(mapping: Mapping) -> Tuple[Dict[str, Dict[str, tuple]], Dict[str, str]]:
    """``{TABLE: {COLUMN: (type, comment)}}`` + ``{TABLE: comment}`` 반환."""
    tables: Dict[str, Dict[str, tuple]] = {}
    table_comments: Dict[str, str] = {}

    # 1) 테이블 매핑 — 컬럼이 한 줄도 없는 TO-BE 테이블도 등록되도록
    for tm in mapping.tables:
        for t in tm.to_be_tables():
            tables.setdefault(t.upper(), {})
            if tm.comment:
                table_comments.setdefault(t.upper(), tm.comment)

    # 2) 컬럼 매핑 — TO-BE ref (ColumnRef | SplitTarget). SplitTarget 은
    #    type/comment 속성이 없어 getattr 기본값 None.
    for cm in mapping.columns:
        for ref in cm.to_be_refs():
            tname = ref.table.upper()
            cname = ref.column.upper()
            ctype = getattr(ref, "type", None)
            ccomment = getattr(ref, "comment", None)
            cols = tables.setdefault(tname, {})
            # 먼저 본 정보 우선. 단 빈 값으로 잡혔다가 나중에 type/comment
            # 가 채워지면 갱신 (같은 컬럼이 여러 매핑에 등장하는 케이스).
            if cname not in cols or (
                cols[cname] == (None, None) and (ctype or ccomment)
            ):
                cols[cname] = (ctype, ccomment)

    return tables, table_comments


def build_to_be_schema_tables(mapping: Mapping) -> Dict[str, Set[str]]:
    """``load_schema_tables`` 와 동일 형태 ``{TABLE: {COLUMN, ...}}`` 반환."""
    tables, _ = _collect(mapping)
    return {t: set(cols.keys()) for t, cols in tables.items()}


def build_to_be_schema_md(mapping: Mapping, owner: str = "TOBE") -> str:
    """파생 TO-BE 스키마를 ``schema`` 커맨드 호환 ``.md`` 텍스트로 직렬화."""
    tables, table_comments = _collect(mapping)
    lines = [f"# {owner}", ""]
    lines.append(
        "> ⚠ 이 파일은 column_mapping.yaml 의 TO-BE 정보로 자동 파생됨 "
        "— 매핑에 안 나온 pass-through 컬럼은 빠져 있음. 정확한 Stage A "
        "검증이 필요하면 실제 TO-BE 스키마로 교체하거나 컬럼을 보강할 것."
    )
    lines.append("")
    for tname in sorted(tables):
        lines.append(f"## {tname}")
        if table_comments.get(tname):
            lines.append(f"> {table_comments[tname]}")
        lines.append("")
        lines.append("| Column | Type | Nullable | Default | Description |")
        lines.append("|--------|------|----------|---------|-------------|")
        for cname in sorted(tables[tname]):
            ctype, ccomment = tables[tname][cname]
            desc = (ccomment or "").replace("|", "\\|").replace("\n", " ").strip()
            lines.append(f"| {cname} | {_safe_type(ctype)} | Y |  | {desc} |")
        lines.append("")
    return "\n".join(lines)
