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
    """Load YAML config and resolve ``${VAR}`` references from env.

    Unresolved placeholders are replaced with empty strings (and a
    warning is printed) so downstream callers never accidentally ship a
    literal ``${LLM_MODEL}`` to an API client.
    """
    with open(config_path, "r", encoding="utf-8") as f:
        content = f.read()

    missing = []

    def replace_env(match):
        var_name = match.group(1)
        value = os.environ.get(var_name)
        if value is None:
            missing.append(var_name)
            return ""
        return value

    content = re.sub(r"\$\{(\w+)\}", replace_env, content)
    if missing:
        uniq = sorted(set(missing))
        print(
            f"  [경고] config.yaml 의 환경변수 {uniq} 미정의 — .env 가 로드됐는지 확인. "
            f"빈 값으로 대체됩니다."
        )
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


def cmd_audit_standards(args):
    """Audit entire schema for naming standard violations."""
    from oracle_embeddings.md_parser import parse_schema_md
    from oracle_embeddings.standards_auditor import audit_schema, save_audit_markdown, save_audit_excel

    load_dotenv()
    config = load_config(args.config)
    output_dir = config.get("storage", {}).get("output_dir", "./output")

    if not args.schema_md:
        print("Error: --schema-md 는 필수입니다.")
        return

    # 1. Parse schema
    print(f"=== Step 1: Parsing Schema: {args.schema_md} ===")
    schema = parse_schema_md(args.schema_md)
    print(f"  Tables: {len(schema['tables'])}")

    # 2. Audit
    print("\n=== Step 2: Auditing ===")
    audit = audit_schema(schema, terms_dict_path=args.terms_md)
    print(f"  Total tables: {audit['total_tables']}, Invalid: {audit['invalid_tables']}")
    print(f"  Total columns: {audit['total_columns']}, Invalid: {audit['invalid_columns']}")
    print(f"  Severity: CRITICAL={audit['severity_counts'].get('CRITICAL', 0)}, "
          f"HIGH={audit['severity_counts'].get('HIGH', 0)}, "
          f"MEDIUM={audit['severity_counts'].get('MEDIUM', 0)}, "
          f"LOW={audit['severity_counts'].get('LOW', 0)}")

    # 3. Save reports
    print("\n=== Step 3: Saving Reports ===")
    md_path = save_audit_markdown(audit, output_dir)
    xlsx_path = save_audit_excel(audit, output_dir)

    print(f"\n  Markdown: {os.path.abspath(md_path)}")
    print(f"  Excel:    {os.path.abspath(xlsx_path)}")


def cmd_gen_ddl(args):
    """Generate DDL from natural language request with validation."""
    from oracle_embeddings.ddl_generator import generate_ddl, save_ddl
    from oracle_embeddings.naming_validator import NamingValidator, format_result_console

    load_dotenv()
    config = load_config(args.config)
    output_dir = config.get("storage", {}).get("output_dir", "./output")

    if not args.request:
        print("Error: --request 로 자연어 요청을 입력하세요.")
        print('예: python main.py gen-ddl --request "고객 주문 이력 테이블 만들어줘" --terms-md ./output/terms.md')
        return

    # 1. Generate DDL
    print("=== Step 1: Generating DDL ===")
    print(f"  Request: {args.request}")
    print(f"  LLM model: {os.environ.get('LLM_MODEL') or config.get('llm', {}).get('model', 'llama3')}")

    result = generate_ddl(
        args.request,
        config,
        terms_md=args.terms_md,
        schema_md=args.schema_md,
    )

    if result.get("error"):
        print(f"  Error: {result['error']}")
        return

    table_name = result.get("table_name", "")
    ddl = result.get("ddl", "")

    print(f"\n  Table: {table_name}")
    print(f"  Comment: {result.get('table_comment', '')}")
    print(f"\n--- Generated DDL ---")
    print(ddl)
    print("---")
    if result.get("explanation"):
        print(f"\n  Explanation: {result['explanation']}")

    # 2. Validate
    print("\n=== Step 2: Validating Naming ===")
    validator = NamingValidator(terms_dict_path=args.terms_md)
    validation_results = validator.validate_ddl(ddl)

    has_issues = False
    for vr in validation_results:
        if not vr["valid"]:
            has_issues = True
        print(format_result_console(vr))
        for cr in vr.get("columns", []):
            if not cr["valid"]:
                has_issues = True
                print(format_result_console(cr))

    if has_issues:
        print("\n  WARNING: 네이밍 표준 위반이 있습니다. DDL 수정 후 재생성 권장.")
    else:
        print("\n  OK: 모든 이름이 표준을 준수합니다.")

    # 3. Save
    print("\n=== Step 3: Saving DDL ===")
    filepath = save_ddl(result, output_dir)
    print(f"  Saved: {os.path.abspath(filepath)}")

    # 4. Confirmation prompt (if requested)
    if args.execute:
        print("\n=== Step 4: Confirmation ===")
        print("실제 DB에 DDL을 실행하시겠습니까?")
        confirm = input("  (yes/no): ").strip().lower()
        if confirm in ("yes", "y"):
            try:
                from oracle_embeddings.db import get_connection
                connection = get_connection(config)
                try:
                    with connection.cursor() as cursor:
                        cursor.execute(ddl)
                    connection.commit()
                    print(f"  DDL executed successfully.")
                finally:
                    connection.close()
            except Exception as e:
                print(f"  Error executing DDL: {e}")
        else:
            print("  DDL execution cancelled.")


