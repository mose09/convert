import logging

from .db import execute_query

logger = logging.getLogger(__name__)


def extract_table_list(connection, owner: str) -> list[str]:
    """Get all table names for the given owner."""
    sql = """
        SELECT table_name
        FROM all_tables
        WHERE owner = :owner
        ORDER BY table_name
    """
    _, rows = execute_query(connection, sql, {"owner": owner.upper()})
    return [row[0] for row in rows]


def extract_table_comments(connection, owner: str) -> dict[str, str]:
    """Get table comments."""
    sql = """
        SELECT table_name, comments
        FROM all_tab_comments
        WHERE owner = :owner AND comments IS NOT NULL
    """
    _, rows = execute_query(connection, sql, {"owner": owner.upper()})
    return {row[0]: row[1] for row in rows}


def extract_column_metadata(connection, owner: str, table_name: str) -> list[dict]:
    """Get column metadata including comments."""
    sql = """
        SELECT c.column_name, c.data_type, c.data_length, c.data_precision,
               c.data_scale, c.nullable, c.data_default,
               cc.comments
        FROM all_tab_columns c
        LEFT JOIN all_col_comments cc
            ON c.owner = cc.owner
            AND c.table_name = cc.table_name
            AND c.column_name = cc.column_name
        WHERE c.owner = :owner AND c.table_name = :table_name
        ORDER BY c.column_id
    """
    _, rows = execute_query(connection, sql, {
        "owner": owner.upper(),
        "table_name": table_name.upper(),
    })
    columns = []
    for row in rows:
        col = {
            "column_name": row[0],
            "data_type": _format_data_type(row[1], row[2], row[3], row[4]),
            "nullable": "Y" if row[5] == "Y" else "N",
            "data_default": str(row[6]).strip() if row[6] else None,
            "comment": row[7],
        }
        columns.append(col)
    return columns


def extract_primary_keys(connection, owner: str, table_name: str) -> list[str]:
    """Get primary key columns for a table."""
    sql = """
        SELECT cc.column_name
        FROM all_constraints c
        JOIN all_cons_columns cc
            ON c.owner = cc.owner
            AND c.constraint_name = cc.constraint_name
        WHERE c.owner = :owner
            AND c.table_name = :table_name
            AND c.constraint_type = 'P'
        ORDER BY cc.position
    """
    _, rows = execute_query(connection, sql, {
        "owner": owner.upper(),
        "table_name": table_name.upper(),
    })
    return [row[0] for row in rows]


def extract_foreign_keys(connection, owner: str, table_name: str) -> list[dict]:
    """Get foreign key relationships for a table."""
    sql = """
        SELECT cc.column_name,
               r_c.table_name AS ref_table,
               rcc.column_name AS ref_column,
               c.constraint_name
        FROM all_constraints c
        JOIN all_cons_columns cc
            ON c.owner = cc.owner
            AND c.constraint_name = cc.constraint_name
        JOIN all_constraints r_c
            ON c.r_owner = r_c.owner
            AND c.r_constraint_name = r_c.constraint_name
        JOIN all_cons_columns rcc
            ON r_c.owner = rcc.owner
            AND r_c.constraint_name = rcc.constraint_name
            AND cc.position = rcc.position
        WHERE c.owner = :owner
            AND c.table_name = :table_name
            AND c.constraint_type = 'R'
        ORDER BY c.constraint_name, cc.position
    """
    _, rows = execute_query(connection, sql, {
        "owner": owner.upper(),
        "table_name": table_name.upper(),
    })
    fks = []
    for row in rows:
        fks.append({
            "column": row[0],
            "ref_table": row[1],
            "ref_column": row[2],
            "constraint_name": row[3],
        })
    return fks


def extract_indexes(connection, owner: str, table_name: str) -> list[dict]:
    """Get index information for a table."""
    sql = """
        SELECT i.index_name, i.uniqueness,
               ic.column_name, ic.column_position
        FROM all_indexes i
        JOIN all_ind_columns ic
            ON i.owner = ic.index_owner
            AND i.index_name = ic.index_name
        WHERE i.table_owner = :owner
            AND i.table_name = :table_name
        ORDER BY i.index_name, ic.column_position
    """
    _, rows = execute_query(connection, sql, {
        "owner": owner.upper(),
        "table_name": table_name.upper(),
    })
    indexes = {}
    for row in rows:
        idx_name = row[0]
        if idx_name not in indexes:
            indexes[idx_name] = {
                "name": idx_name,
                "unique": row[1] == "UNIQUE",
                "columns": [],
            }
        indexes[idx_name]["columns"].append(row[2])
    return list(indexes.values())


def extract_schema(connection, owner: str, table_names: list[str] = None) -> dict:
    """Extract full schema information for the given owner/tables."""
    if table_names:
        tables = [t.upper() for t in table_names]
    else:
        tables = extract_table_list(connection, owner)
        logger.info("Auto-discovered %d tables for owner %s", len(tables), owner)

    table_comments = extract_table_comments(connection, owner)

    schema = {"owner": owner.upper(), "tables": []}
    for table_name in tables:
        logger.info("Extracting schema: %s.%s", owner, table_name)
        table_info = {
            "name": table_name,
            "comment": table_comments.get(table_name),
            "columns": extract_column_metadata(connection, owner, table_name),
            "primary_keys": extract_primary_keys(connection, owner, table_name),
            "foreign_keys": extract_foreign_keys(connection, owner, table_name),
            "indexes": extract_indexes(connection, owner, table_name),
        }
        schema["tables"].append(table_info)

    logger.info("Extracted schema for %d tables", len(schema["tables"]))
    return schema


def _format_data_type(data_type: str, length: int, precision: int, scale: int) -> str:
    """Format Oracle data type with size info."""
    if data_type in ("VARCHAR2", "CHAR", "NVARCHAR2", "NCHAR"):
        return f"{data_type}({length})"
    elif data_type == "NUMBER":
        if precision and scale:
            return f"NUMBER({precision},{scale})"
        elif precision:
            return f"NUMBER({precision})"
        return "NUMBER"
    elif data_type == "FLOAT":
        if precision:
            return f"FLOAT({precision})"
        return "FLOAT"
    return data_type
