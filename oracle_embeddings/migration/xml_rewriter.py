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
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from lxml import etree

from ..mybatis_parser import _read_file_safe
from .dynamic_sql_expander import build_sql_includes, expand_paths
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
    namespace = root.get("namespace", "") or ""
    sql_includes = build_sql_includes(root)

    statements = [e for e in root.iter() if _local(e.tag) in _STATEMENT_TAGS]

    # Rewrite each statement AND each <sql> fragment so references stay
    # consistent when different statements include the same <sql> body.
    results: List[RewriteResult] = []
    all_changes: List[ChangeItem] = []

    for stmt in statements:
        rr = _rewrite_statement(stmt, sql_includes, mapping, xml_path, namespace)
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
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tree.write(
        str(out_path),
        encoding="utf-8",
        xml_declaration=True,
        pretty_print=False,
        standalone=None,
    )


def annotate_statements(
    tree: etree._ElementTree,
    results: List[RewriteResult],
    *,
    preserve_as_is: bool = True,
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
            blocks.append(_format_as_is_block(rr.as_is_sql))
        if rr.status in ("UNRESOLVED", "NEEDS_LLM") and rr.to_be_sql and rr.to_be_sql != rr.as_is_sql:
            blocks.append(_format_suggested_block(rr.to_be_sql))

        if not blocks:
            continue

        body_text = stmt.text or ""
        stmt.text = "\n  "  # leading indent before the first comment

        comments = [etree.Comment(text) for text in blocks]
        for i, comment in enumerate(comments):
            stmt.insert(i, comment)
            # Spacer between comments (overwritten on the last comment below).
            comment.tail = "\n  "
        # Reattach the original SQL body so it appears AFTER all comments.
        comments[-1].tail = "\n  " + body_text.lstrip(" \t\r\n") if body_text.strip() else "\n  "


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


def _format_as_is_block(as_is_sql: str) -> str:
    return "\n  AS-IS (original)\n  " + as_is_sql.strip().replace("\n", "\n  ") + "\n  "


def _format_suggested_block(to_be_sql: str) -> str:
    return (
        "\n  SUGGESTED TO-BE (do not execute — review required)\n  "
        + to_be_sql.strip().replace("\n", "\n  ")
        + "\n  "
    )


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


def _apply_subs_to_tree(
    root: etree._Element,
    subs: List[Tuple[re.Pattern, str]],
) -> None:
    for elem in root.iter():
        # Skip attributes entirely (namespace / refid / id etc. shouldn't be
        # rewritten; we only touch SQL text). If a transform needs to rename
        # ``namespace`` attributes, that's a separate higher-level concern.
        if elem.text:
            elem.text = _apply_subs_outside_literals(elem.text, subs)
        if elem.tail:
            elem.tail = _apply_subs_outside_literals(elem.tail, subs)


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
