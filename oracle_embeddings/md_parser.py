import logging
import re

logger = logging.getLogger(__name__)


def parse_schema_md(md_path: str) -> dict:
    """Parse schema .md file back into structured data."""
    with open(md_path, "r", encoding="utf-8") as f:
        content = f.read()
    content = content.replace("\r\n", "\n")  # Windows CRLF → LF

    schema = {"owner": "UNKNOWN", "tables": []}

    # Extract owner from title
    title_match = re.match(r'^# (\S+)', content)
    if title_match:
        schema["owner"] = title_match.group(1)

    # Split by table sections
    sections = re.split(r'\n(?=## )', content)

    for section in sections:
        section = section.strip()
        if not section.startswith("## "):
            continue

        header_match = re.match(r'^## (\S+)', section)
        if not header_match:
            continue

        table_name = header_match.group(1)
        if table_name in ("Relationship", "Summary"):
            continue

        table = _parse_table_section(section, table_name)
        if table:
            schema["tables"].append(table)

    logger.info("Parsed schema: %d tables from %s", len(schema["tables"]), md_path)
    return schema


def _parse_table_section(section: str, table_name: str) -> dict:
    """Parse a single table section from markdown."""
    table = {
        "name": table_name,
        "comment": None,
        "columns": [],
        "primary_keys": [],
        "foreign_keys": [],
        "indexes": [],
    }

    # Extract comment (> blockquote)
    comment_match = re.search(r'^> (.+)$', section, re.MULTILINE)
    if comment_match:
        table["comment"] = comment_match.group(1)

    # Extract columns from markdown table
    # | COLUMN_NAME (PK) | TYPE | Nullable | Default | Description |
    for match in re.finditer(
        r'^\|\s*(\S+?)(?:\s*\(PK\))?\s*\|\s*(\S+)\s*\|\s*([YN])\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|$',
        section, re.MULTILINE
    ):
        col_name = match.group(1)
        if col_name in ("Column", "--------", "-----"):
            continue

        col = {
            "column_name": col_name,
            "data_type": match.group(2),
            "nullable": match.group(3),
            "data_default": match.group(4).strip() or None,
            "comment": match.group(5).strip() or None,
        }
        table["columns"].append(col)

        # Check PK marker
        pk_check = re.search(r'\(PK\)', match.group(0))
        if pk_check:
            table["primary_keys"].append(col_name)

    # Extract Primary Key line
    pk_match = re.search(r'\*\*Primary Key\*\*:\s*(.+)', section)
    if pk_match and not table["primary_keys"]:
        table["primary_keys"] = [c.strip() for c in pk_match.group(1).split(",")]

    # Extract Foreign Keys
    for fk_match in re.finditer(
        r'`(\w+)`\s*->\s*`(\w+)\.(\w+)`(?:\s*\((\w+)\))?', section
    ):
        table["foreign_keys"].append({
            "column": fk_match.group(1),
            "ref_table": fk_match.group(2),
            "ref_column": fk_match.group(3),
            "constraint_name": fk_match.group(4) or "",
        })

    # Extract Indexes
    for idx_match in re.finditer(
        r'(?:UNIQUE\s+)?`(\w+)`\s*\(([^)]+)\)', section
    ):
        is_unique = "UNIQUE" in section[max(0, idx_match.start() - 10):idx_match.start()]
        table["indexes"].append({
            "name": idx_match.group(1),
            "unique": is_unique,
            "columns": [c.strip() for c in idx_match.group(2).split(",")],
        })

    return table


def parse_query_md(md_path: str) -> list[dict]:
    """Parse query analysis .md file to extract JOIN relationships.

    The ``Sources`` column may contain a single ``mapper#id`` or a list
    separated by ``;`` (when the same join appears in multiple
    statements). Both formats are preserved: the first entry populates
    ``source_mapper`` / ``source_id`` for back-compat and the full
    list is kept in ``sources``.
    """
    with open(md_path, "r", encoding="utf-8") as f:
        content = f.read()
    content = content.replace("\r\n", "\n")

    joins = []

    # Find relationship table rows
    # | TABLE_A | COL1 | <-> | TABLE_B | COL2 | TYPE | SOURCE(s) |
    for match in re.finditer(
        r'^\|\s*(\w+)\s*\|\s*(\w+)\s*\|\s*<->\s*\|\s*(\w+)\s*\|\s*(\w+)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|$',
        content, re.MULTILINE
    ):
        t1, c1, t2, c2 = match.group(1), match.group(2), match.group(3), match.group(4)
        if t1 in ("Table", "--------", "-----", "Source"):
            continue
        sources_raw = match.group(6).strip()
        sources = [s.strip() for s in sources_raw.split(";") if s.strip()]
        first = sources[0] if sources else ""
        if "#" in first:
            mapper, _, sid = first.partition("#")
        else:
            mapper, sid = first, ""
        joins.append({
            "table1": t1,
            "column1": c1,
            "table2": t2,
            "column2": c2,
            "join_type": match.group(5).strip(),
            "source_mapper": mapper,
            "source_id": sid,
            "sources": sources,
        })

    logger.info("Parsed query analysis: %d joins from %s", len(joins), md_path)
    return joins


def parse_query_tables(md_path: str) -> set:
    """Extract all table names referenced in query analysis .md (from Table Usage Summary)."""
    with open(md_path, "r", encoding="utf-8") as f:
        content = f.read()
    content = content.replace("\r\n", "\n")

    tables = set()

    # From Table Usage Summary: | TABLE_NAME | SELECT | INSERT | ...
    for match in re.finditer(
        r'^\|\s*(\w+)\s*\|\s*\d+\s*\|',
        content, re.MULTILINE
    ):
        table = match.group(1)
        if table not in ("Table", "--------", "-----"):
            tables.add(table)

    # Also from Inferred Relationships
    for match in re.finditer(
        r'^\|\s*(\w+)\s*\|\s*\w+\s*\|\s*<->',
        content, re.MULTILINE
    ):
        table = match.group(1)
        if table not in ("Table", "--------", "-----"):
            tables.add(table)

    # Right side of relationships
    for match in re.finditer(
        r'<->\s*\|\s*(\w+)\s*\|',
        content, re.MULTILINE
    ):
        table = match.group(1)
        if table not in ("Table", "--------", "-----"):
            tables.add(table)

    logger.info("Parsed query tables: %d tables from %s", len(tables), md_path)
    return tables


def parse_table_usage(md_path: str) -> dict:
    """Parse Table Usage Summary from query .md to get as_main/as_join counts."""
    with open(md_path, "r", encoding="utf-8") as f:
        content = f.read()
    content = content.replace("\r\n", "\n")

    usage = {}

    # | TABLE | SELECT | INSERT | UPDATE | DELETE | AsMain | AsJoin | Mappers |
    for match in re.finditer(
        r'^\|\s*(\w+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|',
        content, re.MULTILINE
    ):
        table = match.group(1)
        if table in ("Table", "--------", "-----"):
            continue
        usage[table] = {
            "select_count": int(match.group(2)),
            "insert_count": int(match.group(3)),
            "update_count": int(match.group(4)),
            "delete_count": int(match.group(5)),
            "as_main_count": int(match.group(6)),
            "as_join_count": int(match.group(7)),
        }

    logger.info("Parsed table usage: %d tables from %s", len(usage), md_path)
    return usage
