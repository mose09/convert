"""Diagnostic for menu URL → React Route → import scope → API URL chain.

사용자 환경에서 누락된 메뉴 한 건이 어느 단계에서 끊기는지 한 번 실행으로
판정. 결론 한 줄 (✓/⚠/✗ + 다음 액션) 을 먼저 emit, 상세는 그 아래에.

사용법:
    1. 아래 FRONTEND_DIR / MENU_URL 두 상수만 본인 환경에 맞게 수정
       (Windows 경로는 r"..." 형태 권장).
    2. 레포 루트에서:
           python diag_menu.py
    3. 첫 줄의 ✓ / ⚠ / ✗ 와 메시지를 그대로 보고하면 그 다음 fix 방향이
       정해진다. 상세 출력은 옵션.

주의:
    - frontends_root 모드 (멀티 레포) 인 경우 FRONTEND_DIR 는 그 root.
    - 메뉴 URL 은 메뉴 파일 / DB 에 저장된 RAW 형태 그대로 넣어도 됨
      (스크립트가 normalize_url 통과시킨다).
"""
from __future__ import annotations

import os
import sys
import time

# ---- 사용자가 수정할 부분 ---------------------------------------------------
FRONTEND_DIR = r"D:\workspace\frontend"            # 단일 frontend 또는 frontends_root
MENU_URL     = "http://workplace.skhynix.com/apps/gipms-tbmcbmnoplanmodeling"
# ---------------------------------------------------------------------------


def _rel(p: str) -> str:
    try:
        return os.path.relpath(p, FRONTEND_DIR)
    except Exception:
        return p


def main() -> int:
    if not os.path.isdir(FRONTEND_DIR):
        print(f"✗ FRONTEND_DIR not found: {FRONTEND_DIR}")
        return 1

    from oracle_embeddings.legacy_react_router import (
        build_url_to_component_map, build_import_graph,
        collect_menu_scope_files, scan_react_dir,
    )
    from oracle_embeddings.legacy_react_api_scanner import (
        _build_api_url_index_from_files,
    )
    from oracle_embeddings.legacy_util import normalize_url
    from oracle_embeddings.legacy_frontend import _enumerate_buckets

    t0 = time.time()
    key = normalize_url(MENU_URL)
    print(f"# normalized menu key = {key}")

    # multi-bucket 인지 자동 감지: enumerate buckets 가 frontend_dir
    # 자체와 다른 inner 폴더들을 반환하면 multi-mode.
    buckets = _enumerate_buckets(FRONTEND_DIR)
    is_multi = len(buckets) > 0 and not (
        len(buckets) == 1 and os.path.normpath(buckets[0][1]) == os.path.normpath(FRONTEND_DIR)
    )
    if is_multi:
        scan_roots = [b[1] for b in buckets]
        print(f"# multi-bucket mode: {len(scan_roots)} sub-projects")
    else:
        scan_roots = [FRONTEND_DIR]
        print(f"# single-bucket mode")

    # ── Layer 1: url_map 에 메뉴 URL 등록됐는지 ──
    url_map: dict = {}
    for root in scan_roots:
        um = build_url_to_component_map(root)
        for k, v in um.items():
            url_map.setdefault(k, v)
    print(f"# url_map size: {len(url_map)} entries (across {len(scan_roots)} roots)")

    entry = url_map.get(key)
    if entry is None:
        print(f"\n✗ FAIL Layer 1 — menu URL not in url_map. Route extraction or "
              f"normalization mismatch.")
        # 비슷한 키 찾기 — 실제 substr 으로 matching 후보 노출.
        slug_tail = key.rsplit("/", 1)[-1]
        candidates = [k for k in url_map if slug_tail in k]
        if candidates:
            print(f"  similar keys (substr {slug_tail!r}): {candidates[:10]}")
            print(f"  → Route 는 추출됐지만 정규화 결과가 다른 key 로 저장됨.")
            print(f"    (e.g., react_route_prefix prepend / 추가 path segment).")
        else:
            print(f"  similar keys: none.")
            print(f"  → Route 가 아예 추출 안 됨. index.js 의 Route regex 미스 또는 "
                  f"파일이 scan_react_dir 의 skip 디렉토리에 있음.")
        return 0

    print(f"\n✓ PASS Layer 1 — url_map 에 등록됨")
    print(f"  component:   {entry.get('component')!r}")
    print(f"  file_path:   {_rel(entry.get('file_path') or '')}")
    print(f"  declared_in: {_rel(entry.get('declared_in') or '')}")

    # ── Layer 2: import graph BFS 로 reachable scope ──
    merged_graph: dict = {}
    for root in scan_roots:
        merged_graph.update(build_import_graph(root))
    print(f"# import graph: {len(merged_graph)} files")

    scope = collect_menu_scope_files(key, url_map, merged_graph)
    if not scope:
        print(f"\n✗ FAIL Layer 2 — scope 가 비어있음. seed (file_path / declared_in) "
              f"가 import graph 에 없는 듯.")
        return 0
    print(f"\n✓ PASS Layer 2 — scope: {len(scope)} files reachable from seed")
    for f in sorted(scope):
        print(f"    {_rel(f)}")

    # ── Layer 3: scope 안 API URL 추출 ──
    api_idx = _build_api_url_index_from_files(sorted(scope), FRONTEND_DIR)
    if not api_idx:
        print(f"\n⚠ Layer 3 — scope 안에 backend API 호출 0건.")
        print(f"  → 가능 원인:")
        print(f"     a) 화면 컴포넌트가 외부 store/saga 로 dispatch 만 하고")
        print(f"        실제 API 호출이 import 으로 닿지 않는 별도 파일에 있음")
        print(f"     b) API util 이 절대경로 alias (`@/`, `~/`) 로 import → graph 끊김")
        print(f"     c) 호출 패턴이 _DEFAULT_API_METHODS 밖 (custom http wrapper)")
        return 0
    print(f"\n✓ PASS Layer 3 — scope 안 API URL: {len(api_idx)} 건")
    for url in sorted(api_idx):
        files = api_idx[url]
        print(f"    {url}   ← {files[0] if files else ''}"
              + (f"  (+{len(files)-1})" if len(files) > 1 else ""))

    elapsed = time.time() - t0
    print(f"\n# elapsed {elapsed:.1f}s")
    print(f"\n✓ ALL OK — Layer 1+2+3 통과. 만약 리포트 row 가 여전히 비어있다면")
    print(f"  endpoint matching 단계 (controller URL ↔ scope API URL) 가 의심.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
