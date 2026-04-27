"""LLM fallback for statements the DSL pipeline can't fully rewrite
(docs/migration/spec.md §10).

When :func:`sql_rewriter.rewrite_sql` returns ``needs_llm=True`` (merge
tables / split columns outside SELECT / dropped columns / complex JOIN
topology …) we hand off to a local LLM endpoint for a JSON-structured
rewrite suggestion. Follows the same OpenAI-compatible client pattern used
elsewhere in the project (``sql_reviewer_llm``, ``terms_llm`` etc.).

The LLM is asked to:
    - preserve MyBatis OGNL (``#{x}``, ``${y}``) and dynamic tags verbatim
      (the caller feeds it a max-path render, so tags are already erased —
      the hint exists for defensive prompting),
    - return ``converted_sql``, ``confidence``, ``changes``, and a
      ``needs_human_review`` flag,
    - drop the statement into the Unresolved Queue on low confidence.

Confidence < 0.7 or ``needs_human_review = true`` → ``UNRESOLVED`` status.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .mapping_model import ChangeItem, ColumnMapping, Mapping, TableMapping
from .sql_rewriter import SqlRewriteOutcome

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class LlmResult:
    converted_sql: Optional[str] = None
    confidence: float = 0.0
    changes: List[str] = field(default_factory=list)
    needs_human_review: bool = True
    review_reason: str = ""
    raw_response: str = ""
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def llm_rewrite(
    as_is_sql: str,
    mapping: Mapping,
    *,
    partial_outcome: Optional[SqlRewriteOutcome] = None,
    to_be_ddl_snippet: str = "",
    config: Optional[Dict[str, Any]] = None,
    max_retries: int = 2,
    confidence_threshold: float = 0.7,
) -> LlmResult:
    """Ask the LLM to rewrite ``as_is_sql``. Returns :class:`LlmResult`.

    The prompt is intentionally minimal: only the mapping entries that
    reference tables actually present in ``as_is_sql`` are sent, plus any
    partial result from the DSL transformer pipeline (so the LLM starts from
    a half-done baseline instead of a cold SQL).
    """
    config = config or {}

    try:
        from openai import OpenAI  # deferred — optional dep path
    except ImportError as exc:
        return LlmResult(
            error=f"openai SDK unavailable: {exc}", needs_human_review=True,
        )

    relevant = _extract_relevant_mappings(as_is_sql, mapping)
    mapping_yaml = _format_mapping_snippet(relevant)
    partial_text = (
        (partial_outcome.to_be_sql or "")
        if (partial_outcome and partial_outcome.to_be_sql) else "SKIPPED"
    )
    warnings_text = (
        "\n".join(f"- {w}" for w in (partial_outcome.warnings if partial_outcome else []))
        or "-"
    )

    prompt = _build_prompt(
        as_is_sql=as_is_sql,
        mapping_yaml=mapping_yaml,
        partial_text=partial_text,
        warnings_text=warnings_text,
        to_be_ddl_snippet=to_be_ddl_snippet or "(omitted)",
    )

    client, model = _build_client(config)

    for attempt in range(max_retries + 1):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "system",
                        "content": "You are rewriting Oracle SQL for a schema "
                        "migration. Return JSON only, no prose. Keep MyBatis "
                        "OGNL placeholders (#{x}, ${x}) verbatim. Never invent "
                        "columns not listed in the provided schema.",
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
                timeout=180,
            )
            text = (response.choices[0].message.content or "").strip()
            raw = text
            text = _extract_json_block(text)
            data = json.loads(text)
            return _coerce_llm_result(data, raw, confidence_threshold)
        except json.JSONDecodeError as exc:
            logger.warning(
                "LLM returned invalid JSON (attempt %d): %s", attempt + 1, exc
            )
            if attempt < max_retries:
                time.sleep(2 ** (attempt + 1))
                continue
            return LlmResult(
                error=f"LLM JSON parse failed: {exc}",
                raw_response=text,
                needs_human_review=True,
            )
        except Exception as exc:
            logger.error("LLM call failed: %s", exc)
            return LlmResult(
                error=str(exc), needs_human_review=True,
            )

    return LlmResult(error="exhausted retries", needs_human_review=True)


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


_PROMPT_TEMPLATE = """## Context
- AS-IS table/column mappings (only those referenced below):
{mapping_yaml}

