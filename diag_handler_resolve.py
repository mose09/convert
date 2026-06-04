#!/usr/bin/env python3
"""사용자 PC 에서 한 번 실행 — main file 안 handler 정의 + URL 호출
패턴을 자동 분류해서 1~2줄 결론 emit.

usage:
  python diag_handler_resolve.py <main_file> <handler_name>

예시:
  python diag_handler_resolve.py /workspace/frontend/.../MaterialMaster/index.js handleSearch

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
    )
except ImportError as e:
    print(f"✗ import 실패 — repo 루트에서 실행하세요: {e}")
    sys.exit(1)


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
    if len(sys.argv) < 3:
        print("usage: python diag_handler_resolve.py <main_file> <handler>")
        print("예시: python diag_handler_resolve.py "
              "/workspace/frontend/.../MaterialMaster/index.js handleSearch")
        sys.exit(2)
    _classify(sys.argv[1], sys.argv[2])


if __name__ == "__main__":
    main()
