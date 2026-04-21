"""Wrap type-converted columns with ``transform.read/write/where`` templates.

Context rules:
    - In WHERE / JOIN ON  → ``transform.where`` (falls back to ``read``)
    - In UPDATE SET / INSERT VALUES → ``transform.write`` (falls back to ``read``)
    - Everywhere else (SELECT projection, SET RHS, etc.) → ``transform.read``

When the template is missing for the detected context we silently fall through
— the column is still renamed, just not wrapped, so downstream Stage A will
surface any resulting type mismatches.
"""
from __future__ import annotations

from typing import Dict, List, Tuple

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from ..mapping_model import ChangeItem, ColumnRef
from .base import RewriteContext, Transformer, TransformerResult, stmt_tables
from .column_rename import _set_column_name


class TypeConversionTransformer(Transformer):
    name = "TypeConversion"

    def apply(
        self, tree: exp.Expression, context: RewriteContext
    ) -> TransformerResult:
        mapping = context.mapping
        alias_map = context.alias_map
        tables_in_stmt = stmt_tables(alias_map)

        accum: Dict[Tuple[str, str], int] = {}
        warnings: List[str] = []

        for col in list(tree.find_all(exp.Column)):
            col_name = col.name
            if not col_name:
                continue
            source = self._resolve_source(
                col, col_name, alias_map, tables_in_stmt, mapping
            )
            if source is None:
                continue
            cm = mapping.find_column(source, col_name)
            if cm is None or cm.kind != "type_convert":
                continue
            to_be = cm.to_be
            if not isinstance(to_be, ColumnRef):
                continue

            # Rename first so the template's ``{src}`` substitution references
            # the TO-BE column identifier.
            _set_column_name(col, to_be.column)

            ctx = _classify_context(col)
            template = _pick_template(cm.transform, ctx)
            if template:
                replaced = _wrap_with_template(col, to_be, template, warnings)
                if not replaced:
                    continue
            key = (
                f"{source}.{col_name.upper()}",
                f"{to_be.table.upper()}.{to_be.column.upper()}",
            )
            accum[key] = accum.get(key, 0) + 1

        changes = [
            ChangeItem(
                kind="type_wrap",
                as_is=as_is,
                to_be=to_be,
                count=count,
                transformer=self.name,
            )
            for (as_is, to_be), count in sorted(accum.items())
        ]
        return TransformerResult(
            tree=tree, changes=changes, needs_llm=False, warnings=warnings
        )

    def _resolve_source(
        self, col, col_name, alias_map, tables_in_stmt, mapping
    ):
        qualifier = col.table
        if qualifier:
            return alias_map.get(qualifier.upper())
        candidates = [
            t for t in tables_in_stmt
            if mapping.find_column(t, col_name) is not None
        ]
        if len(candidates) == 1:
            return candidates[0]
        return None


def _classify_context(col: exp.Column) -> str:
    """Return ``'where'`` / ``'write'`` / ``'read'``.

    ``'write'`` means the column is the left-hand side of an UPDATE SET
    assignment or appears in an INSERT column list — in both cases the
    ``transform.write`` template fits better than ``transform.read``.
    """
    # WHERE / JOIN ON predicate — both are semantically predicates
    if col.find_ancestor(exp.Where):
        return "where"
    # JOIN's ON clause lives under exp.Join → exp.Condition
    join = col.find_ancestor(exp.Join)
    if join is not None and col.find_ancestor(exp.Condition) is not None:
        return "where"

    # UPDATE SET col = ... / INSERT INTO t (col, ...) — both are "write"
    update = col.find_ancestor(exp.Update)
    if update is not None:
        # Update.expressions is the SET list; each is an EQ with col on left.
        # We also cover WHERE via the earlier check.
        eq = col.find_ancestor(exp.EQ)
        if eq is not None and eq in update.args.get("expressions", []):
            return "write"
        # Inside SET RHS → read
    insert = col.find_ancestor(exp.Insert)
    if insert is not None:
        return "write"
    return "read"


def _pick_template(transform, ctx: str):
    if transform is None:
        return None
    if ctx == "where":
        return transform.where or transform.read
    if ctx == "write":
        return transform.write or transform.read
    return transform.read


def _wrap_with_template(
    col: exp.Column,
    to_be_ref,
    template: str,
    warnings: List[str],
) -> bool:
    """Replace ``col`` with the parsed ``template`` after substituting
    ``{src}`` with the (already renamed) column expression text."""

    q = col.table or to_be_ref.table
    src_text = f"{q}.{to_be_ref.column}" if q else to_be_ref.column
    expr_sql = template.replace("{src}", src_text)

    try:
        expr_tree = sqlglot.parse_one(expr_sql, dialect="oracle")
    except ParseError as exc:
        warnings.append(
            f"transform template failed to parse ({expr_sql!r}): {exc}"
        )
        return False
    if isinstance(expr_tree, exp.Select):
        warnings.append(
            f"transform template produced a SELECT, expected an expression "
            f"({expr_sql!r}); skipped"
        )
        return False

    col.replace(expr_tree)
    return True
