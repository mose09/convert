import logging
import os
import re
import xml.etree.ElementTree as ET

logger = logging.getLogger(__name__)


def _read_file_safe(filepath: str, limit: int = None) -> str:
    """Read a file trying multiple encodings."""
    for encoding in ("utf-8", "euc-kr", "cp949", "latin-1"):
        try:
            with open(filepath, "r", encoding=encoding) as f:
                return f.read(limit) if limit else f.read()
        except (UnicodeDecodeError, UnicodeError):
            continue
    # Final fallback
    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        return f.read(limit) if limit else f.read()


# Directories we always skip while scanning for mapper XML. We only prune
# hidden tool/VCS directories and ``node_modules`` — **never** build-output
# names like ``target`` / ``build`` / ``bin`` / ``out`` / ``dist``, because
# real monorepo subprojects sometimes happen to have folders with those
# exact names, and any XML that isn't a real mapper is already filtered
# out later by ``_is_sql_mapper``. Keeping the skip list minimal avoids
# the "내 하위 프로젝트 mapper 가 안 잡힘" regression.
_MYBATIS_SKIP_DIRS = {".git", ".gradle", ".idea", ".svn", ".hg",
                      ".next", "node_modules"}


def scan_mybatis_dir(base_dir: str) -> list[str]:
    """Find all MyBatis/iBatis mapper XML files recursively.

    Skips typical build-output and VCS directories so that a project-root
    path can be passed safely (the legacy analyzer does this). Each
    candidate XML is still validated via ``_is_sql_mapper`` to filter out
    pom.xml, config files, and other non-mapper XML.
    """
    xml_files = []
    for root, dirs, files in os.walk(base_dir):
        dirs[:] = [d for d in dirs if d.lower() not in _MYBATIS_SKIP_DIRS]
        for f in files:
            if f.endswith(".xml"):
                filepath = os.path.join(root, f)
                if _is_sql_mapper(filepath):
                    xml_files.append(filepath)
    logger.info("Found %d mapper files (MyBatis + iBatis) in %s", len(xml_files), base_dir)
    return xml_files


def _is_sql_mapper(filepath: str) -> bool:
    """Check if an XML file is a MyBatis or iBatis mapper."""
    try:
        head = _read_file_safe(filepath, limit=5000)
        head_lower = head.lower()

        has_sql_tags = ("<select" in head_lower or "<insert" in head_lower or
                        "<update" in head_lower or "<delete" in head_lower)

        # MyBatis: <mapper namespace="...">
        # iBatis: <sqlMap namespace="..."> or <sqlMap>
        has_mapper_root = ("mapper" in head_lower or "sqlmap" in head_lower or
                           "sql-map" in head_lower)

        return has_sql_tags or (has_mapper_root and "namespace" in head_lower)
    except Exception:
        return False


def parse_mapper_file(filepath: str) -> list[dict]:
    """Parse a MyBatis or iBatis mapper XML and extract SQL statements."""
    statements = []
    try:
        # Read and strip DOCTYPE to avoid DTD resolution errors (common in iBatis)
        xml_content = _read_file_safe(filepath)
        xml_content = re.sub(r'<!DOCTYPE[^>]*>', '', xml_content)
        # Remove XML comments <!-- ... -->
        xml_content = re.sub(r'<!--.*?-->', '', xml_content, flags=re.DOTALL)
        root = ET.fromstring(xml_content)
    except ET.ParseError:
        return _parse_mapper_fallback(filepath)

    namespace = root.attrib.get("namespace", "")
    mapper_name = os.path.basename(filepath)

    # MyBatis: select/insert/update/delete
    # iBatis: also uses same tags, but may have additional ones like statement, procedure
    for tag in ("select", "insert", "update", "delete", "statement", "procedure"):
        for elem in root.iter(tag):
            stmt_id = elem.attrib.get("id", "unknown")
            sql_text = _extract_sql_text(elem)
            if sql_text.strip():
                statements.append({
                    "mapper": mapper_name,
                    "mapper_path": filepath,
                    "namespace": namespace,
                    "id": stmt_id,
                    "type": tag.upper(),
                    "sql": sql_text,
                })

    return statements


