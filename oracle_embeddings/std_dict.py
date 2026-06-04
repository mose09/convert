"""표준사전 (단어사전 + 용어사전) Excel → SQLite 캐시 + 인메모리 인덱스.

AS-IS 스키마를 TO-BE 속성명으로 추천하기 위한 표준 데이터 저장소.

- **단어사전** (word): 논리명 / 물리명 / 물리의미(영문풀) / 표준여부 /
  속성분류어 / 동의어 / 설명 / 만료일자 / 출처구분
- **용어사전** (term): 논리명 / 물리명 / 구성정보 / 물리의미 / 도메인명 /
  데이터유형 / 길이 / 소수점 / 표준여부 / 개인정보구분 / 암호화여부 /
  설명 / 만료일자 / 출처구분

용어사전은 이미 `논리명 → 물리명 + 도메인 + 데이터유형` 정답표라 정확매칭(Tier1)
의 핵심이고, 단어사전은 단어조합/동의어 매칭(Tier2) 에 쓴다.

엑셀을 매 실행마다 재파싱하지 않도록 SQLite (`standard_dict.sqlite`) 에 캐시.
원본 mtime 이 바뀌면 자동 재빌드.
"""

import logging
import os
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────
# 헤더 자동 인식
# ─────────────────────────────────────────────────────────────────

def _classify_header(h: str) -> str | None:
    """엑셀 헤더 셀 1개를 표준 필드 키로 분류 (없으면 None).

    헤더의 괄호 부가설명 `(...)` 과 공백은 제거 후 판정.
    예: ``물리의미(영문풀네임)`` → ``eng``, ``표준여부(Y,N)`` → ``is_std``.
    """
    if not h:
        return None
    t = re.sub(r"\(.*?\)", "", str(h)).strip().replace(" ", "")
    if not t:
        return None
    if t in ("논리명", "한글명", "논리명칭", "한글"):
        return "logical"
    if t in ("물리명", "컬럼명", "영문약어", "물리"):
        return "physical"
    if "물리의미" in t or "영문풀" in t or t in ("영문명", "영문"):
        return "eng"
    if "구성정보" in t or t == "구성":
        return "compose"
    if "도메인" in t:
        return "domain"
    if "데이터유형" in t or "데이터타입" in t or "자료형" in t or "데이터타" in t:
        return "data_type"
    if t == "길이" or t.startswith("길이") or "데이터길이" in t:
        return "length"
    if "소수" in t:
        return "scale"
    if "분류어" in t:
        return "is_classifier"
    if "표준여부" in t or t == "표준":
        return "is_std"
    if "동의어" in t or "유의어" in t or "이음동의" in t:
        return "synonyms"
    if "개인정보" in t:
        return "privacy"
    if "암호화" in t:
        return "encrypt"
    if "설명" in t or "정의" in t or "비고" in t:
        return "desc"
    if "만료" in t:
        return "expire"
    if "출처" in t:
        return "source"
    return None


def _header_index(header_row) -> dict[str, int]:
    """헤더 행 → {필드키: 컬럼인덱스}. 같은 키 중복 시 첫 번째 우선."""
    idx: dict[str, int] = {}
    for i, cell in enumerate(header_row):
        key = _classify_header(cell)
        if key and key not in idx:
            idx[key] = i
    return idx


def _find_header_row(rows: list[tuple]) -> int:
    """헤더 행 위치 탐색. 상위 10행 중 `논리명`+`물리명` 둘 다 잡히는 첫 행."""
    for r in range(min(10, len(rows))):
        idx = _header_index(rows[r])
        if "logical" in idx and "physical" in idx:
            return r
    return 0


# ─────────────────────────────────────────────────────────────────
# 정규화 / 셀 추출 헬퍼
# ─────────────────────────────────────────────────────────────────

_PUNCT_RE = re.compile(r"[\s\-_./,()\[\]{}<>~!@#$%^&*+=|\\:;'\"?]+")


def norm_kor(s: str | None) -> str:
    """논리명/코멘트 매칭용 정규화 — 공백·기호 제거."""
    if not s:
        return ""
    return _PUNCT_RE.sub("", str(s)).strip()


def _cell(row, idx: dict[str, int], key: str) -> str:
    i = idx.get(key)
    if i is None or i >= len(row):
        return ""
    v = row[i]
    if v is None:
        return ""
    return str(v).strip()


def _is_yes(s: str) -> bool:
    return str(s).strip().upper() in ("Y", "YES", "TRUE", "1", "O")


