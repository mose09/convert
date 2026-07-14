"""convert-mapping heuristic: TO-BE 가 비면 AS-IS 이름으로 폴백.

요구사항: MD → 매핑 YAML 변환 시 tobe 테이블명/컬럼명이 없으면 오류/삭제
없이 그냥 asis 이름으로 매핑한다. 명시적 drop 마커(-, DROP, 삭제)는 예외.
"""
from oracle_embeddings.migration.mapping_converter import _heuristic_parse


def _cols(data):
    out = {}
    for c in data["columns"]:
        tb = c.get("to_be")
        key = c["as_is"]["table"] + "." + c["as_is"]["column"]
        out[key] = (c["kind"],
                    (tb["table"] + "." + tb["column"]) if isinstance(tb, dict) else tb)
    return out


def test_empty_tobe_values_fall_back_to_asis():
    md = ("| asis_table | asis_column | tobe_table | tobe_column |\n"
          "|---|---|---|---|\n"
          "| ORDERS | ORD_NO |  |  |\n")
    c = _cols(_heuristic_parse(md))
    assert c["ORDERS.ORD_NO"] == ("rename", "ORDERS.ORD_NO")


def test_missing_tobe_headers_entirely():
    """TO-BE 열이 아예 없는 asis-only MD 도 에러 없이 identity 매핑."""
    md = ("| asis_table | asis_column |\n"
          "|---|---|\n"
          "| CUST | CUST_NM |\n")
    d = _heuristic_parse(md)
    assert d["columns"], "columns 이 비면 안 됨(에러로 처리된 것)"
    assert _cols(d)["CUST.CUST_NM"] == ("rename", "CUST.CUST_NM")


def test_column_inherits_table_rename_when_tobe_col_blank():
    """같은 테이블이 다른 행에서 rename 됐으면 빈 컬럼도 그 TO-BE 테이블로."""
    md = ("| asis_table | asis_column | tobe_table | tobe_column |\n"
          "|---|---|---|---|\n"
          "| CUST | CUST_NM | CUSTOMER | CUST_NAME |\n"
          "| CUST | REG_DT  |          |           |\n")
    c = _cols(_heuristic_parse(md))
    assert c["CUST.CUST_NM"] == ("rename", "CUSTOMER.CUST_NAME")
    assert c["CUST.REG_DT"] == ("rename", "CUSTOMER.REG_DT")


def test_explicit_drop_marker_still_drops():
    for marker in ("-", "DROP", "삭제"):
        md = ("| asis_table | asis_column | tobe_table | tobe_column |\n"
              "|---|---|---|---|\n"
              f"| CUST | OLD_COL | CUSTOMER | {marker} |\n")
        c = _cols(_heuristic_parse(md))
        assert c["CUST.OLD_COL"] == ("drop", None), marker


def test_empty_tobe_table_uses_asis_table():
    md = ("| asis_table | asis_column | tobe_table | tobe_column |\n"
          "|---|---|---|---|\n"
          "| LEGACY_T | COL1 |  | NEW_COL |\n")
    tbls = {t["as_is"]: t["to_be"] for t in _heuristic_parse(md)["tables"]}
    assert tbls["LEGACY_T"] == "LEGACY_T"
