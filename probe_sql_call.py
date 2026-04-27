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

import re
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

    # method body 범위 overlap 체크 — brace walker 가 경계 잘못 잡으면
    # 한 메서드의 body 가 다른 메서드 영역을 침범. 사용자 가설:
    # '같은 변수가 여러 service 메서드에 각자 선언돼있는데 위 메서드
    # 영역이 아래로 흘러내려서 sqlId assignment 가 헷갈림' — overlap
    # 발견되면 즉시 의심.
    overlaps = []
    sorted_methods = sorted(methods, key=lambda m: m.get("body_start", 0))
    for i in range(len(sorted_methods) - 1):
        a = sorted_methods[i]
        b = sorted_methods[i + 1]
        a_end = a.get("body_end", 0)
        b_start = b.get("body_start", 0)
        if a_end > b_start:
            overlaps.append((a, b))
    if overlaps:
        print(f"\n🔴 method body 경계 overlap {len(overlaps)} 건 — brace walker 의심:")
        for a, b in overlaps:
            print(f"  - {a.get('name')!r} body_end={a.get('body_end')} > "
                  f"{b.get('name')!r} body_start={b.get('body_start')} "
                  f"(겹침 {a.get('body_end') - b.get('body_start')} chars)")
        print(f"  → A 메서드 body 가 B 메서드 영역까지 침범. A 의 body_sql_calls "
              f"가 B 의 ``sqlId = ...`` 까지 잘못 포함하는 것이 원인.")
    else:
        print(f"\n✓ method body 경계 overlap 없음")

    # 추가로 같은 변수명 (sqlId 등) 가 여러 메서드에 등장하는지 통계
    multi_var = {}
    for m in methods:
        body_x = m.get("body", "")
        for v in re.findall(r'\bString\s+(\w+)\s*=', body_x):
            multi_var.setdefault(v, []).append(m.get("name"))
    shared = {v: ms for v, ms in multi_var.items() if len(ms) > 1}
    if shared:
        print(f"\n동일 변수명 여러 메서드에 선언:")
        for v, ms in list(shared.items())[:8]:
            print(f"  - {v!r}: {ms[:5]}{'…' if len(ms) > 5 else ''}")

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
            print(f"body_sql_calls (parse_java_file 결과): "
                  f"{target_m.get('body_sql_calls', [])}")

            # [3] 과 [4] 결과 일치 여부 검증 — 같은 body + ns 로 직접
            # _collect_body_sql_calls 호출. 만약 결과가 다르면 parse_java_file
            # 의 body / ns_constants 가 우리가 보는 것과 다르다는 의미.
            from oracle_embeddings.legacy_java_parser import (
                _collect_body_sql_calls as _direct_collect,
            )
            direct = _direct_collect(body, ns)
            print(f"\n_collect_body_sql_calls 직접 재호출 (같은 body/ns):")
            for c in direct:
                print(f"  → {c.get('sqlid')!r}")
            if not direct:
                print(f"  → (empty)")
            if direct != target_m.get("body_sql_calls", []):
                # [3] 결과와 다르면 매우 의심스러움
                same_count = (len(direct) == len(target_m.get("body_sql_calls", [])))
                print(f"  ⚠ [3] 결과와 다름! parse_java_file 이 다른 ns_constants 또는 "
                      f"per-file head_re 를 사용하는 것이 원인 (per-file SqlSession "
                      f"detection 영향 가능성).")

            # head matches in body
            heads = list(_SQL_CALL_HEAD_RE.finditer(body))
            print(f"\nSQL_CALL_HEAD_RE body matches: {len(heads)}")
            from oracle_embeddings.legacy_java_parser import _extract_first_arg
            first_arg_vars = []  # 1st arg 가 식별자인 케이스 모음
            import re as _re
            for hm in heads:
                paren_idx = hm.end() - 1
                first_arg = _extract_first_arg(body, paren_idx)
                print(f"  pos={hm.start()} op={hm.group('op')!r} first_arg={first_arg!r}")
                vals = _eval_string_expr(first_arg, body, ns)
                print(f"    eval → {vals}")
                if _re.fullmatch(r"[A-Za-z_]\w*", first_arg or ""):
                    first_arg_vars.append(first_arg)

            # 식별자 first_arg 마다 body 내 모든 assignment 분해 출력
            for var in first_arg_vars:
                print(f"\n[4-{var}] body 내 ``{var} = ...;`` 전체 매칭:")
                assign_re = _re.compile(rf"\b{_re.escape(var)}\s*=\s*([^;]+);")
                hits = list(assign_re.finditer(body))
                print(f"  매치 수: {len(hits)}")
                for am in hits:
                    rhs = am.group(1).strip()
                    sub_eval = _eval_string_expr(rhs, body, ns)
                    line_no = body[:am.start()].count("\n") + 1
                    print(f"  - line +{line_no}: rhs={rhs!r}")
                    print(f"      sub_eval → {sub_eval}")
                    # sub_eval 빈 결과면 원인 진단 — RHS 의 첫 16 글자
                    # codepoint 덤프 (smart quotes / BOM / 한글 등 히든 문자
                    # 감지) + 식별자 reference 인 경우 ns_constants 룩업 결과
                    if not sub_eval:
                        print(f"      ⚠ 빈 결과. RHS 분석:")
                        cps = " ".join(f"U+{ord(c):04X}" for c in rhs[:16])
                        print(f"        - 첫 16자 codepoint: {cps}")
                        # ASCII " 는 U+0022. smart " 는 U+201C / U+201D.
                        if any(ord(c) in (0x201C, 0x201D, 0x2018, 0x2019)
                               for c in rhs):
                            print(f"        - 🔴 smart quote 감지 (U+201C/D 또는 U+2018/9)")
                            print(f"          → 소스 파일에서 ASCII \" / ' 로 교체 필요")
                        if rhs.startswith('﻿') or '﻿' in rhs[:5]:
                            print(f"        - 🔴 BOM (U+FEFF) 감지")
                        # bare 식별자라면 ns_constants 에 있는지 명시적 출력
                        import re as _re2
                        if _re2.fullmatch(r"[A-Za-z_]\w*", rhs):
                            print(f"        - 형태: bare 식별자")
                            print(f"        - ns_constants[{rhs!r}]: "
                                  f"{ns.get(rhs, '<없음>')!r}")
                            # body 안에 이 이름의 assignment 가 있나
                            sub_assign = _re2.compile(rf"\b{_re2.escape(rhs)}\s*=\s*[^;]+;")
                            sub_hits = list(sub_assign.finditer(body))
                            print(f"        - body 내 ``{rhs} = ...;`` 매치: "
                                  f"{len(sub_hits)} 건")

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
