"""심층 진단: ServiceImpl 내부 this.* 자기호출 체인이 끊기는 원인 확인.

사용자 증상: ``Map<String, Object> saveResult = this.saveDpPubNotiInfo(param);``
같은 자기호출이 analyze-legacy 결과에 반영되지 않고, 그 안에서 호출한
다른 서비스 메서드까지 줄줄이 실종되는 경우.

원인 후보 (위에서부터 체크):
  1) 파서가 callee 선언부 매칭을 못해 ``methods`` 에 등재 안 됨
     → _METHOD_SIG_RE 확장 필요
  2) callee 의 body 범위 추출이 망가짐 (body 길이 0 또는 너무 짧음)
     → balanced-brace walker 버그
  3) caller 의 body_field_calls 에 ``this.saveDpPubNotiInfo`` 자체가 없음
     → _FIELD_CALL_RE 가 놓침 (복합 선언문 / 멀티라인 등)
  4) body_field_calls 에 있는데 analyzer 의 체인 walker 가
     ``_find_method_in_class`` 에서 실패 → 다른 클래스 / 상속 관계
  5) (확장) caller body 가 balanced-brace walker 에서 조기 종료 —
     호출 라인 자체가 body 에 안 포함됨. cut-off 위치 주변의
     char literal / 텍스트 블록 등이 원인일 가능성 → [5] 섹션에서
     cut-off 주변 context 출력.

사용법 (Windows PowerShell):
  python diag_service_chain.py <Service파일.java> <호출자메서드명> [<callee메서드명>]

예시:
  python diag_service_chain.py "C:\\work\\backend\\DpPubNotiServiceImpl.java" savePubNoti saveDpPubNotiInfo

callee 이름 생략 시 기본값 ``saveDpPubNotiInfo`` — 사용자 케이스 재현용.
"""
import sys

from oracle_embeddings.legacy_java_parser import parse_java_file
from oracle_embeddings.mybatis_parser import _read_file_safe


