"""AS-IS 스키마 → TO-BE 속성명 추천 엔진 (계층형).

Tier 1  정확매칭   용어사전 논리명/물리명 해시 조회 (결정적, 최고신뢰)
Tier 2  단어조합   단어사전+동의어로 코멘트/물리명 분해 → 물리명 조합,
                   분류어로 도메인·데이터유형 추론
Tier 3  RAG        위 실패한 free-text 코멘트만 유사 표준용어 top-k 후보 검색
Tier 4  LLM        미매칭 단편을 LLM 이 표준 물리명으로 추천 (RAG 후보 참고)

결정적 코어(Tier1·2)는 임베딩/LLM 없이도 독립 동작한다.
"""

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field

from .std_dict import StdDict, TermRow, norm_kor

logger = logging.getLogger(__name__)

ORACLE_NAME_MAX = 30  # Oracle 11g 식별자 길이
LOW_CONFIDENCE = 0.7

# 물리명 분해용 (terms_collector 와 동일 규약)
_CAMEL_RE = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z][a-z]|\b)")


@dataclass
class TokenMatch:
    frag: str            # 원본 단편 (한글 또는 영문 토큰)
    logical: str = ""    # 표준 논리명 (매칭 시)
    abbr: str = ""       # 표준 물리명 (매칭 시)
    matched: bool = False
    via: str = "unmatched"  # word / synonym / abbr / fuzzy / llm / unmatched


@dataclass
class ColumnRec:
    table: str
    column: str
    comment: str
    basis: str            # comment / column / none
    tokens: list[TokenMatch] = field(default_factory=list)
    tobe_name: str = ""
    domain: str = ""
    data_type: str = ""
    confidence: float = 0.0
    tier: str = "미매칭"
    note: str = ""

    @property
    def unmatched_frags(self) -> list[str]:
        return [t.frag for t in self.tokens if not t.matched]


@dataclass
class RecommendStats:
    total: int = 0
    tier1: int = 0       # 용어사전 정확매칭
    already_std: int = 0  # AS-IS 가 이미 표준 물리명
    tier2: int = 0       # 단어조합
    tier_llm: int = 0    # RAG/LLM 보조
    unmatched: int = 0   # 끝내 미매칭
    low_conf: int = 0
    too_long: int = 0
    elapsed_sec: float = 0.0


# ─────────────────────────────────────────────────────────────────
# 토크나이저
# ─────────────────────────────────────────────────────────────────

def _split_physical(name: str) -> list[str]:
    """물리명 → 영문 토큰 (SNAKE / camel / Pascal)."""
    tokens: list[str] = []
    for part in name.split("_"):
        if not part:
            continue
        camel = _CAMEL_RE.findall(part)
        tokens.extend(p.upper() for p in camel) if camel else tokens.append(part.upper())
    return tokens


def tokenize_korean(comment: str, sd: StdDict) -> list[TokenMatch]:
    """한글 코멘트를 표준 단어(논리명/동의어) 기준 최장일치 분해.

    인식 안 되는 구간은 미매칭 단편으로 누적 (1글자 조사성 잡음은 버림).
    """
    text = norm_kor(comment)
    if not text:
        return []
    maxlen = len(sd.word_keys_sorted[0]) if sd.word_keys_sorted else 0
    tokens: list[TokenMatch] = []
    i, n = 0, len(text)
    pending = ""  # 미매칭 누적

    def flush():
        nonlocal pending
        frag = pending.strip()
        pending = ""
        if len(frag) >= 1:
            tokens.append(TokenMatch(frag=frag, matched=False, via="unmatched"))

    while i < n:
        hit = None
        for L in range(min(maxlen, n - i), 0, -1):
            sub = text[i:i + L]
            logical = sd.syn_to_logical.get(sub)
            if logical:
                hit = (L, logical)
                break
        if hit:
            if pending:
                flush()
            L, logical = hit
            # 표준여부 N 단어도 물리명이 있으면 사용 (logical_to_abbr 는 전 단어 포함)
            abbr = sd.logical_to_abbr.get(logical, "")
            via = "word" if text[i:i + L] == norm_kor(logical) else "synonym"
            tokens.append(TokenMatch(frag=text[i:i + L], logical=logical,
                                     abbr=abbr, matched=True, via=via))
            i += L
        else:
            pending += text[i]
            i += 1
    if pending:
        flush()
    # 1글자 미매칭 단편(조사/잡음)은 제거
    return [t for t in tokens if t.matched or len(t.frag) >= 2]


