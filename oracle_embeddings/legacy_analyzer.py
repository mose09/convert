"""Top-level orchestrator for the AS-IS legacy source analyzer.

Walks Java + MyBatis + (optional) React + (optional) DB menu table and
produces a list of "program rows" where each row represents one Controller
endpoint together with its resolved Service → Mapper → Table → RFC chain
and its menu-hierarchy mapping.

The key design choice is **Controller ↔ Menu bidirectional matching** on a
normalized URL key:

* matched          — URL exists in both sides (happy path)
* unmatched_ctrl   — controller endpoint with no menu row (internal APIs)
* orphan_menu      — menu row with no controller (unimplemented / dead menu)

Each category is emitted as its own sheet / section in the output report.
"""

import logging
import os
import re

from .legacy_java_parser import parse_all_java, resolve_type_fqcn
from .legacy_util import normalize_url
from .mybatis_parser import (
    _read_file_safe, extract_crud_from_sql, extract_table_usage,
    parse_all_mappers,
)

logger = logging.getLogger(__name__)


SQL_KEYWORDS_GUARD = {"FROM", "JOIN", "WHERE", "SELECT"}

_FRAMEWORK_SKIP_DIRS = {"target", "build", ".git", ".gradle", ".idea",
                        "bin", "out", "node_modules", "dist", ".next"}

_BUILD_FILE_NAMES = {"pom.xml", "build.gradle", "build.gradle.kts",
                     "settings.gradle", "settings.gradle.kts"}

# Dependency coordinates that strongly indicate each framework. We keep the
# strings short enough to avoid false matches on unrelated comments.
_SPRING_MARKERS = (
    "org.springframework",
    "spring-boot",
    "spring-webmvc",
    "spring-web",
)
_VERTX_MARKERS = (
    "io.vertx",
    "vertx-core",
    "vertx-web",
    "vertx-web-api",
)


def _find_build_files(backend_dir: str, limit: int = 30) -> list[str]:
    """Return up to ``limit`` build files found under ``backend_dir``."""
    results = []
    for root, dirs, files in os.walk(backend_dir):
        dirs[:] = [d for d in dirs if d.lower() not in _FRAMEWORK_SKIP_DIRS]
        for f in files:
            if f in _BUILD_FILE_NAMES:
                results.append(os.path.join(root, f))
                if len(results) >= limit:
                    return results
    return results


def _source_heuristic(backend_dir: str) -> tuple[int, int]:
    """Peek at a handful of ``.java`` files and count framework markers.

    Returns ``(spring_hits, vertx_hits)``. Only a bounded sample (first
    200 java files) is scanned so this stays fast on very large repos.
    """
    spring_hits = 0
    vertx_hits = 0
    seen = 0
    for root, dirs, files in os.walk(backend_dir):
        dirs[:] = [d for d in dirs if d.lower() not in _FRAMEWORK_SKIP_DIRS]
        for f in files:
            if not f.endswith(".java"):
                continue
            fp = os.path.join(root, f)
            try:
                head = _read_file_safe(fp, limit=4000)
            except Exception:
                continue
            seen += 1
            if "org.springframework" in head or re.search(
                r"@(?:Rest)?Controller\b", head
            ):
                spring_hits += 1
            if "io.vertx" in head or "AbstractVerticle" in head:
                vertx_hits += 1
            if seen >= 200:
                return spring_hits, vertx_hits
    return spring_hits, vertx_hits


def detect_backend_framework(backend_dir: str) -> str:
    """Detect whether the backend project is Spring or Vert.x based.

    Strategy (in order, first decisive signal wins):

    1. Build files (``pom.xml``, ``build.gradle``, ``build.gradle.kts``):
       check for ``org.springframework`` / ``spring-boot`` vs ``io.vertx``
       / ``vertx-core`` / ``vertx-web`` coordinates.
    2. Source heuristic: sample up to 200 ``.java`` files and count
       `` @Controller``/``@RestController`` vs ``AbstractVerticle`` /
       ``io.vertx`` occurrences.

    Returns one of:
      * ``"spring"``
      * ``"vertx"``
      * ``"mixed"``   — both frameworks present (rare; polyglot monorepo)
      * ``"unknown"`` — neither signal detected
    """
    spring_hits = 0
    vertx_hits = 0

    for bf in _find_build_files(backend_dir):
        try:
            content = _read_file_safe(bf, limit=50000)
        except Exception:
            continue
        if any(m in content for m in _SPRING_MARKERS):
            spring_hits += 10
        if any(m in content for m in _VERTX_MARKERS):
            vertx_hits += 10

    # If build files were inconclusive, fall back to source heuristic.
    if spring_hits == 0 and vertx_hits == 0:
        spring_hits, vertx_hits = _source_heuristic(backend_dir)

    if spring_hits > 0 and vertx_hits == 0:
        return "spring"
    if vertx_hits > 0 and spring_hits == 0:
        return "vertx"
    if spring_hits > 0 and vertx_hits > 0:
        # Prefer the stronger signal; tie → "mixed"
        if spring_hits >= vertx_hits * 3:
            return "spring"
        if vertx_hits >= spring_hits * 3:
            return "vertx"
        return "mixed"
    return "unknown"


def _tables_for_statement(stmt: dict) -> set:
    """Extract tables referenced by a single statement using the shared util.

    Wraps ``extract_table_usage`` (which aggregates across statements) on a
    single-element list and pulls the keys back out. Works for every SQL
    dialect the bundled parser already supports.
    """
    usage = extract_table_usage([stmt])
    return set(usage.keys())


_VERTX_ENDPOINT_ANNOTATIONS = {"Vert.x", "RestVerticle"}


def _filter_endpoints_by_framework(classes: list[dict], framework: str) -> None:
    """Drop endpoints that don't match the detected framework (in place).

    When framework is ``spring`` we remove Vert.x / RestVerticle endpoints
    and vice versa. ``mixed`` / ``unknown`` keeps both kinds.
    """
    if framework not in ("spring", "vertx"):
        return
    for c in classes:
        eps = c.get("endpoints") or []
        if framework == "spring":
            c["endpoints"] = [
                e for e in eps if e.get("annotation") not in _VERTX_ENDPOINT_ANNOTATIONS
            ]
        else:
            c["endpoints"] = [
                e for e in eps if e.get("annotation") in _VERTX_ENDPOINT_ANNOTATIONS
            ]


def _build_indexes(classes: list[dict], framework: str = "mixed",
                   mybatis_namespaces: set | None = None,
                   patterns: dict | None = None) -> dict:
    """Partition parsed Java classes into role-specific indexes.

    ``framework`` gates which classes are eligible as HTTP entry points:

    * ``spring`` — only ``@Controller`` / ``@RestController``
    * ``vertx``  — only Verticle subclasses
    * ``mixed`` / ``unknown`` — both are accepted (backward-compatible)

    In addition to the stereotype-based rule, **any class that carries at
    least one extracted endpoint is promoted to a controller**. This
    covers the common Vert.x pattern where route setup lives in a plain
    "router builder" class (no ``extends AbstractVerticle``, no Spring
    annotation) that the main Verticle instantiates and delegates to.

    ``mybatis_namespaces`` (optional) enables a namespace-based reverse
    lookup: any class or interface whose FQCN matches a MyBatis
    ``<mapper namespace="...">`` is promoted to a mapper even if its name
    doesn't end in ``Mapper``/``Dao``. This rescues legacy projects that
    name their interfaces ``OrderRepository`` or ``FooBar`` but still wire
    them to MyBatis via the namespace convention.

    Returns a dict with:
      * controllers_by_fqcn
      * services_by_fqcn  (includes ``*ServiceImpl`` + ``@Service``/``@Component``)
      * mappers_by_fqcn   (``@Mapper``/``@Repository`` + ``*Mapper``/``*Dao`` + namespace-matched)
      * by_simple         — ``{SimpleName: [class, ...]}`` for name fallback
    """
    if framework == "spring":
        controller_stereos = {"Controller", "RestController"}
    elif framework == "vertx":
        controller_stereos = {"Verticle"}
    else:
        controller_stereos = {"Controller", "RestController", "Verticle"}

    namespaces = mybatis_namespaces or set()

    # Service detection suffixes. Projects use various naming conventions
    # for business-logic classes (OrderService / OrderBo / OrderBiz /
    # OrderManager / OrderFacade / OrderHelper) and for their
    # implementations (*Impl). We want any of these to land in the
    # services index so that the Controller → Service → Mapper / RFC
    # chain can walk through.
    service_suffixes = (
        "Service", "ServiceImpl",
        "Bo", "BoImpl", "Biz", "BizImpl",
        "Manager", "ManagerImpl",
        "Facade", "FacadeImpl",
    )
    # Extend with pattern overrides
    if patterns:
        extra_svc = patterns.get("service_suffixes") or []
        if extra_svc:
            service_suffixes = tuple(dict.fromkeys(list(service_suffixes) + extra_svc))

    dao_suffixes_extra = []
    if patterns:
        dao_suffixes_extra = patterns.get("dao_suffixes") or []

    controllers = {}
    services = {}
    mappers = {}
    by_simple = {}

    def _is_service_name(n):
        return any(n.endswith(sfx) for sfx in service_suffixes)

    for c in classes:
        fqcn = c["fqcn"]
        by_simple.setdefault(c["class_name"], []).append(c)
        stereo = c.get("stereotype", "")
        name = c["class_name"]
        has_endpoints = bool(c.get("endpoints"))
        is_library = bool(c.get("is_library"))

        # Library classes (from --library-dir) participate in service /
        # mapper / by_simple indexes only — they must NOT become
        # controllers even if they carry endpoints or a Controller
        # stereotype. This lets a shared repo (e.g. ``gipms-common``)
        # expose its services to a main project's chain resolution
        # without generating duplicate endpoint rows.
        if not is_library:
            if stereo in controller_stereos:
                controllers[fqcn] = c
            elif has_endpoints:
                # Promotion: plain class that happens to declare routes. The
                # endpoint-list was already filtered to match ``framework``
                # above, so this can't pull in a Spring class into a Vert.x
                # project or vice versa.
                controllers[fqcn] = c
        if stereo in ("Service", "Component") or _is_service_name(name):
            services[fqcn] = c
        is_mapper = (stereo in ("Mapper", "Repository")
                     or name.endswith("Mapper") or name.endswith("Dao"))
        if not is_mapper and dao_suffixes_extra:
            is_mapper = any(name.endswith(sfx) for sfx in dao_suffixes_extra)
        if is_mapper:
            mappers[fqcn] = c
        elif fqcn in namespaces:
            # Namespace-based rescue: the interface FQCN matches a MyBatis
            # XML namespace, so it's clearly a mapper even without a
            # ``Mapper``/``Dao`` suffix or ``@Mapper`` annotation.
            mappers[fqcn] = c

    return {
        "controllers_by_fqcn": controllers,
        "services_by_fqcn": services,
        "mappers_by_fqcn": mappers,
        "by_simple": by_simple,
    }


def _build_mybatis_indexes(mybatis_result: dict) -> dict:
    """Build namespace- and statement-keyed indexes for Mapper → XML/Tables.

    Returns:
      * namespace_to_xml_files — ``{namespace: sorted list[str]}``
      * namespace_to_tables    — ``{namespace: sorted list[str]}``
      * statement_to_tables    — ``{"ns.id": sorted list[str]}`` — tables
        touched by a *single* SQL statement. This is what lets the
        analyzer report ``TB_ORDER`` for ``order.save`` separately
        from ``TB_ORDER, TB_CUSTOMER`` for ``order.findAll``.
      * statement_to_xml_file  — ``{"ns.id": str}`` — the XML file path
        that contains the statement.
      * statement_to_body_crud — ``{"ns.id": set("C"|"R"|"U"|"D")}`` —
        letters derived purely from SQL body analysis
        (:func:`mybatis_parser.extract_crud_from_sql`). MyBatis tag
        type is intentionally NOT consulted so ``<select>`` containing
        a PL/SQL ``BEGIN ... UPDATE ... END;`` gets ``U`` instead of ``R``.
      * statement_to_procs     — ``{"ns.id": [proc_name, ...]}``
    """
    namespace_to_xml_files = {}
    namespace_to_tables = {}
    statement_to_tables = {}
    statement_to_xml_file = {}
    statement_to_procs: dict[str, list[str]] = {}
    # CRUD letters from pure SQL body analysis. ``extract_crud_from_sql``
    # already strips comments/literals and drops ``SELECT ... FOR UPDATE``
    # before scanning, so false positives are handled.
    statement_to_body_crud: dict[str, set] = {}
    # Column-level CRUD (Phase I). sqlglot-based AST walker in
    # ``extract_column_usage`` — empty dict when parse fails, callers
    # then fall back to table-level CRUD (``statement_to_body_crud``).
    statement_to_column_usage: dict[str, dict[str, dict[str, set]]] = {}
    for stmt in mybatis_result.get("statements", []):
        ns = stmt.get("namespace") or ""
        stmt_id = stmt.get("id") or ""
        tables_for_stmt = _tables_for_statement(stmt)
        if ns:
            if "mapper_path" in stmt:
                namespace_to_xml_files.setdefault(ns, set()).add(stmt["mapper_path"])
            for tbl in tables_for_stmt:
                namespace_to_tables.setdefault(ns, set()).add(tbl)
            if stmt_id:
                key = f"{ns}.{stmt_id}"
                statement_to_tables.setdefault(key, set()).update(tables_for_stmt)
                if "mapper_path" in stmt:
                    statement_to_xml_file.setdefault(key, stmt["mapper_path"])
                procs = stmt.get("procedures") or []
                if procs:
                    # Statements are unique per key, but namespace fallback
                    # can re-hit the same key — union preserving first-seen
                    # order to keep the "primary proc first" convention
                    # from ``extract_procedure_calls``.
                    existing = statement_to_procs.setdefault(key, [])
                    for p in procs:
                        if p not in existing:
                            existing.append(p)
                body_letters = extract_crud_from_sql(stmt.get("sql") or "")
                if body_letters:
                    statement_to_body_crud.setdefault(key, set()).update(body_letters)
                col_usage = stmt.get("column_usage") or {}
                if col_usage:
                    # Merge (table, col) → letters across duplicate keys.
                    merged = statement_to_column_usage.setdefault(key, {})
                    for tbl, cols in col_usage.items():
                        dst_cols = merged.setdefault(tbl, {})
                        for col, letters in cols.items():
                            dst_cols.setdefault(col, set()).update(letters)

    return {
        "namespace_to_xml_files": {k: sorted(v) for k, v in namespace_to_xml_files.items()},
        "namespace_to_tables": {k: sorted(v) for k, v in namespace_to_tables.items()},
        "statement_to_tables": {k: sorted(v) for k, v in statement_to_tables.items()},
        "statement_to_xml_file": statement_to_xml_file,
        "statement_to_procs": statement_to_procs,
        "statement_to_body_crud": statement_to_body_crud,
        "statement_to_column_usage": statement_to_column_usage,
    }


