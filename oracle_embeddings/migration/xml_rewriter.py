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
        elif c.kind == "column":
            # column names after the last dot
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
            elem.text = _apply_subs(elem.text, subs)
        if elem.tail:
            elem.tail = _apply_subs(elem.tail, subs)


def _apply_subs(text: str, subs: List[Tuple[re.Pattern, str]]) -> str:
    for pattern, replacement in subs:
        text = pattern.sub(replacement, text)
    return text


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


def _local(tag: str) -> str:
    if isinstance(tag, str) and "}" in tag:
        return tag.split("}", 1)[-1]
    return tag
