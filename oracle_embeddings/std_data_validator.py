import logging

from .db import execute_query

logger = logging.getLogger(__name__)

MAX_DISTINCT = 50  # DISTINCT 결과 최대 수집 개수


def validate_code_columns(connection, code_columns: list[dict]) -> list[dict]:
    """Query actual data for code-type columns to get distinct values."""
    results = []
    total = len(code_columns)

    for i, col_info in enumerate(code_columns):
        table = col_info["table"]
        column = col_info["column"]

        try:
            sql = (
                f'SELECT "{column}", COUNT(*) AS cnt '
                f'FROM "{table}" '
                f'GROUP BY "{column}" '
                f'ORDER BY COUNT(*) DESC'
            )
            _, rows = execute_query(connection, sql)

            distinct_values = []
            total_rows = 0
            null_count = 0
            for row in rows:
                val, cnt = row[0], row[1]
                total_rows += cnt
                if val is None:
                    null_count = cnt
                else:
                    distinct_values.append({"value": str(val), "count": cnt})

            results.append({
                "table": table,
                "column": column,
                "data_type": col_info["data_type"],
                "comment": col_info.get("comment", ""),
                "total_rows": total_rows,
                "distinct_count": len(distinct_values),
                "null_count": null_count,
                "values": distinct_values[:MAX_DISTINCT],
            })
        except Exception as e:
            logger.warning("Failed to query %s.%s: %s", table, column, e)
            results.append({
                "table": table,
                "column": column,
                "data_type": col_info["data_type"],
                "comment": col_info.get("comment", ""),
                "error": str(e),
            })

        if (i + 1) % 20 == 0 or i + 1 == total:
            print(f"    [{i + 1}/{total}] Code columns validated")

    logger.info("Code column validation: %d columns", len(results))
    return results


def validate_yn_columns(connection, yn_columns: list[dict]) -> list[dict]:
    """Check Y/N columns for abnormal values (not Y/N)."""
    results = []
    total = len(yn_columns)

    for i, col_info in enumerate(yn_columns):
        table = col_info["table"]
        column = col_info["column"]

        try:
            sql = (
                f'SELECT "{column}", COUNT(*) AS cnt '
                f'FROM "{table}" '
                f'GROUP BY "{column}" '
                f'ORDER BY COUNT(*) DESC'
            )
            _, rows = execute_query(connection, sql)

            value_dist = {}
            total_rows = 0
            for row in rows:
                val, cnt = row[0], row[1]
                total_rows += cnt
                key = str(val) if val is not None else "NULL"
                value_dist[key] = cnt

            # Check for abnormal values
            normal_keys = {"Y", "N", "1", "0", "TRUE", "FALSE"}
            abnormal = {k: v for k, v in value_dist.items()
                        if k not in normal_keys and k != "NULL"}

            results.append({
                "table": table,
                "column": column,
                "data_type": col_info["data_type"],
                "comment": col_info.get("comment", ""),
                "total_rows": total_rows,
                "distribution": value_dist,
                "has_null": "NULL" in value_dist,
                "null_count": value_dist.get("NULL", 0),
                "has_abnormal": len(abnormal) > 0,
                "abnormal_values": abnormal,
            })
        except Exception as e:
            logger.warning("Failed to query %s.%s: %s", table, column, e)
            results.append({
                "table": table,
                "column": column,
                "data_type": col_info["data_type"],
                "comment": col_info.get("comment", ""),
                "error": str(e),
            })

        if (i + 1) % 20 == 0 or i + 1 == total:
            print(f"    [{i + 1}/{total}] Y/N columns validated")

    logger.info("Y/N column validation: %d columns", len(results))
    return results


def validate_column_usage(connection, schema: dict, sample_tables: list[str] = None) -> list[dict]:
    """Check actual data length, NULL ratio for columns to find unused/oversized columns."""
    results = []
    tables = schema.get("tables", [])

    if sample_tables:
        tables = [t for t in tables if t["name"] in sample_tables]

    total = len(tables)

    for i, table in enumerate(tables):
        table_name = table["name"]

        for col in table["columns"]:
            col_name = col["column_name"]
            data_type = col["data_type"]

            try:
                # Get NULL ratio and max length
                if "VARCHAR" in data_type.upper() or "CHAR" in data_type.upper():
                    sql = (
                        f'SELECT COUNT(*) AS total, '
                        f'SUM(CASE WHEN "{col_name}" IS NULL THEN 1 ELSE 0 END) AS null_cnt, '
                        f'MAX(LENGTH("{col_name}")) AS max_len '
                        f'FROM "{table_name}" WHERE ROWNUM <= 10000'
                    )
                else:
                    sql = (
                        f'SELECT COUNT(*) AS total, '
                        f'SUM(CASE WHEN "{col_name}" IS NULL THEN 1 ELSE 0 END) AS null_cnt, '
                        f'0 AS max_len '
                        f'FROM "{table_name}" WHERE ROWNUM <= 10000'
                    )

                _, rows = execute_query(connection, sql)
                if rows:
                    total_rows, null_cnt, max_len = rows[0]
                    null_ratio = round(null_cnt / total_rows * 100, 1) if total_rows > 0 else 0

                    # Flag potentially unused columns
                    is_unused = null_ratio == 100.0
                    # Flag oversized columns
                    defined_len = _extract_length(data_type)
                    is_oversized = (max_len is not None and defined_len is not None
                                    and max_len > 0 and defined_len > max_len * 3
                                    and defined_len >= 50)

                    if is_unused or is_oversized:
                        results.append({
                            "table": table_name,
                            "column": col_name,
                            "data_type": data_type,
                            "comment": col.get("comment", ""),
                            "total_rows": total_rows,
                            "null_ratio": null_ratio,
                            "max_length": max_len,
                            "defined_length": defined_len,
                            "is_unused": is_unused,
                            "is_oversized": is_oversized,
                        })
            except Exception as e:
                logger.debug("Failed to validate %s.%s: %s", table_name, col_name, e)
                continue

        if (i + 1) % 10 == 0 or i + 1 == total:
            print(f"    [{i + 1}/{total}] Tables validated for column usage")

    logger.info("Column usage validation: %d issues found", len(results))
    return results


def _extract_length(data_type: str) -> int:
    """Extract defined length from data type string like VARCHAR2(100)."""
    import re
    match = re.search(r'\((\d+)', data_type)
    if match:
        return int(match.group(1))
    return None
