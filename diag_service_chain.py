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
  6) (확장) walker 시뮬레이션 — caller → this.callee 가 실제 resolve
     되는지 + callee 의 namespace 가 mybatis_idx 에 매칭되는지 자동 체크

사용법 (Windows PowerShell):
  python diag_service_chain.py <Service파일.java> <호출자메서드명> [<callee메서드명>] [<backend_dir>]

예시:
  python diag_service_chain.py "C:\\work\\backend\\DpPubNotiServiceImpl.java" savePubNoti saveDpPubNotiInfo
  python diag_service_chain.py "...Service.java" savePubNoti saveDpPubNotiInfo "C:\\work\\backend"

backend_dir 지정 시 MyBatis XML 까지 스캔해서 [7] 에서 namespace 매칭
자동 검증. config.yaml 의 legacy.rfc_depth 도 읽어서 함께 보고.

callee 이름 생략 시 기본값 ``saveDpPubNotiInfo``.
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
    backend_dir = sys.argv[4] if len(sys.argv) >= 5 else ""

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
    # 7) 자동 검증 — backend_dir 제공 시 namespace 매칭 + rfc_depth 직접 확인
    _section(f"[7] 자동 검증 — (b) namespace 매칭 + (c) rfc_depth")
    if not backend_dir:
        print("  backend_dir 미지정 — 4번째 인자로 backend 루트 경로 주시면")
        print("  (b)/(c) 자동 검증. 예:")
        print("    python diag_service_chain.py <.java> <caller> <callee> <backend_dir>")
    else:
        # (b) MyBatis 스캔 → namespace 인덱스 → callee 의 각 namespace 매칭 시도
        #
        # NOTE: ``parse_all_mappers`` 만 부르면 ``namespace_to_xml_files`` 가
        # 비어 있다 (그 인덱스는 ``legacy_analyzer._build_mybatis_indexes`` 가
        # 별도로 빌드함). 그래서 v6 부터는 analyzer 의 인덱스 빌더를 직접
        # 호출해서 실제 analyze-legacy 와 동일한 namespace dict 를 얻음.
        try:
            from oracle_embeddings.mybatis_parser import parse_all_mappers
            from oracle_embeddings.legacy_analyzer import (
                _match_namespace, _build_mybatis_indexes,
            )
        except Exception as e:
            print(f"  import 실패: {e}")
            return 0
        print(f"  MyBatis XML 스캔 중: {backend_dir}")
        mb_raw = parse_all_mappers(backend_dir)
        mb_idx = _build_mybatis_indexes(mb_raw)
        ns_to_xml = mb_idx.get("namespace_to_xml_files", {}) or {}
        print(f"  → {len(ns_to_xml)} 개 namespace 발견")
        callee_sql_calls = callee_m.get("body_sql_calls", []) or []
        if not callee_sql_calls:
            print(f"  callee {callee!r} 에 body_sql_calls 가 없음 — SQL 호출을 안 함")
            print(f"  → (b) 아님. (a) endpoint 체인 도달 실패 또는 다른 원인 가능성.")
        else:
            print(f"\n  (b) callee 의 SQL namespace 매칭:")
            unmatched = []
            for c in callee_sql_calls:
                ns = c.get("namespace") or ""
                sid = c.get("sql_id") or ""
                matched = _match_namespace(ns, ns_to_xml)
                flag = f"✓ matched → {matched!r}" if matched else "✗ NOT matched"
                print(f"    - ns={ns!r} sql_id={sid!r}  [{flag}]")
                if not matched:
                    unmatched.append(ns)
            if unmatched:
                print(f"\n  ⚠ 매칭 실패 namespace {len(unmatched)} 건 — 이게 원인!")
                print(f"    → (b) 확정. namespace 해석 실패 ({set(unmatched)!r})")
                print(f"    원인 가능성:")
                print(f"      * XML 의 namespace 속성이 실제 사용값과 다름 (오타)")
                print(f"      * 변수 namespace 를 2-pass 로 해석했지만 정답 이름이 XML 에 없음")
                print(f"      * MyBatis XML 이 --library-dir 같은 외부 디렉토리에 있음")
            else:
                print(f"\n  ✓ 모든 namespace 매칭 성공 — (b) 는 원인 아님.")

        # (c) config.yaml 의 legacy.rfc_depth 확인
        print(f"\n  (c) rfc_depth 설정 확인:")
        import os
        cfg_path = os.path.join(os.getcwd(), "config.yaml")
        if os.path.exists(cfg_path):
            try:
                import yaml
                with open(cfg_path, "r", encoding="utf-8") as fh:
                    cfg = yaml.safe_load(fh) or {}
                rfc_depth = ((cfg.get("legacy") or {}).get("rfc_depth"))
                if rfc_depth is None:
                    print(f"    legacy.rfc_depth 미설정 → 기본값 2 적용")
                    rfc_depth = 2
                else:
                    print(f"    legacy.rfc_depth = {rfc_depth}")
                print(f"    → self-call (this.X) 은 depth 증가 안 함. rfc_depth={rfc_depth}")
                print(f"      여도 controller→service 체인 깊이가 rfc_depth 이내면 문제없음.")
                if rfc_depth < 2:
                    print(f"    ⚠ rfc_depth 가 2 미만이면 여러 층 service 호출이 끊길 수 있음.")
            except Exception as e:
                print(f"    config.yaml 읽기 실패: {e}")
        else:
            print(f"    config.yaml 이 현재 디렉토리에 없음 → 기본값 rfc_depth=2")

    # 8) 전체 backend 인덱스 vs 단일 파일 dict 불일치 확인
    #    실제 walker 가 사용하는 services_by_fqcn 에 caller 의 class 가
    #    올바로 등재돼 있는지, 그리고 거기 methods 에 callee 가 포함돼
    #    있는지 (parse_java_file 결과와 동일한지) 검증.
    _section(f"[8] 전체 backend 인덱스 검증 — walker 가 보는 class dict")
    if not backend_dir:
        print(f"  backend_dir 미지정 — skip")
        return 0
    try:
        from oracle_embeddings.legacy_java_parser import parse_all_java
        from oracle_embeddings.legacy_analyzer import _build_indexes
    except Exception as e:
        print(f"  import 실패: {e}")
        return 0
    print(f"  전체 backend 파싱 중: {backend_dir}")
    all_classes = parse_all_java(backend_dir)
    print(f"  → {len(all_classes)} 개 class 파싱")
    indexes = _build_indexes(all_classes)
    target_fqcn = cls.get("fqcn")
    print(f"\n  단일 파일 class FQCN: {target_fqcn!r}")

    # 후보 4개 인덱스 중 어디 등재됐는지
    svc_idx = indexes.get("services_by_fqcn") or {}
    ctrl_idx = indexes.get("controllers_by_fqcn") or {}
    mapper_idx = indexes.get("mappers_by_fqcn") or {}
    by_simple = indexes.get("by_simple") or {}

    found_locations = []
    if target_fqcn in svc_idx:
        found_locations.append("services_by_fqcn")
    if target_fqcn in ctrl_idx:
        found_locations.append("controllers_by_fqcn")
    if target_fqcn in mapper_idx:
        found_locations.append("mappers_by_fqcn")
    print(f"  인덱스 등재: {found_locations or '⚠ 어디에도 없음'}")

    # walker 가 같은 methods 리스트를 보는지 (dict 교체 / 복사 버그 확인)
    idx_cls = (svc_idx.get(target_fqcn) or mapper_idx.get(target_fqcn)
               or ctrl_idx.get(target_fqcn))
    if idx_cls is None:
        print(f"  ⚠ target class 가 walker 인덱스에 없음 — walker 가 도달 불가")
        print(f"    → _build_indexes 의 stereotype 판정을 확인해야 함")
    else:
        idx_method_names = {m.get("name") for m in idx_cls.get("methods", [])}
        file_method_names = {m.get("name") for m in cls.get("methods", [])}
        missing_in_idx = file_method_names - idx_method_names
        callee_in_idx = callee in idx_method_names
        print(f"  walker 인덱스 methods 수: {len(idx_method_names)}")
        print(f"  단일파일 methods 수:       {len(file_method_names)}")
        if missing_in_idx:
            print(f"  ⚠ walker 인덱스에 누락된 method: {sorted(missing_in_idx)[:5]}")
            print(f"    → 인덱스 빌드 시 class dict 교체 / 복사 이슈 의심")
        print(f"  callee {callee!r} walker 인덱스 존재 여부: "
              f"{'✓ YES' if callee_in_idx else '✗ NO'}")
        if not callee_in_idx:
            print(f"    → 이게 원인일 가능성 높음. walker 가 callee 를 못 찾음")

    # 동일 simple name 을 가진 다른 class 가 있는지 (충돌 가능성)
    simple_name = (target_fqcn or "").rsplit(".", 1)[-1]
    same_simple = by_simple.get(simple_name) or []
    if len(same_simple) > 1:
        print(f"\n  ⚠ simple name {simple_name!r} 을 가진 class 가 여러 개: {len(same_simple)}")
        for c in same_simple:
            print(f"    - {c.get('fqcn')}")
        print(f"    → FQCN 해석이 엉뚱한 class 로 갈 위험. controller 가 어느 impl "
              f"을 쓰는지 확인 필요.")

    # 9) 실제 walker 시뮬레이션 — caller 를 synthetic endpoint 로 만들어
    #    _resolve_endpoint_chain 을 끝까지 실행하고 수집 결과 출력.
    _section(f"[9] 실제 walker 시뮬레이션 — _resolve_endpoint_chain")
    if idx_cls is None:
        print(f"  target class 가 인덱스에 없어 walker 실행 불가 — skip.")
        return 0
    try:
        from oracle_embeddings.legacy_analyzer import (
            _resolve_endpoint_chain, _build_mybatis_indexes,
            _resolve_service_impls,
        )
        from oracle_embeddings.mybatis_parser import parse_all_mappers
    except Exception as e:
        print(f"  import 실패: {e}")
        return 0

    mb_raw = parse_all_mappers(backend_dir)
    mb_idx = _build_mybatis_indexes(mb_raw)

    # _resolve_endpoint_chain 은 indexes["iface_to_impl"] 도 요구 —
    # _build_indexes 가 만들지 않고 main analyzer 흐름에서 별도로 채움.
    # diag 에서도 동일하게 만들어야 KeyError 안 남.
    if "iface_to_impl" not in indexes:
        indexes["iface_to_impl"] = _resolve_service_impls(
            indexes["services_by_fqcn"], indexes["by_simple"],
        )

    # caller 의 class 안에서 caller method 의 index 를 찾음
    caller_idx = None
    for i, m in enumerate(idx_cls.get("methods", [])):
        if m.get("name") == caller:
            caller_idx = i
            break
    if caller_idx is None:
        print(f"  ⚠ walker 인덱스 class 안에 caller {caller!r} 없음 — skip.")
        return 0

    synthetic_endpoint = {
        "method_name": caller,
        "_method_idx": caller_idx,
        "url": "/diag/sim",
        "http_method": "GET",
    }
    print(f"  synthetic endpoint 로 caller {caller!r} 를 엔드포인트처럼 walker 돌림")

    # walker 의 _find_method_in_class 를 monkey-patch 해서 호출마다 trace 로그.
    # 실제 walker 가 saveDpPubNotiInfo 를 찾으려고 시도하는지, 결과가 None
    # 인지, 시도조차 안 하는지 즉시 드러남.
    import oracle_embeddings.legacy_analyzer as _la
    _orig_find = _la._find_method_in_class
    trace_log = []
    def _traced_find(cls_dict, name):
        result = _orig_find(cls_dict, name)
        trace_log.append({
            "owner_fqcn": cls_dict.get("fqcn"),
            "method_name": name,
            "found": result is not None,
            "body_sql_count": len(result.get("body_sql_calls") or []) if result else 0,
            "body_field_count": len(result.get("body_field_calls") or []) if result else 0,
        })
        return result
    _la._find_method_in_class = _traced_find
    try:
        chain = _resolve_endpoint_chain(
            synthetic_endpoint, idx_cls, indexes, mb_idx, rfc_depth=2,
        )
    finally:
        _la._find_method_in_class = _orig_find

    print(f"\n  === walker trace ({len(trace_log)} 건의 _find_method_in_class 호출) ===")
    callee_attempts = []
    for i, t in enumerate(trace_log):
        marker = ""
        if t["method_name"] == callee:
            callee_attempts.append(t)
            marker = "  ← callee!"
        print(f"    [{i:2d}] find({t['owner_fqcn']!r:60}, {t['method_name']!r})"
              f" → {'✓' if t['found'] else '✗'}  "
              f"(sql={t['body_sql_count']}, fld={t['body_field_count']}){marker}")

    if not callee_attempts:
        print(f"\n  🔴 walker 가 callee {callee!r} 를 한 번도 찾으려 하지 않음.")
        print(f"     → caller 의 body_field_calls 안에 this.{callee} 가 있는데도 "
              f"walker 에 반영 안 됨. walker 인덱스의 caller method dict 와 "
              f"diag [3] 의 caller method dict 가 다를 가능성.")
        # caller 를 walker 인덱스에서 꺼내서 body_field_calls 직접 출력
        walker_caller = None
        for m in idx_cls.get("methods", []):
            if m.get("name") == caller:
                walker_caller = m
                break
        if walker_caller:
            wbfcs = walker_caller.get("body_field_calls", []) or []
            this_calls_walker = [c for c in wbfcs if c.get("receiver") == "this"]
            print(f"\n     walker 인덱스의 caller.body_field_calls 중 this.* 목록:")
            for c in this_calls_walker:
                m2 = " ← 있음" if c.get("method") == callee else ""
                print(f"       - this.{c.get('method')}{m2}")
            has_callee = any(c.get("method") == callee for c in this_calls_walker)
            if not has_callee:
                print(f"     ⚠ walker 인덱스에는 this.{callee} 없음!")
                print(f"       → diag [3] (단일파일) 과 walker 인덱스가 불일치.")
    else:
        for t in callee_attempts:
            if t["found"]:
                print(f"\n  🟢 walker 가 callee 를 찾음 + body_sql_calls {t['body_sql_count']} 건 로드.")
                if t['body_sql_count'] == 0:
                    print(f"     ⚠ 그러나 body_sql_calls 가 0 건. diag [2] 는 14 건이었는데 "
                          f"불일치 → walker 인덱스의 method dict 가 body_sql_calls 누락.")
            else:
                print(f"\n  🔴 walker 가 callee 를 찾으려 했는데 None 반환.")
                print(f"     → owner={t['owner_fqcn']!r} 의 methods 에 {callee!r} 없음.")

    print(f"\n  === walker 수집 결과 ===")
    print(f"  resolved_via:      {chain.get('resolved_via')!r}")
    print(f"  services:          {len(chain.get('services') or [])} 건")
    for s in (chain.get('services') or [])[:10]:
        print(f"    - {s}")
    print(f"  service_methods:   {len(chain.get('service_methods') or [])} 건")
    for sm in (chain.get('service_methods') or [])[:10]:
        print(f"    - {sm}")
    print(f"  tables:            {len(chain.get('tables') or [])} 건")
    for t in sorted(chain.get('tables') or []):
        print(f"    - {t}")
    print(f"  sql_ids:           {len(chain.get('sql_ids') or [])} 건")
    for sid in sorted(chain.get('sql_ids') or []):
        marker = "  ← callee 기인" if callee.lower() in sid.lower() else ""
        print(f"    - {sid}{marker}")
    print(f"  xml_files:         {len(chain.get('xml_files') or [])} 건")
    for xf in sorted(chain.get('xml_files') or [])[:10]:
        print(f"    - {xf}")
    print(f"  rfcs:              {len(chain.get('rfcs') or [])} 건")
    for r in sorted(chain.get('rfcs') or [])[:10]:
        print(f"    - {r}")

    # 최종 판정
    collected_anything = bool(chain.get('tables') or chain.get('sql_ids'))
    callee_related_sqls = [s for s in (chain.get('sql_ids') or [])
                           if callee.lower() in s.lower()]
    print(f"\n  === 최종 판정 ===")
    if not collected_anything:
        print(f"  ⚠ walker 가 아무것도 수집 못함. caller body 자체가 SQL/field_calls 없음.")
    elif callee_related_sqls:
        print(f"  ✓ callee 기인 sql_id {len(callee_related_sqls)} 건 수집됨 — walker 정상.")
        print(f"    → analyze-legacy 결과의 Programs 시트에 원래 나왔어야 함.")
        print(f"      실제로 해당 endpoint 행이 다른 controller 에 속하는지 확인 필요.")
    else:
        print(f"  ⚠ tables/sql_ids 는 수집했지만 callee {callee!r} 기인한 sql_id 는 없음.")
        print(f"    → callee 의 body_sql_calls 가 수집되지 못한 것. [2] 카운트 재확인.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