def _parse_mapper_fallback(filepath: str) -> list[dict]:
    """Fallback parser using regex for malformed XML."""
    statements = []
    mapper_name = os.path.basename(filepath)

    try:
        content = _read_file_safe(filepath)
    except Exception as e:
        logger.warning("Cannot read %s: %s", filepath, e)
        return []

    # Remove XML comments <!-- ... -->
    content = re.sub(r'<!--.*?-->', '', content, flags=re.DOTALL)

    namespace_match = re.search(r'namespace\s*=\s*["\']([^"\']+)', content)
    namespace = namespace_match.group(1) if namespace_match else ""

    pattern = r'<(select|insert|update|delete|statement|procedure)\s+[^>]*id\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</\1>'
    for match in re.finditer(pattern, content, re.DOTALL | re.IGNORECASE):
        tag, stmt_id, sql_body = match.groups()
        sql_text = _clean_sql(sql_body)
        if sql_text.strip():
            statements.append({
                "mapper": mapper_name,
                "mapper_path": filepath,
                "namespace": namespace,
                "id": stmt_id,
                "type": tag.upper(),
                "sql": sql_text,
            })

    return statements


def _extract_sql_text(elem) -> str:
    """Extract full SQL text from an XML element, recursing all nested levels."""
    parts = []
    if elem.text:
        parts.append(elem.text)
    for child in elem:
        # Recursively extract from all nested dynamic SQL elements
        # (<if>, <where>, <foreach>, <choose>, <when>, <otherwise>, <trim>, <set>, <include>, etc.)
        parts.append(_extract_sql_text(child))
        if child.tail:
            parts.append(child.tail)
    return _clean_sql(" ".join(parts))


def _clean_sql(sql: str) -> str:
    """Clean SQL text by removing comments, MyBatis parameters and extra whitespace."""
    # Remove block comments /* ... */
    sql = re.sub(r'/\*.*?\*/', ' ', sql, flags=re.DOTALL)
    # Remove line comments -- ...
    sql = re.sub(r'--[^\n]*', ' ', sql)
    # Remove #{...} and ${...} parameters but keep structure
    sql = re.sub(r'#\{[^}]*\}', '?', sql)
    sql = re.sub(r'\$\{[^}]*\}', '?', sql)
    # Remove CDATA markers
    sql = re.sub(r'<!\[CDATA\[', '', sql)
    sql = re.sub(r'\]\]>', '', sql)
    # Remove XML tags that might remain
    sql = re.sub(r'<[^>]+>', ' ', sql)
    # Normalize whitespace
    sql = re.sub(r'\s+', ' ', sql).strip()
    return sql


def extract_joins(statements: list[dict]) -> list[dict]:
    """Extract JOIN relationships from SQL statements.

    Joins are de-duplicated by ``(table1, column1, table2, column2)``
    but **all** contributing statements are recorded: the first hit
    populates ``source_mapper`` / ``source_id`` (back-compat) and every
    subsequent hit is appended to ``sources`` (``mapper#id`` list) and
    ``source_stmts`` (full dicts). This lets downstream reports tell
    the user WHICH statement the relationship actually came from even
    when several queries share the same column pair.
    """
    joins = []
    seen = {}

    for stmt in statements:
        sql = stmt["sql"].upper()
        found = _parse_joins_from_sql(sql)
        src_key = f"{stmt['mapper']}#{stmt['id']}"
        for join in found:
            key = (join["table1"], join["column1"], join["table2"], join["column2"])
            reverse_key = (join["table2"], join["column2"], join["table1"], join["column1"])
            existing = seen.get(key) or seen.get(reverse_key)
            if existing is None:
                join["source_mapper"] = stmt["mapper"]
                join["source_id"] = stmt["id"]
                join["source_type"] = stmt["type"]
                join["sources"] = [src_key]
                seen[key] = join
                joins.append(join)
            else:
                if src_key not in existing["sources"]:
                    existing["sources"].append(src_key)

    logger.info("Extracted %d unique join relationships", len(joins))
    return joins


SQL_KEYWORDS = {
    "ON", "WHERE", "SET", "AND", "OR", "LEFT", "RIGHT", "INNER", "OUTER",
    "CROSS", "FULL", "JOIN", "SELECT", "INTO", "VALUES", "FROM", "AS",
    "NOT", "NULL", "IN", "EXISTS", "BETWEEN", "LIKE", "CASE", "WHEN",
    "THEN", "ELSE", "END", "GROUP", "ORDER", "BY", "HAVING", "UNION",
    "ALL", "DISTINCT", "INSERT", "UPDATE", "DELETE", "CREATE", "ALTER",
    "DROP", "TABLE", "INDEX", "VIEW", "IS", "ASC", "DESC", "LIMIT",
    "OFFSET", "FETCH", "FIRST", "NEXT", "ROWS", "ONLY", "WITH",
    "RECURSIVE", "MERGE", "USING", "MATCHED", "DUAL", "ROWNUM",
    "SYSDATE", "SYSTIMESTAMP", "NVL", "NVL2", "DECODE", "SUBSTR",
    "TRIM", "UPPER", "LOWER", "COUNT", "SUM", "AVG", "MAX", "MIN",
    "OVER", "PARTITION", "ROW_NUMBER", "RANK", "DENSE_RANK",
}


