"""로컬 실행용 Streamlit 화면 — 비개발자가 CLI 없이 마이그레이션 / 용어검색.

실행:
    streamlit run webui/app.py
    (또는 루트의 run_webui.bat 더블클릭)

두 화면:
  1) SQL 마이그레이션 — AS-IS mapper XML + 매핑 YAML → 변환 XML + 리포트 zip
  2) 용어 검색 — build-dict 로 적재한 표준사전 SQLite 를 LIKE 검색

기존 CLI/함수를 그대로 재사용한다:
  - 마이그레이션은 ``python main.py migrate-sql`` 를 subprocess 로 호출
    (동작 100% 동일 보장). 출력은 임시 폴더로 격리 후 zip.
  - 용어 검색은 표준사전 SQLite 를 직접 조회.
폐쇄망/오프라인 전제 — 외부 네트워크 호출 없음.
"""
from __future__ import annotations

import glob
import io
import os
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import streamlit as st

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None

ROOT = Path(__file__).resolve().parent.parent  # 프로젝트 루트
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

st.set_page_config(page_title="SQL 마이그레이션 & 용어검색",
                   page_icon="🛠️", layout="wide")


# ---------------------------------------------------------------------------
# 공통 헬퍼
# ---------------------------------------------------------------------------
def _default_dict_db() -> Path:
    """config.yaml 의 vectordb.db_path 기준 표준사전 SQLite 경로."""
    db_dir = "./vectordb"
    cfg = ROOT / "config.yaml"
    if yaml and cfg.is_file():
        try:
            data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
            db_dir = (data.get("vectordb") or {}).get("db_path", db_dir)
        except Exception:
            pass
    return (ROOT / db_dir / "standard_dict.sqlite").resolve()


def _write_temp_config(out_dir: Path) -> Path:
    """기존 config.yaml 복사 + storage.output_dir 만 임시 폴더로 덮어써서
    마이그레이션 산출물을 격리한다 (사용자 output/ 오염 방지)."""
    data = {}
    cfg = ROOT / "config.yaml"
    if yaml and cfg.is_file():
        try:
            data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        except Exception:
            data = {}
    data.setdefault("storage", {})["output_dir"] = str(out_dir)
    tmp = out_dir / "_webui_config.yaml"
    tmp.write_text(yaml.safe_dump(data, allow_unicode=True) if yaml
                   else f'storage:\n  output_dir: "{out_dir}"\n',
                   encoding="utf-8")
    return tmp


# ---------------------------------------------------------------------------
# .env 읽기/쓰기 (설정 화면)
# ---------------------------------------------------------------------------
def _env_path() -> Path:
    return ROOT / ".env"


def _read_env() -> dict:
    """현재 ``.env`` (없으면 ``.env.example``) 를 KEY=VALUE dict 로."""
    path = _env_path()
    src = path if path.is_file() else (ROOT / ".env.example")
    env: dict = {}
    if src.is_file():
        for line in src.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def _write_env(updates: dict) -> None:
    """``.env`` 의 알려진 키만 갱신하고 나머지 줄/주석은 보존. 없던 키는 추가."""
    path = _env_path()
    seen: set = set()
    out_lines: list = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s and not s.startswith("#") and "=" in s:
                k = s.split("=", 1)[0].strip()
                if k in updates:
                    out_lines.append(f"{k}={updates[k]}")
                    seen.add(k)
                    continue
            out_lines.append(line)
    for k, v in updates.items():
        if k not in seen:
            out_lines.append(f"{k}={v}")
    path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")