def cmd_validate_naming(args):
    """Validate table/column names against naming standards."""
    from oracle_embeddings.naming_validator import (
        NamingValidator, format_result_console, save_validation_report
    )

    load_dotenv()
    config = load_config(args.config)
    output_dir = config.get("storage", {}).get("output_dir", "./output")

    validator = NamingValidator(terms_dict_path=args.terms_md)
    print(f"  Loaded: {len(validator.standard_abbreviations)} abbreviations, "
          f"{len(validator.standard_words)} full words")

    results = []

    if args.name:
        # Single name
        kind = args.kind or "table"
        result = validator.validate_name(args.name, kind=kind)
        print(format_result_console(result))
        results.append(result)

    elif args.file:
        # File with list of names
        if not os.path.exists(args.file):
            print(f"Error: File not found: {args.file}")
            return
        with open(args.file, "r", encoding="utf-8") as f:
            content = f.read().replace("\r\n", "\n")
        kind = args.kind or "table"
        names = [l.strip() for l in content.split("\n") if l.strip() and not l.startswith("#")]
        print(f"=== Validating {len(names)} {kind} names ===")
        for name in names:
            result = validator.validate_name(name, kind=kind)
            results.append(result)
            if not result["valid"]:
                print(format_result_console(result))

    elif args.ddl:
        # DDL file
        if not os.path.exists(args.ddl):
            print(f"Error: DDL file not found: {args.ddl}")
            return
        with open(args.ddl, "r", encoding="utf-8") as f:
            ddl_text = f.read().replace("\r\n", "\n")
        print(f"=== Validating DDL: {args.ddl} ===")
        table_results = validator.validate_ddl(ddl_text)
        for tr in table_results:
            results.append(tr)
            print(format_result_console(tr))
            for cr in tr.get("columns", []):
                results.append(cr)
                if not cr["valid"]:
                    print(format_result_console(cr))
    else:
        print("Error: --name, --file, 또는 --ddl 중 하나를 지정하세요.")
        return

    # Summary + report
    valid = sum(1 for r in results if r["valid"])
    invalid = len(results) - valid
    print(f"\n=== Summary ===")
    print(f"  Total: {len(results)}")
    print(f"  Valid: {valid}")
    print(f"  Invalid: {invalid}")

    if len(results) > 1:
        md_path, xlsx_path = save_validation_report(results, output_dir)
        print(f"\n  Report: {os.path.abspath(md_path)}")
        print(f"  Excel:  {os.path.abspath(xlsx_path)}")


def cmd_review_sql(args):
    """Review SQL queries for inefficient patterns (static analysis + LLM)."""
    from oracle_embeddings.mybatis_parser import parse_all_mappers
    from oracle_embeddings.sql_reviewer import review_statements
    from oracle_embeddings.sql_review_report import save_review_markdown, save_review_excel

    load_dotenv()
    config = load_config(args.config)
    output_dir = config.get("storage", {}).get("output_dir", "./output")

    if not args.mybatis_dir:
        print("Error: --mybatis-dir 는 필수입니다.")
        return

    if not os.path.isdir(args.mybatis_dir):
        print(f"Error: Directory not found: {args.mybatis_dir}")
        return

    # 1. Parse XML
    print("=== Step 1: Parsing MyBatis/iBatis XML ===")
    analysis = parse_all_mappers(args.mybatis_dir)
    statements = analysis["statements"]
    print(f"  Statements: {len(statements)}")

    # 2. Static analysis
    print("\n=== Step 2: Static Analysis ===")
    review = review_statements(statements)
    print(f"  Statements with issues: {review['statements_with_issues']}")
    print(f"  Severity: CRITICAL={review['severity_summary']['CRITICAL']}, "
          f"HIGH={review['severity_summary']['HIGH']}, "
          f"MEDIUM={review['severity_summary']['MEDIUM']}, "
          f"LOW={review['severity_summary']['LOW']}")

    # 3. LLM review (optional)
    llm_reviews = []
    if args.llm_review:
        print("\n=== Step 3: LLM Review ===")
        from oracle_embeddings.sql_reviewer_llm import llm_review_batch
        max_samples = args.max_samples or 20
        llm_reviews = llm_review_batch(review["by_statement"], config, max_samples)
        print(f"  LLM reviewed: {len(llm_reviews)} statements")
    else:
        print("\n=== Step 3: LLM Review (skipped, use --llm-review) ===")

    # 4. Save reports
    print("\n=== Step 4: Saving Reports ===")
    md_path = save_review_markdown(review, llm_reviews, output_dir)
    xlsx_path = save_review_excel(review, llm_reviews, output_dir)

    print(f"\n  Markdown: {os.path.abspath(md_path)}")
    print(f"  Excel:    {os.path.abspath(xlsx_path)}")


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


def cmd_convert_menu(args):
    """메뉴 Excel / 붙여넣기 텍스트 → 표준 menu.md 변환 (LLM 이 헤더 매핑 학습)."""
    from oracle_embeddings.menu_converter import convert_menu

    load_dotenv()
    config = load_config(args.config) if os.path.exists(args.config) else {}

    xlsx = getattr(args, "menu_xlsx", None)
    text = getattr(args, "menu_md_in", None)
    if not xlsx and not text:
        print("Error: --menu-xlsx 또는 --menu-md-in 중 하나를 지정하세요.")
        return
    if xlsx and text:
        print("Error: --menu-xlsx 와 --menu-md-in 은 동시에 지정할 수 없습니다.")
        return
    source = xlsx or text
    if not os.path.isfile(source):
        print(f"Error: 입력 파일 없음: {source}")
        return

    output = args.output or "input/menu.md"
    abs_path = convert_menu(
        xlsx, output, config,
        sheet_name=args.sheet,
        use_llm=not args.no_llm,
        text_path=text,
    )
    print(f"\nMenu Markdown 저장됨: {abs_path}")
    print(f"사용 예: python main.py analyze-legacy --menu-md {output} ...")


def cmd_convert_mapping(args):
    """AS-IS↔TO-BE 컬럼 매핑 .md → column_mapping.yaml 자동 변환 (LLM 옵션)."""
    from pathlib import Path as _Path
    from oracle_embeddings.migration.mapping_converter import convert_mapping_md

    load_dotenv()
    config = load_config(args.config) if os.path.exists(args.config) else {}

    md_path = _Path(args.mapping_md)
    if not md_path.is_file():
        print(f"Error: --mapping-md 파일 없음: {md_path}")
        return
    output_path = _Path(args.output or "input/column_mapping.yaml")

    abs_path = convert_mapping_md(
        md_path, output_path,
        use_llm=not args.no_llm,
        config=config,
    )
    print(f"\ncolumn_mapping.yaml 저장됨: {abs_path}")
    print(f"사용 예: python main.py migration-impact --mapping {output_path} ...")