def _is_expired(expire: str) -> bool:
    """만료일자 가 오늘 이전이면 True (만료). 비어있으면 유효."""
    s = (expire or "").strip()
    if not s:
        return False
    digits = re.sub(r"[^0-9]", "", s)
    if len(digits) < 8:
        return False
    try:
        d = datetime.strptime(digits[:8], "%Y%m%d").date()
    except ValueError:
        return False
    return d < datetime.now().date()


def _split_synonyms(s: str) -> list[str]:
    if not s:
        return []
    parts = re.split(r"[,/;|·\n]+", s)
    return [p.strip() for p in parts if p.strip()]


def compose_data_type(data_type: str, length: str, scale: str) -> str:
    """데이터유형 + 길이 + 소수점 → ``VARCHAR2(50)`` / ``NUMBER(15,2)`` / ``DATE``."""
    dt = (data_type or "").strip().upper()
    if not dt:
        return ""
    ln = re.sub(r"[^0-9]", "", str(length or ""))
    sc = re.sub(r"[^0-9]", "", str(scale or ""))
    # 이미 길이가 타입에 포함돼 있으면 그대로
    if "(" in dt:
        return dt
    no_len = {"DATE", "TIMESTAMP", "CLOB", "BLOB", "LONG", "ROWID"}
    if dt in no_len or not ln:
        return dt
    if sc and sc != "0":
        return f"{dt}({ln},{sc})"
    return f"{dt}({ln})"


# ─────────────────────────────────────────────────────────────────
# Excel 로드
# ─────────────────────────────────────────────────────────────────

def _read_xlsx(path: str, sheet: str | None) -> list[tuple]:
    try:
        from openpyxl import load_workbook
    except ImportError as e:  # pragma: no cover
        raise ImportError("openpyxl 가 필요합니다: pip install openpyxl") from e
    wb = load_workbook(path, data_only=True, read_only=True)
    ws = wb[sheet] if sheet and sheet in wb.sheetnames else wb.active
    rows = [row for row in ws.iter_rows(values_only=True)]
    wb.close()
    return rows


def _load_word_rows(path: str, sheet: str | None) -> list[dict]:
    rows = _read_xlsx(path, sheet)
    if not rows:
        return []
    hr = _find_header_row(rows)
    idx = _header_index(rows[hr])
    out = []
    for row in rows[hr + 1:]:
        logical = _cell(row, idx, "logical")
        physical = _cell(row, idx, "physical")
        if not logical and not physical:
            continue
        out.append({
            "logical": logical,
            "physical": physical.upper(),
            "eng": _cell(row, idx, "eng"),
            "is_std": _is_yes(_cell(row, idx, "is_std")) if "is_std" in idx else True,
            "is_classifier": _is_yes(_cell(row, idx, "is_classifier")),
            "synonyms": _cell(row, idx, "synonyms"),
            "desc": _cell(row, idx, "desc"),
            "expire": _cell(row, idx, "expire"),
            "source": _cell(row, idx, "source"),
        })
    return out


def _load_term_rows(path: str, sheet: str | None) -> list[dict]:
    rows = _read_xlsx(path, sheet)
    if not rows:
        return []
    hr = _find_header_row(rows)
    idx = _header_index(rows[hr])
    out = []
    for row in rows[hr + 1:]:
        logical = _cell(row, idx, "logical")
        physical = _cell(row, idx, "physical")
        if not logical and not physical:
            continue
        out.append({
            "logical": logical,
            "physical": physical.upper(),
            "compose": _cell(row, idx, "compose"),
            "eng": _cell(row, idx, "eng"),
            "domain": _cell(row, idx, "domain"),
            "data_type": _cell(row, idx, "data_type"),
            "length": _cell(row, idx, "length"),
            "scale": _cell(row, idx, "scale"),
            "is_std": _is_yes(_cell(row, idx, "is_std")) if "is_std" in idx else True,
            "privacy": _cell(row, idx, "privacy"),
            "encrypt": _cell(row, idx, "encrypt"),
            "desc": _cell(row, idx, "desc"),
            "expire": _cell(row, idx, "expire"),
            "source": _cell(row, idx, "source"),
        })
    return out


# ─────────────────────────────────────────────────────────────────
# SQLite 빌드 / 재빌드 판정
# ─────────────────────────────────────────────────────────────────

_WORD_COLS = ["logical", "physical", "eng", "is_std", "is_classifier",
              "synonyms", "desc", "expire", "source"]
_TERM_COLS = ["logical", "physical", "compose", "eng", "domain", "data_type",
              "length", "scale", "is_std", "privacy", "encrypt", "desc",
              "expire", "source"]


def _mtime(path: str | None) -> str:
    if path and os.path.exists(path):
        return str(int(os.path.getmtime(path)))
    return ""


