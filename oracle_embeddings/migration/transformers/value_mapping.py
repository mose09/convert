"""Apply ``value_map`` column mappings (e.g. Y/N → 1/0).

Behaviour:
    1. Rename the column to its TO-BE identifier (same as ColumnRename).
    2. Rewrite **adjacent literal values** in recognisable patterns so the
       literal matches the new column's domain:

          ``col = 'Y'``           → ``col = 1``
          ``col IN ('Y', 'N')``   → ``col IN (1, 0)``
          ``CASE WHEN col = 'Y'`` → … same

       Values not covered by ``value_map`` (and no ``default_value``) get
       flagged as a warning and left alone — better to surface the unknown
       state than silently bucket it into 0.

Limitations (escalated to LLM fallback):
    - ``col || 'X'`` or arithmetic involving the column aren't touched.
    - INSERT … VALUES (…) requires aligning the literal to the column
      position in the header tuple; covered in a second pass.
"""
from __future__ import annotations

from typing import Dict, List, Tuple

from sqlglot import exp

from ..mapping_model import ChangeItem, ColumnRef
from .base import RewriteContext, Transformer, TransformerResult, stmt_tables
from .column_rename import _set_column_name


class ValueMappingTransformer(Transformer):
    name = "ValueMapping"

    def apply(
        self, tree: exp.Expression, context: RewriteContext
    ) -> TransformerResult:
        mapping = context.mapping
        alias_map = context.alias_map
        tables_in_stmt = stmt_tables(alias_map)

        accum: Dict[Tuple[str, str], int] = {}
        value_changes: Dict[Tuple[str, str], int] = {}  # (as_is_val, to_be_val) -> count
        warnings: List[str] = []

        # Pass A — rename + predicate-form literal rewrite
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
            if cm is None or cm.kind != "value_map":
                continue
            to_be = cm.to_be
            if not isinstance(to_be, ColumnRef):
                continue

            _set_column_name(col, to_be.column)
            accum[
                (f"{source}.{col_name.upper()}",
                 f"{to_be.table.upper()}.{to_be.column.upper()}")
            ] = accum.get(
                (f"{source}.{col_name.upper()}",
                 f"{to_be.table.upper()}.{to_be.column.upper()}"), 0
            ) + 1

            # Find a sibling literal in EQ / NEQ / IN
            self._rewrite_adjacent_literals(
                col, cm, value_changes, warnings,
                as_is_loc=f"{source}.{col_name.upper()}",
            )

        changes = []
        # Column-rename change is kind="column" for report consistency;
        # the literal substitutions below are kind="value".
        for (as_is, to_be), count in sorted(accum.items()):
            changes.append(ChangeItem(
                kind="column", as_is=as_is, to_be=to_be,
                count=count, transformer=self.name,
            ))
        for (v_old, v_new), count in sorted(value_changes.items()):
            changes.append(ChangeItem(
                kind="value", as_is=v_old, to_be=v_new,
                count=count, transformer=self.name,
            ))

        return TransformerResult(
            tree=tree, changes=changes, needs_llm=False, warnings=warnings
        )

    def _resolve_source(self, col, col_name, alias_map, tables_in_stmt, mapping):
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

    def _rewrite_adjacent_literals(
        self,
        col: exp.Column,
        cm,
        value_changes: Dict[Tuple[str, str], int],
        warnings: List[str],
        *,
        as_is_loc: str,
    ) -> None:
        parent = col.parent
        if parent is None:
            return

        vmap = cm.value_map or {}

        # EQ / NEQ / GT / LT / GTE / LTE — binary comparison
        if isinstance(parent, (exp.EQ, exp.NEQ)):
            other = parent.right if parent.left is col else parent.left
            self._try_rewrite_literal(
                other, vmap, cm.default_value,
                value_changes, warnings, as_is_loc,
            )

        # IN (…)
        if isinstance(parent, exp.In):
            for lit in parent.args.get("expressions", []) or []:
                self._try_rewrite_literal(
                    lit, vmap, cm.default_value,
                    value_changes, warnings, as_is_loc,
                )

    def _try_rewrite_literal(
        self,
        node: exp.Expression,
        vmap: Dict,
        default,
        value_changes: Dict[Tuple[str, str], int],
        warnings: List[str],
        as_is_loc: str,
    ) -> None:
        if not isinstance(node, exp.Literal):
            return
        old_val = node.this if isinstance(node.this, str) else str(node.this)
        # Match case-sensitively on the raw literal text; value_map keys are
        # typically strings like "Y"/"N".
        if old_val in vmap:
            new_val = vmap[old_val]
        elif default is not None and old_val not in vmap:
            new_val = default
            warnings.append(
                f"value_map on {as_is_loc}: '{old_val}' not in map, "
                f"using default {default!r}"
            )
        else:
            warnings.append(
                f"value_map on {as_is_loc}: literal '{old_val}' has no "
                "mapping; left unchanged"
            )
            return

        # Replace with appropriate literal type
        if isinstance(new_val, bool):
            replacement = exp.Boolean(this=new_val)
        elif isinstance(new_val, (int, float)):
            replacement = exp.Literal.number(new_val)
        else:
            replacement = exp.Literal.string(str(new_val))
        node.replace(replacement)
        value_changes[(f"'{old_val}'", str(new_val))] = (
            value_changes.get((f"'{old_val}'", str(new_val)), 0) + 1
        )
