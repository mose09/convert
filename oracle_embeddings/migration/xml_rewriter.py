"""MyBatis XML 재작성 (docs/migration/spec.md §8).

한 mapper XML 파일을 받아 각 statement 별로 dynamic path 를 전개 → sql_rewriter
로 변환 → ``ChangeItem`` 집합을 얻고, 그 결과를 원본 XML 의 text/CDATA 노드에
word-boundary 치환으로 되돌린다. ``<if>`` / ``<choose>`` / ``<foreach>`` 같은
동적 태그 구조는 그대로 유지된다.

치환 전략
--------

sqlglot AST → XML 복원은 주석/들여쓰기/동적 태그 경계를 모두 날리므로 사용하지
않는다. 대신:

1. Statement + ``<sql>`` 조각마다 max-path 를 렌더링해 ``rewrite_sql`` 에 넣고,
   ``changed_items`` 에서 (AS-IS → TO-BE) 쌍을 수집한다.
2. 모든 쌍을 합쳐 word-boundary 정규식을 컴파일한다.
   (``CUST_NM`` 이 ``CUST_NMM`` 같은 longer identifier 와 헷갈리지 않도록).
3. XML 트리를 walk 하며 ``elem.text`` / ``elem.tail`` 에 치환을 적용한다.

이 접근은 동일 컬럼명이 서로 다른 테이블에서 다른 rename 을 가질 때 오해할
수 있지만 (드뭄), 실제 레거시 프로젝트에서는 거의 발생하지 않는다. 애매한
경우에는 LLM fallback (Step 13) 으로 넘긴다.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from lxml import etree

from ..mybatis_parser import _read_file_safe
from .dynamic_sql_expander import build_sql_includes, expand_paths
from .ibatis_translator import translate_ibatis_to_mybatis
from .mapping_model import ChangeItem, Mapping, RewriteResult, SqlType
from .sql_rewriter import rewrite_sql

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public result type
# ---------------------------------------------------------------------------


@dataclass
class XmlRewriteOutcome:
    """Result of :func:`rewrite_xml`. ``tree`` is the modified XML tree —
    callers decide where/how to serialize (see :func:`serialize_tree`).
    """

    file_path: Path
    namespace: str
    results: List[RewriteResult] = field(default_factory=list)
    tree: Optional[etree._ElementTree] = None
    parse_error: Optional[str] = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


_STATEMENT_TAGS = {"select", "insert", "update", "delete"}
_SQL_TAG = "sql"


def rewrite_xml(
    xml_path: Path,
    mapping: Mapping,
) -> XmlRewriteOutcome:
    """Rewrite every SQL statement in a MyBatis mapper XML.

    Returns :class:`XmlRewriteOutcome`. On parse error the ``tree`` is None
    and ``parse_error`` carries the reason — the caller typically falls back
    to copying the file verbatim and marking every statement as PARSE_FAIL.
    """

    try:
        text = _read_file_safe(str(xml_path))
    except Exception as exc:
        return XmlRewriteOutcome(
            file_path=Path(xml_path),
            namespace="",
            parse_error=f"read failed: {exc}",
        )

    try:
        parser = etree.XMLParser(
            remove_blank_text=False,
            strip_cdata=False,  # keep CDATA text, lxml won't re-wrap though
            resolve_entities=False,
        )
        tree = etree.ElementTree(etree.fromstring(text.encode("utf-8"), parser))
    except etree.XMLSyntaxError as exc:
        return XmlRewriteOutcome(
            file_path=Path(xml_path),
            namespace="",
            parse_error=f"XML parse failed: {exc}",
        )

    root = tree.getroot()

    # Capture each statement's verbatim body BEFORE any translation so the
    # AS-IS comment block can show the user's original iBatis layout
    # (newlines, tabs, ``<isNotNull>`` / ``<dynamic>`` etc.) untouched.
    # Keyed on ``stmt.get("id")`` (semantic id) — lxml's Python proxy
    # wrappers regenerate per ``root.iter()`` call so ``id(stmt)`` is
    # unstable across iterations.
    pre_namespace = root.get("namespace", "") or ""
    raw_by_stmt: Dict[Tuple[str, str], str] = {
        (pre_namespace, stmt.get("id", "") or ""): _capture_stmt_inner_raw(stmt)
        for stmt in root.iter()
        if isinstance(stmt.tag, str) and _local(stmt.tag) in _STATEMENT_TAGS
    }

    # iBatis 2.x → MyBatis 3.x preflight. No-op on an already-MyBatis tree.
    # Doing this before downstream walking lets sql_rewriter / dynamic_sql
    # _expander treat everything as canonical MyBatis.
    translate_ibatis_to_mybatis(tree)

    namespace = root.get("namespace", "") or ""
    sql_includes = build_sql_includes(root)

    statements = [e for e in root.iter() if _local(e.tag) in _STATEMENT_TAGS]

    # Rewrite each statement AND each <sql> fragment so references stay
    # consistent when different statements include the same <sql> body.
    results: List[RewriteResult] = []
    all_changes: List[ChangeItem] = []

    for stmt in statements:
        rr = _rewrite_statement(stmt, sql_includes, mapping, xml_path, namespace)
        # Override post-translate capture with the pre-translate snapshot so
        # the AS-IS block shows the user's original iBatis form verbatim.
        pre = raw_by_stmt.get((pre_namespace, stmt.get("id", "") or ""))
        if pre is not None:
            rr.as_is_raw = pre
        results.append(rr)
        all_changes.extend(rr.changed_items)

    for sql_elem in sql_includes.values():
        # Fragment rewrite purely for text-substitution consistency; not
        # reported as a statement.
        frag_paths = expand_paths(sql_elem, sql_includes=sql_includes)
        if not frag_paths:
            continue
        frag_outcome = rewrite_sql(frag_paths[0].rendered_sql, mapping)
        all_changes.extend(frag_outcome.changed_items)

    subs = _compile_substitutions(all_changes)
    if subs:
        _apply_subs_to_tree(root, subs)

    return XmlRewriteOutcome(
        file_path=Path(xml_path),
        namespace=namespace,
        results=results,
        tree=tree,
    )


def serialize_tree(tree: etree._ElementTree, out_path: Path) -> None:
    """Write ``tree`` back to disk preserving the XML declaration and DTD.

    Uses ``pretty_print=False`` so we don't reformat the author's layout.
    Post-processes the serialized bytes to insert a linebreak around any
    ``<![CDATA[...]]>`` block that lxml emits flush against an adjacent tag
    — lxml's API offers no way to control whitespace before/after a CDATA
    section that's been attached via ``etree.CDATA(...)`` on ``.tail``, so
    a careful string-level fixup is the simplest path.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    raw = etree.tostring(
        tree,
        encoding="utf-8",
        xml_declaration=True,
        pretty_print=False,
        standalone=None,
    )
    out_path.write_bytes(_pretty_cdata_breaks(_localize_cdata_operators(raw)))


