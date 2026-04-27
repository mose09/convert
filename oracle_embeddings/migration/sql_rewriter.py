"""Top-level SQL rewriter (docs/migration/spec.md §6).

The pipeline parses a single static SQL statement with sqlglot, threads it
through a fixed transformer list, and re-emits Oracle-dialect SQL. Higher
levels (xml_rewriter) handle MyBatis dynamic tags and the AS-IS↔TO-BE
diffing.

Design notes
------------

* ``qualify()`` is deliberately NOT run by default. It's handy for column
  disambiguation but it also forces aliases on every table and quotes every
  identifier, which makes the emitted SQL noticeably harder to diff against
  the original. Qualified output is only useful when a column can't be
  resolved otherwise; the transformers fall back to simple alias-map lookup
  which covers all the real-world cases in the test corpus.
* The pipeline is additive — each transformer returns the (possibly mutated)
  tree plus a list of ``ChangeItem`` entries. Transformers that aren't yet
  implemented degrade gracefully by setting ``needs_llm=True`` (caught here
  and surfaced in the outcome).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from .mapping_model import ChangeItem, Mapping, Status
from .transformers import (
    ColumnMergeTransformer,
    ColumnRenameTransformer,
    ColumnSplitTransformer,
    DroppedColumnChecker,
    JoinPathRewriter,
    RewriteContext,
    TableRenameTransformer,
    Transformer,
    TransformerResult,
    TypeConversionTransformer,
    ValueMappingTransformer,
)
from .transformers.base import build_alias_map

# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


DEFAULT_PIPELINE: List[Transformer] = [
    TableRenameTransformer(),
    ColumnRenameTransformer(),
    ColumnSplitTransformer(),
    ColumnMergeTransformer(),
    TypeConversionTransformer(),
    ValueMappingTransformer(),
    JoinPathRewriter(),
    DroppedColumnChecker(),
]


# ---------------------------------------------------------------------------
# Outcome
# ---------------------------------------------------------------------------


@dataclass
class SqlRewriteOutcome:
    """Output of :func:`rewrite_sql`. ``status`` follows the spec §5 enum."""

    as_is_sql: str
    to_be_sql: Optional[str]
    status: Status
    applied_transformers: List[str] = field(default_factory=list)
    changed_items: List[ChangeItem] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    parse_error: Optional[str] = None
    needs_llm: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.changed_items)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def rewrite_sql(
    sql: str,
    mapping: Mapping,
    *,
    pipeline: Optional[List[Transformer]] = None,
    pretty: bool = False,
) -> SqlRewriteOutcome:
    """Rewrite a single static SQL statement through the transformer pipeline.

    Parameters
    ----------
    sql
        Full static SQL text. Dynamic MyBatis tags must be expanded first
        (``dynamic_sql_expander``). OGNL placeholders like ``#{foo}`` /
        ``${bar}`` are left alone — sqlglot treats them as parameters.
    mapping
        Already-loaded :class:`~mapping_model.Mapping`.
    pipeline
        Overrides the default transformer list (mostly for tests).
    pretty
        Pass ``pretty=True`` to sqlglot for indented multi-line output. Off
        by default because our XML rewriter prefers compact single-line SQL.
    """

    pipeline = pipeline if pipeline is not None else DEFAULT_PIPELINE

    # Escape MyBatis OGNL placeholders so sqlglot can parse. sqlglot rejects
    # ``#{foo}`` outright and turns ``${foo}`` into a struct literal — neither
    # is acceptable. We replace each occurrence with a unique bareword token
    # (``__MBP_0__``, ``__MBP_1__`` …) that parses cleanly as an identifier,
    # then restore the originals after re-emit.
    safe_sql, mbp_tokens = mask_mybatis_placeholders(sql)

    try:
        tree = sqlglot.parse_one(safe_sql, dialect="oracle")
    except ParseError as exc:
        return SqlRewriteOutcome(
            as_is_sql=sql,
            to_be_sql=None,
            status="PARSE_FAIL",
            parse_error=str(exc),
        )
    except Exception as exc:  # pragma: no cover - defensive
        return SqlRewriteOutcome(
            as_is_sql=sql,
            to_be_sql=None,
            status="PARSE_FAIL",
            parse_error=f"unexpected parse error: {exc}",
        )

    context = RewriteContext(
        mapping=mapping,
        alias_map=build_alias_map(tree, mapping),
    )
    context.stmt_tables_asis = sorted(set(context.alias_map.values()))

    applied: List[str] = []
    all_changes: List[ChangeItem] = []
    all_warnings: List[str] = []
    needs_llm_overall = False

    for t in pipeline:
        try:
            result: TransformerResult = t.apply(tree, context)
        except Exception as exc:  # pragma: no cover - surface via report
            all_warnings.append(
                f"{t.name} raised {type(exc).__name__}: {exc}"
            )
            continue
        tree = result.tree
        # Only record transformer in ``applied_transformers`` when it actually
        # did something — either produced changes, emitted warnings, or
        # escalated to LLM. Previously every pipeline step was logged which
        # made the Conversions sheet show all 8 names regardless of impact.
        if result.changes or result.warnings or result.needs_llm:
            applied.append(t.name)
        all_changes.extend(result.changes)
        all_warnings.extend(result.warnings)
        if result.needs_llm:
            needs_llm_overall = True
        # Refresh alias map when tables got renamed so downstream transformers
        # still resolve to AS-IS names.
        if any(c.kind == "table" for c in result.changes):
            context.alias_map = build_alias_map(tree, mapping)
            context.stmt_tables_asis = sorted(set(context.alias_map.values()))

    try:
        to_be_sql = tree.sql(dialect="oracle", pretty=pretty)
    except Exception as exc:  # pragma: no cover - defensive
        return SqlRewriteOutcome(
            as_is_sql=sql,
            to_be_sql=None,
            status="PARSE_FAIL",
            applied_transformers=applied,
            changed_items=all_changes,
            warnings=all_warnings,
            parse_error=f"re-emit failed: {exc}",
            needs_llm=needs_llm_overall,
        )

    to_be_sql = unmask_mybatis_placeholders(to_be_sql, mbp_tokens)

    status = _determine_status(all_changes, all_warnings, needs_llm_overall)

    return SqlRewriteOutcome(
        as_is_sql=sql,
        to_be_sql=to_be_sql,
        status=status,
        applied_transformers=applied,
        changed_items=_aggregate_changes(all_changes),
        warnings=all_warnings,
        needs_llm=needs_llm_overall,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _determine_status(
    changes: List[ChangeItem],
    warnings: List[str],
    needs_llm: bool,
) -> Status:
    if needs_llm:
        return "NEEDS_LLM"
    if warnings:
        return "AUTO_WARN"
    return "AUTO"


_MYBATIS_PLACEHOLDER_RE = re.compile(r"[#$]\{[^{}]+\}")


# Bareword token format: ``__MBP_{n}__``. Two leading + two trailing
# underscores make collision with real Oracle identifiers extremely unlikely
# (any user identifier ``MBP_X`` no longer matches), and the trailing ``__``
# acts as a terminator so ``__MBP_1__`` is never a substring of ``__MBP_10__``
# — keeping :func:`unmask_mybatis_placeholders` order-independent.
_MBP_TOKEN_PREFIX = "__MBP_"
_MBP_TOKEN_SUFFIX = "__"
_MBP_TOKEN_RE = re.compile(r"__MBP_\d+__")


def mask_mybatis_placeholders(sql: str) -> Tuple[str, Dict[str, str]]:
    """Swap each MyBatis OGNL placeholder for a unique bareword token.

    Returns ``(safe_sql, {token: original})``. Tokens use the
    ``__MBP_{n}__`` format (MyBatis Placeholder) so they parse cleanly as a
    sqlglot identifier without colliding with real Oracle identifiers.
    Exposed as public API so :mod:`validator_static`, :mod:`sql_formatter`,
    and :mod:`comment_injector` reuse the same convention — see
    :data:`_MBP_TOKEN_RE` for the canonical match pattern.
    """
    tokens: Dict[str, str] = {}

    def _sub(match: "re.Match[str]") -> str:
        i = len(tokens)
        token = f"{_MBP_TOKEN_PREFIX}{i}{_MBP_TOKEN_SUFFIX}"
        tokens[token] = match.group(0)
        return token

    safe = _MYBATIS_PLACEHOLDER_RE.sub(_sub, sql)
    return safe, tokens


def unmask_mybatis_placeholders(sql: str, tokens: Dict[str, str]) -> str:
    for token, original in tokens.items():
        sql = sql.replace(token, original)
    return sql


def _aggregate_changes(changes: List[ChangeItem]) -> List[ChangeItem]:
    """Collapse duplicate ``(kind, as_is, to_be, transformer)`` entries by
    summing counts so the Excel report doesn't explode when a column appears
    multiple times within the same SQL (SELECT + WHERE, etc.)."""

    agg: Dict[tuple, ChangeItem] = {}
    for c in changes:
        key = (c.kind, c.as_is, c.to_be, c.transformer)
        if key in agg:
            agg[key].count += c.count
        else:
            agg[key] = ChangeItem(
                kind=c.kind,
                as_is=c.as_is,
                to_be=c.to_be,
                count=c.count,
                transformer=c.transformer,
            )
    return sorted(agg.values(), key=lambda x: (x.kind, x.as_is))
