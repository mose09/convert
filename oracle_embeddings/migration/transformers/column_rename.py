"""Rewrite ``columns[]`` entries of kind ``rename`` (docs/migration/spec.md §6).

Other kinds (split / merge / type_convert / value_map / drop) are untouched
here — they are the responsibility of dedicated transformers applied later in
the pipeline. Keeping this transformer narrow makes regression easier.
"""
from __future__ import annotations

from typing import Dict, List, Tuple

from sqlglot import exp

from ..mapping_model import ChangeItem, ColumnRef
from .base import RewriteContext, Transformer, TransformerResult, stmt_tables


class ColumnRenameTransformer(Transformer):
    name = "ColumnRename"

    def apply(
        self, tree: exp.Expression, context: RewriteContext
    ) -> TransformerResult:
        mapping = context.mapping
        alias_map = context.alias_map
        tables_in_stmt = stmt_tables(alias_map)

        accum: Dict[Tuple[str, str], int] = {}
        warnings: List[str] = []

        # Pass A — regular column references (SELECT/WHERE/UPDATE SET, etc.)
        for col in tree.find_all(exp.Column):
            col_name = col.name
            if not col_name:
                continue

            source_table_asis = self._resolve_source_table(
                col, col_name, alias_map, tables_in_stmt, mapping, warnings
            )
            if source_table_asis is None:
                continue

            cm = mapping.find_column(source_table_asis, col_name)
            if cm is None or cm.kind != "rename":
                continue

            to_be = cm.to_be
            if not isinstance(to_be, ColumnRef):
                continue

            _set_column_name(col, to_be.column)

            key = (
                f"{source_table_asis}.{col_name.upper()}",
                f"{to_be.table.upper()}.{to_be.column.upper()}",
            )
            accum[key] = accum.get(key, 0) + 1

        # Pass B — INSERT column lists. sqlglot models ``INSERT INTO t (a, b)``
        # as ``Insert(this=Schema(this=Table, expressions=[Identifier, ...]))``
        # so these column names never show up as ``exp.Column`` and would be
        # missed by Pass A.
        for schema in tree.find_all(exp.Schema):
            host = schema.this
            if not isinstance(host, exp.Table):
                continue
            source_table_asis = alias_map.get(host.name.upper())
            if source_table_asis is None:
                continue
            for i, ident in enumerate(list(schema.expressions)):
                if not isinstance(ident, exp.Identifier):
                    continue
                col_name = ident.name
                cm = mapping.find_column(source_table_asis, col_name)
                if cm is None or cm.kind != "rename":
                    continue
                to_be = cm.to_be
                if not isinstance(to_be, ColumnRef):
                    continue
                schema.expressions[i] = exp.to_identifier(
                    to_be.column, quoted=bool(getattr(ident, "quoted", False))
                )
                key = (
                    f"{source_table_asis}.{col_name.upper()}",
                    f"{to_be.table.upper()}.{to_be.column.upper()}",
                )
                accum[key] = accum.get(key, 0) + 1

        changes = [
            ChangeItem(
                kind="column",
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
            needs_llm=False,
            warnings=warnings,
        )

    # ---- internal ---------------------------------------------------------

    def _resolve_source_table(
        self,
        col: exp.Column,
        col_name: str,
        alias_map: Dict[str, str],
        tables_in_stmt: List[str],
        mapping,
        warnings: List[str],
    ) -> str | None:
        """Find the AS-IS table this column resolves to.

        Priority:
          1. Qualified column → look up qualifier in ``alias_map``.
          2. Unqualified column → pick the *unique* AS-IS table whose mapping
             contains this column name. Ambiguous cases log a warning and
             return None (the rewriter leaves the column untouched).
        """

        qualifier = col.table
        if qualifier:
            return alias_map.get(qualifier.upper())

        candidates = [
            t for t in tables_in_stmt
            if mapping.find_column(t, col_name) is not None
        ]
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            warnings.append(
                f"Ambiguous unqualified column '{col_name}' — candidates: "
                f"{candidates}; skipped (qualify the column in SQL or "
                "rely on qualify() with a schema)"
            )
        return None


def _set_column_name(col: exp.Column, new_name: str) -> None:
    """Preserve quoting settings when swapping the column identifier."""
    current = col.this
    quoted = bool(getattr(current, "quoted", False)) if current is not None else False
    col.set("this", exp.to_identifier(new_name, quoted=quoted))
