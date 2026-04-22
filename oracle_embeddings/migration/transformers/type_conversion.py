"""Wrap type-converted columns with ``transform.read/write/where`` templates.

Context rules (spec §4.2 정확한 해석):

    - UPDATE SET col = RHS
        ``col``   → 단순 rename (wrap 안 함)
        ``RHS``   → ``transform.write`` 로 wrap (fallback: ``read``)
        **중요**: write 템플릿을 LHS 컬럼에 씌우면 문법 에러가 난다 →
        ``UPDATE T SET TO_CHAR(NEW, 'YYYYMMDD') = #{dt}`` 는 invalid SQL.
    - INSERT INTO t (col, ...) VALUES (val, ...)
        ``col``   → 컬럼 리스트에서 rename
        ``val``   → 동일 index 의 VALUES 튜플에 write 템플릿 wrap
        INSERT ... SELECT subquery 형태는 위치 매칭 불가 → warning + skip
    - WHERE / JOIN ON predicate
        ``col`` → ``transform.where`` 로 wrap (fallback: ``read``)
    - SELECT projection 등 나머지
        ``col`` → ``transform.read`` 로 wrap

Rename 만 하고 wrap 을 못 했을 때는 downstream Stage A 가 타입 불일치를
잡도록 놔둔다. wrap 시도 자체가 parse fail 했으면 warning 으로 surface.
"""
from __future__ import annotations

from typing import Dict, List, Set, Tuple

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from ..mapping_model import ChangeItem, ColumnRef
from .base import RewriteContext, Transformer, TransformerResult, stmt_tables
from .column_rename import _set_column_name


class TypeConversionTransformer(Transformer):
    name = "TypeConversion"

    def apply(
        self, tree: exp.Expression, context: RewriteContext
    ) -> TransformerResult:
        mapping = context.mapping
        alias_map = context.alias_map
        tables_in_stmt = stmt_tables(alias_map)

        accum: Dict[Tuple[str, str], int] = {}
        warnings: List[str] = []
        # Columns (by object id) already handled by Pass A (UPDATE SET LHS).
        # Pass C 에서 다시 건드리지 않도록 마킹.
        consumed: Set[int] = set()

        # ── Pass A — UPDATE SET: rename LHS col, wrap RHS value with write ──
        for update in tree.find_all(exp.Update):
            set_list = update.args.get("expressions") or []
            for eq in set_list:
                if not isinstance(eq, exp.EQ):
                    continue
                col = eq.this  # left-hand side
                if not isinstance(col, exp.Column):
                    continue
                col_name = col.name
                if not col_name:
                    continue
                source = self._resolve_source(
                    col, col_name, alias_map, tables_in_stmt, mapping
                )
                if source is None:
                    continue
                cm = mapping.find_column(source, col_name)
                if cm is None or cm.kind != "type_convert":
                    continue
                to_be = cm.to_be
                if not isinstance(to_be, ColumnRef):
                    continue

                _set_column_name(col, to_be.column)
                consumed.add(id(col))

                template = _pick_write_template(cm.transform)
                if template:
                    rhs = eq.args.get("expression")
                    if rhs is not None:
                        _wrap_value(rhs, template, warnings)

                _record_change(accum, source, col_name, to_be)

        # ── Pass B — INSERT column list + matching VALUES tuple values ──
        for insert in tree.find_all(exp.Insert):
            schema = insert.this
            if not isinstance(schema, exp.Schema):
                continue
            host = schema.this
            if not isinstance(host, exp.Table):
                continue
            source_table = alias_map.get(host.name.upper())
            if source_table is None:
                continue

            # sqlglot 은 ``insert.expression`` 에 값 소스를 둔다.
            # VALUES literal → exp.Values, SELECT subquery → exp.Select.
            value_source = insert.args.get("expression")
            values_node = (
                value_source if isinstance(value_source, exp.Values) else None
            )

            for i, ident in enumerate(list(schema.expressions)):
                if not isinstance(ident, exp.Identifier):
                    continue
                col_name = ident.name
                cm = mapping.find_column(source_table, col_name)
                if cm is None or cm.kind != "type_convert":
                    continue
                to_be = cm.to_be
                if not isinstance(to_be, ColumnRef):
                    continue

                # Rename the identifier in the column list (header).
                schema.expressions[i] = exp.to_identifier(
                    to_be.column,
                    quoted=bool(getattr(ident, "quoted", False)),
                )

                template = _pick_write_template(cm.transform)
                if template:
                    if values_node is None:
                        warnings.append(
                            f"INSERT INTO {host.name} column "
                            f"'{col_name}' is type_convert but the VALUES "
                            f"source is a SELECT subquery; positional wrap "
                            f"is not supported — column renamed but RHS not "
                            f"wrapped."
                        )
                    else:
                        for tup in values_node.expressions:
                            if not isinstance(tup, exp.Tuple):
                                continue
                            tup_vals = tup.expressions
                            if i >= len(tup_vals):
                                continue
                            _wrap_value(tup_vals[i], template, warnings)

                _record_change(accum, source_table, col_name, to_be)

        # ── Pass C — all other column occurrences (SELECT, WHERE, JOIN, ...) ──
        for col in list(tree.find_all(exp.Column)):
            if id(col) in consumed:
                continue
            col_name = col.name
            if not col_name:
                continue
            source = self._resolve_source(
                col, col_name, alias_map, tables_in_stmt, mapping
            )
            if source is None:
                continue
            cm = mapping.find_column(source, col_name)
            if cm is None or cm.kind != "type_convert":
                continue
            to_be = cm.to_be
            if not isinstance(to_be, ColumnRef):
                continue

            _set_column_name(col, to_be.column)

            ctx = _classify_context(col)
            template = _pick_template(cm.transform, ctx)
            if template:
                replaced = _wrap_with_template(col, to_be, template, warnings)
                if not replaced:
                    continue
            _record_change(accum, source, col_name, to_be)

        changes = [
            ChangeItem(
                kind="type_wrap",
                as_is=as_is,
                to_be=to_be,
                count=count,
                transformer=self.name,
            )
            for (as_is, to_be), count in sorted(accum.items())
        ]
        return TransformerResult(
            tree=tree, changes=changes, needs_llm=False, warnings=warnings
        )

    def _resolve_source(
        self, col, col_name, alias_map, tables_in_stmt, mapping
    ):
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


