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
import re

from .legacy_java_parser import parse_all_java, resolve_type_fqcn
from .legacy_util import normalize_url
from .mybatis_parser import extract_table_usage, parse_all_mappers

logger = logging.getLogger(__name__)


SQL_KEYWORDS_GUARD = {"FROM", "JOIN", "WHERE", "SELECT"}


def _tables_for_statement(stmt: dict) -> set:
    """Extract tables referenced by a single statement using the shared util.

    Wraps ``extract_table_usage`` (which aggregates across statements) on a
    single-element list and pulls the keys back out. Works for every SQL
    dialect the bundled parser already supports.
    """
    usage = extract_table_usage([stmt])
    return set(usage.keys())


def _build_indexes(classes: list[dict]) -> dict:
    """Partition parsed Java classes into role-specific indexes.

    Returns a dict with:
      * controllers_by_fqcn
      * services_by_fqcn  (includes ``*ServiceImpl`` + ``@Service``/``@Component``)
      * mappers_by_fqcn   (``@Mapper``/``@Repository`` interfaces + ``*Mapper`` / ``*Dao``)
      * by_simple         — ``{SimpleName: [class, ...]}`` for name fallback
    """
    controllers = {}
    services = {}
    mappers = {}
    by_simple = {}

    for c in classes:
        fqcn = c["fqcn"]
        by_simple.setdefault(c["class_name"], []).append(c)
        stereo = c.get("stereotype", "")
        name = c["class_name"]

        # Spring @Controller/@RestController and Vert.x Verticles are both
        # treated as HTTP entry points. The parser already produced the
        # right endpoints list for each; here we just unify the index.
        if stereo in ("Controller", "RestController", "Verticle"):
            controllers[fqcn] = c
        if stereo in ("Service", "Component") or name.endswith("ServiceImpl") or name.endswith("Service"):
            services[fqcn] = c
        if stereo in ("Mapper", "Repository") or name.endswith("Mapper") or name.endswith("Dao"):
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


def _resolve_mapper_chain(service_fqcns: list[str], indexes: dict,
                          mybatis_idx: dict) -> tuple:
    """Walk service → mapper → namespace → (xml files, tables).

    Returns ``(query_xml_paths, related_tables, mapper_fqcns)`` as sorted
    lists. Namespace matching tries (1) full FQCN, (2) simple class name.
    """
    ns_to_xml = mybatis_idx["namespace_to_xml_files"]
    ns_to_tbl = mybatis_idx["namespace_to_tables"]

    xml_files = set()
    tables = set()
    mapper_fqcns = []
    seen_mappers = set()

    for svc in service_fqcns:
        for mfqcn in _find_mapper_fqcns(svc, indexes):
            if mfqcn in seen_mappers:
                continue
            seen_mappers.add(mfqcn)
            mapper_fqcns.append(mfqcn)

            # Primary: namespace == mapper FQCN (MyBatis convention)
            if mfqcn in ns_to_xml:
                xml_files.update(ns_to_xml[mfqcn])
            if mfqcn in ns_to_tbl:
                tables.update(ns_to_tbl[mfqcn])

            # Fallback: namespace ends with simple name
            simple = mfqcn.rsplit(".", 1)[-1]
            for ns in ns_to_xml:
                if ns == mfqcn:
                    continue
                if ns.endswith("." + simple) or ns == simple:
                    xml_files.update(ns_to_xml.get(ns, []))
                    tables.update(ns_to_tbl.get(ns, []))

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
    classes = parse_all_java(backend_dir)
    indexes = _build_indexes(classes)
    indexes["iface_to_impl"] = _resolve_service_impls(
        indexes["services_by_fqcn"], indexes["by_simple"]
    )
    print(f"  Classes parsed: {len(classes)} "
          f"(controllers={len(indexes['controllers_by_fqcn'])} "
          f"services={len(indexes['services_by_fqcn'])} "
          f"mappers={len(indexes['mappers_by_fqcn'])})")

    mybatis_result = parse_all_mappers(backend_dir)
    mybatis_idx = _build_mybatis_indexes(mybatis_result)

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
        "backend_dir": backend_dir,
        "frontend_dir": frontend_dir or "",
    }