def _parse_joins_from_sql(sql: str) -> list[dict]:
    """Parse JOIN conditions from SQL to extract table relationships."""
    results = []

    # Step 1: Build alias map from SQL syntax
    alias_map = {}
    known_aliases = set()  # alias로 확인된 이름들

    # Step 1a: Find CTE names: WITH name AS (...), name2 AS (...)
    cte_pattern = r'(?:WITH|,)\s+(\w+)\s+AS\s*\('
    for match in re.finditer(cte_pattern, sql):
        cte_name = match.group(1).upper()
        if cte_name not in SQL_KEYWORDS:
            known_aliases.add(cte_name)

    # Step 1b: Find subquery aliases: (...) alias or (...) AS alias
    subquery_alias_pattern = r'\)\s*(?:AS\s+)?(\w+)'
    for match in re.finditer(subquery_alias_pattern, sql):
        alias = match.group(1).upper()
        if alias not in SQL_KEYWORDS:
            known_aliases.add(alias)

    # Step 1b: FROM/JOIN table_name alias (optional AS)
    table_alias_pattern = r'(?:FROM|JOIN)\s+(\w+)\s+(?:AS\s+)?(\w+)'
    for match in re.finditer(table_alias_pattern, sql):
        table, alias = match.groups()
        if alias.upper() not in SQL_KEYWORDS:
            known_aliases.add(alias.upper())
            # Only map if the table part is not a keyword and not already an alias
            if table.upper() not in SQL_KEYWORDS and table.upper() not in known_aliases:
                alias_map[alias.upper()] = table.upper()

    # Step 1c: FROM/JOIN table_name (no alias)
    no_alias_pattern = r'(?:FROM|JOIN)\s+(\w+)(?:\s*(?:WHERE|ON|,|\)|$))'
    for match in re.finditer(no_alias_pattern, sql):
        table = match.group(1).upper()
        if table not in SQL_KEYWORDS and table not in known_aliases:
            alias_map[table] = table

    # Step 1d: Oracle comma-style FROM clause:
    #     FROM TB_A a, TB_B b, TB_C c
    # The patterns above only capture the FIRST entry (``FROM TB_A a``).
    # We have to walk the full FROM clause, strip parenthesised
    # subqueries, split on commas, and register each ``table [alias]``
    # pair in the alias map. This is what makes Oracle ``(+)`` outer-join
    # style queries resolvable.
    from_clause_re = re.compile(
        r'\bFROM\b(.*?)(?=\bWHERE\b|\bJOIN\b|\bGROUP\b|\bHAVING\b|\bORDER\b|\bCONNECT\b|\bSTART\b|$)',
        re.IGNORECASE | re.DOTALL,
    )
    for fm in from_clause_re.finditer(sql):
        body = fm.group(1)
        # Remove nested parenthesised subqueries — leave their content
        # out of the split so we don't mistake subquery columns for
        # tables.
        prev = None
        while prev != body:
            prev = body
            body = re.sub(r'\([^()]*\)', ' ', body)
        if "," not in body:
            continue
        for part in body.split(","):
            part = part.strip()
            if not part:
                continue
            tokens = part.split()
            if not tokens:
                continue
            table = tokens[0].upper()
            if table in SQL_KEYWORDS:
                continue
            alias = table  # default alias = table itself (no-alias form)
            if len(tokens) >= 2:
                # ``TB_X AS t`` or ``TB_X t``
                cand = tokens[-1].upper()
                if cand not in SQL_KEYWORDS:
                    alias = cand
            known_aliases.add(alias)
            if alias not in alias_map:
                alias_map[alias] = table
            if table not in alias_map:
                alias_map[table] = table

    # Parse join conditions: a.col = b.col
    # Allow the Oracle legacy outer-join marker ``(+)`` on either side,
    # e.g. ``a.col = b.col(+)``. We capture whether the marker is
    # present so _detect_join_type can report LEFT/RIGHT OUTER JOIN.
    on_pattern = (
        r'(\w+)\.(\w+)(?P<l_outer>\s*\(\+\))?\s*=\s*'
        r'(\w+)\.(\w+)(?P<r_outer>\s*\(\+\))?'
    )
    for match in re.finditer(on_pattern, sql):
        alias1, col1, alias2, col2 = match.group(1), match.group(2), match.group(4), match.group(5)
        left_outer = bool(match.group("l_outer"))
        right_outer = bool(match.group("r_outer"))
        table1 = alias_map.get(alias1.upper())
        table2 = alias_map.get(alias2.upper())

        # Skip if aliases not resolved, same table, or constant values (1=1)
        if not table1 or not table2 or table1 == table2:
            continue
        if col1.isdigit() or col2.isdigit():
            continue
        # Oracle ``(+)`` wins over positional detection. ``a.col(+) =
        # b.col`` means ``a`` is the optional side → RIGHT OUTER,
        # ``a.col = b.col(+)`` means ``b`` is the optional side →
        # LEFT OUTER (relative to the first column in our record).
        if left_outer and right_outer:
            join_type = "FULL OUTER JOIN"
        elif left_outer:
            join_type = "RIGHT OUTER JOIN (Oracle +)"
        elif right_outer:
            join_type = "LEFT OUTER JOIN (Oracle +)"
        else:
            join_type = _detect_join_type(sql, match.start())
        results.append({
            "table1": table1,
            "column1": col1.upper(),
            "table2": table2,
            "column2": col2.upper(),
            "join_type": join_type,
        })

    return results


