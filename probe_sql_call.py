"""SQL call 추출 단계별 진단 — 폐쇄망 환경용 임시 스크립트.

사용자 환경에서 ``sqlId = NAMESPACE + "findX"; sqlSession.selectList(sqlId, p);``
패턴이 잡히지 않는 원인을 단계별로 짚어 결론을 한 줄로 emit.

사용:
    python probe_sql_call.py "C:\\path\\to\\SgService.java"

(선택) 두 번째 인자로 메서드명을 주면 그 메서드만 자세히:
    python probe_sql_call.py "<file>" findSgModList

테스트 완료 후 삭제 PR 별도로 올림.
"""
from __future__ import annotations

import sys

from oracle_embeddings.legacy_java_parser import (
    parse_java_file, _extract_ns_constants, _NS_CONST_RE,
    _eval_string_expr, _SQL_CALL_HEAD_RE,
)
from oracle_embeddings.mybatis_parser import _read_file_safe


def _section(title: str) -> None:
    print("\n" + "=" * 60)
    print(title)
    print("=" * 60)


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    target = sys.argv[1]
    focus = sys.argv[2] if len(sys.argv) >= 3 else ""

    raw = _read_file_safe(target)

    # 1) `_NS_CONST_RE` 가 raw matches 를 만들어내는지 (필터 전)
    _section("[1] _NS_CONST_RE raw 매치 — String const 선언 후보")
    raw_matches = list(_NS_CONST_RE.finditer(raw))
    print(f"raw match 수: {len(raw_matches)}")
    for m in raw_matches[:30]:
        print(f"  {m.group('name')!r} = {m.group('value')!r}")
    if len(raw_matches) > 30:
        print(f"  ... +{len(raw_matches) - 30}")

    # 2) `_extract_ns_constants` 가 최종 보관하는 dict (boundary 통과한 것)
    _section("[2] _extract_ns_constants 최종 dict")
    ns = _extract_ns_constants(raw)
    if not ns:
        print("⚠ EMPTY — _NS_CONST_RE 가 raw 매치는 만들었지만 _NS_VALUE_RE")
        print("  boundary 통과한 게 0 — 값에 허용 안 된 문자 존재")
        print("  (\\w + . + - 만 허용. 슬래시/콜론/공백/한글 등 있으면 탈락)")
    for k, v in ns.items():
        ends_dot = "✓ 끝.." if v.endswith(".") else "✗ 끝.없음"
        print(f"  {k!r} = {v!r}  [{ends_dot}]")
    has_namespace = "NAMESPACE" in ns

    # 3) 클래스 파싱 + 메서드 별 body_sql_calls
    _section("[3] parse_java_file → method body_sql_calls")
    cls = parse_java_file(target)
    print(f"FQCN: {cls.get('fqcn')}")
    methods = cls.get("methods", [])
    print(f"method 수: {len(methods)}")

    # 메서드 별 sql_calls 요약
    methods_with_sql = [m for m in methods if m.get("body_sql_calls")]
    methods_no_sql = [m for m in methods if not m.get("body_sql_calls")]
    print(f"\nSQL 호출 잡힌 메서드 ({len(methods_with_sql)}):")
    for m in methods_with_sql:
        sqls = m.get("body_sql_calls", [])
        ids = [c.get("sqlid", "?") for c in sqls]
        print(f"  - {m['name']} → {len(sqls)} 건: {ids[:5]}{'…' if len(ids) > 5 else ''}")
    print(f"\nSQL 미검출 메서드 (NAMESPACE 사용 여부 표시):")
    suspicious = []
    for m in methods_no_sql:
        body = m.get("body", "")
        uses_ns = "NAMESPACE" in body
        uses_sql = any(t in body for t in ("sqlSession", "selectList", "selectOne",
                                            "update(", "insert(", "delete(", "queryFor"))
        if uses_ns and uses_sql:
            suspicious.append(m)
    if suspicious:
        print(f"⚠ NAMESPACE + sqlSession 둘 다 본문에 있는데 sql 호출 0 건인 메서드 "
              f"{len(suspicious)} 건 — 이게 진짜 누락:")
        for m in suspicious[:10]:
            print(f"  - {m['name']}")

    # 4) Focus 메서드 상세 — 인자로 명시 or 의심 메서드 첫 건 자동
    auto_focused = False
    if not focus and suspicious:
        focus = suspicious[0]["name"]
        auto_focused = True
        print(f"\n(2번째 인자 없음 → 의심 메서드 {focus!r} 로 자동 focus)")

    if focus:
        _section(f"[4] focus 메서드 상세: {focus!r}"
                 + (" (자동 선택)" if auto_focused else ""))
        target_m = next((m for m in methods if m["name"] == focus), None)
        if not target_m:
            print(f"⚠ {focus!r} 메서드 없음. 가능한 이름:")
            for m in methods[:20]:
                print(f"  - {m['name']}")
        else:
            body = target_m.get("body", "")
            print(f"body 길이: {len(body)} chars")
            print(f"body_sql_calls: {target_m.get('body_sql_calls', [])}")

            # head matches in body
            heads = list(_SQL_CALL_HEAD_RE.finditer(body))
            print(f"\nSQL_CALL_HEAD_RE body matches: {len(heads)}")
            from oracle_embeddings.legacy_java_parser import _extract_first_arg
            for hm in heads:
                paren_idx = hm.end() - 1
                first_arg = _extract_first_arg(body, paren_idx)
                print(f"  pos={hm.start()} op={hm.group('op')!r} first_arg={first_arg!r}")
                vals = _eval_string_expr(first_arg, body, ns)
                print(f"    eval → {vals}")

            # NAMESPACE / sqlSession 등장 라인 (이름에 포함된 모든 변형)
            print(f"\nNAMESPACE 등장 라인:")
            for line in body.splitlines():
                if "NAMESPACE" in line:
                    print(f"  | {line.strip()}")
            print(f"\nSqlSession 호출 후보 라인 (literal 'sqlSession'/'simpleSqlSession' 등):")
            import re as _re
            sql_re = _re.compile(r"\b\w*[Ss]ql[Ss]ession\b\.\s*\w+\s*\(")
            for line in body.splitlines():
                if sql_re.search(line):
                    print(f"  | {line.strip()}")

    _section("[5] 최종 진단")
    if not raw_matches:
        print("🔴 _NS_CONST_RE 가 매치 못함. 클래스 안의 String 상수 선언이 ")
        print("   regex 와 어긋남 (modifier / 문법). 파일 안의 NAMESPACE 선언")
        print("   한 줄을 그대로 알려주세요.")
    elif not has_namespace:
        print("🔴 _extract_ns_constants 가 NAMESPACE 를 dict 에 넣지 못함.")
        print("   raw 매치는 있지만 _NS_VALUE_RE boundary 탈락 — 값에 ")
        print("   슬래시/콜론/공백/한글 등 비허용 문자 포함 가능성.")
    elif suspicious:
        print(f"🔴 NAMESPACE 등록 OK, 하지만 NAMESPACE+sqlSession 둘 다 본문에")
        print(f"   있는 메서드 {len(suspicious)} 건의 SQL 호출이 비어있음.")
        print(f"   해당 메서드 중 하나의 이름을 두 번째 인자로 다시 실행:")
        print(f"     python probe_sql_call.py {target!r} {suspicious[0]['name']!r}")
        print(f"   [4] 섹션의 first_arg / eval 결과 보면 어디서 깨졌는지 보임.")
    elif not methods_with_sql:
        print("⚠ 어느 메서드에도 SQL 호출 검출 0. SQL receiver 가 default 에 ")
        print("  없거나 SqlSession 자동감지 실패.")
    else:
        print("✓ 검출 잘 되고 있음. 이 파일은 정상.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
