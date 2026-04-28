import logging
import os
import re
import xml.etree.ElementTree as ET

logger = logging.getLogger(__name__)


# Optional file-content cache. analyze-legacy 의 frontend 스캔 단계는 같은
# 파일을 router/import-graph/api-scanner/trigger 등 4~5 단계가 각각 다시
# 읽어서 디스크 I/O 가 dominant 비용. ``file_cache_scope`` 컨텍스트 안에서
# 만 켜고 끝나면 즉시 비워서 메모리 폭증 방지.
_FILE_CONTENT_CACHE: dict[tuple[str, "int | None"], str] = {}
# scan_react_dir / _scan_dir 결과 캐시 — 같은 root 를 여러 번 walk 하는
# 비용을 한 번으로 줄임. file content cache 와 같은 토글로 lifecycle 관리.
_DIR_SCAN_CACHE: dict[tuple[str, str], list[str]] = {}
_CACHE_ENABLED = False


def use_file_cache(enable: bool) -> None:
    """Toggle in-memory file content + dir scan caches. Disabling clears them."""
    global _CACHE_ENABLED
    _CACHE_ENABLED = enable
    if not enable:
        _FILE_CONTENT_CACHE.clear()
        _DIR_SCAN_CACHE.clear()


class file_cache_scope:
    """Context manager to enable cache during a phase.

    Usage::

        with file_cache_scope():
            build_frontend_url_map_multi(...)
            # downstream calls share file reads
    """

    def __enter__(self):
        use_file_cache(True)
        return self

    def __exit__(self, *exc):
        use_file_cache(False)
        return False


def _read_file_safe(filepath: str, limit: int = None) -> str:
    """Read a file trying multiple encodings, optionally cached."""
    if _CACHE_ENABLED:
        key = (filepath, limit)
        cached = _FILE_CONTENT_CACHE.get(key)
        if cached is not None:
            return cached
    for encoding in ("utf-8", "euc-kr", "cp949", "latin-1"):
        try:
            with open(filepath, "r", encoding=encoding) as f:
                content = f.read(limit) if limit else f.read()
            break
        except (UnicodeDecodeError, UnicodeError):
            continue
    else:
        # Final fallback
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read(limit) if limit else f.read()
    if _CACHE_ENABLED:
        _FILE_CONTENT_CACHE[(filepath, limit)] = content
    return content


# Directories we always skip while scanning for mapper XML. We only prune
# hidden tool/VCS directories and ``node_modules`` — **never** build-output
# names like ``target`` / ``build`` / ``bin`` / ``out`` / ``dist``, because
# real monorepo subprojects sometimes happen to have folders with those
# exact names, and any XML that isn't a real mapper is already filtered
# out later by ``_is_sql_mapper``. Keeping the skip list minimal avoids
# the "내 하위 프로젝트 mapper 가 안 잡힘" regression.
_MYBATIS_SKIP_DIRS = {".git", ".gradle", ".idea", ".svn", ".hg",
                      ".next", "node_modules"}


# Build-output **path fragments** (vs raw directory names). Maven copies
# every ``src/main/resources/*.xml`` mapper into ``target/classes/...``
# during ``mvn compile`` / ``mvn package``, and Gradle does the same
# under ``build/resources/main/``. These copies have IDENTICAL content
# so they cause:
#   * each statement counted twice in stats
#   * ``namespace_to_xml_files`` containing both the source path AND the
#     output path (Programs sheet then lists 2 XMLs for the same SQL id)
#   * downstream analyzers (e.g. column_usage / table cross-ref) inflated
#
# Filtering by directory NAME alone (``target``/``build``) would break
# monorepos where a real subproject happens to be named ``target``. We
# instead match the **deeper sub-path** (``/target/classes/``) which is
# universally a build output and never user-authored. Path is normalized
# to forward slashes first so the same fragments match on Windows
# (``\target\classes\``) and Linux/macOS.
_BUILD_OUTPUT_PATH_FRAGMENTS = (
    "/target/classes/",
    "/target/test-classes/",
    "/target/generated-resources/",
    "/build/classes/",
    "/build/resources/main/",
    "/build/resources/test/",
    "/build/generated/",
    "/out/production/",          # IntelliJ default Gradle output
    "/out/test/",
    "/bin/main/",                # Eclipse compiled output
    "/bin/test/",
)


def _is_build_output(path: str) -> bool:
    """Return True if ``path`` lies inside a Maven/Gradle/IDE build output.

    Normalises backslashes so Windows paths match the same fragments
    declared in :data:`_BUILD_OUTPUT_PATH_FRAGMENTS`.
    """
    normalized = path.replace("\\", "/")
    return any(frag in normalized for frag in _BUILD_OUTPUT_PATH_FRAGMENTS)


def scan_mybatis_dir(base_dir: str) -> list[str]:
    """Find all MyBatis/iBatis mapper XML files recursively.

    Skips typical build-output and VCS directories so that a project-root
    path can be passed safely (the legacy analyzer does this). Each
    candidate XML is still validated via ``_is_sql_mapper`` to filter out
    pom.xml, config files, and other non-mapper XML.

    Build-output paths (``/target/classes/``, ``/build/resources/main/``,
    ``/out/production/`` 등) 은 Maven/Gradle 가 ``src/main/resources``
    의 mapper XML 을 그대로 복사한 것이라 동일 statement 가 두 번
    파싱되어 namespace 인덱스에 중복 등록 + 통계 부풀림을 일으킨다.
    이걸 :func:`_is_build_output` 로 추가 필터.
    """
    xml_files = []
    skipped_build_outputs = 0
    for root, dirs, files in os.walk(base_dir):
        dirs[:] = [d for d in dirs if d.lower() not in _MYBATIS_SKIP_DIRS]
        for f in files:
            if f.endswith(".xml"):
                filepath = os.path.join(root, f)
                if _is_build_output(filepath):
                    skipped_build_outputs += 1
                    continue
                if _is_sql_mapper(filepath):
                    xml_files.append(filepath)
    logger.info("Found %d mapper files (MyBatis + iBatis) in %s", len(xml_files), base_dir)
    if skipped_build_outputs:
        logger.info("Skipped %d XML(s) from build outputs "
                    "(target/classes, build/resources, etc.)",
                    skipped_build_outputs)
        print(f"  Skipped {skipped_build_outputs} duplicate XML(s) from build outputs "
              f"(target/classes, build/resources, etc.)")
    return xml_files


