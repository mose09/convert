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
                   mybatis_namespaces: set | None = None) -> dict:
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

    controllers = {}
    services = {}
    mappers = {}
    by_simple = {}

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
        if stereo in ("Service", "Component") or name.endswith("ServiceImpl") or name.endswith("Service"):
            services[fqcn] = c
        if stereo in ("Mapper", "Repository") or name.endswith("Mapper") or name.endswith("Dao"):
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
    """Build namespace-keyed indexes used for the Mapper → XML/Tables hop.

    Returns:
      * namespace_to_xml_files — ``{namespace: sorted list[str]}``
      * namespace_to_tables    — ``{namespace: sorted list[str]}``
    """
    namespace_to_xml_files = {}
    namespace_to_tables = {}
    for stmt in mybatis_result.get("statements", []):
        ns = stmt.get("namespace") or ""
        if not ns:
            continue
        if "mapper_path" in stmt:
            namespace_to_xml_files.setdefault(ns, set()).add(stmt["mapper_path"])
        for tbl in _tables_for_statement(stmt):
            namespace_to_tables.setdefault(ns, set()).add(tbl)

    return {
        "namespace_to_xml_files": {k: sorted(v) for k, v in namespace_to_xml_files.items()},
        "namespace_to_tables": {k: sorted(v) for k, v in namespace_to_tables.items()},
    }


def _resolve_service_impls(services_by_fqcn: dict, by_simple: dict) -> dict:
    """For each Service interface, find its implementing class.

    Heuristic: if ``OrderService`` exists and ``OrderServiceImpl`` also exists
    in the same package (or anywhere with implements matching), map the
    interface FQCN to the Impl FQCN.
    """
    iface_to_impl = {}
    for fqcn, cls in services_by_fqcn.items():
        if cls["kind"] != "interface" and cls.get("implements"):
            # Class implements interface(s); map each interface back to this
            # class
            for iface_simple in cls["implements"]:
                simple = re.sub(r"<.*$", "", iface_simple).strip()
                for candidate in by_simple.get(simple, []):
                    iface_to_impl[candidate["fqcn"]] = fqcn
    # Name-based fallback: XxxService -> XxxServiceImpl
    for fqcn, cls in services_by_fqcn.items():
        impl_name = cls["class_name"] + "Impl"
        for candidate in by_simple.get(impl_name, []):
            iface_to_impl.setdefault(fqcn, candidate["fqcn"])
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


def _build_row(endpoint: dict, controller: dict, indexes: dict,
               mybatis_idx: dict, menu_entry: dict | None,
               react_file: str | None, base_dirs: dict,
               rfc_depth: int = 2) -> dict:
    """Assemble a single program-row dict for one controller endpoint."""
    service_fqcns = _find_service_fqcns(controller, indexes)
    xml_files, tables, mapper_fqcns = _resolve_mapper_chain(service_fqcns, indexes, mybatis_idx)
    rfc_names = _collect_rfc_transitive(
        service_fqcns, indexes, controller.get("rfc_calls", []), depth=rfc_depth
    )

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
        "main_menu": (menu_entry or {}).get("main_menu", ""),
        "sub_menu": (menu_entry or {}).get("sub_menu", ""),
        "tab": (menu_entry or {}).get("tab", ""),
        "program_id": (menu_entry or {}).get("program_id", ""),
        "program_name": (menu_entry or {}).get("program_name", "") or endpoint["method_name"],
        "http_method": endpoint["http_method"],
        "url": endpoint["full_url"],
        "file_name": _rel(controller["filepath"], backend_dir),
        "presentation_layer": _rel(react_file or "", frontend_dir),
        "controller_class": controller["fqcn"],
        "service_class": "; ".join(service_fqcns),
        "query_xml": "; ".join(_rel(p, backend_dir) for p in xml_files),
        "related_tables": ", ".join(tables),
        "rfc": ", ".join(rfc_names),
        "matched": menu_entry is not None,
    }
    return row


def analyze_legacy(backend_dir: str, frontend_dir: str | None = None,
                   menu_rows: list[dict] | None = None,
                   rfc_depth: int = 2) -> dict:
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

    classes = parse_all_java(backend_dir)

    # Apply framework gating to endpoint lists BEFORE counting, so that
    # the "endpoints discovered" diagnostic matches what the role index
    # sees. For ``mixed`` / ``unknown`` this is a no-op.
    _filter_endpoints_by_framework(classes, framework)

    # Diagnostic: stereotype + endpoint + SQL-call distribution so users
    # can self-diagnose why a project might parse to zero controllers /
    # mappers / mapper chains.
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
    print(f"  Classes parsed: {len(classes)}")
    print(f"  Stereotype distribution: {dist_str}")
    print(f"  Endpoints discovered in parser: {ep_total} "
          f"(in {classes_with_eps} classes)")
    print(f"  SQL helper calls: {sql_total} in {classes_with_sql} classes, "
          f"{len(sql_namespaces)} distinct namespaces")

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

    react_url_map = {}
    if frontend_dir:
        try:
            from .legacy_react_router import build_url_to_component_map
            print(f"  Frontend dir: {frontend_dir}")
            react_url_map = build_url_to_component_map(frontend_dir)
            print(f"  React routes indexed: {len(react_url_map)}")
        except Exception as e:
            logger.warning("React scan skipped: %s", e)

    # Menu URL index
    menu_url_index = {}
    for r in (menu_rows or []):
        key = normalize_url(r.get("url", ""))
        if key:
            menu_url_index[key] = r

    base_dirs = {
        "backend_dir": backend_dir,
        "frontend_dir": frontend_dir or "",
    }

    rows = []
    unmatched = []
    controller_urls = set()

    # Iterate every controller endpoint
    for controller in indexes["controllers_by_fqcn"].values():
        if controller.get("abstract"):
            continue
        class_paths = _inherit_class_paths(controller, indexes["controllers_by_fqcn"])
        # Expand class-path×method-path combinations (endpoints already have
        # full_url built from the current class_paths). For inherited case,
        # we re-extract with the parent's class path by prefixing.
        endpoints = controller.get("endpoints") or []
        for ep in endpoints:
            key = normalize_url(ep["full_url"])
            controller_urls.add(key)
            menu_entry = menu_url_index.get(key)
            react_file = (react_url_map.get(key) or {}).get("file_path", "")
            row = _build_row(
                ep, controller, indexes, mybatis_idx,
                menu_entry, react_file, base_dirs, rfc_depth=rfc_depth,
            )
            rows.append(row)
            if not row["matched"]:
                unmatched.append(row)

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

    stats = {
        "backend_framework": framework,
        "controllers": len(indexes["controllers_by_fqcn"]),
        "services": len(indexes["services_by_fqcn"]),
        "mappers": len(indexes["mappers_by_fqcn"]),
        "endpoints": len(rows),
        "matched": len(rows) - len(unmatched),
        "unmatched": len(unmatched),
        "orphan_menus": len(orphan_menus),
        "with_react": sum(1 for r in rows if r["presentation_layer"]),
        "with_rfc": sum(1 for r in rows if r["rfc"]),
    }

    return {
        "rows": rows,
        "unmatched_controllers": unmatched,
        "orphan_menus": orphan_menus,
        "stats": stats,
        "backend_framework": framework,
        "backend_dir": backend_dir,
        "frontend_dir": frontend_dir or "",
    }
