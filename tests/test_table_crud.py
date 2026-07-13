"""Role-aware per-table CRUD 회귀 테스트.

버그: 데몬/컨트롤러 체인의 '테이블(CRUD)' 컬럼이 문장 전체 body CRUD 를
그 문장의 모든 테이블에 union 해서, MERGE 의 USING/JOIN 소스나
INSERT..SELECT 소스 같은 **읽기 전용** 테이블까지 target 의 C/U 를 받았다.
읽기 소스는 R 이어야 한다.
"""
from oracle_embeddings.mybatis_parser import extract_table_crud_from_sql as crud
from oracle_embeddings.legacy_analyzer import (
    _build_mybatis_indexes, _derive_table_crud,
)


def _s(sql):
    return {t: "".join(sorted(ls)) for t, ls in crud(sql).items()}


def test_select_only_all_read():
    r = _s("SELECT a.ID, b.NM FROM MY_TABLE a JOIN OTHER_TBL b ON a.ID=b.ID")
    assert r == {"MY_TABLE": "R", "OTHER_TBL": "R"}


def test_select_for_update_stays_read():
    assert _s("SELECT ID FROM MY_TABLE WHERE ID=#{i} FOR UPDATE") == {"MY_TABLE": "R"}


def test_merge_subquery_using_sources_are_read():
    r = _s("MERGE INTO TGT t USING (SELECT ID,VAL FROM SRC a JOIN JOINT b "
           "ON a.ID=b.ID) s ON(t.ID=s.ID) WHEN MATCHED THEN UPDATE SET "
           "t.VAL=s.VAL WHEN NOT MATCHED THEN INSERT(ID) VALUES(s.ID)")
    assert r["SRC"] == "R" and r["JOINT"] == "R"
    assert r["TGT"] == "CU"


def test_merge_bare_table_using_source_is_read():
    r = _s("MERGE INTO TGT t USING SRC_TBL s ON(t.ID=s.ID) "
           "WHEN MATCHED THEN UPDATE SET t.VAL=s.VAL")
    assert r == {"TGT": "U", "SRC_TBL": "R"}


def test_update_subquery_lookup_is_read():
    r = _s("UPDATE TGT t SET t.X=(SELECT VAL FROM LOOKUP_TBL WHERE CODE=t.CODE)")
    assert r == {"TGT": "U", "LOOKUP_TBL": "R"}


def test_insert_select_source_is_read():
    r = _s("INSERT INTO TGT (ID,VAL) SELECT ID,VAL FROM SRC_TBL a "
           "JOIN JOINT b ON a.ID=b.ID")
    assert r["TGT"] == "C" and r["SRC_TBL"] == "R" and r["JOINT"] == "R"


def test_delete_subquery_source_is_read():
    r = _s("DELETE FROM TGT WHERE ID IN (SELECT ID FROM SRC_TBL WHERE F=#{f})")
    assert r == {"TGT": "D", "SRC_TBL": "R"}


def test_derive_over_chain_keeps_read_source_read():
    """체인이 SELECT-only mapper 만 호출하면 그 테이블은 R (사용자 증상)."""
    res = {"statements": [
        {"namespace": "ns", "id": "selOnly", "type": "SELECT",
         "mapper": "x", "mapper_path": "x.xml",
         "sql": "SELECT a.ID FROM MY_TABLE a JOIN OTHER_TBL b ON a.ID=b.ID"},
        {"namespace": "ns", "id": "mrg", "type": "UPDATE",
         "mapper": "x", "mapper_path": "x.xml",
         "sql": "MERGE INTO TGT t USING (SELECT ID FROM MY_TABLE) s "
                "ON(t.ID=s.ID) WHEN MATCHED THEN UPDATE SET t.V=s.ID"},
    ]}
    idx = _build_mybatis_indexes(res)

    only = {t: "".join(sorted(l))
            for t, l in _derive_table_crud(["ns.selOnly"], idx).items()}
    assert only == {"MY_TABLE": "R", "OTHER_TBL": "R"}

    # MY_TABLE 은 MERGE 에서도 read 소스일 뿐 → 두 문장 합쳐도 R 유지.
    both = {t: "".join(sorted(l))
            for t, l in _derive_table_crud(["ns.selOnly", "ns.mrg"], idx).items()}
    assert both["MY_TABLE"] == "R"
    assert both["TGT"] == "U"


def test_derive_legacy_fallback_when_no_table_crud():
    """구버전 인덱스(table_crud 없음)는 body letters 폴백 유지."""
    idx = {"statement_to_tables": {"ns.q": ["A", "B"]},
           "statement_to_body_crud": {"ns.q": {"R"}},
           "statement_to_table_crud": {}}
    r = {t: "".join(sorted(l))
         for t, l in _derive_table_crud(["ns.q"], idx).items()}
    assert r == {"A": "R", "B": "R"}
