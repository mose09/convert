import argparse
import logging
import os
import re

import yaml
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def load_config(config_path: str) -> dict:
    """Load YAML config and resolve environment variable references."""
    with open(config_path, "r", encoding="utf-8") as f:
        content = f.read()

    def replace_env(match):
        var_name = match.group(1)
        return os.environ.get(var_name, match.group(0))

    content = re.sub(r"\$\{(\w+)\}", replace_env, content)
    return yaml.safe_load(content)


def cmd_schema(args):
    """Extract Oracle schema metadata to Markdown."""
    from oracle_embeddings.db import get_connection
    from oracle_embeddings.extractor import extract_schema
    from oracle_embeddings.storage import save_schema_markdown, save_schema_txt

    load_dotenv()
    config = load_config(args.config)

    owner = args.owner or config.get("oracle", {}).get("schema_owner", os.environ.get("ORACLE_USER", ""))
    table_names = [args.table] if args.table else config.get("tables")
    file_format = args.format or config.get("storage", {}).get("file_format", "markdown")
    output_dir = config.get("storage", {}).get("output_dir", "./output")

    connection = get_connection(config)
    try:
        schema = extract_schema(connection, owner, table_names)

        if file_format == "markdown":
            filepath = save_schema_markdown(schema, output_dir)
        else:
            filepath = save_schema_txt(schema, output_dir)

        print(f"Schema exported: {filepath}")
        print(f"Tables: {len(schema['tables'])}")
        total_cols = sum(len(t['columns']) for t in schema['tables'])
        total_fks = sum(len(t['foreign_keys']) for t in schema['tables'])
        print(f"Columns: {total_cols}, Foreign Keys: {total_fks}")
    finally:
        connection.close()


def cmd_query(args):
    """Analyze MyBatis mapper XML files and extract relationships."""
    from oracle_embeddings.mybatis_parser import parse_all_mappers
    from oracle_embeddings.storage import save_query_markdown

    config = load_config(args.config) if os.path.exists(args.config) else {}
    output_dir = config.get("storage", {}).get("output_dir", "./output")

    mybatis_dir = args.mybatis_dir
    if not os.path.isdir(mybatis_dir):
        print(f"Error: Directory not found: {mybatis_dir}")
        return

    analysis = parse_all_mappers(mybatis_dir)
    filepath = save_query_markdown(analysis, output_dir)

    print(f"Query analysis exported: {filepath}")
    print(f"Mappers: {analysis['mapper_count']}")
    print(f"SQL statements: {analysis['statement_count']}")
    print(f"Inferred relationships: {len(analysis['joins'])}")


def cmd_erd(args):
    """Generate Mermaid ERD from schema + query analysis."""
    from oracle_embeddings.db import get_connection
    from oracle_embeddings.extractor import extract_schema
    from oracle_embeddings.mybatis_parser import parse_all_mappers
    from oracle_embeddings.erd_generator import generate_mermaid_erd, build_erd_markdown

    load_dotenv()
    config = load_config(args.config)

    output_dir = config.get("storage", {}).get("output_dir", "./output")
    owner = args.owner or config.get("oracle", {}).get("schema_owner", os.environ.get("ORACLE_USER", ""))
    table_names = [args.table] if args.table else config.get("tables")

    # 1. Schema extraction
    print("=== Step 1: Schema Extraction ===")
    connection = get_connection(config)
    try:
        schema = extract_schema(connection, owner, table_names)
        print(f"Tables: {len(schema['tables'])}")
    finally:
        connection.close()

    # 2. Query analysis
    joins = []
    if args.mybatis_dir:
        if not os.path.isdir(args.mybatis_dir):
            print(f"Error: Directory not found: {args.mybatis_dir}")
            return
        print("\n=== Step 2: Query Analysis ===")
        analysis = parse_all_mappers(args.mybatis_dir)
        joins = analysis["joins"]
        print(f"Inferred relationships: {len(joins)}")
    else:
        print("\n=== Step 2: Query Analysis (skipped, no --mybatis-dir) ===")

    # 3. LLM assist (optional)
    llm_result = None
    if args.llm_assist:
        print("\n=== Step 3: LLM Assist ===")
        from oracle_embeddings.llm_assist import assist_erd
        llm_result = assist_erd(schema, joins, config)
        extra = len(llm_result.get("inferred_relations", []))
        groups = len(llm_result.get("domain_groups", {}))
        print(f"LLM inferred relations: {extra}, Domain groups: {groups}")
    else:
        print("\n=== Step 3: LLM Assist (skipped, use --llm-assist to enable) ===")

    # 4. Generate ERD
    print("\n=== Step 4: Generate ERD ===")
    mermaid_code = generate_mermaid_erd(schema, joins, llm_result)
    erd_md = build_erd_markdown(mermaid_code, schema, joins, llm_result)

    os.makedirs(output_dir, exist_ok=True)
    from datetime import datetime
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = os.path.join(output_dir, f"erd_{owner}_{timestamp}.md")
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(erd_md)

    print(f"ERD exported: {filepath}")
    total_rels = len(joins) + (len(llm_result.get("inferred_relations", [])) if llm_result else 0)
    print(f"Total relationships: {total_rels}")


