import logging
from collections import defaultdict

logger = logging.getLogger(__name__)

MAX_GROUP_SIZE = 30  # 그룹당 최대 테이블 수


def find_groups(schema: dict, joins: list[dict],
                max_group_size: int = MAX_GROUP_SIZE,
                query_tables: set = None) -> list[dict]:
    """Find table groups based on JOIN relationships using connected components.

    query_tables: XML 쿼리에서 사용된 전체 테이블 목록 (JOIN 여부 무관)
    """

    # Build adjacency list
    adj = defaultdict(set)
    for j in joins:
        adj[j["table1"]].add(j["table2"])
        adj[j["table2"]].add(j["table1"])

    all_tables = {t["name"] for t in schema.get("tables", [])}
    tables_in_joins = set(adj.keys())

    # Find connected components (BFS)
    visited = set()
    components = []

    for table in sorted(tables_in_joins):
        if table in visited:
            continue
        component = _bfs(table, adj, visited)
        components.append(component)

    # Sort components by size (largest first)
    components.sort(key=len, reverse=True)

    # Split large components
    final_groups = []
    for comp in components:
        if len(comp) <= max_group_size:
            final_groups.append(comp)
        else:
            sub_groups = _split_large_group(comp, adj, max_group_size)
            final_groups.extend(sub_groups)

    # Isolated tables: XML에서 사용됐지만 JOIN이 없는 테이블만 포함
    # XML에서 사용되지 않은 테이블은 그룹에서 제외
    if query_tables:
        isolated = (query_tables & all_tables) - tables_in_joins
    else:
        isolated = all_tables - tables_in_joins
    if isolated:
        # Split isolated into chunks
        isolated_list = sorted(isolated)
        for i in range(0, len(isolated_list), max_group_size):
            chunk = set(isolated_list[i:i + max_group_size])
            final_groups.append(chunk)

    # Build group info with schema data
    schema_map = {t["name"]: t for t in schema.get("tables", [])}
    groups = []

    for idx, group_tables in enumerate(final_groups):
        # Filter joins for this group
        group_joins = [
            j for j in joins
            if j["table1"] in group_tables and j["table2"] in group_tables
        ]

        # Filter schema for this group
        group_schema_tables = [
            schema_map[t] for t in sorted(group_tables) if t in schema_map
        ]

        # Generate group name from top tables (most connected)
        connection_count = defaultdict(int)
        for j in group_joins:
            connection_count[j["table1"]] += 1
            connection_count[j["table2"]] += 1

        if connection_count:
            top_tables = sorted(connection_count, key=connection_count.get, reverse=True)[:3]
        else:
            top_tables = sorted(group_tables)[:3]

        is_isolated = len(group_joins) == 0

        groups.append({
            "index": idx + 1,
            "tables": sorted(group_tables),
            "table_count": len(group_tables),
            "joins": group_joins,
            "join_count": len(group_joins),
            "top_tables": top_tables,
            "schema_tables": group_schema_tables,
            "is_isolated": is_isolated,
        })

    # Classify tables
    if query_tables is None:
        query_tables = set()

    classification = {
        "tables_with_joins": sorted(tables_in_joins & all_tables),
        "tables_in_xml_no_join": sorted((query_tables - tables_in_joins) & all_tables),
        "tables_not_in_xml": sorted(all_tables - query_tables - tables_in_joins),
        "tables_in_xml_not_in_schema": sorted(query_tables - all_tables),
    }

    logger.info("Found %d groups (%d with relationships, %d isolated)",
                len(groups),
                sum(1 for g in groups if not g["is_isolated"]),
                sum(1 for g in groups if g["is_isolated"]))
    logger.info("Classification: %d with JOINs, %d in XML no JOIN, %d not in XML, %d in XML not in schema",
                len(classification["tables_with_joins"]),
                len(classification["tables_in_xml_no_join"]),
                len(classification["tables_not_in_xml"]),
                len(classification["tables_in_xml_not_in_schema"]))

    return groups, classification


def _bfs(start: str, adj: dict, visited: set) -> set:
    """BFS to find connected component."""
    queue = [start]
    component = set()
    while queue:
        node = queue.pop(0)
        if node in visited:
            continue
        visited.add(node)
        component.add(node)
        for neighbor in adj.get(node, set()):
            if neighbor not in visited:
                queue.append(neighbor)
    return component


