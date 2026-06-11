"""diag_url_trace.py — 특정 backend 호출 URL 이 `--extract-screen-layout`
화면정의서에서 빠질 때, 어느 단계에서 누락되는지 추적하는 단독 스크립트.

폐쇄망 / 단방향 전송 환경 전제. 사용자가 React 프로젝트 경로 + 누락된 URL
조각 (예: ``meterialMasterSearch``) 한 번 넣으면, 스크립트가 URL 추출
파이프라인 5단계를 순서대로 통과시키며 **한 줄씩 ✓/✗** 로 자동 판정한다.
첫 ✗ 가 원인 — Claude 가 그 단계만 보고 patterns.yaml / 코드 패치 진행.

긴 dump 금지 — 각 단계 1줄 결론 + 마지막에 "다음 액션" 1줄.

5단계:
    1) RAW       : URL 리터럴이 스캔 대상 파일에 실제로 존재하는가
    2) RELEVANT  : 그 파일이 event 수집 대상 필터를 통과하는가
    3) API-INDEX : build_api_url_index 가 URL 을 잡는가 (정규식 추출)
    4) HANDLER   : collect_handler_contexts 가 URL 을 버튼 event 에 링크하는가
    5) SCREEN    : 파일별 그룹핑 후 화면(screen) url_map 에 들어가는가

사용법::

    python diag_url_trace.py --frontend-dir C:\\work\\frontend --url meterialMasterSearch
    python diag_url_trace.py --frontend-dir <path> --url /api/.../search

옵션:
    --frontend-dir   React 프로젝트 루트 (필수)
    --url            누락된 URL 또는 그 일부 (대소문자 무시, 필수)
    --verbose        각 단계 상세 (파일 목록 등) 추가 출력
"""
from __future__ import annotations

import argparse
import os
import sys


_SKIP_DIRS = {"node_modules", "dist", "build", ".next", ".git", "__tests__",
              "test", "tests", "coverage", ".cache", ".idea", ".vscode",
              "storybook", ".storybook", "public"}
_REACT_EXT = (".js", ".jsx", ".ts", ".tsx", ".mjs")


def _walk(root: str):
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in _SKIP_DIRS and not d.startswith(".")]
        for fn in fns:
            if fn.endswith(_REACT_EXT):
                yield os.path.join(dp, fn)