def _classify_context(col: exp.Column) -> str:
    """Return ``'where'`` or ``'read'``.

    Pass C 에서만 호출되므로 'write' 는 제거. UPDATE SET LHS / INSERT
    column list 는 이미 Pass A/B 가 consume 했기 때문이다. 만약 어쩌다
    여기 다시 오더라도 'read' fallback 이 문법적으로 안전하다.
    """
    if col.find_ancestor(exp.Where):
        return "where"
    join = col.find_ancestor(exp.Join)
    if join is not None and col.find_ancestor(exp.Condition) is not None:
        return "where"
    return "read"


def _pick_template(transform, ctx: str):
    if transform is None:
        return None
    if ctx == "where":
        return transform.where or transform.read
    return transform.read


def _pick_write_template(transform):
    """WRITE 컨텍스트 전용 fallback: write → read."""
    if transform is None:
        return None
    return transform.write or transform.read


def _record_change(accum, source: str, col_name: str, to_be: ColumnRef):
    key = (
        f"{source}.{col_name.upper()}",
        f"{to_be.table.upper()}.{to_be.column.upper()}",
    )
    accum[key] = accum.get(key, 0) + 1


def _wrap_value(
    value_node: exp.Expression,
    template: str,
    warnings: List[str],
) -> bool:
    """RHS 값 / VALUES 튜플 값에 write 템플릿 wrap.

    원 표현식 SQL 을 ``{src}`` 자리에 끼워 넣어 재파싱. 파싱 실패 시
    warning 추가 후 원본 유지.
    """
    try:
        value_sql = value_node.sql(dialect="oracle")
    except Exception as exc:  # pragma: no cover - sqlglot 내부 예외 방어
        warnings.append(
            f"write template skipped — couldn't serialize RHS: {exc}"
        )
        return False
    wrapped_sql = template.replace("{src}", value_sql)
    try:
        wrapped = sqlglot.parse_one(wrapped_sql, dialect="oracle")
    except ParseError as exc:
        warnings.append(
            f"write template failed to parse ({wrapped_sql!r}): {exc}"
        )
        return False
    if isinstance(wrapped, exp.Select):
        warnings.append(
            f"write template produced a SELECT, expected expression "
            f"({wrapped_sql!r}); skipped"
        )
        return False
    value_node.replace(wrapped)
    return True


def _wrap_with_template(
    col: exp.Column,
    to_be_ref,
    template: str,
    warnings: List[str],
) -> bool:
    """Pass C (SELECT/WHERE/JOIN) 전용: 컬럼 자체를 wrap."""
    q = col.table or to_be_ref.table
    src_text = f"{q}.{to_be_ref.column}" if q else to_be_ref.column
    expr_sql = template.replace("{src}", src_text)

    try:
        expr_tree = sqlglot.parse_one(expr_sql, dialect="oracle")
    except ParseError as exc:
        warnings.append(
            f"transform template failed to parse ({expr_sql!r}): {exc}"
        )
        return False
    if isinstance(expr_tree, exp.Select):
        warnings.append(
            f"transform template produced a SELECT, expected an expression "
            f"({expr_sql!r}); skipped"
        )
        return False

    col.replace(expr_tree)
    return True
