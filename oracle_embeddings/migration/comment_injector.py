"""Inject Korean column comments into rewritten SQL (docs/migration/spec.md §9).

Given a lookup of ``TO_BE_TABLE.COLUMN → 한글 설명`` (typically derived from
the TO-BE schema's ``COLUMN_COMMENT`` or from ``terms_dictionary.md``), walk
each parsed statement and attach a ``/* 한글 */`` trailing comment to every
matching column reference in the configured scope.

The heavy lifting uses ``sqlglot``'s ``add_comments()`` method rather than
string manipulation — this keeps the comment attached to the token and
correctly survives re-emission with or without ``pretty=True``.

Scope flags mirror the mapping-yaml ``options.comment_scope`` list:
    - ``select``  → SELECT projection
    - ``update``  → UPDATE SET LHS
    - ``insert``  → INSERT column list
    - ``where`` / ``join`` → predicate columns (noisy, off by default)
"""
from __future__ import annotations

import logging
from typing import Dict, Iterable, List, Optional, Set

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from .sql_rewriter import mask_mybatis_placeholders, unmask_mybatis_placeholders

logger = logging.getLogger(__name__)


# Valid scope tokens — keep in sync with mapping_model.CommentScope
_ALL_SCOPES: Set[str] = {"select", "update", "insert", "where", "join"}


def inject_comments(
    sql: str,
    ko_lookup: Dict[str, str],
    *,
    scopes: Iterable[str] = ("select", "update", "insert"),
    dialect: str = "oracle",
) -> str:
    """Return ``sql`` with Korean comments injected for each matching column.

    ``ko_lookup`` keys may be plain ``COLUMN`` names (case-insensitive) or
    qualified ``TABLE.COLUMN``. Qualified entries win over bare ones when
    the column is unambiguous.
    """

    if not ko_lookup:
        return sql

    safe_sql, tokens = mask_mybatis_placeholders(sql)
    try:
        tree = sqlglot.parse_one(safe_sql, dialect=dialect)
    except ParseError:
        return sql

    lookup = _normalise_lookup(ko_lookup)
    active_scopes = {s.lower() for s in scopes} & _ALL_SCOPES
    if not active_scopes:
        return sql

    # Regular SELECT / WHERE / JOIN projections — walk exp.Column nodes and
    # test each against scope gates.
    for col in tree.find_all(exp.Column):
        scope = _column_scope(col)
        if scope is None or scope not in active_scopes:
            continue
        ko = _lookup_ko(col, lookup)
        if ko is None:
            continue
        _safe_add_comment(col, ko)

    # INSERT column list: (col1, col2) — identifiers rather than exp.Column
    if "insert" in active_scopes:
        for schema in tree.find_all(exp.Schema):
            host = schema.this
            if not isinstance(host, exp.Table):
                continue
            tbl_upper = (host.name or "").upper()
            for i, ident in enumerate(list(schema.expressions)):
                if not isinstance(ident, exp.Identifier):
                    continue
                ko = lookup.get(f"{tbl_upper}.{ident.name.upper()}") or lookup.get(
                    ident.name.upper()
                )
                if ko:
                    # Identifiers don't carry comments natively; swap them
                    # with a Column+comment which still re-emits as the same
                    # identifier followed by the comment.
                    col = exp.column(ident.name)
                    _safe_add_comment(col, ko)
                    schema.expressions[i] = col

    out_sql = tree.sql(dialect=dialect)
    return unmask_mybatis_placeholders(out_sql, tokens)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _normalise_lookup(ko_lookup: Dict[str, str]) -> Dict[str, str]:
    """Upper-case keys and keep both ``TABLE.COL`` and ``COL`` forms."""
    out: Dict[str, str] = {}
    for k, v in ko_lookup.items():
        if not v:
            continue
        ku = k.upper()
        out[ku] = v
        if "." in ku:
            col_only = ku.rsplit(".", 1)[1]
            # Don't overwrite a prior explicit bare-col mapping
            out.setdefault(col_only, v)
    return out


def _lookup_ko(col: exp.Column, lookup: Dict[str, str]) -> Optional[str]:
    name = (col.name or "").upper()
    if not name:
        return None
    qualifier = (col.table or "").upper()
    if qualifier:
        qualified = f"{qualifier}.{name}"
        if qualified in lookup:
            return lookup[qualified]
    return lookup.get(name)


def _column_scope(col: exp.Column) -> Optional[str]:
    """Classify the column's syntactic scope — see ``_ALL_SCOPES``."""
    if col.find_ancestor(exp.Where):
        return "where"
    join = col.find_ancestor(exp.Join)
    if join is not None and col.find_ancestor(exp.Condition) is not None:
        return "join"
    update = col.find_ancestor(exp.Update)
    if update is not None:
        eq = col.find_ancestor(exp.EQ)
        if eq is not None:
            # LHS of EQ under Update.expressions → SET target = "update" scope
            if eq in (update.args.get("expressions", []) or []):
                if eq.left is col or _contains(eq.left, col):
                    return "update"
        # Otherwise treat as write-ish read
        return "update"
    insert = col.find_ancestor(exp.Insert)
    if insert is not None:
        return "insert"
    sel = col.find_ancestor(exp.Select)
    if sel is not None:
        for proj in sel.expressions or []:
            if col is proj or _contains(proj, col):
                return "select"
    return None


def _contains(ancestor: exp.Expression, target: exp.Expression) -> bool:
    for node in ancestor.walk():
        if node is target:
            return True
    return False


def _safe_add_comment(node: exp.Expression, ko: str) -> None:
    """Strip accidental ``*/`` and call ``add_comments`` in-place."""
    cleaned = ko.replace("*/", "*∕").strip()
    if not cleaned:
        return
    node.add_comments([cleaned])
