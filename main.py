import argparse
import logging
import os
import re

import yaml
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def load_config(config_path: str) -> dict:
    """Load YAML config and resolve environment variable references."""
    with open(config_path, "r", encoding="utf-8") as f:
        content = f.read()

    def replace_env(match):
        var_name = match.group(1)
        return os.environ.get(var_name, match.group(0))

    content = re.sub(r"\$\{(\w+)\}", replace_env, content)
    return yaml.safe_load(content)


def cmd_schema(args):
    """Extract Oracle schema metadata to Markdown."""
    from oracle_embeddings.db import get_connection
    from oracle_embeddings.extractor import extract_schema
    from oracle_embeddings.storage import save_schema_markdown, save_schema_txt

    load_dotenv()
    config = load_config(args.config)

    owner = args.owner or config.get("oracle", {}).get("schema_owner", os.environ.get("ORACLE_USER", ""))
    table_names = [args.table] if args.table else config.get("tables")
    file_format = args.format or config.get("storage", {}).get("file_format", "markdown")
    output_dir = config.get("storage", {}).get("output_dir", "./output")

    connection = get_connection(config)
    try:
        schema = extract_schema(connection, owner, table_names)

        if file_format == "markdown":
            filepath = save_schema_markdown(schema, output_dir)
        else:
            filepath = save_schema_txt(schema, output_dir)

        print(f"Schema exported: {filepath}")
        print(f"Tables: {len(schema['tables'])}")
        total_cols = sum(len(t['columns']) for t in schema['tables'])
        total_fks = sum(len(t['foreign_keys']) for t in schema['tables'])
        print(f"Columns: {total_cols}, Foreign Keys: {total_fks}")
    finally:
        connection.close()


def cmd_query(args):
    """Analyze MyBatis/iBatis mapper XML files and extract relationships."""
    from oracle_embeddings.mybatis_parser import parse_all_mappers
    from oracle_embeddings.storage import save_query_markdown

    config = load_config(args.config) if os.path.exists(args.config) else {}
    output_dir = config.get("storage", {}).get("output_dir", "./output")

    mybatis_dir = args.mybatis_dir
    if not os.path.isdir(mybatis_dir):
        print(f"Error: Directory not found: {mybatis_dir}")
        return

    # Load schema table names for filtering (optional)
    valid_tables = None
    if args.schema_md:
        from oracle_embeddings.md_parser import parse_schema_md
        schema = parse_schema_md(args.schema_md)
        valid_tables = {t["name"] for t in schema["tables"]}
        print(f"Schema filter: {len(valid_tables)} tables loaded")

    analysis = parse_all_mappers(mybatis_dir)

    # Filter joins/usage to only include tables in schema
    if valid_tables:
        before_joins = len(analysis["joins"])
        analysis["joins"] = [
            j for j in analysis["joins"]
            if j["table1"] in valid_tables and j["table2"] in valid_tables
        ]
        before_usage = len(analysis["table_usage"])
        analysis["table_usage"] = {
            k: v for k, v in analysis["table_usage"].items()
            if k in valid_tables
        }
        print(f"  Filtered joins: {before_joins} → {len(analysis['joins'])}")
        print(f"  Filtered tables: {before_usage} → {len(analysis['table_usage'])}")

    filepath = save_query_markdown(analysis, output_dir)

    print(f"Query analysis exported: {filepath}")
    print(f"Mappers: {analysis['mapper_count']}")
    print(f"SQL statements: {analysis['statement_count']}")
    print(f"Inferred relationships: {len(analysis['joins'])}")