## AS-IS SQL (max-path expanded from MyBatis dynamic tags)
{as_is_sql}

## Pattern-engine partial result
{partial_text}

## Transformer warnings
{warnings_text}

## TO-BE schema (snippet)
{to_be_ddl_snippet}

## Rules
- Keep MyBatis OGNL params (#{{x}}, ${{x}}) UNCHANGED
- Keep dynamic tags (<if>, <foreach>, ...) UNCHANGED — the max-path above
  already flattened them; your output should be the flattened form too
- Do not invent columns not in the schema
- Preserve all business logic
- Return ``confidence`` in [0.0, 1.0]; anything below 0.7 will be flagged
  for human review regardless

## Output (JSON only)
{{
  "converted_sql": "<full TO-BE SQL>",
  "confidence": <number 0..1>,
  "changes": ["change 1", "change 2", ...],
  "needs_human_review": <true|false>,
  "review_reason": "<short why if needs_human_review=true>"
}}
"""


def _build_prompt(
    *,
    as_is_sql: str,
    mapping_yaml: str,
    partial_text: str,
    warnings_text: str,
    to_be_ddl_snippet: str,
) -> str:
    return _PROMPT_TEMPLATE.format(
        mapping_yaml=mapping_yaml,
        as_is_sql=as_is_sql,
        partial_text=partial_text,
        warnings_text=warnings_text,
        to_be_ddl_snippet=to_be_ddl_snippet,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_client(config: Dict[str, Any]):
    from openai import OpenAI

    llm_cfg = (config or {}).get("llm", {})
    api_key = os.environ.get("LLM_API_KEY") or llm_cfg.get("api_key", "ollama")
    api_base = (
        os.environ.get("LLM_API_BASE")
        or llm_cfg.get("api_base", "http://localhost:11434/v1")
    )
    model = (
        os.environ.get("LLM_MODEL")
        or llm_cfg.get("model", "llama3")
    )
    return OpenAI(api_key=api_key, base_url=api_base), model


def _extract_relevant_mappings(
    as_is_sql: str,
    mapping: Mapping,
) -> Dict[str, Any]:
    """Only include mapping entries whose AS-IS table name appears literally
    in the SQL — reduces token count dramatically on large mapping files."""
    sql_upper = as_is_sql.upper()
    tables: List[TableMapping] = []
    columns: List[ColumnMapping] = []

    seen_tables: set = set()
    for tm in mapping.tables:
        for name in tm.as_is_tables():
            if name.upper() in sql_upper and name.upper() not in seen_tables:
                tables.append(tm)
                seen_tables.add(name.upper())

    seen_cm_ids: set = set()
    for cm in mapping.columns:
        if id(cm) in seen_cm_ids:
            continue
        for ref in cm.as_is_refs():
            if ref.table.upper() in sql_upper and re.search(
                rf"\b{re.escape(ref.column)}\b", sql_upper
            ):
                columns.append(cm)
                seen_cm_ids.add(id(cm))
                break

    return {"tables": tables, "columns": columns}


def _format_mapping_snippet(relevant: Dict[str, Any]) -> str:
    lines: List[str] = []
    for tm in relevant["tables"]:
        lines.append(
            f"- table {tm.type}: {tm.as_is} → {tm.to_be}"
            + (f" (discriminator={tm.discriminator_column})" if tm.discriminator_column else "")
        )
    for cm in relevant["columns"]:
        tgt = _format_column_target(cm)
        extras = []
        if cm.value_map is not None:
            extras.append(f"value_map={cm.value_map}")
        if cm.transform is not None and not cm.transform.is_empty():
            extras.append(
                "transform={"
                + ", ".join(f"{k}={v!r}" for k, v in cm.transform.expressions())
                + "}"
            )
        if cm.reverse:
            extras.append(f"reverse={cm.reverse!r}")
        extras_str = f" [{'; '.join(extras)}]" if extras else ""
        as_is_str = ", ".join(r.qualified for r in cm.as_is_refs())
        lines.append(f"- column {cm.kind}: {as_is_str} → {tgt}{extras_str}")
    if not lines:
        return "(no relevant mappings — LLM should preserve tables/columns)"
    return "\n".join(lines)


def _format_column_target(cm: ColumnMapping) -> str:
    if cm.to_be is None:
        return "<dropped>"
    if isinstance(cm.to_be, list):
        return ", ".join(f"{t.table}.{t.column}" for t in cm.to_be)
    return f"{cm.to_be.table}.{cm.to_be.column}"


def _extract_json_block(text: str) -> str:
    """Pull the JSON payload out of an LLM response.

    Tries in order, preferring the most specific match:

    1. ` ```json ... ``` ` fenced block.
    2. Generic ` ``` ... ``` ` fenced block (body must look like JSON).
    3. String-aware brace counter — walks the text searching for the first
       balanced ``{...}`` block that *also* parses as JSON. Skips braces
       inside string literals (with ``\"`` / ``\\`` escape handling), so
       ``{"sql": "x IN ({1,2})"}`` stays intact. If a balanced block fails
       to parse (e.g. the LLM wrote ``{x}`` in prose before the real JSON),
       it advances to the next ``{`` and retries.

    The previous ``find("{")`` / ``rfind("}")`` heuristic broke whenever the
    LLM emitted prose containing ``{x}``-style placeholders, or any extra
    ``}`` after the JSON — the substring would span both and ``json.loads``
    blew up. Returns the original ``text`` as a last resort so
    :func:`json.loads` reports a faithful error rather than silently
    swallowing the response.
    """
    # ``` json ``` / ``` ``` fenced block — prefer explicit JSON marker
    for pattern in (
        r"```json\s*([\s\S]*?)```",
        r"```\s*([\s\S]*?)```",
    ):
        m = re.search(pattern, text)
        if not m:
            continue
        candidate = m.group(1).strip()
        if not candidate or candidate[0] not in "{[":
            continue
        extracted = _walk_for_json(candidate)
        if extracted:
            return extracted

    extracted = _walk_for_json(text)
    return extracted or text


def _walk_for_json(text: str) -> str:
    """Return the first balanced ``{...}`` block in ``text`` that parses as
    JSON. If a balanced span fails to parse, advance to the next ``{`` and
    retry — handles prose like ``... {x} ... {real json}``.
    """
    search = 0
    n = len(text)
    while search < n:
        block, end = _balance_braces(text, search)
        if not block:
            return ""
        try:
            json.loads(block)
            return block
        except json.JSONDecodeError:
            next_open = text.find("{", search + 1)
            if next_open < 0:
                return ""
            search = next_open
    return ""


def _balance_braces(text: str, search_from: int = 0) -> tuple:
    """Return ``(block, end_index)`` for the first balanced ``{...}`` starting
    at the first ``{`` at or after ``search_from``. ``end_index`` is the
    position one past the closing brace.

    String-literal aware — braces inside ``"..."`` (with ``\\`` escape) do
    not affect depth. Returns ``("", -1)`` if no balanced block is found.
    """
    start = text.find("{", search_from)
    if start < 0:
        return "", -1

    depth = 0
    in_string = False
    i = start
    n = len(text)
    while i < n:
        c = text[i]
        if in_string:
            if c == "\\" and i + 1 < n:
                i += 2  # skip the escaped char (handles \" and \\)
                continue
            if c == '"':
                in_string = False
            i += 1
            continue
        if c == '"':
            in_string = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1], i + 1
        i += 1
    return "", -1


def _coerce_llm_result(
    data: Dict[str, Any],
    raw: str,
    threshold: float,
) -> LlmResult:
    sql = data.get("converted_sql")
    conf = float(data.get("confidence", 0.0) or 0.0)
    changes = data.get("changes") or []
    if not isinstance(changes, list):
        changes = [str(changes)]
    needs_review = bool(data.get("needs_human_review", True))
    reason = str(data.get("review_reason", "") or "")
    if conf < threshold:
        needs_review = True
        reason = reason or f"confidence {conf:.2f} below threshold {threshold:.2f}"
    return LlmResult(
        converted_sql=sql,
        confidence=max(0.0, min(1.0, conf)),
        changes=[str(c) for c in changes],
        needs_human_review=needs_review,
        review_reason=reason,
        raw_response=raw,
    )
