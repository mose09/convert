"""Convert MyBatis OGNL placeholders into Oracle-parseable bind markers.

Used by :mod:`validator_db` before handing SQL to ``cursor.parse()`` / the
``DBMS_SQL.PARSE`` equivalent. Oracle understands ``:name`` / ``:1`` bind
markers natively, so ``#{name}`` becomes ``:name``. ``${name}`` is a literal
substitution in MyBatis so we replace it with an innocuous identifier — if
the statement actually needs ``${}`` to be an operator or DDL keyword, that
statement should be flagged for human review upstream anyway.

The conversion is purely textual; it does **not** bind values and does **not**
execute the statement. ``DBMS_SQL.PARSE`` performs the parse + schema lookup
only, so no transaction is opened.
"""
from __future__ import annotations

import re
from typing import Dict, Tuple


_OGNL_HASH_RE = re.compile(r"#\{([^}]+)\}")
_OGNL_DOLLAR_RE = re.compile(r"\$\{[^}]+\}")


def dummify(sql: str) -> str:
    """Return ``sql`` with MyBatis placeholders swapped for Oracle binds.

    - ``#{prop,jdbcType=VARCHAR}`` → ``:prop`` (jdbcType hints are stripped;
      name characters are limited to ``[A-Za-z0-9_]`` so invalid names fall
      back to ``:p`` rather than raising in the Oracle parser).
    - ``${anything}`` → ``DUMMY_IDENT`` (bare identifier that parses).
    """

    def _hash_sub(m: "re.Match[str]") -> str:
        name = _clean_bind_name(m.group(1))
        return f":{name}"

    sql = _OGNL_HASH_RE.sub(_hash_sub, sql)
    sql = _OGNL_DOLLAR_RE.sub("DUMMY_IDENT", sql)
    return sql


def dummify_with_map(sql: str) -> Tuple[str, Dict[str, str]]:
    """Like :func:`dummify` but also returns ``{bind_name: original_ognl}``.

    Useful when callers want to surface the original placeholder in validation
    error messages (e.g. "ORA-00904 at :nm (originally #{nm,jdbcType=...})").
    """
    mapping: Dict[str, str] = {}

    def _hash_sub(m: "re.Match[str]") -> str:
        original = m.group(0)
        name = _clean_bind_name(m.group(1))
        mapping[name] = original
        return f":{name}"

    def _dollar_sub(m: "re.Match[str]") -> str:
        mapping["DUMMY_IDENT"] = m.group(0)
        return "DUMMY_IDENT"

    sql = _OGNL_HASH_RE.sub(_hash_sub, sql)
    sql = _OGNL_DOLLAR_RE.sub(_dollar_sub, sql)
    return sql, mapping


def _clean_bind_name(raw: str) -> str:
    """Extract a valid Oracle bind name from the raw ``#{...}`` body.

    MyBatis allows suffixes like ``foo,jdbcType=VARCHAR,javaType=String`` —
    keep just the leading identifier token. If the result is empty, return
    a generic ``p`` to avoid emitting ``:``.
    """
    token = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)", raw or "")
    if not token:
        return "p"
    return token.group(1)
