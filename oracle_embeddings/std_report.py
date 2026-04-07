import json
import logging
import os
import re
import time
from datetime import datetime

logger = logging.getLogger(__name__)


def generate_report(analysis: dict, data_validation: dict, config: dict,
                    output_dir: str) -> str:
    """Generate full standardization report with LLM proposals."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_dir = os.path.join(output_dir, f"std_report_{timestamp}")
    os.makedirs(report_dir, exist_ok=True)

    files = []

    # 1. JOIN column mismatch report
    path = _write_join_mismatch_report(analysis["join_column_mismatch"], report_dir)
    files.append(path)

    # 2. Type inconsistency report
    path = _write_type_inconsistency_report(analysis["type_inconsistency"], report_dir)
    files.append(path)

    # 3. Naming pattern report
    path = _write_naming_pattern_report(analysis["naming_pattern"], report_dir)
    files.append(path)

    # 4. Identifier pattern report
    path = _write_identifier_pattern_report(analysis["identifier_pattern"], report_dir)
    files.append(path)

    # 5. Code column report (with data validation)
    path = _write_code_column_report(
        analysis["code_columns"],
        data_validation.get("code_validation", []),
        report_dir,
    )
    files.append(path)

    # 6. Y/N column report (with data validation)
    path = _write_yn_column_report(
        analysis["yn_columns"],
        data_validation.get("yn_validation", []),
        report_dir,
    )
    files.append(path)

    # 7. Column usage report
    if data_validation.get("column_usage"):
        path = _write_column_usage_report(data_validation["column_usage"], report_dir)
        files.append(path)

    # 8. LLM standardization proposals
    if config.get("llm"):
        print("  Generating LLM standardization proposals...")
        path = _write_llm_proposals(analysis, data_validation, config, report_dir)
        if path:
            files.append(path)

    # 9. Summary report
    path = _write_summary(analysis, data_validation, files, report_dir)
    files.insert(0, path)

    return report_dir


def _write_summary(analysis: dict, data_validation: dict,
                   report_files: list[str], report_dir: str) -> str:
    """Write overall summary report."""
    filepath = os.path.join(report_dir, "00_summary.md")

    with open(filepath, "w", encoding="utf-8") as f:
        f.write("# Standardization Analysis Report\n\n")
        f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")

        f.write("## Analysis Results\n\n")
        f.write("| Category | Count | Severity |\n")
        f.write("|----------|-------|----------|\n")

        mismatch = len(analysis.get("join_column_mismatch", []))
        type_inc = len(analysis.get("type_inconsistency", []))
        naming = len(analysis.get("naming_pattern", {}).get("violations", []))
        code_cols = len(analysis.get("code_columns", []))
        yn_cols = len(analysis.get("yn_columns", []))

        yn_abnormal = sum(1 for v in data_validation.get("yn_validation", [])
                          if v.get("has_abnormal"))
        unused = sum(1 for v in data_validation.get("column_usage", [])
                     if v.get("is_unused"))

        f.write(f"| JOIN 컬럼명 불일치 | {mismatch} | {'HIGH' if mismatch > 10 else 'MEDIUM'} |\n")
        f.write(f"| 동일 컬럼 타입 불일치 | {type_inc} | HIGH |\n")
        f.write(f"| 네이밍 패턴 이탈 | {naming} | LOW |\n")
        f.write(f"| 코드성 컬럼 | {code_cols} | INFO |\n")
        f.write(f"| Y/N 컬럼 | {yn_cols} | INFO |\n")
        f.write(f"| Y/N 이상 데이터 | {yn_abnormal} | {'HIGH' if yn_abnormal > 0 else '-'} |\n")
        f.write(f"| 미사용 컬럼 (NULL 100%) | {unused} | MEDIUM |\n")
        f.write("\n")

        f.write("## Report Files\n\n")
        for fp in report_files:
            name = os.path.basename(fp)
            f.write(f"- [{name}]({name})\n")
        f.write("\n")

    return filepath


def _write_join_mismatch_report(mismatches: list[dict], report_dir: str) -> str:
    filepath = os.path.join(report_dir, "01_join_column_mismatch.md")

    with open(filepath, "w", encoding="utf-8") as f:
        f.write("# JOIN Column Name Mismatch\n\n")
        f.write("동일한 관계인데 컬럼명이 다른 경우입니다. 용어 표준화 대상입니다.\n\n")

        if not mismatches:
            f.write("No mismatches found.\n")
            return filepath

        f.write(f"Total: {len(mismatches)}\n\n")
        f.write("| Table A | Column A | Table B | Column B | JOIN Type | Source |\n")
        f.write("|---------|----------|---------|----------|-----------|--------|\n")
        for m in mismatches:
            f.write(f"| {m['table1']} | {m['column1']} | {m['table2']} | {m['column2']} "
                    f"| {m['join_type']} | {m['source']} |\n")
        f.write("\n")

    return filepath


def _write_type_inconsistency_report(inconsistencies: list[dict], report_dir: str) -> str:
    filepath = os.path.join(report_dir, "02_type_inconsistency.md")

    with open(filepath, "w", encoding="utf-8") as f:
        f.write("# Column Type Inconsistency\n\n")
        f.write("여러 테이블에 동일한 컬럼명이 있지만 타입이 다른 경우입니다.\n\n")

        if not inconsistencies:
            f.write("No inconsistencies found.\n")
            return filepath

        f.write(f"Total: {len(inconsistencies)} columns\n\n")

        for inc in inconsistencies:
            f.write(f"### {inc['column_name']} ({inc['table_count']} tables)\n\n")
            f.write(f"Types: {', '.join(inc['types'])}\n\n")
            f.write("| Table | Type | Nullable |\n")
            f.write("|-------|------|----------|\n")
            for occ in inc["occurrences"]:
                f.write(f"| {occ['table']} | {occ['data_type']} | {occ['nullable']} |\n")
            f.write("\n")

    return filepath


def _write_naming_pattern_report(naming: dict, report_dir: str) -> str:
    filepath = os.path.join(report_dir, "03_naming_pattern.md")

    with open(filepath, "w", encoding="utf-8") as f:
        f.write("# Naming Pattern Violations\n\n")
        f.write("테이블 내 컬럼 네이밍 패턴(접두어)에서 이탈한 컬럼입니다.\n\n")

        violations = naming.get("violations", [])
        if not violations:
            f.write("No violations found.\n")
            return filepath

        f.write(f"Total: {len(violations)} tables\n\n")

        for v in violations:
            f.write(f"### {v['table']}\n\n")
            f.write(f"- Dominant prefix: `{v['dominant_prefix']}_` "
                    f"({v['conforming_columns']}/{v['total_columns']} columns, "
                    f"{int(v['prefix_ratio'] * 100)}%)\n")
            f.write(f"- Outlier columns:\n")
            for col in v["outlier_columns"]:
                f.write(f"  - `{col}`\n")
            f.write("\n")

    return filepath


def _write_identifier_pattern_report(id_pattern: dict, report_dir: str) -> str:
    filepath = os.path.join(report_dir, "04_identifier_pattern.md")

    with open(filepath, "w", encoding="utf-8") as f:
        f.write("# PK/Identifier Naming Patterns\n\n")
        f.write("Primary Key 컬럼의 접미어 패턴 분석입니다.\n\n")

        f.write(f"Total PKs: {id_pattern.get('total_pks', 0)}\n\n")
        f.write("| Suffix | Count | Examples |\n")
        f.write("|--------|-------|----------|\n")
        for p in id_pattern.get("patterns", []):
            examples = ", ".join(p["examples"])
            f.write(f"| `{p['suffix']}` | {p['count']} | {examples} |\n")
        f.write("\n")

    return filepath


def _write_code_column_report(code_columns: list[dict],
                               code_validation: list[dict],
                               report_dir: str) -> str:
    filepath = os.path.join(report_dir, "05_code_columns.md")

    # Build validation lookup
    val_map = {}
    for v in code_validation:
        key = f"{v['table']}.{v['column']}"
        val_map[key] = v

    with open(filepath, "w", encoding="utf-8") as f:
        f.write("# Code Column Analysis\n\n")
        f.write("코드성 컬럼(_CD, _TYPE, _CODE 등)의 현황 및 실데이터 분석입니다.\n\n")
        f.write(f"Total: {len(code_columns)} columns\n\n")

        for col_info in code_columns:
            key = f"{col_info['table']}.{col_info['column']}"
            f.write(f"### {key}\n\n")
            f.write(f"- Type: `{col_info['data_type']}`\n")
            if col_info.get("comment"):
                f.write(f"- Comment: {col_info['comment']}\n")

            val = val_map.get(key)
            if val and not val.get("error"):
                f.write(f"- Total rows: {val['total_rows']}\n")
                f.write(f"- Distinct values: {val['distinct_count']}\n")
                f.write(f"- NULL count: {val['null_count']}\n\n")

                if val.get("values"):
                    f.write("| Value | Count |\n")
                    f.write("|-------|-------|\n")
                    for v in val["values"][:20]:
                        f.write(f"| {v['value']} | {v['count']} |\n")
                    if len(val["values"]) > 20:
                        f.write(f"| ... | +{len(val['values']) - 20} more |\n")
                    f.write("\n")
            elif val and val.get("error"):
                f.write(f"- Error: {val['error']}\n\n")
            else:
                f.write("- (실데이터 검증 미실행)\n\n")

    return filepath


def _write_yn_column_report(yn_columns: list[dict],
                             yn_validation: list[dict],
                             report_dir: str) -> str:
    filepath = os.path.join(report_dir, "06_yn_columns.md")

    val_map = {}
    for v in yn_validation:
        key = f"{v['table']}.{v['column']}"
        val_map[key] = v

    with open(filepath, "w", encoding="utf-8") as f:
        f.write("# Y/N Column Analysis\n\n")
        f.write("여부 컬럼(_YN, _FLAG)의 현황 및 이상 데이터 체크입니다.\n\n")

        # Abnormal first
        abnormal_list = [v for v in yn_validation if v.get("has_abnormal")]
        if abnormal_list:
            f.write(f"## Abnormal Data Found ({len(abnormal_list)} columns)\n\n")
            f.write("| Table | Column | Type | Abnormal Values | Count |\n")
            f.write("|-------|--------|------|-----------------|-------|\n")
            for v in abnormal_list:
                abnormal_str = ", ".join(f"{k}({cnt})" for k, cnt in v["abnormal_values"].items())
                f.write(f"| {v['table']} | {v['column']} | {v['data_type']} "
                        f"| {abnormal_str} | {v['total_rows']} |\n")
            f.write("\n")

        # Full list
        f.write(f"## All Y/N Columns ({len(yn_columns)})\n\n")
        f.write("| Table | Column | Type | Y | N | NULL | Abnormal | Status |\n")
        f.write("|-------|--------|------|---|---|------|----------|--------|\n")
        for col_info in yn_columns:
            key = f"{col_info['table']}.{col_info['column']}"
            val = val_map.get(key)
            if val and not val.get("error"):
                dist = val["distribution"]
                y_cnt = dist.get("Y", dist.get("1", 0))
                n_cnt = dist.get("N", dist.get("0", 0))
                null_cnt = val["null_count"]
                status = "ABNORMAL" if val["has_abnormal"] else "OK"
                f.write(f"| {col_info['table']} | {col_info['column']} | {col_info['data_type']} "
                        f"| {y_cnt} | {n_cnt} | {null_cnt} | "
                        f"{'YES' if val['has_abnormal'] else '-'} | {status} |\n")
            else:
                f.write(f"| {col_info['table']} | {col_info['column']} | {col_info['data_type']} "
                        f"| - | - | - | - | N/A |\n")
        f.write("\n")

    return filepath


def _write_column_usage_report(usage: list[dict], report_dir: str) -> str:
    filepath = os.path.join(report_dir, "07_column_usage.md")

    unused = [u for u in usage if u.get("is_unused")]
    oversized = [u for u in usage if u.get("is_oversized")]

    with open(filepath, "w", encoding="utf-8") as f:
        f.write("# Column Usage Analysis\n\n")

        if unused:
            f.write(f"## Unused Columns - NULL 100% ({len(unused)})\n\n")
            f.write("| Table | Column | Type | Comment |\n")
            f.write("|-------|--------|------|---------|\n")
            for u in unused:
                f.write(f"| {u['table']} | {u['column']} | {u['data_type']} "
                        f"| {u.get('comment', '')} |\n")
            f.write("\n")

        if oversized:
            f.write(f"## Oversized Columns ({len(oversized)})\n\n")
            f.write("| Table | Column | Defined | Actual Max | Usage |\n")
            f.write("|-------|--------|---------|------------|-------|\n")
            for u in oversized:
                ratio = f"{u['max_length']}/{u['defined_length']}"
                f.write(f"| {u['table']} | {u['column']} | {u['data_type']} "
                        f"| {u['max_length']}자 | {ratio} |\n")
            f.write("\n")

    return filepath


def _write_llm_proposals(analysis: dict, data_validation: dict,
                          config: dict, report_dir: str) -> str:
    """Generate LLM standardization proposals."""
    from openai import OpenAI

    llm_config = config.get("llm", {})
    api_key = os.environ.get("LLM_API_KEY") or llm_config.get("api_key", "ollama")
    api_base = os.environ.get("LLM_API_BASE") or llm_config.get("api_base", "http://localhost:11434/v1")
    client = OpenAI(api_key=api_key, base_url=api_base)
    model = os.environ.get("LLM_MODEL") or llm_config.get("model", "llama3")

    filepath = os.path.join(report_dir, "08_llm_proposals.md")

    # Build context for LLM
    context_parts = []

    # Join mismatches
    mismatches = analysis.get("join_column_mismatch", [])[:30]
    if mismatches:
        lines = ["JOIN 컬럼명 불일치:"]
        for m in mismatches:
            lines.append(f"  {m['table1']}.{m['column1']} = {m['table2']}.{m['column2']}")
        context_parts.append("\n".join(lines))

    # Type inconsistencies
    type_inc = analysis.get("type_inconsistency", [])[:20]
    if type_inc:
        lines = ["타입 불일치:"]
        for t in type_inc:
            tables = ", ".join(f"{o['table']}({o['data_type']})" for o in t["occurrences"][:5])
            lines.append(f"  {t['column_name']}: {tables}")
        context_parts.append("\n".join(lines))

    # Identifier patterns
    id_patterns = analysis.get("identifier_pattern", {}).get("patterns", [])
    if id_patterns:
        lines = ["PK 접미어 패턴:"]
        for p in id_patterns[:10]:
            lines.append(f"  {p['suffix']}: {p['count']}개")
        context_parts.append("\n".join(lines))

    # YN abnormal
    yn_abnormal = [v for v in data_validation.get("yn_validation", []) if v.get("has_abnormal")]
    if yn_abnormal:
        lines = ["Y/N 이상 데이터:"]
        for v in yn_abnormal[:10]:
            lines.append(f"  {v['table']}.{v['column']}: {v['abnormal_values']}")
        context_parts.append("\n".join(lines))

    context = "\n\n".join(context_parts)

    prompt = f"""다음은 Oracle DB 표준화 분석 결과입니다. 각 항목에 대해 구체적인 표준화 제안을 해주세요.

{context}

## 요청사항
1. JOIN 컬럼명 불일치: 어떤 이름으로 통일할지 제안 (근거 포함)
2. 타입 불일치: 어떤 타입으로 통일할지 제안
3. PK 접미어: 표준 접미어 제안 (_ID, _NO, _CD 중 추천)
4. Y/N 이상 데이터: 정비 방안 제안

## 응답 형식
마크다운으로 작성. 각 항목별로 현행, 문제점, 표준안, 조치사항을 포함."""

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "당신은 데이터 표준화 전문가입니다. 구체적이고 실행 가능한 표준화 방안을 제시합니다."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
        )
        proposal_text = response.choices[0].message.content.strip()
    except Exception as e:
        logger.error("LLM proposal generation failed: %s", e)
        proposal_text = f"LLM 호출 실패: {e}"

    with open(filepath, "w", encoding="utf-8") as f:
        f.write("# Standardization Proposals (LLM)\n\n")
        f.write("로컬 LLM이 분석 결과를 기반으로 제안한 표준화 방안입니다.\n\n")
        f.write("---\n\n")
        f.write(proposal_text)
        f.write("\n")

    return filepath
