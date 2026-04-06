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
    """Search vector DB to gather relevant schema and query context.

    Multi-pass retrieval:
    1. JOIN 관계 정보 (모든 관계 또는 대상 테이블 관련)
    2. 테이블 스키마 (overview + 컬럼 그룹 전부)
    3. 제약조건 (PK/FK/Index)
    4. 쿼리 상세 (JOIN이 있는 쿼리)
    """
    context_parts = []
    seen_texts = set()

    def _add(label: str, results: list[dict]):
        for r in results:
            text_key = r["text"][:200]  # dedup by first 200 chars
            if text_key not in seen_texts:
                seen_texts.add(text_key)
                context_parts.append(f"[{label}]\n{r['text']}")

    # 1. JOIN 관계 정보
    _add("JOIN RELATIONSHIPS", search(
        "테이블 간 JOIN 관계 relationship foreign key",
        config, db_path, n_results=10, collections=["queries"],
    ))

    # 2. 테이블 스키마 - 대상 테이블별로 모든 청크(overview + columns + constraints) 수집
    if target_tables:
        for table in target_tables:
            # 테이블 개요
            _add(f"SCHEMA: {table}", search(
                f"{table} table schema columns PRIMARY KEY",
                config, db_path, n_results=5, collections=["schema"],
            ))
            # 컬럼 상세 (컬럼이 많은 테이블은 여러 청크)
            _add(f"COLUMNS: {table}", search(
                f"{table} column type nullable default",
                config, db_path, n_results=5, collections=["schema"],
            ))
    else:
        # 전체 스키마 개요
        _add("SCHEMA", search(
            "테이블 컬럼 데이터타입 PRIMARY KEY NOT NULL",
            config, db_path, n_results=20, collections=["schema"],
        ))
        # 제약조건
        _add("CONSTRAINTS", search(
            "Primary Key Foreign Key Index constraint",
            config, db_path, n_results=10, collections=["schema"],
        ))

    # 3. 테이블 사용 통계
    _add("TABLE USAGE", search(
        "테이블 사용 통계 SELECT INSERT UPDATE DELETE",
        config, db_path, n_results=3, collections=["queries"],
    ))

    # 4. 쿼리 상세 (JOIN 컨텍스트)
    if target_tables:
        for table in target_tables:
            _add(f"QUERY: {table}", search(
                f"{table} SELECT JOIN FROM",
                config, db_path, n_results=3, collections=["queries"],
            ))
    else:
        _add("QUERY DETAIL", search(
            "SELECT JOIN FROM WHERE",
            config, db_path, n_results=5, collections=["queries"],
        ))

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
