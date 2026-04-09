import logging
import os
import re
import xml.etree.ElementTree as ET

logger = logging.getLogger(__name__)


def scan_mybatis_dir(base_dir: str) -> list[str]:
    """Find all MyBatis/iBatis mapper XML files recursively."""
    xml_files = []
    for root, dirs, files in os.walk(base_dir):
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
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            head = f.read(5000)
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
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            xml_content = f.read()
        xml_content = re.sub(r'<!DOCTYPE[^>]*>', '', xml_content)
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
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except Exception as e:
        logger.warning("Cannot read %s: %s", filepath, e)
        return []

    namespace_match = re.search(r'namespace\s*=\s*["\']([^"\']+)', content)
    namespace = namespace_match.group(1) if namespace_match else ""

    pattern = r'<(select|insert|update|delete|statement|procedure)\s+[^>]*id\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</\1>'
    for match in re.finditer(pattern, content, re.DOTALL | re.IGNORECASE):
        tag, stmt_id, sql_body = match.groups()
        sql_text = _clean_sql(sql_body)
        if sql_text.strip():
            statements.append({
                "mapper": mapper_name,
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
    """Clean SQL text by removing MyBatis parameters and extra whitespace."""
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
    """Extract JOIN relationships from SQL statements."""
    joins = []
    seen = set()

    for stmt in statements:
        sql = stmt["sql"].upper()
        found = _parse_joins_from_sql(sql)
        for join in found:
            key = (join["table1"], join["column1"], join["table2"], join["column2"])
            reverse_key = (join["table2"], join["column2"], join["table1"], join["column1"])
            if key not in seen and reverse_key not in seen:
                seen.add(key)
                join["source_mapper"] = stmt["mapper"]
                join["source_id"] = stmt["id"]
                join["source_type"] = stmt["type"]
                joins.append(join)

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

    # Parse ON conditions: a.col = b.col
    on_pattern = r'(\w+)\.(\w+)\s*=\s*(\w+)\.(\w+)'
    for match in re.finditer(on_pattern, sql):
        alias1, col1, alias2, col2 = match.groups()
        table1 = alias_map.get(alias1.upper())
        table2 = alias_map.get(alias2.upper())

        # Only include if BOTH aliases resolved to real table names
        if table1 and table2 and table1 != table2:
            results.append({
                "table1": table1,
                "column1": col1.upper(),
                "table2": table2,
                "column2": col2.upper(),
                "join_type": _detect_join_type(sql, match.start()),
            })

    return results


def _detect_join_type(sql: str, pos: int) -> str:
    """Detect the type of JOIN from context."""
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

        # Identify main table (FROM 바로 뒤) vs join tables
        main_table = _extract_main_table(sql, aliases_in_stmt)
        join_tables = set()
        for match in re.finditer(r'JOIN\s+(\w+)', sql):
            jt = match.group(1)
            if jt not in SQL_KEYWORDS and jt not in aliases_in_stmt:
                join_tables.add(jt)

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
    m = re.search(r'\bFROM\s+(\w+)', sql)
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
