import json
import logging
import os
import re
import time

logger = logging.getLogger(__name__)

BATCH_SIZE = 30  # LLM에 한번에 보내는 단어 수


def enrich_terms(words: list[dict], config: dict) -> list[dict]:
    """Use LLM to generate abbreviation, full English name, and Korean name for each word."""
    from openai import OpenAI

    llm_config = config.get("llm", {})
    api_key = os.environ.get("LLM_API_KEY") or llm_config.get("api_key", "ollama")
    api_base = os.environ.get("LLM_API_BASE") or llm_config.get("api_base", "http://localhost:11434/v1")
    client = OpenAI(api_key=api_key, base_url=api_base)
    model = os.environ.get("LLM_MODEL") or llm_config.get("model", "llama3")

    total = len(words)
    total_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE
    enriched = 0

    print(f"  LLM model: {model}")
    print(f"  Words to process: {total} ({total_batches} batches)")

    for i in range(0, total, BATCH_SIZE):
        batch = words[i:i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1

        result = _enrich_batch(client, model, batch)

        if result:
            for w in batch:
                word = w["word"]
                if word in result:
                    r = result[word]
                    w["abbreviation"] = r.get("abbreviation", "")
                    w["english_full"] = r.get("english_full", "")
                    w["korean"] = r.get("korean", "")
                    w["definition"] = r.get("definition", "")
                    if w["korean"]:
                        enriched += 1

        if batch_num % 5 == 0 or batch_num == total_batches:
            print(f"  [{batch_num}/{total_batches}] {enriched}/{total} words enriched")

    print(f"  Enrichment complete: {enriched} words with Korean meaning")
    return words


def _enrich_batch(client, model: str, words: list[dict], max_retries: int = 2) -> dict:
    """Ask LLM to interpret a batch of words."""
    word_list = "\n".join(
        f"  {w['word']} (DB:{w['db_count']}, FE:{w['fe_count']})"
        for w in words
    )

    prompt = f"""다음은 Oracle DB 컬럼명과 React 소스코드에서 수집한 단어 목록입니다.
각 단어에 대해 약어, 영문 Full Name, 한글명, 한글 정의를 생성해주세요.

## 규칙
1. DB 컬럼 약어 해석: CUST→CUSTOMER→고객, ORD→ORDER→주문, DT→DATE→일자, AMT→AMOUNT→금액
2. React 변수 단어: 이미 영문 Full Name인 경우가 많음 (customer, order 등)
3. 약어(Abbreviation)는 표준 DB 약어를 제안 (2~5자)
4. 확신할 수 없는 단어는 모든 필드를 빈 문자열 ""로 두세요
5. 의미가 명확한 것만 작성하세요
6. 정의(Definition)는 해당 용어가 업무에서 어떤 의미인지 한글로 1~2문장 (50자 내외) 으로 설명. 확신 없으면 빈 문자열.

## 단어 목록 (DB출현/FE출현 횟수)
{word_list}

## 응답 형식
JSON만 응답하세요:
{{
  "WORD": {{
    "abbreviation": "약어 (2~5자)",
    "english_full": "영문 Full Name",
    "korean": "한글명",
    "definition": "한글 정의 (1~2문장)"
  }}
}}"""

    for attempt in range(max_retries + 1):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "당신은 데이터 표준화 전문가입니다. DB 컬럼 약어와 프론트엔드 변수명을 정확히 해석합니다. 유효한 JSON만 응답하세요."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
                timeout=120,
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
            else:
                logger.error("Failed to parse LLM response")
        except Exception as e:
            logger.error("LLM call failed: %s", e)
            break

    return {}
