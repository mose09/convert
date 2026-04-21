"""Split-column transformer.

SELECT projection: substitutes the AS-IS column with the ``reverse``
expression so the query still exposes a value shaped like the old column
(e.g. ``FIRST_NAME || ' ' || LAST_NAME`` for a combined FULL_NAME).

WHERE / UPDATE / INSERT contexts: left untouched but flagged ``needs_llm``
because they require reasoning we can't automate from the template alone
(e.g. ``WHERE FULL_NAME = ?`` should probably become ``WHERE (FIRST_NAME ||
' ' || LAST_NAME) = ?`` or decompose into separate predicates — that's a
judgement call).
"""
from __future__ import annotations

from typing import Dict, List, Tuple

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from ..mapping_model import ChangeItem, SplitTarget
from .base import RewriteContext, Transformer, TransformerResult, stmt_tables


class ColumnSplitTransformer(Transformer):
    name = "ColumnSplit"

    def apply(
        self, tree: exp.Expression, context: RewriteContext
    ) -> TransformerResult:
        mapping = context.mapping
        alias_map = context.alias_map
        tables_in_stmt = stmt_tables(alias_map)

        accum: Dict[Tuple[str, str], int] = {}
        warnings: List[str] = []
        needs_llm = False

        for col in list(tree.find_all(exp.Column)):
            col_name = col.name
            if not col_name:
                continue

            qualifier = col.table
            source = None
            if qualifier:
                source = alias_map.get(qualifier.upper())
            else:
                candidates = [
                    t for t in tables_in_stmt
                    if mapping.find_column(t, col_name) is not None
                ]
                if len(candidates) == 1:
                    source = candidates[0]
            if source is None:
                continue

            cm = mapping.find_column(source, col_name)
            if cm is None or cm.kind != "split":
                continue

            if not _is_in_select_projection(col):
                warnings.append(
                    f"split column {source}.{col_name} appears outside "
                    "SELECT projection — manual review needed"
                )
                needs_llm = True
                continue

            if not cm.reverse:
                warnings.append(
                    f"split column {source}.{col_name} has no 'reverse' "
                    "expression; cannot substitute in SELECT"
                )
                needs_llm = True
                continue

            # Substitute each {to_be_col} placeholder with the concrete
            # TO-BE column reference (qualified with existing alias).
            expr_sql = _expand_reverse(cm.reverse, cm.to_be, qualifier)
            try:
                expr_tree = sqlglot.parse_one(expr_sql, dialect="oracle")
            except ParseError as exc:
                warnings.append(
                    f"split 'reverse' failed to parse ({expr_sql!r}): {exc}"
                )
                needs_llm = True
                continue

            col.replace(expr_tree)
            targets_desc = ",".join(
                f"{t.table}.{t.column}"
                for t in cm.to_be  # type: ignore[union-attr]
                if isinstance(t, SplitTarget)
            )
            key = (f"{source}.{col_name.upper()}", targets_desc.upper())
            accum[key] = accum.get(key, 0) + 1

        changes = [
            ChangeItem(
                kind="column", as_is=as_is, to_be=to_be,
                count=count, transformer=self.name,
            )
            for (as_is, to_be), count in sorted(accum.items())
        ]
        return TransformerResult(
            tree=tree, changes=changes, needs_llm=needs_llm, warnings=warnings
        )


def _is_in_select_projection(col: exp.Column) -> bool:
    sel = col.find_ancestor(exp.Select)
    if sel is None:
        return False
    node = col
    while node is not None and node is not sel:
        if node in (sel.expressions or []):
            return True
        node = node.parent
    return False


def _expand_reverse(reverse: str, targets, qualifier: str) -> str:
    """Replace ``{column}`` placeholders with qualified TO-BE column refs."""
    out = reverse
    for t in targets or []:
        if isinstance(t, SplitTarget):
            ref = f"{qualifier}.{t.column}" if qualifier else t.column
            out = out.replace("{" + t.column + "}", ref)
            # also support case-insensitive upper keys
            out = out.replace("{" + t.column.upper() + "}", ref)
    return out