# .env 필드 그룹 — (그룹명, [(키, 라벨, 비밀번호여부), ...])
_ENV_GROUPS = [
    ("Oracle AS-IS", [
        ("ORACLE_USER", "사용자", False),
        ("ORACLE_PASSWORD", "비밀번호", True),
        ("ORACLE_DSN", "DSN (host:port/service)", False),
        ("ORACLE_SCHEMA_OWNER", "스키마 owner", False),
        ("ORACLE_INSTANT_CLIENT_DIR", "Instant Client 경로 (thick)", False),
    ]),
    ("Oracle TO-BE (validate-migration Stage B)", [
        ("ORACLE_TOBE_DSN", "TO-BE DSN", False),
        ("ORACLE_TOBE_USER", "TO-BE 사용자", False),
        ("ORACLE_TOBE_PASSWORD", "TO-BE 비밀번호", True),
    ]),
    ("LLM (enrich-schema / terms / standardize 등)", [
        ("LLM_API_BASE", "API Base", False),
        ("LLM_API_KEY", "API Key", True),
        ("LLM_MODEL", "모델", False),
    ]),
    ("임베딩 (erd-rag / recommend-names RAG)", [
        ("EMBEDDING_API_BASE", "API Base", False),
        ("EMBEDDING_API_KEY", "API Key", True),
        ("EMBEDDING_MODEL", "모델", False),
    ]),
    ("패턴/코딩 LLM (discover-patterns / biz 추출 / 화면변환)", [
        ("PATTERN_LLM_API_BASE", "API Base", False),
        ("PATTERN_LLM_API_KEY", "API Key", True),
        ("PATTERN_LLM_MODEL", "모델", False),
    ]),
]


def render_settings() -> None:
    st.header("⚙️ 설정 (LLM / DB 환경)")
    st.caption("`.env` 파일을 편집합니다. 값은 이 PC 로컬에만 저장되고 외부로 "
               "전송되지 않습니다. 저장 후 실행 중인 화면/CLI 에 반영됩니다.")
    if not _env_path().is_file():
        st.info("`.env` 가 없어 `.env.example` 기본값을 표시합니다. 저장하면 "
                "`.env` 가 생성됩니다.")

    env = _read_env()
    updates: dict = {}
    with st.form("settings_form"):
        for group, fields in _ENV_GROUPS:
            st.subheader(group)
            cols = st.columns(2)
            for i, (key, label, secret) in enumerate(fields):
                updates[key] = cols[i % 2].text_input(
                    f"{label}  ·  `{key}`", value=env.get(key, ""),
                    type="password" if secret else "default",
                    key=f"env_{key}")
        saved = st.form_submit_button("💾 저장", type="primary")
    if saved:
        # 빈 값도 그대로 저장 (사용자가 지운 것 반영)
        _write_env(updates)
        st.success(f"저장됨: {_env_path()}")


# ---------------------------------------------------------------------------
# 화면: 매핑 만들기 (9컬럼 → MD)
# ---------------------------------------------------------------------------
_MAPPING_HEADERS = [
    "asis_table", "asis_column", "asis_column_type",
    "tobe_table", "tobe_table_comment", "tobe_column",
    "tobe_column_type", "tobe_column_comment", "remark",
]
_MAPPING_EXAMPLE = [
    ["CUST", "CUST_ID", "NUMBER(10)", "CUSTOMER", "고객 마스터",
     "CUSTOMER_ID", "NUMBER(10)", "고객ID", ""],
    ["CUST", "REG_DT", "VARCHAR2(8)", "CUSTOMER", "고객 마스터",
     "REGISTER_DATE", "DATE", "등록일자", "YYYYMMDD → DATE"],
    ["CUST", "OBSOLETE_FLAG", "CHAR(1)", "CUSTOMER", "고객 마스터",
     "", "", "", "TO-BE 에서 삭제"],
]


def _macro_bas_text() -> str:
    p = Path(__file__).resolve().parent / "assets" / "mapping_macro.bas"
    return p.read_text(encoding="utf-8") if p.is_file() else ""


def _rows_to_md(rows: list) -> str:
    """9컬럼 행 리스트 → convert-mapping 용 마크다운 표 텍스트."""
    def esc(v):
        return str(v if v is not None else "").replace("|", "\\|").replace(
            "\r", " ").replace("\n", " ").strip()
    out = ["| " + " | ".join(_MAPPING_HEADERS) + " |",
           "|" + "------|" * len(_MAPPING_HEADERS)]
    for r in rows:
        cells = [esc(r[i]) if i < len(r) else "" for i in range(len(_MAPPING_HEADERS))]
        if any(c for c in cells):
            out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out) + "\n"


