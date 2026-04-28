"""Korean Legacy / ANSI 스타일 SQL 포매터.

사용자가 국내 레거시 현장에서 쓰는 "SELECT/FROM/WHERE 를 6-char 우측정렬,
리딩 콤마, 컬럼 주석 인라인" 패턴을 재현. 기존 sqlglot 의 generic
``pretty=True`` 는 이 스타일을 만들지 못하므로 AST 를 직접 walk 해
라인별로 emit.

사용:
    from oracle_embeddings.migration.sql_formatter import (
        format_sql, KoreanLegacyStyle, AnsiStyle,
    )
    formatted = format_sql(to_be_sql, style=KoreanLegacyStyle(),
                            ko_lookup={"CUSTOMER.NAME": "고객명"})

스타일 프로파일:
    - 모든 절-keyword 를 공통 7-char prefix 로 맞춰 ``text starts at col 7``
    - SELECT/FROM/WHERE/AND/OR/ON/HAVING 은 공백 padding 으로 우측정렬
    - INNER JOIN / ORDER BY / GROUP BY 처럼 긴 keyword 는 자체 폭 사용
    - 리딩 콤마 ``     , `` (5 spaces + comma + space = 7 chars)
    - 테이블 주석은 ``/* T:한글 */`` prefix, 컬럼 주석은 ``/* 한글 */``
    - ANSI 스타일은 모든 keyword 를 왼쪽 정렬 + 4-space indent (미구현
      스텁만, 사용자 표준이 변경되면 채움)

Scope 제한:
    - 최상위 exp.Select / exp.Update / exp.Insert / exp.Delete / exp.Merge 처리
    - CTE (``WITH``) 는 본문을 포매터에 재귀 적용
    - 중첩 subquery 는 sqlglot 기본 emit 으로 fallback (한 줄)
    - MyBatis 동적 태그 (``<if>`` 등) 경계는 xml_rewriter 레이어의 몫 —
      여기선 expand 된 static SQL 만 받음
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from .sql_rewriter import mask_mybatis_placeholders, unmask_mybatis_placeholders


# ---------------------------------------------------------------------------
# Style profiles
# ---------------------------------------------------------------------------


@dataclass
class KoreanLegacyStyle:
    """국내 레거시 표준. 변경 어렵게 const 에 박아둠.

    * 기본 prefix 폭 7 (끝이 col 6 인 keyword + space)
    * comma-continuation 도 동일 7 col → SELECT/FROM/GROUP BY 리스트 정렬
    * JOIN/ORDER BY/GROUP BY 는 자체 폭 (keyword 자체가 길어서 7 을 넘음)
    """

    name: str = "korean_legacy"
    keyword_col_width: int = 6   # text starts at col 7
    leading_comma: bool = True
    keyword_case: str = "upper"  # keyword 항상 대문자
    table_comment_prefix: str = "T:"
    emit_column_comments: bool = True  # 있을 때만 자동 인라인
    # 컬럼 주석 폭 통일 여부 (블록 내 주석 끝 col 맞춤)
    normalize_comment_width: bool = True

    def keyword_prefix(self, kw: str) -> str:
        """주어진 keyword 를 우측정렬해 ``keyword + space`` 반환.

        폭이 ``keyword_col_width`` 보다 크면 자체 폭 유지 (JOIN 등).
        """
        kw = kw.upper()
        pad = self.keyword_col_width - len(kw)
        if pad < 0:
            return " " + kw + " "  # 한 칸 들여서 자체 폭 유지
        return " " * pad + kw + " "

    def comma_prefix(self) -> str:
        """리딩 콤마 줄의 prefix — keyword 와 정렬."""
        # 끝이 col 6 인 ``,`` + space 7 → 5 space + ", "
        return " " * (self.keyword_col_width - 1) + ", "


@dataclass
class AnsiStyle:
    """ANSI-ish. 미래 확장용 스텁; 현재는 fallback = sqlglot pretty."""

    name: str = "ansi"
    keyword_col_width: int = 0
    leading_comma: bool = False
    keyword_case: str = "upper"
    table_comment_prefix: str = ""
    emit_column_comments: bool = False
    normalize_comment_width: bool = False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def format_sql(
    sql: str,
    *,
    style: Optional[object] = None,
    ko_lookup: Optional[Dict[str, str]] = None,
) -> str:
    """Format a single SQL statement per the given style.

    Parse / placeholder-masking / emit 의 실패는 원본을 그대로 반환해
    호출 측이 fallback 로 취급할 수 있게 함. ``style`` 이 ANSI / None 이면
    현재는 sqlglot ``pretty=True`` fallback.
    """
    if style is None:
        style = KoreanLegacyStyle()
    if getattr(style, "name", "") != "korean_legacy":
        try:
            masked, mapping = mask_mybatis_placeholders(sql)
            tree = sqlglot.parse_one(masked, dialect="oracle")
            out = tree.sql(dialect="oracle", pretty=True)
            return unmask_mybatis_placeholders(out, mapping)
        except Exception:
            return sql

    try:
        masked, ph_map = mask_mybatis_placeholders(sql)
        tree = sqlglot.parse_one(masked, dialect="oracle")
    except ParseError:
        return sql

    fmt = _Formatter(style, ko_lookup or {})
    try:
        out = fmt.emit(tree)
    except Exception:
        out = tree.sql(dialect="oracle")
    return unmask_mybatis_placeholders(out, ph_map)


# ---------------------------------------------------------------------------
# Internal AST walker
# ---------------------------------------------------------------------------


@dataclass
class _Line:
    prefix: str         # e.g. "SELECT ", "     , "
    text: str           # 표현식 본문
    comment: str = ""   # ``/* 한글 */`` (있으면)


class _Formatter:
    def __init__(self, style: KoreanLegacyStyle, ko_lookup: Dict[str, str]):
        self.style = style
        self.ko = _normalise_lookup(ko_lookup)

    # ── Dispatcher ──────────────────────────────────────────────────────
    def emit(self, node: exp.Expression) -> str:
        if isinstance(node, exp.Select):
            return self._emit_select(node)
        if isinstance(node, exp.Update):
            return self._emit_update(node)
        if isinstance(node, exp.Insert):
            return self._emit_insert(node)
        if isinstance(node, exp.Delete):
            return self._emit_delete(node)
        if isinstance(node, exp.Merge):
            return self._emit_merge(node)
        # With clause carrying a top-level select
        if isinstance(node, exp.With):
            # fallback — CTE 자체는 sqlglot 기본 사용 (복잡)
            return node.sql(dialect="oracle", pretty=True)
        return node.sql(dialect="oracle")

    # ── SELECT ──────────────────────────────────────────────────────────
    def _emit_select(self, sel: exp.Select) -> str:
        lines: List[_Line] = []

        # WITH (CTE) — ``WITH a AS ( ... ), b AS ( ... ) SELECT ...`` 형태.
        # sqlglot 의 Oracle dialect 는 ``with_`` 키로 저장 (다른 dialect 는
        # ``with`` — 두 키 모두 조회). 첫 CTE 는 ``WITH`` prefix, 나머지는
        # leading-comma prefix, 각 본문은 multi-line nested SELECT.
        with_clause = sel.args.get("with_") or sel.args.get("with")
        if with_clause is not None and with_clause.expressions:
            lines.extend(self._emit_with(with_clause))

        # SELECT 절
        projs = sel.expressions or []
        proj_lines = self._build_list_lines(
            items=projs,
            first_prefix=self._kw("SELECT"),
            cont_prefix=self.style.comma_prefix(),
        )
        lines.extend(proj_lines)

        # FROM 절
        frm = sel.args.get("from_") or sel.args.get("from")
        from_items = _from_sources(frm) if frm is not None else []
        # 실제 JOIN 과 comma-FROM 형태의 Join 을 분리.
        # sqlglot 는 Oracle 의 ``FROM T1, T2`` 를 From(T1) + Join(T2, kind=None,
        # on=None) 로 파싱한다 → bare Join 은 FROM 리스트 연속으로 취급.
        all_joins = sel.args.get("joins") or []
        real_joins = []
        for j in all_joins:
            if not j.args.get("kind") and not j.args.get("side") and not j.args.get("on"):
                from_items.append(j.this)  # Join 의 Table 만 빼서 FROM 리스트에
            else:
                real_joins.append(j)
        if from_items:
            lines.extend(self._build_list_lines(
                items=from_items,
                first_prefix=self._kw("FROM"),
                cont_prefix=self.style.comma_prefix(),
                is_from=True,
            ))

        # JOIN 절 — INNER/LEFT/RIGHT/OUTER + ON 절
        for join in real_joins:
            lines.extend(self._emit_join(join))

        # WHERE 절
        where = sel.args.get("where")
        if where is not None:
            lines.extend(self._emit_where(where))

        # GROUP BY
        grp = sel.args.get("group")
        if grp is not None and grp.expressions:
            lines.extend(self._build_list_lines(
                items=grp.expressions,
                first_prefix=self._kw("GROUP BY"),
                cont_prefix=self.style.comma_prefix(),
            ))

        # HAVING
        hav = sel.args.get("having")
        if hav is not None:
            lines.extend(self._split_predicate(
                hav.this,
                first_prefix=self._kw("HAVING"),
                cont_kw="AND",
            ))

        # ORDER BY
        order = sel.args.get("order")
        if order is not None and order.expressions:
            lines.extend(self._build_list_lines(
                items=order.expressions,
                first_prefix=self._kw("ORDER BY"),
                cont_prefix=self.style.comma_prefix(),
            ))

        return self._render(lines)

    # ── UPDATE ──────────────────────────────────────────────────────────
    def _emit_update(self, upd: exp.Update) -> str:
        lines: List[_Line] = []

        tgt = upd.this
        target_sql = self._sql(tgt) if tgt else ""
        lines.append(_Line(
            prefix=self._kw("UPDATE"),
            text=target_sql,
            comment=self._table_comment(tgt),
        ))

        # SET 리스트 (EQ 들)
        set_items = upd.args.get("expressions") or []
        if set_items:
            # 각 EQ 를 ``LHS = RHS`` 로 한 줄씩
            first = True
            for eq in set_items:
                if not isinstance(eq, exp.EQ):
                    continue
                lhs = self._sql(eq.this)
                rhs = self._sql(eq.args.get("expression"))
                line_text = f"{lhs} = {rhs}"
                col_comment = self._col_comment(eq.this)
                lines.append(_Line(
                    prefix=self._kw("SET") if first else self.style.comma_prefix(),
                    text=line_text,
                    comment=col_comment,
                ))
                first = False

        where = upd.args.get("where")
        if where is not None:
            lines.extend(self._emit_where(where))

        return self._render(lines)

    # ── INSERT ──────────────────────────────────────────────────────────
    def _emit_insert(self, ins: exp.Insert) -> str:
        lines: List[_Line] = []

        # target + 컬럼 리스트
        target = ins.this
        if isinstance(target, exp.Schema):
            tbl = target.this
            cols = target.expressions or []
            lines.append(_Line(
                prefix=self._kw("INSERT"),
                text=f"INTO {self._sql(tbl)} (",
                comment=self._table_comment(tbl),
            ))
            # 컬럼 이름들을 leading-comma 로 풀어 적음
            for i, c in enumerate(cols):
                col_sql = self._sql(c)
                col_comment = self._col_comment(c, tbl)
                lines.append(_Line(
                    prefix=self.style.comma_prefix() if i else self._kw(""),
                    text=col_sql,
                    comment=col_comment,
                ))
            # 닫는 괄호
            lines.append(_Line(prefix=self._kw(""), text=")"))
        elif isinstance(target, exp.Table):
            lines.append(_Line(
                prefix=self._kw("INSERT"),
                text=f"INTO {self._sql(target)}",
                comment=self._table_comment(target),
            ))
        else:
            lines.append(_Line(prefix=self._kw("INSERT"), text=self._sql(target)))

        # VALUES / SELECT source
        source = ins.args.get("expression")
        if isinstance(source, exp.Values):
            # VALUES (v1, v2), (v3, v4), ...
            first = True
            for tup in source.expressions:
                if not isinstance(tup, exp.Tuple):
                    continue
                tup_sql = self._sql(tup)  # "(v1, v2)"
                lines.append(_Line(
                    prefix=self._kw("VALUES") if first else self.style.comma_prefix(),
                    text=tup_sql,
                ))
                first = False
        elif isinstance(source, exp.Select):
            # SELECT ... 는 재귀 적용 → sub-format 후 각 줄 그대로 이어붙임
            sub = self._emit_select(source)
            lines.append(_Line(prefix="", text=sub, comment=""))  # 라인 그대로 삽입
        elif source is not None:
            lines.append(_Line(prefix="", text=self._sql(source)))

        return self._render(lines)

    # ── DELETE ──────────────────────────────────────────────────────────
    def _emit_delete(self, dele: exp.Delete) -> str:
        lines: List[_Line] = []
        tgt = dele.this
        lines.append(_Line(
            prefix=self._kw("DELETE"),
            text=f"FROM {self._sql(tgt)}" if tgt else "",
            comment=self._table_comment(tgt),
        ))
        where = dele.args.get("where")
        if where is not None:
            lines.extend(self._emit_where(where))
        return self._render(lines)

    # ── MERGE ───────────────────────────────────────────────────────────
    def _emit_merge(self, m: exp.Merge) -> str:
        # MERGE 는 레이아웃이 특수해서 기본 pretty 로 fallback (Phase 3b 확장)
        return m.sql(dialect="oracle", pretty=True)

    # ── JOIN ────────────────────────────────────────────────────────────
    def _emit_join(self, join: exp.Join) -> List[_Line]:
        kind = (join.args.get("kind") or "").upper()
        side = (join.args.get("side") or "").upper()
        parts = [side, kind, "JOIN"] if (side or kind) else ["JOIN"]
        keyword = " ".join(p for p in parts if p)
        tgt = join.this
        target_sql = self._sql(tgt)
        lines = [_Line(
            prefix=self._kw(keyword),
            text=target_sql,
            comment=self._table_comment(tgt),
        )]
        cond = join.args.get("on")
        if cond is not None:
            # AND chain 전개
            lines.extend(self._split_predicate(
                cond,
                first_prefix=self._kw("ON"),
                cont_kw="AND",
            ))
        using = join.args.get("using") or []
        if using:
            lines.append(_Line(
                prefix=self._kw("USING"),
                text="(" + ", ".join(self._sql(u) for u in using) + ")",
            ))
        return lines

    # ── WITH (CTE) ──────────────────────────────────────────────────────
    def _emit_with(self, with_clause: exp.With) -> List[_Line]:
        """Emit ``WITH name AS ( ... ), name2 AS ( ... )`` — leading-comma
        between CTEs, each body multi-line nested SELECT.
        """
        lines: List[_Line] = []
        ctes = with_clause.expressions or []
        indent = " " * (self.style.keyword_col_width + 1)
        for i, cte in enumerate(ctes):
            alias = cte.alias_or_name or ""
            inner = cte.this
            if isinstance(inner, exp.Select):
                body = self._wrap_nested_select(inner)
                text = f"{alias} AS {body}" if alias else body
            else:
                # Non-Select CTE body (rare) — fall back to generic emit.
                text = self._sql(cte)
            prefix = self._kw("WITH") if i == 0 else self.style.comma_prefix()
            lines.append(_Line(prefix=prefix, text=text))
        return lines

    # ── WHERE ───────────────────────────────────────────────────────────
    def _emit_where(self, where: exp.Where) -> List[_Line]:
        return self._split_predicate(
            where.this,
            first_prefix=self._kw("WHERE"),
            cont_kw="AND",
        )

    # ── 공통: AND/OR chain 분해 + 단순 `<lhs> <op> <rhs>` 의 = 정렬 ─────
    def _split_predicate(
        self, pred: exp.Expression, *, first_prefix: str, cont_kw: str
    ) -> List[_Line]:
        """Flatten an AND chain and align simple comparison operators.

        For predicates of the form ``<column> <op> <expr>`` (EQ / NEQ / GT /
        LT / GTE / LTE), the LHS column gets right-padded so every
        operator (``=``, ``<>``, …) lands on the same column — matching the
        Korean legacy convention::

            WHERE EQ_MST_ID = #{EQ_MST_ID}
              AND EQ_ID     = #{EQ_ID}

        Predicates that don't match the simple shape (function calls,
        ``EXISTS``, ``IN`` with subquery, ``BETWEEN``, …) are emitted as-is
        and don't disturb the alignment of their neighbors.
        """
        parts = list(_flatten_and(pred))

        # Pass 1: classify each predicate.
        Tracked = Tuple[str, str, str, str]  # (prefix, lhs_text, op_text, rhs_text)
        Plain   = Tuple[str, str]            # (prefix, raw_sql_text)
        items: List[object] = []
        simple_lhs_widths: List[int] = []
        for i, p in enumerate(parts):
            prefix = first_prefix if i == 0 else self._kw(cont_kw)
            simple = _classify_simple_compare(p)
            if simple is not None:
                lhs_text = self._sql(simple.lhs)
                rhs_text = self._sql(simple.rhs)
                items.append((prefix, lhs_text, simple.op, rhs_text))
                simple_lhs_widths.append(len(lhs_text))
            else:
                items.append((prefix, self._sql(p)))

        # Pass 2: figure out target LHS width from the simple ones only.
        target = max(simple_lhs_widths, default=0)

        lines: List[_Line] = []
        for it in items:
            if len(it) == 4:
                prefix, lhs, op, rhs = it  # type: ignore[misc]
                padded_lhs = lhs.ljust(target) if target else lhs
                lines.append(_Line(
                    prefix=prefix,
                    text=f"{padded_lhs} {op} {rhs}",
                ))
            else:
                prefix, raw = it  # type: ignore[misc]
                lines.append(_Line(prefix=prefix, text=raw))
        return lines

    # ── 공통: 리스트 항목 줄 빌드 ───────────────────────────────────────
    def _build_list_lines(
        self,
        *,
        items: List[exp.Expression],
        first_prefix: str,
        cont_prefix: str,
        is_from: bool = False,
    ) -> List[_Line]:
        lines: List[_Line] = []
        for i, it in enumerate(items):
            text = self._sql(it)
            comment = (
                self._table_comment(it) if is_from else self._col_comment(it)
            )
            lines.append(_Line(
                prefix=first_prefix if i == 0 else cont_prefix,
                text=text,
                comment=comment,
            ))
        return lines

    # ── 공통: 렌더 (폭 통일 + 주석 right-pad) ──────────────────────────
    def _render(self, lines: List[_Line]) -> str:
        # 각 줄의 ``prefix + text`` 길이의 max 에 맞춰 주석 시작 col 통일
        if self.style.normalize_comment_width and any(l.comment for l in lines):
            body_widths = [len(l.prefix) + len(l.text) for l in lines]
            target = max(body_widths) + 2  # text 뒤 2-space gap
            # 주석 본문 폭도 max 맞춤 — ``/* 한글 */`` 에서 ``한글`` 부분만.
            # 근데 좌->우 갈수록 주석 폭이 다를 수 있음 → 한 block 기준.
            comments_inner = [_strip_comment_braces(l.comment) for l in lines]
            max_ci = max((len(c) for c in comments_inner if c), default=0)
        else:
            target = 0
            comments_inner = [_strip_comment_braces(l.comment) for l in lines]
            max_ci = 0

        out = []
        for l, ci in zip(lines, comments_inner):
            body = l.prefix + l.text
            if l.comment:
                pad = max(target - len(body), 1)
                ci_padded = ci.ljust(max_ci) if max_ci else ci
                out.append(body + " " * pad + f"/* {ci_padded} */")
            else:
                out.append(body.rstrip())
        return "\n".join(out)

    # ── 내부 헬퍼 ────────────────────────────────────────────────────────
    def _kw(self, kw: str) -> str:
        if not kw:
            return " " * (self.style.keyword_col_width + 1)  # 7-space 빈 prefix
        return self.style.keyword_prefix(kw)

    def _sql(self, node) -> str:
        """Emit a single AST node to SQL text. Falls through to sqlglot's
        generic emit, but specially handles nested SELECTs so they pick up
        our KoreanLegacy layout (leading-comma columns, ``=`` alignment,
        keyword right-justification) instead of getting flattened to a
        single line.
        """
        if node is None:
            return ""
        # ``(SELECT ...) AS x`` — scalar subquery in projection list arrives
        # wrapped in exp.Alias. Unwrap and re-emit with the alias appended.
        if isinstance(node, exp.Alias) and isinstance(node.this, exp.Subquery):
            sub = node.this
            if isinstance(sub.this, exp.Select):
                alias_name = node.alias or ""
                inner = self._wrap_nested_select(sub.this)
                return inner + (f" AS {alias_name}" if alias_name else "")
        # ``(SELECT ...)`` — surfaced from FROM inline views, scalar
        # subqueries in SELECT, IN-subqueries on the RHS of comparisons.
        if isinstance(node, exp.Subquery) and isinstance(node.this, exp.Select):
            return self._wrap_nested_select(node.this, alias=node.alias_or_name)
        # ``EXISTS (SELECT ...)``
        if isinstance(node, exp.Exists):
            inner = node.this
            if isinstance(inner, exp.Subquery) and isinstance(inner.this, exp.Select):
                return "EXISTS " + self._wrap_nested_select(inner.this)
            if isinstance(inner, exp.Select):
                return "EXISTS " + self._wrap_nested_select(inner)
        # ``<lhs> IN (SELECT ...)`` — exp.In with ``query`` arg set
        if isinstance(node, exp.In):
            query = node.args.get("query")
            if query is not None:
                if isinstance(query, exp.Subquery) and isinstance(query.this, exp.Select):
                    return f"{self._sql(node.this)} IN " + self._wrap_nested_select(query.this)
                if isinstance(query, exp.Select):
                    return f"{self._sql(node.this)} IN " + self._wrap_nested_select(query)
        return node.sql(dialect="oracle")

    def _wrap_nested_select(
        self, sel: exp.Select, *, alias: str = "",
    ) -> str:
        """Emit ``sel`` in the same KoreanLegacy layout as the top-level
        statement, indented one keyword-column-width deeper, and surrounded
        by parens. Each inner line is prefixed with 7 spaces so the inner
        ``SELECT`` lands directly under the caller's keyword (``  FROM (``,
        ``      , ``, …) — visually nested without breaking column counts.
        """
        inner = self._emit_select(sel)
        indent = " " * (self.style.keyword_col_width + 1)  # 7 spaces
        indented = "\n".join(indent + ln for ln in inner.split("\n"))
        suffix = f" {alias}" if alias else ""
        return f"(\n{indented}\n{indent})" + suffix

    def _col_comment(self, node, table_hint: Optional[exp.Expression] = None) -> str:
        """Column 에 해당하는 한글 주석 반환 (없으면 빈 문자열)."""
        if not self.style.emit_column_comments or not self.ko:
            return ""
        col_name = ""
        tbl_name = ""
        if isinstance(node, exp.Column):
            col_name = (node.name or "").upper()
            tbl_name = (node.table or "").upper()
        elif isinstance(node, exp.Alias):
            inner = node.this
            if isinstance(inner, exp.Column):
                col_name = (inner.name or "").upper()
                tbl_name = (inner.table or "").upper()
        elif isinstance(node, exp.Identifier):
            col_name = (node.name or "").upper()
        else:
            return ""
        if not col_name:
            return ""
        # Qualified lookup 먼저, 그 다음 bare
        if tbl_name:
            qualified = f"{tbl_name}.{col_name}"
            if qualified in self.ko:
                return self.ko[qualified]
        if isinstance(table_hint, exp.Table):
            t = (table_hint.name or "").upper()
            if t:
                q = f"{t}.{col_name}"
                if q in self.ko:
                    return self.ko[q]
        return self.ko.get(col_name, "")

    def _table_comment(self, node) -> str:
        """Table 에 해당하는 ``T:한글`` prefix 주석 반환."""
        if not self.style.emit_column_comments or not self.ko:
            return ""
        if isinstance(node, exp.Table):
            t = (node.name or "").upper()
            if t and t in self.ko:
                return f"{self.style.table_comment_prefix}{self.ko[t]}"
        return ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalise_lookup(ko_lookup: Dict[str, str]) -> Dict[str, str]:
    """Upper-case keys; keep both ``TABLE.COL`` and ``COL`` forms."""
    out: Dict[str, str] = {}
    for k, v in (ko_lookup or {}).items():
        if not v:
            continue
        ku = k.upper()
        out[ku] = v
        if "." in ku:
            out.setdefault(ku.rsplit(".", 1)[1], v)
    return out


def _flatten_and(node: exp.Expression):
    """Recursively flatten AND chain into a list of atoms."""
    if isinstance(node, exp.And):
        yield from _flatten_and(node.this)
        yield from _flatten_and(node.args.get("expression"))
    else:
        yield node


# ----------------------------------------------------------------------------
# Simple ``<column> <op> <expr>`` predicate classifier (used by `=` alignment)
# ----------------------------------------------------------------------------


@dataclass
class _SimpleCompare:
    lhs: exp.Expression
    op: str
    rhs: exp.Expression


# sqlglot binary comparison node → operator text. Oracle uses ``<>`` for NEQ
# in legacy code; sqlglot emits whichever the input had, so we mirror.
_COMPARE_OPS: Dict[type, str] = {
    exp.EQ:  "=",
    exp.NEQ: "<>",
    exp.GT:  ">",
    exp.LT:  "<",
    exp.GTE: ">=",
    exp.LTE: "<=",
}


def _classify_simple_compare(node: exp.Expression):
    """Return ``_SimpleCompare`` when ``node`` is exactly ``<col> <op> <expr>``
    with a Column / Identifier on the LHS (so it has a meaningful "name"
    width to align on), else ``None``.

    The RHS can be anything — literal, parameter placeholder, function call.
    Only the LHS shape matters for alignment.
    """
    op = _COMPARE_OPS.get(type(node))
    if op is None:
        return None
    lhs = node.this
    if not isinstance(lhs, (exp.Column, exp.Identifier, exp.Dot)):
        return None
    rhs = node.args.get("expression")
    if rhs is None:
        return None
    return _SimpleCompare(lhs=lhs, op=op, rhs=rhs)


def _from_sources(frm: exp.From) -> List[exp.Expression]:
    """exp.From 아래의 소스 리스트 추출.

    Oracle 의 comma-FROM 은 ``From.expressions`` 에 여러 Table 이 들어갈 수
    있고, ANSI JOIN 은 exp.From.this 단일. 양 쪽 모두 커버.
    """
    exps = frm.args.get("expressions")
    if exps:
        return list(exps)
    single = frm.this
    if single is not None:
        return [single]
    return []


def _strip_comment_braces(raw: str) -> str:
    """Return comment text without ``/* ... */`` wrap (for width calculation)."""
    s = raw.strip()
    if s.startswith("/*") and s.endswith("*/"):
        return s[2:-2].strip()
    return s