def cmd_migrate_sql(args):
    """column_mapping.yaml 기반으로 MyBatis XML 전체를 TO-BE 스키마용으로 변환.

    Stage A (sqlglot static) 는 항상 실행. --llm-fallback 이면 NEEDS_LLM 상태
    statement 를 LLM 으로 보조 변환 시도. 산출물: output/sql_migration/
    converted/<경로>.xml (--output-format 에 xml 포함) + Excel 5 시트 리포트.
    """
    import os
    from datetime import datetime as _dt
    from pathlib import Path

    from oracle_embeddings.migration.comment_injector import inject_comments
    from oracle_embeddings.migration.impact_analyzer import load_schema_tables
    from oracle_embeddings.migration.llm_fallback import llm_rewrite
    from oracle_embeddings.migration.mapping_loader import load_mapping
    from oracle_embeddings.migration.migration_report import (
        write_migration_report,
    )
    from oracle_embeddings.migration.validator_static import validate_static
    from oracle_embeddings.migration.xml_rewriter import (
        annotate_statements, rewrite_xml, serialize_tree,
    )

    load_dotenv()
    config = load_config(args.config) if os.path.exists(args.config) else {}

    mybatis_dir = Path(args.mybatis_dir)
    mapping_path = Path(args.mapping)
    to_be_schema_path = Path(args.to_be_schema)
    terms_md = Path(args.terms_md) if args.terms_md else None

    if not mybatis_dir.is_dir():
        print(f"Error: --mybatis-dir 없음: {mybatis_dir}")
        return
    if not mapping_path.is_file():
        print(f"Error: --mapping 파일 없음: {mapping_path}")
        return
    if not to_be_schema_path.is_file():
        print(f"Error: --to-be-schema 파일 없음: {to_be_schema_path}")
        return

    formats = {f.strip().lower() for f in (args.output_format or "excel,xml").split(",")}
    emit_excel = "excel" in formats
    emit_xml = "xml" in formats

    to_be_schema_tables = load_schema_tables(to_be_schema_path)
    try:
        mapping = load_mapping(mapping_path, to_be_schema=to_be_schema_tables)
    except Exception as exc:
        print(f"Error: 매핑 파일 로드 실패:\n{exc}")
        return

    # Korean comment lookup for --emit-column-comments
    ko_lookup: dict = {}
    if args.emit_column_comments:
        ko_lookup = _build_ko_lookup(to_be_schema_path, terms_md)
        print(f"Korean comment lookup: {len(ko_lookup)} entries")

    # Collect XML files
    xml_files = sorted(mybatis_dir.rglob("*.xml"))
    print(f"Scanning {len(xml_files)} XML file(s) under {mybatis_dir}...")

    all_results = []
    out_root = Path("output/sql_migration")
    converted_root = out_root / "converted"

    for xml_path in xml_files:
        out = rewrite_xml(xml_path, mapping)
        if out.parse_error:
            print(f"  skip (parse err): {xml_path}: {out.parse_error}")
            continue

        for rr in out.results:
            # Stage A
            if rr.to_be_sql:
                vr = validate_static(rr.to_be_sql, to_be_schema_tables)
                rr.stage_a_pass = vr.ok
                if not vr.ok and not rr.parse_error:
                    rr.parse_error = "; ".join(
                        f"[{i.code}] {i.message}" for i in vr.errors
                    )

            # LLM fallback
            if args.llm_fallback and rr.status == "NEEDS_LLM":
                outcome_stub = _make_partial_outcome(rr)
                llm_out = llm_rewrite(
                    rr.as_is_sql,
                    mapping,
                    partial_outcome=outcome_stub,
                    config=config,
                )
                if llm_out.error:
                    rr.warnings.append(f"LLM fallback error: {llm_out.error}")
                elif llm_out.converted_sql:
                    rr.to_be_sql = llm_out.converted_sql
                    rr.llm_confidence = llm_out.confidence
                    rr.llm_reasoning = llm_out.review_reason
                    rr.conversion_method = "LLM"
                    rr.applied_transformers.append("LLMFallback")
                    if llm_out.needs_human_review:
                        rr.status = "UNRESOLVED"
                    else:
                        rr.status = "AUTO_WARN"
                    # Re-run Stage A on the LLM output
                    vr = validate_static(llm_out.converted_sql, to_be_schema_tables)
                    rr.stage_a_pass = vr.ok
                    if not vr.ok:
                        rr.parse_error = "; ".join(
                            f"[{i.code}] {i.message}" for i in vr.errors
                        )

            # Korean comments
            if args.emit_column_comments and rr.to_be_sql and ko_lookup:
                scopes = mapping.options.comment_scope
                rr.to_be_sql = inject_comments(
                    rr.to_be_sql, ko_lookup, scopes=scopes,
                )

        all_results.extend(out.results)

        if emit_xml and not args.dry_run and out.tree is not None:
            annotate_statements(
                out.tree, out.results,
                preserve_as_is=not args.no_xml_preserve_as_is,
            )
            rel = xml_path.relative_to(mybatis_dir)
            out_path = converted_root / rel
            serialize_tree(out.tree, out_path)

    # Stats
    from collections import Counter
    status_count = Counter(r.status for r in all_results)
    total = len(all_results)
    auto = status_count.get("AUTO", 0)
    auto_warn = status_count.get("AUTO_WARN", 0)
    needs_llm = status_count.get("NEEDS_LLM", 0)
    unresolved = status_count.get("UNRESOLVED", 0)
    parse_fail = status_count.get("PARSE_FAIL", 0)

    print()
    print(f"Converted {total} statement(s): "
          f"AUTO={auto} AUTO_WARN={auto_warn} NEEDS_LLM={needs_llm} "
          f"UNRESOLVED={unresolved} PARSE_FAIL={parse_fail}")
    stage_a_passed = sum(1 for r in all_results if r.stage_a_pass)
    stage_a_ran = sum(1 for r in all_results if r.stage_a_pass is not None)
    if stage_a_ran:
        print(f"Stage A: {stage_a_passed}/{stage_a_ran} pass "
              f"({stage_a_passed / stage_a_ran * 100:.1f}%)")

    if emit_excel:
        ts = _dt.now().strftime("%Y%m%d_%H%M%S")
        report_path = out_root / f"sql_migration_{ts}.xlsx"
        write_migration_report(
            all_results, mapping, report_path,
            mybatis_dir=mybatis_dir,
            mapping_path=mapping_path,
        )
        print(f"Excel report: {report_path}")
    if emit_xml and not args.dry_run:
        print(f"Converted XML: {converted_root}")
    elif args.dry_run:
        print("(--dry-run: XML 파일은 작성되지 않음)")