def _section(title: str) -> None:
    print("\n" + "=" * 60)
    print(title)
    print("=" * 60)


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1 if pos >= 0 else -1


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    target = sys.argv[1]
    caller = sys.argv[2]
    callee = sys.argv[3] if len(sys.argv) >= 4 else "saveDpPubNotiInfo"

    cls = parse_java_file(target)
    if not cls:
        print(f"(파서가 클래스를 찾지 못함: {target})")
        print("→ 경로 / 파일 내용 확인. class 선언이 없거나 인코딩 문제일 수 있음.")
        return 1

    print(f"class:   {cls.get('fqcn')}")
    print(f"kind:    {cls.get('kind')}")
    print(f"extends: {cls.get('extends')!r}")
    methods = cls.get("methods", [])
    print(f"\n--- {len(methods)} methods parsed ---")
    for m in methods:
        print(f"  - {m.get('name')}")

    method_by_name = {m.get("name"): m for m in methods}

    # 1) callee 가 파서에 등재됐는지
    _section(f"[1] callee 파서 등재 확인: {callee!r}")
    callee_m = method_by_name.get(callee)
    if callee_m is None:
        print(f"⚠ {callee!r} 가 methods 에 없음 — _METHOD_SIG_RE 매칭 실패.")
        print("  → 해당 메서드 선언부 한 줄 (annotation 포함) 공유 부탁드립니다.")
        return 0
    print(f"✓ 등재됨")
    body = callee_m.get("body", "") or ""
    print(f"  body 길이: {len(body)} chars")
    if not body:
        print("⚠ body 가 비어있음 — balanced-brace walker 실패 가능성.")
    else:
        print(f"  body 앞 160자:\n    {body[:160]!r}")
        print(f"  body 끝 80자:\n    ...{body[-80:]!r}")

    # 2) callee 내부의 body_* 수집 결과
    _section(f"[2] callee 내부 호출/SQL/RFC 수집: {callee!r}")
    fcs = callee_m.get("body_field_calls", []) or []
    sqls = callee_m.get("body_sql_calls", []) or []
    rfcs = callee_m.get("body_rfc_calls", []) or []
    print(f"body_field_calls ({len(fcs)}):")
    for c in fcs:
        print(f"  - {c.get('receiver')}.{c.get('method')}")
    print(f"body_sql_calls   ({len(sqls)}):")
    for c in sqls:
        print(f"  - {c}")
    print(f"body_rfc_calls   ({len(rfcs)}):")
    for c in rfcs:
        print(f"  - {c}")

    # 3) caller 가 this.callee 를 잡고 있는지
    _section(f"[3] caller 의 this.{callee} 호출 감지: {caller!r}")
    caller_m = method_by_name.get(caller)
    if caller_m is None:
        print(f"⚠ caller {caller!r} 도 methods 에 없음.")
        return 0
    caller_body = caller_m.get("body", "") or ""
    this_calls = [c for c in caller_m.get("body_field_calls", []) or []
                  if c.get("receiver") == "this"]
    print(f"body 길이: {len(caller_body)} chars")
    print(f"this.* 호출 {len(this_calls)} 건:")
    for c in this_calls:
        mark = "  ← 문제의 호출" if c.get("method") == callee else ""
        print(f"  - this.{c.get('method')}{mark}")

    if any(c.get("method") == callee for c in this_calls):
        print(f"\n✓ caller body_field_calls 에 this.{callee} 감지됨.")
        print("  → 원인은 analyzer 체인 walker 또는 _find_method_in_class 쪽.")
        print("    (보고 시: 이 결과 + analyze-legacy 콘솔 로그 함께 공유 부탁)")
    else:
        print(f"\n⚠ this.{callee} 가 body_field_calls 에 없음!")
        idx = caller_body.find(callee)
        if idx < 0:
            print(f"  → caller body 에 {callee!r} 문자열 자체가 없음.")
            print(f"    body 추출 범위가 잘못됐을 가능성 (balanced-brace walker).")
            print(f"    [5] 섹션에서 cut-off 위치 주변 context 확인.")
        else:
            excerpt = caller_body[max(0, idx - 40):idx + 80]
            print(f"  → body 위치 {idx} 에 있음. 주변:\n    {excerpt!r}")
            print("    _FIELD_CALL_RE 가 이 패턴을 못 잡음 — regex 확장 필요.")

    # 4) 파일 전체에서 callee 문자열 위치 vs caller body 범위 비교
    _section(f"[4] 원본 파일 전체에서 {callee!r} 문자열 위치 탐색")
    raw = _read_file_safe(target)
    # caller body_* 인덱스는 파서가 가지고 있음
    body_start = caller_m.get("body_start", -1)
    body_end = caller_m.get("body_end", -1)
    sig_start = caller_m.get("sig_start", -1)
    print(f"caller sig_start = {sig_start} (line {_line_of(raw, sig_start)})")
    print(f"caller body_start = {body_start} (line {_line_of(raw, body_start)})")
    print(f"caller body_end   = {body_end} (line {_line_of(raw, body_end)})")
    print(f"caller body size = {body_end - body_start if body_end > body_start else 0} chars")

    positions = []
    start = 0
    while True:
        p = raw.find(callee, start)
        if p < 0:
            break
        positions.append(p)
        start = p + len(callee)
    print(f"\n파일 전체에서 {callee!r} 발견 {len(positions)} 건:")
    for p in positions:
        line = _line_of(raw, p)
        in_caller = (body_start <= p < body_end) if body_end > 0 else False
        tag = "✓ caller body 안" if in_caller else "✗ caller body 밖"
        excerpt = raw[max(0, p - 30):p + 50].replace("\n", " ⏎ ")
        print(f"  - pos {p} (line {line}) [{tag}]")
        print(f"    {excerpt!r}")
        # "caller body 밖" 인 경우 — 실제로는 어느 메서드 body 안인지 탐색
        if not in_caller:
            owning = None
            for m in methods:
                mbs = m.get("body_start", -1)
                mbe = m.get("body_end", -1)
                if mbs <= p < mbe:
                    owning = m
                    break
            if owning:
                print(f"    → 실제로는 {owning.get('name')!r} 의 body 안 "
                      f"(body_start={owning.get('body_start')} "
                      f"line {_line_of(raw, owning.get('body_start', 0))}, "
                      f"body_end={owning.get('body_end')} "
                      f"line {_line_of(raw, owning.get('body_end', 0))})")
                print(f"    ⚠ caller 이름이 잘못 지정됐을 수 있음 — 실제 caller는 "
                      f"{owning.get('name')!r} 로 추정")
            else:
                print(f"    → 어떤 메서드 body 에도 속하지 않음. "
                      f"brace walker 로 인해 range 가 전체적으로 꼬였거나 "
                      f"inner class / lambda 내부일 가능성.")

    # 5) body cut-off 위치 주변 context
    _section(f"[5] caller body 끝 지점 주변 — balanced-brace 조기 종료 의심")
    if body_end <= 0 or body_end > len(raw):
        print("  body_end 값이 비정상 — skip.")
    else:
        # body_end 직전 300자 + body_end 직후 300자
        before = raw[max(0, body_end - 300):body_end]
        after = raw[body_end:min(len(raw), body_end + 300)]
        print(f"body_end={body_end} 직전 300자:")
        print("─" * 40)
        print(before)
        print("─" * 40)
        print(f"body_end={body_end} 직후 300자 (← 이 구간에 호출이 있으면 body 잘림 확정):")
        print("─" * 40)
        print(after)
        print("─" * 40)
        # 잘린 경계 직전의 suspicious 문자 패턴 확인
        hints = []
        tail = before[-60:]
        if '"""' in tail or '"""' in after[:60]:
            hints.append("텍스트 블록 (Java 15+) 의심 — walker 가 지원 안 함")
        if tail.count("'") % 2 != 0:
            hints.append("홀수 개 char literal — 이스케이프 misparse 가능성")
        if "\\u" in tail:
            hints.append("유니코드 이스케이프 (\\uXXXX) — walker 가 지원 안 함")
        if hints:
            print("\n의심 패턴:")
            for h in hints:
                print(f"  - {h}")
        else:
            print("\n특별한 의심 패턴은 자동 감지되지 않음 — 실제 context 를 공유해주세요.")

    # 6) walker 시뮬레이션 — [3] 에서 감지됐다면 실제 _find_method_in_class 가
    #    동작하는지, 그리고 callee body 의 SQL/RFC 가 수집되는지 확인
    _section(f"[6] walker 시뮬레이션 — caller → this.{callee} 해석 + 수집")
    try:
        from oracle_embeddings.legacy_analyzer import (
            _find_method_in_class as _la_find,
            _collect_body_calls as _la_collect,
        )
    except Exception as e:
        print(f"  import 실패: {e} — skip.")
    else:
        # caller body_field_calls 순회하며 각 호출이 resolve 되는지 체크
        print(f"caller {caller!r} 의 body_field_calls 해석:")
        bfcs = caller_m.get("body_field_calls", []) or []
        if not bfcs:
            print("  (body_field_calls 없음)")
        for fc in bfcs:
            recv = fc.get("receiver", "")
            meth = fc.get("method", "")
            if recv != "this":
                print(f"  - {recv}.{meth} (cross-class — 이 진단은 self-call 에 집중)")
                continue
            resolved = _la_find(cls, meth)
            flag = "✓ resolve OK" if resolved else "✗ NOT FOUND in cls.methods"
            mark = " ← 문제의 호출" if meth == callee else ""
            print(f"  - this.{meth}{mark}  [{flag}]")
            if resolved is None:
                print(f"    → 이게 None 이 나오면 cls['methods'] 와 비교해서 이름 불일치 확인")
                print(f"    methods 이름 목록에 {meth!r} 가 있나? "
                      f"{meth in {m.get('name') for m in cls.get('methods', [])}}")

        # callee body 의 SQL 이 실제로 collect 되는지 (mybatis_idx 없어도
        # raw body_sql_calls 는 확인 가능)
        print(f"\ncallee {callee!r} 의 raw body_sql_calls 재확인:")
        for c in callee_m.get("body_sql_calls", []) or []:
            ns = c.get("namespace") or ""
            sid = c.get("sql_id") or ""
            print(f"  - namespace={ns!r} sql_id={sid!r}")
        print(f"\n→ 위 목록이 존재하면서도 analyze-legacy 결과에 반영 안 된다면:")
        print(f"  (a) endpoint 체인이 caller 까지 닿지 못함 (중간 경로 단절)")
        print(f"  (b) callee 의 namespace 가 mybatis_idx 에 매칭 안 됨 "
              f"(_match_namespace 실패 — 로그에서 'Namespace not matched' 확인)")
        print(f"  (c) depth ≥ rfc_depth 로 인해 탐색 중단 (config.yaml legacy.rfc_depth 확인)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
