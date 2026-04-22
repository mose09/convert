"""Morpheme analysis — LLM-based attribute name tokenization.

속성명 리스트를 LLM 으로 형태소(단어) 단위로 분해한다. 배치 크기는
평균 속성 길이에 따라 자동 조정되고, JSON 파싱 실패 시 배치를 절반으로
축소해 최대 2회 재시도한다.

주요 API:
- ``load_attributes_txt(path)`` — txt 입력 (줄당 1속성, 공백/중복 제거)
- ``load_guide(path)`` — 지침 md 텍스트 읽기 (D3: 필수 경로)
- ``analyze_morphemes(attrs, guide_text, config, ...)`` — 메인 엔트리
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 30
MIN_BATCH_SIZE = 10
MAX_BATCH_SIZE = 50
BATCH_TOKEN_BUDGET = 1200  # 배치크기 자동계산용 상수 (input token 기준)
MAX_TOKENS_PER_ATTR = 12
LOW_CONFIDENCE_THRESHOLD = 0.7


@dataclass
class MorphemeResult:
    """형태소 분해 결과 1건."""
    attr: str
    tokens: list[str] = field(default_factory=list)
    confidence: float = 0.0
    note: str = ""  # 비고 컬럼용


@dataclass
class AnalysisStats:
    """분석 통계 (리포트 상단에 들어감)."""
    total: int = 0
    success: int = 0
    low_confidence: int = 0
    parse_failed: int = 0
    truncated: int = 0
    batch_size_effective: int = 0
    batches_total: int = 0
    retries: int = 0
    elapsed_sec: float = 0.0
    parallel: int = 1


def load_attributes_txt(path: str) -> list[str]:
    """Load attributes from plain text file (one attribute per line).

    - BOM / trailing whitespace / 빈 줄 제거
    - 중복 제거 (첫 등장 순서 유지)
    - 한국어 주석 지원: `#` 로 시작하는 줄은 스킵
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(f"--input 파일을 찾을 수 없습니다: {path}")

    seen: set[str] = set()
    result: list[str] = []
    with open(path, "r", encoding="utf-8-sig") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line in seen:
                continue
            seen.add(line)
            result.append(line)
    return result


def load_guide(path: str) -> str:
    """Load 지침.md 텍스트 (D3: 인자 누락/파일 없음은 에러)."""
    if not path:
        raise ValueError("--guide 경로가 필요합니다 (지침.md)")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"--guide 파일을 찾을 수 없습니다: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _compute_batch_size(attrs: list[str], user_override: int | None = None) -> int:
    """평균 길이 기반 자동 배치크기.

    - 사용자가 ``--batch-size`` 를 주면 그대로 사용 (10~50 으로 clamp)
    - 그 외: `max(10, min(50, 1200 // avg_len))`
    """
    if user_override is not None:
        return max(MIN_BATCH_SIZE, min(MAX_BATCH_SIZE, user_override))
    if not attrs:
        return DEFAULT_BATCH_SIZE
    avg_len = sum(len(a) for a in attrs) / len(attrs)
    if avg_len <= 0:
        return DEFAULT_BATCH_SIZE
    computed = int(BATCH_TOKEN_BUDGET // max(avg_len, 1))
    return max(MIN_BATCH_SIZE, min(MAX_BATCH_SIZE, computed))


def _get_llm_client(config: dict):
    from openai import OpenAI

    llm_config = config.get("llm", {})
    api_key = os.environ.get("LLM_API_KEY") or llm_config.get("api_key", "ollama")
    api_base = os.environ.get("LLM_API_BASE") or llm_config.get(
        "api_base", "http://localhost:11434/v1"
    )
    return OpenAI(api_key=api_key, base_url=api_base)


def _build_user_prompt(guide_text: str, batch: list[str]) -> str:
    numbered = "\n".join(f"{i+1}. {a}" for i, a in enumerate(batch))
    return (
        f"{guide_text}\n\n"
        f"## 분석 대상 속성 목록 ({len(batch)}개)\n\n"
        f"{numbered}\n\n"
        f"위 속성 목록을 지침에 따라 형태소 분해해 JSON 배열로만 응답하세요.\n"
        f"각 원소는 {{\"attr\": 원본속성, \"tokens\": [단어...], \"confidence\": 0.0~1.0}} 형식이어야 합니다.\n"
        f"응답은 {len(batch)}개 원소를 입력 순서대로 포함해야 합니다.\n"
        f"설명/코드펜스/부가 텍스트 없이 JSON 배열만 출력하세요."
    )


def _parse_response(text: str) -> list[dict]:
    """모델 응답에서 JSON 배열을 추출."""
    text = text.strip()
    # 1) 코드펜스 우선 시도
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if m:
        text = m.group(1).strip()
    # 2) 첫 `[` ~ 마지막 `]` 슬라이스 폴백
    if not text.startswith("["):
        start = text.find("[")
        end = text.rfind("]")
        if start != -1 and end != -1 and end > start:
            text = text[start : end + 1]
    return json.loads(text)


def _call_llm_once(
    client, model: str, guide_text: str, batch: list[str], timeout: int
) -> list[dict]:
    prompt = _build_user_prompt(guide_text, batch)
    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": (
                    "당신은 반도체 제조 및 공급망 시스템의 데이터 표준화 전문가입니다. "
                    "반드시 유효한 JSON 배열만 응답하세요. 설명이나 부가 텍스트 없이 JSON 만 출력하세요."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0.0,
        timeout=timeout,
    )
    text = response.choices[0].message.content or ""
    return _parse_response(text)