def cmd_erd(args):
    """Generate Mermaid ERD from schema + query analysis."""
    from oracle_embeddings.db import get_connection
    from oracle_embeddings.extractor import extract_schema
    from oracle_embeddings.mybatis_parser import parse_all_mappers
    from oracle_embeddings.erd_generator import generate_mermaid_erd, build_erd_markdown

    load_dotenv()
    config = load_config(args.config)

    output_dir = config.get("storage", {}).get("output_dir", "./output")
    owner = args.owner or config.get("oracle", {}).get("schema_owner", os.environ.get("ORACLE_USER", ""))
    table_names = [args.table] if args.table else config.get("tables")

    # 1. Schema extraction
    print("=== Step 1: Schema Extraction ===")
    connection = get_connection(config)
    try:
        schema = extract_schema(connection, owner, table_names)
        print(f"Tables: {len(schema['tables'])}")
    finally:
        connection.close()

    # 2. Query analysis
    joins = []
    if args.mybatis_dir:
        if not os.path.isdir(args.mybatis_dir):
            print(f"Error: Directory not found: {args.mybatis_dir}")
            return
        print("\n=== Step 2: Query Analysis ===")
        analysis = parse_all_mappers(args.mybatis_dir)
        joins = analysis["joins"]
        print(f"Inferred relationships: {len(joins)}")
    else:
        print("\n=== Step 2: Query Analysis (skipped, no --mybatis-dir) ===")

    # 3. LLM assist (optional)
    llm_result = None
    if args.llm_assist:
        print("\n=== Step 3: LLM Assist ===")
        from oracle_embeddings.llm_assist import assist_erd
        llm_result = assist_erd(schema, joins, config)
        extra = len(llm_result.get("inferred_relations", []))
        groups = len(llm_result.get("domain_groups", {}))
        print(f"LLM inferred relations: {extra}, Domain groups: {groups}")
    else:
        print("\n=== Step 3: LLM Assist (skipped, use --llm-assist to enable) ===")

    # 4. Generate ERD
    print("\n=== Step 4: Generate ERD ===")
    mermaid_code = generate_mermaid_erd(schema, joins, llm_result)
    erd_md = build_erd_markdown(mermaid_code, schema, joins, llm_result)

    os.makedirs(output_dir, exist_ok=True)
    from datetime import datetime
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = os.path.join(output_dir, f"erd_{owner}_{timestamp}.md")
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(erd_md)

    print(f"ERD exported: {filepath}")
    total_rels = len(joins) + (len(llm_result.get("inferred_relations", [])) if llm_result else 0)
    print(f"Total relationships: {total_rels}")


def cmd_embed(args):
    """Embed .md files into ChromaDB vector store."""
    from oracle_embeddings.vector_store import embed_schema_md, embed_query_md

    load_dotenv()
    config = load_config(args.config)
    db_path = config.get("vectordb", {}).get("db_path", "./vectordb")

    if args.schema_md:
        print(f"=== Embedding Schema: {args.schema_md} ===")
        count = embed_schema_md(args.schema_md, config, db_path)
        print(f"Schema chunks embedded: {count}")

    if args.query_md:
        print(f"=== Embedding Query Analysis: {args.query_md} ===")
        count = embed_query_md(args.query_md, config, db_path)
        print(f"Query chunks embedded: {count}")

    if not args.schema_md and not args.query_md:
        print("Error: --schema-md 또는 --query-md 중 하나 이상 지정하세요.")
        return

    print(f"\nVector DB saved: {db_path}")


def cmd_erd_rag(args):
    """Generate Mermaid ERD using RAG (vector search + LLM)."""
    from oracle_embeddings.rag_erd import generate_erd_with_rag

    load_dotenv()
    config = load_config(args.config)
    db_path = config.get("vectordb", {}).get("db_path", "./vectordb")
    output_dir = config.get("storage", {}).get("output_dir", "./output")

    # Validate vector DB exists
    if not os.path.isdir(db_path):
        print(f"Error: Vector DB not found at '{os.path.abspath(db_path)}'")
        print("먼저 'python main.py embed' 를 실행하세요.")
        return

    target_tables = None
    if args.tables:
        target_tables = [t.strip().upper() for t in args.tables.split(",")]

    print("=== RAG-based ERD Generation ===")
    print(f"Vector DB: {os.path.abspath(db_path)}")
    print(f"Output dir: {os.path.abspath(output_dir)}")
    if target_tables:
        print(f"Target tables: {', '.join(target_tables)}")

    try:
        filepath = generate_erd_with_rag(config, db_path, output_dir, target_tables)
        if filepath is None:
            print("\nERD generation aborted (no context).")
        elif os.path.exists(filepath):
            size = os.path.getsize(filepath)
            print(f"\nERD exported: {os.path.abspath(filepath)} ({size} bytes)")
        else:
            print(f"\nError: File was not created at {os.path.abspath(filepath)}")
    except Exception as e:
        logger.error("ERD generation failed: %s", e, exc_info=True)
        print(f"\nError: {e}")