# 텍스트 영역(태그 밖)에서 escape 된 비교 연산자만 국소 CDATA 로 감싼다.
# 긴 것 우선 (``&lt;&gt;`` 가 ``&lt;`` 보다 먼저). 속성값/주석 안의 연산자는
# 건드리지 않는다 — 분리 시 태그 토큰은 그대로 두기 때문 (아래 참고).
_ESCAPED_OP_RE = re.compile(rb"&lt;=|&gt;=|&lt;&gt;|&lt;|&gt;")
_TAG_SPLIT_RE = re.compile(rb"(<[^>]*>)")


def _localize_cdata_operators(xml_bytes: bytes) -> bytes:
    """``A.STS &lt;&gt; 'D'`` → ``A.STS <![CDATA[<>]]> 'D'``.

    lxml 이 SQL 본문의 ``<`` / ``>`` 비교 연산자를 ``&lt;`` / ``&gt;`` 로
    escape 하는데, MyBatis 관용대로 그 **연산자만** ``<![CDATA[..]]>`` 로
    국소 래핑한다 (SELECT 전체를 감싸지 않음).

    안전성: ``<[^>]*>`` 로 split 하면 실제 태그(``<select>`` / ``<if ...>``)
    와 속성값(``test="a &lt; b"`` — 이스케이프 엔티티에 literal ``>`` 가
    없어 태그 토큰 안에 그대로 포함)은 토큰으로 분리돼 건드리지 않는다.
    주석 안의 연산자는 raw ``<>`` 라 ``&lt;``/``&gt;`` 가 아니므로 매칭 안 됨.
    """
    def _wrap(m: "re.Match") -> bytes:
        op = m.group(0).replace(b"&lt;", b"<").replace(b"&gt;", b">")
        return b"<![CDATA[" + op + b"]]>"

    parts = _TAG_SPLIT_RE.split(xml_bytes)
    # split 결과: 짝수 인덱스 = 태그 밖 텍스트, 홀수 인덱스 = 태그 토큰
    for i in range(0, len(parts), 2):
        if b"&lt;" in parts[i] or b"&gt;" in parts[i]:
            parts[i] = _ESCAPED_OP_RE.sub(_wrap, parts[i])
    return b"".join(parts)


# Post-process patterns. Each replacement preserves the original characters
# and only inserts whitespace — never deletes anything — so the round-trip
# stays valid XML and idempotent on already-broken outputs.
_CDATA_AFTER_COMMENT_RE = re.compile(rb"-->(<!\[CDATA\[)")
_CDATA_AFTER_OPEN_TAG_RE = re.compile(rb"(<[A-Za-z][^>]*>)(<!\[CDATA\[)")
_CDATA_BEFORE_CLOSE_TAG_RE = re.compile(rb"(\]\]>)(</[A-Za-z])")


