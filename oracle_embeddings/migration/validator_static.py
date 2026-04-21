"""Stage A — sqlglot-based static validation (docs/migration/spec.md §11).

Parses TO-BE SQL, then checks that every ``exp.Table`` and ``exp.Column``
reference maps to a real entry in the TO-BE schema. No database access is
required; this is the first of the two validation stages (Stage B uses
``DBMS_SQL.PARSE`` and is implemented in Step 8).

The validator is deliberately forgiving about things it can't resolve
deterministically (subquery aliases, CTE bodies) — those become warnings, not
errors, so the user still sees the clear ``UNKNOWN_COLUMN`` / ``UNKNOWN_TABLE``
signals without drowning in noise.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Literal, Optional, Set

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from .sql_rewriter import mask_mybatis_placeholders

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


IssueLevel = Literal["error", "warning"]


@dataclass
class ValidationIssue:
    level: IssueLevel
    code: str                    # UNKNOWN_TABLE / UNKNOWN_COLUMN / PARSE_FAIL / AMBIGUOUS_COLUMN
    message: str
    location: Optional[str] = None


@dataclass
class ValidationResult:
    ok: bool
    issues: List[ValidationIssue] = field(default_factory=list)
    parse_error: Optional[str] = None

    @property
    def errors(self) -> List[ValidationIssue]:
        return [i for i in self.issues if i.level == "error"]

    @property
    def warnings(self) -> List[ValidationIssue]:
        return [i for i in self.issues if i.level == "warning"]


# ---------------------------------------------------------------------------
# Oracle pseudocolumns — parsed as exp.Column but not in any user schema
# ---------------------------------------------------------------------------


_ORACLE_PSEUDOCOLS: Set[str] = {
    "ROWNUM", "ROWID",
    "USER", "UID", "SYSDATE", "SYSTIMESTAMP",
    "LEVEL",
    "CURRVAL", "NEXTVAL",
    "COLUMN_VALUE",
    "SESSIONTIMEZONE", "DBTIMEZONE",
    "ORA_ROWSCN",
    "NULL", "TRUE", "FALSE",
}

_ORACLE_PSEUDO_TABLES: Set[str] = {
    "DUAL",
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_static(
    to_be_sql: str,
    to_be_schema: Dict[str, Set[str]],
) -> ValidationResult:
    """Parse + schema-check ``to_be_sql`` against ``to_be_schema``.

    ``to_be_schema`` is ``{TABLE_UPPER: {COLUMN_UPPER, ...}}`` — typically
    produced via :func:`impact_analyzer.load_schema_tables`.

    MyBatis OGNL placeholders (``#{x}``, ``${y}``) are masked before parsing
    and never appear in the issue list — we only validate identifiers that
    survive into the canonical Oracle AST.
    """

    safe_sql, _tokens = mask_mybatis_placeholders(to_be_sql)

    try:
        tree = sqlglot.parse_one(safe_sql, dialect="oracle")
    except ParseError as exc:
        return ValidationResult(
            ok=False,
            parse_error=str(exc),
            issues=[ValidationIssue(
                level="error",
                code="PARSE_FAIL",
                message=str(exc),
            )],
        )
    except Exception as exc:  # pragma: no cover - defensive
        return ValidationResult(
            ok=False,
            parse_error=f"unexpected parse error: {exc}",
            issues=[ValidationIssue(
                level="error",
                code="PARSE_FAIL",
                message=str(exc),
            )],
        )

    issues: List[ValidationIssue] = []

    cte_names = {
        (cte.alias_or_name or "").upper()
        for cte in tree.find_all(exp.CTE)
        if cte.alias_or_name
    }

    # Table resolution pass ------------------------------------------------
    alias_map: Dict[str, str] = {}      # qualifier_upper → table_upper
    stmt_tables: List[str] = []

    for tbl in tree.find_all(exp.Table):
        name = (tbl.name or "").upper()
        if not name:
            continue

        alias_key = (tbl.alias or "").upper() or name
        alias_map[alias_key] = name
        # Bare-name qualifier fallback so ``CUST.CUST_NM`` resolves even when
        # the user didn't alias the table.
        alias_map.setdefault(name, name)

        if name in stmt_tables:
            continue
        stmt_tables.append(name)

        if name in cte_names or name in _ORACLE_PSEUDO_TABLES:
            continue

        if name not in to_be_schema:
            issues.append(ValidationIssue(
                level="error",
                code="UNKNOWN_TABLE",
                message=f"table '{name}' not in TO-BE schema",
                location=name,
            ))

    # Column resolution pass -----------------------------------------------
    for col in tree.find_all(exp.Column):
        cname = (col.name or "").upper()
        if not cname:
            continue
        if cname in _ORACLE_PSEUDOCOLS:
            continue
        if cname.startswith("MBP_"):
            # Masked MyBatis placeholder — ignore.
            continue

        qualifier = (col.table or "").upper()
        if qualifier:
            source = alias_map.get(qualifier)
            if source is None:
                # Likely a subquery/CTE-local alias — can't resolve without a
                # deeper scope walker. Flag as warning so the user can eyeball
                # without treating it as a hard failure.
                issues.append(ValidationIssue(
                    level="warning",
                    code="UNRESOLVED_QUALIFIER",
                    message=f"qualifier '{qualifier}' for column '{cname}' "
                    "is not a known table or alias in this statement",
                    location=f"{qualifier}.{cname}",
                ))
                continue
            if source in cte_names or source in _ORACLE_PSEUDO_TABLES:
                continue
            cols = to_be_schema.get(source)
            if cols is None:
                # Already reported as UNKNOWN_TABLE — don't double-flag.
                continue
            if cname not in cols:
                issues.append(ValidationIssue(
                    level="error",
                    code="UNKNOWN_COLUMN",
                    message=f"column '{source}.{cname}' not in TO-BE schema",
                    location=f"{source}.{cname}",
                ))
        else:
            matches = [
                t for t in stmt_tables
                if t in to_be_schema and cname in to_be_schema[t]
            ]
            if not matches:
                # Before failing, check if any stmt table is a CTE — if so,
                # this column may come from the CTE body and we can't resolve.
                if any(t in cte_names for t in stmt_tables):
                    issues.append(ValidationIssue(
                        level="warning",
                        code="UNRESOLVED_COLUMN",
                        message=f"unqualified column '{cname}' may reference "
                        "a CTE body; skipped",
                        location=cname,
                    ))
                    continue
                issues.append(ValidationIssue(
                    level="error",
                    code="UNKNOWN_COLUMN",
                    message=f"unqualified column '{cname}' not found in any "
                    f"table in this statement {stmt_tables}",
                    location=cname,
                ))
            elif len(matches) > 1:
                issues.append(ValidationIssue(
                    level="warning",
                    code="AMBIGUOUS_COLUMN",
                    message=f"unqualified column '{cname}' matches multiple "
                    f"tables: {matches}",
                    location=cname,
                ))

    ok = not any(i.level == "error" for i in issues)
    return ValidationResult(ok=ok, issues=issues)
