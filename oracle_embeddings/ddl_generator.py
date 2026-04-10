import json
import logging
import os
import re
import time
from datetime import datetime

logger = logging.getLogger(__name__)


def generate_ddl(request: str, config: dict,
                 terms_md: str = None, schema_md: str = None,
                 sample_tables: int = 5) -> dict:
    """Generate DDL from natural language request using LLM.

    request: 자연어 요청 (예: "고객 주문 이력 테이블 만들어줘")
    terms_md: 용어사전 .md 경로
    schema_md: 기존 스키마 .md 경로 (벤치마킹용)
    sample_tables: 참고할 기존 테이블 샘플 수
    """
    from openai import OpenAI

    llm_config = config.get("llm", {})
    api_key = os.environ.get("LLM_API_KEY") or llm_config.get("api_key", "ollama")
    api_base = os.environ.get("LLM_API_BASE") or llm_config.get("api_base", "http://localhost:11434/v1")
    client = OpenAI(api_key=api_key, base_url=api_base)
    model = os.environ.get("LLM_MODEL") or llm_config.get("model", "llama3")

    # Build context
    context_parts = []

    # 1. Terms dictionary (abbreviations)
    if terms_md and os.path.exists(terms_md):
        terms_context = _load_terms_context(terms_md)
        if terms_context:
            context_parts.append("## 표준 약어 사전\n" + terms_context)

    # 2. Sample schema (for style reference)
    if schema_md and os.path.exists(schema_md):
        schema_samples = _load_schema_samples(schema_md, sample_tables)
        if schema_samples:
            context_parts.append("## 기존 테이블 예시 (스타일 참고)\n" + schema_samples)

    context = "\n\n".join(context_parts) if context_parts else "(참고 자료 없음)"

    prompt = f"""사용자 요청에 따라 Oracle CREATE TABLE DDL을 생성해주세요.

## 사용자 요청
{request}

## 참고 자료
{context}

## 규칙
1. 테이블명은 TB_ 접두어 사용 (예: TB_CUSTOMER_ORDER)
2. 컬럼명은 대문자 + 언더스코어 (SNAKE_CASE)
3. 표준 약어 사전에 있는 약어를 우선 사용 (CUST, ORD, DT, NM 등)
4. 모든 테이블에 PK 필수 (테이블명_ID 형태)
5. 등록일자(REG_DT), 수정일자(MOD_DT), 등록자(REG_USER_ID) 공통 컬럼 포함
6. 적절한 데이터타입과 길이 지정
7. NOT NULL 제약 적절히 적용
8. 컬럼에 COMMENT 추가

## 응답 형식 (JSON)
{{
  "table_name": "테이블 물리명",
  "table_comment": "테이블 한글 설명",
  "ddl": "CREATE TABLE ... 전체 DDL 코드",
  "explanation": "생성 근거 설명",
  "columns": [
    {{"name": "COLUMN_NAME", "type": "VARCHAR2(100)", "nullable": "N", "comment": "한글 설명"}}
  ]
}}

JSON만 응답하세요. 설명 없이 JSON만 출력하세요."""

    result = _call_llm_json(client, model, prompt)

    if not result:
        return {"error": "LLM 호출 실패 또는 JSON 파싱 실패"}

    # Append standard audit columns to DDL if missing
    ddl = result.get("ddl", "")
    if ddl and "REG_DT" not in ddl.upper():
        logger.warning("LLM이 공통 컬럼(REG_DT)을 생성하지 않았습니다.")

    return result


def _load_terms_context(terms_md: str) -> str:
    """용어사전 .md에서 약어 목록 추출."""
    with open(terms_md, "r", encoding="utf-8") as f:
        content = f.read()
    content = content.replace("\r\n", "\n")

    abbrs = []
    # | Word | Abbreviation | English Full | Korean | ...
    pattern = r'^\|\s*(\w+)\s*\|\s*(\w+)\s*\|\s*(\w+)\s*\|\s*(\S+)?\s*\|'
    for match in re.finditer(pattern, content, re.MULTILINE):
        word, abbr, full, korean = match.group(1), match.group(2), match.group(3), match.group(4)
        if word in ("Word", "--------"):
            continue
        if abbr and full and korean:
            abbrs.append(f"  {abbr} = {full} ({korean})")

    if not abbrs:
        return ""

    # 최대 50개까지만
    return "\n".join(abbrs[:50])


def _load_schema_samples(schema_md: str, limit: int) -> str:
    """기존 스키마에서 테이블 샘플 추출."""
    from .md_parser import parse_schema_md

    try:
        schema = parse_schema_md(schema_md)
    except Exception:
        return ""

    samples = []
    for table in schema.get("tables", [])[:limit]:
        cols = []
        for col in table["columns"][:10]:
            cols.append(f"    {col['column_name']} {col['data_type']}")
        if not cols:
            continue
        table_str = f"  {table['name']}:\n" + "\n".join(cols)
        samples.append(table_str)

    return "\n\n".join(samples)


def _call_llm_json(client, model: str, prompt: str, max_retries: int = 2) -> dict:
    """Call LLM and parse JSON response."""
    for attempt in range(max_retries + 1):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "당신은 Oracle DB DA 전문가입니다. 사용자 요청에 따라 표준을 준수하는 DDL을 생성합니다. 반드시 유효한 JSON만 응답하세요."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.2,
                timeout=180,
            )
            text = response.choices[0].message.content.strip()

            json_match = re.search(r'```(?:json)?\s*([\s\S]*?)```', text)
            if json_match:
                text = json_match.group(1).strip()

            return json.loads(text)
        except json.JSONDecodeError as e:
            wait = 2 ** (attempt + 1)
            if attempt < max_retries:
                logger.warning("LLM returned invalid JSON (attempt %d), retrying in %ds...", attempt + 1, wait)
                time.sleep(wait)
            else:
                logger.error("Failed to parse LLM response: %s", e)
        except Exception as e:
            logger.error("LLM call failed: %s", e)
            break

    return {}


def save_ddl(result: dict, output_dir: str) -> str:
    """Save generated DDL to file."""
    os.makedirs(output_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    table_name = result.get("table_name", "generated")
    filepath = os.path.join(output_dir, f"ddl_{table_name}_{timestamp}.sql")

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(f"-- Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"-- Table: {result.get('table_name', '')}\n")
        f.write(f"-- Comment: {result.get('table_comment', '')}\n")
        f.write(f"-- Explanation: {result.get('explanation', '')}\n")
        f.write("\n")
        f.write(result.get("ddl", ""))
        f.write("\n")

    return filepath
