import logging

logger = logging.getLogger(__name__)


def generate_mermaid_erd(schema: dict, joins: list[dict],
                         llm_result: dict = None) -> str:
    """Generate Mermaid ERD code from schema and join analysis."""
    lines = ["erDiagram"]

    tables_in_erd = set()

    # Collect tables involved in relationships
    for j in joins:
        tables_in_erd.add(j["table1"])
        tables_in_erd.add(j["table2"])

    # Also include all schema tables
    schema_table_map = {}
    for t in schema.get("tables", []):
        schema_table_map[t["name"]] = t
        tables_in_erd.add(t["name"])

    # LLM descriptions
    descriptions = {}
    domain_groups = {}
    extra_relations = []
    if llm_result:
        descriptions = llm_result.get("descriptions", {})
        domain_groups = llm_result.get("domain_groups", {})
        extra_relations = llm_result.get("inferred_relations", [])

    # Write domain group comments
    if domain_groups:
        lines.append("")
        for domain, tables in sorted(domain_groups.items()):
            lines.append(f"    %% Domain: {domain}")
            for t in tables:
                lines.append(f"    %% - {t}")
        lines.append("")

    # Write table definitions
    for table_name in sorted(tables_in_erd):
        table_info = schema_table_map.get(table_name)
        if table_info:
            table_comment = table_info.get("comment", "")
            if table_comment:
                lines.append(f"    %% {table_name}: {table_comment}")
            lines.append(f"    {table_name} {{")
            pk_cols = set(table_info.get("primary_keys", []))
            for col in table_info.get("columns", []):
                col_name = col.get("column_name", "UNKNOWN")
                data_type = (col.get("data_type") or "VARCHAR2").split("(")[0]
                constraint = ""
                if col_name in pk_cols:
                    constraint = " PK"
                # Actual FK from schema constraints
                fk_cols = {fk["column"] for fk in table_info.get("foreign_keys", [])}
                if col_name in fk_cols:
                    if not constraint:
                        constraint = " FK"
                # JOIN reference (not actual FK) - mark in comment instead
                is_ref = False
                join_fk_cols = _get_fk_columns_from_joins(table_name, joins + extra_relations)
                if col_name in join_fk_cols and col_name not in fk_cols and not constraint:
                    is_ref = True

                # Description: schema comment 우선, 없으면 LLM description
                desc_key = f"{table_name}.{col_name}"
                desc = col.get("comment") or descriptions.get(desc_key, "")
                if is_ref:
                    desc = f"[REF] {desc}" if desc else "[REF]"
                comment = f' "{desc}"' if desc else ""

                lines.append(f"        {data_type} {col_name}{constraint}{comment}")
            lines.append("    }")
        # Skip tables not in schema (don't include UNKNOWN_SCHEMA)

    lines.append("")

    # Write relationships from JOINs (only if both tables are in schema)
    seen_rels = set()
    all_joins = joins + extra_relations
    for j in all_joins:
        t1, t2 = j["table1"], j["table2"]
        if t1 not in schema_table_map or t2 not in schema_table_map:
            continue
        key = tuple(sorted([t1, t2]))
        if key in seen_rels:
            continue
        seen_rels.add(key)

        cardinality = _infer_cardinality(j, schema_table_map)
        label = f"{j['column1']} = {j['column2']}"
        is_inferred = j in extra_relations
        if is_inferred:
            label += " [LLM inferred]"

        lines.append(f'    {t1} {cardinality} {t2} : "{label}"')

    return "\n".join(lines)


def _get_fk_columns_from_joins(table_name: str, joins: list[dict]) -> set:
    """Get columns that act as FK based on JOIN analysis."""
    fk_cols = set()
    for j in joins:
        if j["table1"] == table_name:
            fk_cols.add(j["column1"])
        elif j["table2"] == table_name:
            fk_cols.add(j["column2"])
    return fk_cols


def _infer_cardinality(join: dict, schema_table_map: dict) -> str:
    """Infer relationship cardinality from PK information."""
    t1 = join["table1"]
    t2 = join["table2"]
    c1 = join["column1"]
    c2 = join["column2"]

    t1_info = schema_table_map.get(t1, {})
    t2_info = schema_table_map.get(t2, {})

    t1_pks = set(t1_info.get("primary_keys", []))
    t2_pks = set(t2_info.get("primary_keys", []))

    c1_is_pk = c1 in t1_pks
    c2_is_pk = c2 in t2_pks

    # PK-to-PK: 1:1
    if c1_is_pk and c2_is_pk:
        return "||--||"
    # PK-to-FK: 1:N
    if c2_is_pk and not c1_is_pk:
        return "}o--||"
    if c1_is_pk and not c2_is_pk:
        return "||--o{"
    # Both non-PK: N:M (or unknown)
    return "}o--o{"


def build_erd_markdown(mermaid_code: str, schema: dict, joins: list[dict],
                       llm_result: dict = None) -> str:
    """Build a full Markdown document with the ERD."""
    lines = []
    lines.append("# Entity Relationship Diagram\n")

    owner = schema.get("owner", "UNKNOWN")
    table_count = len(schema.get("tables", []))
    rel_count = len(joins)
    extra_count = len(llm_result.get("inferred_relations", [])) if llm_result else 0

    lines.append(f"- Owner: {owner}")
    lines.append(f"- Tables: {table_count}")
    lines.append(f"- Relationships (from JOIN): {rel_count}")
    if extra_count:
        lines.append(f"- Relationships (LLM inferred): {extra_count}")
    lines.append("")

    # Domain groups
    if llm_result and llm_result.get("domain_groups"):
        lines.append("## Domain Groups\n")
        for domain, tables in sorted(llm_result["domain_groups"].items()):
            lines.append(f"### {domain}")
            for t in tables:
                desc = llm_result.get("table_descriptions", {}).get(t, "")
                suffix = f" - {desc}" if desc else ""
                lines.append(f"- {t}{suffix}")
            lines.append("")

    # Mermaid ERD
    lines.append("## ERD Diagram\n")
    lines.append("```mermaid")
    lines.append(mermaid_code)
    lines.append("```\n")

    # Relationship detail
    lines.append("## Relationship Details\n")
    lines.append("| Table A | Column | <-> | Table B | Column | Source |")
    lines.append("|---------|--------|-----|---------|--------|--------|")
    for j in joins:
        lines.append(f"| {j['table1']} | {j['column1']} | <-> | {j['table2']} | {j['column2']} | JOIN ({j.get('source_mapper', '')}) |")
    if llm_result:
        for j in llm_result.get("inferred_relations", []):
            lines.append(f"| {j['table1']} | {j['column1']} | <-> | {j['table2']} | {j['column2']} | LLM inferred |")
    lines.append("")

    return "\n".join(lines)
