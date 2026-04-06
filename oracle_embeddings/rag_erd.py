import logging
import os
import re

from .vector_store import search

logger = logging.getLogger(__name__)


def generate_erd_with_rag(config: dict, db_path: str = "./vectordb",
                          output_dir: str = "./output",
                          target_tables: list[str] = None) -> str:
    """Generate Mermaid ERD using RAG: vector search + LLM generation."""
    from openai import OpenAI

    llm_config = config.get("llm", {})
    llm_client = OpenAI(
        api_key=os.environ.get("LLM_API_KEY") or llm_config.get("api_key", "ollama"),
        base_url=llm_config.get("api_base", "http://localhost:11434/v1"),
    )
    model = llm_config.get("model", "llama3")

    # Step 1: Gather context via RAG
    logger.info("Gathering context from vector DB...")
    context = _gather_context(config, db_path, target_tables)
    logger.info("Context gathered: %d characters", len(context))

    # Step 2: Generate ERD via LLM
    logger.info("Generating Mermaid ERD via LLM...")
    mermaid_code = _generate_erd_llm(llm_client, model, context, target_tables)

    # Step 3: Build output markdown
    erd_md = _build_output(mermaid_code, target_tables)

    # Step 4: Save
    os.makedirs(output_dir, exist_ok=True)
    from datetime import datetime
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    scope = "_".join(target_tables[:3]) if target_tables else "full"
    filepath = os.path.join(output_dir, f"erd_rag_{scope}_{timestamp}.md")

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(erd_md)

    logger.info("RAG ERD exported: %s", filepath)
    return filepath


def _gather_context(config: dict, db_path: str,
                    target_tables: list[str] = None) -> str:
    """Search vector DB to gather relevant schema and query context."""
    context_parts = []

    # Query 1: Get all relationship info
    rel_results = search(
        "테이블 간 JOIN 관계 relationship foreign key",
        config, db_path, n_results=5,
        collections=["queries"],
    )
    for r in rel_results:
        context_parts.append(f"[QUERY RELATIONSHIP]\n{r['text']}")

    # Query 2: Get table usage info
    usage_results = search(
        "테이블 사용 통계 SELECT INSERT UPDATE DELETE",
        config, db_path, n_results=3,
        collections=["queries"],
    )
    for r in usage_results:
        if r["text"] not in [p.split("\n", 1)[-1] for p in context_parts]:
            context_parts.append(f"[TABLE USAGE]\n{r['text']}")

    # Query 3: Get specific table schemas
    if target_tables:
        for table in target_tables:
            table_results = search(
                f"{table} 테이블 컬럼 스키마 PRIMARY KEY",
                config, db_path, n_results=2,
                collections=["schema"],
            )
            for r in table_results:
                context_parts.append(f"[SCHEMA: {table}]\n{r['text']}")
    else:
        # Get all schema info
        schema_results = search(
            "테이블 컬럼 데이터타입 PRIMARY KEY NOT NULL",
            config, db_path, n_results=15,
            collections=["schema"],
        )
        for r in schema_results:
            context_parts.append(f"[SCHEMA]\n{r['text']}")

    # Query 4: Get query details for JOIN context
    query_results = search(
        "SELECT JOIN FROM WHERE",
        config, db_path, n_results=5,
        collections=["queries"],
    )
    for r in query_results:
        if r["text"] not in [p.split("\n", 1)[-1] for p in context_parts]:
            context_parts.append(f"[QUERY DETAIL]\n{r['text']}")

    return "\n\n---\n\n".join(context_parts)


def _generate_erd_llm(client, model: str, context: str,
                      target_tables: list[str] = None) -> str:
    """Ask LLM to generate Mermaid ERD code from RAG context."""
    table_scope = ""
    if target_tables:
        table_scope = f"\n\n대상 테이블: {', '.join(target_tables)} 과 이들과 관련된 테이블들"

    prompt = f"""다음은 Oracle DB의 스키마 정보와 쿼리 분석(JOIN 관계) 결과입니다.
이 정보를 기반으로 Mermaid erDiagram 코드를 생성해주세요.{table_scope}

## 요구사항
1. 모든 테이블을 포함하고, 각 테이블의 주요 컬럼(PK, FK, 중요 컬럼)을 포함
2. JOIN 분석에서 발견된 관계를 모두 반영
3. 컬럼명이 같은 테이블 간의 누락된 관계도 추론해서 포함
4. PK 컬럼은 PK로, FK 역할 컬럼은 FK로 표시
5. 관계 카디널리티(1:1, 1:N, N:M)를 PK/FK 기반으로 추론
6. 한국어 코멘트를 컬럼 설명으로 추가 (약어 해석: CUST_NO→고객번호, ORD_DT→주문일자 등)

## 참고 데이터

{context}

## 출력 형식
반드시 아래 형식의 Mermaid erDiagram 코드만 출력하세요. 설명 텍스트 없이 코드만 출력하세요.

erDiagram
    TABLE_NAME {{
        TYPE COLUMN_NAME PK "설명"
        TYPE COLUMN_NAME FK "설명"
    }}
    TABLE_A ||--o{{ TABLE_B : "COLUMN = COLUMN"
"""

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "당신은 Oracle DB ERD 전문가입니다. Mermaid erDiagram 코드만 생성하세요. 부가 설명 없이 코드만 출력하세요."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.1,
    )

    text = response.choices[0].message.content.strip()

    # Extract mermaid code block if wrapped
    code_match = re.search(r'```(?:mermaid)?\s*(erDiagram[\s\S]*?)```', text)
    if code_match:
        return code_match.group(1).strip()

    # If it starts with erDiagram, use as-is
    if text.startswith("erDiagram"):
        return text

    # Fallback: try to find erDiagram anywhere
    erd_match = re.search(r'(erDiagram[\s\S]+)', text)
    if erd_match:
        return erd_match.group(1).strip()

    logger.warning("LLM did not return valid Mermaid code, returning raw output")
    return text


def _build_output(mermaid_code: str, target_tables: list[str] = None) -> str:
    """Build the output markdown document."""
    lines = []
    lines.append("# ERD (RAG-Generated)\n")
    lines.append("로컬 임베딩 모델 + 벡터 DB(ChromaDB) + 로컬 LLM을 활용한 RAG 기반 ERD입니다.\n")

    if target_tables:
        lines.append(f"대상 테이블: {', '.join(target_tables)}\n")

    lines.append("")
    lines.append("## Mermaid ERD\n")
    lines.append("```mermaid")
    lines.append(mermaid_code)
    lines.append("```\n")

    lines.append("## 렌더링 방법\n")
    lines.append("- **VS Code**: Mermaid 확장 설치 후 미리보기")
    lines.append("- **mermaid-cli**: `mmdc -i erd.md -o erd.png`")
    lines.append("- **Msty**: 코드 블록 붙여넣기\n")

    return "\n".join(lines)