def _pretty_cdata_breaks(xml_bytes: bytes) -> bytes:
    """Insert ``\\n  `` between adjacent ``-->`` / open-tag / close-tag and a
    neighbouring CDATA section so the SQL body lines up under the SELECT.

    Handles three flush-against-tag patterns:

    1. ``--><![CDATA[`` — comment close immediately followed by CDATA open
       (the AS-IS / SUGGESTED comment block followed by the body)
    2. ``<select id="x"><![CDATA[`` — element open followed by CDATA (rare;
       only happens when the body is set on ``stmt.text`` directly)
    3. ``]]></select>`` — CDATA close flush against the parent's close tag
    """
    indent = b"\n  "
    xml_bytes = _CDATA_AFTER_COMMENT_RE.sub(b"-->" + indent + rb"\1", xml_bytes)
    xml_bytes = _CDATA_AFTER_OPEN_TAG_RE.sub(rb"\1" + indent + rb"\2", xml_bytes)
    xml_bytes = _CDATA_BEFORE_CLOSE_TAG_RE.sub(rb"\1" + indent + rb"\2", xml_bytes)
    return xml_bytes


def annotate_statements(
    tree: etree._ElementTree,
    results: List[RewriteResult],
    *,
    preserve_as_is: bool = True,
    force_show_to_be: bool = False,
) -> None:
    """Prepend a migration metadata comment to each statement (docs spec §12.2).

    Writes two comment blocks at the top of every ``<select>/<insert>/
    <update>/<delete>`` element:

    1. ``MIGRATION: <sql_id>`` summary (Status / Method / Applied / Changed /
       Stage A / Stage B / Notes).
    2. ``AS-IS (original)`` with the max-path AS-IS SQL — only when
       ``preserve_as_is`` is True.

    UNRESOLVED / NEEDS_LLM rows keep their AS-IS SQL as the active statement
    body (xml_rewriter left them untouched). The suggested TO-BE from
    ``rr.to_be_sql`` is emitted inside the metadata block as a ``SUGGESTED``
    comment so it's visible but never executed.

    ``force_show_to_be=True`` (used by ``--format-only``) emits the SUGGESTED
    block for *every* row whose to_be_sql differs from as_is_sql — useful as
    a visual preview of the formatter output even when the row's status is
    AUTO and the XML body itself wasn't replaced.
    """
    by_id = {
        (r.namespace or "", r.sql_id or ""): r for r in results
    }
    root = tree.getroot()
    ns_attr = root.get("namespace", "") or ""

    for stmt in root.iter():
        tag = _local(stmt.tag)
        if tag not in _STATEMENT_TAGS:
            continue
        rr = by_id.get((ns_attr, stmt.get("id", "") or ""))
        if rr is None:
            continue

        # Build comment text(s) and place them BEFORE the SQL body text,
        # matching docs/migration/spec.md §12.2: all metadata/AS-IS/SUGGESTED
        # comments come first, then the body SQL, then any dynamic-tag
        # children. lxml's ``elem.insert(0, comment)`` would put the comment
        # AFTER ``elem.text`` (the SQL body) — so we explicitly relocate the
        # body text onto the last comment's ``.tail``.
        blocks: List[str] = [_format_metadata_block(rr)]
        if preserve_as_is and rr.as_is_sql:
            blocks.append(_format_as_is_block(rr))
        show_to_be = (
            rr.status in ("UNRESOLVED", "NEEDS_LLM") or force_show_to_be
        )
        if show_to_be and rr.to_be_sql and rr.to_be_sql != rr.as_is_sql:
            blocks.append(_format_suggested_block(rr.to_be_sql))

        if not blocks:
            continue

        body_text = stmt.text or ""
        stmt.text = "\n  "  # leading indent before the first comment

        comments = [etree.Comment(_sanitize_for_xml(text)) for text in blocks]
        for i, comment in enumerate(comments):
            stmt.insert(i, comment)
            # Spacer between comments (overwritten on the last comment below).
            comment.tail = "\n  "
        # Reattach the original SQL body so it appears AFTER all comments.
        # 본문의 ``<`` / ``>`` 비교 연산자는 직렬화 시 escape 되지만,
        # ``_localize_cdata_operators`` 후처리가 그 연산자만 국소 CDATA 로
        # 감싼다 (SELECT 본문 전체를 CDATA 로 감싸지 않음).
        if any(isinstance(c.tag, str) for c in stmt):
            # element 자식(동적 <if> / 인라인 <bind> 등) 존재 — 본문이 여러
            # text/tail 조각으로 쪼개지므로 혼합 콘텐츠 재들여쓰기.
            # body_text(첫 자식 앞 SQL) 를 raw 로 넘겨 조각 간 정렬을 일관되게.
            comments[-1].tail = body_text
            _reindent_dynamic(stmt, comments[-1])
        else:
            # 동적/인라인 태그 없는 순수 SQL — 단일 조각 재들여쓰기.
            comments[-1].tail = _reindent_body(body_text)


# ---------------------------------------------------------------------------
# Metadata block formatting
# ---------------------------------------------------------------------------


