"""Rewrite ``tables[]`` entries of kind ``rename`` (docs/migration/spec.md §6).

Split / merge / drop are out of scope here — flagged as ``needs_llm`` (or as
warnings for drop) so the downstream pipeline can defer to the LLM fallback.
"""
from __future__ import annotations

from typing import Dict, Tuple

from sqlglot import exp

from ..mapping_model import ChangeItem
from .base import RewriteContext, Transformer, TransformerResult


class TableRenameTransformer(Transformer):
    name = "TableRename"

    def apply(
        self, tree: exp.Expression, context: RewriteContext
    ) -> TransformerResult:
        mapping = context.mapping

        accum: Dict[Tuple[str, str], int] = {}
        warnings: list[str] = []
        needs_llm = False
        rename_map: Dict[str, str] = {}  # upper(old) -> upper(new)

        # Pass 1: rewrite Table nodes for simple renames, record defers for the rest.
        for tbl in list(tree.find_all(exp.Table)):
            old_name = tbl.name
            if not old_name:
                continue
            tm = mapping.find_table(old_name)
            if tm is None:
                continue

            if tm.type == "rename":
                if not isinstance(tm.to_be, str):
                    # Loader should have caught this; defend anyway.
                    continue
                new_name = tm.to_be
                _set_table_name(tbl, new_name)
                rename_map[old_name.upper()] = new_name.upper()
                key = (old_name.upper(), new_name.upper())
                accum[key] = accum.get(key, 0) + 1
            elif tm.type in ("split", "merge"):
                warnings.append(
                    f"Table '{old_name}' has type={tm.type} — "
                    "complex transformation deferred to LLM"
                )
                needs_llm = True
            elif tm.type == "drop":
                warnings.append(
                    f"Table '{old_name}' is marked for drop — statement "
                    "references an obsolete table"
                )
                needs_llm = True

        # Pass 2: fix column qualifiers that still point to the old table name
        # (happens when the original SQL used ``TABLE.COL`` without aliasing).
        if rename_map:
            for col in tree.find_all(exp.Column):
                q = col.table
                if not q:
                    continue
                new_q = rename_map.get(q.upper())
                if new_q is not None:
                    col.set("table", exp.to_identifier(new_q, quoted=False))

        changes = [
            ChangeItem(
                kind="table",
                as_is=as_is,
                to_be=to_be,
                count=count,
                transformer=self.name,
            )
            for (as_is, to_be), count in sorted(accum.items())
        ]

        return TransformerResult(
            tree=tree,
            changes=changes,
            needs_llm=needs_llm,
            warnings=warnings,
        )


def _set_table_name(tbl: exp.Table, new_name: str) -> None:
    """Replace ``tbl.this`` with ``new_name`` preserving quoting settings.

    sqlglot stores the identifier either as a plain string (rare) or as an
    ``exp.Identifier`` — the latter carries quoting info we want to inherit.
    """

    current = tbl.this
    quoted = bool(getattr(current, "quoted", False)) if current is not None else False
    tbl.set("this", exp.to_identifier(new_name, quoted=quoted))
