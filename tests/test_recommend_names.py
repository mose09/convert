"""recommend-names 결정적 코어 회귀 테스트.

LLM/임베딩 없이 동작하는 Tier1·2 만 검증한다.
실행: ``python tests/test_recommend_names.py`` 또는
      ``python -m unittest tests.test_recommend_names``
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oracle_embeddings.std_dict import (  # noqa: E402
    compose_data_type,
    ensure_std_dict,
    norm_kor,
)
from oracle_embeddings.tobe_recommender import recommend_column  # noqa: E402


def _make_word_dict(path):
    from openpyxl import Workbook
    wb = Workbook(); ws = wb.active
    ws.append(["논리명", "물리명", "물리의미(영문풀네임)", "표준여부(Y,N)",
               "속성분류어(Y,N)", "동의어", "설명", "만료일자", "출처구분"])
    for r in [
        ("고객", "CUST", "CUSTOMER", "Y", "N", "손님,거래처", "", "", "표준"),
        ("주문", "ORD", "ORDER", "Y", "N", "오더", "", "", "표준"),
        ("금액", "AMT", "AMOUNT", "Y", "Y", "", "", "", "표준"),
        ("번호", "NO", "NUMBER", "Y", "Y", "", "", "", "표준"),
        ("등록", "REG", "REGISTER", "Y", "N", "", "", "20200101", "폐기"),  # 만료
    ]:
        ws.append(r)
    wb.save(path)


def _make_term_dict(path):
    from openpyxl import Workbook
    wb = Workbook(); ws = wb.active
    ws.append(["논리명", "물리명", "구성정보", "물리의미", "도메인명", "데이터유형",
               "길이", "소수점", "표준여부(Y,N)", "개인정보구분", "암호화여부",
               "설명", "만료일자", "출처구분"])
    for r in [
        ("고객번호", "CUST_NO", "고객+번호", "", "번호", "VARCHAR2", "20", "",
         "Y", "Y", "N", "", "", "표준"),
        ("주문금액", "ORD_AMT", "주문+금액", "", "금액", "NUMBER", "15", "2",
         "Y", "N", "N", "", "", "표준"),
    ]:
        ws.append(r)
    wb.save(path)


class RecommendCoreTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        wpath = os.path.join(cls.tmp, "word.xlsx")
        tpath = os.path.join(cls.tmp, "term.xlsx")
        _make_word_dict(wpath)
        _make_term_dict(tpath)
        cls.sd = ensure_std_dict(os.path.join(cls.tmp, "dict.sqlite"), wpath, tpath)

    def test_norm_and_type(self):
        self.assertEqual(norm_kor(" 고객 번호 "), "고객번호")
        self.assertEqual(compose_data_type("VARCHAR2", "20", ""), "VARCHAR2(20)")
        self.assertEqual(compose_data_type("NUMBER", "15", "2"), "NUMBER(15,2)")
        self.assertEqual(compose_data_type("DATE", "", ""), "DATE")

    def test_term_exact_match(self):
        rec = recommend_column("T", "CUST_NO", "고객번호", self.sd)
        self.assertEqual(rec.tier, "정확매칭(용어)")
        self.assertEqual(rec.tobe_name, "CUST_NO")
        self.assertEqual(rec.data_type, "VARCHAR2(20)")
        self.assertEqual(rec.confidence, 1.0)

    def test_word_composition(self):
        rec = recommend_column("T", "ORD_NO", "주문번호", self.sd)
        self.assertEqual(rec.tobe_name, "ORD_NO")
        self.assertTrue(rec.tier.startswith("단어조합"))
        self.assertEqual(rec.data_type, "VARCHAR2(20)")  # 번호 분류어 추론

    def test_synonym_match(self):
        # '손님' 은 '고객' 의 동의어
        rec = recommend_column("T", "GUEST_AMT", "손님금액", self.sd)
        self.assertEqual(rec.tobe_name, "CUST_AMT")

    def test_expired_word_excluded(self):
        # '등록' 단어는 만료 → 미매칭 단편으로 남고 표준 약어로 조합 안 됨
        rec = recommend_column("T", "REG_NO", "등록번호", self.sd)
        self.assertIn("등록", rec.unmatched_frags)
        self.assertNotIn("REG", rec.tobe_name.split("_"))

    def test_unmatched_no_comment(self):
        rec = recommend_column("T", "XYZ123", None, self.sd)
        self.assertEqual(rec.tier, "미매칭")
        self.assertEqual(rec.confidence, 0.0)


class SheetDetectionTest(unittest.TestCase):
    """표지 시트 + 개행/번호 헤더 자동 인식 회귀."""

    def test_cover_sheet_skipped_and_header_normalized(self):
        from openpyxl import Workbook

        from oracle_embeddings.std_dict import _classify_header, _pick_sheet
        tmp = tempfile.mkdtemp()
        wb = Workbook()
        cover = wb.active
        cover.title = "표지"
        cover.append(["표준 단어사전", "v1"])
        ws = wb.create_sheet("단어목록")
        ws.append(["1. 논리\n명", "물리명 ", "표준여부(Y,N)"])
        ws.append(["고객", "CUST", "Y"])
        path = os.path.join(tmp, "w.xlsx")
        wb.save(path)

        name, _, idx, _ = _pick_sheet(path, None)
        self.assertEqual(name, "단어목록")
        self.assertIn("logical", idx)
        self.assertIn("physical", idx)
        self.assertEqual(_classify_header("1. 논리\n명"), "logical")
        self.assertEqual(_classify_header("물리명 "), "physical")

    def test_nfd_unicode_header_and_data(self):
        # macOS 등에서 생성된 NFD(조합형) 한글도 인식돼야 함
        import unicodedata

        from oracle_embeddings.std_dict import _classify_header, norm_kor
        self.assertEqual(_classify_header(unicodedata.normalize("NFD", "논리명")),
                         "logical")
        self.assertEqual(_classify_header(unicodedata.normalize("NFD", "물리명")),
                         "physical")
        self.assertEqual(norm_kor(unicodedata.normalize("NFD", "고객번호")),
                         norm_kor("고객번호"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