def needs_rebuild(db_path: str, word_xlsx: str | None, term_xlsx: str | None) -> bool:
    """SQLite 가 없거나 원본 Excel mtime 이 바뀌었으면 재빌드 필요."""
    if not os.path.exists(db_path):
        return True
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("SELECT key, value FROM meta")
        meta = dict(cur.fetchall())
        conn.close()
    except sqlite3.Error:
        return True
    if word_xlsx and meta.get("word_mtime", "") != _mtime(word_xlsx):
        return True
    if term_xlsx and meta.get("term_mtime", "") != _mtime(term_xlsx):
        return True
    return False


def build_std_dict(db_path: str, word_xlsx: str | None = None,
                   term_xlsx: str | None = None) -> dict:
    """단어사전/용어사전 Excel → SQLite 캐시 생성. 통계 dict 반환."""
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)

    words = _load_word_rows(word_xlsx, None) if word_xlsx else []
    terms = _load_term_rows(term_xlsx, None) if term_xlsx else []

    # 만료 항목 집계 (적재는 하되 표준 인덱스에서 제외하도록 플래그만)
    word_expired = sum(1 for w in words if _is_expired(w["expire"]))
    term_expired = sum(1 for t in terms if _is_expired(t["expire"]))

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS word")
    cur.execute("DROP TABLE IF EXISTS term")
    cur.execute("DROP TABLE IF EXISTS meta")
    cur.execute(
        "CREATE TABLE word (logical TEXT, logical_norm TEXT, physical TEXT, "
        "eng TEXT, is_std INT, is_classifier INT, synonyms TEXT, desc TEXT, "
        "expired INT, source TEXT)"
    )
    cur.execute(
        "CREATE TABLE term (logical TEXT, logical_norm TEXT, physical TEXT, "
        "compose TEXT, eng TEXT, domain TEXT, data_type TEXT, length TEXT, "
        "scale TEXT, is_std INT, privacy TEXT, encrypt TEXT, desc TEXT, "
        "expired INT, source TEXT)"
    )
    cur.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")

    cur.executemany(
        "INSERT INTO word VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(w["logical"], norm_kor(w["logical"]), w["physical"], w["eng"],
          int(w["is_std"]), int(w["is_classifier"]), w["synonyms"], w["desc"],
          int(_is_expired(w["expire"])), w["source"]) for w in words],
    )
    cur.executemany(
        "INSERT INTO term VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(t["logical"], norm_kor(t["logical"]), t["physical"], t["compose"],
          t["eng"], t["domain"], t["data_type"], t["length"], t["scale"],
          int(t["is_std"]), t["privacy"], t["encrypt"], t["desc"],
          int(_is_expired(t["expire"])), t["source"]) for t in terms],
    )
    cur.execute("CREATE INDEX ix_word_norm ON word(logical_norm)")
    cur.execute("CREATE INDEX ix_word_phys ON word(physical)")
    cur.execute("CREATE INDEX ix_term_norm ON term(logical_norm)")
    cur.execute("CREATE INDEX ix_term_phys ON term(physical)")

    meta = {
        "word_mtime": _mtime(word_xlsx),
        "term_mtime": _mtime(term_xlsx),
        "word_xlsx": word_xlsx or "",
        "term_xlsx": term_xlsx or "",
        "built_at": datetime.now().isoformat(timespec="seconds"),
    }
    cur.executemany("INSERT INTO meta VALUES (?,?)", list(meta.items()))
    conn.commit()
    conn.close()

    stats = {
        "words": len(words), "word_expired": word_expired,
        "terms": len(terms), "term_expired": term_expired,
    }
    logger.info("표준사전 빌드: 단어 %d (만료 %d) / 용어 %d (만료 %d) → %s",
                stats["words"], word_expired, stats["terms"], term_expired, db_path)
    return stats


# ─────────────────────────────────────────────────────────────────
# 인메모리 인덱스 로드
# ─────────────────────────────────────────────────────────────────

@dataclass
class WordRow:
    logical: str
    physical: str
    eng: str
    is_classifier: bool
    synonyms: list[str]
    desc: str


@dataclass
class TermRow:
    logical: str
    physical: str
    domain: str
    data_type: str  # 길이·소수점 합성된 최종 타입
    eng: str
    desc: str
    privacy: str
    encrypt: str