def _is_sql_mapper(filepath: str) -> bool:
    """Check if an XML file is a MyBatis or iBatis mapper."""
    try:
        head = _read_file_safe(filepath, limit=5000)
        head_lower = head.lower()

        has_sql_tags = ("<select" in head_lower or "<insert" in head_lower or
                        "<update" in head_lower or "<delete" in head_lower)

        # MyBatis: <mapper namespace="...">
        # iBatis: <sqlMap namespace="..."> or <sqlMap>
        has_mapper_root = ("mapper" in head_lower or "sqlmap" in head_lower or
                           "sql-map" in head_lower)

        return has_sql_tags or (has_mapper_root and "namespace" in head_lower)
    except Exception:
        return False


def parse_mapper_file(filepath: str) -> list[dict]:
    """Parse a MyBatis or iBatis mapper XML and extract SQL statements.

    ``<include refid="x">`` 형태 (다른 ``<sql id="x">`` 조각 참조) 도
    pre-built fragment registry 로 inline 치환해서 select/insert/update/
    delete 본문에 실제 SQL 이 들어가도록 한다 — 그렇지 않으면
    ``<select>`` 안이 ``<include>`` 만 있는 경우 sql_text 가 비어 테이블
    추출 0건.
    """
    statements = []
    try:
        # Read and strip DOCTYPE to avoid DTD resolution errors (common in iBatis)
        xml_content = _read_file_safe(filepath)
        xml_content = re.sub(r'<!DOCTYPE[^>]*>', '', xml_content)
        # Remove XML comments <!-- ... -->
        xml_content = re.sub(r'<!--.*?-->', '', xml_content, flags=re.DOTALL)
        root = ET.fromstring(xml_content)
    except ET.ParseError:
        return _parse_mapper_fallback(filepath)

    namespace = root.attrib.get("namespace", "")
    mapper_name = os.path.basename(filepath)

    # 같은 파일 안의 ``<sql id="...">`` 조각 수집. cross-file include
    # (``namespace.frag``) 은 현재 미지원 — 같은 파일 안 정의가 99% 케이스.
    sql_fragments = _build_sql_fragments(root)

    # MyBatis: select/insert/update/delete
    # iBatis: also uses same tags, but may have additional ones like statement, procedure
    for tag in ("select", "insert", "update", "delete", "statement", "procedure"):
        for elem in root.iter(tag):
            stmt_id = elem.attrib.get("id", "unknown")
            sql_text = _extract_sql_text(elem, sql_fragments=sql_fragments)
            if sql_text.strip():
                statements.append({
                    "mapper": mapper_name,
                    "mapper_path": filepath,
                    "namespace": namespace,
                    "id": stmt_id,
                    "type": tag.upper(),
                    "sql": sql_text,
                    "procedures": extract_procedure_calls(sql_text, tag),
                    "column_usage": extract_column_usage(sql_text),
                })

    return statements


def _build_sql_fragments(root) -> dict:
    """Collect ``{sql_id: sql_text}`` for all ``<sql id="x">`` elements.

    Used to expand ``<include refid="x">`` references inline. Same-file
    fragments only (cross-file ``namespace.frag`` references skipped —
    very rare in practice).
    """
    fragments: dict[str, str] = {}
    for sql_elem in root.iter("sql"):
        frag_id = sql_elem.attrib.get("id", "")
        if not frag_id:
            continue
        # Concatenate text + tail of all children, recursively. We don't
        # call _extract_sql_text yet because fragments may include OTHER
        # fragments — handled at expansion time with visited-set guard.
        fragments[frag_id] = _raw_element_text(sql_elem)
    return fragments


def _raw_element_text(elem) -> str:
    """Recursively concatenate text + tail of an element + children.

    Used for sql fragment registration. ``<include>`` 자식은 refid 만
    보존해서 expansion 단계에서 다시 치환되도록 sentinel 형태로 emit.
    """
    parts = []
    if elem.text:
        parts.append(elem.text)
    for child in elem:
        # 다른 fragment 를 참조하는 ``<include refid="x">`` 는 expansion
        # 단계에서 다시 풀어야 하므로 sentinel 으로 보존.
        if child.tag == "include":
            ref = child.attrib.get("refid", "")
            if ref:
                # ref 이름에 underscore / hyphen / dot 들어가도 안전하도록
                # BEGIN/END 마커 사이에 raw ref 보존.
                parts.append(f" __MYBATIS_INC_BEGIN__{ref}__MYBATIS_INC_END__ ")
        else:
            parts.append(_raw_element_text(child))
        if child.tail:
            parts.append(child.tail)
    return " ".join(parts)


# Oracle stored-procedure / package invocation patterns, in priority order.
#
# 1. JDBC escape ``{CALL ...}`` — unambiguous
# 2. Plain ``CALL [schema.]pkg.proc(...)`` — MyBatis-standard stored proc call
# 3. ``EXEC`` / ``EXECUTE [schema.]pkg.proc(...)``
# 4. PL/SQL block: ``BEGIN [schema.]pkg.proc(...); END;`` — we capture the
#    first identifier-call immediately after ``BEGIN`` (typically the main
#    procedure; outer DML inside a block is rare and wouldn't match).
#
# Captured group ``proc`` is the dotted identifier (``schema.pkg.proc``),
# ``[\w.]+`` allows arbitrary depth.
_PROC_CALL_PATTERNS = [
    re.compile(r"\{\s*CALL\s+(?P<proc>[\w.]+)\s*\(", re.IGNORECASE),
    re.compile(r"\bCALL\s+(?P<proc>[\w.]+)\s*\(", re.IGNORECASE),
    re.compile(r"\b(?:EXEC|EXECUTE)\s+(?P<proc>[\w.]+)\s*\(", re.IGNORECASE),
    re.compile(r"\bBEGIN\s+(?P<proc>[\w.]+)\s*\(", re.IGNORECASE),
]

# Oracle PL/SQL built-ins that would masquerade as procedure calls in a
# naive BEGIN capture (e.g. ``BEGIN DBMS_OUTPUT.PUT_LINE('start'); ...``).
# We still emit the main first-level call but skip these to reduce noise.
_PROC_CALL_BUILTINS = frozenset({
    # Oracle noise that shouldn't surface as "business procedure"
    "NULL", "DBMS_OUTPUT.PUT_LINE", "COMMIT", "ROLLBACK", "SAVEPOINT",
})


# ── CRUD body scanner (hybrid detection) ──────────────────────────
#
# MyBatis tag (``<select>`` / ``<insert>`` / ``<update>`` / ``<delete>``)
# captures developer intent but misses:
#   1. ``<select>BEGIN proc_that_updates(); END;</select>`` — tag says R,
#      body actually runs UPDATE
#   2. ``<update>`` containing ``MERGE INTO ...`` — tag says U, body also
#      INSERTs (MERGE = C+U)
#   3. ``<procedure>`` / ``<statement>`` — no tag letter at all
# The analyzer's ``_derive_table_crud`` unions tag letter with
# ``extract_crud_from_sql(sql)`` below so CRUD column reflects both the
# developer's label AND any operation the body actually performs.