def cmd_embed(args):
    """Embed .md files into ChromaDB vector store."""
    from oracle_embeddings.vector_store import embed_schema_md, embed_query_md

    load_dotenv()
    config = load_config(args.config)
    db_path = config.get("vectordb", {}).get("db_path", "./vectordb")

    if args.schema_md:
        print(f"=== Embedding Schema: {args.schema_md} ===")
        count = embed_schema_md(args.schema_md, config, db_path)
        print(f"Schema chunks embedded: {count}")

    if args.query_md:
        print(f"=== Embedding Query Analysis: {args.query_md} ===")
        count = embed_query_md(args.query_md, config, db_path)
        print(f"Query chunks embedded: {count}")

    if not args.schema_md and not args.query_md:
        print("Error: --schema-md 또는 --query-md 중 하나 이상 지정하세요.")
        return

    print(f"\nVector DB saved: {db_path}")


def cmd_erd_rag(args):
    """Generate Mermaid ERD using RAG (vector search + LLM)."""
    from oracle_embeddings.rag_erd import generate_erd_with_rag

    load_dotenv()
    config = load_config(args.config)
    db_path = config.get("vectordb", {}).get("db_path", "./vectordb")
    output_dir = config.get("storage", {}).get("output_dir", "./output")

    # Validate vector DB exists
    if not os.path.isdir(db_path):
        print(f"Error: Vector DB not found at '{os.path.abspath(db_path)}'")
        print("먼저 'python main.py embed' 를 실행하세요.")
        return

    target_tables = None
    if args.tables:
        target_tables = [t.strip().upper() for t in args.tables.split(",")]

    print("=== RAG-based ERD Generation ===")
    print(f"Vector DB: {os.path.abspath(db_path)}")
    print(f"Output dir: {os.path.abspath(output_dir)}")
    if target_tables:
        print(f"Target tables: {', '.join(target_tables)}")

    try:
        filepath = generate_erd_with_rag(config, db_path, output_dir, target_tables)
        if filepath is None:
            print("\nERD generation aborted (no context).")
        elif os.path.exists(filepath):
            size = os.path.getsize(filepath)
            print(f"\nERD exported: {os.path.abspath(filepath)} ({size} bytes)")
        else:
            print(f"\nError: File was not created at {os.path.abspath(filepath)}")
    except Exception as e:
        logger.error("ERD generation failed: %s", e, exc_info=True)
        print(f"\nError: {e}")


def cmd_erd_md(args):
    """Generate Mermaid ERD from existing .md files (no DB, no LLM)."""
    from oracle_embeddings.md_parser import parse_schema_md, parse_query_md
    from oracle_embeddings.erd_generator import generate_mermaid_erd, build_erd_markdown

    config = load_config(args.config) if os.path.exists(args.config) else {}
    output_dir = config.get("storage", {}).get("output_dir", "./output")

    if not args.schema_md:
        print("Error: --schema-md 는 필수입니다.")
        return

    # 1. Parse schema .md
    print(f"=== Step 1: Parsing Schema: {args.schema_md} ===")
    schema = parse_schema_md(args.schema_md)
    print(f"  Tables: {len(schema['tables'])}")
    total_cols = sum(len(t['columns']) for t in schema['tables'])
    print(f"  Columns: {total_cols}")

    # 2. Parse query .md (optional)
    joins = []
    if args.query_md:
        print(f"\n=== Step 2: Parsing Query Analysis: {args.query_md} ===")
        joins = parse_query_md(args.query_md)
        print(f"  JOIN relationships: {len(joins)}")
    else:
        print("\n=== Step 2: Query Analysis (skipped, no --query-md) ===")

    # 3. Filter tables if specified
    if args.tables:
        target_tables = {t.strip().upper() for t in args.tables.split(",")}
        # Include related tables from joins
        related = set()
        for j in joins:
            if j["table1"] in target_tables or j["table2"] in target_tables:
                related.add(j["table1"])
                related.add(j["table2"])
        target_tables.update(related)

        schema["tables"] = [t for t in schema["tables"] if t["name"] in target_tables]
        joins = [j for j in joins if j["table1"] in target_tables or j["table2"] in target_tables]
        print(f"\n  Filtered to {len(schema['tables'])} tables (+ related)")

    # 4. Filter: only tables with relationships (optional)
    if args.related_only and not args.tables:
        tables_with_rels = set()
        for j in joins:
            tables_with_rels.add(j["table1"])
            tables_with_rels.add(j["table2"])
        schema["tables"] = [t for t in schema["tables"] if t["name"] in tables_with_rels]
        print(f"\n  Filtered to {len(schema['tables'])} tables (with relationships only)")

    # 5. Generate ERD
    print(f"\n=== Step 3: Generating Mermaid ERD ===")
    mermaid_code = generate_mermaid_erd(schema, joins)
    erd_md = build_erd_markdown(mermaid_code, schema, joins)

    os.makedirs(output_dir, exist_ok=True)
    from datetime import datetime
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    owner = schema.get("owner", "UNKNOWN")
    filepath = os.path.join(output_dir, f"erd_{owner}_{timestamp}.md")

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(erd_md)

    print(f"\nERD exported: {os.path.abspath(filepath)}")
    print(f"Tables: {len(schema['tables'])}, Relationships: {len(joins)}")
    lines = mermaid_code.count("\n") + 1
    print(f"Mermaid code: {lines} lines")