# ─────────────────────────────────────────────────────────────────
# 도메인/타입 추론 + 조합
# ─────────────────────────────────────────────────────────────────

def _infer_domain_type(tokens: list[TokenMatch], sd: StdDict) -> tuple[str, str]:
    """마지막 분류어 토큰으로 (도메인, 데이터유형) 추론.

    도메인명이 도메인사전에 있으면 그 권위 데이터유형으로 보정.
    """
    for t in reversed(tokens):
        if t.logical and t.logical in sd.classifier_type:
            domain, dtype = sd.classifier_type[t.logical]
            if domain:
                dt2, _single = sd.resolve_domain_type(domain)
                if dt2:
                    dtype = dt2
            return domain, dtype
    return ("", "")


def _compose_name(tokens: list[TokenMatch]) -> str:
    """매칭 토큰은 표준 약어로, 미매칭 단편은 «...» 로 감싸 표준 아님을 명시.

    미매칭 한글 단편을 물리명에 그대로 흘려 넣으면 'TO-BE 물리명에 한글'
    처럼 보이므로, 표준화 안 된 부분임이 한눈에 보이게 마커로 감싼다.
    """
    parts = []
    for t in tokens:
        if t.abbr:
            parts.append(t.abbr)
        elif t.frag:
            parts.append(f"«{t.frag.upper()}»")
    return "_".join(p for p in parts if p)


def _finalize(rec: ColumnRec) -> ColumnRec:
    matched = sum(1 for t in rec.tokens if t.matched)
    total = len(rec.tokens)
    rec.confidence = round(matched / total, 2) if total else 0.0
    if rec.tobe_name and len(rec.tobe_name) > ORACLE_NAME_MAX:
        rec.note = (rec.note + " / " if rec.note else "") + \
            f"길이초과({len(rec.tobe_name)}/{ORACLE_NAME_MAX})"
    return rec


# ─────────────────────────────────────────────────────────────────
# 결정적 추천 (Tier 1 · 2)
# ─────────────────────────────────────────────────────────────────

def _apply_term(rec: ColumnRec, tr: TermRow, tier: str, note: str) -> ColumnRec:
    rec.tobe_name = tr.physical
    rec.domain = tr.domain
    rec.data_type = tr.data_type
    rec.tier = tier
    rec.note = note
    rec.tokens = [TokenMatch(frag=rec.comment or rec.column, logical=tr.logical or "",
                             abbr=tr.physical, matched=True, via="term")]
    rec.confidence = 1.0
    return rec


def _from_comment(rec: ColumnRec, sd: StdDict, cleaned: str) -> ColumnRec:
    norm = norm_kor(cleaned)
    tr = sd.term_by_logical.get(norm)
    if tr:
        return _apply_term(rec, tr, "정확매칭(용어)", "용어사전 논리명 1:1 매칭")
    # 단어조합
    rec.tokens = tokenize_korean(cleaned, sd)
    rec.tobe_name = _compose_name(rec.tokens)
    rec.domain, rec.data_type = _infer_domain_type(rec.tokens, sd)
    if rec.tokens and all(t.matched for t in rec.tokens):
        rec.tier = "단어조합"
    elif any(t.matched for t in rec.tokens):
        rec.tier = "단어조합(부분)"
    else:
        rec.tier = "미매칭"
    return _finalize(rec)


def _from_physical(rec: ColumnRec, sd: StdDict) -> ColumnRec:
    up = rec.column.upper()
    tr = sd.term_by_physical.get(up)
    if tr:
        rec.tokens = [TokenMatch(frag=up, logical=tr.logical or "", abbr=up,
                                 matched=True, via="term")]
        rec.tobe_name = up
        rec.domain, rec.data_type = tr.domain, tr.data_type
        rec.tier = "이미표준"
        rec.note = "AS-IS 물리명이 용어사전 표준"
        rec.confidence = 1.0
        return rec
    # 물리명 분해 → 표준 약어 검증
    for tok in _split_physical(rec.column):
        logical = sd.abbr_to_logical.get(tok)
        if logical:
            rec.tokens.append(TokenMatch(frag=tok, logical=logical, abbr=tok,
                                         matched=True, via="abbr"))
        else:
            rec.tokens.append(TokenMatch(frag=tok, matched=False, via="unmatched"))
    rec.tobe_name = _compose_name(rec.tokens)
    rec.domain, rec.data_type = _infer_domain_type(rec.tokens, sd)
    if rec.tokens and all(t.matched for t in rec.tokens):
        rec.tier = "이미표준" if rec.tobe_name == up else "단어조합"
        rec.note = "모든 토큰이 표준 약어" if rec.tobe_name == up else ""
    elif any(t.matched for t in rec.tokens):
        rec.tier = "단어조합(부분)"
    else:
        rec.tier = "미매칭"
    return _finalize(rec)


