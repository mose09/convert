import argparse
import logging
import os
import re
import sys

import yaml
from dotenv import load_dotenv

from oracle_embeddings.db import get_connection
from oracle_embeddings.extractor import extract_rows
from oracle_embeddings.textifier import rows_to_texts
from oracle_embeddings.embedder import generate_embeddings
from oracle_embeddings.storage import save, EMBEDDING_FREE_FORMATS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def load_config(config_path: str) -> dict:
    """Load YAML config and resolve environment variable references."""
    with open(config_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Resolve ${ENV_VAR} references
    def replace_env(match):
        var_name = match.group(1)
        return os.environ.get(var_name, match.group(0))

    content = re.sub(r"\$\{(\w+)\}", replace_env, content)
    return yaml.safe_load(content)


def process_table(config: dict, connection, table_cfg: dict, skip_embedding: bool = False):
    """Process a single table: extract -> textify -> embed -> store."""
    table_name = table_cfg["name"]
    logger.info("Processing table: %s", table_name)

    row_limit = config["processing"].get("row_limit")
    columns, rows = extract_rows(connection, table_cfg, row_limit)

    if not rows:
        logger.warning("No rows found in %s, skipping", table_name)
        return

    file_format = config["storage"].get("file_format", "parquet")
    need_embedding = not skip_embedding and file_format not in EMBEDDING_FREE_FORMATS

    texts = rows_to_texts(columns, rows, config["processing"])

    if need_embedding:
        embeddings = generate_embeddings(texts, config["embedding"])
    else:
        embeddings = None
        logger.info("Skipping embedding generation (format: %s)", file_format)

    save(table_name, columns, rows, embeddings, config["storage"],
         connection, config["processing"])

    logger.info("Completed %s: %d rows processed", table_name, len(rows))


def main():
    parser = argparse.ArgumentParser(description="Convert Oracle table columns to LLM embeddings")
    parser.add_argument("--config", default="config.yaml", help="Path to config file")
    parser.add_argument("--table", help="Process only this table (overrides config)")
    parser.add_argument("--dry-run", action="store_true", help="Extract and textify only, skip embedding")
    parser.add_argument("--skip-embedding", action="store_true", help="Skip embedding generation (auto-enabled for txt/markdown formats)")
    args = parser.parse_args()

    load_dotenv()
    config = load_config(args.config)

    connection = get_connection(config)
    try:
        tables = config["tables"]
        if args.table:
            tables = [{"name": args.table}]

        for table_cfg in tables:
            if args.dry_run:
                row_limit = config["processing"].get("row_limit")
                columns, rows = extract_rows(connection, table_cfg, row_limit)
                texts = rows_to_texts(columns, rows, config["processing"])
                for i, text in enumerate(texts[:5]):
                    print(f"[{i}] {text}")
                if len(texts) > 5:
                    print(f"... and {len(texts) - 5} more rows")
            else:
                process_table(config, connection, table_cfg, args.skip_embedding)
    finally:
        connection.close()
        logger.info("Connection closed")


if __name__ == "__main__":
    main()