_CRUD_KEYWORD_TO_LETTER = {
    "INSERT": "C",
    "UPDATE": "U",
    "DELETE": "D",
    # MERGE bodies always contain both INSERT and UPDATE keywords in
    # their WHEN clauses, so the regex picks up C+U automatically —
    # no special case needed.
    "MERGE": None,
    "SELECT": "R",
}

_CRUD_KEYWORD_RE = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|SELECT)\b",
    re.IGNORECASE,
)

# ``SELECT ... FOR UPDATE`` is a row-lock hint, not a mutation. Strip
# the ``FOR UPDATE`` fragment before keyword scanning so the statement
# stays classified as R only.
_FOR_UPDATE_RE = re.compile(r"\bFOR\s+UPDATE\b(?:\s+OF\s+[\w.,\s]+)?(?:\s+NOWAIT|\s+WAIT\s+\d+)?",
                              re.IGNORECASE)

# Strip string literals + comments before scanning so ``SELECT 'note
# about UPDATE' FROM T`` doesn't falsely flag U. Order: block comments
# first, then line comments, then single-quoted strings (with doubled
# single-quote escape).
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
_STRING_LITERAL_RE = re.compile(r"'(?:''|[^'])*'")


def _strip_sql_noise(sql: str) -> str:
    """Remove block/line comments and string literals from ``sql``.

    Replacements use a single space so tokens stay separable (e.g.
    ``UPDATE'x'FROM`` → ``UPDATE FROM``).
    """
    s = _BLOCK_COMMENT_RE.sub(" ", sql)
    s = _LINE_COMMENT_RE.sub(" ", s)
    s = _STRING_LITERAL_RE.sub(" ", s)
    return s


def extract_crud_from_sql(sql_text: str) -> set[str]:
    """Return the set of CRUD letters implied by the SQL body.

    Letters: ``"C"`` (INSERT) / ``"R"`` (SELECT) / ``"U"`` (UPDATE) /
    ``"D"`` (DELETE). MERGE statements emit both ``"C"`` and ``"U"``
    via the regex naturally (their body contains INSERT + UPDATE
    clauses). ``SELECT ... FOR UPDATE`` is treated as ``R`` only —
    the lock hint's ``UPDATE`` keyword is stripped before scanning.

    String literals + comments are removed first to prevent false
    positives (e.g. ``'UPDATE the record'`` inside a constant).
    """
    if not sql_text:
        return set()
    cleaned = _strip_sql_noise(sql_text)
    cleaned = _FOR_UPDATE_RE.sub(" ", cleaned)
    letters: set[str] = set()
    for m in _CRUD_KEYWORD_RE.finditer(cleaned):
        letter = _CRUD_KEYWORD_TO_LETTER.get(m.group(1).upper())
        if letter:
            letters.add(letter)
    return letters


def extract_procedure_calls(sql_text: str, stmt_tag: str = "") -> list[str]:
    """Return Oracle stored-procedure / package names invoked in ``sql_text``.

    Detects the four common shapes documented on ``_PROC_CALL_PATTERNS``
    (JDBC ``{CALL}``, plain ``CALL``, ``EXEC``/``EXECUTE``, PL/SQL ``BEGIN``).
    Deduplicates while preserving first-seen order so the Programs
    report lists the primary procedure first.

    ``stmt_tag`` lets callers flag that this SQL lived inside an explicit
    ``<procedure>`` MyBatis tag. We don't synthesize a name from the
    statement id alone (it's a MyBatis key, not necessarily the Oracle
    procedure) — we only bump confidence that the regex result is a real
    procedure call. If regex finds nothing but ``stmt_tag == "procedure"``,
    we return ``[]`` so the caller can still see the statement in the
    Programs row via Tables/SQL-ids columns.
    """
    if not sql_text:
        return []
    seen: dict[str, None] = {}
    for pat in _PROC_CALL_PATTERNS:
        for m in pat.finditer(sql_text):
            name = (m.group("proc") or "").strip()
            if not name:
                continue
            canonical = name.upper()
            if canonical in _PROC_CALL_BUILTINS:
                continue
            if canonical not in seen:
                seen[canonical] = None
    return list(seen.keys())


# ── Column-level CRUD extraction (sqlglot AST) ────────────────────
#
# Table-level CRUD (``extract_crud_from_sql``) answers "which operations
# touch this table at all". For the Programs report we also want "which
# columns exactly" so users see e.g.
#
#     ORDERS.id(R), ORDERS.status(U), ORDER_HIST.id(C)
#
# sqlglot parses the SQL into a real AST so we can walk SELECT
# projections / INSERT column lists / UPDATE SET LHS / MERGE WHEN
# clauses. MyBatis ``#{x}`` / ``${x}`` placeholders defeat sqlglot so we
# reuse the masking helper already in ``migration/sql_rewriter.py``.
#
# Parser failure returns an empty dict — the caller should fall back to
# table-level CRUD which already exists (PR #30). No attempt to guess
# column lists for ``SELECT *`` — users can inspect the Tables column
# and the original XML.


def extract_column_usage(sql_text: str) -> dict[str, dict[str, set]]:
    """Return ``{TABLE: {COL: set("C"|"R"|"U"|"D")}}`` for ``sql_text``.

    Table + column identifiers are upper-cased so they align with
    ``extract_table_usage`` output and ``statement_to_tables``. Only
    statements with clearly attributable columns are reported:

    * ``SELECT col1, t.col2 FROM T`` → ``{T: {COL1: R, COL2: R}}``
    * ``INSERT INTO T (a, b) VALUES (...)`` → ``{T: {A: C, B: C}}``
    * ``UPDATE T t SET t.a = ... WHERE ...`` → ``{T: {A: U}}``
    * ``MERGE INTO T ... WHEN MATCHED THEN UPDATE SET T.a = ... WHEN NOT
      MATCHED THEN INSERT (id, a) VALUES (...)`` →
      ``{T: {A: U, ID: C}}``

    For ``SELECT *`` / unqualified columns in multi-table FROM clauses
    we intentionally skip (can't disambiguate without schema). DELETE
    has no column dimension (table-level only). Parse failures return
    ``{}``.
    """
    if not sql_text or not sql_text.strip():
        return {}
    try:  # lazy import — sqlglot is already required by migration/
        import sqlglot
        from sqlglot import expressions as _exp
        from .migration.sql_rewriter import mask_mybatis_placeholders
    except Exception:
        return {}
    safe, _tokens = mask_mybatis_placeholders(sql_text)
    try:
        tree = sqlglot.parse_one(safe, dialect="oracle")
    except Exception:
        return {}
    if tree is None:
        return {}
    out: dict[str, dict[str, set]] = {}
    _walk_for_columns(tree, out)
    return out