def _format_table_crud(tables: list[str], crud_map: dict[str, set]) -> str:
    """Render the Programs-sheet Tables column.

    For each table, append a ``(CRUD)`` suffix derived from ``crud_map``
    (which maps table name → set of C/R/U/D letters). Items are joined
    with ``",\\n"`` so Excel displays one table per line in a single
    cell — users asked for at-a-glance reading of many tables.

    The CRUD letters are sorted in canonical order (``C``, ``R``, ``U``,
    ``D``). Tables with no known operation (e.g. parsed from a
    ``<statement>`` tag we can't classify) get no suffix — the bare name
    is still emitted so the user still sees which tables an endpoint
    touches.
    """
    canonical = ("C", "R", "U", "D")
    parts = []
    for tbl in tables:
        letters = crud_map.get(tbl) or set()
        suffix = ""
        if letters:
            ordered = "".join(ch for ch in canonical if ch in letters)
            if ordered:
                suffix = f"({ordered})"
        parts.append(f"{tbl}{suffix}")
    return ",\n".join(parts)


def _format_list_with_newlines(items: list[str]) -> str:
    """Join a list with ``",\\n"`` so Excel wraps each item per line."""
    return ",\n".join(items)


_TERMS_ROW_RE = re.compile(
    # Matches a terminology table row. Accepts either 3-col
    # (Abbr | English | Korean) or 4-col (Word | Abbr | English | Korean)
    # layouts — both exist in terms_report output. We only care about
    # abbreviation → Korean here, so we grab the first non-header cell
    # that looks like an uppercase identifier + a subsequent Korean cell.
    r"^\|\s*([A-Za-z][\w]*)\s*\|\s*([A-Za-z][\w]*)?\s*\|\s*([^|]+?)?\s*\|\s*([^|]+?)?\s*\|",
    re.MULTILINE,
)


def _load_terms_dict(path: str) -> dict[str, str]:
    """Load a terms-dictionary Markdown and return ``{upper_word: korean}``.

    Robust to the two common formats emitted by ``terms_report`` —
    ``| Abbr | English | Korean | Definition |`` or
    ``| Word | Abbr | English | Korean | DB | FE | Total |``. We map
    the short token (``Abbr`` or ``Word``) to the Korean label.
    Duplicate keys: first-seen wins. Lookup in ``_format_column_crud``
    tries the column name as-is so both ``CUST_NO`` and ``CUST`` hit
    their respective entries.
    """
    out: dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            content = fh.read()
    except Exception:
        return out
    for m in _TERMS_ROW_RE.finditer(content):
        token = (m.group(1) or "").strip()
        if not token or token.lower() in ("word", "abbreviation", "abbr"):
            continue
        korean = None
        # Korean in 3rd column (Abbr|English|Korean) or 4th
        # (Word|Abbr|English|Korean). Prefer 4th when present.
        for g in (m.group(4), m.group(3)):
            if g and any("가" <= ch <= "힣" for ch in g):
                korean = g.strip()
                break
        if not korean:
            continue
        out.setdefault(token.upper(), korean)
    return out


def _format_column_crud(column_crud: dict[str, dict[str, set]],
                         terms_dict: dict[str, str] | None = None) -> str:
    """Render the Programs-sheet ``Columns`` column.

    Produces ``TBL.col1[한글](R),\\nTBL.col2[등록일자](U)`` with one
    ``TABLE.COLUMN`` per line. When ``terms_dict`` is provided and the
    column name has a Korean translation, a bracketed ``[한글]`` token
    is inserted between the name and the CRUD suffix. Missing terms
    simply omit the bracket. Letters are sorted in canonical C/R/U/D
    order to match :func:`_format_table_crud`.

    Input ``column_crud`` comes from :func:`_derive_column_crud`.
    Tables/columns are upper-cased by the sqlglot walker, so the dict
    traversal yields deterministic sorted output.
    """
    canonical = ("C", "R", "U", "D")
    terms = terms_dict or {}
    parts: list[str] = []
    for tbl in sorted(column_crud):
        cols = column_crud[tbl]
        for col in sorted(cols):
            letters = cols[col] or set()
            ordered = "".join(ch for ch in canonical if ch in letters)
            korean = terms.get(col) or terms.get(f"{tbl}.{col}") or ""
            ko_part = f"[{korean}]" if korean else ""
            suffix = f"({ordered})" if ordered else ""
            parts.append(f"{tbl}.{col}{ko_part}{suffix}")
    return ",\n".join(parts)


# Name-based impl discovery suffixes. Legacy projects use many different
# suffixes for the concrete implementation of a service interface.
# NOTE: ``Handler`` is intentionally NOT here — in Vert.x projects it is
# a Controller (verticle-adjacent HTTP handler), not a service impl.
# ``Facade`` is present in both lists to stay consistent with
# ``_SERVICE_STRIP`` below.
_IMPL_SUFFIXES = ("Impl", "Bo", "Biz", "Manager", "Facade", "Helper", "Delegate")
# And for core-name variants: OrderService → Order, OrderBiz → Order, ...
_SERVICE_STRIP = re.compile(r"(?:Service|Bo|Biz|Manager|Facade)$")


def _resolve_service_impls(services_by_fqcn: dict, by_simple: dict) -> dict:
    """For each Service interface, find its implementing class.

    Three strategies run in order; first hit wins:

    1. **``implements`` declaration**: a class in the service index declares
       ``implements OrderService`` → map that interface to the impl.
    2. **Name-based suffix fallback**: for a service named ``OrderService``
       try every suffix in ``_IMPL_SUFFIXES`` (``OrderServiceImpl``,
       ``OrderServiceBo``, …) against the global class index.
    3. **Core-name fallback**: strip any trailing ``Service``/``Bo``/
       ``Biz``/``Manager``/``Facade`` from the interface name (``OrderBo`` →
       ``Order``) and try ``Order`` + each impl suffix
       (``OrderImpl``/``OrderHandler``/…). This catches projects where the
       "interface" carries the business suffix and the implementation
       uses a different one.
    """
    iface_to_impl = {}

    for fqcn, cls in services_by_fqcn.items():
        if cls["kind"] != "interface" and cls.get("implements"):
            for iface_simple in cls["implements"]:
                simple = re.sub(r"<.*$", "", iface_simple).strip()
                for candidate in by_simple.get(simple, []):
                    iface_to_impl[candidate["fqcn"]] = fqcn

    for fqcn, cls in services_by_fqcn.items():
        if fqcn in iface_to_impl:
            continue
        name = cls["class_name"]
        for suffix in _IMPL_SUFFIXES:
            impl_name = name + suffix
            if impl_name == name:
                continue
            for candidate in by_simple.get(impl_name, []):
                if candidate["fqcn"] != fqcn:
                    iface_to_impl.setdefault(fqcn, candidate["fqcn"])
                    break
            if fqcn in iface_to_impl:
                break

    for fqcn, cls in services_by_fqcn.items():
        if fqcn in iface_to_impl:
            continue
        name = cls["class_name"]
        core = _SERVICE_STRIP.sub("", name)
        if not core or core == name:
            continue
        for suffix in _IMPL_SUFFIXES + ("Service",):
            impl_name = core + suffix
            if impl_name == name:
                continue
            for candidate in by_simple.get(impl_name, []):
                if candidate["fqcn"] != fqcn:
                    iface_to_impl.setdefault(fqcn, candidate["fqcn"])
                    break
            if fqcn in iface_to_impl:
                break

    return iface_to_impl


def _find_service_fqcns(controller: dict, indexes: dict) -> list[str]:
    """Return the FQCNs of Services injected into ``controller``.

    Tries exact FQCN match first, then simple-name fallback (helpful for
    legacy projects where ``@Autowired`` field types aren't imported).
    """
    svc_index = indexes["services_by_fqcn"]
    by_simple = indexes["by_simple"]
    results = []
    seen = set()
    for f in controller.get("autowired_fields", []):
        fqcn = f.get("type_fqcn") or ""
        if fqcn in svc_index and fqcn not in seen:
            seen.add(fqcn)
            results.append(fqcn)
            continue
        # Name fallback
        for candidate in by_simple.get(f["type_simple"], []):
            if candidate["fqcn"] in svc_index and candidate["fqcn"] not in seen:
                seen.add(candidate["fqcn"])
                results.append(candidate["fqcn"])
    return results


def _find_mapper_fqcns(service_fqcn: str, indexes: dict) -> list[str]:
    """Return Mapper FQCNs injected into ``service_fqcn`` (or its Impl)."""
    svc_index = indexes["services_by_fqcn"]
    mapper_index = indexes["mappers_by_fqcn"]
    by_simple = indexes["by_simple"]
    iface_to_impl = indexes["iface_to_impl"]

    target = iface_to_impl.get(service_fqcn, service_fqcn)
    service = svc_index.get(target) or svc_index.get(service_fqcn)
    if not service:
        return []

    results = []
    seen = set()
    for f in service.get("autowired_fields", []):
        fqcn = f.get("type_fqcn") or ""
        if fqcn in mapper_index and fqcn not in seen:
            seen.add(fqcn)
            results.append(fqcn)
            continue
        for candidate in by_simple.get(f["type_simple"], []):
            if candidate["fqcn"] in mapper_index and candidate["fqcn"] not in seen:
                seen.add(candidate["fqcn"])
                results.append(candidate["fqcn"])
    return results


def _find_service_namespaces(service_fqcn: str, indexes: dict) -> set:
    """Return the set of SQL namespaces referenced by a service or its Impl.

    Looks at ``sql_calls`` collected by the Java parser — these are
    string-based helper calls like ``CommonSQL.selectList("ns.id", ...)``
    that bypass the Mapper interface convention entirely.
    """
    svc_index = indexes["services_by_fqcn"]
    iface_to_impl = indexes["iface_to_impl"]
    by_simple = indexes["by_simple"]

    target_fqcn = iface_to_impl.get(service_fqcn, service_fqcn)
    cls = svc_index.get(target_fqcn) or svc_index.get(service_fqcn)

    namespaces = set()

    def _collect(c):
        if not c:
            return
        for call in c.get("sql_calls") or []:
            ns = call.get("namespace") or ""
            if ns:
                namespaces.add(ns)

    _collect(cls)
    # Name-based impl fallback for interfaces that don't declare ``implements``
    # but whose impl is discoverable via the ``*Impl`` suffix.
    if cls and cls.get("kind") == "interface":
        impl_name = cls["class_name"] + "Impl"
        for cand in by_simple.get(impl_name, []):
            _collect(cand)
    return namespaces


# 진단 카운터 — analyze_legacy 시작 시 _reset_chain_diag() 로 초기화하고
# 끝에 _emit_chain_diag() 로 콘솔 출력. 어느 단계 (statement miss /
# namespace ambiguous / chain depth cap) 가 chain 누락의 원인인지
# 가시화. 어떤 row 도 안 바꾸고 logging 만 추가.
_CHAIN_DIAG: dict = {
    "unresolved_stmt": 0,
    "unresolved_stmt_samples": [],
    "ambiguous_ns": 0,
    "ambiguous_ns_samples": [],
    "truncated_chain": 0,
}


def _reset_chain_diag() -> None:
    _CHAIN_DIAG["unresolved_stmt"] = 0
    _CHAIN_DIAG["unresolved_stmt_samples"] = []
    _CHAIN_DIAG["ambiguous_ns"] = 0
    _CHAIN_DIAG["ambiguous_ns_samples"] = []
    _CHAIN_DIAG["truncated_chain"] = 0


def _emit_chain_diag() -> None:
    """Print 1~3 줄 진단 요약. 0 인 항목은 skip."""
    parts = []
    if _CHAIN_DIAG["unresolved_stmt"]:
        s = _CHAIN_DIAG["unresolved_stmt_samples"][:3]
        parts.append(f"  Chain diag: {_CHAIN_DIAG['unresolved_stmt']} statement id "
                     f"unresolved (sample: {s})")
    if _CHAIN_DIAG["ambiguous_ns"]:
        s = _CHAIN_DIAG["ambiguous_ns_samples"][:3]
        parts.append(f"  Chain diag: {_CHAIN_DIAG['ambiguous_ns']} namespace "
                     f"ambiguous matches (sample: {s})")
    if _CHAIN_DIAG["truncated_chain"]:
        parts.append(f"  Chain diag: {_CHAIN_DIAG['truncated_chain']} transitive "
                     f"chain walks truncated by rfc_depth cap")
    for line in parts:
        print(line)


def _match_namespace(ns: str, ns_to_xml: dict) -> str | None:
    """Find the MyBatis XML namespace that corresponds to ``ns``.

    SQL helper calls may use the full FQCN (``com.example.order``) or a
    shorthand (``order``, ``OrderSQL``). The XML file's ``namespace``
    attribute may be one or the other. We try:

    1. Exact match
    2. ``ns`` is a suffix of an XML namespace (``order`` → ``com.x.order``)
    3. ``ns`` is a prefix and the XML namespace is a suffix of ``ns``
       (``com.x.order`` → ``order``)
    """
    if not ns:
        return None
    if ns in ns_to_xml:
        return ns
    # Suffix match: shorthand SQL call → longer XML namespace
    suffix_matches = [
        k for k in ns_to_xml
        if k == ns or k.endswith("." + ns)
    ]
    if suffix_matches:
        if len(suffix_matches) > 1:
            _CHAIN_DIAG["ambiguous_ns"] += 1
            samples = _CHAIN_DIAG["ambiguous_ns_samples"]
            if len(samples) < 5:
                samples.append(f"{ns} → {sorted(suffix_matches)}")
        # 기존 선택 알고리즘 유지 (max by length) — 회귀 방지.
        return max(suffix_matches, key=len)
    # Prefix match: FQCN SQL call → shorter XML namespace
    parts = ns.split(".")
    for n in range(len(parts) - 1, 0, -1):
        candidate = ".".join(parts[-n:])
        if candidate in ns_to_xml:
            return candidate
    return None


