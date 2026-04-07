import logging
import os
import re
import xml.etree.ElementTree as ET

logger = logging.getLogger(__name__)


def scan_mybatis_dir(base_dir: str) -> list[str]:
    """Find all MyBatis mapper XML files recursively."""
    xml_files = []
    for root, dirs, files in os.walk(base_dir):
        for f in files:
            if f.endswith(".xml"):
                filepath = os.path.join(root, f)
                if _is_mybatis_mapper(filepath):
                    xml_files.append(filepath)
    logger.info("Found %d MyBatis mapper files in %s", len(xml_files), base_dir)
    return xml_files


def _is_mybatis_mapper(filepath: str) -> bool:
    """Check if an XML file is a MyBatis mapper."""
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            head = f.read(1000)
        return "mapper" in head.lower() and ("<select" in head.lower() or
               "<insert" in head.lower() or "<update" in head.lower() or
               "<delete" in head.lower() or "namespace" in head.lower())
    except Exception:
        return False


def parse_mapper_file(filepath: str) -> list[dict]:
    """Parse a single MyBatis mapper XML and extract SQL statements."""
    statements = []
    try:
        tree = ET.parse(filepath)
        root = tree.getroot()
    except ET.ParseError:
        # MyBatis XML may have unresolved entities or includes
        return _parse_mapper_fallback(filepath)

    namespace = root.attrib.get("namespace", "")
    mapper_name = os.path.basename(filepath)

    for tag in ("select", "insert", "update", "delete"):
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

    pattern = r'<(select|insert|update|delete)\s+[^>]*id\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</\1>'
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
    """Extract full SQL text from an XML element, including nested elements."""
    parts = []
    if elem.text:
        parts.append(elem.text)
    for child in elem:
        # Handle <if>, <where>, <foreach>, <choose>, <trim>, <set>, <include> etc.
        if child.text:
            parts.append(child.text)
        if child.tail:
            parts.append(child.tail)
        # Recurse into nested dynamic SQL elements
        for sub in child:
            if sub.text:
                parts.append(sub.text)
            if sub.tail:
                parts.append(sub.tail)
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
    # "FROM ORDERS o" -> alias o = ORDERS
    # "JOIN CUSTOMERS AS c" -> alias c = CUSTOMERS
    alias_map = {}
    known_aliases = set()  # alias로 확인된 이름들

    # Pattern: FROM/JOIN table_name alias (optional AS)
    table_alias_pattern = r'(?:FROM|JOIN)\s+(\w+)\s+(?:AS\s+)?(\w+)'
    for match in re.finditer(table_alias_pattern, sql):
        table, alias = match.groups()
        if alias.upper() not in SQL_KEYWORDS:
            alias_map[alias.upper()] = table.upper()
            known_aliases.add(alias.upper())

    # Pattern: FROM/JOIN table_name (no alias, followed by WHERE/ON/newline/comma/end)
    no_alias_pattern = r'(?:FROM|JOIN)\s+(\w+)(?:\s*(?:WHERE|ON|,|\)|$))'
    for match in re.finditer(no_alias_pattern, sql):
        table = match.group(1).upper()
        if table not in SQL_KEYWORDS and table not in known_aliases:
            alias_map[table] = table

    # Parse ON conditions: a.col = b.col
    on_pattern = r'(\w+)\.(\w+)\s*=\s*(\w+)\.(\w+)'
    for match in re.finditer(on_pattern, sql):
        alias1, col1, alias2, col2 = match.groups()
        table1 = alias_map.get(alias1.upper(), alias1.upper())
        table2 = alias_map.get(alias2.upper(), alias2.upper())

        # Skip if alias couldn't be resolved (still an alias, not a table)
        if table1 != table2 and table1 not in SQL_KEYWORDS and table2 not in SQL_KEYWORDS:
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

        # First pass: identify aliases
        for match in re.finditer(r'(?:FROM|JOIN)\s+(\w+)\s+(?:AS\s+)?(\w+)', sql):
            table, alias = match.groups()
            if alias not in SQL_KEYWORDS:
                aliases_in_stmt.add(alias)
                if table not in SQL_KEYWORDS:
                    tables.add(table)

        # Second pass: tables without alias
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

        for table in tables:
            if table not in usage:
                usage[table] = {
                    "select_count": 0, "insert_count": 0,
                    "update_count": 0, "delete_count": 0,
                    "mappers": set(), "queries": [],
                }
            key = f"{stmt['type'].lower()}_count"
            if key in usage[table]:
                usage[table][key] += 1
            usage[table]["mappers"].add(stmt["mapper"])
            usage[table]["queries"].append(f"{stmt['mapper']}#{stmt['id']}")

    # Convert sets to lists for serialization
    for table in usage:
        usage[table]["mappers"] = sorted(usage[table]["mappers"])

    return usage


def parse_all_mappers(base_dir: str) -> dict:
    """Parse all MyBatis mappers and return analysis result."""
    xml_files = scan_mybatis_dir(base_dir)

    all_statements = []
    for filepath in xml_files:
        stmts = parse_mapper_file(filepath)
        all_statements.extend(stmts)
        logger.info("Parsed %s: %d statements", os.path.basename(filepath), len(stmts))

    joins = extract_joins(all_statements)
    table_usage = extract_table_usage(all_statements)

    return {
        "base_dir": base_dir,
        "mapper_count": len(xml_files),
        "statement_count": len(all_statements),
        "statements": all_statements,
        "joins": joins,
        "table_usage": table_usage,
    }
