import logging
import re

logger = logging.getLogger(__name__)


# 패턴 정의: (name, severity, pattern, description, suggestion)
PATTERNS = [
    {
        "id": "SELECT_STAR",
        "name": "SELECT * 사용",
        "severity": "MEDIUM",
        "pattern": r'\bSELECT\s+\*\s+FROM\b',
        "description": "SELECT *는 필요 없는 컬럼까지 조회하여 네트워크 부하와 메모리 낭비를 유발합니다.",
        "suggestion": "필요한 컬럼만 명시적으로 SELECT 하세요.",
    },
    {
        "id": "NOT_IN",
        "name": "NOT IN 사용",
        "severity": "HIGH",
        "pattern": r'\bNOT\s+IN\s*\(',
        "description": "NOT IN은 NULL 처리와 성능 문제가 있습니다.",
        "suggestion": "NOT EXISTS 또는 LEFT JOIN + IS NULL 사용을 권장합니다.",
    },
    {
        "id": "LIKE_LEADING_WILDCARD",
        "name": "LIKE 선두 와일드카드",
        "severity": "HIGH",
        "pattern": r"LIKE\s+['\"]%",
        "description": "LIKE '%...'는 인덱스를 사용할 수 없어 풀테이블 스캔을 유발합니다.",
        "suggestion": "선두 와일드카드를 제거하거나 전문 검색 인덱스 사용을 고려하세요.",
    },
    {
        "id": "FUNCTION_ON_COLUMN",
        "name": "WHERE 절 컬럼에 함수 적용",
        "severity": "MEDIUM",
        "pattern": r'WHERE[^=<>]*(?:UPPER|LOWER|SUBSTR|TRUNC|TO_CHAR|TO_DATE|NVL)\s*\(\s*\w+\.\w+',
        "description": "WHERE 절 컬럼에 함수를 적용하면 인덱스를 사용할 수 없습니다.",
        "suggestion": "함수 기반 인덱스를 생성하거나 함수를 제거하세요.",
    },
    {
        "id": "CARTESIAN_JOIN",
        "name": "콤마 조인 사용 (카티시안 곱 가능성)",
        "severity": "HIGH",
        "pattern": r'FROM\s+\w+(?:\s+\w+)?\s*,\s*\w+',
        "description": "FROM 절에 콤마로 나열된 테이블은 JOIN 조건을 빠뜨리면 카티시안 곱이 발생합니다.",
        "suggestion": "명시적 JOIN ON 절을 사용하세요.",
    },
    {
        "id": "OR_IN_WHERE",
        "name": "WHERE 절 OR 사용",
        "severity": "LOW",
        "pattern": r'WHERE[^;]*\bOR\b[^;]*',
        "description": "WHERE 절 OR는 인덱스 사용을 방해할 수 있습니다.",
        "suggestion": "UNION ALL 또는 IN 으로 변경을 검토하세요.",
    },
    {
        "id": "DISTINCT",
        "name": "DISTINCT 사용",
        "severity": "LOW",
        "pattern": r'\bSELECT\s+DISTINCT\b',
        "description": "DISTINCT는 정렬 작업을 수반하여 비용이 높습니다. JOIN 설계 문제일 가능성이 있습니다.",
        "suggestion": "GROUP BY 또는 EXISTS 서브쿼리로 대체를 검토하세요.",
    },
    {
        "id": "IMPLICIT_CONVERSION",
        "name": "암시적 형변환 의심",
        "severity": "MEDIUM",
        "pattern": r"=\s*['\"]\d+['\"]|=\s*\d+\s+AND\s+\w+\s*=\s*['\"]",
        "description": "NUMBER 컬럼과 문자열 비교 시 암시적 형변환이 발생하여 인덱스를 사용할 수 없습니다.",
        "suggestion": "컬럼 타입과 동일한 리터럴 타입을 사용하세요.",
    },
    {
        "id": "ORDER_BY_NO_INDEX",
        "name": "ORDER BY + LIMIT/ROWNUM",
        "severity": "LOW",
        "pattern": r'ORDER\s+BY[^;]*ROWNUM',
        "description": "ORDER BY와 ROWNUM 함께 사용 시 의도대로 동작하지 않을 수 있습니다.",
        "suggestion": "인라인 뷰로 ORDER BY 후 ROWNUM 적용을 권장합니다.",
    },
    {
        "id": "SUBQUERY_IN_SELECT",
        "name": "SELECT 절 스칼라 서브쿼리",
        "severity": "MEDIUM",
        "pattern": r'SELECT\s+[^,]*\(\s*SELECT\b',
        "description": "SELECT 절의 스칼라 서브쿼리는 행 단위로 실행되어 성능 문제를 일으킬 수 있습니다.",
        "suggestion": "JOIN으로 변경을 검토하세요.",
    },
    # MISSING_WHERE는 review_statements에서 특별 처리 (regex로는 부정 조건 처리가 어려움)
]


