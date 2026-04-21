"""Merge-column transformer — MVP flags references for LLM fallback.

Automatic handling of merge columns (e.g. YYYY + MM + DD → EVENT_DATE) is
non-trivial in non-INSERT contexts:

* SELECT: ``SELECT YYYY FROM EVT`` → needs ``EXTRACT(YEAR FROM EVENT_DATE)``,
  but the template only specifies the combine direction.
* WHERE: even harder; collapsing three predicates into one requires
  reasoning across the whole clause.

The transformer therefore walks columns, detects references to any AS-IS
member of a merge mapping, and sets ``needs_llm=True`` with a clear warning.
Future iterations can add the concrete rewrites once we have a reliable
template vocabulary for the reverse direction.
"""
from __future__ import annotations

from typing import List

from sqlglot import exp

from ..mapping_model import ChangeItem
from .base import RewriteContext, Transformer, TransformerResult, stmt_tables


class ColumnMergeTransformer(Transformer):
    name = "ColumnMerge"

    def apply(
        self, tree: exp.Expression, context: RewriteContext
    ) -> TransformerResult:
        mapping = context.mapping
        alias_map = context.alias_map
        tables_in_stmt = stmt_tables(alias_map)

        warnings: List[str] = []
        flagged = set()
        needs_llm = False

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
            if cm is None or cm.kind != "merge":
                continue
            key = (source, col_name.upper())
            if key in flagged:
                continue
            flagged.add(key)
            warnings.append(
                f"merge column {source}.{col_name} referenced — manual "
                "decomposition required (LLM fallback recommended)"
            )
            needs_llm = True

        changes: List[ChangeItem] = []  # nothing automatically applied here
        return TransformerResult(
            tree=tree, changes=changes, needs_llm=needs_llm, warnings=warnings
        )
