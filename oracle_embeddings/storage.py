import logging
import os
from datetime import datetime

logger = logging.getLogger(__name__)


def save_schema_markdown(schema: dict, output_dir: str):
    """Save schema as a Markdown file for Msty Knowledge Base."""
    os.makedirs(output_dir, exist_ok=True)

    owner = schema["owner"]
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = os.path.join(output_dir, f"{owner}_schema_{timestamp}.md")

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(f"# {owner} Database Schema\n\n")
        f.write(f"Total tables: {len(schema['tables'])}\n\n")
        f.write("---\n\n")

        for table in schema["tables"]:
            _write_table_section(f, table)

        # Relationship summary
        _write_relationship_summary(f, schema["tables"])

    logger.info("Saved schema markdown: %s (%d tables)", filepath, len(schema["tables"]))
    return filepath


def save_schema_txt(schema: dict, output_dir: str):
    """Save schema as a plain text file for Msty Knowledge Base."""
    os.makedirs(output_dir, exist_ok=True)

    owner = schema["owner"]
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = os.path.join(output_dir, f"{owner}_schema_{timestamp}.txt")

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(f"DATABASE SCHEMA: {owner}\n")
        f.write(f"Total tables: {len(schema['tables'])}\n")
        f.write("=" * 60 + "\n\n")

        for table in schema["tables"]:
            f.write(f"TABLE: {table['name']}\n")
            if table.get("comment"):
                f.write(f"Description: {table['comment']}\n")
            f.write("-" * 40 + "\n")

            # Columns
            for col in table["columns"]:
                nullable = "" if col["nullable"] == "Y" else " NOT NULL"
                default = f" DEFAULT {col['data_default']}" if col.get("data_default") else ""
                comment = f"  -- {col['comment']}" if col.get("comment") else ""
                f.write(f"  {col['column_name']} {col['data_type']}{nullable}{default}{comment}\n")

            # PK
            if table["primary_keys"]:
                f.write(f"  PRIMARY KEY: ({', '.join(table['primary_keys'])})\n")

            # FK
            for fk in table["foreign_keys"]:
                f.write(f"  FK: {fk['column']} -> {fk['ref_table']}.{fk['ref_column']}\n")

            f.write("\n")

    logger.info("Saved schema text: %s (%d tables)", filepath, len(schema["tables"]))
    return filepath


def _write_table_section(f, table: dict):
    """Write a single table section in Markdown."""
    f.write(f"## {table['name']}\n\n")

    if table.get("comment"):
        f.write(f"> {table['comment']}\n\n")

    # Column table
    f.write("| Column | Type | Nullable | Default | Description |\n")
    f.write("|--------|------|----------|---------|-------------|\n")
    for col in table["columns"]:
        pk_mark = ""
        if col["column_name"] in table.get("primary_keys", []):
            pk_mark = " (PK)"
        nullable = "Y" if col["nullable"] == "Y" else "N"
        default = col.get("data_default") or ""
        comment = col.get("comment") or ""
        f.write(f"| {col['column_name']}{pk_mark} | {col['data_type']} | {nullable} | {default} | {comment} |\n")

    f.write("\n")

    # Primary Key
    if table["primary_keys"]:
        f.write(f"**Primary Key**: {', '.join(table['primary_keys'])}\n\n")

    # Foreign Keys
    if table["foreign_keys"]:
        f.write("**Foreign Keys**:\n")
        for fk in table["foreign_keys"]:
            f.write(f"- `{fk['column']}` -> `{fk['ref_table']}.{fk['ref_column']}` ({fk['constraint_name']})\n")
        f.write("\n")

    # Indexes
    if table["indexes"]:
        f.write("**Indexes**:\n")
        for idx in table["indexes"]:
            unique = "UNIQUE " if idx["unique"] else ""
            f.write(f"- {unique}`{idx['name']}` ({', '.join(idx['columns'])})\n")
        f.write("\n")

    f.write("---\n\n")