def _resolve_mapper_chain(service_fqcns: list[str], indexes: dict,
                          mybatis_idx: dict) -> tuple:
    """Walk service → mapper → namespace → (xml files, tables).

    Two resolution paths are combined:

    * **Interface path**: ``service.autowired_fields`` → Mapper interface
      → namespace FQCN (the MyBatis default convention).
    * **SQL-call path**: ``service.sql_calls`` → ``CommonSQL.xxx("ns.id")``
      → direct namespace match (``_match_namespace``). This is the legacy
      pattern used by projects that don't declare Mapper interfaces at
      all — they use a string-keyed helper instead.

    Returns ``(query_xml_paths, related_tables, mapper_fqcns)`` as sorted
    lists. ``mapper_fqcns`` only contains Java interface FQCNs found via
    the interface path; the SQL-call path contributes xml/tables without
    a Java-side mapper identifier.
    """
    ns_to_xml = mybatis_idx["namespace_to_xml_files"]
    ns_to_tbl = mybatis_idx["namespace_to_tables"]

    xml_files = set()
    tables = set()
    mapper_fqcns = []
    seen_mappers = set()

    for svc in service_fqcns:
        # --- Path 1: injected Mapper interface -----------------------
        for mfqcn in _find_mapper_fqcns(svc, indexes):
            if mfqcn in seen_mappers:
                continue
            seen_mappers.add(mfqcn)
            mapper_fqcns.append(mfqcn)

            if mfqcn in ns_to_xml:
                xml_files.update(ns_to_xml[mfqcn])
            if mfqcn in ns_to_tbl:
                tables.update(ns_to_tbl[mfqcn])

            simple = mfqcn.rsplit(".", 1)[-1]
            for ns in ns_to_xml:
                if ns == mfqcn:
                    continue
                if ns.endswith("." + simple) or ns == simple:
                    xml_files.update(ns_to_xml.get(ns, []))
                    tables.update(ns_to_tbl.get(ns, []))

        # --- Path 2: string-based SQL helper calls -------------------
        for raw_ns in _find_service_namespaces(svc, indexes):
            matched_ns = _match_namespace(raw_ns, ns_to_xml)
            if not matched_ns:
                continue
            xml_files.update(ns_to_xml.get(matched_ns, []))
            tables.update(ns_to_tbl.get(matched_ns, []))

    return sorted(xml_files), sorted(tables), mapper_fqcns


def _collect_rfc_transitive(root_fqcns: list[str], indexes: dict,
                            controller_rfc: list[dict], depth: int = 2) -> list[str]:
    """Union of RFC function names reachable from controller + its services.

    ``depth`` limits the service-of-service walk; cycles are broken by a
    visited set. Returns a sorted unique list of RFC names (strings).
    """
    rfc_names = set()
    for r in controller_rfc:
        rfc_names.add(r["name"])

    svc_index = indexes["services_by_fqcn"]
    mapper_index = indexes["mappers_by_fqcn"]
    iface_to_impl = indexes["iface_to_impl"]
    by_simple = indexes["by_simple"]

    visited = set()

    def _walk(fqcn: str, remaining: int):
        if fqcn in visited or remaining < 0:
            return
        visited.add(fqcn)
        target = iface_to_impl.get(fqcn, fqcn)
        cls = svc_index.get(target) or svc_index.get(fqcn)
        if not cls:
            return
        for r in cls.get("rfc_calls", []):
            rfc_names.add(r["name"])
        if remaining == 0:
            return
        # Walk into other injected services / components
        for f in cls.get("autowired_fields", []):
            child = f.get("type_fqcn") or ""
            if child in svc_index and child not in visited:
                _walk(child, remaining - 1)
                continue
            for cand in by_simple.get(f.get("type_simple", ""), []):
                if cand["fqcn"] in svc_index and cand["fqcn"] not in visited:
                    _walk(cand["fqcn"], remaining - 1)

    for svc in root_fqcns:
        _walk(svc, depth)

    return sorted(rfc_names)


def _lookup_menu_by_app(menu_rows: list[dict], app_key_spec: dict | None,
                         app_slug: str,
                         by_frontend: dict | None = None) -> dict | None:
    """Return the first menu row whose URL maps to ``app_slug``.

    Matching strategies, tried in order:

      1. Structured (preferred): when ``app_key_spec`` is configured,
         extract slug from each menu URL with :func:`_extract_app_key`
         and compare.
      2. Route-path alias: when ``by_frontend`` is provided and the
         structured match fails, leverage the alias that
         ``build_frontend_url_map_multi`` registers for each Route path.
         Menu slug may differ from folder name (사용자 사례: folder
         ``hypm_materialMaster`` + Route path ``/apps/gipms-materialmasternew``).
         If ``by_frontend[menu_slug]`` points to the SAME bucket object
         as ``by_frontend[app_slug]`` (alias pair), this menu row
         declared a Route living in ``app_slug``'s folder → match.
      3. Substring fallback: if all structured matches fail, look for
         the ``app_slug`` string inside the raw menu URL
         (case-insensitive).
    """
    if not menu_rows or not app_slug:
        return None
    app_slug_lower = app_slug.lower()
    if app_key_spec:
        for row in menu_rows:
            if _extract_app_key(row.get("url", ""), app_key_spec) == app_slug_lower:
                return row
        # Structured match failed — try Route-path alias before substring.
        if by_frontend:
            target_bucket = by_frontend.get(app_slug_lower)
            if target_bucket is not None:
                for row in menu_rows:
                    menu_slug = _extract_app_key(row.get("url", ""), app_key_spec)
                    if not menu_slug:
                        continue
                    # `is` check — build_frontend_url_map_multi aliases by
                    # reusing the same dict object for both keys.
                    if by_frontend.get(menu_slug) is target_bucket:
                        return row
    for row in menu_rows:
        if app_slug_lower in (row.get("url", "") or "").lower():
            return row
    return None


def _extract_app_key(raw_url: str, app_key_spec: dict | None) -> str:
    """Pull the app identifier out of a raw menu URL per ``app_key_spec``.

    ``app_key_spec`` shape (from ``patterns.yaml`` ``url.app_key``)::

        {"source": "path_segment", "index": 1}   # 1-based segment index
        {"source": "query_param", "name": "app"}

    Returns the extracted app name lowercased, or ``""`` on any miss.
    Works on the raw (pre-normalized) URL so we can see query strings
    and original casing before the normalize step drops them.
    """
    if not raw_url or not app_key_spec:
        return ""
    source = app_key_spec.get("source")
    try:
        # Split host/path — we don't use urlparse to avoid stdlib overhead
        # and stay robust to non-URL inputs like "/apps/foo/x".
        path = re.sub(r"^https?://[^/]+", "", raw_url.strip())
        if source == "path_segment":
            idx = int(app_key_spec.get("index", 1))
            # Split off query first
            path_only = path.split("?", 1)[0]
            parts = [p for p in path_only.split("/") if p]
            # 1-based, so user says index=2 means the second non-empty segment.
            # Also accept "/apps/foo/order" with index=2 → "foo".
            if 1 <= idx <= len(parts):
                return parts[idx - 1].lower()
            return ""
        if source == "query_param":
            name = str(app_key_spec.get("name") or "").lower()
            if not name or "?" not in path:
                return ""
            query = path.split("?", 1)[1]
            for chunk in query.split("&"):
                if "=" not in chunk:
                    continue
                k, v = chunk.split("=", 1)
                if k.strip().lower() == name:
                    return v.strip().lower()
            return ""
    except Exception:
        return ""
    return ""


def _resolve_field_type_fqcn(receiver: str, controller: dict,
                             indexes: dict) -> str:
    """Map a field name (``orderService``) back to its FQCN.

    Consults the controller's ``autowired_fields`` first. Falls back to
    a simple-name lookup in ``by_simple`` (``OrderService`` →
    ``com.example.service.OrderService``) if the field wasn't captured
    as an autowired field.

    Searches services, mappers (DAO/Repository), AND controllers so
    that Nexcore-style chains (Controller → Service → DAO → SQL) and
    controller-to-controller delegation both resolve.
    """
    walkable = indexes["services_by_fqcn"]
    walkable2 = indexes["mappers_by_fqcn"]
    walkable3 = indexes["controllers_by_fqcn"]
    by_simple = indexes["by_simple"]

    def _in_any(fqcn):
        return fqcn in walkable or fqcn in walkable2 or fqcn in walkable3

    for f in controller.get("autowired_fields", []):
        if f.get("name") == receiver:
            fqcn = f.get("type_fqcn") or ""
            if _in_any(fqcn):
                return fqcn
            # Try simple-name fallback
            for cand in by_simple.get(f.get("type_simple", ""), []):
                if _in_any(cand["fqcn"]):
                    return cand["fqcn"]
            return ""
    # Name-based fallback — receiver could itself be a type name
    # (static call) or the field name happens to match a simple type.
    for cand in by_simple.get(receiver, []):
        if _in_any(cand["fqcn"]):
            return cand["fqcn"]
    # Try capitalising the receiver (common Java convention)
    simple_candidate = receiver[:1].upper() + receiver[1:] if receiver else ""
    for cand in by_simple.get(simple_candidate, []):
        if _in_any(cand["fqcn"]):
            return cand["fqcn"]
    return ""


def _find_method_in_class(cls: dict, method_name: str) -> dict | None:
    """Return the method dict in ``cls`` whose ``name`` matches, or None."""
    for m in cls.get("methods") or []:
        if m.get("name") == method_name:
            return m
    return None


def _collect_body_calls(method: dict, mybatis_idx: dict) -> tuple[set, set, set, set]:
    """Translate a method's ``body_*_calls`` into
    ``(xml_files, tables, rfcs, sql_ids)``.

    SQL calls are resolved first to a specific statement (``ns.id``)
    via :func:`_match_namespace`; this lets us read
    ``statement_to_tables`` and ``statement_to_xml_file`` directly so
    each row only reports the tables that its SQL actually touches.
    The set of matched ``namespace.id`` keys is returned as ``sql_ids``
    so callers can list the exact XML statements used by the endpoint.
    """
    xml_files = set()
    tables = set()
    rfcs = set()
    sql_ids = set()
    ns_to_xml = mybatis_idx["namespace_to_xml_files"]
    ns_to_tbl = mybatis_idx["namespace_to_tables"]
    stmt_to_tbl = mybatis_idx.get("statement_to_tables", {})
    stmt_to_xml = mybatis_idx.get("statement_to_xml_file", {})

    for sql in method.get("body_sql_calls") or []:
        ns = sql.get("namespace") or ""
        sql_id = sql.get("sql_id") or ""
        matched_ns = _match_namespace(ns, ns_to_xml)
        if matched_ns:
            stmt_key = f"{matched_ns}.{sql_id}"
            if stmt_key in stmt_to_tbl:
                sql_ids.add(stmt_key)
                tables.update(stmt_to_tbl[stmt_key])
                xml_file = stmt_to_xml.get(stmt_key)
                if xml_file:
                    xml_files.add(xml_file)
                continue
            # Statement id not recognized — 해당 statement 의 실제
            # tables/xml 은 알 수 없으므로 tables 컬럼에는 절대 주입하지
            # 않는다. 과거 로직은 ``ns_to_tbl[matched_ns]`` 의 전 테이블을
            # row 에 쏟아 Tables 리스트를 오염시키고 CRUD 괄호가 누락된
            # 이름이 섞여나왔다 (사용자 제보). 진단용으로 sql_ids 에만
            # raw key 를 남겨 XML method 컬럼에서 "미해결 statement" 로
            # 드러나게 한다.
            raw_key = f"{matched_ns}.{sql_id}" if sql_id else matched_ns
            sql_ids.add(raw_key)
            _CHAIN_DIAG["unresolved_stmt"] += 1
            samples = _CHAIN_DIAG["unresolved_stmt_samples"]
            if len(samples) < 5 and raw_key not in samples:
                samples.append(raw_key)

    for rfc in method.get("body_rfc_calls") or []:
        name = rfc.get("name")
        if name:
            rfcs.add(name)

    return xml_files, tables, rfcs, sql_ids


