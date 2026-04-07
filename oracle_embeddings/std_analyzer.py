import logging
import re
from collections import defaultdict

logger = logging.getLogger(__name__)


def analyze_all(schema: dict, joins: list[dict]) -> dict:
    """Run all structural analysis on schema and joins."""
    results = {
        "join_column_mismatch": find_join_column_mismatch(joins),
        "type_inconsistency": find_type_inconsistency(schema),
        "naming_pattern": analyze_naming_patterns(schema),
        "identifier_pattern": analyze_identifier_patterns(schema),
        "code_columns": find_code_columns(schema),
        "yn_columns": find_yn_columns(schema),
    }
    return results


def find_join_column_mismatch(joins: list[dict]) -> list[dict]:
    """Find JOIN relationships where column names differ (same meaning, different name)."""
    mismatches = []
    for j in joins:
        c1 = j["column1"]
        c2 = j["column2"]
        if c1 != c2:
            mismatches.append({
                "table1": j["table1"],
                "column1": c1,
                "table2": j["table2"],
                "column2": c2,
                "join_type": j.get("join_type", ""),
                "source": j.get("source_mapper", ""),
            })

    logger.info("JOIN column mismatches: %d", len(mismatches))
    return mismatches


def find_type_inconsistency(schema: dict) -> list[dict]:
    """Find columns with same name but different types across tables."""
    # Group columns by name
    col_types = defaultdict(list)
    for table in schema.get("tables", []):
        for col in table["columns"]:
            col_types[col["column_name"]].append({
                "table": table["name"],
                "data_type": col["data_type"],
                "nullable": col["nullable"],
            })

    inconsistencies = []
    for col_name, occurrences in col_types.items():
        if len(occurrences) < 2:
            continue

        types = set(o["data_type"] for o in occurrences)
        if len(types) > 1:
            inconsistencies.append({
                "column_name": col_name,
                "occurrences": occurrences,
                "types": sorted(types),
                "table_count": len(occurrences),
            })

    # Sort by number of tables (more widespread = more important)
    inconsistencies.sort(key=lambda x: x["table_count"], reverse=True)
    logger.info("Type inconsistencies: %d columns", len(inconsistencies))
    return inconsistencies


def analyze_naming_patterns(schema: dict) -> dict:
    """Analyze column naming patterns per table (prefix/suffix consistency)."""
    violations = []

    for table in schema.get("tables", []):
        cols = [c["column_name"] for c in table["columns"]]
        if len(cols) < 3:
            continue

        # Detect common prefix
        prefixes = defaultdict(list)
        for col in cols:
            parts = col.split("_")
            if len(parts) >= 2:
                prefixes[parts[0]].append(col)

        # Find dominant prefix (if any)
        if prefixes:
            dominant_prefix, dominant_cols = max(prefixes.items(), key=lambda x: len(x[1]))
            ratio = len(dominant_cols) / len(cols)

            if ratio >= 0.4 and ratio < 1.0:
                # Some columns don't follow the dominant prefix
                outliers = [c for c in cols if not c.startswith(dominant_prefix + "_")]
                if outliers:
                    violations.append({
                        "table": table["name"],
                        "dominant_prefix": dominant_prefix,
                        "prefix_ratio": round(ratio, 2),
                        "total_columns": len(cols),
                        "conforming_columns": len(dominant_cols),
                        "outlier_columns": outliers,
                    })

    logger.info("Naming pattern violations: %d tables", len(violations))
    return {"violations": violations}


def analyze_identifier_patterns(schema: dict) -> dict:
    """Analyze PK/FK naming patterns (_ID, _NO, _CD, _SEQ, etc.)."""
    pk_patterns = defaultdict(list)
    all_pks = []

    for table in schema.get("tables", []):
        for pk_col in table.get("primary_keys", []):
            all_pks.append({"table": table["name"], "column": pk_col})

            # Extract suffix
            parts = pk_col.split("_")
            if len(parts) >= 2:
                suffix = "_" + parts[-1]
                pk_patterns[suffix].append({"table": table["name"], "column": pk_col})
            else:
                pk_patterns["(no suffix)"].append({"table": table["name"], "column": pk_col})

    # Sort patterns by count
    sorted_patterns = sorted(pk_patterns.items(), key=lambda x: len(x[1]), reverse=True)

    logger.info("PK patterns: %d unique suffixes from %d PKs", len(pk_patterns), len(all_pks))
    return {
        "total_pks": len(all_pks),
        "patterns": [
            {
                "suffix": suffix,
                "count": len(tables),
                "examples": [t["table"] + "." + t["column"] for t in tables[:5]],
            }
            for suffix, tables in sorted_patterns
        ],
    }


def find_code_columns(schema: dict) -> list[dict]:
    """Find columns that look like code/type columns (_CD, _TYPE, _CODE, _GB, _GBN, _CL)."""
    code_suffixes = ("_CD", "_TYPE", "_CODE", "_GB", "_GBN", "_CL", "_KIND", "_GUBUN", "_DIV")
    code_columns = []

    for table in schema.get("tables", []):
        for col in table["columns"]:
            col_name = col["column_name"].upper()
            if any(col_name.endswith(s) for s in code_suffixes):
                code_columns.append({
                    "table": table["name"],
                    "column": col["column_name"],
                    "data_type": col["data_type"],
                    "comment": col.get("comment", ""),
                })

    logger.info("Code columns found: %d", len(code_columns))
    return code_columns


def find_yn_columns(schema: dict) -> list[dict]:
    """Find Y/N flag columns (_YN, _FLAG, _TF)."""
    yn_suffixes = ("_YN", "_FLAG", "_TF", "_AT")
    yn_columns = []

    for table in schema.get("tables", []):
        for col in table["columns"]:
            col_name = col["column_name"].upper()
            if any(col_name.endswith(s) for s in yn_suffixes):
                yn_columns.append({
                    "table": table["name"],
                    "column": col["column_name"],
                    "data_type": col["data_type"],
                    "comment": col.get("comment", ""),
                })

    logger.info("Y/N columns found: %d", len(yn_columns))
    return yn_columns
