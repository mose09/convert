"""Transformer base + shared context.

Each concrete transformer is a stateless callable that takes a sqlglot
expression tree plus a :class:`RewriteContext` and returns a
:class:`TransformerResult`. The context carries cross-transformer state
(alias map, needs_llm flags, accumulated warnings) so the pipeline doesn't
have to re-derive it per step.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar, Dict, List, Optional

from sqlglot import exp

from ..mapping_model import ChangeItem, Mapping


@dataclass
class RewriteContext:
    """Shared state threaded through the transformer pipeline.

    ``alias_map`` uses upper-cased keys and maps qualifiers (either a table
    alias or a bare table name used as qualifier) to the AS-IS table name.
    Built once at the top of the pipeline so later transformers can resolve
    columns after ``TableRenameTransformer`` has already swapped table
    identifiers.
    """

    mapping: Mapping
    alias_map: Dict[str, str] = field(default_factory=dict)
    stmt_tables_asis: List[str] = field(default_factory=list)


@dataclass
class TransformerResult:
    """Return value of ``Transformer.apply``.

    ``changes`` uses :class:`ChangeItem` from mapping_model. Multiple entries
    are valid — aggregate by (as_is, to_be) before emitting to the report.
    """

    tree: exp.Expression
    changes: List[ChangeItem] = field(default_factory=list)
    needs_llm: bool = False
    warnings: List[str] = field(default_factory=list)


class Transformer:
    """Abstract base. Concrete transformers set ``name`` and override
    :meth:`apply`."""

    name: ClassVar[str] = "Transformer"

    def apply(
        self, tree: exp.Expression, context: RewriteContext
    ) -> TransformerResult:  # pragma: no cover - abstract
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def build_alias_map(
    tree: exp.Expression, mapping: Mapping
) -> Dict[str, str]:
    """Build ``{qualifier_upper: AS_IS_table_upper}`` from a parsed tree.

    Inverts AS-IS ↔ TO-BE renames so the lookup works *after* the table-rename
    transformer has rewritten Table nodes — callers always get the AS-IS
    table name, regardless of whether the table was already renamed.
    """

    to_be_to_as_is: Dict[str, str] = {}
    for tm in mapping.tables:
        if tm.type == "rename" and isinstance(tm.to_be, str) and isinstance(tm.as_is, str):
            to_be_to_as_is[tm.to_be.upper()] = tm.as_is.upper()

    out: Dict[str, str] = {}
    for tbl in tree.find_all(exp.Table):
        real_now = (tbl.name or "").upper()
        if not real_now:
            continue
        as_is = to_be_to_as_is.get(real_now, real_now)

        alias = tbl.alias
        if alias:
            out[alias.upper()] = as_is
        else:
            out[real_now] = as_is
            if as_is != real_now:
                # Still allow resolving by the original AS-IS name in case a
                # column qualifier references it pre-rename.
                out[as_is] = as_is
    return out


def stmt_tables(alias_map: Dict[str, str]) -> List[str]:
    """De-duplicated list of AS-IS tables touched by the current tree."""
    return sorted(set(alias_map.values()))