def cmd_standardize(args):
    """Generate standardization analysis report."""
    from oracle_embeddings.md_parser import parse_schema_md, parse_query_md
    from oracle_embeddings.std_analyzer import analyze_all
    from oracle_embeddings.std_report import generate_report

    load_dotenv()
    config = load_config(args.config)
    output_dir = config.get("storage", {}).get("output_dir", "./output")

    if not args.schema_md:
        print("Error: --schema-md 는 필수입니다.")
        return

    # 1. Parse
    print("=== Step 1: Parsing ===")
    schema = parse_schema_md(args.schema_md)
    print(f"  Tables: {len(schema['tables'])}")

    joins = []
    if args.query_md:
        joins = parse_query_md(args.query_md)
        print(f"  JOIN relationships: {len(joins)}")

    # 2. Structure analysis
    print("\n=== Step 2: Structure Analysis ===")
    analysis = analyze_all(schema, joins)
    print(f"  JOIN column mismatches: {len(analysis['join_column_mismatch'])}")
    print(f"  Type inconsistencies: {len(analysis['type_inconsistency'])}")
    print(f"  Naming violations: {len(analysis['naming_pattern'].get('violations', []))}")
    print(f"  Code columns: {len(analysis['code_columns'])}")
    print(f"  Y/N columns: {len(analysis['yn_columns'])}")

    # 3. Data validation (optional, requires Oracle)
    data_validation = {}
    if args.validate_data:
        print("\n=== Step 3: Data Validation (Oracle) ===")
        from oracle_embeddings.db import get_connection
        from oracle_embeddings.std_data_validator import (
            validate_code_columns, validate_yn_columns, validate_column_usage
        )

        connection = get_connection(config)
        try:
            print("  Validating code columns...")
            data_validation["code_validation"] = validate_code_columns(
                connection, analysis["code_columns"]
            )

            print("  Validating Y/N columns...")
            data_validation["yn_validation"] = validate_yn_columns(
                connection, analysis["yn_columns"]
            )

            if not args.skip_usage:
                print("  Validating column usage (may take time)...")
                # Only validate tables that are in XML queries
                query_tables = None
                if args.query_md:
                    from oracle_embeddings.md_parser import parse_query_tables
                    query_tables = list(parse_query_tables(args.query_md))
                data_validation["column_usage"] = validate_column_usage(
                    connection, schema, query_tables
                )
        finally:
            connection.close()
    else:
        print("\n=== Step 3: Data Validation (skipped, use --validate-data) ===")

    # 4. Generate report
    print("\n=== Step 4: Generating Report ===")
    report_dir = generate_report(analysis, data_validation, config, output_dir)

    print(f"\nReport generated: {os.path.abspath(report_dir)}")


