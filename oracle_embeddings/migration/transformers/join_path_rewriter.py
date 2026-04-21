"""JOIN path rewriter — scaffold only (marked experimental in the spec).

Detects statements that span multiple renamed/split/merged tables and may
need their JOIN topology rewritten (e.g. ``ORD_HEADER JOIN ORD_ITEM`` when
``ORDER_HIST`` was split). MVP just emits a warning + ``needs_llm`` so the
LLM fallback handles it. Real JOIN topology rewrites land in a follow-up.
"""
from __future__ import annotations

from typing import List, Set

from sqlglot import exp

from ..mapping_model import ChangeItem
from .base import RewriteContext, Transformer, TransformerResult


class JoinPathRewriter(Transformer):
    name = "JoinPathRewriter"

    def apply(
        self, tree: exp.Expression, context: RewriteContext
    ) -> TransformerResult:
        mapping = context.mapping
        warnings: List[str] = []
        needs_llm = False

        joins = list(tree.find_all(exp.Join))
        if not joins:
            return TransformerResult(tree=tree, changes=[], warnings=[])

        split_or_merge_tables: Set[str] = set()
        for tm in mapping.tables:
            if tm.type in ("split", "merge"):
                for name in tm.as_is_tables():
                    split_or_merge_tables.add(name.upper())

        stmt_tables_set = {
            (tbl.name or "").upper()
            for tbl in tree.find_all(exp.Table)
        }
        impacted = stmt_tables_set & split_or_merge_tables
        if impacted:
            warnings.append(
                f"JOIN with split/merge tables {sorted(impacted)} detected — "
                "topology rewrite deferred to LLM fallback"
            )
            needs_llm = True

        changes: List[ChangeItem] = []
        return TransformerResult(
            tree=tree, changes=changes, needs_llm=needs_llm, warnings=warnings
        )