@dataclass
class StdDict:
    # Tier1: 용어사전 정확매칭
    term_by_logical: dict[str, TermRow] = field(default_factory=dict)  # norm논리명 → term
    term_by_physical: dict[str, TermRow] = field(default_factory=dict)  # 물리명 → term
    # Tier2: 단어사전 조합
    word_by_logical: dict[str, WordRow] = field(default_factory=dict)  # 논리명 → word
    syn_to_logical: dict[str, str] = field(default_factory=dict)  # norm(동의어/논리명) → 논리명
    word_keys_sorted: list[str] = field(default_factory=list)  # norm 논리명/동의어 (길이 desc)
    abbr_to_logical: dict[str, str] = field(default_factory=dict)  # 물리명 → 논리명
    classifier_type: dict[str, tuple[str, str]] = field(default_factory=dict)  # 분류어논리명 → (domain, data_type)
    counts: dict = field(default_factory=dict)

    def has_terms(self) -> bool:
        return bool(self.term_by_logical or self.term_by_physical)

    def has_words(self) -> bool:
        return bool(self.word_by_logical)


def load_std_dict(db_path: str) -> StdDict:
    """SQLite → 매칭용 인메모리 인덱스. 만료/비표준 항목은 정확매칭에서 제외."""
    sd = StdDict()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # 용어사전 — 표준이고 만료 안 된 것만 정확매칭 인덱스에 적재
    for r in cur.execute("SELECT * FROM term"):
        if r["expired"]:
            continue
        tr = TermRow(
            logical=r["logical"], physical=r["physical"], domain=r["domain"],
            data_type=compose_data_type(r["data_type"], r["length"], r["scale"]),
            eng=r["eng"], desc=r["desc"], privacy=r["privacy"], encrypt=r["encrypt"],
        )
        nl = r["logical_norm"]
        if nl and nl not in sd.term_by_logical:
            sd.term_by_logical[nl] = tr
        if r["physical"] and r["physical"] not in sd.term_by_physical:
            sd.term_by_physical[r["physical"]] = tr

    # 분류어별 대표 (도메인, 데이터유형) — 용어사전 물리명 마지막 토큰 기준 최빈값
    classifier_samples: dict[str, Counter] = defaultdict(Counter)

    # 단어사전
    word_keys: set[str] = set()
    for r in cur.execute("SELECT * FROM word"):
        if r["expired"]:
            continue
        syns = _split_synonyms(r["synonyms"])
        wr = WordRow(
            logical=r["logical"], physical=r["physical"], eng=r["eng"],
            is_classifier=bool(r["is_classifier"]), synonyms=syns, desc=r["desc"],
        )
        if r["is_std"] and r["logical"] and r["logical"] not in sd.word_by_logical:
            sd.word_by_logical[r["logical"]] = wr
        if r["physical"] and r["physical"] not in sd.abbr_to_logical:
            sd.abbr_to_logical[r["physical"]] = r["logical"]
        # 동의어/논리명 → 논리명 매핑 (표준 단어로 귀결)
        for key in [r["logical"], *syns]:
            nk = norm_kor(key)
            if nk and nk not in sd.syn_to_logical:
                sd.syn_to_logical[nk] = r["logical"]
                word_keys.add(nk)

    # 분류어 데이터유형 추론: 용어사전 물리명 끝 토큰 == 분류어 물리명 인 경우 집계
    classifier_phys = {
        w.physical: w.logical for w in sd.word_by_logical.values()
        if w.is_classifier and w.physical
    }
    for tr in sd.term_by_physical.values():
        if not tr.data_type:
            continue
        last = tr.physical.split("_")[-1] if tr.physical else ""
        logical = classifier_phys.get(last)
        if logical:
            classifier_samples[logical][(tr.domain, tr.data_type)] += 1
    for logical, ctr in classifier_samples.items():
        (domain, dtype), _ = ctr.most_common(1)[0]
        sd.classifier_type[logical] = (domain, dtype)

    sd.word_keys_sorted = sorted(word_keys, key=len, reverse=True)
    sd.counts = {
        "terms": len(sd.term_by_logical),
        "words": len(sd.word_by_logical),
        "synonyms": len(sd.syn_to_logical),
        "classifiers": len(sd.classifier_type),
    }
    conn.close()
    logger.info("표준사전 로드: 용어 %d / 단어 %d / 동의어 %d / 분류어타입 %d",
                sd.counts["terms"], sd.counts["words"],
                sd.counts["synonyms"], sd.counts["classifiers"])
    return sd


def ensure_std_dict(db_path: str, word_xlsx: str | None, term_xlsx: str | None,
                    force: bool = False) -> StdDict:
    """필요 시 빌드 후 로드. (mtime 변경/강제 시 재빌드)"""
    if force or needs_rebuild(db_path, word_xlsx, term_xlsx):
        if not (word_xlsx or term_xlsx):
            raise FileNotFoundError(
                f"표준사전 SQLite 가 없고 (--word-dict/--term-dict) 도 없습니다: {db_path}"
            )
        build_std_dict(db_path, word_xlsx, term_xlsx)
    return load_std_dict(db_path)