def _format_metadata_block(rr: RewriteResult) -> str:
    applied = ", ".join(rr.applied_transformers) or "-"
    changed = _format_changes_short(rr.changed_items)
    notes = "; ".join(rr.warnings + rr.notes) or "-"
    stage_a = _tri(rr.stage_a_pass)
    stage_b = _tri(rr.stage_b_pass)
    ora = (rr.parse_error or "").strip()[:400] or "-"
    return (
        "\n"
        f"  ========== MIGRATION: {rr.sql_id} ==========\n"
        f"  Status           : {rr.status}\n"
        f"  Method           : {rr.conversion_method}\n"
        f"  Applied          : {applied}\n"
        f"  Changed          : {changed}\n"
        f"  Dynamic paths    : {rr.dynamic_paths_expanded}\n"
        f"  Stage A (static) : {stage_a}\n"
        f"  Stage B (parse)  : {stage_b}\n"
        f"  ORA              : {ora}\n"
        f"  Notes            : {notes}\n"
        f"  ========================================================\n  "
    )


def _format_as_is_block(rr: "RewriteResult") -> str:  # type: ignore[name-defined]
    """Render the AS-IS comment block.

    Prefers ``rr.as_is_raw`` (the user's verbatim XML body, preserving
    original line breaks / tabs / dynamic tags) when available, falling back
    to ``rr.as_is_sql`` (single-line max-path render) for legacy callers
    that build RewriteResult directly without populating ``as_is_raw``.
    """
    raw = rr.as_is_raw if rr.as_is_raw is not None else rr.as_is_sql
    if not raw:
        return "\n  AS-IS (original)\n  -\n  "
    # Drop only outer blank padding; keep any meaningful internal whitespace.
    body = raw.strip("\r\n")
    body = body.rstrip()
    # Re-indent to sit cleanly inside the comment frame ("  " before each
    # line). Existing tabs / extra spaces inside the user's SQL are kept.
    body = body.replace("\n", "\n  ")
    return "\n  AS-IS (original)\n  " + body + "\n  "


def _format_suggested_block(to_be_sql: str) -> str:
    return (
        "\n  SUGGESTED TO-BE (do not execute — review required)\n  "
        + to_be_sql.strip().replace("\n", "\n  ")
        + "\n  "
    )


# ---------------------------------------------------------------------------
# XML 1.0 comment text sanitization
# ---------------------------------------------------------------------------


# XML 1.0 forbids control chars below 0x20 except TAB / LF / CR. Real legacy
# mapper SQL occasionally carries BEL / VT / FF / NUL etc. as copy-paste
# residue from old IDEs or DB tools — feeding that into ``etree.Comment(text)``
# raises ``ValueError: All strings must be XML compatible …``. We escape such
# bytes to a visible ``\xNN`` token so the comment block still renders and the
# reviewer can spot where the gunk is, instead of the whole migrate-sql run
# crashing on a single bad statement.
_XML_FORBIDDEN_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _sanitize_for_xml(text: str) -> str:
    """Replace XML-forbidden control chars with ``\\xNN`` and neutralise the
    ``--`` sequence which is also illegal inside an XML comment body."""
    if not text:
        return text
    out = _XML_FORBIDDEN_CTRL_RE.sub(
        lambda m: f"\\x{ord(m.group(0)):02x}", text
    )
    # ``-->`` would prematurely close the comment; ``--`` itself is illegal
    # inside <!-- ... -->. Soften with a thin space so users still see the
    # original characters without breaking the XML.
    if "--" in out:
        out = out.replace("--", "- -")
    return out


def _format_changes_short(items: List["ChangeItem"]) -> str:  # type: ignore[name-defined]
    if not items:
        return "-"
    parts = []
    for c in items[:8]:
        parts.append(f"{c.as_is}→{c.to_be}")
    if len(items) > 8:
        parts.append(f"... (+{len(items) - 8} more)")
    return ", ".join(parts)


def _tri(v) -> str:
    if v is True:
        return "PASS"
    if v is False:
        return "FAIL"
    return "-"


# ---------------------------------------------------------------------------
# Per-statement rewrite
# ---------------------------------------------------------------------------


def _rewrite_statement(
    stmt: etree._Element,
    sql_includes: Dict[str, etree._Element],
    mapping: Mapping,
    xml_path: Path,
    namespace: str,
) -> RewriteResult:
    sql_id = stmt.get("id", "") or ""
    sql_type: SqlType = _local(stmt.tag).upper()  # type: ignore[assignment]

    paths = expand_paths(stmt, sql_includes=sql_includes)
    if not paths:
        return RewriteResult(
            xml_file=Path(xml_path),
            namespace=namespace,
            sql_id=sql_id,
            sql_type=sql_type,
            as_is_sql="",
            to_be_sql=None,
            status="PARSE_FAIL",
            notes=["expand_paths returned no paths"],
        )

    max_path = paths[0]
    outcome = rewrite_sql(max_path.rendered_sql, mapping)

    return RewriteResult(
        xml_file=Path(xml_path),
        namespace=namespace,
        sql_id=sql_id,
        sql_type=sql_type,
        as_is_sql=max_path.rendered_sql,
        as_is_raw=_capture_stmt_inner_raw(stmt),
        to_be_sql=outcome.to_be_sql,
        status=outcome.status,
        applied_transformers=outcome.applied_transformers,
        conversion_method="sqlglot-AST",
        changed_items=outcome.changed_items,
        dynamic_paths_expanded=len(paths),
        parse_error=outcome.parse_error,
        warnings=outcome.warnings,
        last_modified=datetime.now(),
    )


