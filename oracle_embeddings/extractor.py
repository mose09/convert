import logging

from .db import execute_query

logger = logging.getLogger(__name__)

EXCLUDED_TYPES = {"BLOB", "BFILE", "RAW", "LONG RAW", "XMLTYPE"}


def get_column_metadata(connection, table_name: str) -> list[dict]:
    """Get column metadata from Oracle data dictionary."""
    sql = """
        SELECT column_name, data_type, nullable, data_length
        FROM all_tab_columns
        WHERE table_name = :table_name
        ORDER BY column_id
    """
    columns, rows = execute_query(connection, sql, {"table_name": table_name.upper()})
    metadata = []
    for row in rows:
        metadata.append({
            "column_name": row[0],
            "data_type": row[1],
            "nullable": row[2],
            "data_length": row[3],
        })
    return metadata


def extract_rows(connection, table_cfg: dict, row_limit: int = None) -> tuple[list[str], list[tuple]]:
    """Extract rows from a table, auto-discovering columns if not specified."""
    table_name = table_cfg["name"]
    specified_columns = table_cfg.get("columns")

    if specified_columns:
        columns = specified_columns
    else:
        metadata = get_column_metadata(connection, table_name)
        columns = [
            m["column_name"] for m in metadata
            if m["data_type"] not in EXCLUDED_TYPES
        ]
        logger.info("Auto-discovered %d columns for %s", len(columns), table_name)

    sql = build_select_sql(table_name, columns, row_limit)
    logger.info("Extracting data: %s", sql)
    _, rows = execute_query(connection, sql)
    logger.info("Extracted %d rows from %s", len(rows), table_name)
    return columns, rows


def build_select_sql(table_name: str, columns: list[str], row_limit: int = None) -> str:
    """Build a SELECT statement for the given table and columns.
    Uses ROWNUM for Oracle 11g compatibility (FETCH FIRST is 12c+).
    """
    col_list = ", ".join(f'"{c}"' for c in columns)
    sql = f'SELECT {col_list} FROM "{table_name}"'
    if row_limit:
        sql += f" WHERE ROWNUM <= {int(row_limit)}"
    return sql
