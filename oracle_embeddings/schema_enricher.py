import json
import logging
import os
import re
import time

logger = logging.getLogger(__name__)

BATCH_TABLES = 5  # LLM에 한번에 보내는 테이블 수


def enrich_schema(schema: dict, config: dict) -> dict:
    """Enrich schema with LLM-generated comments for empty descriptions."""
    from openai import OpenAI

    llm_config = config.get("llm", {})
    api_key = os.environ.get("LLM_API_KEY") or llm_config.get("api_key", "ollama")
    api_base = os.environ.get("LLM_API_BASE") or llm_config.get("api_base", "http://localhost:11434/v1")
    client = OpenAI(api_key=api_key, base_url=api_base)
    model = os.environ.get("LLM_MODEL") or llm_config.get("model", "llama3")

    # Collect tables that need enrichment
    tables_to_enrich = []
    for table in schema["tables"]:
        empty_cols = [c for c in table["columns"] if not c.get("comment")]
        if empty_cols or not table.get("comment"):
            tables_to_enrich.append(table)

    if not tables_to_enrich:
        print("  All tables/columns already have comments. Nothing to enrich.")
        return schema

    total = len(tables_to_enrich)
    enriched_count = 0
    col_enriched = 0
    table_enriched = 0

    print(f"  Tables needing enrichment: {total}")
    print(f"  LLM model: {model}")

    # Process in batches
    for i in range(0, total, BATCH_TABLES):
        batch = tables_to_enrich[i:i + BATCH_TABLES]
        batch_num = i // BATCH_TABLES + 1
        total_batches = (total + BATCH_TABLES - 1) // BATCH_TABLES

        result = _enrich_batch(client, model, batch)

        if result:
            for table in batch:
                table_name = table["name"]
                table_result = result.get(table_name, {})

                # Enrich table comment
                if not table.get("comment") and table_result.get("table_comment"):
                    table["comment"] = table_result["table_comment"]
                    table_enriched += 1

                # Enrich column comments
                col_comments = table_result.get("columns", {})
                for col in table["columns"]:
                    if not col.get("comment") and col["column_name"] in col_comments:
                        col["comment"] = col_comments[col["column_name"]]
                        col_enriched += 1

            enriched_count += len(batch)

        if batch_num % 5 == 0 or batch_num == total_batches:
            print(f"  [{batch_num}/{total_batches}] {enriched_count}/{total} tables processed "
                  f"(tables: {table_enriched}, columns: {col_enriched} enriched)")

    print(f"  Enrichment complete: {table_enriched} table comments, {col_enriched} column comments added")
    return schema


def _enrich_batch(client, model: str, tables: list[dict], max_retries: int = 2) -> dict:
    """Ask LLM to generate comments for a batch of tables."""
    # Build table info for prompt
    table_info_parts = []
    for table in tables:
        cols_text = []
        for c in table["columns"]:
            has_comment = "O" if c.get("comment") else "X"
            existing = f' (기존: {c["comment"]})' if c.get("comment") else ""
            cols_text.append(f"    {c['column_name']} {c['data_type']}{existing}")

        table_comment_status = f' (기존: {table["comment"]})' if table.get("comment") else " (없음)"
        pk = ", ".join(table.get("primary_keys", []))
        pk_text = f"  PK: {pk}" if pk else ""

        table_info_parts.append(
            f"TABLE: {table['name']}{table_comment_status}{pk_text}\n" +
            "\n".join(cols_text)
        )

    tables_text = "\n\n".join(table_info_parts)

    prompt = f"""다음 Oracle 테이블과 컬럼 정보를 보고, 비어있는 코멘트를 한국어로 생성해주세요.

## 규칙
1. 테이블명과 컬럼명의 약어를 해석하세요 (예: CUST→고객, ORD→주문, DT→일자, NO→번호, CD→코드, NM→명, ST→상태, AMT→금액, QTY→수량, YN→여부, SEQ→순번, REG→등록, MOD→수정, DEL→삭제)
2. 이미 코멘트가 있는 컬럼은 기존 코멘트를 유지하세요
3. 코멘트는 간결하게 (2~10자) 작성하세요 (예: "고객번호", "주문일자", "배송상태코드")
4. 테이블 코멘트가 없으면 테이블의 역할을 추론해서 작성하세요

## 테이블 정보

{tables_text}

## 응답 형식
반드시 아래 JSON 형식으로만 응답하세요. 설명 없이 JSON만 출력하세요.

{{
  "TABLE_NAME": {{
    "table_comment": "테이블 설명",
    "columns": {{
      "COL1": "컬럼 설명",
      "COL2": "컬럼 설명"
    }}
  }}
}}"""

    for attempt in range(max_retries + 1):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "당신은 Oracle DB 스키마 전문가입니다. 테이블/컬럼 약어를 정확히 해석하여 한국어 코멘트를 생성합니다. 반드시 유효한 JSON만 응답하세요."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
            )
            text = response.choices[0].message.content.strip()

            # Extract JSON from markdown code block
            json_match = re.search(r'```(?:json)?\s*([\s\S]*?)```', text)
            if json_match:
                text = json_match.group(1).strip()

            return json.loads(text)
        except json.JSONDecodeError:
            if attempt < max_retries:
                logger.warning("LLM returned invalid JSON (attempt %d), retrying...", attempt + 1)
                time.sleep(1)
            else:
                logger.error("Failed to parse LLM response after %d attempts", max_retries + 1)
        except Exception as e:
            logger.error("LLM call failed: %s", e)
            break

    return {}


def save_enriched_schema_md(schema: dict, output_dir: str) -> str:
    """Save enriched schema as a new .md file."""
    from datetime import datetime

    os.makedirs(output_dir, exist_ok=True)
    owner = schema.get("owner", "UNKNOWN")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = os.path.join(output_dir, f"{owner}_schema_enriched_{timestamp}.md")

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(f"# {owner} Database Schema (Enriched)\n\n")
        f.write(f"Total tables: {len(schema['tables'])}\n\n")
        f.write("---\n\n")

        for table in schema["tables"]:
            _write_enriched_table(f, table)

    return filepath


def _write_enriched_table(f, table: dict):
    """Write a single table section with enriched comments."""
    f.write(f"## {table['name']}\n\n")

    if table.get("comment"):
        f.write(f"> {table['comment']}\n\n")

    f.write("| Column | Type | Nullable | Default | Description |\n")
    f.write("|--------|------|----------|---------|-------------|\n")
    for col in table["columns"]:
        pk_mark = ""
        if col["column_name"] in table.get("primary_keys", []):
            pk_mark = " (PK)"
        nullable = "Y" if col["nullable"] == "Y" else "N"
        default = col.get("data_default") or ""
        comment = col.get("comment") or ""
        f.write(f"| {col['column_name']}{pk_mark} | {col['data_type']} | {nullable} | {default} | {comment} |\n")

    f.write("\n")

    if table.get("primary_keys"):
        f.write(f"**Primary Key**: {', '.join(table['primary_keys'])}\n\n")

    if table.get("foreign_keys"):
        f.write("**Foreign Keys**:\n")
        for fk in table["foreign_keys"]:
            f.write(f"- `{fk['column']}` -> `{fk['ref_table']}.{fk['ref_column']}` ({fk.get('constraint_name', '')})\n")
        f.write("\n")

    if table.get("indexes"):
        f.write("**Indexes**:\n")
        for idx in table["indexes"]:
            unique = "UNIQUE " if idx.get("unique") else ""
            f.write(f"- {unique}`{idx['name']}` ({', '.join(idx['columns'])})\n")
        f.write("\n")

    f.write("---\n\n")
