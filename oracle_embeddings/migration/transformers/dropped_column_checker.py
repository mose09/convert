"""Flag statements that reference AS-IS columns marked for drop.

Dropped columns have ``cm.to_be is None``. References to them indicate the
statement relied on data that won't exist in TO-BE. We emit warnings but do
**not** modify the tree — removal or replacement is too destructive to do
automatically. The resulting status bubbles up as ``AUTO_WARN`` so the user
sees it in the Unresolved Queue sheet of the migration report.
"""
from __future__ import annotations

from typing import List, Set, Tuple

from sqlglot import exp

from ..mapping_model import ChangeItem
from .base import RewriteContext, Transformer, TransformerResult, stmt_tables


class DroppedColumnChecker(Transformer):
    name = "DroppedColumnChecker"

    def apply(
        self, tree: exp.Expression, context: RewriteContext
    ) -> TransformerResult:
        mapping = context.mapping
        alias_map = context.alias_map
        tables_in_stmt = stmt_tables(alias_map)

        warnings: List[str] = []
        seen: Set[Tuple[str, str]] = set()

        for col in tree.find_all(exp.Column):
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
            if cm is None:
                continue
            if cm.to_be is not None:
                continue
            key = (source, col_name.upper())
            if key in seen:
                continue
            seen.add(key)
            warnings.append(
                f"dropped column referenced: {source}.{col_name} "
                f"(action={cm.action})"
            )

        return TransformerResult(
            tree=tree, changes=[], needs_llm=False, warnings=warnings
        )
