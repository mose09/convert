#!/usr/bin/env python3
"""사용자 PC 에서 한 번 실행 — main file 안 handler 정의 + URL 호출
패턴을 자동 분류해서 1~2줄 결론 emit.

usage:
  # mode 1: handler 정의 자동 분류
  python diag_handler_resolve.py <main_file> <handler_name>

  # mode 2: closure 빌드 + popup/흡수 시뮬레이션 (전체 파이프라인)
  python diag_handler_resolve.py --simulate <main_file> <frontend_root> <handler>

예시:
  python diag_handler_resolve.py /workspace/frontend/.../MaterialMaster/index.js handleSearch
  python diag_handler_resolve.py --simulate \\
    /workspace/frontend/apps/X/MaterialMaster/index.js \\
    /workspace/frontend handleSearch

CLAUDE.md 컨벤션 (단방향 전송 환경) — 짧은 결론만 emit, 사용자가 수기
타이핑할 수 있게 self-classify. 출력 한 줄을 알려주면 됩니다.
"""
import os
import re
import sys

# repo root 를 sys.path 에 추가 (스크립트 위치 기준)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from oracle_embeddings.legacy_react_api_scanner import (
        _locate_handler_start, _slice_function_body,
    )
    from oracle_embeddings.legacy_screen_extractor import (
        _extract_main_prop_handler_map, _resolve_main_handler_urls,
        _resolve_props_callback_url, _collect_popup_url_map,
    )
except ImportError as e:
    print(f"✗ import 실패 — repo 루트에서 실행하세요: {e}")
    sys.exit(1)


def _simulate(main_path: str, frontend_root: str, handler: str) -> None:
    """closure 빌드 + popup 분류 + 흡수 시뮬레이션 1~2줄 결론."""
    try:
        from oracle_embeddings.legacy_react_closure import build_closure
        from oracle_embeddings.screen_spec.extractors import (
            find_popup_files_in_closure,
        )
    except Exception as e:
        print(f"✗ closure 모듈 import 실패: {e}")
        return
    try:
        closure = build_closure(
            entry_file=main_path, repo_root=frontend_root,
            max_depth=3, token_budget=12000,
        )
    except Exception as e:
        print(f"✗ closure 빌드 실패: {e}")
        return
    if closure is None or not closure.files:
        print(f"✗ closure 비어있음 (tree-sitter 미설치?)")
        return
    try:
        with open(main_path, encoding="utf-8") as f:
            main_content = f.read()
    except Exception as e:
        print(f"✗ main 파일 못 읽음: {e}")
        return
    popup_abs_set = find_popup_files_in_closure(closure)
    main_abs = str(closure.entry_file)
    main_prop_map = _extract_main_prop_handler_map(main_content)

    # closure 의 sub 파일들 (main, popup 제외) 에서 handler 이름 매칭
    sub_files = [str(f.abs_path) for f in closure.files
                 if str(f.abs_path) != main_abs
                 and str(f.abs_path) not in popup_abs_set]
    popup_files = [str(p) for p in popup_abs_set]

    # 어느 closure file 에 handler 가 등장하나?
    handler_in_sub = []
    handler_in_popup = []
    for p in sub_files:
        try:
            with open(p, encoding="utf-8") as f:
                if re.search(rf"\b{re.escape(handler)}\b", f.read()):
                    handler_in_sub.append(os.path.basename(os.path.dirname(p)) or os.path.basename(p))
        except Exception:
            continue
    for p in popup_files:
        try:
            with open(p, encoding="utf-8") as f:
                if re.search(rf"\b{re.escape(handler)}\b", f.read()):
                    handler_in_popup.append(os.path.basename(os.path.dirname(p)) or os.path.basename(p))
        except Exception:
            continue

    # 시뮬레이션 흡수: sub_files 에 대해 _collect_popup_url_map + resolve
    absorbed = 0
    resolved = 0
    resolved_for_handler = False
    for p in sub_files:
        sub_events = _collect_popup_url_map(p)
        for k, v in sub_events.items():
            sub_handler = v.pop("_handler", "")
            if not v.get("urls") and sub_handler:
                urls = _resolve_props_callback_url(
                    sub_handler, main_prop_map, {}, main_content=main_content
                )
                if urls:
                    resolved += 1
                    if handler in sub_handler or sub_handler == handler:
                        resolved_for_handler = True
            absorbed += 1

    print(f"closure: total={len(closure.files)}, "
          f"sub_files={len(sub_files)}, popup_files={len(popup_files)}")
    print(f"  '{handler}' 등장: sub={handler_in_sub or '(없음)'}, "
          f"popup={handler_in_popup or '(없음)'}")
    print(f"  main_prop_map: {dict(list(main_prop_map.items())[:3])}"
          + (f" (+{len(main_prop_map)-3})" if len(main_prop_map) > 3 else ""))
    if resolved_for_handler:
        print(f"✓ 흡수 시뮬: absorbed={absorbed}, resolved={resolved} "
              f"(그 중 '{handler}' 매칭 ✓) — 실제 분석이 0 인 건 main "
              f"화면이 enumerate 안 됐을 가능성")
    elif handler_in_popup and not handler_in_sub:
        print(f"⚠ '{handler}' 가 popup 파일에만 등장 — popup 처리 단계의 "
              f"`_handler` 제거로 resolve skip. fix 필요.")
    elif resolved == 0 and absorbed > 0:
        print(f"⚠ 흡수 {absorbed}건, resolve 0 — main_prop_map / fallback 3 "
              f"매칭 실패. handler 등장 sub_file 의 onClick 형태 확인 필요.")
    else:
        print(f"? 흡수 시뮬: absorbed={absorbed}, resolved={resolved} — "
              f"data 부족")