def _build_ko_lookup(to_be_schema_path, terms_md):
    """Assemble ``{TABLE.COL: 한글}`` from TO-BE schema.md (column comments)
    plus an optional terms_dictionary.md (Korean gloss)."""
    from oracle_embeddings.md_parser import parse_schema_md
    out: dict = {}
    schema = parse_schema_md(str(to_be_schema_path))
    for tbl in schema["tables"]:
        for col in tbl["columns"]:
            c = col.get("comment")
            if c:
                out[f"{tbl['name'].upper()}.{col['column_name'].upper()}"] = c
    if terms_md and terms_md.is_file():
        try:
            import re
            text = terms_md.read_text(encoding="utf-8")
            for m in re.finditer(
                r"^\|\s*[^|]*\|\s*([A-Z_][A-Z0-9_]*)\s*\|\s*([^|]+?)\s*\|",
                text, flags=re.MULTILINE,
            ):
                col = m.group(1).upper()
                ko = m.group(2).strip()
                if ko and col:
                    out.setdefault(col, ko)
        except Exception:
            pass
    return out


def _make_partial_outcome(rr):
    """Adapt a RewriteResult into a lightweight SqlRewriteOutcome for
    llm_fallback.llm_rewrite (it only reads warnings + to_be_sql)."""
    from oracle_embeddings.migration.sql_rewriter import SqlRewriteOutcome
    return SqlRewriteOutcome(
        as_is_sql=rr.as_is_sql,
        to_be_sql=rr.to_be_sql,
        status=rr.status,
        applied_transformers=list(rr.applied_transformers),
        changed_items=list(rr.changed_items),
        warnings=list(rr.warnings),
    )