def cmd_terms(args):
    """Generate terminology dictionary from schema and/or React source."""
    from oracle_embeddings.terms_collector import collect_from_schema, collect_from_react, merge_words
    from oracle_embeddings.terms_report import save_terms_markdown, save_terms_excel

    load_dotenv()
    config = load_config(args.config)
    output_dir = config.get("storage", {}).get("output_dir", "./output")

    if not args.schema_md and not args.react_dir:
        print("Error: --schema-md 또는 --react-dir 중 하나 이상 지정하세요.")
        return

    # 1. Collect words
    print("=== Step 1: Collecting Words ===")
    schema_words = {}
    react_words = {}

    if args.schema_md:
        print(f"  Schema: {args.schema_md}")
        schema_words = collect_from_schema(args.schema_md)
        print(f"  Schema words: {len(schema_words)}")

    if args.react_dir:
        if not os.path.isdir(args.react_dir):
            print(f"  Error: Directory not found: {args.react_dir}")
            return
        print(f"  React: {args.react_dir}")
        react_words = collect_from_react(args.react_dir)
        print(f"  React words: {len(react_words)}")

    # 2. Merge
    print("\n=== Step 2: Merging ===")
    merged = merge_words(schema_words, react_words)
    print(f"  Total unique words: {len(merged)}")

    both_count = sum(1 for w in merged if w["db_count"] > 0 and w["fe_count"] > 0)
    print(f"  DB+FE 공통: {both_count}")

    # 3. LLM enrichment
    if not args.skip_llm:
        print("\n=== Step 3: LLM Enrichment ===")
        from oracle_embeddings.terms_llm import enrich_terms
        merged = enrich_terms(merged, config)
    else:
        print("\n=== Step 3: LLM Enrichment (skipped) ===")

    # 4. Save
    print("\n=== Step 4: Saving ===")
    md_path = save_terms_markdown(merged, output_dir)
    xlsx_path = save_terms_excel(merged, output_dir)

    print(f"\n  Markdown: {os.path.abspath(md_path)}")
    print(f"  Excel:    {os.path.abspath(xlsx_path)}")

    enriched_count = sum(1 for w in merged if w.get("korean"))
    print(f"\n  Total: {len(merged)} words, Enriched: {enriched_count}")


def cmd_enrich_schema(args):
    """Enrich schema .md with LLM-generated comments for empty descriptions."""
    from oracle_embeddings.md_parser import parse_schema_md
    from oracle_embeddings.schema_enricher import enrich_schema, save_enriched_schema_md

    load_dotenv()
    config = load_config(args.config)
    output_dir = config.get("storage", {}).get("output_dir", "./output")

    if not args.schema_md:
        print("Error: --schema-md 는 필수입니다.")
        return

    # 1. Parse existing schema
    print(f"=== Step 1: Parsing Schema ===")
    schema = parse_schema_md(args.schema_md)
    total_tables = len(schema["tables"])
    total_cols = sum(len(t["columns"]) for t in schema["tables"])
    empty_table_comments = sum(1 for t in schema["tables"] if not t.get("comment"))
    empty_col_comments = sum(
        1 for t in schema["tables"] for c in t["columns"] if not c.get("comment")
    )
    print(f"  Tables: {total_tables}, Columns: {total_cols}")
    print(f"  Empty table comments: {empty_table_comments}")
    print(f"  Empty column comments: {empty_col_comments}")

    if empty_table_comments == 0 and empty_col_comments == 0:
        print("\nAll comments are already filled. Nothing to enrich.")
        return

    # 2. Enrich with LLM
    print(f"\n=== Step 2: LLM Enrichment ===")
    enriched_schema = enrich_schema(schema, config)

    # 3. Save enriched schema
    print(f"\n=== Step 3: Saving Enriched Schema ===")
    filepath = save_enriched_schema_md(enriched_schema, output_dir)
    print(f"  Enriched schema saved: {os.path.abspath(filepath)}")

    # Stats
    new_empty_table = sum(1 for t in enriched_schema["tables"] if not t.get("comment"))
    new_empty_col = sum(
        1 for t in enriched_schema["tables"] for c in t["columns"] if not c.get("comment")
    )
    print(f"\n  Table comments: {empty_table_comments} empty → {new_empty_table} empty")
    print(f"  Column comments: {empty_col_comments} empty → {new_empty_col} empty")


