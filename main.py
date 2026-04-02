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


def cmd_all(args):
    """Run both schema extraction and query analysis."""
    print("=== Step 1: Schema Extraction ===")
    cmd_schema(args)
    print()
    print("=== Step 2: Query Analysis ===")
    cmd_query(args)


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

    # all command
    all_parser = subparsers.add_parser("all", help="Run both schema and query analysis")
    all_parser.add_argument("mybatis_dir", help="Path to MyBatis mapper XML directory")
    all_parser.add_argument("--format", choices=["markdown", "txt"], default=None)
    all_parser.add_argument("--owner", help="Schema owner (overrides config)")
    all_parser.add_argument("--table", help="Extract specific table only")

    args = parser.parse_args()

    if args.command == "schema":
        cmd_schema(args)
    elif args.command == "query":
        cmd_query(args)
    elif args.command == "all":
        cmd_all(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
