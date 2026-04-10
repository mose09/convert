import logging
import os
import re
from collections import defaultdict

logger = logging.getLogger(__name__)

# 무시할 일반적인 단어 (프로그래밍/SQL 키워드)
STOP_WORDS = {
    # 프로그래밍 공통
    "GET", "SET", "IS", "HAS", "CAN", "DO", "ON", "TO", "BY", "IN", "OF",
    "THE", "AND", "OR", "NOT", "FOR", "WITH", "FROM", "THIS", "THAT",
    "NEW", "OLD", "ALL", "ANY", "USE", "USED",
    # React/JS 공통
    "PROPS", "STATE", "RENDER", "RETURN", "EXPORT", "DEFAULT", "IMPORT",
    "CONST", "LET", "VAR", "FUNCTION", "ASYNC", "AWAIT", "CLASS",
    "COMPONENT", "HOOK", "EFFECT", "REF", "MEMO", "CALLBACK",
    "HANDLE", "HANDLER", "EVENT", "CLICK", "CHANGE", "SUBMIT",
    "TRUE", "FALSE", "NULL", "UNDEFINED",
    # HTML/CSS
    "DIV", "SPAN", "INPUT", "BUTTON", "FORM", "TABLE", "MODAL",
    "HEADER", "FOOTER", "WRAPPER", "CONTAINER", "CONTENT", "LAYOUT",
    "STYLE", "STYLED", "CSS", "WIDTH", "HEIGHT", "COLOR",
    # 일반 프로그래밍
    "INDEX", "KEY", "VALUE", "ITEM", "ITEMS", "ARRAY", "OBJECT",
    "MAP", "FILTER", "REDUCE", "FIND", "SORT", "PUSH", "POP",
    "LENGTH", "SIZE", "COUNT", "TOTAL", "MAX", "MIN",
    "INIT", "LOAD", "FETCH", "SEND", "POST", "PUT", "DELETE",
    "REQUEST", "RESPONSE", "ERROR", "SUCCESS", "FAIL", "RESULT",
    "DATA", "INFO", "TYPE", "TYPES", "ENUM", "INTERFACE",
    "ID", "IDX", "NUM", "NO", "SEQ",
    # 접두/접미어
    "TB", "TBL", "VW", "IF", "FN", "SP", "PK", "FK", "IX",
}

# DB 컬럼에서 흔한 접미어 (단독 사용 시 무시)
DB_SUFFIXES = {"CD", "NM", "NO", "DT", "YN", "ST", "SN", "GB", "TY", "AT", "ID", "SEQ", "AMT", "QTY", "RT", "CT"}


def collect_from_schema(md_path: str) -> dict[str, dict]:
    """Collect words from schema .md file (table names + column names)."""
    from .md_parser import parse_schema_md

    schema = parse_schema_md(md_path)
    words = defaultdict(lambda: {"db_count": 0, "fe_count": 0, "sources": set()})

    for table in schema.get("tables", []):
        # Split table name
        table_parts = _split_identifier(table["name"])
        for part in table_parts:
            if _is_valid_word(part):
                words[part]["db_count"] += 1
                words[part]["sources"].add(f"TABLE:{table['name']}")

        # Split column names
        for col in table["columns"]:
            col_parts = _split_identifier(col["column_name"])
            for part in col_parts:
                if _is_valid_word(part):
                    words[part]["db_count"] += 1
                    words[part]["sources"].add(f"COL:{table['name']}.{col['column_name']}")

    logger.info("Schema words collected: %d unique words", len(words))
    return dict(words)