def _build_mapping_xlsx() -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    wb = Workbook()
    ws = wb.active
    ws.title = "매핑"
    hdr_fill = PatternFill("solid", fgColor="1F3A5F")
    for j, h in enumerate(_MAPPING_HEADERS, start=1):
        cell = ws.cell(row=1, column=j, value=h)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = hdr_fill
        ws.column_dimensions[ws.cell(row=1, column=j).column_letter].width = 20
    for i, row in enumerate(_MAPPING_EXAMPLE, start=2):
        for j, v in enumerate(row, start=1):
            ws.cell(row=i, column=j, value=v)

    guide = wb.create_sheet("매크로_설치법")
    steps = [
        "AS-IS→TO-BE 컬럼 매핑 작성 → MD 내보내기",
        "",
        "[1] '매핑' 시트 2행부터 값을 채웁니다 (예시 3행 참고).",
        "    - tobe_column 을 비우면 해당 컬럼은 삭제(drop) 로 처리됩니다.",
        "",
        "[2] 매크로 설치 (최초 1회):",
        "    1) Alt+F11 (Visual Basic 편집기)",
        "    2) 파일 > 파일 가져오기 > mapping_macro.bas 선택",
        "       (또는 아래 소스를 새 모듈에 붙여넣기)",
        "    3) 통합문서를 'Excel 매크로 사용 통합문서(*.xlsm)' 로 저장",
        "",
        "[3] 사용: 개발도구 > 매크로 > ExportMappingMd 실행",
        "    → 통합문서와 같은 폴더에 column_mapping.md 생성 (UTF-8).",
        "    이 .md 를 마이그레이션 화면 입력으로 사용하세요.",
        "",
        "──────── 매크로 소스 (mapping_macro.bas) ────────",
    ]
    row = 1
    for s in steps:
        guide.cell(row=row, column=1, value=s)
        row += 1
    for line in _macro_bas_text().splitlines():
        guide.cell(row=row, column=1, value=line)
        row += 1
    guide.column_dimensions["A"].width = 90

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def render_mapping_maker() -> None:
    st.header("📝 매핑 만들기 (AS-IS → TO-BE)")
    st.caption("9컬럼 엑셀에 매핑을 채워 convert-mapping 용 `column_mapping.md` "
               "를 만듭니다. 이후 마이그레이션 화면 입력으로 사용합니다.")

    st.subheader("① 엑셀 템플릿 + 매크로 (권장)")
    c1, c2 = st.columns(2)
    c1.download_button("⬇ 매핑 템플릿 (.xlsx)", data=_build_mapping_xlsx(),
                       file_name="mapping_template.xlsx",
                       mime="application/vnd.openxmlformats-officedocument."
                            "spreadsheetml.sheet")
    c2.download_button("⬇ VBA 매크로 (.bas)", data=_macro_bas_text(),
                       file_name="mapping_macro.bas", mime="text/plain")
    st.markdown(
        "- 템플릿의 **매핑** 시트에 값을 채우고, **매크로_설치법** 시트의 "
        "안내대로 매크로를 1회 임포트 → `.xlsm` 로 저장.\n"
        "- 이후 **개발도구 ▸ 매크로 ▸ `ExportMappingMd`** 실행하면 같은 "
        "폴더에 `column_mapping.md` 가 생성됩니다 (UTF-8, 한글 OK).")

    st.divider()
    st.subheader("② (매크로 없이) 채운 엑셀 → MD 바로 변환")
    up = st.file_uploader("작성한 매핑 엑셀 (.xlsx) — '매핑' 시트 9컬럼",
                          type=["xlsx"])
    if up is not None:
        try:
            from openpyxl import load_workbook
            wb = load_workbook(io.BytesIO(up.getbuffer()), data_only=True)
            ws = wb["매핑"] if "매핑" in wb.sheetnames else wb.active
            rows = [[c.value for c in row]
                    for row in ws.iter_rows(min_row=2, max_col=len(_MAPPING_HEADERS))]
            md = _rows_to_md(rows)
        except Exception as e:  # noqa: BLE001
            st.error(f"엑셀 읽기 실패: {type(e).__name__}: {e}")
        else:
            n = md.count("\n") - 2
            st.success(f"{max(n, 0)} 행 변환됨.")
            st.download_button("⬇ column_mapping.md 내려받기", data=md,
                               file_name="column_mapping.md",
                               mime="text/markdown")
            with st.expander("MD 미리보기"):
                st.code(md, language="markdown")