def save_query_markdown(analysis: dict, output_dir: str):
    """Save MyBatis query analysis as Markdown for Msty Knowledge Base."""
    os.makedirs(output_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = os.path.join(output_dir, f"query_analysis_{timestamp}.md")

    with open(filepath, "w", encoding="utf-8") as f:
        f.write("# Query Analysis (MyBatis)\n\n")
        f.write(f"- Mapper files: {analysis['mapper_count']}\n")
        f.write(f"- SQL statements: {analysis['statement_count']}\n")
        f.write(f"- Discovered relationships: {len(analysis['joins'])}\n\n")
        f.write("---\n\n")

        # 1. Inferred relationships from JOINs
        _write_join_relationships(f, analysis["joins"])

        # 2. Table usage summary
        _write_table_usage(f, analysis["table_usage"])

        # 3. Query details per mapper
        _write_query_details(f, analysis["statements"])

    logger.info("Saved query analysis: %s", filepath)
    return filepath


def _write_join_relationships(f, joins: list[dict]):
    """Write discovered JOIN relationships."""
    if not joins:
        f.write("## Inferred Relationships\n\nNo JOIN relationships found.\n\n---\n\n")
        return

    f.write("## Inferred Relationships (from JOIN)\n\n")
    f.write("FK가 없는 테이블 간의 관계를 쿼리의 JOIN 조건에서 추론한 결과입니다.\n\n")
    f.write("| Table A | Column | JOIN | Table B | Column | Type | Source |\n")
    f.write("|---------|--------|------|---------|--------|------|--------|\n")
    for j in joins:
        f.write(f"| {j['table1']} | {j['column1']} | <-> | {j['table2']} | {j['column2']} "
                f"| {j['join_type']} | {j['source_mapper']}#{j['source_id']} |\n")
    f.write("\n---\n\n")


def _write_table_usage(f, table_usage: dict):
    """Write table usage statistics."""
    if not table_usage:
        return

    f.write("## Table Usage Summary\n\n")
    f.write("각 테이블이 쿼리에서 어떻게 사용되는지 통계입니다.\n\n")
    f.write("| Table | SELECT | INSERT | UPDATE | DELETE | Mappers |\n")
    f.write("|-------|--------|--------|--------|--------|----------|\n")

    for table in sorted(table_usage.keys()):
        u = table_usage[table]
        mappers = ", ".join(u["mappers"][:3])
        if len(u["mappers"]) > 3:
            mappers += f" +{len(u['mappers']) - 3}"
        f.write(f"| {table} | {u['select_count']} | {u['insert_count']} "
                f"| {u['update_count']} | {u['delete_count']} | {mappers} |\n")

    f.write("\n---\n\n")


def _write_query_details(f, statements: list[dict]):
    """Write detailed query information grouped by mapper."""
    if not statements:
        return

    f.write("## Query Details\n\n")

    # Group by mapper
    mappers = {}
    for stmt in statements:
        mapper = stmt["mapper"]
        if mapper not in mappers:
            mappers[mapper] = []
        mappers[mapper].append(stmt)

    for mapper_name in sorted(mappers.keys()):
        stmts = mappers[mapper_name]
        namespace = stmts[0].get("namespace", "")

        f.write(f"### {mapper_name}\n\n")
        if namespace:
            f.write(f"Namespace: `{namespace}`\n\n")

        for stmt in stmts:
            f.write(f"**{stmt['type']}** `{stmt['id']}`\n\n")
            f.write(f"```sql\n{stmt['sql']}\n```\n\n")

        f.write("---\n\n")


def _write_relationship_summary(f, tables: list[dict]):
    """Write a summary of all FK relationships."""
    all_fks = []
    for table in tables:
        for fk in table["foreign_keys"]:
            all_fks.append((table["name"], fk["column"], fk["ref_table"], fk["ref_column"]))

    if not all_fks:
        return

    f.write("## Relationship Summary\n\n")
    f.write("| Source Table | Column | -> | Target Table | Column |\n")
    f.write("|-------------|--------|-----|-------------|--------|\n")
    for src_table, src_col, ref_table, ref_col in all_fks:
        f.write(f"| {src_table} | {src_col} | -> | {ref_table} | {ref_col} |\n")
    f.write("\n")
