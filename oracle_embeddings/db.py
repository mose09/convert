import os
import logging

import oracledb

logger = logging.getLogger(__name__)


def get_connection(config: dict) -> oracledb.Connection:
    """Create an Oracle database connection."""
    oracle_cfg = config["oracle"]

    if oracle_cfg.get("thick_mode"):
        oracledb.init_oracle_client()

    user = os.environ.get("ORACLE_USER", oracle_cfg.get("user", ""))
    password = os.environ["ORACLE_PASSWORD"]
    dsn = oracle_cfg["dsn"]

    logger.info("Connecting to Oracle: %s@%s", user, dsn)
    connection = oracledb.connect(user=user, password=password, dsn=dsn)
    logger.info("Connected successfully")
    return connection


def execute_query(connection, sql: str, params: dict = None) -> tuple[list[str], list[tuple]]:
    """Execute a SQL query and return (column_names, rows)."""
    with connection.cursor() as cursor:
        cursor.execute(sql, params or {})
        columns = [col[0] for col in cursor.description]
        rows = cursor.fetchall()
    return columns, rows