def cmd_validate_migration(args):
    """변환된 XML 의 TO-BE SQL 을 TO-BE DB 에 parse-only 로 검증 (Stage B).

    cursor.parse() 로 실행 없이 구문 + 스키마 검증만 수행. 기본 10 parallel.
    --dry-run 로 DB 접속 없이 statement 카운트만 확인 가능.
    """
    import os
    from datetime import datetime as _dt
    from pathlib import Path
    from typing import Dict, Tuple

    from lxml import etree

    from oracle_embeddings.migration.dynamic_sql_expander import (
        build_sql_includes, expand_paths,
    )
    from oracle_embeddings.migration.validator_db import (
        BatchItem, BatchResult, validate_db_batch, write_validation_report,
    )
    from oracle_embeddings.migration.validator_static import ValidationResult
    from oracle_embeddings.mybatis_parser import _read_file_safe

    load_dotenv()

    converted_dir = Path(args.converted_dir)
    if not converted_dir.is_dir():
        print(f"Error: --converted-dir 없음: {converted_dir}")
        return

    items = []
    stmt_meta: Dict[Tuple[str, ...], Dict] = {}
    xml_files = sorted(converted_dir.rglob("*.xml"))
    print(f"Scanning {len(xml_files)} XML files...")

    for xml_path in xml_files:
        try:
            text = _read_file_safe(str(xml_path))
            root = etree.fromstring(text.encode("utf-8"))
        except Exception as e:
            print(f"  skip {xml_path}: {e}")
            continue
        namespace = root.get("namespace", "") or ""
        sql_includes = build_sql_includes(root)
        for elem in root.iter():
            tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if tag not in ("select", "insert", "update", "delete"):
                continue
            sql_id = elem.get("id", "") or ""
            paths = expand_paths(elem, sql_includes=sql_includes)
            if not paths:
                continue
            sql = paths[0].rendered_sql
            rel = str(xml_path.relative_to(converted_dir))
            key = (rel, namespace, sql_id)
            items.append(BatchItem(key=key, sql=sql))
            stmt_meta[key] = {"sql": sql}

    print(f"Collected {len(items)} statement(s)")

    if args.dry_run:
        print("--dry-run: DB 접속 skip; 리포트만 생성")
        results = [
            BatchResult(key=it.key, result=ValidationResult(ok=True))
            for it in items
        ]
    else:
        # TO-BE 우선, 미설정 시 AS-IS env (ORACLE_*) 로 fallback. CLI 가 항상 우선.
        dsn = args.dsn or os.environ.get("ORACLE_TOBE_DSN", "")
        user = (
            args.user
            or os.environ.get("ORACLE_TOBE_USER")
            or os.environ.get("ORACLE_USER", "")
        )
        password = (
            args.password
            or os.environ.get("ORACLE_TOBE_PASSWORD")
            or os.environ.get("ORACLE_PASSWORD", "")
        )
        instant_dir = (
            args.instant_client_dir
            or os.environ.get("ORACLE_INSTANT_CLIENT_DIR")
        )
        if not dsn:
            print("Error: --dsn 또는 ORACLE_TOBE_DSN env 필요")
            return
        if not password:
            print("Error: --password 또는 ORACLE_TOBE_PASSWORD/ORACLE_PASSWORD env 필요")
            return
        print(f"Connecting to {dsn} as {user} (parallel={args.parallel})...")
        results = validate_db_batch(
            items,
            dsn=dsn,
            user=user,
            password=password,
            parallel=args.parallel,
            thick_mode=True,
            oracle_client_dir=instant_dir,
        )

    passed = sum(1 for r in results if r.result.ok)
    failed = len(results) - passed
    print(f"\nStage B: {passed} pass / {failed} fail / {len(results)} total")

    output = (
        Path(args.report)
        if args.report
        else converted_dir.parent / f"validation_report_{_dt.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    )
    write_validation_report(results, stmt_meta, output)
    print(f"Report: {output}")


def cmd_migration_impact(args):
    """column_mapping.yaml 가 AS-IS MyBatis 쿼리에 얼마나 영향을 주는지 사전 분석.

    변환은 안 함 — 매핑 파일 문법 검증 + 스키마 대조 + 영향 리포트만.
    """
    from pathlib import Path
    from datetime import datetime as _dt
    from oracle_embeddings.migration.impact_analyzer import (
        analyze_impact, print_impact_summary, write_impact_excel,
    )

    mybatis_dir = Path(args.mybatis_dir)
    mapping_path = Path(args.mapping)
    as_is_schema = Path(args.as_is_schema)
    to_be_schema = Path(args.to_be_schema) if args.to_be_schema else None

    if not mybatis_dir.is_dir():
        print(f"Error: --mybatis-dir 디렉토리 없음: {mybatis_dir}")
        return
    if not mapping_path.is_file():
        print(f"Error: --mapping 파일 없음: {mapping_path}")
        return
    if not as_is_schema.is_file():
        print(f"Error: --as-is-schema 파일 없음: {as_is_schema}")
        return
    if to_be_schema is not None and not to_be_schema.is_file():
        print(f"Error: --to-be-schema 파일 없음: {to_be_schema}")
        return

    report = analyze_impact(
        mybatis_dir=mybatis_dir,
        mapping_path=mapping_path,
        as_is_schema_path=as_is_schema,
        to_be_schema_path=to_be_schema,
    )
    print_impact_summary(report)

    if args.output:
        output_path = Path(args.output)
    else:
        ts = _dt.now().strftime("%Y%m%d_%H%M%S")
        output_path = Path("output/sql_migration") / f"impact_report_{ts}.xlsx"
    write_impact_excel(report, output_path)
    print(f"\nExcel report: {output_path}")


def cmd_discover_patterns(args):
    """Discover project-specific patterns using LLM."""
    from oracle_embeddings.legacy_pattern_discovery import discover_patterns, save_patterns

    load_dotenv()
    config = load_config(args.config) if os.path.exists(args.config) else {}
    output_dir = config.get("storage", {}).get("output_dir", "./output")

    if not os.path.isdir(args.backend_dir):
        print(f"Error: Backend dir not found: {args.backend_dir}")
        return

    patterns = discover_patterns(
        args.backend_dir, config,
        menu_md=getattr(args, "menu_md", None),
        frontends_root=getattr(args, "frontends_root", None),
        frontend_dir=getattr(args, "frontend_dir", None),
    )

    if args.output:
        output_path = args.output
    else:
        os.makedirs(os.path.join(output_dir, "legacy_analysis"), exist_ok=True)
        output_path = os.path.join(output_dir, "legacy_analysis", "patterns.yaml")

    save_patterns(patterns, output_path)
    print(f"\nPatterns saved: {os.path.abspath(output_path)}")
    print(f"Usage: python main.py analyze-legacy --backend-dir {args.backend_dir} --patterns {output_path}")


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


def cmd_analyze_legacy(args):
    """Analyze AS-IS legacy sources (backend + frontend + DB menu).

    Supports two modes:

      * Single project — ``--backend-dir /path/to/one-backend``
      * Batch (monorepo) — ``--backends-root /path/containing/many-backends``
        scans each direct child directory and emits a single combined
        Markdown + Excel.
    """
    from oracle_embeddings.legacy_analyzer import (
        analyze_legacy,
        analyze_legacy_batch,
    )
    from oracle_embeddings.legacy_report import (
        save_legacy_batch_excel,
        save_legacy_batch_markdown,
        save_legacy_excel,
        save_legacy_markdown,
    )

    load_dotenv()
    config = load_config(args.config) if os.path.exists(args.config) else {}
    output_dir = config.get("storage", {}).get("output_dir", "./output")

    backends_root = args.backends_root
    backend_dir = args.backend_dir

    if not backends_root and not backend_dir:
        print("Error: either --backend-dir or --backends-root is required.")
        return
    if backends_root and backend_dir:
        print("Error: --backend-dir and --backends-root are mutually exclusive.")
        return
    if backends_root and not os.path.isdir(backends_root):
        print(f"Error: Backends root not found: {backends_root}")
        return
    if backend_dir and not os.path.isdir(backend_dir):
        print(f"Error: Backend dir not found: {backend_dir}")
        return
    # Resolve frontend dir: --frontends-root takes priority over --frontend-dir
    frontend_dir = getattr(args, "frontends_root", None) or args.frontend_dir
    is_frontends_root = bool(getattr(args, "frontends_root", None))
    if frontend_dir and not os.path.isdir(frontend_dir):
        print(f"Error: Frontend dir not found: {frontend_dir}")
        return

    # Resolve menu source priority:
    #   1. --skip-menu            → no menu lookup
    #   2. --menu-md PATH         → read from a Markdown pipe table (DRM-free)
    #   3. --menu-xlsx PATH       → read from a project-specific Excel
    #   4. (default)              → query the DB menu table
    menu_programs = None
    if args.skip_menu:
        pass
    elif args.menu_md:
        try:
            from oracle_embeddings.legacy_menu_loader import load_menu_from_markdown
            print("=== Step 1: Loading menu Markdown ===")
            print(f"  Menu md: {args.menu_md}")
            menu_programs = load_menu_from_markdown(args.menu_md)
            print(f"  Menu programs: {len(menu_programs)}")
        except FileNotFoundError:
            print(f"  Error: menu md not found: {args.menu_md}")
            return
        except Exception as e:
            print(f"  Warning: Menu Markdown load failed - {e}")
            menu_programs = None
    elif args.menu_xlsx:
        try:
            from oracle_embeddings.legacy_menu_loader import load_menu_from_excel
            print("=== Step 1: Loading menu Excel ===")
            print(f"  Menu xlsx: {args.menu_xlsx}")
            menu_programs = load_menu_from_excel(args.menu_xlsx)
            print(f"  Menu programs: {len(menu_programs)}")
        except FileNotFoundError:
            print(f"  Error: menu xlsx not found: {args.menu_xlsx}")
            return
        except Exception as e:
            print(f"  Warning: Menu Excel load failed - {e}")
            print("  Continuing without menu hierarchy (use --skip-menu to suppress).")
            menu_programs = None
    else:
        try:
            from oracle_embeddings.legacy_menu_loader import load_menu_hierarchy
            print("=== Step 1: Loading menu table ===")
            menu_programs = load_menu_hierarchy(config, table_override=args.menu_table)
            print(f"  Menu programs: {len(menu_programs)}")
        except Exception as e:
            print(f"  Warning: Menu table load failed - {e}")
            print("  Continuing without menu hierarchy (use --skip-menu to suppress).")
            menu_programs = None

    print("\n=== Step 2: Parsing sources ===")
    rfc_depth = args.rfc_depth
    if rfc_depth is None:
        rfc_depth = config.get("legacy", {}).get("rfc_depth", 2)

    frontend_framework = args.frontend_framework
    if frontend_framework == "auto":
        frontend_framework = None  # let analyzer auto-detect

    # Load patterns file if provided
    loaded_patterns = None
    if getattr(args, "patterns", None):
        from oracle_embeddings.legacy_pattern_discovery import load_patterns
        print(f"  Loading patterns: {args.patterns}")
        loaded_patterns = load_patterns(args.patterns)

    menu_only = getattr(args, "menu_only", False)

    if backends_root:
        result = analyze_legacy_batch(
            backends_root=backends_root,
            frontend_dir=frontend_dir,
            menu_rows=menu_programs,
            rfc_depth=rfc_depth,
            include_all=args.include_all,
            frontend_framework=frontend_framework,
            patterns=loaded_patterns,
            frontends_root=is_frontends_root,
            menu_only=menu_only,
        )
    else:
        result = analyze_legacy(
            backend_dir=backend_dir,
            frontend_dir=frontend_dir,
            menu_rows=menu_programs,
            rfc_depth=rfc_depth,
            frontend_framework=frontend_framework,
            patterns=loaded_patterns,
            frontends_root=is_frontends_root,
            menu_only=menu_only,
        )

    print("\n=== Step 3: Writing report ===")
    fmt = args.format
    md_path = None
    xlsx_path = None
    if result.get("is_batch"):
        if fmt in ("markdown", "both"):
            md_path = save_legacy_batch_markdown(result, output_dir, menu_only=menu_only)
            print(f"  Markdown: {os.path.abspath(md_path)}")
        if fmt in ("excel", "both"):
            xlsx_path = save_legacy_batch_excel(result, output_dir, menu_only=menu_only)
            print(f"  Excel:    {os.path.abspath(xlsx_path)}")
    else:
        if fmt in ("markdown", "both"):
            md_path = save_legacy_markdown(result, output_dir, menu_only=menu_only)
            print(f"  Markdown: {os.path.abspath(md_path)}")
        if fmt in ("excel", "both"):
            xlsx_path = save_legacy_excel(result, output_dir, menu_only=menu_only)
            print(f"  Excel:    {os.path.abspath(xlsx_path)}")

    s = result["stats"]
    print()
    if result.get("is_batch"):
        print(f"  Projects: {s.get('projects', 0)}")
    print(f"  Endpoints: {s['endpoints']} "
          f"(matched: {s['matched']}, unmatched: {s['unmatched']})")
    print(f"  With React file: {s.get('with_react', 0)}, With RFC: {s.get('with_rfc', 0)}")


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

    # audit-standards command
    audit_parser = subparsers.add_parser("audit-standards", help="Audit schema for naming standard violations")
    audit_parser.add_argument("--schema-md", required=True, help="Path to schema .md file")
    audit_parser.add_argument("--terms-md", help="Path to terms_dictionary .md file")

    # gen-ddl command
    gen_ddl_parser = subparsers.add_parser("gen-ddl", help="Generate DDL from natural language request")
    gen_ddl_parser.add_argument("--request", required=True, help="Natural language request (예: '고객 주문 이력 테이블 만들어줘')")
    gen_ddl_parser.add_argument("--terms-md", help="Path to terms_dictionary .md for standard abbreviations")
    gen_ddl_parser.add_argument("--schema-md", help="Path to schema .md for style reference")
    gen_ddl_parser.add_argument("--execute", action="store_true",
                                 help="Prompt to execute DDL on Oracle after confirmation")

    # validate-naming command
    vn_parser = subparsers.add_parser("validate-naming", help="Validate table/column names against naming standards")
    vn_parser.add_argument("--name", help="Single name to validate")
    vn_parser.add_argument("--file", help="File containing list of names (one per line)")
    vn_parser.add_argument("--ddl", help="DDL file to parse and validate")
    vn_parser.add_argument("--kind", choices=["table", "column"], default=None,
                            help="Kind of name (default: table for --name/--file)")
    vn_parser.add_argument("--terms-md", help="Path to terms_dictionary .md file")

    # review-sql command
    review_sql_parser = subparsers.add_parser("review-sql", help="Review SQL queries for inefficient patterns")
    review_sql_parser.add_argument("--mybatis-dir", required=True, help="Path to MyBatis/iBatis mapper XML directory")
    review_sql_parser.add_argument("--llm-review", action="store_true",
                                    help="Use LLM for detailed review of top issues")
    review_sql_parser.add_argument("--max-samples", type=int, default=20,
                                    help="Max statements for LLM review (default: 20)")

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

    # analyze-legacy command
    al_parser = subparsers.add_parser(
        "analyze-legacy",
        help="Analyze AS-IS legacy sources (backend + frontend + DB menu)")
    al_group = al_parser.add_mutually_exclusive_group(required=True)
    al_group.add_argument("--backend-dir",
                          help="Single backend project root (recursively scans .java "
                               "+ MyBatis XML; target/build/.git 등 자동 제외)")
    al_group.add_argument("--backends-root",
                          help="Monorepo root containing multiple backend projects. "
                               "각 직계 하위 폴더 중 pom.xml / build.gradle 또는 "
                               "src/main/java 를 가진 것을 backend project 로 인식하여 "
                               "일괄 분석. 결과는 단일 통합 Markdown + Excel 로 생성.")
    al_parser.add_argument("--include-all", action="store_true",
                           help="With --backends-root: include every direct child "
                                "directory regardless of build-file detection")
    al_parser.add_argument("--frontend-dir",
                           help="단일 프론트엔드 프로젝트 루트 (React/Polymer, optional)")
    al_parser.add_argument("--frontends-root",
                           help="여러 프론트엔드 레포가 있는 상위 디렉토리. 각 하위 폴더를 "
                                "개별 프론트엔드 프로젝트로 인식해 URL 맵을 통합. "
                                "--frontend-dir 대신 사용.")
    al_parser.add_argument("--frontend-framework", choices=["auto", "react", "polymer"],
                           default="auto",
                           help="Frontend framework override. 기본 'auto' = package.json 의존성 + "
                                "파일 콘텐츠 샘플링으로 React vs Polymer 자동 감지. 강제하려면 "
                                "'react' 또는 'polymer' 지정.")
    al_parser.add_argument("--menu-table", help="Menu table name (overrides config)")
    al_parser.add_argument("--menu-md",
                           help="Path to a Markdown menu file (pipe table). DRM 환경에서 Excel 대신 사용. "
                                "input/menu_template.md 참조. --menu-xlsx 보다 우선합니다.")
    al_parser.add_argument("--menu-xlsx",
                           help="Path to a project-specific menu Excel file. "
                                "Expected columns: 1레벨 / 2레벨 / 3레벨 / 4레벨 / 5레벨 / URL "
                                "(case-insensitive Korean or English headers, URL row 만 메인 페이지로 인정). "
                                "지정되면 DB 메뉴 테이블 대신 이 파일을 사용합니다.")
    al_parser.add_argument("--skip-menu", action="store_true",
                           help="Skip menu load entirely (menu columns left blank)")
    al_parser.add_argument("--menu-only", action="store_true",
                           help="Program Detail 에 메뉴 매칭된 endpoint 만 표시. "
                                "매칭 안 된 endpoint 는 Unmatched Controllers 섹션으로 분리.")
    al_parser.add_argument("--patterns",
                           help="Path to patterns.yaml (LLM 이 생성한 프로젝트 패턴 파일). "
                                "discover-patterns 로 생성하거나 수동 작성. 지정하면 "
                                "파서/분석기가 해당 패턴으로 커스텀 분석 수행.")
    al_parser.add_argument("--rfc-depth", type=int, default=None,
                           help="Service-of-service walk depth for RFC collection (default 2)")
    al_parser.add_argument("--format", choices=["markdown", "excel", "both"], default="both",
                           help="Output format (default both)")

    # discover-patterns command
    dp_parser = subparsers.add_parser(
        "discover-patterns",
        help="LLM 으로 프로젝트 패턴 자동 발견 (controller/service/DAO/SQL 패턴)")
    dp_parser.add_argument("--backend-dir", required=True,
                           help="Backend project root to analyze")
    dp_parser.add_argument("--output",
                           help="Output YAML path (default: output/legacy_analysis/patterns.yaml)")
    dp_parser.add_argument("--menu-md",
                           help="메뉴 Markdown (URL 관례 학습용). 지정하면 LLM 이 "
                                "url_prefix_strip / app_key 등 URL 섹션도 추출.")
    dp_parser.add_argument("--frontends-root",
                           help="프론트 멀티 레포 루트 (URL 관례 학습용). 하위 "
                                "디렉토리명을 app 후보로, 샘플 라우트를 LLM 에 제공. "
                                "frontend-dir 미지정 시 가장 큰 하위 레포를 "
                                "프론트 패턴 학습 대표로 자동 선택.")
    dp_parser.add_argument("--frontend-dir",
                           help="프론트 패턴 학습용 대표 레포 단일 경로. 29 개 앱 전체를 "
                                "샘플링하면 프롬프트가 너무 커져 LLM 이 JSON 파싱 실패하므로 "
                                "대표 하나만 지정하면 안정적. frontends-root 와 같이 쓰면 "
                                "URL 관례는 root 기준, 프론트 패턴은 이 dir 기준.")

    # convert-menu command
    cm_parser = subparsers.add_parser(
        "convert-menu",
        help="임의 양식의 메뉴 소스 → 표준 menu.md 변환 (LLM 이 헤더 매핑 학습)")
    cm_parser.add_argument("--menu-xlsx",
                           help="변환할 메뉴 Excel 파일 경로 (DRM 없을 때)")
    cm_parser.add_argument("--menu-md-in",
                           help="DRM 우회용: Excel 셀을 복사해 붙여넣은 텍스트 "
                                "파일 (.md / .txt / .tsv). 파이프 테이블·TSV·CSV "
                                "자동 감지")
    cm_parser.add_argument("--output",
                           help="출력 menu.md 경로 (기본: input/menu.md)")
    cm_parser.add_argument("--sheet",
                           help="시트명 (--menu-xlsx 때만, 미지정 시 가장 내용 "
                                "많은 시트 자동 선택)")
    cm_parser.add_argument("--no-llm", action="store_true",
                           help="LLM 호출 없이 헤더 동의어만으로 변환 (폐쇄망/오프라인)")

    # convert-mapping command
    cmap_parser = subparsers.add_parser(
        "convert-mapping",
        help="AS-IS↔TO-BE 컬럼 매핑 .md → column_mapping.yaml 자동 변환 (LLM 이 kind/transform 추론)",
    )
    cmap_parser.add_argument("--mapping-md", required=True,
                             help="AS-IS/TO-BE 매핑이 적힌 .md 파일 (임의 양식)")
    cmap_parser.add_argument("--output",
                             help="출력 YAML 경로 (기본: input/column_mapping.yaml)")
    cmap_parser.add_argument("--no-llm", action="store_true",
                             help="LLM 호출 없이 pipe-table heuristic 만 사용 (폐쇄망/오프라인)")

    # migrate-sql command
    ms_parser = subparsers.add_parser(
        "migrate-sql",
        help="column_mapping.yaml 기반 MyBatis XML → TO-BE 스키마용 변환 + Stage A 검증 + 산출물 생성",
    )
    ms_parser.add_argument("--mybatis-dir", required=True,
                           help="AS-IS MyBatis mapper XML 디렉토리")
    ms_parser.add_argument("--mapping", required=True,
                           help="column_mapping.yaml 경로")
    ms_parser.add_argument("--to-be-schema", required=True,
                           help="TO-BE 스키마 .md (schema 커맨드 결과물)")
    ms_parser.add_argument("--terms-md",
                           help="(선택) terms_dictionary.md — 한글 주석 삽입용")
    ms_parser.add_argument("--output-format", default="excel,xml",
                           help="쉼표 구분 (기본: excel,xml)")
    ms_parser.add_argument("--emit-column-comments", action="store_true",
                           help="변환된 SELECT 컬럼에 /* 한글 */ 주석 삽입 (기본 off)")
    ms_parser.add_argument("--no-xml-preserve-as-is", action="store_true",
                           help="AS-IS 주석 보존 비활성화 (기본 preserve)")
    ms_parser.add_argument("--llm-fallback", action="store_true",
                           help="NEEDS_LLM 상태 statement 를 LLM 으로 보조 변환")
    ms_parser.add_argument("--dry-run", action="store_true",
                           help="XML 파일은 쓰지 않고 리포트만 생성")

    # validate-migration command (Stage B)
    vm_parser = subparsers.add_parser(
        "validate-migration",
        help="변환된 XML 의 TO-BE SQL 을 TO-BE DB 에 parse-only 검증 (Stage B)",
    )
    vm_parser.add_argument("--converted-dir", required=True,
                           help="migrate-sql 이 만든 변환 XML 디렉토리")
    vm_parser.add_argument("--dsn",
                           help="TO-BE Oracle DSN (host:1521/service). "
                                "ORACLE_TOBE_DSN env 로 대체 가능. "
                                "--dry-run 아닐 때 필수")
    vm_parser.add_argument("--user",
                           help="(선택) ORACLE_TOBE_USER → ORACLE_USER env 로 fallback")
    vm_parser.add_argument("--password",
                           help="(선택) ORACLE_TOBE_PASSWORD → ORACLE_PASSWORD env 로 fallback")
    vm_parser.add_argument("--instant-client-dir",
                           help="(선택) thick 모드 client lib 경로 "
                                "(ORACLE_INSTANT_CLIENT_DIR env 로 대체 가능)")
    vm_parser.add_argument("--parallel", type=int, default=10,
                           help="worker thread 수 (기본 10)")
    vm_parser.add_argument("--report",
                           help="출력 Excel 경로 (기본: <converted-dir 상위>/validation_report_TIMESTAMP.xlsx)")
    vm_parser.add_argument("--dry-run", action="store_true",
                           help="DB 접속 skip, statement 수집/리포트만 생성")

    # migration-impact command
    mi_parser = subparsers.add_parser(
        "migration-impact",
        help="SQL Migration 사전 영향분석 (column_mapping.yaml 검증 + AS-IS 쿼리 영향 리포트)",
    )
    mi_parser.add_argument("--mybatis-dir", required=True,
                           help="MyBatis mapper XML 디렉토리")
    mi_parser.add_argument("--mapping", required=True,
                           help="column_mapping.yaml 경로 (input/column_mapping_template.yaml 참고)")
    mi_parser.add_argument("--as-is-schema", required=True,
                           help="AS-IS 스키마 .md (schema 커맨드 결과물)")
    mi_parser.add_argument("--to-be-schema",
                           help="(선택) TO-BE 스키마 .md — 지정 시 매핑 타겟 검증")
    mi_parser.add_argument("--output",
                           help="출력 Excel 경로 (기본: output/sql_migration/impact_report_TIMESTAMP.xlsx)")

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
    elif args.command == "audit-standards":
        cmd_audit_standards(args)
    elif args.command == "gen-ddl":
        cmd_gen_ddl(args)
    elif args.command == "validate-naming":
        cmd_validate_naming(args)
    elif args.command == "review-sql":
        cmd_review_sql(args)
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
    elif args.command == "analyze-legacy":
        cmd_analyze_legacy(args)
    elif args.command == "discover-patterns":
        cmd_discover_patterns(args)
    elif args.command == "convert-menu":
        cmd_convert_menu(args)
    elif args.command == "migration-impact":
        cmd_migration_impact(args)
    elif args.command == "convert-mapping":
        cmd_convert_mapping(args)
    elif args.command == "migrate-sql":
        cmd_migrate_sql(args)
    elif args.command == "validate-migration":
        cmd_validate_migration(args)
    elif args.command == "all":
        cmd_all(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