def _resolve_endpoint_chain(endpoint: dict, controller: dict,
                            indexes: dict, mybatis_idx: dict,
                            rfc_depth: int = 3) -> dict:
    """Walk the controller method body and resolve the call graph.

    Returns a dict with:
      * services   — list of service FQCNs actually invoked by this
        endpoint's method body (depth-limited through transitive
        service-to-service calls)
      * xml_files  — MyBatis XML paths touched by the resolved chain
      * tables     — DB tables touched by the resolved chain
      * rfcs       — RFC function names invoked by the resolved chain
      * mapper_fqcns — mapper interface FQCNs (best-effort)
      * resolved_via — 'method-scope' | 'class-scope-fallback'

    If the endpoint's controller method cannot be located (or produces
    an empty call graph) the function falls back to the legacy
    class-wide aggregation so existing mocks keep working.
    """
    svc_index = indexes["services_by_fqcn"]
    iface_to_impl = indexes["iface_to_impl"]

    services: set[str] = set()
    service_methods: list[str] = []  # preserves call order, "FQCN#method"
    seen_service_methods: set[tuple] = set()
    xml_files: set[str] = set()
    tables: set[str] = set()
    rfcs: set[str] = set()
    sql_ids: set[str] = set()
    mapper_fqcns: list[str] = []

    # Prefer the explicit method index set by the parser (works even
    # when the endpoint's ``method_name`` doesn't match a Java method
    # name — e.g. @RestVerticle where method_name == class name).
    methods_list = controller.get("methods") or []
    root_method = None
    mname = endpoint.get("method_name") or ""
    idx = endpoint.get("_method_idx")
    if idx is not None and 0 <= idx < len(methods_list):
        root_method = methods_list[idx]
    elif mname:
        root_method = _find_method_in_class(controller, mname)

    if root_method is not None:
        visited: set[tuple] = set()
        # Queue of (method, owner_class_dict, depth)
        queue = [(root_method, controller, 0)]
        while queue:
            method, owner, depth = queue.pop()
            key = (owner.get("fqcn"), method.get("name"))
            if key in visited:
                continue
            visited.add(key)

            # Direct calls in this method's body
            xf, tb, rf, sids = _collect_body_calls(method, mybatis_idx)
            xml_files.update(xf)
            tables.update(tb)
            rfcs.update(rf)
            sql_ids.update(sids)

            if depth >= rfc_depth:
                # 더 깊은 service helper 가 있을 수 있는데 depth cap 으로
                # walk 중단. body_field_calls 에 inter-class call (this 아닌)
                # 이 있으면 그 만큼 transitive chain 을 놓치는 것 — 진단
                # 카운터에 기록.
                inter_class_calls = sum(
                    1 for fc in (method.get("body_field_calls") or [])
                    if fc.get("receiver") and fc.get("receiver") != "this"
                )
                if inter_class_calls:
                    _CHAIN_DIAG["truncated_chain"] += inter_class_calls
                continue

            # Follow field.method() calls into their service classes.
            for fc in method.get("body_field_calls") or []:
                receiver = fc.get("receiver") or ""
                target_method_name = fc.get("method") or ""
                # Same-class self call (``this.foo()``): stay inside the
                # current ``owner`` class so helper methods' SQL/RFC are
                # still attributed to this endpoint. Do NOT bump depth —
                # we're not crossing a service boundary, and rfc_depth
                # should only gate cross-class transitive calls.
                if receiver == "this":
                    if not target_method_name:
                        continue
                    self_method = _find_method_in_class(owner, target_method_name)
                    if self_method is not None:
                        queue.append((self_method, owner, depth))
                        # 같은 클래스 helper 도 service_methods 컬럼에 노출 —
                        # 원 메서드만 드러나면 `this.saveX()` 같은 비즈니스
                        # 핵심 helper 가 리포트에서 사라지고, biz extractor 의
                        # service_methods seed 에서도 빠져 체인이 끊긴다.
                        owner_fqcn = owner.get("fqcn") or ""
                        sm_key = (owner_fqcn, target_method_name)
                        if owner_fqcn and sm_key not in seen_service_methods:
                            seen_service_methods.add(sm_key)
                            service_methods.append(
                                f"{owner_fqcn}#{target_method_name}"
                            )
                    continue
                svc_fqcn = _resolve_field_type_fqcn(receiver, owner, indexes)
                if not svc_fqcn:
                    continue
                # If ``svc_fqcn`` is an interface with a known impl,
                # surface the impl FQCN in the report columns so
                # interface-backed services look the same as
                # direct-impl injection. Without this the Service column
                # shows ``UserDefineService#getUserSdptAuthLIst`` while
                # other (impl-injected) services show ``FooServiceImpl#...``.
                # ``iface_to_impl.get`` returns ``svc_fqcn`` unchanged
                # when no mapping exists, so direct-impl is unaffected.
                mapper_index = indexes["mappers_by_fqcn"]
                ctrl_index = indexes["controllers_by_fqcn"]
                impl_fqcn = iface_to_impl.get(svc_fqcn, svc_fqcn)
                services.add(impl_fqcn)
                sm_key = (impl_fqcn, target_method_name)
                if target_method_name and sm_key not in seen_service_methods:
                    seen_service_methods.add(sm_key)
                    service_methods.append(f"{impl_fqcn}#{target_method_name}")
                # Walk into the interface's impl if we have one.
                # Search services, mappers (DAO/Repository), and
                # controllers so Nexcore chains (Svc→DAO) resolve.
                impl_cls = (svc_index.get(impl_fqcn) or svc_index.get(svc_fqcn)
                            or mapper_index.get(impl_fqcn) or mapper_index.get(svc_fqcn)
                            or ctrl_index.get(impl_fqcn) or ctrl_index.get(svc_fqcn))
                if not impl_cls:
                    continue
                target_method = _find_method_in_class(impl_cls, target_method_name)
                if target_method is not None:
                    queue.append((target_method, impl_cls, depth + 1))
                else:
                    # Method not found inside the impl body — fall back
                    # to class-level aggregation for THIS service only,
                    # so we still capture something sensible.
                    for sql in impl_cls.get("sql_calls", []) or []:
                        ns = sql.get("namespace") or ""
                        sql_id = sql.get("sql_id") or ""
                        matched_ns = _match_namespace(
                            ns, mybatis_idx["namespace_to_xml_files"]
                        )
                        if not matched_ns:
                            continue
                        stmt_key = f"{matched_ns}.{sql_id}"
                        stbl = mybatis_idx.get("statement_to_tables", {})
                        sxml = mybatis_idx.get("statement_to_xml_file", {})
                        sql_ids.add(stmt_key)
                        if stmt_key in stbl:
                            tables.update(stbl[stmt_key])
                            if stmt_key in sxml:
                                xml_files.add(sxml[stmt_key])
                        # statement id 가 namespace 에 없으면 sql_ids
                        # diagnostic 만 남기고 tables/xml_files 는 추가하지
                        # 않는다 (_collect_body_calls 와 동일 정책).
                    for rfc in impl_cls.get("rfc_calls", []) or []:
                        if rfc.get("name"):
                            rfcs.add(rfc["name"])

        mapper_fqcns = sorted(
            fqcn for fqcn in services if fqcn in indexes["mappers_by_fqcn"]
        )
        table_crud = _derive_table_crud(sql_ids, mybatis_idx)
        column_crud = _derive_column_crud(sql_ids, mybatis_idx)
        procs = _derive_procedures(sql_ids, mybatis_idx)
        return {
            "services": sorted(services),
            "service_methods": service_methods,
            "xml_files": sorted(xml_files),
            "tables": sorted(tables),
            "table_crud": table_crud,
            "column_crud": column_crud,
            "procs": procs,
            "rfcs": sorted(rfcs),
            "sql_ids": sorted(sql_ids),
            "mapper_fqcns": mapper_fqcns,
            "resolved_via": "method-scope",
        }

    # --- Fallback: legacy class-wide aggregation (no method match) ----
    service_fqcns = _find_service_fqcns(controller, indexes)
    xml_files_l, tables_l, mapper_fqcns = _resolve_mapper_chain(
        service_fqcns, indexes, mybatis_idx
    )
    rfc_names = _collect_rfc_transitive(
        service_fqcns, indexes, controller.get("rfc_calls", []), depth=rfc_depth
    )
    # Class-scope fallback has no per-statement resolution (it aggregates
    # whole-class tables via namespace). Leave table_crud empty so tables
    # still render but without suffix.
    return {
        "services": service_fqcns,
        "service_methods": [],
        "xml_files": xml_files_l,
        "tables": tables_l,
        "table_crud": {},
        "column_crud": {},
        "procs": [],
        "rfcs": rfc_names,
        "sql_ids": [],
        "mapper_fqcns": mapper_fqcns,
        "resolved_via": "class-scope-fallback",
    }


def _derive_procedures(sql_ids, mybatis_idx: dict) -> list[str]:
    """Collect Oracle procedure calls across every resolved statement.

    Uses the per-statement ``procedures`` lists produced by
    :func:`mybatis_parser.extract_procedure_calls`. Deduplicates while
    preserving first-seen order so the Programs row lists the primary
    procedure first. Statements not present in ``statement_to_procs`` or
    matched only via the namespace fallback produce no entries.
    """
    stmt_to_procs = mybatis_idx.get("statement_to_procs", {})
    out: list[str] = []
    seen: set[str] = set()
    for key in sql_ids or ():
        for p in stmt_to_procs.get(key, ()):
            if p not in seen:
                seen.add(p)
                out.append(p)
    return out


def _derive_table_crud(sql_ids, mybatis_idx: dict) -> dict[str, set]:
    """Build ``{table: set("C"|"R"|"U"|"D")}`` from resolved statement keys.

    CRUD letters come **only from SQL body analysis** (not the MyBatis
    tag). Tag-based classification was dropped because it follows
    developer intent rather than the actual query — e.g. a ``<select>``
    with a PL/SQL ``BEGIN ... UPDATE ... END;`` body should NOT be
    labeled R. ``extract_crud_from_sql`` already:

    * Strips block / line comments and single-quoted string literals
    * Drops ``SELECT ... FOR UPDATE`` (lock hint) before scanning
    * Matches ``\\b(INSERT|UPDATE|DELETE|MERGE|SELECT)\\b`` — MERGE
      bodies naturally contain INSERT+UPDATE in WHEN clauses so C+U
      fall out for free.

    Trade-off: a pure ``CALL pkg.proc(...)`` statement with no DML
    keyword in its body produces an empty letter set. Tables (if any
    get extracted) render with no suffix — acceptable because the
    actual mutation happens inside the stored procedure, which the
    static scanner cannot see anyway.
    """
    stmt_to_tbl = mybatis_idx.get("statement_to_tables", {})
    stmt_to_body_crud = mybatis_idx.get("statement_to_body_crud", {})
    out: dict[str, set] = {}
    for key in sql_ids or ():
        letters = stmt_to_body_crud.get(key) or set()
        if not letters:
            continue
        for tbl in stmt_to_tbl.get(key, ()):
            out.setdefault(tbl, set()).update(letters)
    return out


def _derive_column_crud(sql_ids, mybatis_idx: dict) -> dict[str, dict[str, set]]:
    """Aggregate per-column CRUD across every resolved statement.

    Parallel to :func:`_derive_table_crud` but with column granularity.
    Returns ``{TABLE: {COL: set("C"|"R"|"U"|"D")}}``. Tables / columns
    that never got a letter (e.g. statement sqlglot parse failed) are
    simply absent — callers fall back to the table-level result which
    already exists.

    Only statements present in ``statement_to_column_usage`` contribute.
    Namespace-fallback keys (no ``.id``) have no entry so they're
    silently skipped.
    """
    stmt_to_cols = mybatis_idx.get("statement_to_column_usage", {})
    out: dict[str, dict[str, set]] = {}
    for key in sql_ids or ():
        col_map = stmt_to_cols.get(key) or {}
        for tbl, cols in col_map.items():
            dst = out.setdefault(tbl, {})
            for col, letters in cols.items():
                dst.setdefault(col, set()).update(letters)
    return out


def trace_chain_events(endpoint: dict, controller: dict,
                         indexes: dict, mybatis_idx: dict,
                         rfc_depth: int = 3) -> list[dict]:
    """Walk the endpoint chain and return per-call events in source order.

    Mermaid sequence diagram (Phase A) 용. ``_resolve_endpoint_chain`` 과
    동일한 탐색 로직을 가지지만 set 누적 대신 **호출 순서를 보존한 event
    리스트** 를 반환. 각 event 는 ``kind`` 로 분기:

    * ``call``   — cross-class or self method call. ``from_class`` →
                   ``to_class`` (method 이름 포함)
    * ``sql``    — MyBatis mapper 호출. ``from_class`` → XML namespace
                   (sql_id 포함). 매칭된 statement 의 tables 는
                   ``tables`` 필드로 동봉.
    * ``rfc``    — SAP RFC 호출. ``from_class`` → ``RFC`` participant.

    Event 순서는 source offset 기반 (depth-first). 같은 method 내에서는
    파서가 기록한 ``offset`` 필드로 정렬. 서브 호출은 부모의 offset
    자리에 inline 으로 재귀 삽입 (sequence 가 자연스러운 호출-복귀 형태).
    """
    svc_index = indexes["services_by_fqcn"]
    iface_to_impl = indexes.get("iface_to_impl", {})
    mapper_index = indexes.get("mappers_by_fqcn", {})
    ctrl_index = indexes.get("controllers_by_fqcn", {})

    events: list[dict] = []
    visited: set[tuple] = set()

    stmt_to_tbl = mybatis_idx.get("statement_to_tables", {})
    ns_to_tbl = mybatis_idx.get("namespace_to_tables", {})

    def _emit_method_events(method: dict, owner: dict, depth: int) -> None:
        """Walk one method body emitting events in offset order."""
        key = (owner.get("fqcn"), method.get("name"))
        if key in visited:
            return
        visited.add(key)

        # 세 가지 call 종류를 offset 정렬로 merge
        calls_by_offset: list[tuple[int, str, dict]] = []
        for fc in method.get("body_field_calls") or []:
            calls_by_offset.append((fc.get("offset", 0), "field", fc))
        for sc in method.get("body_sql_calls") or []:
            calls_by_offset.append((sc.get("offset", 0), "sql", sc))
        for rc in method.get("body_rfc_calls") or []:
            calls_by_offset.append((rc.get("offset", 0), "rfc", rc))

        # SQL / RFC 가 같은 offset 의 field_call 을 가린다 (sqlSession.selectList
        # 는 field_call 과 sql_call 양쪽에 기록되기 때문에 중복 제거).
        sql_rfc_offsets = {o for o, k, _ in calls_by_offset if k != "field"}
        calls_by_offset.sort(key=lambda t: t[0])

        # Mermaid Phase B — 제어 블록 context. 각 event 에 context_stack 부착.
        # control_blocks 는 body_start 기준 오름차순. 특정 offset 이 어느
        # 블록 안인지 결정하려면 그 offset 을 감싸는 모든 블록을 필터해
        # depth 순으로 정렬.
        #
        # 렌더러가 method 경계를 넘어간 context 를 구분할 수 있도록
        # method_key 를 포함한 block 복사본을 반환.
        control_blocks = method.get("body_control_blocks") or []
        method_key = f"{owner.get('fqcn', '')}#{method.get('name', '')}"

        def _context_for(off: int) -> list[dict]:
            enclosing = [b for b in control_blocks
                         if b["body_start"] <= off < b["body_end"]]
            enclosing.sort(key=lambda b: b["depth"])
            # 복사본 + method_key prefix 가 포함된 block_id 로 렌더러가
            # 같은 체인 sibling 을 식별 가능하게.
            return [{
                **b,
                "method_key": method_key,
                "block_id": f"{method_key}#{b.get('chain_id')}#{b.get('chain_index')}",
            } for b in enclosing]

        for off, kind, call in calls_by_offset:
            if kind == "field":
                if off in sql_rfc_offsets:
                    continue  # SQL / RFC 가 우선
                recv = call.get("receiver", "") or ""
                meth = call.get("method", "") or ""
                if not meth:
                    continue
                if recv == "this":
                    # self-call: 동일 owner 안에서 재귀. depth 증가 안 함
                    self_method = _find_method_in_class(owner, meth)
                    if self_method is None:
                        continue
                    events.append({
                        "kind": "call", "from_class": owner.get("fqcn"),
                        "to_class": owner.get("fqcn"), "method": meth,
                        "depth": depth, "self_call": True,
                        "context_stack": _context_for(off),
                    })
                    _emit_method_events(self_method, owner, depth)
                    continue
                # cross-class: receiver field → FQCN 해석
                svc_fqcn = _resolve_field_type_fqcn(recv, owner, indexes)
                if not svc_fqcn:
                    continue
                impl_fqcn = iface_to_impl.get(svc_fqcn, svc_fqcn)
                impl_cls = (svc_index.get(impl_fqcn) or svc_index.get(svc_fqcn)
                            or mapper_index.get(impl_fqcn) or mapper_index.get(svc_fqcn)
                            or ctrl_index.get(impl_fqcn) or ctrl_index.get(svc_fqcn))
                if impl_cls is None:
                    continue
                events.append({
                    "kind": "call", "from_class": owner.get("fqcn"),
                    "to_class": impl_fqcn, "method": meth,
                    "depth": depth, "self_call": False,
                    "context_stack": _context_for(off),
                })
                if depth + 1 > rfc_depth:
                    continue  # 체인은 명시하되 그 안쪽 body 는 탐색 중단
                target_method = _find_method_in_class(impl_cls, meth)
                if target_method is not None:
                    _emit_method_events(target_method, impl_cls, depth + 1)
            elif kind == "sql":
                ns_raw = call.get("namespace") or ""
                sid = call.get("sql_id") or ""
                matched = _match_namespace(ns_raw, mybatis_idx["namespace_to_xml_files"])
                tables: list[str] = []
                if matched:
                    stmt_key = f"{matched}.{sid}"
                    tables = list(stmt_to_tbl.get(stmt_key, ()))
                    if not tables:
                        tables = list(ns_to_tbl.get(matched, ()))
                events.append({
                    "kind": "sql",
                    "from_class": owner.get("fqcn"),
                    "namespace": matched or ns_raw,
                    "sql_id": sid,
                    "op": call.get("op", "").lower(),
                    "tables": tables,
                    "depth": depth,
                    "context_stack": _context_for(off),
                })
            elif kind == "rfc":
                events.append({
                    "kind": "rfc",
                    "from_class": owner.get("fqcn"),
                    "rfc_name": call.get("name", ""),
                    "depth": depth,
                    "context_stack": _context_for(off),
                })

    methods_list = controller.get("methods") or []
    mname = endpoint.get("method_name") or ""
    idx = endpoint.get("_method_idx")
    root_method = None
    if idx is not None and 0 <= idx < len(methods_list):
        root_method = methods_list[idx]
    elif mname:
        root_method = _find_method_in_class(controller, mname)
    if root_method is None:
        return events
    _emit_method_events(root_method, controller, 0)
    return events