def _capture_stmt_inner_raw(stmt: etree._Element) -> str:
    """Serialize the stmt's inner content (text + dynamic-tag children) to a
    string, preserving the user's original whitespace exactly. Comment nodes
    are skipped because :func:`annotate_statements` adds them later — we only
    want the SQL body the user wrote.
    """
    parts: List[str] = []
    if stmt.text:
        parts.append(stmt.text)
    for child in stmt:
        if isinstance(child.tag, str):
            parts.append(etree.tostring(child, encoding="unicode"))
        elif child.tail:
            # Bare comment / PI we skipped — still preserve its tail so we
            # don't drop trailing whitespace between siblings.
            parts.append(child.tail)
    return "".join(parts)


# ---------------------------------------------------------------------------
# Text substitution
# ---------------------------------------------------------------------------


def _compile_substitutions(
    changes: List[ChangeItem],
) -> List[Tuple[re.Pattern, str]]:
    """Deduplicate ChangeItems and compile word-boundary regexes.

    Table rename: ``CUST → CUSTOMER_MASTER``
    Column rename: ``CUST.CUST_NM → CUSTOMER_MASTER.CUSTOMER_NAME``
        → emit ``CUST_NM → CUSTOMER_NAME`` (column-name-only replacement).

    The emitted text for each match is the TO-BE identifier verbatim
    (uppercase if the mapping yaml wrote it uppercase). Matching is
    case-insensitive so mixed-case occurrences in the XML are still caught.
    """

    pairs: Dict[Tuple[str, str], None] = {}  # order-preserving set
    for c in changes:
        if c.kind == "table":
            pairs.setdefault((c.as_is.upper(), c.to_be.upper()), None)
            continue
        if c.kind != "column":
            # type_wrap / value / join_path can't be expressed as 1:1 text
            # substitution — they need a structural rewrite that would erase
            # MyBatis dynamic tags. The metadata comment block (Step 12) tells
            # the user what the full TO-BE looks like instead.
            continue
        # column kind — only 1:1 renames; skip split/merge targets (commas)
        if "," in c.to_be:
            continue
        try:
            _, c_old = c.as_is.rsplit(".", 1)
            _, c_new = c.to_be.rsplit(".", 1)
        except ValueError:
            continue
        if c_old.upper() != c_new.upper():
            pairs.setdefault((c_old.upper(), c_new.upper()), None)

    # Longer identifiers first so ``CUST_NM`` wins over the substring ``CUST``
    # — otherwise ``CUST_NM`` would get partially substituted.
    ordered = sorted(pairs.keys(), key=lambda kv: len(kv[0]), reverse=True)

    compiled: List[Tuple[re.Pattern, str]] = []
    for old, new in ordered:
        pat = re.compile(rf"\b{re.escape(old)}\b", re.IGNORECASE)
        compiled.append((pat, new))
    return compiled


def _disp_width(s: str) -> int:
    """모노스페이스 표시 폭 (한글/CJK 전각 = 2칸)."""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
               for ch in s)


# 줄 끝 인라인 주석 (``코드  /* 한글 */`` 또는 ``코드  -- ...``). 코드부 +
# 공백 gap + 주석으로 분리. 코드부가 비면 (주석만 있는 줄) 매치 안 됨.
_TRAILING_COMMENT_RE = re.compile(
    r"^(?P<code>.*?\S)(?P<gap>[ \t]+)(?P<comment>/\*.*?\*/|--.*?)[ \t]*$"
)


