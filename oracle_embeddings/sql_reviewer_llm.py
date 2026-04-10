import json
import logging
import os
import re
import time

logger = logging.getLogger(__name__)


def llm_review_statement(client, model: str, stmt: dict,
                          static_findings: list[dict],
                          max_retries: int = 2) -> dict:
    """Ask LLM to review a single SQL statement and suggest improvements."""
    findings_text = ""
    if static_findings:
        findings_text = "\n".join(
            f"- [{f['severity']}] {f['pattern_name']}: {f['description']}"
            for f in static_findings
        )

    prompt = f"""다음 Oracle SQL 쿼리를 리뷰해주세요. 정적 분석에서 발견된 패턴도 참고하세요.

## SQL
```sql
{stmt['sql']}
```

## 정적 분석 결과
{findings_text or "없음"}

## 요청사항
1. 성능상 문제점을 찾아주세요
2. 가독성/유지보수 문제도 지적해주세요
3. 개선된 SQL을 제안해주세요

## 응답 형식 (JSON)
{{
  "severity": "CRITICAL | HIGH | MEDIUM | LOW",
  "issues": ["문제점 1", "문제점 2"],
  "improved_sql": "개선된 SQL 코드",
  "explanation": "개선 이유 설명"
}}

JSON만 응답하세요."""

    for attempt in range(max_retries + 1):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "당신은 Oracle DBA이자 SQL 튜닝 전문가입니다. 성능과 가독성 관점에서 SQL을 리뷰하고 개선안을 제시합니다. 유효한 JSON만 응답하세요."},
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
        except json.JSONDecodeError:
            wait = 2 ** (attempt + 1)
            if attempt < max_retries:
                logger.warning("LLM returned invalid JSON (attempt %d), retrying in %ds...", attempt + 1, wait)
                time.sleep(wait)
        except Exception as e:
            logger.error("LLM call failed: %s", e)
            break

    return {}


def llm_review_batch(statements_with_findings: list[dict], config: dict,
                     max_samples: int = 20) -> list[dict]:
    """Review critical statements with LLM (limited to max_samples for cost control)."""
    from openai import OpenAI

    llm_config = config.get("llm", {})
    api_key = os.environ.get("LLM_API_KEY") or llm_config.get("api_key", "ollama")
    api_base = os.environ.get("LLM_API_BASE") or llm_config.get("api_base", "http://localhost:11434/v1")
    client = OpenAI(api_key=api_key, base_url=api_base)
    model = os.environ.get("LLM_MODEL") or llm_config.get("model", "llama3")

    # Prioritize statements with higher severity
    severity_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    def _max_severity(stmt_data):
        if not stmt_data["findings"]:
            return 99
        return min(severity_order.get(f["severity"], 99) for f in stmt_data["findings"])

    sorted_stmts = sorted(statements_with_findings, key=_max_severity)[:max_samples]

    print(f"  LLM model: {model}")
    print(f"  Reviewing top {len(sorted_stmts)} statements")

    reviewed = []
    for i, stmt_data in enumerate(sorted_stmts, 1):
        print(f"  [{i}/{len(sorted_stmts)}] {stmt_data['mapper']}#{stmt_data['stmt_id']}")

        review = llm_review_statement(client, model, {
            "sql": stmt_data["sql"],
            "mapper": stmt_data["mapper"],
            "id": stmt_data["stmt_id"],
            "type": stmt_data["stmt_type"],
        }, stmt_data["findings"])

        if review:
            reviewed.append({
                "mapper": stmt_data["mapper"],
                "stmt_id": stmt_data["stmt_id"],
                "stmt_type": stmt_data["stmt_type"],
                "sql": stmt_data["sql"],
                "static_findings": stmt_data["findings"],
                "llm_review": review,
            })

    return reviewed
