"""Saga chain URL 추적 진단 — 사용자 환경 단방향이라 1줄 결론 + 옵션 상세.

사용:
  python diag_saga_chain.py --frontend-dir <react-root> --handler search

Optional:
  --screen-file <path/to/index.js>   handler 가 정의된 파일 (없으면 walk
                                     으로 첫 매칭 파일 사용)
  --verbose                          단계별 상세 dump

체인:
  1. handler body 찾기 (같은 파일)
  2. body 안 ``this.props.X`` / ``dispatch(actions.Y)`` / fn 호출 식별
  3. ``mapDispatchToProps`` 의 ``X: ...actions.Y`` 매핑으로 action 추출
  4. ``actions.js`` 의 ``export const Y = ...({type: KEY, ...})`` 에서 KEY
  5. ``saga.js`` 의 ``takeLatest(KEY, sagaFn)`` 에서 sagaFn
  6. sagaFn body 의 URL literal

마지막 단계까지 따라간 결과 1줄로 emit. 중간 끊기면 어디서 끊겼는지
화살표 표시.
"""

from __future__ import annotations

import argparse
import os
import sys


def main() -> int:
    ap = argparse.ArgumentParser(description="Saga chain URL 추적 진단")
    ap.add_argument("--frontend-dir", required=True,
                    help="React 소스 root (재귀)")
    ap.add_argument("--handler", required=True,
                    help="진단할 handler 함수 이름 (예: search)")
    ap.add_argument("--screen-file",
                    help="handler 가 정의된 파일 (생략 시 자동 탐색)")
    ap.add_argument("--verbose", action="store_true",
                    help="단계별 상세 dump")
    args = ap.parse_args()

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from oracle_embeddings.legacy_react_api_scanner import (
        _scan_dir, _strip_comments, _read_file_safe,
        _collect_function_bodies, _collect_action_to_type,
        _collect_saga_urls_by_action_type, _collect_mdtp_action_map,
        _locate_handler_body, _locate_handler_start,
        _MDTP_KV_RE, _THIS_PROPS_CALL_LEAF_RE, _DISPATCH_ACTION_RE,
        _EXPORT_ACTION_RE, _ACTION_TYPE_RE, _SAGA_TAKE_RE,
        _URL_LITERAL_IN_BODY_RE, _slice_function_body, _FN_CALL_LEAF_RE,
        _build_call_regex, _DEFAULT_API_METHODS, _collect_url_constants,
        _resolve_saga_urls_for_handler,
        _extract_destructured_props, _extract_proptypes_names,
    )

    fe_dir = os.path.abspath(args.frontend_dir)
    if not os.path.isdir(fe_dir):
        print(f"✗ frontend-dir not found: {fe_dir}")
        return 2

    print(f"frontend-dir: {fe_dir}")
    all_files = _scan_dir(fe_dir)
    print(f"scanned: {len(all_files)} files")

    # ── 1. handler 가 정의된 파일 찾기 ─────────────────────────────
    handler = args.handler
    screen_file = args.screen_file and os.path.abspath(args.screen_file)
    if not screen_file:
        for fp in all_files:
            try:
                content = _strip_comments(_read_file_safe(fp))
            except Exception:
                continue
            if _locate_handler_start(content, handler) is not None:
                screen_file = fp
                break
    if not screen_file:
        print(f"✗ Step 1: handler '{handler}' 정의 못 찾음 (전체 {len(all_files)} 파일 스캔)")
        return 1

    rel_screen = os.path.relpath(screen_file, fe_dir)
    content = _strip_comments(_read_file_safe(screen_file))
    body = _locate_handler_body(content, handler)
    body_lines = body.count("\n") + 1 if body else 0
    print(f"✓ Step 1: handler '{handler}' → {rel_screen} ({body_lines}줄)")
    if args.verbose:
        print(f"  body[:300]: {body[:300]!r}")

    # ── 2. body 안 직접/간접 호출 ──────────────────────────────────
    direct_actions = [m.group("act") for m in _DISPATCH_ACTION_RE.finditer(body)]
    props_keys = set(m.group(1) for m in _THIS_PROPS_CALL_LEAF_RE.finditer(body))
    # destructure + propTypes 도 props_key 후보로 (resolver 와 동일 로직).
    destructured = _extract_destructured_props(content)
    proptypes = _extract_proptypes_names(content)
    prop_candidates = destructured | proptypes
    direct_call_props: set[str] = set()
    if prop_candidates:
        for m in _FN_CALL_LEAF_RE.finditer(body):
            fn = m.group("fn") or ""
            if fn in prop_candidates:
                direct_call_props.add(fn)
                props_keys.add(fn)

    print(f"  · this.props.X() 호출: {sorted([k for k in props_keys if k not in direct_call_props]) or '(없음)'}")
    print(f"  · 직접 dispatch(actions.Y): {direct_actions or '(없음)'}")
    print(f"  · destructure ({{X}} = this.props) 후 직접 호출: "
          f"{sorted([k for k in direct_call_props if k in destructured]) or '(없음)'}")
    print(f"  · propTypes 선언 + 직접 호출: "
          f"{sorted([k for k in direct_call_props if k in proptypes and k not in destructured]) or '(없음)'}")
    if args.verbose:
        print(f"  · destructured set ({len(destructured)}): {sorted(destructured)[:10]}{'...' if len(destructured) > 10 else ''}")
        print(f"  · propTypes set ({len(proptypes)}): {sorted(proptypes)[:10]}{'...' if len(proptypes) > 10 else ''}")

    if not direct_actions and not props_keys:
        print(f"✗ Step 2: body 에 redux dispatch / this.props 호출 없음 + destructure/propTypes 매칭 0 — "
              f"saga chain 시작점 없음 (handler 가 axios 를 직접 호출하지 않으면 URL 추적 불가)")
        # 사용자 진단 단서: body 안 모든 fn 호출 leaf
        all_calls = sorted(set(m.group("fn") for m in _FN_CALL_LEAF_RE.finditer(body) if m.group("fn")))
        if all_calls:
            print(f"   → body 안 fn 호출 전체: {all_calls[:8]}{'...' if len(all_calls) > 8 else ''}")
            print(f"   → 위 함수 중 prop 인 것이 있다면 destructure / propTypes 선언이 못 잡힌 형태 (functional"
                  f" component arg 분해? render prop?). --verbose 로 raw body 확인.")
        return 1
    print(f"✓ Step 2: trigger 시작점 식별")

    # ── 3. mapDispatchToProps 매핑 ────────────────────────────────
    mdtp_pairs = [(m.group("key"), m.group("act")) for m in _MDTP_KV_RE.finditer(content)]
    matched_actions = set(direct_actions)
    for key, act in mdtp_pairs:
        if key in props_keys:
            matched_actions.add(act)
    if args.verbose:
        print(f"  · 파일 안 mDTP pairs 전체: {mdtp_pairs}")
    print(f"  · props_keys → action_name 해결: {[(k,a) for k,a in mdtp_pairs if k in props_keys] or '(없음)'}")

    if props_keys and not any(k in props_keys for k, _ in mdtp_pairs):
        print(f"⚠ Step 3: this.props.{props_keys} 가 mapDispatchToProps 에 없음.")
        print(f"   → bindActionCreators 패턴이거나, 이름이 다른 형태 (예: 'actionCreator: action.Y' 가 아닌 'actionCreator: dispatch(...)' 만 있는 경우).")

    print(f"✓ Step 3: candidate action 이름 = {sorted(matched_actions) or '(없음)'}")

    # ── 4. action_to_type 매핑 ─────────────────────────────────────
    action_to_type = _collect_action_to_type(all_files)
    print(f"  · 전체 action_to_type 매핑 수: {len(action_to_type)}")
    if args.verbose:
        for a in sorted(matched_actions):
            tks = action_to_type.get(a, set())
            print(f"  · {a} → types: {sorted(tks) if tks else '(없음)'}")

    type_keys: set[str] = set()
    missing_in_actions = []
    for a in matched_actions:
        tks = action_to_type.get(a)
        if tks:
            type_keys |= tks
        else:
            missing_in_actions.append(a)
    if missing_in_actions:
        print(f"⚠ Step 4: 다음 action 의 type: 값을 actions.js 에서 못 찾음: {missing_in_actions}")
        print(f"   → actions.js 의 export 패턴 또는 'type:' prefix 가 비표준일 수 있음 (현재 지원: literal | constants.X | actionTypes.X | types.X | ActionTypes.X | ActionType.X | UPPER_SNAKE).")
        # 비슷한 이름 후보
        for a in missing_in_actions:
            cands = [k for k in action_to_type if a.lower() in k.lower() or k.lower() in a.lower()]
            if cands:
                print(f"   → '{a}' 와 유사한 이름 후보: {cands[:5]}")
    if not type_keys:
        print(f"✗ Step 4: action_to_type 빈 결과 — 체인 끊김")
        return 1
    print(f"✓ Step 4: action → type 매핑 = {sorted(type_keys)}")

    # ── 5. saga URL by type ───────────────────────────────────────
    call_re = _build_call_regex(list(_DEFAULT_API_METHODS))
    const_map = _collect_url_constants(all_files, [])
    fn_index = _collect_function_bodies(all_files)
    saga_urls = _collect_saga_urls_by_action_type(
        all_files, fn_index, call_re, const_map, strip_patterns=None,
    )
    print(f"  · 전체 saga_urls_by_type 매핑 수: {len(saga_urls)}")
    if args.verbose:
        for tk in sorted(type_keys):
            print(f"  · {tk} → urls: {saga_urls.get(tk, set())}")

    final_urls = set()
    missing_in_saga = []
    for tk in type_keys:
        urls = saga_urls.get(tk)
        if urls:
            final_urls |= urls
        else:
            missing_in_saga.append(tk)
    if missing_in_saga:
        print(f"⚠ Step 5: 다음 type 의 saga 매핑을 못 찾음: {missing_in_saga}")
        print(f"   원인 후보:")
        print(f"   (a) saga 파일이 frontend-dir 밖에 있음 (모노레포 분리)")
        print(f"   (b) takeLatest/takeEvery 가 비표준 (saga middleware fork 패턴, channel 등)")
        print(f"   (c) saga 함수 body 에 URL literal 가 변수 / 함수 호출로 되어 있어 quoted '/path' 매칭 실패")
        print(f"   (d) saga 가 'function*' 이 아닌 다른 형태")

    # ── 6. 최종 통합 resolver ─────────────────────────────────────
    mdtp_map = _collect_mdtp_action_map(all_files)
    resolved = _resolve_saga_urls_for_handler(
        body, content, action_to_type, saga_urls,
        fn_index=fn_index, mdtp_map=mdtp_map, depth=3,
    )

    print()
    if resolved:
        print(f"✓✓✓ 최종: handler '{handler}' → URL = {sorted(resolved)}")
        return 0
    elif final_urls:
        print(f"⚠ 부분 성공: action_to_type / saga_urls 는 채워졌으나 resolver 통합에서 URL 0건.")
        print(f"   → handler body 가 candidate props_key 와 안 맞는 형태일 수 있음 (chain follow depth 부족 등).")
        return 1
    else:
        print(f"✗✗✗ 최종: URL 0건 — 위 단계 중 어디서 끊겼는지 확인 후 수정 필요.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