def _alias_map(tree) -> dict[str, str]:
    """Return ``{alias_upper: table_upper}`` for every ``exp.Table`` in ``tree``."""
    from sqlglot import expressions as _exp
    m: dict[str, str] = {}
    for t in tree.find_all(_exp.Table):
        name = (t.name or "").upper()
        if not name:
            continue
        alias = (t.alias or "").upper()
        if alias:
            m[alias] = name
        m[name] = name  # table's own name resolves to itself
    return m


def _add_col(out: dict, table: str, col: str, letter: str) -> None:
    if not table or not col or not letter:
        return
    out.setdefault(table.upper(), {}).setdefault(col.upper(), set()).add(letter)


def _first_table(tree) -> str:
    """Best-guess table for unqualified columns: the first FROM table."""
    from sqlglot import expressions as _exp
    for t in tree.find_all(_exp.Table):
        if t.name:
            return t.name.upper()
    return ""


def _resolve_col_table(col, alias_map: dict[str, str], fallback: str) -> str:
    """Return the upper-cased table name for a ``Column`` node."""
    qualifier = (getattr(col, "table", "") or "").upper()
    if qualifier:
        return alias_map.get(qualifier, qualifier)
    return fallback


def _walk_for_columns(tree, out: dict) -> None:
    """Dispatch by statement type. Nested SELECT inside INSERT / MERGE /
    WITH walks recursively so sub-reads are captured too."""
    from sqlglot import expressions as _exp
    if isinstance(tree, _exp.Select):
        _collect_select_cols(tree, out)
    elif isinstance(tree, _exp.Insert):
        _collect_insert_cols(tree, out)
        sub = tree.expression  # exp.Select inside INSERT ... SELECT
        if sub is not None:
            _walk_for_columns(sub, out)
    elif isinstance(tree, _exp.Update):
        _collect_update_cols(tree, out)
    elif isinstance(tree, _exp.Delete):
        # DELETE has no column dimension — table-level handled elsewhere
        return
    elif isinstance(tree, _exp.Merge):
        _collect_merge_cols(tree, out)


def _collect_select_cols(tree, out: dict) -> None:
    from sqlglot import expressions as _exp
    alias_map = _alias_map(tree)
    fallback = _first_table(tree)
    # If there's more than one table in FROM / JOIN we can't safely
    # attribute unqualified columns — drop them to avoid over-reporting.
    tables_in_scope = {t.name.upper() for t in tree.find_all(_exp.Table) if t.name}
    multi_table = len(tables_in_scope) > 1
    for e in tree.expressions:
        col = e.this if isinstance(e, _exp.Alias) else e
        if isinstance(col, _exp.Star) or not isinstance(col, _exp.Column):
            continue
        tbl = _resolve_col_table(col, alias_map, "" if multi_table else fallback)
        _add_col(out, tbl, col.name, "R")


def _collect_insert_cols(tree, out: dict) -> None:
    from sqlglot import expressions as _exp
    target = tree.this
    if isinstance(target, _exp.Schema):
        table = target.this
        name = getattr(table, "name", "") or ""
        for ident in target.expressions:
            _add_col(out, name, getattr(ident, "name", ""), "C")


def _collect_update_cols(tree, out: dict) -> None:
    from sqlglot import expressions as _exp
    alias_map = _alias_map(tree)
    target = tree.this
    main_table = getattr(target, "name", "") or ""
    for e in tree.expressions:
        if not isinstance(e, _exp.EQ):
            continue
        lhs = e.left
        if not isinstance(lhs, _exp.Column):
            continue
        tbl = _resolve_col_table(lhs, alias_map, main_table)
        _add_col(out, tbl, lhs.name, "U")


def _collect_merge_cols(tree, out: dict) -> None:
    from sqlglot import expressions as _exp
    alias_map = _alias_map(tree)
    target = tree.this  # exp.Table (MERGE INTO target)
    main_table = getattr(target, "name", "") or ""
    # USING subquery columns get R attribution if it's a SELECT
    using = tree.args.get("using")
    if using is not None:
        src = using if isinstance(using, _exp.Select) else using.find(_exp.Select)
        if src is not None:
            _collect_select_cols(src, out)
    whens = tree.args.get("whens")
    if whens is None:
        return
    for when in whens.expressions:
        then = when.args.get("then")
        if isinstance(then, _exp.Update):
            for e in then.expressions:
                if isinstance(e, _exp.EQ) and isinstance(e.left, _exp.Column):
                    tbl = _resolve_col_table(e.left, alias_map, main_table)
                    _add_col(out, tbl, e.left.name, "U")
        elif isinstance(then, _exp.Insert):
            inner = then.this
            if isinstance(inner, _exp.Schema):
                for ci in inner.expressions:
                    _add_col(out, main_table, getattr(ci, "name", ""), "C")
            elif isinstance(inner, _exp.Tuple):
                # MERGE INSERT can appear as (col, col, col) tuple
                for ci in inner.expressions:
                    if isinstance(ci, _exp.Column):
                        tbl = _resolve_col_table(ci, alias_map, main_table)
                        _add_col(out, tbl, ci.name, "C")


def _parse_mapper_fallback(filepath: str) -> list[dict]:
    """Fallback parser using regex for malformed XML."""
    statements = []
    mapper_name = os.path.basename(filepath)

    try:
        content = _read_file_safe(filepath)
    except Exception as e:
        logger.warning("Cannot read %s: %s", filepath, e)
        return []

    # Remove XML comments <!-- ... -->
    content = re.sub(r'<!--.*?-->', '', content, flags=re.DOTALL)

    namespace_match = re.search(r'namespace\s*=\s*["\']([^"\']+)', content)
    namespace = namespace_match.group(1) if namespace_match else ""

    pattern = r'<(select|insert|update|delete|statement|procedure)\s+[^>]*id\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</\1>'
    for match in re.finditer(pattern, content, re.DOTALL | re.IGNORECASE):
        tag, stmt_id, sql_body = match.groups()
        sql_text = _clean_sql(sql_body)
        tag_lower = tag.lower()
        if sql_text.strip():
            statements.append({
                "mapper": mapper_name,
                "mapper_path": filepath,
                "namespace": namespace,
                "id": stmt_id,
                "type": tag.upper(),
                "sql": sql_text,
                "procedures": extract_procedure_calls(sql_text, tag_lower),
                "column_usage": extract_column_usage(sql_text),
            })

    return statements


