import json
import logging
import os
from datetime import datetime

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

logger = logging.getLogger(__name__)


EMBEDDING_FREE_FORMATS = {"txt", "markdown"}


def save(table_name: str, columns: list[str], rows: list[tuple],
         embeddings: list[list[float]], storage_config: dict,
         connection=None, processing_config: dict = None):
    """Save embeddings to file and optionally to Oracle."""
    file_format = storage_config.get("file_format", "parquet")
    output_dir = storage_config.get("output_dir", "./output")

    os.makedirs(output_dir, exist_ok=True)

    if file_format == "parquet":
        save_to_parquet(table_name, columns, rows, embeddings, output_dir)
    elif file_format == "jsonl":
        save_to_jsonl(table_name, columns, rows, embeddings, output_dir)
    elif file_format == "txt":
        save_to_txt(table_name, columns, rows, output_dir, processing_config or {})
    elif file_format == "markdown":
        save_to_markdown(table_name, columns, rows, output_dir, processing_config or {})
    else:
        raise ValueError(f"Unsupported file format: {file_format}")

    if storage_config.get("write_to_oracle") and connection:
        target_table = storage_config.get("oracle_target_table", "EMBEDDINGS_STORE")
        save_to_oracle(table_name, columns, rows, embeddings, connection, target_table)


def save_to_parquet(table_name: str, columns: list[str], rows: list[tuple],
                    embeddings: list[list[float]], output_dir: str):
    """Save results as a Parquet file."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = os.path.join(output_dir, f"{table_name}_{timestamp}.parquet")

    data = {"row_index": list(range(len(rows)))}
    for i, col in enumerate(columns):
        data[col] = [row[i] for row in rows]
    data["embedding"] = [emb for emb in embeddings]

    table = pa.table({
        k: pa.array(v) if k != "embedding" else pa.array(v, type=pa.list_(pa.float32()))
        for k, v in data.items()
    })
    pq.write_table(table, filepath)
    logger.info("Saved parquet: %s (%d rows)", filepath, len(rows))


def save_to_jsonl(table_name: str, columns: list[str], rows: list[tuple],
                  embeddings: list[list[float]], output_dir: str):
    """Save results as a JSONL file."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = os.path.join(output_dir, f"{table_name}_{timestamp}.jsonl")

    with open(filepath, "w", encoding="utf-8") as f:
        for idx, (row, emb) in enumerate(zip(rows, embeddings)):
            record = {
                "row_index": idx,
                "source_table": table_name,
                "data": {col: _serialize_value(val) for col, val in zip(columns, row)},
                "embedding": emb,
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    logger.info("Saved JSONL: %s (%d rows)", filepath, len(rows))


def save_to_oracle(table_name: str, columns: list[str], rows: list[tuple],
                   embeddings: list[list[float]], connection, target_table: str):
    """Save embeddings back to an Oracle table."""
    create_sql = f"""
        BEGIN
            EXECUTE IMMEDIATE '
                CREATE TABLE "{target_table}" (
                    source_table VARCHAR2(128),
                    row_index NUMBER,
                    source_data CLOB,
                    embedding CLOB
                )
            ';
        EXCEPTION
            WHEN OTHERS THEN
                IF SQLCODE = -955 THEN NULL;
                ELSE RAISE;
                END IF;
        END;
    """
    with connection.cursor() as cursor:
        cursor.execute(create_sql)

    insert_sql = f"""
        INSERT INTO "{target_table}" (source_table, row_index, source_data, embedding)
        VALUES (:source_table, :row_index, :source_data, :embedding)
    """
    with connection.cursor() as cursor:
        for idx, (row, emb) in enumerate(zip(rows, embeddings)):
            source_data = json.dumps(
                {col: _serialize_value(val) for col, val in zip(columns, row)},
                ensure_ascii=False,
            )
            cursor.execute(insert_sql, {
                "source_table": table_name,
                "row_index": idx,
                "source_data": source_data,
                "embedding": json.dumps(emb),
            })
    connection.commit()
    logger.info("Saved %d embeddings to Oracle table %s", len(embeddings), target_table)


def save_to_txt(table_name: str, columns: list[str], rows: list[tuple],
                output_dir: str, processing_config: dict):
    """Save results as a plain text file for Msty Knowledge Base."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = os.path.join(output_dir, f"{table_name}_{timestamp}.txt")

    template = processing_config.get("text_template", "{column_name}: {value}")
    separator = processing_config.get("row_separator", " | ")

    with open(filepath, "w", encoding="utf-8") as f:
        for idx, row in enumerate(rows):
            parts = []
            for col_name, value in zip(columns, row):
                if value is not None:
                    str_value = _format_value(value)
                    parts.append(template.format(column_name=col_name, value=str_value))
            text = separator.join(parts)
            f.write(text + "\n\n")

    logger.info("Saved TXT: %s (%d rows)", filepath, len(rows))


def save_to_markdown(table_name: str, columns: list[str], rows: list[tuple],
                     output_dir: str, processing_config: dict):
    """Save results as a Markdown file for Msty Knowledge Base."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = os.path.join(output_dir, f"{table_name}_{timestamp}.md")

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(f"# {table_name}\n\n")
        f.write(f"Source: Oracle Table `{table_name}` | Columns: {', '.join(columns)}\n\n")
        f.write("---\n\n")

        for idx, row in enumerate(rows):
            f.write(f"## Record {idx + 1}\n\n")
            for col_name, value in zip(columns, row):
                str_value = _format_value(value) if value is not None else "NULL"
                f.write(f"- **{col_name}**: {str_value}\n")
            f.write("\n")

    logger.info("Saved Markdown: %s (%d rows)", filepath, len(rows))


def _format_value(value) -> str:
    """Format a value to string for text output."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    return str(value)


def _serialize_value(value):
    """Convert a value to a JSON-serializable type."""
    if value is None:
        return None
    if isinstance(value, (datetime,)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    return value
