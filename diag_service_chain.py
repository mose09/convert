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

사용법 (Windows PowerShell):
  python diag_service_chain.py <Service파일.java> <호출자메서드명> [<callee메서드명>]

예시:
  python diag_service_chain.py "C:\\work\\backend\\DpPubNotiServiceImpl.java" savePubNoti saveDpPubNotiInfo

callee 이름 생략 시 기본값 ``saveDpPubNotiInfo`` — 사용자 케이스 재현용.
"""
import sys

from oracle_embeddings.legacy_java_parser import parse_java_file


def _section(title: str) -> None:
    print("\n" + "=" * 60)
    print(title)
    print("=" * 60)


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
        else:
            excerpt = caller_body[max(0, idx - 40):idx + 80]
            print(f"  → body 위치 {idx} 에 있음. 주변:\n    {excerpt!r}")
            print("    _FIELD_CALL_RE 가 이 패턴을 못 잡음 — regex 확장 필요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