def _extract_sql_text(elem, sql_fragments: dict | None = None,
                       _visited: set | None = None) -> str:
    """Extract full SQL text from an XML element, recursing all nested levels.

    ``sql_fragments`` (dict ``{sql_id: raw_text}``) 가 주어지면 ``<include
    refid="x">`` 자식을 fragment text 로 inline 치환. 재귀 fragment
    (A → B → A cycle) 는 ``_visited`` set 으로 차단.
    """
    sql_fragments = sql_fragments or {}
    visited = _visited if _visited is not None else set()
    parts = []
    if elem.text:
        parts.append(elem.text)
    for child in elem:
        if child.tag == "include":
            ref = child.attrib.get("refid", "")
            if ref and ref not in visited and ref in sql_fragments:
                # 재귀 expansion: fragment text 안에 또 ``<include>`` 가
                # 있을 수 있어 sentinel 토큰을 같은 식으로 inline 치환.
                parts.append(_expand_include_sentinels(
                    sql_fragments[ref], sql_fragments, visited | {ref},
                ))
            # ref 가 unknown 이거나 cycle 이면 silently skip — 빈 텍스트
        else:
            parts.append(_extract_sql_text(child, sql_fragments, visited))
        if child.tail:
            parts.append(child.tail)
    return _clean_sql(" ".join(parts))


def _expand_include_sentinels(text: str, sql_fragments: dict,
                                visited: set) -> str:
    """``__MYBATIS_INCLUDE__:ref__`` sentinel 들을 fragment text 로 치환.

    ``_raw_element_text`` 가 fragment 등록 시 ``<include>`` 를 sentinel
    로 보존해뒀던 것을 expansion 단계에서 풀어줌. visited cycle guard
    로 재귀 무한루프 차단 (depth 5 까지 안전).
    """
    pattern = re.compile(
        r"\s*__MYBATIS_INC_BEGIN__(.*?)__MYBATIS_INC_END__\s*"
    )

    def _sub(m):
        ref = m.group(1)
        if ref in visited or ref not in sql_fragments:
            return " "
        return _expand_include_sentinels(
            sql_fragments[ref], sql_fragments, visited | {ref},
        )

    out = text
    for _ in range(5):  # depth 5 cap
        new = pattern.sub(_sub, out)
        if new == out:
            break
        out = new
    return out


def _clean_sql(sql: str) -> str:
    """Clean SQL text by removing comments, MyBatis parameters and extra whitespace."""
    # Remove block comments /* ... */
    sql = re.sub(r'/\*.*?\*/', ' ', sql, flags=re.DOTALL)
    # Remove line comments -- ...
    sql = re.sub(r'--[^\n]*', ' ', sql)
    # Remove #{...} and ${...} parameters but keep structure
    sql = re.sub(r'#\{[^}]*\}', '?', sql)
    sql = re.sub(r'\$\{[^}]*\}', '?', sql)
    # Remove CDATA markers
    sql = re.sub(r'<!\[CDATA\[', '', sql)
    sql = re.sub(r'\]\]>', '', sql)
    # Remove XML tags that might remain. Require the char after `<` to
    # be an XML-tag opener (`/`, `!`, `?`, or letter) so SQL operators
    # such as `a < b`, `a <= b`, `a <> b` are NOT mistaken for tags —
    # otherwise `<[^>]+>` would greedily eat from `<=` up to the next
    # `>` and devour real table names in between.
    sql = re.sub(r'<(?=[/!?A-Za-z])[^>]*>', ' ', sql)
    # Normalize whitespace
    sql = re.sub(r'\s+', ' ', sql).strip()
    return sql


def extract_joins(statements: list[dict]) -> list[dict]:
    """Extract JOIN relationships from SQL statements.

    Joins are de-duplicated by ``(table1, column1, table2, column2)``
    but **all** contributing statements are recorded: the first hit
    populates ``source_mapper`` / ``source_id`` (back-compat) and every
    subsequent hit is appended to ``sources`` (``mapper#id`` list) and
    ``source_stmts`` (full dicts). This lets downstream reports tell
    the user WHICH statement the relationship actually came from even
    when several queries share the same column pair.
    """
    joins = []
    seen = {}

    for stmt in statements:
        sql = stmt["sql"].upper()
        found = _parse_joins_from_sql(sql)
        src_key = f"{stmt['mapper']}#{stmt['id']}"
        for join in found:
            key = (join["table1"], join["column1"], join["table2"], join["column2"])
            reverse_key = (join["table2"], join["column2"], join["table1"], join["column1"])
            existing = seen.get(key) or seen.get(reverse_key)
            if existing is None:
                join["source_mapper"] = stmt["mapper"]
                join["source_id"] = stmt["id"]
                join["source_type"] = stmt["type"]
                join["sources"] = [src_key]
                seen[key] = join
                joins.append(join)
            else:
                if src_key not in existing["sources"]:
                    existing["sources"].append(src_key)

    logger.info("Extracted %d unique join relationships", len(joins))
    return joins


SQL_KEYWORDS = {
    "ON", "WHERE", "SET", "AND", "OR", "LEFT", "RIGHT", "INNER", "OUTER",
    "CROSS", "FULL", "JOIN", "SELECT", "INTO", "VALUES", "FROM", "AS",
    "NOT", "NULL", "IN", "EXISTS", "BETWEEN", "LIKE", "CASE", "WHEN",
    "THEN", "ELSE", "END", "GROUP", "ORDER", "BY", "HAVING", "UNION",
    "ALL", "DISTINCT", "INSERT", "UPDATE", "DELETE", "CREATE", "ALTER",
    "DROP", "TABLE", "INDEX", "VIEW", "IS", "ASC", "DESC", "LIMIT",
    "OFFSET", "FETCH", "FIRST", "NEXT", "ROWS", "ONLY", "WITH",
    "RECURSIVE", "MERGE", "USING", "MATCHED", "DUAL", "ROWNUM",
    "SYSDATE", "SYSTIMESTAMP", "NVL", "NVL2", "DECODE", "SUBSTR",
    "TRIM", "UPPER", "LOWER", "COUNT", "SUM", "AVG", "MAX", "MIN",
    "OVER", "PARTITION", "ROW_NUMBER", "RANK", "DENSE_RANK",
}


