import json
import logging
import os
import re
import time

logger = logging.getLogger(__name__)


def get_llm_client(config: dict):
    """Create an OpenAI-compatible client for local LLM."""
    from openai import OpenAI

    llm_config = config.get("llm", {})
    api_key = os.environ.get("LLM_API_KEY") or llm_config.get("api_key", "ollama")
    api_base = os.environ.get("LLM_API_BASE") or llm_config.get("api_base", "http://localhost:11434/v1")
    return OpenAI(api_key=api_key, base_url=api_base)


def assist_erd(schema: dict, joins: list[dict], config: dict) -> dict:
    """Use local LLM to enrich ERD with descriptions, groups, and inferred relations."""
    client = get_llm_client(config)
    model = config.get("llm", {}).get("model", "llama3")

    result = {
        "descriptions": {},
        "table_descriptions": {},
        "domain_groups": {},
        "inferred_relations": [],
    }

    # 1. Generate column descriptions
    logger.info("LLM: Generating column descriptions...")
    descriptions = _generate_descriptions(client, model, schema)
    result["descriptions"] = descriptions.get("columns", {})
    result["table_descriptions"] = descriptions.get("tables", {})

    # 2. Infer missing relationships by column name similarity
    logger.info("LLM: Inferring missing relationships...")
    existing_pairs = {(j["table1"], j["table2"]) for j in joins}
    existing_pairs.update({(j["table2"], j["table1"]) for j in joins})
    inferred = _infer_missing_relations(client, model, schema, existing_pairs)
    result["inferred_relations"] = inferred

    # 3. Group tables by domain
    logger.info("LLM: Grouping tables by domain...")
    groups = _group_by_domain(client, model, schema)
    result["domain_groups"] = groups

    return result


def _generate_descriptions(client, model: str, schema: dict) -> dict:
    """Ask LLM to interpret column names and generate Korean descriptions."""
    tables_summary = []
    for t in schema.get("tables", []):
        cols = [f"{c['column_name']}({c['data_type']})" for c in t["columns"]]
        tables_summary.append(f"Table: {t['name']}, Columns: {', '.join(cols)}")

    prompt = f"""다음은 Oracle DB 테이블/컬럼 목록입니다. 약어가 많으니 각 테이블과 컬럼의 의미를 한국어로 추론해주세요.

{chr(10).join(tables_summary)}

JSON 형식으로만 응답하세요:
{{
  "tables": {{
    "테이블명": "테이블 설명(한국어)",
    ...
  }},
  "columns": {{
    "테이블명.컬럼명": "컬럼 설명(한국어)",
    ...
  }}
}}"""

    return _call_llm_json(client, model, prompt, default={"tables": {}, "columns": {}})


def _infer_missing_relations(client, model: str, schema: dict,
                              existing_pairs: set) -> list[dict]:
    """Ask LLM to find potential relationships not captured in existing JOINs."""
    tables_info = []
    for t in schema.get("tables", []):
        pk = t.get("primary_keys", [])
        cols = [c["column_name"] for c in t["columns"]]
        tables_info.append(f"Table: {t['name']}, PK: {pk}, Columns: {cols}")

    existing_list = [f"{t1} <-> {t2}" for t1, t2 in existing_pairs]

    prompt = f"""다음 Oracle 테이블 목록에서 컬럼명 유사도를 기반으로 누락된 테이블 간 관계를 찾아주세요.
예: 두 테이블에 같은 이름의 컬럼(CUSTOMER_ID 등)이 있으면 관계가 있을 가능성이 높습니다.

{chr(10).join(tables_info)}

이미 발견된 관계 (제외):
{chr(10).join(existing_list) if existing_list else "없음"}

새로 추론한 관계만 JSON 배열로 응답하세요:
[
  {{"table1": "TABLE_A", "column1": "COL_X", "table2": "TABLE_B", "column2": "COL_Y", "reason": "이유"}},
  ...
]

관계가 없으면 빈 배열 []을 반환하세요."""

    result = _call_llm_json(client, model, prompt, default=[])
    if isinstance(result, list):
        # Validate and clean
        cleaned = []
        for r in result:
            if all(k in r for k in ("table1", "column1", "table2", "column2")):
                pair = (r["table1"], r["table2"])
                reverse = (r["table2"], r["table1"])
                if pair not in existing_pairs and reverse not in existing_pairs:
                    cleaned.append({
                        "table1": r["table1"].upper(),
                        "column1": r["column1"].upper(),
                        "table2": r["table2"].upper(),
                        "column2": r["column2"].upper(),
                        "join_type": "INFERRED",
                        "source_mapper": "LLM",
                        "source_id": r.get("reason", "column name similarity"),
                    })
        return cleaned
    return []


def _group_by_domain(client, model: str, schema: dict) -> dict:
    """Ask LLM to group tables into business domains."""
    table_names = [t["name"] for t in schema.get("tables", [])]

    prompt = f"""다음 Oracle 테이블 목록을 업무 도메인(비즈니스 영역)별로 분류해주세요.

테이블 목록:
{chr(10).join(table_names)}

JSON 형식으로만 응답하세요:
{{
  "도메인명(한국어)": ["TABLE1", "TABLE2", ...],
  ...
}}"""

    result = _call_llm_json(client, model, prompt, default={})
    if isinstance(result, dict):
        return result
    return {}


def _call_llm_json(client, model: str, prompt: str, default=None,
                    max_retries: int = 2):
    """Call LLM and parse JSON response."""
    for attempt in range(max_retries + 1):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "당신은 Oracle DB 스키마 분석 전문가입니다. 반드시 유효한 JSON만 응답하세요. 설명이나 부가 텍스트 없이 JSON만 출력하세요."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
            )
            text = response.choices[0].message.content.strip()

            # Extract JSON from possible markdown code block
            json_match = re.search(r'```(?:json)?\s*([\s\S]*?)```', text)
            if json_match:
                text = json_match.group(1).strip()

            return json.loads(text)
        except json.JSONDecodeError:
            logger.warning("LLM returned invalid JSON (attempt %d/%d)", attempt + 1, max_retries + 1)
            if attempt < max_retries:
                time.sleep(1)
        except Exception as e:
            logger.error("LLM call failed: %s", e)
            break

    logger.warning("Falling back to default value")
    return default