def cmd_erd_group(args):
    """Generate ERD files grouped by relationship clusters."""
    from oracle_embeddings.md_parser import parse_schema_md, parse_query_md, parse_query_tables, parse_table_usage
    from oracle_embeddings.graph_cluster import find_groups, build_summary_markdown, build_summary_excel
    from oracle_embeddings.erd_generator import generate_mermaid_erd, build_erd_markdown

    config = load_config(args.config) if os.path.exists(args.config) else {}
    output_dir = config.get("storage", {}).get("output_dir", "./output")
    max_size = args.max_size or 30

    if not args.schema_md:
        print("Error: --schema-md 는 필수입니다.")
        return

    # 1. Parse
    print(f"=== Step 1: Parsing ===")
    schema = parse_schema_md(args.schema_md)
    print(f"  Tables: {len(schema['tables'])}")

    joins = []
    query_tables = set()
    table_usage = None
    if args.query_md:
        joins = parse_query_md(args.query_md)
        query_tables = parse_query_tables(args.query_md)
        table_usage = parse_table_usage(args.query_md)
        print(f"  JOIN relationships: {len(joins)}")
        print(f"  Tables in XML: {len(query_tables)}")
        if table_usage:
            print(f"  Table usage data: {len(table_usage)} tables")

    # Parse common tables
    common_tables_manual = None

    # Priority: file > manual > auto-detect
    if args.common_tables_file:
        if os.path.exists(args.common_tables_file):
            with open(args.common_tables_file, "r", encoding="utf-8") as f:
                common_tables_manual = set()
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        common_tables_manual.add(line.upper())
            print(f"  Common tables from file: {len(common_tables_manual)} tables")
        else:
            print(f"  Warning: {args.common_tables_file} not found, using auto-detect")
    elif args.common_tables:
        common_tables_manual = {t.strip().upper() for t in args.common_tables.split(",")}
        print(f"  Manual common tables: {len(common_tables_manual)}")

    common_threshold = args.common_threshold

    # 2. Find groups
    print(f"\n=== Step 2: Clustering (max {max_size} tables/group) ===")
    groups, classification = find_groups(
        schema, joins, max_size, query_tables,
        common_threshold=common_threshold,
        common_tables_manual=common_tables_manual,
        table_usage=table_usage,
    )
    rel_groups = [g for g in groups if not g["is_isolated"]]
    iso_groups = [g for g in groups if g["is_isolated"]]
    print(f"  Common tables: {len(classification.get('common_tables', []))}")
    print(f"  Groups with relationships: {len(rel_groups)}")
    print(f"  Isolated table groups: {len(iso_groups)}")
    print(f"  JOIN 관계 테이블: {len(classification['tables_with_joins'])}")
    print(f"  XML에 있지만 JOIN 없음: {len(classification['tables_in_xml_no_join'])}")
    print(f"  XML에 없는 테이블: {len(classification['tables_not_in_xml'])}")
    print(f"  XML에만 있고 스키마에 없음: {len(classification['tables_in_xml_not_in_schema'])}")

    # Export common tables file
    if args.export_common:
        common_list = classification.get("common_tables", [])
        common_file_path = os.path.join(output_dir, "common_tables.txt")
        with open(common_file_path, "w", encoding="utf-8") as f:
            f.write("# 공통 테이블 목록 (자동 감지)\n")
            f.write("# 판단 기준: JOIN으로만 사용되는 비율이 80% 이상인 테이블\n")
            f.write("# 잘못 분류된 테이블은 삭제하고, 빠진 테이블은 추가하세요.\n")
            f.write("# '#'으로 시작하는 줄은 무시됩니다.\n")
            f.write("#\n")
            f.write("# 테이블명 | 주테이블 횟수 | JOIN 횟수 | JOIN 비율\n")
            for t in common_list:
                u = (table_usage or {}).get(t, {})
                main_c = u.get("as_main_count", 0)
                join_c = u.get("as_join_count", 0)
                total = main_c + join_c
                ratio = f"{join_c/total*100:.0f}%" if total > 0 else "-"
                f.write(f"{t}  # main:{main_c} join:{join_c} ratio:{ratio}\n")
        print(f"\n  Common tables exported: {os.path.abspath(common_file_path)} ({len(common_list)} tables)")
        print(f"  → 파일을 편집한 후 --common-tables-file 옵션으로 재실행하세요.")

    # 3. Generate ERD per group
    print(f"\n=== Step 3: Generating ERD files ===")
    from datetime import datetime
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    erd_dir = os.path.join(output_dir, f"erd_groups_{timestamp}")
    os.makedirs(erd_dir, exist_ok=True)

    generated = 0
    for g in groups:
        if g["is_isolated"]:
            continue

        group_schema = {
            "owner": schema.get("owner", "UNKNOWN"),
            "tables": g["schema_tables"],
        }

        # Skip groups where no tables exist in schema
        if not group_schema["tables"]:
            continue

        mermaid_code = generate_mermaid_erd(group_schema, g["joins"])

        # Skip if mermaid code is essentially empty (only header, no tables)
        if mermaid_code.strip() == "erDiagram" or mermaid_code.count("{") == 0:
            continue

        erd_md = build_erd_markdown(mermaid_code, group_schema, g["joins"])

        top_names = "_".join(g["top_tables"][:3])
        md_filename = f"erd_group_{g['index']:02d}_{top_names}.md"
        md_filepath = os.path.join(erd_dir, md_filename)
        with open(md_filepath, "w", encoding="utf-8") as f:
            f.write(erd_md)

        # HTML ERD
        from oracle_embeddings.erd_html import generate_html_erd
        html_filename = f"erd_group_{g['index']:02d}_{top_names}.html"
        html_filepath = os.path.join(erd_dir, html_filename)
        generate_html_erd(group_schema, g["joins"], html_filepath)

        generated += 1
        print(f"  [{g['index']:02d}] {md_filename} + .html ({g['table_count']} tables, {g['join_count']} rels)")

    # 4. Summary files (markdown + excel)
    summary = build_summary_markdown(groups, classification)
    summary_md_path = os.path.join(erd_dir, "00_summary.md")
    with open(summary_md_path, "w", encoding="utf-8") as f:
        f.write(summary)

    excel_path = os.path.join(erd_dir, "00_summary.xlsx")
    build_summary_excel(groups, classification, schema, excel_path)

    print(f"\n  Summary: {summary_md_path}")
    print(f"  Excel:   {excel_path}")
    print(f"\nERD files exported to: {os.path.abspath(erd_dir)}")
    print(f"Total: {generated} ERD files + summary (.md + .xlsx)")


