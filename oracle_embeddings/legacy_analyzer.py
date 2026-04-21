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
from .mybatis_parser import _read_file_safe, extract_table_usage, parse_all_mappers

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
    """
    namespace_to_xml_files = {}
    namespace_to_tables = {}
    statement_to_tables = {}
    statement_to_xml_file = {}
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

    return {
        "namespace_to_xml_files": {k: sorted(v) for k, v in namespace_to_xml_files.items()},
        "namespace_to_tables": {k: sorted(v) for k, v in namespace_to_tables.items()},
        "statement_to_tables": {k: sorted(v) for k, v in statement_to_tables.items()},
        "statement_to_xml_file": statement_to_xml_file,
    }


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
                         app_slug: str) -> dict | None:
    """Return the first menu row whose URL maps to ``app_slug``.

    Two matching strategies, tried in order:

      1. Structured (preferred): when ``app_key_spec`` is configured,
         extract slug from each menu URL with :func:`_extract_app_key`
         and compare.
      2. Substring fallback: if no structured match (or spec missing),
         look for the ``app_slug`` string inside the raw menu URL
         (case-insensitive). This catches cases where the learned
         ``app_key.index`` is off by one (common LLM mistake) — instead
         of silently returning no match, we still get a best-effort
         attribution so 2-hop matching keeps producing useful rows.
    """
    if not menu_rows or not app_slug:
        return None
    app_slug_lower = app_slug.lower()
    if app_key_spec:
        for row in menu_rows:
            if _extract_app_key(row.get("url", ""), app_key_spec) == app_slug_lower:
                return row
        # Structured match failed — fall through to substring below.
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
            # Statement id not recognized — fall back to all tables
            # registered under the namespace. Record the raw call id so
            # operators know which SQL key the resolver could not find.
            sql_ids.add(f"{matched_ns}.{sql_id}" if sql_id else matched_ns)
            tables.update(ns_to_tbl.get(matched_ns, []))
            xml_files.update(ns_to_xml.get(matched_ns, []))

    for rfc in method.get("body_rfc_calls") or []:
        name = rfc.get("name")
        if name:
            rfcs.add(name)

    return xml_files, tables, rfcs, sql_ids


def _resolve_endpoint_chain(endpoint: dict, controller: dict,
                            indexes: dict, mybatis_idx: dict,
                            rfc_depth: int = 2) -> dict:
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
                    continue
                svc_fqcn = _resolve_field_type_fqcn(receiver, owner, indexes)
                if not svc_fqcn:
                    continue
                services.add(svc_fqcn)
                sm_key = (svc_fqcn, target_method_name)
                if target_method_name and sm_key not in seen_service_methods:
                    seen_service_methods.add(sm_key)
                    service_methods.append(f"{svc_fqcn}#{target_method_name}")
                # Walk into the interface's impl if we have one.
                # Search services, mappers (DAO/Repository), and
                # controllers so Nexcore chains (Svc→DAO) resolve.
                mapper_index = indexes["mappers_by_fqcn"]
                ctrl_index = indexes["controllers_by_fqcn"]
                impl_fqcn = iface_to_impl.get(svc_fqcn, svc_fqcn)
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
                        else:
                            tables.update(
                                mybatis_idx["namespace_to_tables"].get(matched_ns, [])
                            )
                            xml_files.update(
                                mybatis_idx["namespace_to_xml_files"].get(matched_ns, [])
                            )
                    for rfc in impl_cls.get("rfc_calls", []) or []:
                        if rfc.get("name"):
                            rfcs.add(rfc["name"])

        mapper_fqcns = sorted(
            fqcn for fqcn in services if fqcn in indexes["mappers_by_fqcn"]
        )
        return {
            "services": sorted(services),
            "service_methods": service_methods,
            "xml_files": sorted(xml_files),
            "tables": sorted(tables),
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
    return {
        "services": service_fqcns,
        "service_methods": [],
        "xml_files": xml_files_l,
        "tables": tables_l,
        "rfcs": rfc_names,
        "sql_ids": [],
        "mapper_fqcns": mapper_fqcns,
        "resolved_via": "class-scope-fallback",
    }


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

    Only the **menu-side** columns carry data. Frontend / backend /
    chain fields are empty so the Program Detail sheet shows every
    menu.md entry while flagging un-implemented ones as a gap.
    """
    return {
        "backend_project": "",
        "backend_framework": "",
        "main_menu": menu_entry.get("main_menu", ""),
        "sub_menu": menu_entry.get("sub_menu", ""),
        "tab": menu_entry.get("tab", ""),
        "menu_path": menu_entry.get("menu_path", ""),
        "menu_url": menu_entry.get("url", ""),
        "program_id": menu_entry.get("program_id", ""),
        "program_name": menu_entry.get("program_name", ""),
        "http_method": "",
        "url": "",
        "file_name": "",
        "frontend_project": "",
        "presentation_layer": "",
        "frontend_trigger": "",
        "controller_class": "",
        "service_class": "",
        "service_methods": "",
        "query_xml": "",
        "sql_ids": "",
        "related_tables": "",
        "rfc": "",
        "matched": False,
        "resolved_via": "",
    }