def review_statements(statements: list[dict]) -> dict:
    """Review all SQL statements and return findings."""
    findings_by_pattern = {}
    findings_by_stmt = []

    for stmt in statements:
        sql = stmt["sql"]
        sql_upper = sql.upper()
        stmt_findings = []

        # Special check: UPDATE/DELETE without WHERE clause
        stmt_type_upper = stmt.get("type", "").upper()
        if stmt_type_upper in ("UPDATE", "DELETE"):
            if not re.search(r'\bWHERE\b', sql_upper, re.IGNORECASE | re.DOTALL):
                missing_where_pattern = {
                    "id": "MISSING_WHERE",
                    "name": "UPDATE/DELETE WHERE 없음",
                    "severity": "CRITICAL",
                    "description": "UPDATE 또는 DELETE 문에 WHERE 조건이 없으면 전체 테이블이 영향을 받습니다.",
                    "suggestion": "WHERE 조건을 반드시 추가하세요. 의도된 것이면 검토 후 실행하세요.",
                }
                finding = {
                    "pattern_id": missing_where_pattern["id"],
                    "pattern_name": missing_where_pattern["name"],
                    "severity": missing_where_pattern["severity"],
                    "mapper": stmt["mapper"],
                    "stmt_id": stmt["id"],
                    "stmt_type": stmt["type"],
                    "description": missing_where_pattern["description"],
                    "suggestion": missing_where_pattern["suggestion"],
                    "sql_preview": sql[:200] + ("..." if len(sql) > 200 else ""),
                }
                stmt_findings.append(finding)
                if "MISSING_WHERE" not in findings_by_pattern:
                    findings_by_pattern["MISSING_WHERE"] = {
                        "pattern": missing_where_pattern,
                        "occurrences": [],
                    }
                findings_by_pattern["MISSING_WHERE"]["occurrences"].append({
                    "mapper": stmt["mapper"],
                    "stmt_id": stmt["id"],
                    "stmt_type": stmt["type"],
                })

        for pattern in PATTERNS:
            regex = pattern["pattern"]
            if re.search(regex, sql_upper, re.IGNORECASE | re.MULTILINE):
                finding = {
                    "pattern_id": pattern["id"],
                    "pattern_name": pattern["name"],
                    "severity": pattern["severity"],
                    "mapper": stmt["mapper"],
                    "stmt_id": stmt["id"],
                    "stmt_type": stmt["type"],
                    "description": pattern["description"],
                    "suggestion": pattern["suggestion"],
                    "sql_preview": sql[:200] + ("..." if len(sql) > 200 else ""),
                }
                stmt_findings.append(finding)

                if pattern["id"] not in findings_by_pattern:
                    findings_by_pattern[pattern["id"]] = {
                        "pattern": pattern,
                        "occurrences": [],
                    }
                findings_by_pattern[pattern["id"]]["occurrences"].append({
                    "mapper": stmt["mapper"],
                    "stmt_id": stmt["id"],
                    "stmt_type": stmt["type"],
                })

        if stmt_findings:
            findings_by_stmt.append({
                "mapper": stmt["mapper"],
                "stmt_id": stmt["id"],
                "stmt_type": stmt["type"],
                "sql": sql,
                "findings": stmt_findings,
            })

    severity_summary = {
        "CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0,
    }
    for p_id, data in findings_by_pattern.items():
        sev = data["pattern"]["severity"]
        severity_summary[sev] += len(data["occurrences"])

    return {
        "total_statements": len(statements),
        "statements_with_issues": len(findings_by_stmt),
        "by_pattern": findings_by_pattern,
        "by_statement": findings_by_stmt,
        "severity_summary": severity_summary,
    }