# ---------------------------------------------------------------------------
# 화면 1: SQL 마이그레이션
# ---------------------------------------------------------------------------
def render_migration() -> None:
    st.header("🛠️ SQL 마이그레이션")
    st.caption("AS-IS MyBatis mapper XML + 컬럼 매핑 YAML → TO-BE 변환 XML + "
               "Excel 리포트. 결과는 zip 으로 내려받습니다.")

    mappers = st.file_uploader(
        "① AS-IS mapper XML (여러 개 선택 가능)",
        type=["xml"], accept_multiple_files=True)
    mapping = st.file_uploader("② 컬럼 매핑 YAML", type=["yaml", "yml"])

    schema_mode = st.radio(
        "③ TO-BE 스키마",
        ["매핑 YAML 에서 자동 파생 (DB/스키마 없이)", "TO-BE 스키마 .md 업로드"],
        help="스키마가 아직 없으면 매핑에서 파생하세요. pass-through 컬럼은 "
             "Stage A 검증에서 오탐될 수 있으나 변환 XML 은 정상입니다.")
    schema_md = None
    if schema_mode.startswith("TO-BE 스키마 .md"):
        schema_md = st.file_uploader("TO-BE 스키마 .md", type=["md"])

    c1, c2, c3 = st.columns(3)
    emit_comments = c1.checkbox("한글 주석 삽입", value=False,
                                help="--emit-column-comments")
    no_validate = c2.checkbox("Stage A 검증 건너뛰기", value=True,
                              help="--no-validate (DB/스키마 미완성 단계 권장)")
    llm_fallback = c3.checkbox("LLM 보조 변환", value=False,
                               help="--llm-fallback (NEEDS_LLM 케이스)")

    ready = bool(mappers) and mapping is not None and not (
        schema_mode.startswith("TO-BE 스키마 .md") and schema_md is None)
    if st.button("▶ 변환 실행", type="primary", disabled=not ready):
        _run_migration(mappers, mapping, schema_mode, schema_md,
                       emit_comments, no_validate, llm_fallback)

    # 직전 실행 결과 (rerun 후에도 유지)
    res = st.session_state.get("mig_result")
    if res:
        if res["ok"]:
            st.success(res["summary"])
            st.download_button(
                "⬇ 변환 결과 zip 내려받기", data=res["zip"],
                file_name="migration_result.zip", mime="application/zip")
        else:
            st.error("변환 실패")
        with st.expander("실행 로그"):
            st.code(res["log"] or "(로그 없음)")


def _run_migration(mappers, mapping, schema_mode, schema_md,
                   emit_comments, no_validate, llm_fallback) -> None:
    with st.spinner("변환 중…"):
        work = Path(tempfile.mkdtemp(prefix="webui_mig_"))
        in_dir = work / "mapper"
        in_dir.mkdir()
        for f in mappers:
            (in_dir / f.name).write_bytes(f.getbuffer())
        map_path = work / "mapping.yaml"
        map_path.write_bytes(mapping.getbuffer())
        out_dir = work / "out"
        out_dir.mkdir()
        cfg = _write_temp_config(out_dir)

        # ``--config`` 는 전역 인자라 subcommand(migrate-sql) **앞** 에 온다.
        cmd = [sys.executable, str(ROOT / "main.py"), "--config", str(cfg),
               "migrate-sql",
               "--mybatis-dir", str(in_dir),
               "--mapping", str(map_path),
               "--output-format", "excel,xml"]
        if schema_mode.startswith("TO-BE 스키마 .md"):
            sp = work / "to_be_schema.md"
            sp.write_bytes(schema_md.getbuffer())
            cmd += ["--to-be-schema", str(sp)]
        else:
            cmd += ["--to-be-schema-from-mapping"]
        if emit_comments:
            cmd += ["--emit-column-comments"]
        if no_validate:
            cmd += ["--no-validate"]
        if llm_fallback:
            cmd += ["--llm-fallback"]

        proc = subprocess.run(cmd, capture_output=True, text=True,
                              cwd=str(ROOT))
        log = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")

        # 산출물(migration/<date>/) 을 zip 으로
        dated = sorted(glob.glob(str(out_dir / "migration" / "*")))
        zip_buf = io.BytesIO()
        summary = ""
        if dated:
            latest = Path(dated[-1])
            with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for p in latest.rglob("*"):
                    if p.is_file():
                        zf.write(p, p.relative_to(latest))
            zip_buf.seek(0)
        for line in log.splitlines():
            if line.startswith("Converted "):
                summary = line.strip()
                break

    st.session_state["mig_result"] = {
        "ok": proc.returncode == 0 and bool(dated),
        "summary": summary or "변환 완료 (요약 라인 없음 — 로그 확인)",
        "zip": zip_buf.getvalue(),
        "log": log,
    }
    st.rerun()