def _index_response(
    parsed: list[dict], batch: list[str]
) -> dict[str, dict]:
    """모델 응답을 attr -> dict 로 인덱싱 (attr 키 누락 시 순서 매칭)."""
    by_attr: dict[str, dict] = {}
    for i, item in enumerate(parsed):
        if not isinstance(item, dict):
            continue
        attr = item.get("attr") or (batch[i] if i < len(batch) else None)
        if attr is None:
            continue
        by_attr[attr] = item
    return by_attr


def _post_process_item(attr: str, item: dict | None) -> MorphemeResult:
    """단일 속성의 모델 출력을 검증하고 `MorphemeResult` 로 변환."""
    if not item:
        return MorphemeResult(
            attr=attr, tokens=[], confidence=0.0, note="파싱 실패 - 모델 응답 없음"
        )

    tokens_raw = item.get("tokens") or []
    if not isinstance(tokens_raw, list):
        return MorphemeResult(
            attr=attr,
            tokens=[],
            confidence=0.0,
            note="파싱 실패 - tokens 필드가 배열이 아님",
        )

    tokens = [str(t).strip() for t in tokens_raw if str(t).strip()]
    try:
        confidence = float(item.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    notes: list[str] = []

    # 단어 잘림 처리
    original_count = len(tokens)
    if original_count > MAX_TOKENS_PER_ATTR:
        dropped = tokens[MAX_TOKENS_PER_ATTR:]
        tokens = tokens[:MAX_TOKENS_PER_ATTR]
        notes.append(
            f"13번째 이후 {len(dropped)}개 생략: {', '.join(dropped)}"
        )

    if confidence < LOW_CONFIDENCE_THRESHOLD and confidence > 0.0:
        notes.append("저신뢰도 (수동 검토 필요)")

    if not tokens:
        notes.append("파싱 실패 - tokens 비어있음")

    return MorphemeResult(
        attr=attr,
        tokens=tokens,
        confidence=confidence,
        note=" / ".join(notes),
    )


def _process_batch(
    client,
    model: str,
    guide_text: str,
    batch: list[str],
    timeout: int,
    stats_counter: dict,
) -> list[MorphemeResult]:
    """배치 1개 처리. JSON 실패 시 절반씩 축소 재시도 (최대 2단계)."""
    current = batch
    remaining_levels = 2  # full → half → quarter
    last_error = ""
    parsed: list[dict] | None = None

    while remaining_levels >= 0:
        try:
            parsed = _call_llm_once(client, model, guide_text, current, timeout)
            if isinstance(parsed, list):
                break
            last_error = "응답이 JSON 배열이 아님"
        except json.JSONDecodeError as e:
            last_error = f"JSON 파싱 실패: {e}"
            stats_counter["retries"] = stats_counter.get("retries", 0) + 1
        except Exception as e:  # noqa: BLE001 — LLM endpoint 예측 불가
            last_error = f"LLM 호출 실패: {e}"
            logger.error("LLM call failed: %s", e)
            break

        remaining_levels -= 1
        if remaining_levels < 0:
            break
        new_size = max(1, len(current) // 2)
        if new_size == len(current):
            break
        logger.warning(
            "Batch parse failed, reducing size: %d → %d",
            len(current),
            new_size,
        )
        # 재시도는 앞에서부터 잘라서 전체 입력 재돌림
        current = current[:new_size]
        # 반으로 자른 뒤에도 실패하면 break
        time.sleep(1)

    # 전체 batch 에 대한 결과를 만든다 (재시도에서 커버 못한 꼬리는 파싱 실패)
    results: list[MorphemeResult] = []
    if parsed is not None and isinstance(parsed, list):
        index = _index_response(parsed, current)
        for attr in batch:
            if attr in index:
                results.append(_post_process_item(attr, index[attr]))
            else:
                results.append(
                    MorphemeResult(
                        attr=attr,
                        tokens=[],
                        confidence=0.0,
                        note=f"파싱 실패 - 응답 누락 ({last_error or '재시도 범위 밖'})",
                    )
                )
    else:
        for attr in batch:
            results.append(
                MorphemeResult(
                    attr=attr,
                    tokens=[],
                    confidence=0.0,
                    note=f"파싱 실패 - 재시도 후 실패 ({last_error})",
                )
            )
    return results


def analyze_morphemes(
    attrs: list[str],
    guide_text: str,
    config: dict,
    batch_size: int | None = None,
    parallel: int = 1,
    timeout: int = 120,
) -> tuple[list[MorphemeResult], AnalysisStats]:
    """속성명 리스트를 LLM 으로 형태소 분해.

    Returns ``(results, stats)`` — results 는 입력 순서대로.
    """
    effective_batch = _compute_batch_size(attrs, batch_size)
    client = _get_llm_client(config)
    model = (
        os.environ.get("LLM_MODEL")
        or config.get("llm", {}).get("model", "llama3")
    )

    total = len(attrs)
    batches: list[list[str]] = [
        attrs[i : i + effective_batch] for i in range(0, total, effective_batch)
    ]
    total_batches = len(batches)

    stats = AnalysisStats(
        total=total,
        batch_size_effective=effective_batch,
        batches_total=total_batches,
        parallel=max(1, parallel),
    )
    stats_counter = {"retries": 0}

    print(f"  LLM model: {model}")
    print(
        f"  Attributes: {total} / batch size: {effective_batch} "
        f"({total_batches} batches) / parallel: {stats.parallel}"
    )

    start = time.time()
    # batch_index → list[MorphemeResult] 로 수집 후 순서대로 flatten
    results_by_idx: dict[int, list[MorphemeResult]] = {}

    if stats.parallel == 1:
        for i, batch in enumerate(batches):
            results_by_idx[i] = _process_batch(
                client, model, guide_text, batch, timeout, stats_counter
            )
            if (i + 1) % 5 == 0 or (i + 1) == total_batches:
                done = sum(len(r) for r in results_by_idx.values())
                print(f"  [{i+1}/{total_batches}] {done}/{total} attributes processed")
    else:
        with ThreadPoolExecutor(max_workers=stats.parallel) as pool:
            future_to_idx = {
                pool.submit(
                    _process_batch,
                    client,
                    model,
                    guide_text,
                    batch,
                    timeout,
                    stats_counter,
                ): i
                for i, batch in enumerate(batches)
            }
            completed = 0
            for future in as_completed(future_to_idx):
                i = future_to_idx[future]
                results_by_idx[i] = future.result()
                completed += 1
                if completed % 5 == 0 or completed == total_batches:
                    done = sum(len(r) for r in results_by_idx.values())
                    print(
                        f"  [{completed}/{total_batches}] "
                        f"{done}/{total} attributes processed"
                    )

    elapsed = time.time() - start

    # Flatten in batch order
    ordered_results: list[MorphemeResult] = []
    for i in range(total_batches):
        ordered_results.extend(results_by_idx.get(i, []))

    # Stats 집계
    for r in ordered_results:
        if r.tokens and not r.note.startswith("파싱 실패"):
            stats.success += 1
        if r.confidence > 0.0 and r.confidence < LOW_CONFIDENCE_THRESHOLD:
            stats.low_confidence += 1
        if r.note.startswith("파싱 실패"):
            stats.parse_failed += 1
        if "생략" in r.note:
            stats.truncated += 1

    stats.retries = stats_counter.get("retries", 0)
    stats.elapsed_sec = elapsed

    print(
        f"  Done in {elapsed:.1f}s — success={stats.success}, "
        f"low_conf={stats.low_confidence}, parse_failed={stats.parse_failed}, "
        f"truncated={stats.truncated}, retries={stats.retries}"
    )

    return ordered_results, stats
