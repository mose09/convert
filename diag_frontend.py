"""Diagnostic for frontend Route 추출 / framework 감지 / bucket enumeration.

사용자 환경에서 ``Frontend routes indexed: 0`` 으로 frontend biz 가 시작
조차 못 하는 케이스 진단. 한 번 실행으로 ✓/⚠/✗ 판정.

사용법:
    1. 아래 FRONTEND_DIR 한 줄 본인 환경 맞게 수정 (Windows 경로는
       ``r"..."`` 권장).
    2. 레포 루트에서:
           python diag_frontend.py
    3. 첫 줄의 ✓ / ⚠ / ✗ 와 메시지 그대로 보고하면 fix 방향 결정.
"""
from __future__ import annotations

import os
import sys

# ---- 사용자가 수정할 부분 ---------------------------------------------------
FRONTEND_DIR = r"D:\workspace\frontend"   # 단일 frontend 또는 frontends_root
# ---------------------------------------------------------------------------


def main() -> int:
    if not os.path.isdir(FRONTEND_DIR):
        print(f"✗ FRONTEND_DIR not found: {FRONTEND_DIR}")
        return 1

    from oracle_embeddings.legacy_frontend import (
        _enumerate_buckets,
        build_frontend_url_map,
        build_frontend_url_map_multi,
        detect_frontend_framework,
    )

    # ── Layer 1: framework auto-detection ──
    fw = detect_frontend_framework(FRONTEND_DIR)
    print(f"# framework auto-detect = {fw!r}")

    if fw == "unknown":
        print(f"\n✗ FAIL Layer 1 — framework unknown.")
        print(f"  package.json 의 dependencies 에 react-router-dom 또는 polymer")
        print(f"  키워드가 안 보이거나 sample 파일에 react/polymer 패턴 없음.")
        print(f"  → --frontend-framework react|polymer 명시로 강제 가능.")
        return 0

    # ── Layer 2: bucket enumeration (multi-app 구조) ──
    buckets = _enumerate_buckets(FRONTEND_DIR)
    print(f"\n# enumerated buckets: {len(buckets)}")
    for name, path in buckets[:10]:
        print(f"    {name}  ({path})")
    if len(buckets) > 10:
        print(f"    ... +{len(buckets) - 10} more")

    # ── Layer 3: route extraction ──
    if not buckets or (len(buckets) == 1 and
                       os.path.normpath(buckets[0][1]) == os.path.normpath(FRONTEND_DIR)):
        # 단일 bucket: build_frontend_url_map 호출
        url_map, det_fw = build_frontend_url_map(FRONTEND_DIR, framework=fw)
        print(f"\n# single-bucket: {len(url_map)} routes (framework={det_fw})")
        url_map_keys = list(url_map.keys())[:10]
        for k in url_map_keys:
            print(f"    {k}")
        if len(url_map) > 10:
            print(f"    ... +{len(url_map) - 10} more")
        if not url_map:
            print(f"\n✗ FAIL Layer 3 — Route 추출 0건. 가능 원인:")
            print(f"  a) Route 선언 syntax 가 미지원 변형 (e.g., createBrowserRouter)")
            print(f"  b) Router 가 .ts / .tsx / .jsx / .js 외 다른 확장자")
            print(f"  c) Route 가 minified/bundle 파일 안에만 있음 (skip)")
            print(f"  → diag_menu.py 의 Layer 1 으로 특정 메뉴 한 건 진단 권장.")
            return 0
        print(f"\n✓ ALL OK (single-bucket) — {len(url_map)} routes 추출")
        return 0

    # multi-bucket
    merged, det_fw, by_fe, api_fe, trig_fe = build_frontend_url_map_multi(
        FRONTEND_DIR, framework=fw,
    )
    print(f"\n# multi-bucket: {len(merged)} merged routes "
          f"(framework={det_fw}, {len(by_fe)} bucket keys)")
    print(f"# bucket keys: {sorted(by_fe.keys())[:15]}")

    if not merged:
        print(f"\n✗ FAIL Layer 3 — bucket 은 분리됐는데 Route 추출 0건.")
        print(f"  각 bucket 안에 Route 선언이 있는지 확인 필요.")
        print(f"  가능 원인: Route 가 sub-folder 깊이 / variant syntax / minified")
        return 0

    api_total = sum(len(v) for v in api_fe.values())
    trig_total = sum(len(v) for v in trig_fe.values())
    print(f"# api calls indexed: {api_total} across {len(api_fe)} buckets")
    print(f"# button triggers:   {trig_total}")
    print(f"\n✓ ALL OK (multi-bucket) — {len(merged)} routes, "
          f"{api_total} api calls, {trig_total} triggers")
    return 0


if __name__ == "__main__":
    sys.exit(main())