def cmd_erd_md(args):
    """Generate Mermaid ERD from existing .md files (no DB, no LLM)."""
    from oracle_embeddings.md_parser import parse_schema_md, parse_query_md
    from oracle_embeddings.erd_generator import generate_mermaid_erd, build_erd_markdown

    config = load_config(args.config) if os.path.exists(args.config) else {}
    output_dir = config.get("storage", {}).get("output_dir", "./output")

    if not args.schema_md:
        print("Error: --schema-md 는 필수입니다.")
        return

    # 1. Parse schema .md
    print(f"=== Step 1: Parsing Schema: {args.schema_md} ===")
    schema = parse_schema_md(args.schema_md)
    print(f"  Tables: {len(schema['tables'])}")
    total_cols = sum(len(t['columns']) for t in schema['tables'])
    print(f"  Columns: {total_cols}")

    # 2. Parse query .md (optional)
    joins = []
    if args.query_md:
        print(f"\n=== Step 2: Parsing Query Analysis: {args.query_md} ===")
        joins = parse_query_md(args.query_md)
        print(f"  JOIN relationships: {len(joins)}")
    else:
        print("\n=== Step 2: Query Analysis (skipped, no --query-md) ===")

    # 3. Filter tables if specified
    if args.tables:
        target_tables = {t.strip().upper() for t in args.tables.split(",")}
        # Include related tables from joins
        related = set()
        for j in joins:
            if j["table1"] in target_tables or j["table2"] in target_tables:
                related.add(j["table1"])
                related.add(j["table2"])
        target_tables.update(related)

        schema["tables"] = [t for t in schema["tables"] if t["name"] in target_tables]
        joins = [j for j in joins if j["table1"] in target_tables or j["table2"] in target_tables]
        print(f"\n  Filtered to {len(schema['tables'])} tables (+ related)")

    # 4. Filter: only tables with relationships (optional)
    if args.related_only and not args.tables:
        tables_with_rels = set()
        for j in joins:
            tables_with_rels.add(j["table1"])
            tables_with_rels.add(j["table2"])
        schema["tables"] = [t for t in schema["tables"] if t["name"] in tables_with_rels]
        print(f"\n  Filtered to {len(schema['tables'])} tables (with relationships only)")

    # 5. Generate ERD
    print(f"\n=== Step 3: Generating Mermaid ERD ===")
    mermaid_code = generate_mermaid_erd(schema, joins)
    erd_md = build_erd_markdown(mermaid_code, schema, joins)

    os.makedirs(output_dir, exist_ok=True)
    from datetime import datetime
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    owner = schema.get("owner", "UNKNOWN")
    filepath = os.path.join(output_dir, f"erd_{owner}_{timestamp}.md")

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(erd_md)

    # HTML ERD
    from oracle_embeddings.erd_html import generate_html_erd
    html_path = os.path.join(output_dir, f"erd_{owner}_{timestamp}.html")
    generate_html_erd(schema, joins, html_path)

    print(f"\nERD exported:")
    print(f"  Mermaid: {os.path.abspath(filepath)}")
    print(f"  HTML:    {os.path.abspath(html_path)}")
    print(f"Tables: {len(schema['tables'])}, Relationships: {len(joins)}")


