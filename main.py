import argparse
import logging
import os
import re

import yaml
from dotenv import load_dotenv

from oracle_embeddings.db import get_connection
from oracle_embeddings.extractor import extract_schema
from oracle_embeddings.storage import save_schema_markdown, save_schema_txt

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


def main():
    parser = argparse.ArgumentParser(
        description="Extract Oracle table/column schema to Markdown for Msty Knowledge Base"
    )
    parser.add_argument("--config", default="config.yaml", help="Path to config file")
    parser.add_argument("--format", choices=["markdown", "txt"], default=None,
                        help="Output format (overrides config)")
    parser.add_argument("--owner", help="Schema owner (overrides config)")
    parser.add_argument("--table", help="Extract specific table only")
    args = parser.parse_args()

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
        logger.info("Connection closed")


if __name__ == "__main__":
    main()