def _realign_trailing_comments(text: str) -> str:
    """식별자 치환으로 폭이 바뀐 뒤, 줄 끝 인라인 주석의 ``/*`` 시작을
    윗줄과 다시 맞춘다.

    AS-IS 에서 컬럼별로 정렬돼 있던 ``/* 한글 */`` 주석은 ``CUST_NM`` →
    ``CUSTOMER_NAME`` 처럼 컬럼명 길이가 바뀌면 그만큼 밀려 어긋난다. 본
    함수는 **연속된** 줄 끝 주석 줄 묶음마다 코드부 최대 표시폭 + 1칸에
    ``/*`` 가 오도록 공백을 재패딩한다 (코드/주석 내용은 보존, 정렬만 교정).
    표시폭 기준이라 한글이 섞여도 어긋나지 않는다.
    """
    if "\n" not in text or ("/*" not in text and "--" not in text):
        return text
    lines = text.split("\n")
    parsed: List[Optional[Tuple[str, str]]] = []
    for ln in lines:
        m = _TRAILING_COMMENT_RE.match(ln)
        parsed.append((m.group("code"), m.group("comment")) if m else None)

    out = list(lines)
    i, n = 0, len(lines)
    while i < n:
        if parsed[i] is None:
            i += 1
            continue
        j = i
        while j < n and parsed[j] is not None:
            j += 1
        if j - i >= 2:  # 2줄 이상 연속일 때만 정렬 의미 있음
            target = max(_disp_width(parsed[k][0]) for k in range(i, j)) + 1
            for k in range(i, j):
                code, comment = parsed[k]
                pad = max(target - _disp_width(code), 1)
                out[k] = code + " " * pad + comment
        i = j
    return "\n".join(out)


# 변환 본문을 ``<statement>`` 한 단계 안으로 들여쓸 때의 기준 들여쓰기.
# 메타/AS-IS 주석 프레임이 2칸이므로 본문은 그보다 한 단계 깊은 4칸.
_BODY_BASE_INDENT = "    "


def _reindent_body(body_text: str) -> str:
    """SQL 본문 블록을 ``<select>`` 등 statement 태그 한 단계 안으로
    재들여쓰기. **내부 정렬(리딩 콤마 / 줄 끝 주석)은 그대로 유지**한다.

    기존엔 첫 줄만 ``lstrip`` 후 2칸을 붙여, 첫 줄(``SELECT``)이 이어지는
    ``, ...`` / ``FROM`` 줄과 어긋나고 본문이 태그 밑으로 안 들어갔다.
    여기선 비어있지 않은 줄들의 공통 들여쓰기(common indent)만 벗기고
    ``_BODY_BASE_INDENT`` 를 일괄로 다시 붙여, 상대 정렬을 보존하면서
    블록 전체를 한 단계 안으로 옮긴다.
    """
    if not body_text.strip():
        return "\n  "
    lines = body_text.strip("\r\n").split("\n")
    # 바깥쪽 공백-only 줄 제거 (원본 ``\n  `` 꼬리 등이 빈 줄로 남지 않게).
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    nonblank = [ln for ln in lines if ln.strip()]
    common = min(
        (len(ln) - len(ln.lstrip(" \t")) for ln in nonblank), default=0
    )
    rebased = "\n".join(
        (_BODY_BASE_INDENT + ln[common:]).rstrip() if ln.strip() else ""
        for ln in lines
    )
    # ``=`` 정렬 먼저 (lhs 폭 변동 흡수) → 줄 끝 주석 정렬.
    rebased = _realign_trailing_comments(_realign_equals(rebased))
    return "\n" + rebased + "\n  "


# MyBatis 블록형 동적 SQL 태그 — SQL 을 감싸며, SQL 본문(스페이스)보다
# 깊이만큼 탭으로 들여쓰고, 그 안의 SQL 은 바깥 SQL 과 같은 기준으로 정렬.
_DYNAMIC_TAGS = {"if", "choose", "when", "otherwise", "foreach", "where",
                 "set", "trim"}
# 인라인/self-closing 디렉티브 — 본문(SQL)이 없으므로 펼치지 않고
# (``<bind .../>`` 유지) SQL 본문 기준 들여쓰기에 둔다.
_INLINE_TAGS = {"bind", "include"}


def _is_dynamic(elem) -> bool:
    return isinstance(elem.tag, str) and _local(elem.tag) in _DYNAMIC_TAGS


def _is_inline_tag(elem) -> bool:
    return isinstance(elem.tag, str) and _local(elem.tag) in _INLINE_TAGS


def _global_common_indent(frags: List[str]) -> int:
    """여러 SQL 조각에 걸친 공통 최소 들여쓰기 (전체를 한 덩어리로 보고
    상대 정렬을 보존하기 위함 — 조각별로 따로 dedent 하면 ``<if>`` 안의
    SQL 이 바깥 SQL 과 어긋난다)."""
    indents = [
        len(ln) - len(ln.lstrip(" \t"))
        for f in frags for ln in f.split("\n") if ln.strip()
    ]
    return min(indents) if indents else 0


# 줄 끝/중간 ``lhs = rhs`` 의 ``=`` 정렬용. ``=`` 앞뒤가 공백이어야 하고
# (``<=`` / ``>=`` / ``<>`` / ``!=`` / ``==`` 는 ``=`` 바로 앞이 공백이
# 아니라 매칭 제외), lhs 는 줄 첫 비공백~``=`` 앞 마지막 비공백.
_EQ_ALIGN_RE = re.compile(r"^(?P<lead>\s*\S.*?\S) +=(?P<rest> .*)$")