def collect_from_react(react_dir: str) -> dict[str, dict]:
    """Collect words from React source files (.js, .jsx, .ts, .tsx)."""
    words = defaultdict(lambda: {"db_count": 0, "fe_count": 0, "sources": set()})

    extensions = {".js", ".jsx", ".ts", ".tsx"}
    file_count = 0

    for root, dirs, files in os.walk(react_dir):
        # Skip node_modules, build, dist
        dirs[:] = [d for d in dirs if d not in ("node_modules", "build", "dist", ".next", ".git")]

        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext not in extensions:
                continue

            filepath = os.path.join(root, f)
            file_count += 1

            try:
                with open(filepath, "r", encoding="utf-8", errors="ignore") as fh:
                    content = fh.read()
            except Exception:
                continue

            rel_path = os.path.relpath(filepath, react_dir)
            file_words = _extract_words_from_source(content)

            for word in file_words:
                if _is_valid_word(word):
                    words[word]["fe_count"] += 1
                    words[word]["sources"].add(f"FE:{rel_path}")

    logger.info("React words collected: %d unique words from %d files", len(words), file_count)
    return dict(words)


def merge_words(schema_words: dict, react_words: dict) -> list[dict]:
    """Merge schema and react words into a unified list."""
    all_keys = set(schema_words.keys()) | set(react_words.keys())

    merged = []
    for word in sorted(all_keys):
        sw = schema_words.get(word, {"db_count": 0, "fe_count": 0, "sources": set()})
        rw = react_words.get(word, {"db_count": 0, "fe_count": 0, "sources": set()})

        db_count = sw.get("db_count", 0)
        fe_count = rw.get("fe_count", 0)
        sources = set()
        if isinstance(sw.get("sources"), set):
            sources |= sw["sources"]
        if isinstance(rw.get("sources"), set):
            sources |= rw["sources"]

        merged.append({
            "word": word,
            "db_count": db_count,
            "fe_count": fe_count,
            "total_count": db_count + fe_count,
            "source_count": len(sources),
            "sample_sources": sorted(sources)[:5],
        })

    # Sort by total count descending
    merged.sort(key=lambda x: x["total_count"], reverse=True)

    logger.info("Merged: %d unique words (DB: %d, FE: %d, Both: %d)",
                len(merged),
                sum(1 for w in merged if w["db_count"] > 0 and w["fe_count"] == 0),
                sum(1 for w in merged if w["fe_count"] > 0 and w["db_count"] == 0),
                sum(1 for w in merged if w["db_count"] > 0 and w["fe_count"] > 0))

    return merged


def _split_identifier(name: str) -> list[str]:
    """Split an identifier into words.

    Handles:
    - SNAKE_CASE: CUST_ORDER_DT → [CUST, ORDER, DT]
    - camelCase: customerOrder → [CUSTOMER, ORDER]
    - PascalCase: CustomerOrder → [CUSTOMER, ORDER]
    """
    # First split by underscore
    parts = name.split("_")

    words = []
    for part in parts:
        if not part:
            continue
        # Split camelCase/PascalCase
        camel_parts = re.findall(r'[A-Z]?[a-z]+|[A-Z]+(?=[A-Z][a-z]|\b)', part)
        if camel_parts:
            words.extend(p.upper() for p in camel_parts)
        else:
            words.append(part.upper())

    return words


def _extract_words_from_source(content: str) -> set:
    """Extract meaningful words from JavaScript/TypeScript source code."""
    words = set()

    # Remove string literals and comments
    content = re.sub(r'//[^\n]*', '', content)
    content = re.sub(r'/\*.*?\*/', '', content, flags=re.DOTALL)
    content = re.sub(r'"[^"]*"', '', content)
    content = re.sub(r"'[^']*'", '', content)
    content = re.sub(r'`[^`]*`', '', content, flags=re.DOTALL)

    # Extract identifiers (variable names, function names, etc.)
    identifiers = re.findall(r'\b[a-zA-Z_][a-zA-Z0-9_]{2,}\b', content)

    for ident in identifiers:
        parts = _split_identifier(ident)
        for part in parts:
            words.add(part)

    return words


def _is_valid_word(word: str) -> bool:
    """Check if a word is worth collecting."""
    word = word.upper()
    if len(word) < 2:
        return False
    if word in STOP_WORDS:
        return False
    if word.isdigit():
        return False
    return True
