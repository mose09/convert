import os
import logging

import oracledb

logger = logging.getLogger(__name__)


def get_connection(config: dict) -> oracledb.Connection:
    """Create an Oracle database connection."""
    oracle_cfg = config.get("oracle", {})

    lib_dir = os.environ.get("ORACLE_INSTANT_CLIENT_DIR") or oracle_cfg.get("instant_client_dir")
    if lib_dir:
        try:
            oracledb.init_oracle_client(lib_dir=lib_dir)
            logger.info("Thick mode initialized (Oracle Instant Client)")
        except oracledb.ProgrammingError:
            pass  # Already initialized
        except Exception as e:
            logger.error("Oracle Instant Client init failed: %s", e)
            print(f"Error: Oracle Instant Client 초기화 실패 - {e}")
            print(f"  경로를 확인하세요: {lib_dir}")
            raise

    user = os.environ.get("ORACLE_USER") or oracle_cfg.get("user", "")
    password = os.environ["ORACLE_PASSWORD"]
    dsn = os.environ.get("ORACLE_DSN") or oracle_cfg.get("dsn", "")

    logger.info("Connecting to Oracle: %s@%s", user, dsn)
    connection = oracledb.connect(user=user, password=password, dsn=dsn)
    logger.info("Connected successfully (DB version: %s)", connection.version)
    return connection


def execute_query(connection, sql: str, params: dict = None) -> tuple[list[str], list[tuple]]:
    """Execute a SQL query and return (column_names, rows)."""
    with connection.cursor() as cursor:
        cursor.execute(sql, params or {})
        columns = [col[0] for col in cursor.description]
        rows = cursor.fetchall()
    return columns, rows