# 코멘트 노이즈: 괄호 주석 + enrich-schema 등이 붙이는 마커
_COMMENT_PAREN_RE = re.compile(r"\([^)]*\)|\[[^\]]*\]|（[^）]*）|【[^】]*】")
_COMMENT_MARK_RE = re.compile(r"(LLM\s*추천|자동\s*생성|추천\s*값|미정|TODO|N/?A)", re.I)


def _clean_comment(comment: str | None) -> str:
    """코멘트에서 괄호 주석·(LLM추천) 류 마커 제거 → 순수 논리명만 남김."""
    if not comment:
        return ""
    c = _COMMENT_PAREN_RE.sub(" ", comment)   # (LLM추천), (PK), (YYYYMMDD) 등 제거
    c = _COMMENT_MARK_RE.sub(" ", c)
    return re.sub(r"\s+", " ", c).strip()


def recommend_column(table: str, column: str, comment: str | None,
                     sd: StdDict) -> ColumnRec:
    raw = (comment or "").strip()
    cleaned = _clean_comment(raw)
    # 마커만 있던 코멘트는 비게 됨 → 물리명 기준으로 폴백
    rec = ColumnRec(table=table, column=column, comment=raw,
                    basis="comment" if cleaned else "column")
    if cleaned:
        rec.comment = raw  # 리포트엔 원본 코멘트 노출
        return _from_comment(rec, sd, cleaned)
    return _from_physical(rec, sd)


def recommend_schema(schema: dict, sd: StdDict) -> tuple[list[ColumnRec], RecommendStats]:
    """결정적 추천 (Tier 1·2)."""
    recs: list[ColumnRec] = []
    for table in schema.get("tables", []):
        for col in table.get("columns", []):
            recs.append(recommend_column(
                table["name"], col["column_name"], col.get("comment"), sd))
    return recs, _collect_stats(recs)


def _collect_stats(recs: list[ColumnRec]) -> RecommendStats:
    st = RecommendStats(total=len(recs))
    for r in recs:
        if r.tier == "정확매칭(용어)":
            st.tier1 += 1
        elif r.tier == "이미표준":
            st.already_std += 1
        elif r.tier.startswith("단어조합"):
            st.tier2 += 1
        elif r.tier in ("RAG+LLM", "LLM"):
            st.tier_llm += 1
        else:
            st.unmatched += 1
        if 0 < r.confidence < LOW_CONFIDENCE:
            st.low_conf += 1
        if "길이초과" in r.note:
            st.too_long += 1
    return st


def needs_assist(rec: ColumnRec) -> bool:
    """RAG/LLM 보조가 필요한가 — 미매칭이거나 부분매칭(미해결 단편 존재)."""
    return rec.tier == "미매칭" or bool(rec.unmatched_frags)


# ─────────────────────────────────────────────────────────────────
# Tier 3: RAG — 용어사전 임베딩 + 후보 검색
# ─────────────────────────────────────────────────────────────────

STD_TERMS_COLLECTION = "std_terms"


def _term_doc(tr) -> str:
    """임베딩할 문서 텍스트 — 한글 논리명을 주신호로, 설명을 보조로.

    사전성 데이터는 '논리명' 이 곧 검색 대상(원자적 레코드)이므로 논리명을
    앞세우고, 설명은 free-text 코멘트 매칭 보조로만 덧붙인다. 영문명은
    한글 쿼리 임베딩을 희석하므로 문서 텍스트엔 넣지 않고 메타데이터로만 보관.
    """
    logical = (tr.logical or "").strip()
    desc = (tr.desc or "").strip()
    if logical and desc and desc != logical:
        return f"{logical}. {desc}"
    return logical or desc or tr.physical