# ---------------------------------------------------------------------------
# 화면 2: 용어 검색
# ---------------------------------------------------------------------------
_SEARCH_SPECS = {
    "용어": ("term",
             ["logical", "physical", "eng", "domain", "data_type",
              "length", "scale", "is_std", "desc"],
             ["logical", "physical", "eng", "desc"]),
    "단어": ("word",
             ["logical", "physical", "eng", "is_std", "is_classifier",
              "synonyms", "desc"],
             ["logical", "physical", "eng", "synonyms", "desc"]),
    "도메인": ("domain",
              ["grp", "name", "data_type", "length", "scale", "full_type",
               "desc"],
              ["name", "grp", "desc"]),
}


def render_term_search() -> None:
    st.header("🔎 용어 검색")
    st.caption("build-dict 로 적재한 표준 단어/용어/도메인 사전(SQLite)을 "
               "검색합니다.")

    db_path = Path(st.text_input("표준사전 SQLite 경로",
                                 value=str(_default_dict_db())))
    if not db_path.is_file():
        st.warning(f"표준사전이 없습니다: {db_path}\n\n"
                   "먼저 `python main.py build-dict --word-dict ... "
                   "--term-dict ...` 로 적재하세요.")
        return

    c1, c2 = st.columns([3, 1])
    q = c1.text_input("검색어 (한글 논리명 / 영문 / 물리명 일부)")
    target = c2.radio("대상", list(_SEARCH_SPECS.keys()), horizontal=False)

    include_expired = st.checkbox("만료 항목 포함", value=False)

    if not q.strip():
        st.info("검색어를 입력하세요.")
        return

    table, cols, search_cols = _SEARCH_SPECS[target]
    where = " OR ".join(f"{c} LIKE ?" for c in search_cols)
    params = [f"%{q.strip()}%"] * len(search_cols)
    sql = f"SELECT {', '.join(cols)} FROM {table} WHERE ({where})"
    if not include_expired and table in ("word", "term", "domain"):
        sql += " AND COALESCE(expired, 0) = 0"
    sql += " LIMIT 500"

    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(sql, params)]
        conn.close()
    except Exception as e:  # noqa: BLE001
        st.error(f"검색 실패: {type(e).__name__}: {e}")
        return

    st.write(f"**{len(rows)}건** (최대 500)")
    if rows:
        st.dataframe(rows, use_container_width=True, hide_index=True)
    else:
        st.info("일치하는 항목이 없습니다.")


# ---------------------------------------------------------------------------
# 라우팅
# ---------------------------------------------------------------------------
_PAGES = {
    "매핑 만들기": render_mapping_maker,
    "SQL 마이그레이션": render_migration,
    "용어 검색": render_term_search,
    "설정": render_settings,
}


def main() -> None:
    st.sidebar.title("메뉴")
    page = st.sidebar.radio("화면", list(_PAGES.keys()),
                            label_visibility="collapsed")
    st.sidebar.markdown("---")
    st.sidebar.caption("로컬 실행 · 오프라인 · 외부 전송 없음")
    _PAGES[page]()


main()