def _inherit_class_paths(controller: dict, controllers_by_fqcn: dict) -> list[str]:
    """If a controller has no class-level mapping but extends another
    controller, inherit the parent's class-level path. Recursive to cover
    multi-level hierarchies, bounded depth 5.
    """
    paths = controller.get("class_request_mapping") or [""]
    if paths and any(p for p in paths):
        return paths
    parent_fqcn = controller.get("extends") or ""
    for _ in range(5):
        parent = controllers_by_fqcn.get(parent_fqcn)
        if not parent:
            break
        parent_paths = parent.get("class_request_mapping") or [""]
        if any(p for p in parent_paths):
            return parent_paths
        parent_fqcn = parent.get("extends") or ""
    return paths


def _menu_only_row(menu_entry: dict, base_dirs: dict) -> dict:
    """Placeholder row for a menu entry that didn't match any endpoint.

    Backend / chain 컬럼은 여전히 빈 값이지만, **메뉴 URL 이 Route
    선언에 직접 존재** (e.g. index.js 의
    ``<Route path="/apps/gipms-materialmasternew"/>``) 하면 frontend 쪽
    컬럼 (presentation_layer / frontend_project) 은 채운다. backend
    endpoint 가 그 메뉴 URL 과 일치하지 않는 프로젝트 (frontend-URL 전용
    메뉴) 에서도 "어느 파일이 이 메뉴를 구현하는지" 정보는 유의미.
    """
    react_url_map = base_dirs.get("react_url_map") or {}
    url_strip = base_dirs.get("url_strip") or None
    frontend_dir = base_dirs.get("frontend_dir") or ""
    raw_menu_url = menu_entry.get("url", "")
    menu_url_norm = normalize_url(raw_menu_url, url_strip) if raw_menu_url else ""
    re_entry = react_url_map.get(menu_url_norm) if menu_url_norm else None
    presentation_layer = ""
    frontend_project = ""
    if re_entry:
        abs_path = re_entry.get("file_path") or re_entry.get("declared_in") or ""
        # _build_row 와 동일한 정책: 절대경로면 frontend_dir 기준
        # 상대경로로 변환해 다른 row 와 같은 포맷으로 맞춤.
        if abs_path and frontend_dir and os.path.isabs(abs_path):
            try:
                presentation_layer = os.path.relpath(abs_path, frontend_dir)
            except Exception:
                presentation_layer = abs_path
        else:
            presentation_layer = abs_path
        frontend_project = re_entry.get("frontend_name", "")
    return {
        "backend_project": "",
        "backend_framework": "",
        "main_menu": menu_entry.get("main_menu", ""),
        "sub_menu": menu_entry.get("sub_menu", ""),
        "tab": menu_entry.get("tab", ""),
        "menu_path": menu_entry.get("menu_path", ""),
        "menu_url": raw_menu_url,
        "program_id": menu_entry.get("program_id", ""),
        "program_name": menu_entry.get("program_name", ""),
        "method_name": "",
        "http_method": "",
        "url": "",
        "file_name": "",
        "frontend_project": frontend_project,
        "presentation_layer": presentation_layer,
        "frontend_trigger": "",
        "frontend_validation_summary": "",
        "controller_class": "",
        "service_class": "",
        "service_methods": "",
        "biz_summary": "",
        "biz_detail_key": "",
        "query_xml": "",
        "sql_ids": "",
        "related_tables": "",
        "related_columns": "",
        "procedures": "",
        "rfc": "",
        "sequence_diagram": "",
        "matched": False,
        "resolved_via": "",
    }


def _split_rows_per_trigger(rows: list[dict]) -> list[dict]:
    """``--row-per-trigger`` 활성 시 각 row 의 frontend_trigger (``;\\n`` join)
    를 단일 라벨 단위로 분리해 N row 로 확장.

    같은 endpoint 가 여러 트리거 (조회/등록/등록완료/...) 에서 호출될 때
    한 셀에 모이는 대신 각 트리거 = 1 row 로 펼침. 백엔드 chain
    (Controller / Service / XML / Tables) 은 동일 복제. 사용자 요청:
    "이벤트 별 백엔드 chain" 시각화. trigger 가 0~1 개 row 는 그대로.
    """
    out: list[dict] = []
    for r in rows:
        triggers_raw = r.get("frontend_trigger") or ""
        triggers = [t.strip() for t in triggers_raw.split(";\n") if t.strip()]
        if len(triggers) <= 1:
            out.append(r)
            continue
        for t in triggers:
            new_r = dict(r)
            new_r["frontend_trigger"] = t
            out.append(new_r)
    return out


def _reorder_rows_by_menu(rows: list[dict], menu_rows: list[dict] | None,
                           base_dirs: dict) -> list[dict]:
    """Reorder ``rows`` to follow the **menu.md source order** and
    emit a menu-only placeholder for every menu entry that wasn't
    matched to any endpoint.

    The invariant: Program Detail has **one row per menu entry** at
    minimum (placeholder when no backend mapping). Menu entries that
    match multiple endpoints expand into multiple rows (one per
    endpoint) but those rows stay clustered in their menu's position.

    When ``menu_rows`` is empty (e.g. ``--skip-menu``) there is no
    "menu order" to enforce — return the rows as-is (matched + unmatched)
    so the backend-only report retains all endpoint rows.
    """
    if not menu_rows:
        # --skip-menu or no menu loaded: preserve all rows for legacy
        # backend-only reporting.
        return list(rows)

    matched_by_url: dict[str, list[dict]] = {}
    for r in rows:
        if not r.get("matched"):
            continue
        mu = r.get("menu_url", "")
        matched_by_url.setdefault(mu, []).append(r)

    ordered: list[dict] = []
    for m in menu_rows:
        url = m.get("url", "")
        if url in matched_by_url:
            ordered.extend(matched_by_url[url])
        else:
            ordered.append(_menu_only_row(m, base_dirs))
    return ordered