def _realign_equals(text: str) -> str:
    """식별자 치환으로 폭이 바뀐 뒤, 연속된 ``컬럼 = 값`` 줄들의 ``=`` 를
    같은 표시폭 컬럼으로 다시 맞춘다 (WHERE / SET / ON 절 정렬). ``<=`` /
    ``>=`` / ``<>`` / ``!=`` 는 매칭 제외."""
    if "\n" not in text or " = " not in text:
        return text
    lines = text.split("\n")
    parsed = [_EQ_ALIGN_RE.match(ln) for ln in lines]
    out = list(lines)
    i, n = 0, len(lines)
    while i < n:
        if parsed[i] is None:
            i += 1
            continue
        j = i
        while j < n and parsed[j] is not None:
            j += 1
        if j - i >= 2:  # 2줄 이상 연속일 때만 정렬 의미
            target = max(_disp_width(parsed[k].group("lead"))
                         for k in range(i, j)) + 1
            for k in range(i, j):
                lead = parsed[k].group("lead")
                pad = max(target - _disp_width(lead), 1)
                out[k] = lead + " " * pad + "=" + parsed[k].group("rest")
        i = j
    return "\n".join(out)


def _emit_sql_fragment(text: str, common: int) -> str:
    """SQL 조각을 전역 ``common`` 만큼 dedent 후 ``_BODY_BASE_INDENT`` 로
    재부여 (상대 정렬 보존) + ``=`` / 줄 끝 주석 재정렬. 선행 ``\\n`` 포함,
    후행 공백/개행 없음. SQL 이 없으면 ``""``."""
    if not text or not text.strip():
        return ""
    lines = text.strip("\r\n").split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    rebased = "\n".join(
        (_BODY_BASE_INDENT + ln[common:]).rstrip() if ln.strip() else ""
        for ln in lines
    )
    # ``=`` 먼저 (lhs 폭 변동 흡수) → 그 뒤 줄 끝 주석 정렬.
    return "\n" + _realign_trailing_comments(_realign_equals(rebased))


def _dyn_indent(depth: int) -> str:
    """동적 태그(``<if>`` 등) 들여쓰기. SQL 본문(``_BODY_BASE_INDENT``,
    4칸)보다 중첩 깊이만큼 **탭** 더 깊게 — 사용자 표준: "SELECT 본문이
    한 탭 들어가 있으니 거기서 한 탭 더". 중첩 ``<if>`` 안 ``<if>`` 는 탭 2개."""
    return _BODY_BASE_INDENT + "\t" * depth


def _child_indent(child, parent_depth: int) -> str:
    """``parent_depth`` 안에 있는 ``child`` 태그를 배치할 들여쓰기.

    블록 동적태그(``<if>`` 등)는 ``_dyn_indent(parent_depth+1)`` (탭).
    인라인(``<bind>``/``<include>``)은 statement 최상위(``parent_depth==0``)
    에선 SQL 본문 기준(4칸), 동적 태그 안(``parent_depth>=1``)에선 그
    동적 태그보다 한 탭 더 (= ``_dyn_indent(parent_depth+1)``). 예: ``<if>``
    안의 ``<bind>`` 는 ``<if>`` 보다 한 탭 더."""
    if _is_dynamic(child):
        return _dyn_indent(parent_depth + 1)
    return (_BODY_BASE_INDENT if parent_depth == 0
            else _dyn_indent(parent_depth + 1))


def _reindent_dynamic(stmt, body_owner) -> None:
    """동적/인라인 태그를 포함한 statement 본문 재들여쓰기.

    모든 SQL 텍스트 조각(``body_owner.tail`` = 첫 자식 앞 SQL, 각 element
    자식의 ``.text`` / ``.tail``)을 **전역 common** 으로 일괄 dedent →
    ``_BODY_BASE_INDENT`` rebase (상대 정렬 보존 → ``<if>`` 안 SQL 이 바깥과
    정렬). 블록 동적태그는 탭, 인라인 ``<bind>`` 는 self-closing 유지.
    """
    frags = [body_owner.tail or ""]

    def _gather(el):
        for c in el:
            if not isinstance(c.tag, str):
                continue  # comment
            if _is_dynamic(c):
                frags.append(c.text or "")
                _gather(c)
            frags.append(c.tail or "")

    _gather(stmt)
    common = _global_common_indent(frags)
    _emit_mixed(stmt, common, lambda: body_owner.tail,
                lambda v: setattr(body_owner, "tail", v),
                depth=0, close_indent="  ")