def cmd_all(args):
    """Run schema, query, and erd generation."""
    print("=== Schema Extraction ===")
    cmd_schema(args)
    print()
    print("=== Query Analysis ===")
    cmd_query(args)
    print()
    print("=== ERD Generation ===")
    cmd_erd(args)


def main():
    parser = argparse.ArgumentParser(
        description="Oracle Schema & Query Analyzer for Msty Knowledge Base"
    )
    parser.add_argument("--config", default="config.yaml", help="Path to config file")

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # schema command
    schema_parser = subparsers.add_parser("schema", help="Extract Oracle schema metadata")
    schema_parser.add_argument("--format", choices=["markdown", "txt"], default=None)
    schema_parser.add_argument("--owner", help="Schema owner (overrides config)")
    schema_parser.add_argument("--table", help="Extract specific table only")

    # query command
    query_parser = subparsers.add_parser("query", help="Analyze MyBatis mapper XML files")
    query_parser.add_argument("mybatis_dir", help="Path to MyBatis/iBatis mapper XML directory")
    query_parser.add_argument("--schema-md", help="Path to schema .md file (filters out non-existent tables)")

    # erd command (direct, requires Oracle connection)
    erd_parser = subparsers.add_parser("erd", help="Generate Mermaid ERD (direct DB access)")
    erd_parser.add_argument("--mybatis-dir", help="Path to MyBatis mapper XML directory")
    erd_parser.add_argument("--owner", help="Schema owner (overrides config)")
    erd_parser.add_argument("--table", help="Extract specific table only")
    erd_parser.add_argument("--llm-assist", action="store_true",
                            help="Use local LLM for column descriptions, missing relations, domain grouping")

    # embed command
    embed_parser = subparsers.add_parser("embed", help="Embed .md files into vector DB")
    embed_parser.add_argument("--schema-md", help="Path to schema .md file")
    embed_parser.add_argument("--query-md", help="Path to query analysis .md file")

    # terms command
    terms_parser = subparsers.add_parser("terms", help="Generate terminology dictionary")
    terms_parser.add_argument("--schema-md", help="Path to schema .md file")
    terms_parser.add_argument("--react-dir", help="Path to React source directory")
    terms_parser.add_argument("--skip-llm", action="store_true",
                              help="Skip LLM enrichment (collect words only)")

    # standardize command
    std_parser = subparsers.add_parser("standardize", help="Generate standardization analysis report")
    std_parser.add_argument("--schema-md", required=True, help="Path to schema .md file")
    std_parser.add_argument("--query-md", help="Path to query analysis .md file")
    std_parser.add_argument("--validate-data", action="store_true",
                            help="Validate actual data via Oracle (code columns, Y/N, usage)")
    std_parser.add_argument("--skip-usage", action="store_true",
                            help="Skip column usage validation (slow for large schemas)")

    # enrich-schema command
    enrich_parser = subparsers.add_parser("enrich-schema", help="Enrich schema with LLM-generated comments")
    enrich_parser.add_argument("--schema-md", required=True, help="Path to schema .md file")

    # erd-md command (from .md files, no DB, no LLM)
    erd_md_parser = subparsers.add_parser("erd-md", help="Generate ERD from .md files (no DB, no LLM)")
    erd_md_parser.add_argument("--schema-md", required=True, help="Path to schema .md file")
    erd_md_parser.add_argument("--query-md", help="Path to query analysis .md file")
    erd_md_parser.add_argument("--tables", help="Comma-separated table names to focus on (+ related tables)")
    erd_md_parser.add_argument("--related-only", action="store_true",
                               help="Only include tables that have relationships")

    # erd-group command (grouped by relationship clusters)
    erd_group_parser = subparsers.add_parser("erd-group", help="Generate ERD files grouped by relationship clusters")
    erd_group_parser.add_argument("--schema-md", required=True, help="Path to schema .md file")
    erd_group_parser.add_argument("--query-md", help="Path to query analysis .md file")
    erd_group_parser.add_argument("--max-size", type=int, default=30,
                                  help="Max tables per group (default: 30)")
    erd_group_parser.add_argument("--common-tables",
                                  help="Comma-separated common table names (e.g. TB_USER,TB_DEPT)")
    erd_group_parser.add_argument("--common-tables-file",
                                  help="Path to common_tables.txt file")
    erd_group_parser.add_argument("--common-threshold", type=int, default=None,
                                  help="Auto-detect: tables joined with N+ others are common (default: auto)")
    erd_group_parser.add_argument("--export-common", action="store_true",
                                  help="Export auto-detected common tables to common_tables.txt")

    # erd-rag command
    erd_rag_parser = subparsers.add_parser("erd-rag", help="Generate ERD via RAG (vector DB + LLM)")
    erd_rag_parser.add_argument("--tables", help="Comma-separated table names to focus on")

    # all command
    all_parser = subparsers.add_parser("all", help="Run schema + query + erd")
    all_parser.add_argument("mybatis_dir", help="Path to MyBatis mapper XML directory")
    all_parser.add_argument("--format", choices=["markdown", "txt"], default=None)
    all_parser.add_argument("--owner", help="Schema owner (overrides config)")
    all_parser.add_argument("--table", help="Extract specific table only")
    all_parser.add_argument("--llm-assist", action="store_true",
                            help="Use local LLM for ERD enrichment")

    args = parser.parse_args()

    if args.command == "schema":
        cmd_schema(args)
    elif args.command == "query":
        cmd_query(args)
    elif args.command == "erd":
        cmd_erd(args)
    elif args.command == "embed":
        cmd_embed(args)
    elif args.command == "terms":
        cmd_terms(args)
    elif args.command == "standardize":
        cmd_standardize(args)
    elif args.command == "enrich-schema":
        cmd_enrich_schema(args)
    elif args.command == "erd-md":
        cmd_erd_md(args)
    elif args.command == "erd-group":
        cmd_erd_group(args)
    elif args.command == "erd-rag":
        cmd_erd_rag(args)
    elif args.command == "all":
        cmd_all(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