def embed_std_terms(sd: StdDict, config: dict, db_path: str = "./vectordb") -> int:
    """용어사전을 ChromaDB 에 임베딩 (Tier3 후보검색용).

    전략 (사전성 데이터):
    - **1 용어 = 1 벡터** (원자적 레코드, 청킹 불필요)
    - 문서 텍스트는 **논리명 우선 + 설명** (`_term_doc`), 영문명은 메타데이터로
    - **cosine** 거리 (텍스트 임베딩 표준)
    - 재적재 시 **컬렉션 초기화** 후 재생성 (stale 벡터 방지)
    - 모델별 prefix (`embedding.doc_prefix`, e5/bge 의 ``passage:``) 지원
    - 메타데이터에 논리명/물리명/도메인/데이터유형/영문명 보관 (재조회 불필요)
    """
    from .vector_store import (
        get_embedding_client,
        init_vectordb,
        _get_embeddings_batch,
    )

    # 논리명 기준 원자적 레코드 (중복 논리명 제거)
    seen: set[str] = set()
    terms = []
    for tr in sd.term_by_logical.values():
        key = (tr.logical or tr.physical).strip()
        if key and key not in seen:
            seen.add(key)
            terms.append(tr)
    if not terms:
        return 0

    client = init_vectordb(db_path)
    emb = get_embedding_client(config)
    ecfg = config.get("embedding", {})
    model = ecfg.get("model", "nomic-embed-text")
    doc_prefix = ecfg.get("doc_prefix", "")  # e5/bge: "passage: "

    # 재적재 stale 제거 — 컬렉션 초기화 후 cosine 으로 재생성
    try:
        client.delete_collection(STD_TERMS_COLLECTION)
    except Exception:  # noqa: BLE001 — 없으면 무시
        pass
    col = client.get_or_create_collection(
        name=STD_TERMS_COLLECTION, metadata={"hnsw:space": "cosine"})

    stored = 0
    for i in range(0, len(terms), 32):  # 32 = 임베딩 API 배치 (청크사이즈 아님)
        batch = terms[i:i + 32]
        docs = [doc_prefix + _term_doc(tr) for tr in batch]
        try:
            vecs = _get_embeddings_batch(emb, model, docs)
        except Exception as e:  # noqa: BLE001
            logger.error("std_terms 임베딩 배치 실패: %s", e)
            continue
        col.upsert(
            ids=[f"t{i + j}" for j in range(len(batch))],
            documents=docs,
            embeddings=vecs,
            metadatas=[{"logical": tr.logical, "physical": tr.physical,
                        "domain": tr.domain, "data_type": tr.data_type,
                        "eng": tr.eng} for tr in batch],
        )
        stored += len(batch)
    logger.info("std_terms 임베딩: %d 건 저장 (cosine, 논리명 기준)", stored)
    return stored


def has_std_terms_collection(db_path: str) -> bool:
    """RAG용 std_terms 임베딩 컬렉션이 존재하고 비어있지 않은지."""
    try:
        from .vector_store import init_vectordb
        client = init_vectordb(db_path)
        col = client.get_collection(STD_TERMS_COLLECTION)
        return col.count() > 0
    except Exception:  # noqa: BLE001 — 컬렉션 없음/chroma 미설치
        return False


def rag_candidates(comment: str, config: dict, db_path: str,
                   k: int = 5) -> list[dict]:
    """코멘트와 유사한 표준용어 top-k 후보 (논리명/물리명/도메인/데이터유형).

    쿼리도 문서와 대칭이 되도록 코멘트 노이즈((LLM추천) 등)를 정리하고
    모델별 query prefix (e5/bge 의 ``query:``) 를 적용한다.
    """
    from .vector_store import get_embedding_client, init_vectordb, _get_embedding
    q = _clean_comment(comment) or (comment or "").strip()
    if not q:
        return []
    try:
        client = init_vectordb(db_path)
        col = client.get_collection(STD_TERMS_COLLECTION)
    except Exception:
        return []
    emb = get_embedding_client(config)
    ecfg = config.get("embedding", {})
    model = ecfg.get("model", "nomic-embed-text")
    query_prefix = ecfg.get("query_prefix", "")  # e5/bge: "query: "
    try:
        qv = _get_embedding(emb, model, query_prefix + q)
        res = col.query(query_embeddings=[qv], n_results=k)
    except Exception as e:  # noqa: BLE001
        logger.error("RAG 후보 검색 실패: %s", e)
        return []
    metas = (res.get("metadatas") or [[]])[0]
    return [m for m in metas if m]


# ─────────────────────────────────────────────────────────────────
# Tier 4: LLM 보조
# ─────────────────────────────────────────────────────────────────