def _classify(main_path: str, handler: str) -> None:
    try:
        with open(main_path, encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        print(f"✗ 파일 못 읽음: {e}")
        return

    # 1) handler 정의 위치 — 기존 _locate_handler_start 패턴
    start = _locate_handler_start(content, handler)
    if start is None:
        # 패턴 미지원 — 더 광범위하게 시도해서 어떤 형태인지 분류
        candidate_patterns = [
            ("객체 entry (mapDispatchToProps 등)",
             rf"\b{re.escape(handler)}\s*:\s*[^,}}\n]+"),
            ("destructured 구조분해",
             rf"\{{\s*[^}}]*\b{re.escape(handler)}\b[^}}]*\}}\s*="),
            ("import 식별자",
             rf"\bimport\s+[^;]*\b{re.escape(handler)}\b"),
            ("단순 이름 등장",
             rf"\b{re.escape(handler)}\b"),
        ]
        matched_kinds = []
        first_line = ""
        for kind, pat in candidate_patterns:
            m = re.search(pat, content)
            if m:
                matched_kinds.append(kind)
                if not first_line:
                    ls = content.rfind("\n", 0, m.start()) + 1
                    le = content.find("\n", m.end())
                    if le < 0:
                        le = len(content)
                    line = content[ls:le].strip()
                    if len(line) > 140:
                        line = line[:137] + "..."
                    first_line = line
        if not matched_kinds:
            print(f"A) ✗ '{handler}' 가 main 파일에 전혀 안 등장 "
                  f"— 다른 파일에 정의됨")
        else:
            print(f"B) ⚠ '{handler}' 등장 but _locate_handler_start 매칭 X "
                  f"— 형태: {', '.join(matched_kinds[:2])}")
            if first_line:
                print(f"   샘플: {first_line}")
        return

    # 2) 정의는 잡혔다 — body 안 URL 추출 시도
    body = _slice_function_body(content, start, max_len=8000)
    urls = _resolve_main_handler_urls(content, handler)
    if urls:
        print(f"C) ✓ '{handler}' 정의 + URL 추출 성공: {urls[0]}"
              + (f" (+{len(urls)-1})" if len(urls) > 1 else ""))
        return

    # 3) 정의는 잡혔으나 URL 0 — body 안 API method 후보
    method_re = re.compile(
        r"\b(\w*(?:[Aa]xios|[Aa]pi|[Hh]ttp|[Rr]equest|[Ff]etch)\w*)\s*\(")
    methods_seen = sorted({m.group(1) for m in method_re.finditer(body)})
    if methods_seen:
        print(f"D) ⚠ '{handler}' 정의는 잡힘 but URL 0건 "
              f"— body 안 API 후보: {methods_seen[:3]}")
        print(f"   call_re 가 그 method 명을 지원 안 하거나 URL 인자 형태 "
              f"가 builder 패턴 (getBackendUrl) 외인 듯")
    else:
        print(f"E) ⚠ '{handler}' 정의는 잡힘 but body 안 API 호출 0건 "
              f"— handler 가 다른 함수에 위임 (chain follow 필요)")
        # 위임 후보: this.X / dispatch / props.X 호출
        delegates = sorted(set(
            re.findall(r"\b(?:this\.|props\.|dispatch\()(\w+)", body)
        ))[:5]
        if delegates:
            print(f"   위임 후보: {delegates}")


def main():
    args = sys.argv[1:]
    if args and args[0] == "--simulate":
        if len(args) < 4:
            print("usage: python diag_handler_resolve.py --simulate "
                  "<main_file> <frontend_root> <handler>")
            sys.exit(2)
        _simulate(args[1], args[2], args[3])
        return
    if len(args) < 2:
        print("usage: python diag_handler_resolve.py <main_file> <handler>")
        print("       python diag_handler_resolve.py --simulate "
              "<main_file> <frontend_root> <handler>")
        sys.exit(2)
    _classify(args[0], args[1])


if __name__ == "__main__":
    main()