def _reorder_rows_by_menu(rows: list[dict], menu_rows: list[dict] | None,
                           base_dirs: dict) -> list[dict]:
    """Reorder ``rows`` to follow the **menu.md source order** and
    emit a menu-only placeholder for every menu entry that wasn't
    matched to any endpoint.

    The invariant: Program Detail has **one row per menu entry** at
    minimum (placeholder when no backend mapping). Menu entries that
    match multiple endpoints expand into multiple rows (one per
    endpoint) but those rows stay clustered in their menu's position.

    Only rows with ``matched=True`` are included — unmatched endpoints
    continue to be listed separately in Unmatched Controllers.
    """
    if not menu_rows:
        # No menu → return matched rows as-is (legacy-ish path).
        return [r for r in rows if r.get("matched")]

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
               rfc_depth: int = 2,
               menu_raw_url: str = "",
               react_entry: dict | None = None,
               frontend_trigger: str = "") -> dict:
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
        "http_method": endpoint["http_method"],
        "url": endpoint["full_url"],
        "file_name": _rel(controller["filepath"], backend_dir),
        "frontend_project": (react_entry or {}).get("frontend_name", ""),
        # react_file may be (a) a single absolute path from the Router
        # scanner → _rel it to frontend_dir, or (b) a "; "-joined list
        # already relative to frontends_root (from the 2-hop api
        # scanner). Only normalize the first case.
        "presentation_layer": (
            _rel(react_file, frontend_dir)
            if react_file and "; " not in react_file and os.path.isabs(react_file)
            else (react_file or "")
        ),
        "frontend_trigger": frontend_trigger,
        "controller_class": controller["fqcn"],
        "service_class": "; ".join(service_fqcns),
        "service_methods": "; ".join(service_methods),
        "query_xml": "; ".join(_rel(p, backend_dir) for p in xml_files),
        "sql_ids": "; ".join(sql_ids),
        "related_tables": ", ".join(tables),
        "rfc": ", ".join(rfc_names),
        "matched": menu_entry is not None,
        "resolved_via": chain.get("resolved_via", "method-scope"),
    }
    return row


def analyze_legacy(backend_dir: str, frontend_dir: str | None = None,
                   menu_rows: list[dict] | None = None,
                   rfc_depth: int = 2,
                   frontend_framework: str | None = None,
                   patterns: dict | None = None,
                   frontends_root: bool = False,
                   menu_only: bool = False,
                   precomputed_frontend: dict | None = None,
                   skip_menu_reorder: bool = False) -> dict:
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
                promoted = _lookup_menu_by_app(menu_rows or [], app_key_spec, two_hop_app)
                if promoted:
                    menu_entry = promoted
                    raw_menu_url = menu_entry.get("url", "")
                    app_slug = two_hop_app
                    app_slug_lower = two_hop_app

            # react_entry (for frontend_project metadata + direct-match
            # presentation_layer) — prefer the resolved app's bucket.
            react_entry = None
            if app_slug_lower and by_frontend:
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
            presentation = "; ".join(screen_files) if screen_files else react_file

            row = _build_row(
                ep, controller, indexes, mybatis_idx,
                menu_entry, presentation, base_dirs, rfc_depth=rfc_depth,
                menu_raw_url=raw_menu_url,
                react_entry=react_entry,
                frontend_trigger="; ".join(trigger_labels),
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

    # Reorder rows for display to follow menu.md source order + emit
    # menu-only placeholder for every menu entry without a matching
    # endpoint. Unmatched endpoints continue to live in `unmatched_controllers`.
    # In batch mode the caller aggregates matched rows across backends
    # and performs its own reorder, so per-backend reorder is skipped
    # to avoid duplicated placeholders.
    if skip_menu_reorder:
        display_rows = [r for r in rows if r.get("matched")]
    else:
        display_rows = _reorder_rows_by_menu(rows, menu_rows, base_dirs)

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
                        rfc_depth: int = 2,
                        include_all: bool = False,
                        frontend_framework: str | None = None,
                        patterns: dict | None = None,
                        frontends_root: bool = False,
                        menu_only: bool = False) -> dict:
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

    all_rows = []
    all_unmatched = []
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
    display_rows = _reorder_rows_by_menu(all_rows, menu_rows, {})

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
    }