def _llm_client(config: dict):
    from openai import OpenAI
    cfg = config.get("llm", {})
    return OpenAI(
        api_key=os.environ.get("LLM_API_KEY") or cfg.get("api_key", "ollama"),
        base_url=os.environ.get("LLM_API_BASE") or cfg.get("api_base",
                                                            "http://localhost:11434/v1"),
    )


def _build_llm_prompt(items: list[dict]) -> str:
    lines = []
    for it in items:
        cand = ""
        if it.get("candidates"):
            cand = " / 표준후보: " + ", ".join(
                f"{c.get('logical','')}→{c.get('physical')}" for c in it["candidates"][:5])
        lines.append(
            f"{it['idx']}. AS-IS={it['asis']} | 의미={it['meaning']}"
            f" | 미매칭단어={', '.join(it['frags']) or '-'}{cand}")
    body = "\n".join(lines)
    return (
        "당신은 한국 공공/금융 SI 의 데이터 표준화 전문가입니다. "
        "AS-IS 컬럼을 TO-BE 표준 물리명(영문 대문자+언더스코어 약어 조합)으로 추천하세요.\n"
        "표준 약어를 모르면 일반적 약어를 쓰되 confidence 를 낮추세요. "
        "후보가 적절하면 그 물리명을 사용하세요.\n\n"
        f"## 대상 ({len(items)}건)\n{body}\n\n"
        "각 항목을 아래 JSON 배열로만 응답 (설명/코드펜스 금지):\n"
        '[{"idx":번호,"tobe_name":"물리명","data_type":"타입또는빈값",'
        '"confidence":0.0~1.0,"note":"근거"}]'
    )


def _parse_json_array(text: str) -> list[dict]:
    text = text.strip()
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if m:
        text = m.group(1).strip()
    if not text.startswith("["):
        s, e = text.find("["), text.rfind("]")
        if s != -1 and e > s:
            text = text[s:e + 1]
    return json.loads(text)


def assist_with_llm(recs: list[ColumnRec], sd: StdDict, config: dict,
                    use_rag: bool, db_path: str, top_k: int = 5,
                    batch_size: int = 20, timeout: int = 120) -> int:
    """미해결 컬럼을 RAG 후보 + LLM 으로 보강. 갱신 건수 반환."""
    targets = [r for r in recs if needs_assist(r)]
    if not targets:
        return 0

    client = _llm_client(config)
    model = os.environ.get("LLM_MODEL") or config.get("llm", {}).get("model", "llama3")

    print(f"  LLM 보조 대상: {len(targets)}건 / model: {model} / RAG: {use_rag}")
    updated = 0
    for start in range(0, len(targets), batch_size):
        chunk = targets[start:start + batch_size]
        items = []
        for i, r in enumerate(chunk):
            cands = []
            if use_rag and r.comment:
                cands = rag_candidates(r.comment, config, db_path, top_k)
            items.append({
                "idx": i,
                "asis": r.column,
                "meaning": r.comment or "(코멘트 없음)",
                "frags": r.unmatched_frags or ([r.column] if not r.tokens else []),
                "candidates": cands,
            })
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "반드시 유효한 JSON 배열만 출력."},
                    {"role": "user", "content": _build_llm_prompt(items)},
                ],
                temperature=0.0, timeout=timeout,
            )
            parsed = _parse_json_array(resp.choices[0].message.content or "")
        except Exception as e:  # noqa: BLE001
            logger.error("LLM 보조 배치 실패: %s", e)
            time.sleep(1)
            continue
        by_idx = {int(p.get("idx", -1)): p for p in parsed if isinstance(p, dict)}
        for i, r in enumerate(chunk):
            p = by_idx.get(i)
            if not p or not p.get("tobe_name"):
                continue
            r.tobe_name = str(p["tobe_name"]).upper().strip()
            if p.get("data_type"):
                r.data_type = str(p["data_type"]).strip()
            r.tier = "RAG+LLM" if (use_rag and r.comment) else "LLM"
            try:
                r.confidence = max(0.0, min(1.0, float(p.get("confidence", 0.5))))
            except (TypeError, ValueError):
                r.confidence = 0.5
            note = str(p.get("note", "")).strip()
            r.note = (r.note + " / " if r.note else "") + (note or "LLM 추천")
            if len(r.tobe_name) > ORACLE_NAME_MAX:
                r.note += f" / 길이초과({len(r.tobe_name)})"
            updated += 1
    print(f"  LLM 보조 완료: {updated}건 갱신")
    return updated