def _parse_joins_from_sql(sql: str) -> list[dict]:
    """Parse JOIN conditions from SQL to extract table relationships."""
    results = []

    # Step 1: Build alias map from SQL syntax
    alias_map = {}
    known_aliases = set()  # alias로 확인된 이름들

    # Step 1a: Find CTE names: WITH name AS (...), name2 AS (...)
    cte_pattern = r'(?:WITH|,)\s+(\w+)\s+AS\s*\('
    for match in re.finditer(cte_pattern, sql):
        cte_name = match.group(1).upper()
        if cte_name not in SQL_KEYWORDS:
            known_aliases.add(cte_name)

    # Step 1b: Find subquery aliases: (...) alias or (...) AS alias
    subquery_alias_pattern = r'\)\s*(?:AS\s+)?(\w+)'
    for match in re.finditer(subquery_alias_pattern, sql):
        alias = match.group(1).upper()
        if alias not in SQL_KEYWORDS:
            known_aliases.add(alias)

    # Step 1b: FROM/JOIN [owner.]table_name alias (optional AS)
    # Owner-qualified 형태 (``SCHEMA1.TB_ORDER alias``) 도 잡도록
    # ``(?:\w+\.)*`` 로 0~N 개 owner prefix 허용. 마지막 ``\w+`` 만
    # 테이블명으로 캡처해서 인덱스/매칭이 owner 무관하게 작동.
    table_alias_pattern = r'(?:FROM|JOIN)\s+(?:\w+\.)*(\w+)\s+(?:AS\s+)?(\w+)'
    for match in re.finditer(table_alias_pattern, sql):
        table, alias = match.groups()
        if alias.upper() not in SQL_KEYWORDS:
            known_aliases.add(alias.upper())
            # Only map if the table part is not a keyword and not already an alias
            if table.upper() not in SQL_KEYWORDS and table.upper() not in known_aliases:
                alias_map[alias.upper()] = table.upper()

    # Step 1c: FROM/JOIN [owner.]table_name (no alias)
    no_alias_pattern = r'(?:FROM|JOIN)\s+(?:\w+\.)*(\w+)(?:\s*(?:WHERE|ON|,|\)|$))'
    for match in re.finditer(no_alias_pattern, sql):
        table = match.group(1).upper()
        if table not in SQL_KEYWORDS and table not in known_aliases:
            alias_map[table] = table

    # Step 1d: Oracle comma-style FROM clause:
    #     FROM TB_A a, TB_B b, TB_C c
    # The patterns above only capture the FIRST entry (``FROM TB_A a``).
    # We have to walk the full FROM clause, strip parenthesised
    # subqueries, split on commas, and register each ``table [alias]``
    # pair in the alias map. This is what makes Oracle ``(+)`` outer-join
    # style queries resolvable.
    from_clause_re = re.compile(
        r'\bFROM\b(.*?)(?=\bWHERE\b|\bJOIN\b|\bGROUP\b|\bHAVING\b|\bORDER\b|\bCONNECT\b|\bSTART\b|$)',
        re.IGNORECASE | re.DOTALL,
    )
    for fm in from_clause_re.finditer(sql):
        body = fm.group(1)
        # Remove nested parenthesised subqueries — leave their content
        # out of the split so we don't mistake subquery columns for
        # tables.
        prev = None
        while prev != body:
            prev = body
            body = re.sub(r'\([^()]*\)', ' ', body)
        if "," not in body:
            continue
        for part in body.split(","):
            part = part.strip()
            if not part:
                continue
            tokens = part.split()
            if not tokens:
                continue
            table = tokens[0].upper()
            if table in SQL_KEYWORDS:
                continue
            alias = table  # default alias = table itself (no-alias form)
            if len(tokens) >= 2:
                # ``TB_X AS t`` or ``TB_X t``
                cand = tokens[-1].upper()
                if cand not in SQL_KEYWORDS:
                    alias = cand
            known_aliases.add(alias)
            if alias not in alias_map:
                alias_map[alias] = table
            if table not in alias_map:
                alias_map[table] = table

    # Parse join conditions: a.col = b.col
    # Allow the Oracle legacy outer-join marker ``(+)`` on either side,
    # e.g. ``a.col = b.col(+)``. We capture whether the marker is
    # present so _detect_join_type can report LEFT/RIGHT OUTER JOIN.
    on_pattern = (
        r'(\w+)\.(\w+)(?P<l_outer>\s*\(\+\))?\s*=\s*'
        r'(\w+)\.(\w+)(?P<r_outer>\s*\(\+\))?'
    )
    for match in re.finditer(on_pattern, sql):
        alias1, col1, alias2, col2 = match.group(1), match.group(2), match.group(4), match.group(5)
        left_outer = bool(match.group("l_outer"))
        right_outer = bool(match.group("r_outer"))
        table1 = alias_map.get(alias1.upper())
        table2 = alias_map.get(alias2.upper())

        # Skip if aliases not resolved, same table, or constant values (1=1)
        if not table1 or not table2 or table1 == table2:
            continue
        if col1.isdigit() or col2.isdigit():
            continue
        # Oracle ``(+)`` wins over positional detection. ``a.col(+) =
        # b.col`` means ``a`` is the optional side → RIGHT OUTER,
        # ``a.col = b.col(+)`` means ``b`` is the optional side →
        # LEFT OUTER (relative to the first column in our record).
        if left_outer and right_outer:
            join_type = "FULL OUTER JOIN"
        elif left_outer:
            join_type = "RIGHT OUTER JOIN (Oracle +)"
        elif right_outer:
            join_type = "LEFT OUTER JOIN (Oracle +)"
        else:
            join_type = _detect_join_type(sql, match.start())
        results.append({
            "table1": table1,
            "column1": col1.upper(),
            "table2": table2,
            "column2": col2.upper(),
            "join_type": join_type,
        })

    return results


def _detect_join_type(sql: str, pos: int) -> str:
    """Detect the type of JOIN from context (ANSI keywords only)."""
    prefix = sql[:pos].rstrip()
    if "LEFT" in prefix[-30:]:
        return "LEFT JOIN"
    elif "RIGHT" in prefix[-30:]:
        return "RIGHT JOIN"
    elif "FULL" in prefix[-30:]:
        return "FULL JOIN"
    elif "CROSS" in prefix[-30:]:
        return "CROSS JOIN"
    elif "WHERE" in prefix[-30:]:
        return "WHERE (implicit)"
    return "INNER JOIN"