def _build_row(endpoint: dict, controller: dict, indexes: dict,
               mybatis_idx: dict, menu_entry: dict | None,
               react_file: str | None, base_dirs: dict,
               rfc_depth: int = 3,
               menu_raw_url: str = "",
               react_entry: dict | None = None,
               frontend_trigger: str = "",
               emit_sequence_diagram: bool = False,
               sequence_diagram_with_frontend: bool = False) -> dict:
    """Assemble a single program-row dict for one controller endpoint.

    Resolution runs against the **controller method's own body** when
    possible (precise, per-endpoint) and falls back to class-scope
    aggregation when the method can't be located in the parsed data.
    """
    chain = _resolve_endpoint_chain(
        endpoint, controller, indexes, mybatis_idx, rfc_depth=rfc_depth
    )
    service_fqcns = chain["services"]
    service_methods = chain.get("service_methods", [])
    xml_files = chain["xml_files"]
    tables = chain["tables"]
    mapper_fqcns = chain["mapper_fqcns"]
    rfc_names = chain["rfcs"]

    # Mermaid sequence diagram (Phase A) — opt-in. 같은 체인을 한 번 더
    # walk 하지만 event 순서 (source offset) 보존. LLM 불필요.
    sequence_diagram_text = ""
    if emit_sequence_diagram:
        try:
            events = trace_chain_events(
                endpoint, controller, indexes, mybatis_idx, rfc_depth=rfc_depth,
            )
            from .legacy_mermaid import render_sequence_diagram
            # frontend portion 은 opt-in (--sequence-diagram-frontend).
            # 비활성 시 빈 문자열 전달 → backend-only 다이어그램 (회귀
            # 호환).
            _ft = frontend_trigger or "" if sequence_diagram_with_frontend else ""
            _pl = react_file or "" if sequence_diagram_with_frontend else ""
            sequence_diagram_text = render_sequence_diagram(
                events, endpoint, controller.get("fqcn", ""),
                frontend_trigger=_ft,
                presentation_layer=_pl,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("sequence diagram render failed: %s", e)
    sql_ids = chain.get("sql_ids", [])

    # Relative file paths for readability. Both Java sources and MyBatis
    # XMLs live under ``backend_dir`` now, so we resolve both against it.
    backend_dir = base_dirs.get("backend_dir") or ""
    frontend_dir = base_dirs.get("frontend_dir") or ""

    def _rel(path: str, base: str) -> str:
        if not path or not base:
            return path or ""
        try:
            import os
            return os.path.relpath(path, base)
        except Exception:
            return path

    row = {
        "backend_project": base_dirs.get("backend_project", ""),
        "backend_framework": base_dirs.get("backend_framework", ""),
        "main_menu": (menu_entry or {}).get("main_menu", ""),
        "sub_menu": (menu_entry or {}).get("sub_menu", ""),
        "tab": (menu_entry or {}).get("tab", ""),
        "menu_path": (menu_entry or {}).get("menu_path", ""),
        "menu_url": menu_raw_url or (menu_entry or {}).get("url", ""),
        "program_id": (menu_entry or {}).get("program_id", ""),
        "program_name": (menu_entry or {}).get("program_name", "") or endpoint["method_name"],
        # method_name 은 Controller/Verticle 의 Java 메서드명 원본. program_name 은
        # 메뉴 매칭 시 메뉴의 화면명으로 덮이기 때문에, biz 추출 등 Java 메서드
        # 레벨에서 접근해야 하는 소비자는 이 필드를 사용.
        "method_name": endpoint.get("method_name") or "",
        "http_method": endpoint["http_method"],
        "url": endpoint["full_url"],
        "file_name": _rel(controller["filepath"], backend_dir),
        "frontend_project": (react_entry or {}).get("frontend_name", ""),
        # react_file may be (a) a single absolute path from the Router
        # scanner → _rel it to frontend_dir, or (b) a ";\n"-joined list
        # already relative to frontends_root (from the 2-hop api
        # scanner). Only normalize the first case (single absolute path).
        "presentation_layer": (
            _rel(react_file, frontend_dir)
            if react_file and ";" not in react_file and os.path.isabs(react_file)
            else (react_file or "")
        ),
        "frontend_trigger": frontend_trigger,
        "frontend_validation_summary": "",  # Phase B 가 나중에 채움
        "controller_class": controller["fqcn"],
        # 여러 항목이 들어가는 컬럼은 Excel 셀에서 한 항목당 한 줄씩
        # 보이도록 ``;\n`` / ``,\n`` 로 구분. 셀은 wrap_text=True 가 이미
        # 적용돼 있어 실제 줄바꿈으로 렌더됨. 단일 항목이면 개행 없음.
        "service_class": ";\n".join(service_fqcns),
        "service_methods": ";\n".join(service_methods),
        "biz_summary": "",       # Phase A: enrich_rows_with_biz 가 나중에 채움
        "biz_detail_key": "",    # Phase A: enrich_rows_with_biz 가 나중에 채움
        "query_xml": ";\n".join(_rel(p, backend_dir) for p in xml_files),
        "sql_ids": ";\n".join(sql_ids),
        # Tables rendered with per-table CRUD suffix and one-per-line so
        # Excel cells wrap neatly (e.g. ``TABLE_A(R),\nTABLE_B(CRU)``).
        # RFC uses the same comma-newline layout for symmetry.
        "related_tables": _format_table_crud(tables, chain.get("table_crud") or {}),
        # Columns column (Phase I) — per-column CRUD from sqlglot AST
        # walker. Empty when sqlglot parse fails; table-level CRUD above
        # still reflects the operation set so no information is lost.
        "related_columns": _format_column_crud(
            chain.get("column_crud") or {}, base_dirs.get("terms_dict"),
        ),
        # Procedures column — Oracle stored procs invoked from this
        # endpoint's chain. Same ",\n" convention as Tables/RFC.
        "procedures": _format_list_with_newlines(chain.get("procs") or []),
        "rfc": _format_list_with_newlines(rfc_names),
        "sequence_diagram": sequence_diagram_text,
        "matched": menu_entry is not None,
        "resolved_via": chain.get("resolved_via", "method-scope"),
    }
    return row


def analyze_legacy(backend_dir: str, frontend_dir: str | None = None,
                   menu_rows: list[dict] | None = None,
                   rfc_depth: int = 3,
                   frontend_framework: str | None = None,
                   patterns: dict | None = None,
                   frontends_root: bool = False,
                   menu_only: bool = False,
                   precomputed_frontend: dict | None = None,
                   skip_menu_reorder: bool = False,
                   extract_biz: bool = False,
                   biz_scope: str = "both",
                   biz_max_methods: int = 500,
                   biz_max_handlers: int = 300,
                   biz_use_cache: bool = True,
                   biz_config: dict | None = None,
                   library_dirs: list[str] | None = None,
                   terms_md: str | None = None,
                   extract_program_spec: bool = False,
                   emit_sequence_diagram: bool = False,
                   sequence_diagram_with_frontend: bool = False,
                   row_per_trigger: bool = False) -> dict:
    """Run the full legacy analysis and return a structured result.

    Parameters
    ----------
    backend_dir : str
        Root directory of the backend project. Both ``.java`` sources and
        MyBatis/iBatis mapper XMLs are scanned recursively under this
        path — build/VCS folders (``target``, ``build``, ``.git`` etc.)
        are skipped automatically, so a project root is a safe value.
    frontend_dir : str or None
        Root directory of the React source tree. If ``None``, the
        ``presentation_layer`` column is left blank.
    menu_rows : list of dict or None
        Pre-loaded DB menu rows from ``legacy_menu_loader``. Each row is
        ``{program_id, program_name, main_menu, sub_menu, tab, url}``. If
        ``None``, menu columns are left blank and ``unmatched_controllers``
        contains every endpoint.
    rfc_depth : int
        Maximum depth of service-of-service walk for RFC collection.

    Returns
    -------
    dict with keys:
        rows, unmatched_controllers, orphan_menus, stats
    """
    print(f"  Backend dir: {backend_dir}")
    _reset_chain_diag()
    framework = detect_backend_framework(backend_dir)
    _FRAMEWORK_LABEL = {
        "spring": "Spring (detected via pom/gradle or @Controller annotations)",
        "vertx": "Vert.x (detected via pom/gradle or AbstractVerticle usage)",
        "mixed": "mixed (both Spring and Vert.x signals present)",
        "unknown": "unknown (no framework signal detected - accepting both)",
    }
    print(f"  Backend framework: {_FRAMEWORK_LABEL[framework]}")

    # Apply discovered patterns to the parser before scanning
    if patterns:
        from .legacy_java_parser import apply_patterns
        apply_patterns(patterns)
        print(f"  Patterns applied: {patterns.get('framework_type', 'custom')} "
              f"({len(patterns.get('controller_base_classes', []))} base classes, "
              f"{len(patterns.get('sql_receivers', []))} sql receivers)")

    classes = parse_all_java(backend_dir)

    # Library repos (``--library-dir``): scanned for .java + MyBatis XMLs
    # but their classes are flagged ``is_library=True`` so ``_build_indexes``
    # keeps them OUT of the controller index (no endpoint rows generated).
    # They still feed services / mappers / by_simple so that the main
    # project's Controller → Service → Mapper chain can resolve against
    # shared-repo classes. Use case: ``gipms-main`` (handlers) calls
    # services from sibling ``gipms-common`` (pure service repo).
    library_classes: list[dict] = []
    library_dirs_effective = [d for d in (library_dirs or []) if d]
    for lib_dir in library_dirs_effective:
        print(f"  Library dir: {lib_dir}")
        lib_cls = parse_all_java(lib_dir)
        for c in lib_cls:
            c["is_library"] = True
        library_classes.extend(lib_cls)
    if library_classes:
        print(f"  Library classes indexed: {len(library_classes)} (from {len(library_dirs_effective)} lib dirs)")
        classes.extend(library_classes)

    # Apply framework gating to endpoint lists BEFORE counting, so that
    # the "endpoints discovered" diagnostic matches what the role index
    # sees. For ``mixed`` / ``unknown`` this is a no-op.
    _filter_endpoints_by_framework(classes, framework)

    # Diagnostic: stereotype + endpoint + SQL-call + RFC-call distribution
    # so users can self-diagnose why a project might parse to zero
    # controllers / mappers / mapper chains / RFC calls.
    from collections import Counter
    stereo_dist = Counter(c.get("stereotype") or "(none)" for c in classes)
    dist_str = ", ".join(f"{k}={v}" for k, v in sorted(stereo_dist.items()))
    ep_total = sum(len(c.get("endpoints") or []) for c in classes)
    classes_with_eps = sum(1 for c in classes if c.get("endpoints"))
    sql_total = sum(len(c.get("sql_calls") or []) for c in classes)
    classes_with_sql = sum(1 for c in classes if c.get("sql_calls"))
    sql_namespaces = {
        call["namespace"]
        for c in classes
        for call in (c.get("sql_calls") or [])
        if call.get("namespace")
    }
    rfc_total = sum(len(c.get("rfc_calls") or []) for c in classes)
    classes_with_rfc = sum(1 for c in classes if c.get("rfc_calls"))
    rfc_names = {
        r["name"]
        for c in classes
        for r in (c.get("rfc_calls") or [])
        if r.get("name")
    }
    rfc_hints_total = sum(c.get("rfc_hint_count", 0) for c in classes)
    print(f"  Classes parsed: {len(classes)}")
    print(f"  Stereotype distribution: {dist_str}")
    print(f"  Endpoints discovered in parser: {ep_total} "
          f"(in {classes_with_eps} classes)")
    print(f"  SQL helper calls: {sql_total} in {classes_with_sql} classes, "
          f"{len(sql_namespaces)} distinct namespaces")
    print(f"  RFC calls: {rfc_total} in {classes_with_rfc} classes, "
          f"{len(rfc_names)} distinct names")
    if rfc_names and rfc_total > 0:
        sample = sorted(rfc_names)[:8]
        more = f", … (+{len(rfc_names) - 8} more)" if len(rfc_names) > 8 else ""
        print(f"    sample: {', '.join(sample)}{more}")
    if rfc_hints_total and rfc_total < rfc_hints_total:
        print(f"    (hint: {rfc_hints_total} call sites match the loose "
              f"'.*Function(' pattern but only {rfc_total} were captured "
              f"- the strict regex may be missing the project's actual "
              f"method-name or call shape)")

    mybatis_result = parse_all_mappers(backend_dir)
    # Also scan library repos for MyBatis XMLs — DAO interfaces in
    # gipms-common typically have their mapper XMLs colocated, so without
    # this the service → mapper → tables chain would stop at the DAO
    # interface. Merge statement lists + re-derive table_usage / joins
    # as a flat union (namespaces are globally unique by FQCN so no
    # collision risk).
    for lib_dir in library_dirs_effective:
        # 진단: 동일 path 가 직접 호출에선 11 잡히는데 library-dir 모드에선
        # 0 으로 보이는 케이스. abs path / cwd / dir 존재 여부를 한 줄
        # emit 해서 path 변형 가능성 즉시 검증.
        abs_lib = os.path.abspath(lib_dir)
        print(f"  Library dir resolve: input={lib_dir!r} abs={abs_lib!r} "
              f"exists={os.path.isdir(abs_lib)} cwd={os.getcwd()!r}")
        lib_result = parse_all_mappers(lib_dir)
        lib_mc = lib_result.get("mapper_count", 0)
        lib_sc = lib_result.get("statement_count", 0)
        print(f"  Library mappers from {lib_dir}: "
              f"{lib_mc} XMLs, {lib_sc} statements")
        if lib_result.get("statements"):
            mybatis_result["statements"].extend(lib_result["statements"])
            mybatis_result["mapper_count"] = (
                mybatis_result.get("mapper_count", 0) + lib_mc
            )
            mybatis_result["statement_count"] = (
                mybatis_result.get("statement_count", 0) + lib_sc
            )
    mybatis_idx = _build_mybatis_indexes(mybatis_result)
    xml_namespaces = set(mybatis_idx["namespace_to_xml_files"].keys())

    # How many of the SQL call namespaces actually match an XML namespace?
    matched_ns = 0
    for raw_ns in sql_namespaces:
        if _match_namespace(raw_ns, mybatis_idx["namespace_to_xml_files"]):
            matched_ns += 1
    print(f"  Mapper XMLs parsed: {mybatis_result.get('mapper_count', 0)} "
          f"({len(xml_namespaces)} namespaces); "
          f"SQL-call namespaces matched: {matched_ns}/{len(sql_namespaces)}")

    indexes = _build_indexes(
        classes, framework=framework,
        mybatis_namespaces=xml_namespaces,
        patterns=patterns,
    )
    indexes["iface_to_impl"] = _resolve_service_impls(
        indexes["services_by_fqcn"], indexes["by_simple"]
    )
    # Report how many of those controllers came from stereotype match vs
    # the endpoint-promotion fallback, so users can understand the flow.
    promoted = sum(
        1 for c in indexes["controllers_by_fqcn"].values()
        if c.get("stereotype") not in ("Controller", "RestController", "Verticle")
    )
    promo_note = f" (of which {promoted} promoted via endpoint-only rule)" if promoted else ""
    print(f"  Role index: controllers={len(indexes['controllers_by_fqcn'])}"
          f"{promo_note} services={len(indexes['services_by_fqcn'])} "
          f"mappers={len(indexes['mappers_by_fqcn'])}")

    # URL-convention patterns (from discover-patterns LLM or hand-written):
    # thread strip_patterns + route_prefix through every URL normalizer.
    url_section = (patterns or {}).get("url") or {}
    url_strip = url_section.get("url_prefix_strip") or []
    route_prefix = url_section.get("react_route_prefix")
    app_key_spec = url_section.get("app_key")
    if url_strip or route_prefix or app_key_spec:
        print(f"  URL conventions: strip={len(url_strip)} "
              f"route_prefix={route_prefix!r} app_key={app_key_spec}")

    # Menu-driven scan: extract the set of app slugs referenced by any
    # menu entry so the frontend scanner can skip unreferenced buckets.
    # `None` means "no app_key configured" → scan everything (legacy).
    menu_apps: set[str] | None = None
    if app_key_spec:
        slugs = set()
        for r in menu_rows or []:
            raw = r.get("url", "")
            s = _extract_app_key(raw, app_key_spec)
            if s:
                slugs.add(s)
        menu_apps = slugs if slugs else None
        if menu_apps and menu_only:
            print(f"  Menu-driven scope: {len(menu_apps)} apps referenced by menu "
                  f"({sorted(menu_apps)[:5]}{'...' if len(menu_apps) > 5 else ''})")

    react_url_map = {}
    by_frontend: dict[str, dict] = {}
    api_by_frontend: dict[str, dict] = {}
    triggers_by_frontend: dict[str, dict] = {}
    single_api_index: dict[str, list[str]] = {}
    single_triggers: dict[str, list[str]] = {}
    detected_frontend = "unknown"
    # frontend 스캔 단계는 같은 파일을 router/import-graph/api-scanner/
    # trigger 등 4~5개 path 가 각각 다시 읽어서 디스크 I/O 가 dominant.
    # scoped cache 를 켜고 단계 종료 시 즉시 비워 메모리 폭증 방지.
    from .mybatis_parser import use_file_cache as _use_fc
    _use_fc(True)
    if precomputed_frontend:
        # Batch mode hoists this work above the per-backend loop.
        react_url_map = precomputed_frontend.get("react_url_map") or {}
        detected_frontend = precomputed_frontend.get("detected_frontend") or "unknown"
        by_frontend = precomputed_frontend.get("by_frontend") or {}
        api_by_frontend = precomputed_frontend.get("api_by_frontend") or {}
        triggers_by_frontend = precomputed_frontend.get("triggers_by_frontend") or {}
        single_api_index = precomputed_frontend.get("single_api_index") or {}
        single_triggers = precomputed_frontend.get("single_triggers") or {}
    elif frontend_dir:
        try:
            from .legacy_frontend import (
                build_frontend_url_map, build_frontend_url_map_multi,
                build_frontend_api_index,
            )
            is_multi = frontends_root is not None
            if is_multi:
                print(f"  Frontends root: {frontend_dir}")
                # Menu-driven narrowing: skip apps not referenced by menu.
                # Only apply when menu_only is active so full audits still
                # scan everything.
                allowed = menu_apps if (menu_only and menu_apps) else None
                (react_url_map, detected_frontend, by_frontend,
                 api_by_frontend, triggers_by_frontend) = build_frontend_url_map_multi(
                    frontend_dir, framework=frontend_framework,
                    strip_patterns=url_strip, route_prefix=route_prefix,
                    patterns=patterns, allowed_apps=allowed,
                )
            else:
                print(f"  Frontend dir: {frontend_dir}")
                react_url_map, detected_frontend = build_frontend_url_map(
                    frontend_dir, framework=frontend_framework,
                    strip_patterns=url_strip, route_prefix=route_prefix,
                )
                single_api_index, single_triggers = build_frontend_api_index(
                    frontend_dir, patterns=patterns, strip_patterns=url_strip,
                )
            print(f"  Frontend framework: {detected_frontend}")
            print(f"  Frontend routes indexed: {len(react_url_map)}")
            if by_frontend:
                print(f"  Frontend buckets: {sorted(by_frontend)}")
            if api_by_frontend:
                total_api = sum(len(v) for v in api_by_frontend.values())
                print(f"  Frontend API calls indexed: {total_api} across "
                      f"{len(api_by_frontend)} buckets")
            elif single_api_index:
                print(f"  Frontend API calls indexed: {len(single_api_index)}")
            if triggers_by_frontend:
                total_trig = sum(len(v) for v in triggers_by_frontend.values())
                print(f"  Button triggers indexed: {total_trig} urls labeled")
            elif single_triggers:
                print(f"  Button triggers indexed: {len(single_triggers)} urls labeled")
        except Exception as e:
            logger.warning("Frontend scan skipped: %s", e)

    # Menu scope resolver — **import chain 기반** endpoint 매칭.
    # 사용자 지적: 폴더명 heuristic 은 hypm_materialMaster / gipms-... 처럼
    # folder 이름과 메뉴 URL slug 가 다를 때 부정확. 메뉴 URL → Route 선언
    # 파일 → import BFS 로 scope 파일 집합 → 그 파일들의 API 호출 → backend
    # endpoint 매칭 인덱스를 미리 구축.
    #
    # endpoint_to_menus: {normalized_endpoint_url: [menu_url_norm, ...]}
    # menu_to_scope_files: {menu_url_norm: [rel_file, ...]} — presentation/
    #   screen_files 소스로도 활용 가능.
    endpoint_to_menus: dict[str, list[str]] = {}
    menu_to_scope_files: dict[str, list[str]] = {}
    if frontend_dir and react_url_map and menu_rows:
        try:
            import os as _os
            import time as _time
            _t0 = _time.time()
            from .legacy_react_router import (
                build_import_graph, collect_menu_scope_files, scan_react_dir,
            )
            from .legacy_react_api_scanner import (
                _build_api_url_index_from_files,
            )
            # 1) import graph (서브 레포별 빌드 후 합집합).
            import_graphs: list[tuple[str, dict]] = []
            if frontends_root is not None:
                from .legacy_frontend import _enumerate_buckets
                for _name, bucket_path in _enumerate_buckets(frontend_dir):
                    import_graphs.append(
                        (bucket_path, build_import_graph(bucket_path))
                    )
            else:
                import_graphs.append((frontend_dir, build_import_graph(frontend_dir)))
            merged_graph: dict[str, set[str]] = {}
            for _root, g in import_graphs:
                merged_graph.update(g)
            # 2) **ALL files 1회만 API URL 스캔** → reverse index.
            #    이전 구현은 menu 마다 _build_api_url_index_from_files 를
            #    재호출해 같은 파일을 N 번 읽음 (N = menu 수). 이걸 1번
            #    스캔 + dict lookup 으로 평탄화. 사용자 제보: 수행이
            #    매우 느렸던 주 원인.
            all_react_files: set[str] = set()
            for _root, _ in import_graphs:
                all_react_files.update(scan_react_dir(_root))
            full_api_idx = _build_api_url_index_from_files(
                sorted(all_react_files), frontend_dir,
                patterns=patterns, strip_patterns=url_strip,
            )
            # rel_file → set(api_url) reverse map.
            file_to_apis: dict[str, set[str]] = {}
            for url, files in full_api_idx.items():
                for rel in files:
                    file_to_apis.setdefault(rel, set()).add(url)
            # 3) 각 menu 별 scope (BFS) → scope 파일의 API URL 합집합.
            scope_linked_count = 0
            for r in (menu_rows or []):
                m_url = normalize_url(r.get("url", ""), url_strip)
                if not m_url or m_url not in react_url_map:
                    continue
                scope = collect_menu_scope_files(m_url, react_url_map, merged_graph)
                if not scope:
                    continue
                scope_rel: list[str] = []
                menu_apis: set[str] = set()
                for fabs in scope:
                    try:
                        rel = _os.path.relpath(fabs, frontend_dir)
                    except Exception:
                        continue
                    scope_rel.append(rel)
                    menu_apis.update(file_to_apis.get(rel, ()))
                if not menu_apis:
                    # scope 는 잡혔지만 API 호출 없는 메뉴도 정보로 남김.
                    menu_to_scope_files[m_url] = sorted(scope_rel)
                    continue
                menu_to_scope_files[m_url] = sorted(scope_rel)
                for endpoint_url in menu_apis:
                    endpoint_to_menus.setdefault(endpoint_url, []).append(m_url)
                scope_linked_count += 1
            if scope_linked_count:
                _elapsed = _time.time() - _t0
                print(f"  Menu import-chain scope: {scope_linked_count} menus "
                      f"linked to {len(endpoint_to_menus)} backend endpoints "
                      f"via {len(merged_graph)} files' imports ({_elapsed:.1f}s)")
        except Exception as e:
            logger.warning("Menu scope resolver skipped: %s", e)

    # frontend scan 끝났으니 cache 즉시 해제 (메모리 회수).
    _use_fc(False)

    # Menu URL index — preserve raw_url alongside the normalized key so
    # the app_key extractor can inspect the pre-normalization form later.
    menu_url_index = {}
    menu_raw_by_key = {}
    for r in (menu_rows or []):
        raw = r.get("url", "")
        key = normalize_url(raw, url_strip)
        if key:
            menu_url_index[key] = r
            menu_raw_by_key[key] = raw

    backend_project = os.path.basename(os.path.normpath(backend_dir or ""))
    base_dirs = {
        "backend_dir": backend_dir,
        "frontend_dir": frontend_dir or "",
        "backend_project": backend_project,
        "backend_framework": framework,
        # Column→Korean lookup for the Programs "Columns" render. ``None``
        # is valid — formatter just skips ``[한글]`` annotation when
        # terms dictionary isn't provided.
        "terms_dict": _load_terms_dict(terms_md) if terms_md else None,
        # _menu_only_row 가 orphan 메뉴의 frontend 필드를 채우기 위해
        # menu URL 을 react_url_map 에서 직접 조회한다. url_strip 도
        # 같이 넘겨야 normalize 결과가 일관.
        "react_url_map": react_url_map,
        "url_strip": url_strip,
    }

    rows = []
    unmatched = []
    controller_urls = set()
    skipped_no_menu = 0

    # Menu-driven "interesting URL" set. An endpoint is a candidate when
    # either (a) its URL directly matches a menu URL, or (b) it's an API
    # URL called from any React file under a menu-referenced app bucket
    # (2-hop). In --menu-only we use this to cheaply skip chain
    # resolution for endpoints that can never feed a Program Detail row.
    interesting_urls: set[str] = set(menu_url_index.keys())
    for _app, _idx in api_by_frontend.items():
        interesting_urls.update(_idx.keys())
    if single_api_index:
        interesting_urls.update(single_api_index.keys())
    # import-chain 으로 menu 에 귀속되는 backend endpoint 도 scope 에 포함.
    interesting_urls.update(endpoint_to_menus.keys())
    if menu_only:
        print(f"  Menu-driven interesting URLs: {len(interesting_urls)}")

    # Iterate every controller endpoint
    for controller in indexes["controllers_by_fqcn"].values():
        if controller.get("abstract"):
            continue
        class_paths = _inherit_class_paths(controller, indexes["controllers_by_fqcn"])
        endpoints = controller.get("endpoints") or []
        for ep in endpoints:
            key = normalize_url(ep["full_url"], url_strip)
            controller_urls.add(key)
            menu_entry = menu_url_index.get(key)

            # 신규: import-chain 기반 매칭.
            # endpoint URL 이 menu 의 scope 파일들에서 API 로 호출됐으면
            # 그 menu 로 귀속. 폴더 이름 heuristic 보다 정확 —
            # Route 선언이 있는 index.js 에서 import 로 연결된 실제 파일만
            # scope 에 포함되므로 false positive 거의 없음.
            import_chain_menu_key = ""
            if menu_entry is None:
                menu_keys = endpoint_to_menus.get(key) or []
                if menu_keys:
                    import_chain_menu_key = menu_keys[0]
                    menu_entry = menu_url_index.get(import_chain_menu_key)

            # menu_only optimization: skip expensive chain resolution
            # for endpoints that aren't in the interesting URL set. This
            # includes both direct menu matches and 2-hop candidates
            # (URLs called from any menu-referenced app's React files).
            if menu_only and not menu_entry and key not in interesting_urls:
                skipped_no_menu += 1
                unmatched.append({
                    "backend_project": base_dirs.get("backend_project", ""),
                    "backend_framework": base_dirs.get("backend_framework", ""),
                    "program_name": ep["method_name"],
                    "http_method": ep["http_method"],
                    "url": ep["full_url"],
                    "file_name": controller["filepath"],
                    "controller_class": controller["fqcn"],
                    "matched": False,
                })
                continue

            # Step 1 — app_slug from direct menu match (if any).
            app_slug = ""
            raw_menu_url = ""
            if menu_entry:
                raw_menu_url = menu_raw_by_key.get(key, menu_entry.get("url", ""))
                if app_key_spec:
                    app_slug = _extract_app_key(raw_menu_url, app_key_spec)
            # _extract_app_key returns lowercase; buckets are also stored
            # lowercase by build_frontend_url_map_multi. Case parity ⇒
            # direct .get() lookup works regardless of original casing.
            app_slug_lower = app_slug.lower() if app_slug else ""

            # Step 2 — 2-hop match via API-call index. If direct menu
            # match failed (menu URL ≠ controller URL — the common case
            # where menu points at app root and controller is a REST
            # API), look up the endpoint URL across every app bucket's
            # api index. First hit wins; we attribute the endpoint to
            # that app's menu row.
            screen_files: list[str] = []
            two_hop_app = ""
            if api_by_frontend:
                # Prefer the pre-known app bucket (from direct match).
                if app_slug_lower:
                    files = (api_by_frontend.get(app_slug_lower) or {}).get(key) or []
                    if files:
                        screen_files = list(files)
                        two_hop_app = app_slug_lower
                if not screen_files:
                    for app_name, idx in api_by_frontend.items():
                        files = idx.get(key)
                        if files:
                            screen_files = list(files)
                            two_hop_app = app_name
                            break
            elif single_api_index:
                screen_files = list(single_api_index.get(key) or [])

            # If no direct menu but 2-hop found an app bucket, attribute
            # endpoint to that app's menu row.
            if menu_entry is None and two_hop_app:
                promoted = _lookup_menu_by_app(menu_rows or [], app_key_spec,
                                                two_hop_app, by_frontend)
                if promoted:
                    menu_entry = promoted
                    raw_menu_url = menu_entry.get("url", "")
                    app_slug = two_hop_app
                    app_slug_lower = two_hop_app

            # react_entry (for frontend_project metadata + direct-match
            # presentation_layer) — prefer the resolved app's bucket.
            react_entry = None
            # Menu-URL 기반 직접 조회 (가장 신뢰성 높음): 메뉴 URL 이 곧
            # Route path (`<Route path="/apps/gipms-materialmasternew"/>`) 로
            # 선언돼 있으면 react_url_map 에서 직접 파일을 잡을 수 있다.
            # 이 경로는 backend endpoint URL 과 menu URL 이 달라도 성립 —
            # 사용자 사례 (folder 이름 ≠ URL slug) 의 핵심 해결책.
            if menu_entry:
                menu_url_norm = normalize_url(menu_entry.get("url", ""), url_strip)
                if menu_url_norm and menu_url_norm != key:
                    react_entry = react_url_map.get(menu_url_norm)
            if react_entry is None and app_slug_lower and by_frontend:
                react_entry = (by_frontend.get(app_slug_lower) or {}).get(key)
            if react_entry is None:
                react_entry = react_url_map.get(key)
            # If 2-hop supplied an app_slug but the Router didn't index
            # any routes (common: custom routing or menu hits app root),
            # synthesise a minimal react_entry so Frontend project column
            # isn't empty.
            if react_entry is None and app_slug_lower:
                react_entry = {"frontend_name": app_slug_lower, "file_path": ""}

            # Button labels — same bucketing logic as screen files.
            trigger_labels: list[str] = []
            if triggers_by_frontend:
                bucket = app_slug_lower or two_hop_app
                if bucket:
                    trigger_labels = list((triggers_by_frontend.get(bucket) or {}).get(key) or [])
            elif single_triggers:
                trigger_labels = list(single_triggers.get(key) or [])

            react_file = (react_entry or {}).get("file_path", "")
            # Prefer the concrete 2-hop screens as Frontend screen — they
            # are the files that actually call this endpoint. Fall back
            # to the router-matched file for simple projects.
            # ``;\n`` 구분자로 한 셀 안 한 줄씩 표시 — Excel / Markdown
            # 가독성. service_methods / query_xml / sql_ids 와 일관.
            presentation = ";\n".join(screen_files) if screen_files else react_file

            row = _build_row(
                ep, controller, indexes, mybatis_idx,
                menu_entry, presentation, base_dirs, rfc_depth=rfc_depth,
                menu_raw_url=raw_menu_url,
                react_entry=react_entry,
                frontend_trigger=";\n".join(trigger_labels),
                emit_sequence_diagram=emit_sequence_diagram,
                sequence_diagram_with_frontend=sequence_diagram_with_frontend,
            )
            rows.append(row)
            if not row["matched"]:
                unmatched.append(row)

    if skipped_no_menu:
        print(f"  menu-only: skipped {skipped_no_menu} non-matching endpoints "
              f"(chain resolution saved)")

    orphan_menus = []
    for key, m in menu_url_index.items():
        if key not in controller_urls:
            orphan_menus.append({
                "program_id": m.get("program_id", ""),
                "main_menu": m.get("main_menu", ""),
                "sub_menu": m.get("sub_menu", ""),
                "tab": m.get("tab", ""),
                "program_name": m.get("program_name", ""),
                "url": m.get("url", ""),
            })

    # Business logic extraction (Phase A: backend; Phase B: frontend). opt-in via
    # ``extract_biz=True``. scope 는 이미 _resolve_endpoint_chain 이
    # 결정한 service_methods 집합을 재사용 (사용자 결정: 엔드포인트 체인에
    # 걸린 메서드만). biz_map 은 result dict 에 실려 report 가 시트로 emit.
    biz_map: dict = {}
    fe_biz_map: dict = {}
    if extract_biz and biz_scope in ("backend", "both"):
        from . import legacy_biz_extractor as biz
        targets = biz.collect_chain_methods(rows, indexes)
        biz_map = biz.extract_backend_biz_logic(
            targets,
            patterns or {},
            max_methods=biz_max_methods,
            use_cache=biz_use_cache,
            config=biz_config or {},
        )
        biz.enrich_rows_with_biz(rows, biz_map)

    if extract_biz and biz_scope in ("frontend", "both") and frontend_dir:
        from . import legacy_biz_extractor as biz
        from .legacy_react_api_scanner import collect_handler_contexts
        # single_api_index 는 analyzer 의 single-mode 경로에서만 채워짐.
        # 사용자가 --frontend-dir 로 단일 프로젝트를 가리켰는데 analyzer 가
        # multi 경로로 처리된 경우라도 frontend_dir 하나면 재스캔해서 Phase B
        # 를 돌릴 수 있음. 진짜 multi-repo (frontends_root 여러 앱) 는
        # per-app 반복이 필요해 Phase B3 로 미룸 — 현재는 병합 api_index 에
        # 모든 URL 이 들어있어 fallback 동작.
        api_idx = dict(single_api_index) if single_api_index else {}
        if not api_idx:
            # Rebuild on the fly from the merged multi-repo api_by_frontend.
            for app_idx in (api_by_frontend or {}).values():
                for url, files in (app_idx or {}).items():
                    api_idx.setdefault(url, []).extend(files or [])
        if api_idx:
            handlers_by_url = collect_handler_contexts(
                frontend_dir, api_idx, patterns or {},
            )
            print(f"  frontend biz: api_idx={len(api_idx)} URLs, "
                  f"handlers_by_url={len(handlers_by_url)} URLs collected")
            if handlers_by_url:
                fe_biz_map = biz.extract_frontend_biz_logic(
                    handlers_by_url,
                    patterns or {},
                    max_handlers=biz_max_handlers,
                    use_cache=biz_use_cache,
                    config=biz_config or {},
                )
                biz.enrich_rows_with_frontend_biz(rows, fe_biz_map)
            else:
                print(f"  frontend biz: handler 컨텍스트 0건 — LLM skip. 가능 원인:")
                print(f"     a) <Button>label</Button> 같은 텍스트-children 패턴 없음")
                print(f"     b) onClick/onSubmit/onChange 가 아닌 다른 이벤트명")
                print(f"     c) handler 가 i18n key 또는 동적 함수 (변수 바인딩)")
                print(f"     d) API 호출 파일과 button JSX 가 분리되어 같은 파일 안에")
                print(f"        모두 있어야 매칭되는데 분산된 구조")
        else:
            print("  frontend biz: no API calls indexed — skip")

    # Phase II — endpoint narrative (Program Specification sheet).
    # Opt-in via ``extract_program_spec`` flag + depends on Phase A/B
    # having run (otherwise ``biz_summary`` / ``frontend_*`` fields are
    # empty and the LLM has nothing to summarize).
    endpoint_spec_map: dict = {}
    if extract_program_spec and extract_biz:
        from . import legacy_biz_extractor as biz
        endpoint_spec_map = biz.extract_endpoint_narrative(
            rows,
            patterns or {},
            use_cache=biz_use_cache,
            config=biz_config or {},
        )
        biz.enrich_rows_with_endpoint_spec(rows, endpoint_spec_map)
        print(f"  endpoint spec: {len(endpoint_spec_map)} endpoints narrated")

    resolved_method_scope = sum(
        1 for r in rows if r.get("resolved_via") == "method-scope"
    )
    resolved_class_scope = sum(
        1 for r in rows if r.get("resolved_via") == "class-scope-fallback"
    )
    # Stats are computed against the raw per-endpoint rows (pre-reorder)
    # so the menu placeholders we add for display don't inflate matched
    # counts.
    matched_count = sum(1 for r in rows if r.get("matched"))
    stats = {
        "backend_framework": framework,
        "frontend_framework": detected_frontend,
        "controllers": len(indexes["controllers_by_fqcn"]),
        "services": len(indexes["services_by_fqcn"]),
        "mappers": len(indexes["mappers_by_fqcn"]),
        "mapper_xml_files": mybatis_result.get("mapper_count", 0),
        "mapper_xml_namespaces": len(xml_namespaces),
        # `endpoints` counts every controller endpoint the analyzer
        # considered (rows + the lightweight skip-stubs in unmatched),
        # so a 29-backend batch with --menu-only still reports the true
        # endpoint total.
        "endpoints": len(rows) + skipped_no_menu,
        "matched": matched_count,
        "unmatched": (len(rows) - matched_count) + skipped_no_menu,
        "orphan_menus": len(orphan_menus),
        "with_react": sum(1 for r in rows if r["presentation_layer"]),
        "with_rfc": sum(1 for r in rows if r["rfc"]),
        "resolved_method_scope": resolved_method_scope,
        "resolved_class_scope": resolved_class_scope,
    }
    print(f"  Method-scope resolution: {resolved_method_scope}/{len(rows)} "
          f"endpoints (fallback: {resolved_class_scope})")
    _emit_chain_diag()

    # Reorder rows for display to follow menu.md source order + emit
    # menu-only placeholder for every menu entry without a matching
    # endpoint. Unmatched endpoints continue to live in `unmatched_controllers`.
    # In batch mode the caller aggregates matched rows across backends
    # and performs its own reorder, so per-backend reorder is skipped
    # to avoid duplicated placeholders.
    if skip_menu_reorder:
        # Batch will reorder globally. Pass rows through untouched so
        # the aggregator sees both matched and unmatched entries and
        # can decide based on the global menu_rows.
        display_rows = list(rows)
    else:
        display_rows = _reorder_rows_by_menu(rows, menu_rows, base_dirs)
    if row_per_trigger:
        display_rows = _split_rows_per_trigger(display_rows)

    return {
        "rows": display_rows,
        "unmatched_controllers": unmatched,
        "orphan_menus": orphan_menus,
        "stats": stats,
        "backend_framework": framework,
        "frontend_framework": detected_frontend,
        "backend_dir": backend_dir,
        "backend_project": backend_project,
        "frontend_dir": frontend_dir or "",
        "biz_map": biz_map,
        "fe_biz_map": fe_biz_map,
        "endpoint_spec_map": endpoint_spec_map,
    }


def _looks_like_backend(path: str) -> bool:
    """Heuristic: does ``path`` contain the markers of a backend project?

    Accepts any directory that has a build descriptor (``pom.xml`` /
    ``build.gradle`` / ``build.gradle.kts`` / ``settings.gradle``) or a
    standard Java source layout (``src/main/java``).
    """
    for marker in ("pom.xml", "build.gradle", "build.gradle.kts",
                   "settings.gradle", "settings.gradle.kts"):
        if os.path.isfile(os.path.join(path, marker)):
            return True
    if os.path.isdir(os.path.join(path, "src", "main", "java")):
        return True
    return False


def discover_backend_projects(backends_root: str,
                              include_all: bool = False) -> list[tuple]:
    """Return ``[(project_name, project_path), ...]`` for direct children
    of ``backends_root`` that look like backend projects.

    ``include_all=True`` skips the heuristic and returns every direct
    subdirectory regardless of structure.
    """
    if not backends_root or not os.path.isdir(backends_root):
        return []
    projects = []
    for entry in sorted(os.listdir(backends_root)):
        path = os.path.join(backends_root, entry)
        if not os.path.isdir(path):
            continue
        if not include_all and not _looks_like_backend(path):
            continue
        projects.append((entry, path))
    return projects


def analyze_legacy_batch(backends_root: str,
                        frontend_dir: str | None = None,
                        menu_rows: list[dict] | None = None,
                        rfc_depth: int = 3,
                        include_all: bool = False,
                        frontend_framework: str | None = None,
                        patterns: dict | None = None,
                        frontends_root: bool = False,
                        menu_only: bool = False,
                        extract_biz: bool = False,
                        biz_scope: str = "both",
                        biz_max_methods: int = 500,
                        biz_max_handlers: int = 300,
                        biz_use_cache: bool = True,
                        biz_config: dict | None = None,
                        library_dirs: list[str] | None = None,
                        terms_md: str | None = None,
                        extract_program_spec: bool = False,
                        emit_sequence_diagram: bool = False,
                        sequence_diagram_with_frontend: bool = False,
                        row_per_trigger: bool = False) -> dict:
    """Run :func:`analyze_legacy` against every backend project under
    ``backends_root`` and merge the resulting rows.

    Each output row carries ``backend_project`` and ``backend_framework``
    so that downstream reporters can filter / pivot by service.
    Per-project stats are kept in ``per_project_stats`` for the Summary
    sheet.
    """
    projects = discover_backend_projects(backends_root, include_all=include_all)
    print(f"  Backends root: {backends_root}")
    print(f"  Discovered backend projects: {len(projects)}")
    for name, path in projects:
        print(f"    - {name}  ({path})")

    # Pre-compute the frontend index ONCE up front. Previously each
    # analyze_legacy call re-scanned frontend_dir, which meant 29×
    # redundant work on a monorepo with 29 backends. Hoisting it here
    # also lets menu-driven narrowing apply to the whole batch.
    precomputed_frontend: dict | None = None
    if frontend_dir:
        # Batch 모드 frontend precompute 도 scoped cache 활용 — 같은
        # frontend tree 가 multi-bucket 스캐너 / api scanner / trigger
        # extractor 등 여러 단계가 다 같은 파일을 읽는다. 끝에서 즉시 해제.
        from .mybatis_parser import use_file_cache as _use_fc
        _use_fc(True)
        from .legacy_frontend import (
            build_frontend_url_map, build_frontend_url_map_multi,
            build_frontend_api_index,
        )
        url_section = (patterns or {}).get("url") or {}
        strip = url_section.get("url_prefix_strip") or []
        rp = url_section.get("react_route_prefix")
        app_key_spec = url_section.get("app_key")
        menu_apps: set[str] | None = None
        if app_key_spec:
            slugs = {_extract_app_key(r.get("url", ""), app_key_spec) for r in (menu_rows or [])}
            slugs = {s for s in slugs if s}
            menu_apps = slugs or None
        allowed = menu_apps if (menu_only and menu_apps) else None
        print(f"\n=== Scanning frontend (batch-wide, once) ===")
        if frontends_root:
            (react_map, det_fw, by_fe,
             api_fe, trig_fe) = build_frontend_url_map_multi(
                frontend_dir, framework=frontend_framework,
                strip_patterns=strip, route_prefix=rp,
                patterns=patterns, allowed_apps=allowed,
            )
            single_api, single_trig = {}, {}
        else:
            react_map, det_fw = build_frontend_url_map(
                frontend_dir, framework=frontend_framework,
                strip_patterns=strip, route_prefix=rp,
            )
            by_fe = {}
            api_fe, trig_fe = {}, {}
            single_api, single_trig = build_frontend_api_index(
                frontend_dir, patterns=patterns, strip_patterns=strip,
            )
        precomputed_frontend = {
            "react_url_map": react_map,
            "detected_frontend": det_fw,
            "by_frontend": by_fe,
            "api_by_frontend": api_fe,
            "triggers_by_frontend": trig_fe,
            "single_api_index": single_api,
            "single_triggers": single_trig,
        }
        total_api = sum(len(v) for v in api_fe.values()) if api_fe else len(single_api)
        total_trig = sum(len(v) for v in trig_fe.values()) if trig_fe else len(single_trig)
        print(f"  Frontend framework:      {det_fw}")
        print(f"  Frontend routes:         {len(react_map)}")
        print(f"  Frontend API calls:      {total_api} across {len(api_fe) or (1 if single_api else 0)} buckets")
        print(f"  Button triggers:         {total_trig}")
        # frontend precompute 끝났으니 cache 즉시 해제. 백엔드 루프는
        # frontend 파일을 다시 읽지 않으므로 메모리 회수 안전.
        _use_fc(False)

    all_rows = []
    all_unmatched = []
    all_biz_map: dict = {}
    all_fe_biz_map: dict = {}
    all_endpoint_spec_map: dict = {}
    all_orphans = []
    per_project_stats = {}
    project_frameworks = {}

    for name, path in projects:
        print(f"\n--- Analyzing {name} ---")
        result = analyze_legacy(
            backend_dir=path,
            frontend_dir=frontend_dir,
            menu_rows=menu_rows,
            rfc_depth=rfc_depth,
            frontend_framework=frontend_framework,
            patterns=patterns,
            frontends_root=frontends_root,
            menu_only=menu_only,
            skip_menu_reorder=True,  # batch does its own global reorder
            precomputed_frontend=precomputed_frontend,
            extract_biz=extract_biz,
            biz_scope=biz_scope,
            biz_max_methods=biz_max_methods,
            biz_max_handlers=biz_max_handlers,
            biz_use_cache=biz_use_cache,
            biz_config=biz_config,
            # Share library dirs across every sub-project in the batch,
            # so a common service repo is available to all backends for
            # chain resolution (FQCN, impls, mapper/XML lookup).
            library_dirs=library_dirs,
            # Same terms dictionary is applied to every sub-project — a
            # column-name → Korean translation is project-neutral so one
            # file serves the whole batch.
            terms_md=terms_md,
            # Opt-in endpoint narrative (Phase II). Each sub-project
            # extracts independently — keys are endpoint-level hashes so
            # no collisions across projects.
            extract_program_spec=extract_program_spec,
            # Mermaid sequence diagram (Phase A). Opt-in, parser-only,
            # LLM 불필요. 각 row 에 sequence_diagram 필드가 붙음.
            emit_sequence_diagram=emit_sequence_diagram,
            sequence_diagram_with_frontend=sequence_diagram_with_frontend,
            row_per_trigger=row_per_trigger,
        )
        # Make sure every row carries the project name even if downstream
        # consumers iterate the merged rows directly.
        for r in result.get("rows", []):
            r.setdefault("backend_project", name)
            r.setdefault("backend_framework", result.get("backend_framework", ""))
        for u in result.get("unmatched_controllers", []):
            u.setdefault("backend_project", name)
            u.setdefault("backend_framework", result.get("backend_framework", ""))
        all_rows.extend(result.get("rows", []))
        all_unmatched.extend(result.get("unmatched_controllers", []))
        all_orphans.extend(result.get("orphan_menus", []))
        per_project_stats[name] = result.get("stats", {})
        project_frameworks[name] = result.get("backend_framework", "")
        sub_biz = result.get("biz_map") or {}
        if sub_biz:
            all_biz_map.update(sub_biz)
        sub_fe = result.get("fe_biz_map") or {}
        if sub_fe:
            all_fe_biz_map.update(sub_fe)
        sub_spec = result.get("endpoint_spec_map") or {}
        if sub_spec:
            all_endpoint_spec_map.update(sub_spec)

    # Aggregate stats across projects
    def _sum(key):
        return sum(s.get(key, 0) or 0 for s in per_project_stats.values())

    # Frontend framework is per-run, not per-project (one frontend dir for batch)
    detected_frontend_fw = ""
    for s in per_project_stats.values():
        if s.get("frontend_framework"):
            detected_frontend_fw = s["frontend_framework"]
            break

    aggregated = {
        "projects": len(projects),
        "frontend_framework": detected_frontend_fw,
        "controllers": _sum("controllers"),
        "services": _sum("services"),
        "mappers": _sum("mappers"),
        "mapper_xml_files": _sum("mapper_xml_files"),
        "mapper_xml_namespaces": _sum("mapper_xml_namespaces"),
        "endpoints": len(all_rows),
        "matched": sum(1 for r in all_rows if r.get("matched")),
        "unmatched": sum(1 for r in all_rows if not r.get("matched")),
        "orphan_menus": len(all_orphans),
        "with_react": sum(1 for r in all_rows if r.get("presentation_layer")),
        "with_rfc": sum(1 for r in all_rows if r.get("rfc")),
        "resolved_method_scope": sum(
            1 for r in all_rows if r.get("resolved_via") == "method-scope"
        ),
        "resolved_class_scope": sum(
            1 for r in all_rows if r.get("resolved_via") == "class-scope-fallback"
        ),
    }

    # Global menu-order reorder across backends. Placeholders are
    # emitted once per un-matched menu entry (not once per backend).
    # Frontend-side base_dirs 를 batch 에도 전달해 orphan 메뉴의 React
    # 컬럼이 _menu_only_row 에서 채워지게 한다 (사용자 사례: frontend-URL
    # 전용 메뉴 가 backend endpoint 와 일치하지 않는 경우).
    batch_base_dirs = {
        "react_url_map": (precomputed_frontend or {}).get("react_url_map") or {},
        "url_strip": (patterns or {}).get("url", {}).get("url_prefix_strip") or [],
    }
    display_rows = _reorder_rows_by_menu(all_rows, menu_rows, batch_base_dirs)
    if row_per_trigger:
        display_rows = _split_rows_per_trigger(display_rows)

    return {
        "rows": display_rows,
        "unmatched_controllers": all_unmatched,
        "orphan_menus": all_orphans,
        "stats": aggregated,
        "per_project_stats": per_project_stats,
        "project_frameworks": project_frameworks,
        "backends_root": backends_root,
        "frontend_dir": frontend_dir or "",
        "is_batch": True,
        "biz_map": all_biz_map,
        "fe_biz_map": all_fe_biz_map,
        "endpoint_spec_map": all_endpoint_spec_map,
    }