def cmd_all(args):
    """Run schema, query, and erd generation."""
    print("=== Schema Extraction ===")
    cmd_schema(args)
    print()
    print("=== Query Analysis ===")
    cmd_query(args)
    print()
    print("=== ERD Generation ===")
    cmd_erd(args)


def main():
    parser = argparse.ArgumentParser(
        description="Oracle Schema & Query Analyzer for Msty Knowledge Base"
    )
    parser.add_argument("--config", default="config.yaml", help="Path to config file")

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # schema command
    schema_parser = subparsers.add_parser("schema", help="Extract Oracle schema metadata")
    schema_parser.add_argument("--format", choices=["markdown", "txt"], default=None)
    schema_parser.add_argument("--owner", help="Schema owner (overrides config)")
    schema_parser.add_argument("--table", help="Extract specific table only")

    # query command
    query_parser = subparsers.add_parser("query", help="Analyze MyBatis mapper XML files")
    query_parser.add_argument("mybatis_dir", help="Path to MyBatis mapper XML directory")

    # erd command (direct, requires Oracle connection)
    erd_parser = subparsers.add_parser("erd", help="Generate Mermaid ERD (direct DB access)")
    erd_parser.add_argument("--mybatis-dir", help="Path to MyBatis mapper XML directory")
    erd_parser.add_argument("--owner", help="Schema owner (overrides config)")
    erd_parser.add_argument("--table", help="Extract specific table only")
    erd_parser.add_argument("--llm-assist", action="store_true",
                            help="Use local LLM for column descriptions, missing relations, domain grouping")

    # embed command
    embed_parser = subparsers.add_parser("embed", help="Embed .md files into vector DB")
    embed_parser.add_argument("--schema-md", help="Path to schema .md file")
    embed_parser.add_argument("--query-md", help="Path to query analysis .md file")

    # erd-md command (from .md files, no DB, no LLM)
    erd_md_parser = subparsers.add_parser("erd-md", help="Generate ERD from .md files (no DB, no LLM)")
    erd_md_parser.add_argument("--schema-md", required=True, help="Path to schema .md file")
    erd_md_parser.add_argument("--query-md", help="Path to query analysis .md file")
    erd_md_parser.add_argument("--tables", help="Comma-separated table names to focus on (+ related tables)")
    erd_md_parser.add_argument("--related-only", action="store_true",
                               help="Only include tables that have relationships")

    # erd-rag command
    erd_rag_parser = subparsers.add_parser("erd-rag", help="Generate ERD via RAG (vector DB + LLM)")
    erd_rag_parser.add_argument("--tables", help="Comma-separated table names to focus on")

    # all command
    all_parser = subparsers.add_parser("all", help="Run schema + query + erd")
    all_parser.add_argument("mybatis_dir", help="Path to MyBatis mapper XML directory")
    all_parser.add_argument("--format", choices=["markdown", "txt"], default=None)
    all_parser.add_argument("--owner", help="Schema owner (overrides config)")
    all_parser.add_argument("--table", help="Extract specific table only")
    all_parser.add_argument("--llm-assist", action="store_true",
                            help="Use local LLM for ERD enrichment")

    args = parser.parse_args()

    if args.command == "schema":
        cmd_schema(args)
    elif args.command == "query":
        cmd_query(args)
    elif args.command == "erd":
        cmd_erd(args)
    elif args.command == "embed":
        cmd_embed(args)
    elif args.command == "erd-md":
        cmd_erd_md(args)
    elif args.command == "erd-rag":
        cmd_erd_rag(args)
    elif args.command == "all":
        cmd_all(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
