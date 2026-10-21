"""Conservative, position-based Oracle PL/SQL migration.

Parse embedded SQL, never regenerate the PL/SQL program. Only identifier spans
are edited; comments, literals, binds, INTO targets and procedural code survive.
Unsupported SQL is kept verbatim and reported for review. No database execution.
"""
from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass, field
from pathlib import Path

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import Scope, traverse_scope

from .mapping_model import ChangeItem, ColumnRef, Mapping


@dataclass
class Token:
    text: str
    start: int
    end: int

    @property
    def upper(self):
        return self.text.upper()


@dataclass
class PlsqlOutcome:
    text: str
    changes: list[ChangeItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    statements: int = 0


# Oracle q-quoted strings require a matching delimiter, not ordinary quote rules.
_LEX = re.compile(r"--[^\r\n]*|/\*[\s\S]*?\*/|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|[A-Za-z_\u0080-\uffff][\w$#]*|\s+|.")
_IDENT = re.compile(r'^(?:"(?:""|[^"])+"|[A-Za-z_\u0080-\uffff][\w$#]*)$')
_DML = {'SELECT', 'WITH', 'INSERT', 'UPDATE', 'DELETE', 'MERGE'}


def _tokens(text):
    tokens = []
    i = 0
    while i < len(text):
        q = re.match(r"(?i)(?:n?q)'(.)", text[i:])
        if q:
            opening = q.group(1)
            closing = {'[': ']', '(': ')', '{': '}', '<': '>'}.get(opening, opening)
            end = text.find(closing + "'", i + q.end())
            if end < 0:
                raise ValueError('unterminated Oracle q-quoted string')
            i = end + 2
            continue
        if text.startswith('/*', i) and text.find('*/', i + 2) < 0:
            raise ValueError('unterminated block comment')
        m = _LEX.match(text, i)
        value = m.group()
        if value == "'" or value == '"':
            raise ValueError('unterminated quoted string or identifier')
        if value.startswith('/*') and not value.endswith('*/'):
            raise ValueError('unterminated block comment')
        if value.startswith("'") or value.startswith('--') or value.startswith('/*') or value.isspace():
            i = m.end()
            continue
        tokens.append(Token(value, i, m.end()))
        i = m.end()
    return tokens


def _name(token):
    return token.text[1:-1].replace('""', '"') if token.text.startswith('"') else token.text


def _quoted(value, original):
    if original.startswith('"') and not value.startswith('"'):
        return '"' + value.replace('"', '""') + '"'
    return value


def _mask_systimestamp_precision(sql):
    """Shield a legacy precision call from sqlglot's zero-argument builtin.

    This is parser input only. Keep every source position and the original
    expression intact; the caller reports that Oracle syntax needs review.
    Quoted/package-qualified functions, literals and comments are untouched.
    """
    tokens = _tokens(sql)
    masked = list(sql)
    occurrences = []
    for i, token in enumerate(tokens[:-3]):
        if token.upper != 'SYSTIMESTAMP' or (i and tokens[i - 1].text in {'.', ':'}):
            continue
        opening, precision, closing = tokens[i + 1:i + 4]
        if (opening.text != '(' or closing.text != ')'
                or not re.fullmatch(r'[0-9]', precision.text)):
            continue
        # A same-length anonymous function bypasses the builtin parser without
        # moving the identifier metadata used by the source-span rewriter.
        masked[token.start] = 'X'
        occurrences.append((token.start, precision.text))
    return ''.join(masked), occurrences


def _parse_static_sql(sql):
    """Parse Oracle MERGE's DELETE WHERE without regenerating source SQL.

    sqlglot accepts the UPDATE/INSERT filters but not the DELETE filter after
    UPDATE. Parse that predicate separately using its original character positions,
    then attach it to the update so normal source/scope resolution still works.
    """
    tokens = _tokens(sql)
    if not tokens or tokens[0].upper != 'MERGE':
        return sqlglot.parse_one(sql, read='oracle')
    depth = 0
    delete_at = None
    matched_update = False
    end = len(sql)
    for i, token in enumerate(tokens):
        if token.text == '(':
            depth += 1
        elif token.text == ')':
            depth -= 1
        elif depth == 0:
            if token.upper == 'WHEN':
                following = [t.upper for t in tokens[i + 1:i + 4]]
                if following[:2] == ['MATCHED', 'THEN']:
                    matched_update = following[2:] == ['UPDATE']
                elif following == ['NOT', 'MATCHED', 'THEN']:
                    matched_update = False
            if token.upper == 'DELETE' and i + 1 < len(tokens) and tokens[i + 1].upper == 'WHERE':
                if not matched_update:
                    raise ValueError('MERGE DELETE filter must follow a matched UPDATE')
                if delete_at is not None:
                    raise ValueError('multiple MERGE DELETE filters require manual review')
                delete_at = i
            elif delete_at is not None and token.upper == 'WHEN':
                following = [t.upper for t in tokens[i + 1:i + 4]]
                if following[:2] == ['MATCHED', 'THEN'] or following == ['NOT', 'MATCHED', 'THEN']:
                    end = token.start
                    break
    if delete_at is None:
        return sqlglot.parse_one(sql, read='oracle')
    start = tokens[delete_at].start
    predicate = tokens[delete_at + 1].end
    # Whitespace masking retains the metadata offsets of every remaining name.
    masked = sql[:start] + ''.join('\n' if c == '\n' else ' ' for c in sql[start:end]) + sql[end:]
    tree = sqlglot.parse_one(masked, read='oracle')
    updates = [w.args.get('then') for w in tree.args['whens'].expressions
               if w.args.get('matched') and isinstance(w.args.get('then'), exp.Update)]
    if len(updates) != 1:
        raise ValueError('MERGE DELETE filter requires exactly one matched UPDATE')
    prefix = 'SELECT 1 WHERE'
    condition = sqlglot.parse_one(prefix + ' ' * (predicate - len(prefix)) + sql[predicate:end], read='oracle')
    if (not isinstance(condition, exp.Select) or condition.args.get('where') is None
            or any(value for key, value in condition.args.items()
                   if key not in {'expressions', 'where'})):
        raise ValueError('unsupported MERGE DELETE filter')
    updates[0].set('delete_where', condition.args['where'])
    return tree


def rewrite_plsql(text: str, mapping: Mapping) -> PlsqlOutcome:
    out = PlsqlOutcome(text)
    probe = sqlglot.parse_one('SELECT __PLSQL_POSITION__ FROM DUAL', read='oracle')
    if 'start' not in probe.expressions[0].this.meta:
        out.warnings.append('PL/SQL unchanged: sqlglot lacks identifier positions; install sqlglot>=30.23.0')
        return out
    edits: dict[tuple[int, int], str] = {}

    def warn(message, pos):
        line = text.count('\n', 0, pos) + 1
        message = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', message)
        message = ' '.join(message.splitlines())
        msg = f'line {line}: {message}'
        if msg not in out.warnings:
            out.warnings.append(msg)

    def edit(start, end, value, kind, old):
        if text[start:end] == value:
            return
        key = (start, end)
        if key in edits:
            return
        edits[key] = value
        out.changes.append(ChangeItem(kind, old.upper(), value, 1, 'PLSQLIdentifierRename'))

    try:
        tokens = _tokens(text)
    except ValueError as exc:
        out.warnings.append(str(exc))
        return out

    unit_has_begin = False
    pending_subprogram = None
    for token in tokens:
        line_start = text.rfind('\n', 0, token.start) + 1
        line_end = text.find('\n', token.end)
        if line_end < 0:
            line_end = len(text)
        if token.text == '/' and text[line_start:line_end].strip() == '/':
            unit_has_begin = False
            pending_subprogram = None
        elif token.upper in {'PROCEDURE', 'FUNCTION'}:
            if pending_subprogram == 'body':
                warn('nested subprogram needs lexical-scope review; entire PL/SQL file unchanged', token.start)
                return out
            pending_subprogram = 'header'
        elif token.upper in {'AS', 'IS'} and pending_subprogram:
            pending_subprogram = 'body'
        elif token.text == ';' and pending_subprogram == 'header':
            pending_subprogram = None
        elif token.upper == 'BEGIN':
            unit_has_begin = True
            pending_subprogram = None
        elif token.upper == 'DECLARE' and unit_has_begin:
            warn('nested DECLARE needs lexical-scope review; entire PL/SQL file unchanged', token.start)
            return out

    def table_target(name, pos):
        tm = mapping.find_table(name)
        if tm is None:
            return None
        if tm.type != 'rename' or not isinstance(tm.to_be, str):
            warn(f'table {name}: {tm.type} mapping requires manual review; unchanged', pos)
            return None
        return tm.to_be

    def column_target(table, column, pos):
        cm = mapping.find_column(table, column)
        if cm is None:
            return None
        if cm.kind != 'rename' or not isinstance(cm.to_be, ColumnRef):
            warn(f'column {table}.{column}: {cm.kind} mapping requires manual review; unchanged', pos)
            return None
        target_table = table_target(table, pos)
        if target_table is None or cm.to_be.table.upper() != target_table.upper():
            warn(f'column {table}.{column}: target table differs from table mapping; unchanged', pos)
            return None
        return cm.to_be

    def lookup_table(parts):
        qualified = '.'.join(parts)
        if mapping.find_table(qualified):
            return qualified
        return parts[-1]

    records = {}
    record_names = set()
    sql_regions = []
    # Keep similarly named records in different packaged subprograms separate.
    # Nested outer-scope captures are conservatively reported rather than guessed.
    program_starts = [t.start for t in tokens if t.upper in {'CREATE', 'PROCEDURE', 'FUNCTION'}]

    def program_at(pos):
        return bisect_right(program_starts, pos)

    # Recognize declaration-shaped names to avoid treating a local custom
    # record anchor (R.FIELD%TYPE) as a same-named database table. This is
    # deliberately only a shadowing guard, not a full PL/SQL type resolver.
    declared_names = set()
    reserved = _DML | {'TYPE', 'SUBTYPE', 'CURSOR', 'PROCEDURE', 'FUNCTION', 'RETURN',
                       'BEGIN', 'END', 'IF', 'THEN', 'ELSE', 'ELSIF', 'LOOP', 'FOR',
                       'WHILE', 'WHEN', 'EXCEPTION', 'RAISE', 'OPEN', 'CLOSE', 'FETCH',
                       'EXECUTE', 'CREATE', 'PACKAGE', 'PRAGMA', 'NULL', 'IS', 'AS'}
    for n in range(1, len(tokens) - 1):
        name, after = tokens[n:n + 2]
        if (tokens[n - 1].upper in {'DECLARE', 'AS', 'IS', ';', '(', ','}
            and _IDENT.match(name.text) and name.upper not in reserved
            and _IDENT.match(after.text) and after.upper not in reserved):
            declared_names.add((program_at(name.start), _name(name).upper()))

    def record_table(name, pos):
        key = (program_at(pos), name.upper())
        if key not in records and name.upper() in record_names:
            warn(f'record {name}: declaration is outside the current subprogram; field needs review', pos)
        return records.get(key)


    # Declaration anchors (also valid in package signatures / RETURN types).
    for i, tok in enumerate(tokens):
        if tok.text != '%' or i + 1 >= len(tokens) or tokens[i + 1].upper not in {'TYPE', 'ROWTYPE'}:
            continue
        j = i - 1
        if j < 0 or not _IDENT.match(tokens[j].text):
            continue
        chain = [tokens[j]]
        while j >= 2 and tokens[j - 1].text == '.' and _IDENT.match(tokens[j - 2].text):
            j -= 2
            chain.insert(0, tokens[j])
        rowtype = tokens[i + 1].upper == 'ROWTYPE'
        table_tokens = chain if rowtype else chain[:-1]
        if not table_tokens:  # local_variable%TYPE
            continue
        if any(t.text.startswith('"') and _name(t) != _name(t).upper() for t in chain):
            warn('case-sensitive quoted anchor requires manual review; unchanged', tok.start)
            continue
        # A locally declared record shadows an identically named DB table.
        # R.ID%TYPE must follow the record field, not a table named R.
        if not rowtype and len(table_tokens) == 1 and _name(table_tokens[0]).upper() in record_names:
            record = _name(table_tokens[0])
            source = record_table(record, tok.start)
            target = column_target(source, _name(chain[-1]), tok.start) if source else None
            if target:
                edit(chain[-1].start, chain[-1].end, _quoted(target.column, chain[-1].text), 'column', f'{source}.{_name(chain[-1])}')
            continue
        if (not rowtype and len(table_tokens) == 1
            and (program_at(tok.start), _name(table_tokens[0]).upper()) in declared_names):
            warn('local custom-record %TYPE anchor cannot be resolved safely; unchanged', tok.start)
            continue
        parts = [_name(t) for t in table_tokens]
        table = lookup_table(parts)
        target = table_target(table, tok.start)
        if not target:
            continue
        declaration = j - 1
        while declaration >= 0 and tokens[declaration].upper in {'IN', 'OUT', 'NOCOPY', 'CONSTANT'}:
            declaration -= 1
        if rowtype and declaration >= 0 and _IDENT.match(tokens[declaration].text) and tokens[declaration].upper != 'RETURN':
            record = _name(tokens[declaration]).upper()
            key = (program_at(tok.start), record)
            record_names.add(record)
            if key in records and records[key] != table:
                records[key] = None
                warn(f'record {record} has multiple declarations; field references need review', tok.start)
            else:
                records[key] = table
        first = table_tokens[0] if '.' in target or mapping.find_table('.'.join(parts)) else table_tokens[-1]
        if re.search(r'/\*|--', text[first.start:table_tokens[-1].end]):
            warn('qualified anchor contains an embedded comment; unchanged for manual review', tok.start)
            continue
        if not rowtype:
            column = _name(chain[-1])
            cm = column_target(table, column, tok.start)
            if cm is None:
                warn(f'%TYPE reference {table}.{column}: no safe column mapping; unchanged', tok.start)
                continue
            edit(chain[-1].start, chain[-1].end, _quoted(cm.column, chain[-1].text), 'column', f'{table}.{column}')
        first = table_tokens[0] if '.' in target or mapping.find_table('.'.join(parts)) else table_tokens[-1]
        edit(first.start, table_tokens[-1].end, _quoted(target, first.text) if '.' not in target else target, 'table', table)

    # Locate complete static SQL regions, including CURSOR ... IS SELECT,
    # OPEN ... FOR SELECT and FOR record IN (SELECT ...). Nested SQL stays
    # inside its enclosing region. PL/SQL and SQL*Plus delimiters stay outside.
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.upper == 'DBMS_SQL' and i + 2 < len(tokens) and tokens[i + 1].text == '.' and tokens[i + 2].upper == 'PARSE':
            warn('dynamic DBMS_SQL.PARSE is preserved; inspect manually', tok.start)
        if tok.upper in {'EXECUTE', 'OPEN'} and i + 1 < len(tokens):
            if tok.upper == 'EXECUTE' and tokens[i + 1].upper == 'IMMEDIATE':
                warn('dynamic SQL is preserved; inspect EXECUTE IMMEDIATE manually', tok.start)
            if tok.upper == 'OPEN':
                tail = tokens[i + 1:]
                before_end = next((k for k,t in enumerate(tail) if t.text == ';'), len(tail))
                head = tail[:before_end]
                for k,t in enumerate(head):
                    if t.upper == 'FOR' and (k + 1 == len(head) or head[k + 1].upper not in _DML):
                        warn('dynamic OPEN FOR is preserved; inspect manually', tok.start)
        if tok.upper not in _DML:
            i += 1
            continue
        depth = 0
        end = i
        while end < len(tokens):
            t = tokens[end]
            if t.text == ';' or (t.text == ')' and depth == 0):
                break
            if t.text == '(':
                depth += 1
            elif t.text == ')':
                depth -= 1
            end += 1
        if end == i:
            i += 1
            continue
        start_pos = tok.start
        end_pos = tokens[end].start if end < len(tokens) else len(text)
        sql = text[start_pos:end_pos]
        boundary = i - 1
        while boundary >= 0 and tokens[boundary].text not in {';', '/'}:
            boundary -= 1
        prefix = [t.upper for t in tokens[boundary + 1:i]]
        cursor_query = ('CURSOR' in prefix or 'OPEN' in prefix or
                        ('FOR' in prefix and 'IN' in prefix and prefix[-1:] == ['(']))
        sql_regions.append((start_pos, end_pos))
        change_start = len(out.changes)
        out.statements += 1
        try:
            parser_sql, timestamp_precisions = _mask_systimestamp_precision(sql)
            tree = _parse_static_sql(parser_sql)
            for position, precision in timestamp_precisions:
                warn(f'SYSTIMESTAMP({precision}) preserved: Oracle SYSTIMESTAMP takes no precision argument; '
                     'verify SQL syntax manually', start_pos + position)
            if not isinstance(tree, (exp.Select, exp.Union, exp.Intersect, exp.Except, exp.Insert, exp.Update, exp.Delete, exp.Merge)):
                raise ValueError('unsupported SQL structure')
            if any(ident.quoted and ident.name != ident.name.upper() for ident in tree.find_all(exp.Identifier)):
                raise ValueError('case-sensitive quoted identifiers require manual review')
            if any('@' in t.name for t in tree.find_all(exp.Table)):
                raise ValueError('DB link references require an explicit remote-object review')
            if any(join.args.get('method') == 'NATURAL' for join in tree.find_all(exp.Join)):
                raise ValueError('NATURAL JOIN may change its implicit join keys after renaming; review manually')
            # Scope analysis must precede any identifier edits. It resolves CTEs,
            # subqueries and correlated references without touching aliases.
            scopes = list(traverse_scope(tree))
            source_by_expr = {id(s.expression): s for s in scopes}
            table_nodes = {}
            for scope in scopes:
                for _, source in scope.selected_sources.values():
                    if isinstance(source, exp.Table):
                        table_nodes[id(source)] = source
            # DML targets / USING sources are not returned as SELECT scopes.
            for t in tree.find_all(exp.Table):
                if isinstance(t.parent, exp.Into):
                    continue
                ancestor = t.find_ancestor(exp.Select, exp.Union, exp.Intersect, exp.Except)
                if ancestor is None:
                    table_nodes[id(t)] = t

            def table_key(t):
                return lookup_table([x.name for x in t.parts])

            root_sources = {}
            for t in table_nodes.values():
                ancestor = t.find_ancestor(exp.Select)
                if ancestor is None:
                    root_sources[(t.alias_or_name or '').upper()] = t
                    root_sources.setdefault(t.name.upper(), t)
            for subquery in tree.find_all(exp.Subquery):
                if subquery.alias and subquery.find_ancestor(exp.Select) is None:
                    scope = source_by_expr.get(id(subquery.this))
                    if scope is not None:
                        root_sources[subquery.alias.upper()] = scope

            def sources_for(scope):
                return {name: source for name, (_, source) in scope.selected_sources.items()} if scope else root_sources

            def resolve(source, cname, seen=None):
                seen = set() if seen is None else seen
                if isinstance(source, exp.Table):
                    return table_key(source)
                if not isinstance(source, Scope) or id(source) in seen:
                    return None
                seen = seen | {id(source)}
                # Explicit CTE column lists and projection aliases are contracts;
                # callers continue using those names.
                if source.outer_columns:
                    return None
                if isinstance(source.expression, (exp.Union, exp.Intersect, exp.Except)):
                    return resolve(source_by_expr.get(id(source.expression.this)), cname, seen)
                for projection in source.expression.selects:
                    if projection.alias and projection.alias.upper() == cname.upper():
                        return None
                    if isinstance(projection, exp.Column) and projection.name.upper() == cname.upper():
                        return resolve_column(projection, source, seen)
                if any(p.is_star for p in source.expression.selects):
                    candidates = {resolve(s, cname, seen) for s in sources_for(source).values()}
                    candidates.discard(None)
                    matches = [t for t in candidates if mapping.find_column(t, cname)]
                    return matches[0] if len(matches) == 1 else None
                return None

            def resolve_column(col, scope, seen=None):
                sources = sources_for(scope)
                normalized = {k.upper(): v for k,v in sources.items()}
                if col.table:
                    source = normalized.get(col.table.upper())
                    if source is None and scope and scope.parent:
                        return resolve_column(col, scope.parent, seen)
                    if source is None and scope:
                        source = root_sources.get(col.table.upper())
                    return resolve(source, col.name, seen)
                candidates = {resolve(s, col.name, seen) for s in sources.values()}
                candidates.discard(None)
                matches = [t for t in candidates if mapping.find_column(t, col.name)]
                if len(matches) > 1:
                    warn(f'ambiguous column {col.name}; unchanged', start_pos)
                return matches[0] if len(matches) == 1 else None

            # USING identifiers are not Column AST nodes. Both sides must map
            # to the same physical name; otherwise keep this entire SQL intact.
            for join in tree.find_all(exp.Join):
                using = join.args.get('using') or []
                if not using:
                    continue
                scope = source_by_expr.get(id(join.find_ancestor(exp.Select)))
                sources = sources_for(scope)
                for ident in using:
                    candidates = {resolve(source, ident.name) for source in sources.values()}
                    targets = set()
                    for table in candidates:
                        cm = mapping.find_column(table, ident.name) if table else None
                        target = column_target(table, ident.name, start_pos) if cm else None
                        if cm and target is None:
                            raise ValueError(f'USING({ident.name}) has a non-rename mapping')
                        targets.add(target.column if target else ident.name)
                    if len({name.upper() for name in targets}) > 1:
                        raise ValueError(f'USING({ident.name}) maps to different names on the joined tables; rewrite as ON')
                    if targets and 'start' in ident.meta:
                        a,b = start_pos+ident.meta['start'], start_pos+ident.meta['end']+1
                        edit(a,b,_quoted(next(iter(targets)),text[a:b]),'column',ident.name)

            for col in tree.find_all(exp.Column):
                # INTO is represented as a Table by sqlglot; binds/variables
                # without a mapped SQL source cannot resolve to a column.
                if isinstance(col.parent, exp.Parameter) or col.find_ancestor(exp.Into):
                    continue
                ancestor = col.find_ancestor(exp.Select, exp.Union, exp.Intersect, exp.Except)
                scope = source_by_expr.get(id(ancestor)) if ancestor is not None else None
                if not col.table and scope and col.find_ancestor(exp.Order):
                    if any(p.alias and p.alias.upper() == col.name.upper() for p in scope.expression.selects):
                        continue
                merge_target = False
                if isinstance(tree, exp.Merge) and not col.table and ancestor is None:
                    update = col.find_ancestor(exp.Update)
                    insert = col.find_ancestor(exp.Insert)
                    merge_target = (
                        update is not None and isinstance(col.parent, exp.EQ)
                        and col.parent in update.expressions and col.arg_key == 'this'
                        or update is not None and update.args.get('delete_where') is not None
                        and col.find_ancestor(exp.Where) is update.args['delete_where']
                        or insert is not None and col.parent is insert.this
                    )
                table = table_key(tree.this) if merge_target else resolve_column(col, scope)
                if table is None and col.table:
                    sources = sources_for(scope)
                    if col.table.upper() not in {k.upper() for k in sources}:
                        table = record_table(col.table, start_pos)
                if table is None:
                    continue
                target = column_target(table, col.name, start_pos)
                if target and isinstance(col.this, exp.Identifier):
                    ident = col.this
                    if 'start' in ident.meta:
                        a, b = start_pos + ident.meta['start'], start_pos + ident.meta['end'] + 1
                        edit(a, b, _quoted(target.column, text[a:b]), 'column', f'{table}.{col.name}')
                # Qualifiers must follow table renames even for unchanged columns.
                # A PL/SQL record's variable name and explicit SQL aliases survive.
                if col.table and col.table.upper() == table.split('.')[-1].upper():
                    visible_sources = list(sources_for(scope).values())
                    parent = scope.parent if scope else None
                    while parent is not None:
                        visible_sources.extend(sources_for(parent).values())
                        parent = parent.parent
                    visible_sources.extend(root_sources.values())
                    physical = any(isinstance(source, exp.Table) and source.name.upper() == col.table.upper() for source in visible_sources)
                    alias_used = any(isinstance(source, exp.Table) and source.alias and source.alias.upper() == col.table.upper() for source in visible_sources)
                    new_table = table_target(table,start_pos) if physical and not alias_used else None
                    ident = col.args.get('table')
                    if new_table and isinstance(ident,exp.Identifier) and 'start' in ident.meta:
                        a,b=start_pos+ident.meta['start'],start_pos+ident.meta['end']+1
                        edit(a,b,_quoted(new_table.split('.')[-1],text[a:b]),'table',table)
                        db=col.args.get('db')
                        if '.' in new_table and isinstance(db,exp.Identifier) and 'start' in db.meta:
                            a,b=start_pos+db.meta['start'],start_pos+db.meta['end']+1
                            edit(a,b,new_table.rsplit('.',1)[0],'table',table)
            # INSERT target column list consists of Identifiers, not Columns.
            for schema in tree.find_all(exp.Schema):
                if not isinstance(schema.this, exp.Table):
                    continue
                table = table_key(schema.this)
                for ident in schema.expressions:
                    if not isinstance(ident, exp.Identifier) or 'start' not in ident.meta:
                        continue
                    target = column_target(table, ident.name, start_pos)
                    if target:
                        a,b=start_pos+ident.meta['start'],start_pos+ident.meta['end']+1
                        edit(a,b,_quoted(target.column,text[a:b]),'column',f'{table}.{ident.name}')
            # Cursor record fields are part of the procedural interface. Keep
            # their original labels via explicit aliases when a projection's
            # physical column changes (rec.ID / cursor%ROWTYPE stay valid).
            if cursor_query and isinstance(tree, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
                for projection in tree.selects:
                    if projection.is_star:
                        warn('cursor SELECT * changes row fields; review record field references manually', start_pos)
                    if not isinstance(projection, exp.Column) or projection.is_star:
                        continue
                    ident = projection.this
                    if isinstance(ident, exp.Identifier) and 'end' in ident.meta:
                        a,b = start_pos+ident.meta['start'], start_pos+ident.meta['end']+1
                        if (a,b) in edits:
                            edit(b,b,' AS ' + text[a:b],'column',projection.name)
            # An INTO record field follows its declared table%ROWTYPE, whereas
            # scalar INTO targets must remain untouched.
            for into in tree.find_all(exp.Into):
                target = into.this
                if isinstance(target, exp.Table) and target.db:
                    table = record_table(target.db, start_pos)
                    ident = target.this
                    cm = column_target(table,target.name,start_pos) if table else None
                    if cm and isinstance(ident,exp.Identifier) and 'start' in ident.meta:
                        a,b=start_pos+ident.meta['start'],start_pos+ident.meta['end']+1
                        edit(a,b,_quoted(cm.column,text[a:b]),'column',f'{table}.{target.name}')
            for t in table_nodes.values():
                table = table_key(t)
                target = table_target(table, start_pos)
                if not target or not isinstance(t.this, exp.Identifier) or 'start' not in t.this.meta:
                    continue
                ident = t.this
                first = t.parts[0] if '.' in target or mapping.find_table('.'.join(x.name for x in t.parts)) else ident
                a,b = start_pos+first.meta['start'],start_pos+ident.meta['end']+1
                if re.search(r'/\*|--', text[a:b]):
                    raise ValueError('qualified table name contains an embedded comment; review manually')
                edit(a,b,_quoted(target,text[a:b]) if '.' not in target else target,'table',table)
        except Exception as exc:
            # Drop every edit from this region if parsing/resolution failed.
            for key in list(edits):
                if start_pos <= key[0] < end_pos:
                    del edits[key]
            del out.changes[change_start:]
            warn(f'SQL preserved for manual review: {str(exc)[:180]}', start_pos)
        i = max(end, i + 1)

    # Fields of explicitly table-anchored records outside SQL also follow the
    # new row type. SQL aliases take precedence inside SQL (handled above).
    for k in range(len(tokens) - 2):
        rec, dot, col = tokens[k:k + 3]
        table = record_table(_name(rec), rec.start) if _name(rec).upper() in record_names and dot.text == '.' else None
        if not table or dot.text != '.' or not _IDENT.match(col.text):
            continue
        if any(a <= rec.start < b for a,b in sql_regions):
            continue
        target = column_target(table, _name(col), col.start)
        if target:
            edit(col.start,col.end,_quoted(target.column,col.text),'column',f'{table}.{_name(col)}')

    for (a,b), value in sorted(edits.items(), reverse=True):
        out.text = out.text[:a] + value + out.text[b:]
    return out


def read_sql_file(path: Path):
    """Read Oracle exports without silently replacing undecodable characters."""
    raw = path.read_bytes()
    for encoding in ('utf-8-sig', 'cp949'):
        try:
            text = raw.decode(encoding)
            return text, ('utf-8-sig' if raw.startswith(b'\xef\xbb\xbf') else 'utf-8') if encoding == 'utf-8-sig' else encoding
        except UnicodeDecodeError:
            continue
    raise ValueError(f'{path}: expected UTF-8 or CP949 SQL file')