def _detect_join_type(sql: str, pos: int) -> str:
    """Detect the type of JOIN from context (ANSI keywords only)."""
    prefix = sql[:pos].rstrip()
    if "LEFT" in prefix[-30:]:
        return "LEFT JOIN"
    elif "RIGHT" in prefix[-30:]:
        return "RIGHT JOIN"
    elif "FULL" in prefix[-30:]:
        return "FULL JOIN"
    elif "CROSS" in prefix[-30:]:
        return "CROSS JOIN"
    elif "WHERE" in prefix[-30:]:
        return "WHERE (implicit)"
    return "INNER JOIN"


def extract_table_usage(statements: list[dict]) -> dict[str, dict]:
    """Analyze which tables are used in which queries and how."""
    usage = {}

    for stmt in statements:
        sql = stmt["sql"].upper()

        # Extract real table names (not aliases) from FROM/JOIN clauses
        tables = set()
        aliases_in_stmt = set()

        # CTE names: WITH name AS (...)
        for match in re.finditer(r'(?:WITH|,)\s+(\w+)\s+AS\s*\(', sql):
            cte_name = match.group(1)
            if cte_name not in SQL_KEYWORDS:
                aliases_in_stmt.add(cte_name)

        # Subquery aliases: (...) d1
        for match in re.finditer(r'\)\s*(?:AS\s+)?(\w+)', sql):
            alias = match.group(1)
            if alias not in SQL_KEYWORDS:
                aliases_in_stmt.add(alias)

        # Table aliases: FROM TABLE t1
        for match in re.finditer(r'(?:FROM|JOIN)\s+(\w+)\s+(?:AS\s+)?(\w+)', sql):
            table, alias = match.groups()
            if alias not in SQL_KEYWORDS:
                aliases_in_stmt.add(alias)
                if table not in SQL_KEYWORDS and table not in aliases_in_stmt:
                    tables.add(table)

        # Tables without alias
        for match in re.finditer(r'(?:FROM|JOIN)\s+(\w+)(?:\s*(?:WHERE|ON|,|\)|$))', sql):
            table = match.group(1)
            if table not in SQL_KEYWORDS and table not in aliases_in_stmt:
                tables.add(table)

        # INSERT INTO table
        insert_match = re.search(r'INSERT\s+INTO\s+(\w+)', sql)
        if insert_match:
            tables.add(insert_match.group(1))

        # UPDATE table
        update_match = re.search(r'UPDATE\s+(\w+)', sql)
        if update_match:
            tables.add(update_match.group(1))

        # DELETE FROM table
        delete_match = re.search(r'DELETE\s+FROM\s+(\w+)', sql)
        if delete_match:
            tables.add(delete_match.group(1))

        # Oracle comma-style JOIN: FROM T1 [a1], T2 [a2], T3 [a3] [JOIN ... | WHERE | ...]
        # Strict pattern: require at least one comma and word-only table names
        comma_tables = []  # preserve order; idx 0 is main, rest are joins
        # Match: FROM TBL [alias] (, TBL [alias])+ before any non-word-comma content
        # First capture first table + optional alias
        strict_from = re.search(
            r'\bFROM\s+(\w+)(?:\s+(?:AS\s+)?(\w+))?((?:\s*,\s*\w+(?:\s+(?:AS\s+)?\w+)?)+)',
            sql, re.IGNORECASE,
        )
        if strict_from:
            first_table = strict_from.group(1)
            rest_clause = strict_from.group(3) or ""
            # first table
            if first_table and first_table.upper() not in SQL_KEYWORDS and first_table not in aliases_in_stmt:
                comma_tables.append(first_table)
                tables.add(first_table)
            # remaining tables in comma list
            for match in re.finditer(r',\s*(\w+)(?:\s+(?:AS\s+)?(\w+))?', rest_clause):
                tbl = match.group(1)
                alias = match.group(2)
                if tbl.upper() in SQL_KEYWORDS:
                    continue
                if tbl in aliases_in_stmt:
                    continue
                comma_tables.append(tbl)
                tables.add(tbl)
                if alias and alias.upper() not in SQL_KEYWORDS:
                    aliases_in_stmt.add(alias)

        # Identify main table (FROM 바로 뒤) vs join tables
        main_table = _extract_main_table(sql, aliases_in_stmt)
        join_tables = set()

        # ANSI JOIN: JOIN TABLE
        for match in re.finditer(r'JOIN\s+(\w+)', sql):
            jt = match.group(1)
            if jt not in SQL_KEYWORDS and jt not in aliases_in_stmt:
                join_tables.add(jt)

        # Comma-joined tables (2nd and beyond)
        for idx, tbl in enumerate(comma_tables):
            if idx > 0 and tbl != main_table:
                join_tables.add(tbl)

        for table in tables:
            if table not in usage:
                usage[table] = {
                    "select_count": 0, "insert_count": 0,
                    "update_count": 0, "delete_count": 0,
                    "as_main_count": 0, "as_join_count": 0,
                    "mappers": set(), "queries": [],
                }
            key = f"{stmt['type'].lower()}_count"
            if key in usage[table]:
                usage[table][key] += 1
            if table == main_table:
                usage[table]["as_main_count"] += 1
            if table in join_tables:
                usage[table]["as_join_count"] += 1
            usage[table]["mappers"].add(stmt["mapper"])
            usage[table]["queries"].append(f"{stmt['mapper']}#{stmt['id']}")

    # Convert sets to lists for serialization
    for table in usage:
        usage[table]["mappers"] = sorted(usage[table]["mappers"])

    return usage