def _read(fp: str) -> str:
    try:
        with open(fp, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except OSError:
        return ""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="버튼-URL 누락 5단계 추적 진단")
    ap.add_argument("--frontend-dir", required=True)
    ap.add_argument("--url", required=True, help="누락된 URL 또는 일부 (대소문자 무시)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    fe = args.frontend_dir
    needle = args.url.lower().lstrip("/")
    if not os.path.isdir(fe):
        print(f"✗ frontend-dir 없음: {fe}")
        return 2

    # scanner 함수 재사용 — 이 스크립트는 레포 루트에서 실행 전제.
    try:
        from oracle_embeddings.legacy_react_api_scanner import (
            build_api_url_index, collect_handler_contexts,
            _is_relevant_react_file, _repo_has_apps_layout,
        )
        from oracle_embeddings.legacy_screen_extractor import (
            _group_handlers_by_file,
        )
    except Exception as e:  # pragma: no cover - import 환경 의존
        print(f"✗ scanner import 실패 (레포 루트에서 실행하세요): {e}")
        return 2

    verdicts: list[str] = []

    # ── 1) RAW: URL 리터럴이 어느 파일에 있는가 ──────────────────────
    raw_files: list[str] = []
    for fp in _walk(fe):
        if needle in _read(fp).lower():
            raw_files.append(os.path.relpath(fp, fe).replace("\\", "/"))
    if not raw_files:
        print(f"✗ 1.RAW      : '{args.url}' 리터럴이 스캔 대상 파일에 없음")
        print("→ 다음 액션: URL 이 상수/변수 참조이거나 (getBackendURL('BASE', URL_CONST)) "
              "다른 경로에 있음. --url 을 실제 리터럴 일부로 바꾸거나 frontend-dir 확인.")
        return 1
    print(f"✓ 1.RAW      : {len(raw_files)}개 파일에 리터럴 존재 (예: {raw_files[0]})")
    if args.verbose:
        for r in raw_files[:10]:
            print(f"             - {r}")

    # ── 2) RELEVANT: 그 파일들이 event 수집 필터를 통과하는가 ────────
    has_apps = _repo_has_apps_layout(fe)
    relevant = [r for r in raw_files if _is_relevant_react_file(r, has_apps)]
    if not relevant:
        layout = "apps/ 모노레포" if has_apps else "단일 레포"
        print(f"✗ 2.RELEVANT : URL 보유 파일이 모두 필터 제외됨 ({layout} 레이아웃)")
        print("→ 다음 액션: apps/ 레이아웃이면 index.* / popup 폴더만 통과. "
              "해당 파일을 index 화하거나 popup 폴더로 인식시켜야 함.")
        return 1
    print(f"✓ 2.RELEVANT : {len(relevant)}/{len(raw_files)}개 파일 필터 통과 "
          f"(layout={'apps' if has_apps else 'single'})")

    # ── 3) API-INDEX: 정규식이 URL 을 추출하는가 ────────────────────
    api = build_api_url_index(fe)
    api_hit = [u for u in api if needle in u.lower()]
    if not api_hit:
        print(f"✗ 3.API-INDEX: build_api_url_index 가 URL 추출 실패")
        print("→ 다음 액션: API 호출 함수명 (postAxios 등) 또는 URL 빌더 형태가 "
              "정규식 밖. 호출 한 줄을 알려주면 _DEFAULT_API_METHODS / 빌더 정규식 패치.")
        return 1
    print(f"✓ 3.API-INDEX: URL 추출됨 → {api_hit[0]}  (정의 파일: {api[api_hit[0]][:2]})")

    # ── 4) HANDLER: URL 이 버튼 event 에 링크되는가 ─────────────────
    ctx = collect_handler_contexts(fe, api, include_url_less=True)
    linked = []
    for u, cs in ctx.items():
        if needle in u.lower():
            for c in cs:
                linked.append((c.get("file"), c.get("event"), c.get("handler"), c.get("label")))
    if not linked:
        print(f"✗ 4.HANDLER  : URL 이 어떤 버튼/event 에도 링크 안 됨")
        print("→ 다음 액션: 버튼 onClick → handler → (cross-file) URL 체인이 끊김. "
              "버튼 JSX 와 handler 정의 형태를 알려주면 체인 resolver 패치.")
        return 1
    print(f"✓ 4.HANDLER  : {len(linked)}개 event 에 링크됨 "
          f"(예: {linked[0][0]} · {linked[0][1]} · {linked[0][2]} · '{linked[0][3]}')")
    if args.verbose:
        for f, e, h, l in linked[:10]:
            print(f"             - {f} | {e} | {h} | {l}")

    # ── 5) SCREEN: 파일별 그룹핑 후 화면 url_map 에 들어가는가 ────────
    # 구조: {file: {handler_label: {"urls": [...], ...}}} — URL 은 중첩됨.
    by_file = _group_handlers_by_file(ctx)
    screen_hit = []
    for rel, handler_map in by_file.items():
        urls = [u for entry in (handler_map or {}).values()
                for u in (entry.get("urls") or [])]
        if any(needle in (u or "").lower() for u in urls):
            screen_hit.append(rel)
    if not screen_hit:
        print(f"✗ 5.SCREEN   : URL 이 화면(screen) 단위 그룹에 안 들어감")
        print("→ 다음 액션: _group_handlers_by_file 가 이 URL 의 file 을 화면으로 "
              "안 묶음. 버튼 파일이 화면 entry 로 인식되는지 확인 필요.")
        return 1
    print(f"✓ 5.SCREEN   : 화면 {len(screen_hit)}개에 URL 포함 (예: {screen_hit[0]})")
    print("→ 결론: 5단계 모두 통과 — URL 은 화면정의서에 들어가야 정상. "
          "출력 xlsx/HTML 의 해당 화면 행을 다시 확인하거나, 캐시 (use_cache) 를 "
          "지우고 재실행. 그래도 누락이면 출력 렌더링 단계 문제.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