def _split_large_group(tables: set, adj: dict, max_size: int) -> list[set]:
    """Split a large connected component into smaller sub-groups.

    Strategy: pick the most connected node, BFS up to max_size,
    then repeat for remaining nodes.
    """
    remaining = set(tables)
    sub_groups = []

    while remaining:
        # Find most connected node in remaining
        best = max(remaining, key=lambda t: len(adj.get(t, set()) & remaining))

        # BFS from best, limited to max_size
        sub = set()
        queue = [best]
        while queue and len(sub) < max_size:
            node = queue.pop(0)
            if node not in remaining or node in sub:
                continue
            sub.add(node)
            # Prioritize neighbors that are in remaining
            neighbors = sorted(adj.get(node, set()) & remaining - sub)
            queue.extend(neighbors)

        remaining -= sub
        sub_groups.append(sub)

    return sub_groups


def build_summary_markdown(groups: list[dict], classification: dict = None) -> str:
    """Build a summary markdown showing all groups and table classification."""
    lines = []
    lines.append("# ERD Groups Summary\n")

    rel_groups = [g for g in groups if not g["is_isolated"]]
    iso_groups = [g for g in groups if g["is_isolated"]]

    lines.append(f"- Total groups: {len(groups)}")
    lines.append(f"- Groups with relationships: {len(rel_groups)}")
    lines.append(f"- Isolated table groups: {len(iso_groups)}")
    total_tables = sum(g["table_count"] for g in groups)
    total_joins = sum(g["join_count"] for g in groups)
    lines.append(f"- Total tables: {total_tables}")
    lines.append(f"- Total relationships: {total_joins}")
    lines.append("")

    # Table classification
    if classification:
        lines.append("## Table Classification\n")
        c = classification
        lines.append(f"| Category | Count |")
        lines.append(f"|----------|-------|")
        lines.append(f"| JOIN 관계가 있는 테이블 (ERD에 포함) | {len(c['tables_with_joins'])} |")
        lines.append(f"| XML에 존재하지만 JOIN 없음 | {len(c['tables_in_xml_no_join'])} |")
        lines.append(f"| XML에 존재하지 않는 테이블 | {len(c['tables_not_in_xml'])} |")
        lines.append(f"| XML에 있지만 스키마에 없는 테이블 | {len(c['tables_in_xml_not_in_schema'])} |")
        lines.append("")

    # Groups with relationships
    if rel_groups:
        lines.append("## ERD Groups (with Relationships)\n")
        lines.append("| Group | Tables | Relationships | Key Tables |")
        lines.append("|-------|--------|---------------|------------|")
        for g in rel_groups:
            top = ", ".join(g["top_tables"])
            lines.append(f"| {g['index']:02d} | {g['table_count']} | {g['join_count']} | {top} |")
        lines.append("")

    # Tables in XML but no JOIN
    if classification and classification["tables_in_xml_no_join"]:
        tables = classification["tables_in_xml_no_join"]
        lines.append(f"## XML에 존재하지만 JOIN 없는 테이블 ({len(tables)}개)\n")
        lines.append("쿼리에서 단독 SELECT/INSERT/UPDATE/DELETE로 사용되지만 다른 테이블과 JOIN이 없는 테이블입니다.\n")
        for t in tables:
            lines.append(f"- {t}")
        lines.append("")

    # Tables not in XML
    if classification and classification["tables_not_in_xml"]:
        tables = classification["tables_not_in_xml"]
        lines.append(f"## XML에 존재하지 않는 테이블 ({len(tables)}개)\n")
        lines.append("스키마에는 있지만 MyBatis XML 쿼리에서 사용되지 않는 테이블입니다.\n")
        for t in tables:
            lines.append(f"- {t}")
        lines.append("")

    # Tables in XML but not in schema
    if classification and classification["tables_in_xml_not_in_schema"]:
        tables = classification["tables_in_xml_not_in_schema"]
        lines.append(f"## XML에 있지만 스키마에 없는 테이블 ({len(tables)}개)\n")
        lines.append("쿼리에서 참조하지만 스키마 .md에 정의가 없는 테이블입니다. (다른 스키마 소유이거나 뷰일 수 있음)\n")
        for t in tables:
            lines.append(f"- {t}")
        lines.append("")

    return "\n".join(lines)