def _strip_sql_comments(sql: str) -> str:
    """Remove ``/* ... */`` and ``-- ...`` comments from SQL.

    Comments are replaced with a single space so identifier tokens stay
    separated (``DELETE/*c*/FROM T`` becomes ``DELETE FROM T``, not
    ``DELETEFROM T``). Block comments can span lines. String literals
    are left alone — comments inside quoted strings is exceedingly rare
    in mybatis mappers and not worth the extra tokenizer complexity.
    """
    if not sql:
        return sql
    # /* ... */  (non-greedy, multi-line)
    sql = re.sub(r"/\*[\s\S]*?\*/", " ", sql)
    # -- to end-of-line
    sql = re.sub(r"--[^\n]*", " ", sql)
    return sql


def extract_table_usage(statements: list[dict]) -> dict[str, dict]:
    """Analyze which tables are used in which queries and how."""
    usage = {}

    for stmt in statements:
        # Strip block / line comments before upper-casing so inline
        # comments between DML keyword and table name don't hide
        # identifiers from the regexes below.
        sql = _strip_sql_comments(stmt["sql"]).upper()

        # Extract real table names (not aliases) from FROM/JOIN clauses
        tables = set()
        aliases_in_stmt = set()

        # CTE names: WITH name AS (...)
        for match in re.finditer(r'(?:WITH|,)\s+(\w+)\s+AS\s*\(', sql):
            cte_name = match.group(1)
            if cte_name not in SQL_KEYWORDS:
                aliases_in_stmt.add(cte_name)

        # Subquery aliases: (...) d1
        for match in re.finditer(r'\)\s*(?:AS\s+)?(\w+)', sql):
            alias = match.group(1)
            if alias not in SQL_KEYWORDS:
                aliases_in_stmt.add(alias)

        # Table aliases: FROM [owner.]TABLE t1
        for match in re.finditer(r'(?:FROM|JOIN)\s+(?:\w+\.)*(\w+)\s+(?:AS\s+)?(\w+)', sql):
            table, alias = match.groups()
            if alias not in SQL_KEYWORDS:
                aliases_in_stmt.add(alias)
                if table not in SQL_KEYWORDS and table not in aliases_in_stmt:
                    tables.add(table)

        # Tables without alias. The follow-up alternation needs word
        # boundaries around WHERE / ON / JOIN etc. so that `\w+`
        # backtracking doesn't split names that happen to END with
        # those letters — e.g. ``SCA_SHEET_QUESTION SA WHERE`` would
        # otherwise backtrack to ``SCA_SHEET_QUESTI`` + ``ON`` and emit
        # the truncated form as a second (fake) table.
        _TABLE_END_KEYWORDS = (
            "WHERE", "ON", "JOIN",
            "INNER", "LEFT", "RIGHT", "FULL", "CROSS", "OUTER", "NATURAL",
            "GROUP", "ORDER", "HAVING", "UNION", "MINUS", "INTERSECT",
            "START", "CONNECT", "FETCH", "WITH", "AS",
        )
        _follow_alt = "|".join(_TABLE_END_KEYWORDS)
        for match in re.finditer(
            rf'(?:FROM|JOIN)\s+(?:\w+\.)*(\w+)(?=\s*[,\)]|\s+(?:{_follow_alt})\b|\s*$)',
            sql,
        ):
            table = match.group(1)
            if table not in SQL_KEYWORDS and table not in aliases_in_stmt:
                tables.add(table)

        def _add_table(name: str) -> None:
            """Add ``name`` to tables iff it's neither a keyword nor an alias.

            Keyword-filtering here is critical for Oracle ``MERGE`` where
            ``UPDATE SET`` appears inside ``WHEN MATCHED`` — without this
            check the regex captures ``SET`` as a table.
            """
            if not name:
                return
            upper = name.upper()
            if upper in SQL_KEYWORDS:
                return
            if name in aliases_in_stmt:
                return
            tables.add(name)

        # INSERT INTO [owner.]table
        for m in re.finditer(r'INSERT\s+INTO\s+(?:\w+\.)*(\w+)', sql):
            _add_table(m.group(1))

        # UPDATE [owner.]table — use finditer so both the merge-UPDATE-SET
        # false positive is filtered AND real multi-UPDATE dynamic SQL
        # still picks up every target.
        for m in re.finditer(r'UPDATE\s+(?:\w+\.)*(\w+)', sql):
            _add_table(m.group(1))

        # DELETE [FROM] [owner.]table — Oracle 은 FROM 을 생략 가능하므로 둘 다 허용
        for m in re.finditer(r'DELETE\s+(?:FROM\s+)?(?:\w+\.)*(\w+)', sql):
            _add_table(m.group(1))

        # Oracle MERGE: target is ``MERGE INTO <tbl> [alias]``,
        # source is ``USING <tbl>`` (but not ``USING (subquery)``).
        for m in re.finditer(r'MERGE\s+INTO\s+(\w+)(?:\s+(?:AS\s+)?(\w+))?', sql):
            _add_table(m.group(1))
            alias = m.group(2)
            if alias and alias.upper() not in SQL_KEYWORDS:
                aliases_in_stmt.add(alias)
        for m in re.finditer(r'USING\s+(\w+)(?:\s+(?:AS\s+)?(\w+))?', sql):
            _add_table(m.group(1))
            alias = m.group(2)
            if alias and alias.upper() not in SQL_KEYWORDS:
                aliases_in_stmt.add(alias)

        # Oracle comma-style JOIN: FROM T1 [a1], T2 [a2], T3 [a3] [JOIN ... | WHERE | ...]
        # Use finditer so every subquery level's comma-FROM is processed.
        # Previously `re.search` only caught the first occurrence, so nested
        # subqueries like:
        #     ... FROM (SELECT ... FROM A a, B b WHERE ...) x,
        #         (SELECT ... FROM C a, D b WHERE ...) y
        # dropped C / D at the inner level.
        comma_tables = []  # preserve order; idx 0 is main, rest are joins
        for strict_from in re.finditer(
            # ``[owner.]?TABLE alias?, [owner.]?T2 alias?, ...`` 까지 통째로 매치.
            # comma-tail 의 각 entry 도 ``(?:\w+\.)*\w+`` 로 owner prefix 허용.
            r'\bFROM\s+(?:\w+\.)*(\w+)(?:\s+(?:AS\s+)?(\w+))?'
            r'((?:\s*,\s*(?:\w+\.)*\w+(?:\s+(?:AS\s+)?\w+)?)+)',
            sql, re.IGNORECASE,
        ):
            first_table = strict_from.group(1)
            first_alias = strict_from.group(2)
            rest_clause = strict_from.group(3) or ""
            if first_table and first_table.upper() not in SQL_KEYWORDS and first_table not in aliases_in_stmt:
                if first_table not in comma_tables:
                    comma_tables.append(first_table)
                tables.add(first_table)
                if first_alias and first_alias.upper() not in SQL_KEYWORDS:
                    aliases_in_stmt.add(first_alias)
            # remaining tables in comma list ([owner.]?TABLE alias?)
            for match in re.finditer(r',\s*(?:\w+\.)*(\w+)(?:\s+(?:AS\s+)?(\w+))?', rest_clause):
                tbl = match.group(1)
                alias = match.group(2)
                if tbl.upper() in SQL_KEYWORDS:
                    continue
                if tbl in aliases_in_stmt:
                    continue
                if tbl not in comma_tables:
                    comma_tables.append(tbl)
                tables.add(tbl)
                if alias and alias.upper() not in SQL_KEYWORDS:
                    aliases_in_stmt.add(alias)

        # FROM (subquery) alias, T1 alias, T2 alias, ... form.
        # The strict comma-FROM regex above requires the first operand to
        # be a simple table, so when FROM opens with a subquery the
        # sibling tables in the comma list are missed entirely. Walk the
        # balanced parens after each `FROM (` to find the subquery close,
        # then consume the trailing comma list.
        for m in re.finditer(r'\bFROM\s*\(', sql):
            i = m.end() - 1  # points at '('
            depth = 1
            i += 1
            n = len(sql)
            while i < n and depth > 0:
                ch = sql[i]
                if ch == '(':
                    depth += 1
                elif ch == ')':
                    depth -= 1
                i += 1
            if depth != 0:
                continue  # malformed — skip
            tail = sql[i:]
            tm = re.match(
                r'\s*(?:AS\s+)?(?P<sq_alias>\w+)'
                r'(?P<rest>(?:\s*,\s*(?:\w+\.)*\w+(?:\s+(?:AS\s+)?\w+)?)+)',
                tail, re.IGNORECASE,
            )
            if not tm:
                continue
            # subquery alias is already collected by the subquery-alias
            # pass above — only need to process the comma-tail.
            rest = tm.group("rest") or ""
            for inner in re.finditer(r',\s*(?:\w+\.)*(\w+)(?:\s+(?:AS\s+)?(\w+))?', rest):
                tbl = inner.group(1)
                alias = inner.group(2)
                if tbl.upper() in SQL_KEYWORDS:
                    continue
                if tbl in aliases_in_stmt:
                    continue
                if tbl not in comma_tables:
                    comma_tables.append(tbl)
                tables.add(tbl)
                if alias and alias.upper() not in SQL_KEYWORDS:
                    aliases_in_stmt.add(alias)

        # Identify main table (FROM 바로 뒤) vs join tables
        main_table = _extract_main_table(sql, aliases_in_stmt)
        join_tables = set()

        # ANSI JOIN: JOIN [owner.]TABLE
        for match in re.finditer(r'JOIN\s+(?:\w+\.)*(\w+)', sql):
            jt = match.group(1)
            if jt not in SQL_KEYWORDS and jt not in aliases_in_stmt:
                join_tables.add(jt)

        # Comma-joined tables (2nd and beyond)
        for idx, tbl in enumerate(comma_tables):
            if idx > 0 and tbl != main_table:
                join_tables.add(tbl)

        for table in tables:
            if table not in usage:
                usage[table] = {
                    "select_count": 0, "insert_count": 0,
                    "update_count": 0, "delete_count": 0,
                    "as_main_count": 0, "as_join_count": 0,
                    "mappers": set(), "queries": [],
                }
            key = f"{stmt['type'].lower()}_count"
            if key in usage[table]:
                usage[table][key] += 1
            if table == main_table:
                usage[table]["as_main_count"] += 1
            if table in join_tables:
                usage[table]["as_join_count"] += 1
            usage[table]["mappers"].add(stmt["mapper"])
            usage[table]["queries"].append(f"{stmt['mapper']}#{stmt['id']}")

    # Convert sets to lists for serialization
    for table in usage:
        usage[table]["mappers"] = sorted(usage[table]["mappers"])

    return usage