def _emit_mixed(elem, common: int, get_leading, set_leading,
                depth: int, close_indent: str) -> None:
    """``elem`` 의 혼합 콘텐츠(선행 SQL + element 자식들)를 재배치.

    ``get_leading`` / ``set_leading`` 으로 선행 SQL 조각(statement 는
    last-comment.tail, 블록태그는 자신의 ``.text``)을 읽고 쓴다.
    """
    children = [c for c in elem if isinstance(c.tag, str)]
    lead = _emit_sql_fragment(get_leading(), common)
    if not children:
        set_leading(lead + "\n" + close_indent)
        return
    set_leading(lead + "\n" + _child_indent(children[0], depth))
    for i, c in enumerate(children):
        if _is_dynamic(c):
            _emit_mixed(
                c, common,
                (lambda c=c: c.text),
                (lambda v, c=c: setattr(c, "text", v)),
                depth=depth + 1,
                close_indent=_dyn_indent(depth + 1),
            )
        # 인라인(<bind>/<include>): .text 안 건드림 → self-closing 유지.
        nxt = (close_indent if i == len(children) - 1
               else _child_indent(children[i + 1], depth))
        c.tail = _emit_sql_fragment(c.tail or "", common) + "\n" + nxt


def _apply_subs_to_tree(
    root: etree._Element,
    subs: List[Tuple[re.Pattern, str]],
) -> None:
    """Walk the tree and apply word-boundary substitutions to text/tail.

    Critically, we **only reassign elem.text / elem.tail when the value
    actually changed**. Reassigning a string to ``elem.text`` clobbers
    lxml's internal CDATA marker even when the value is identical — so
    untouched nodes keep their original CDATA (``strip_cdata=False``).

    When the text *does* change we assign plain text; ``<`` / ``>`` 비교
    연산자는 lxml 이 ``&lt;`` / ``&gt;`` 로 escape 하지만, 직렬화 후처리
    (:func:`_localize_cdata_operators`) 가 그 연산자만 ``<![CDATA[..]]>``
    로 국소 래핑한다 (SELECT 본문 전체를 감싸지 않음).

    치환으로 식별자 폭이 바뀌면 줄 끝 정렬 주석이 어긋나므로,
    ``_realign_trailing_comments`` 로 ``/*`` 시작을 다시 맞춘다.
    """
    for elem in root.iter():
        if elem.text:
            new_text = _apply_subs_outside_literals(elem.text, subs)
            if new_text != elem.text:
                elem.text = _realign_trailing_comments(new_text)
        if elem.tail:
            new_tail = _apply_subs_outside_literals(elem.tail, subs)
            if new_tail != elem.tail:
                elem.tail = _realign_trailing_comments(new_tail)


def _apply_subs(text: str, subs: List[Tuple[re.Pattern, str]]) -> str:
    for pattern, replacement in subs:
        text = pattern.sub(replacement, text)
    return text


def _apply_subs_outside_literals(
    text: str,
    subs: List[Tuple[re.Pattern, str]],
) -> str:
    """Apply word-boundary substitutions only to "code" regions of SQL text.

    Walks the text once and skips over regions where identifier-shaped
    substrings must NOT be rewritten:

    * single-quoted string literals — ``'CUST_NM'`` (Oracle ``''`` escape)
    * SQL line comments — ``-- ...`` to end of line
    * SQL block comments — ``/* ... */``
    * MyBatis OGNL placeholders — ``#{name,jdbcType=VARCHAR}`` / ``${TBL}``

    Anything outside those regions goes through :func:`_apply_subs` (the
    plain word-boundary regex pass). Unterminated literals/comments fall
    through as-is so we never corrupt malformed SQL fragments.
    """
    if not text or not subs:
        return text

    out: List[str] = []
    code_buf: List[str] = []
    n = len(text)
    i = 0

    def _flush_code() -> None:
        if code_buf:
            out.append(_apply_subs("".join(code_buf), subs))
            code_buf.clear()

    while i < n:
        c = text[i]
        c2 = text[i:i + 2]

        # MyBatis OGNL: #{...} / ${...}
        if c2 in ("#{", "${"):
            _flush_code()
            end = text.find("}", i + 2)
            if end < 0:
                out.append(text[i:])
                return "".join(out)
            out.append(text[i:end + 1])
            i = end + 1
            continue

        # Block comment /* ... */
        if c2 == "/*":
            _flush_code()
            end = text.find("*/", i + 2)
            if end < 0:
                out.append(text[i:])
                return "".join(out)
            out.append(text[i:end + 2])
            i = end + 2
            continue

        # Line comment -- ... \n  (newline itself is code, not part of comment)
        if c2 == "--":
            _flush_code()
            end = text.find("\n", i + 2)
            if end < 0:
                out.append(text[i:])
                return "".join(out)
            out.append(text[i:end])
            i = end
            continue

        # Single-quoted string literal with Oracle '' escape
        if c == "'":
            _flush_code()
            j = i + 1
            while j < n:
                if text[j] == "'":
                    if j + 1 < n and text[j + 1] == "'":
                        j += 2  # escaped quote, stay in literal
                        continue
                    j += 1
                    break
                j += 1
            else:
                # Unterminated literal — emit verbatim and bail.
                out.append(text[i:])
                return "".join(out)
            out.append(text[i:j])
            i = j
            continue

        code_buf.append(c)
        i += 1

    _flush_code()
    return "".join(out)


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


def _local(tag: str) -> str:
    if isinstance(tag, str) and "}" in tag:
        return tag.split("}", 1)[-1]
    return tag