def _extract_main_table(sql: str, aliases: set) -> str:
    """Extract the main table from SQL (first table after FROM, not after JOIN)."""
    # SELECT ... FROM main_table ...
    # INSERT INTO main_table ...
    # UPDATE main_table ...
    # DELETE FROM main_table ...

    # INSERT INTO
    m = re.search(r'INSERT\s+INTO\s+(\w+)', sql)
    if m and m.group(1) not in SQL_KEYWORDS:
        return m.group(1)

    # UPDATE
    m = re.search(r'UPDATE\s+(\w+)', sql)
    if m and m.group(1) not in SQL_KEYWORDS:
        return m.group(1)

    # DELETE FROM
    m = re.search(r'DELETE\s+FROM\s+(\w+)', sql)
    if m and m.group(1) not in SQL_KEYWORDS:
        return m.group(1)

    # SELECT ... FROM table (first FROM, not inside subquery)
    # Remove paren content to skip FROMs inside subqueries
    cleaned = sql
    while re.search(r'\([^()]*\)', cleaned):
        cleaned = re.sub(r'\([^()]*\)', ' ', cleaned)
    m = re.search(r'\bFROM\s+(\w+)', cleaned)
    if m:
        table = m.group(1)
        if table not in SQL_KEYWORDS and table not in aliases:
            return table

    return None


def parse_all_mappers(base_dir: str) -> dict:
    """Parse all MyBatis mappers and return analysis result."""
    xml_files = scan_mybatis_dir(base_dir)

    all_statements = []
    xml_parse_count = 0
    fallback_count = 0

    for filepath in xml_files:
        stmts = parse_mapper_file(filepath)
        all_statements.extend(stmts)
        if stmts:
            xml_parse_count += 1
        logger.info("Parsed %s: %d statements", os.path.basename(filepath), len(stmts))

    # Count statements with JOIN keyword
    join_stmts = [s for s in all_statements if "JOIN" in s["sql"].upper()]

    joins = extract_joins(all_statements)
    table_usage = extract_table_usage(all_statements)

    print(f"  Mapper files found: {len(xml_files)}")
    print(f"  Mappers with statements: {xml_parse_count}")
    print(f"  Total SQL statements: {len(all_statements)}")
    print(f"  Statements with JOIN: {len(join_stmts)}")
    print(f"  Unique JOIN relationships: {len(joins)}")
    print(f"  Tables referenced: {len(table_usage)}")

    return {
        "base_dir": base_dir,
        "mapper_count": len(xml_files),
        "statement_count": len(all_statements),
        "statements": all_statements,
        "joins": joins,
        "table_usage": table_usage,
    }