def _extract_main_table(sql: str, aliases: set) -> str:
    """Extract the main table from SQL (first table after FROM, not after JOIN)."""
    # SELECT ... FROM main_table ...
    # INSERT INTO main_table ...
    # UPDATE main_table ...
    # DELETE FROM main_table ...

    # INSERT INTO [owner.]
    m = re.search(r'INSERT\s+INTO\s+(?:\w+\.)*(\w+)', sql)
    if m and m.group(1) not in SQL_KEYWORDS:
        return m.group(1)

    # UPDATE [owner.]
    m = re.search(r'UPDATE\s+(?:\w+\.)*(\w+)', sql)
    if m and m.group(1) not in SQL_KEYWORDS:
        return m.group(1)

    # DELETE FROM [owner.]
    m = re.search(r'DELETE\s+FROM\s+(?:\w+\.)*(\w+)', sql)
    if m and m.group(1) not in SQL_KEYWORDS:
        return m.group(1)

    # SELECT ... FROM [owner.]table (first FROM, not inside subquery)
    # Remove paren content to skip FROMs inside subqueries
    cleaned = sql
    while re.search(r'\([^()]*\)', cleaned):
        cleaned = re.sub(r'\([^()]*\)', ' ', cleaned)
    m = re.search(r'\bFROM\s+(?:\w+\.)*(\w+)', cleaned)
    if m:
        table = m.group(1)
        if table not in SQL_KEYWORDS and table not in aliases:
            return table

    return None


def parse_all_mappers(base_dir: str) -> dict:
    """Parse all MyBatis mappers and return analysis result."""
    xml_files = scan_mybatis_dir(base_dir)

    all_statements = []
    xml_parse_count = 0
    fallback_count = 0

    for filepath in xml_files:
        stmts = parse_mapper_file(filepath)
        all_statements.extend(stmts)
        if stmts:
            xml_parse_count += 1
        logger.info("Parsed %s: %d statements", os.path.basename(filepath), len(stmts))

    # Count statements with JOIN keyword
    join_stmts = [s for s in all_statements if "JOIN" in s["sql"].upper()]

    joins = extract_joins(all_statements)
    table_usage = extract_table_usage(all_statements)

    print(f"  Mapper files found: {len(xml_files)}")
    print(f"  Mappers with statements: {xml_parse_count}")
    print(f"  Total SQL statements: {len(all_statements)}")
    print(f"  Statements with JOIN: {len(join_stmts)}")
    print(f"  Unique JOIN relationships: {len(joins)}")
    print(f"  Tables referenced: {len(table_usage)}")

    return {
        "base_dir": base_dir,
        "mapper_count": len(xml_files),
        "statement_count": len(all_statements),
        "statements": all_statements,
        "joins": joins,
        "table_usage": table_usage,
    }
