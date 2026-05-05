"""사용자 PC 에서 실 프로젝트의 Trigger 컬럼 진단.

사용 예:
  python diag_trigger.py --frontend-dir D:/hcp/workspace/frontend
  python diag_trigger.py --frontend-dir <path> --target-url /api/cmphead/save

출력은 모바일 타이핑 가능한 형태 — 카운트 + 판정 letter (P1~P4).
샘플 file:handler 만 보여주고 raw body 덤프 안 함.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from oracle_embeddings.legacy_react_api_scanner import (
    _extract_app_slug,
    _is_indirect_handoff,
    _locate_handler_body,
    _scan_dir,
    build_api_url_index,
    collect_event_handlers,
    extract_button_triggers,
)
from oracle_embeddings.mybatis_parser import _read_file_safe


_AXIOS_RE = re.compile(r"\b(axios|fetch|api|http)\s*[.(]")
_FUNC_CALL_RE = re.compile(r"\b\w+\s*\(")


def _classify(body: str) -> str:
    if not body:
        return "C"  # 핸들러 같은 파일에 없음 → import 추정
    if _AXIOS_RE.search(body):
        return "A"
    if _is_indirect_handoff(body):
        return "B"
    if _FUNC_CALL_RE.search(body):
        return "D"
    return "F"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frontend-dir", required=True)
    ap.add_argument("--max-samples", type=int, default=3,
                    help="버킷별 샘플 출력 개수 (default 3)")
    ap.add_argument("--target-url",
                    help="특정 URL 만 집중 진단 (선택)")
    args = ap.parse_args()

    fd = args.frontend_dir.strip().rstrip("\\/").rstrip()
    if not os.path.isdir(fd):
        print(f"[X] frontend-dir 없음: {fd}")
        return 2

    # L1: api_idx
    api_idx = build_api_url_index(fd)
    print(f"L1 api_idx URLs: {len(api_idx)}")

    if args.target_url:
        if args.target_url in api_idx:
            print(f"  target {args.target_url} → axios 위치: "
                  f"{len(api_idx[args.target_url])} 파일")
            for f in api_idx[args.target_url][:args.max_samples]:
                print(f"    {f}")
        else:
            print(f"  [X] target {args.target_url} api_idx 에 없음")

    # L2: trigger map
    trig = extract_button_triggers(fd, api_idx)
    matched_pct = (len(trig) * 100 // len(api_idx)) if api_idx else 0
    print(f"L2 trigger 매핑: {len(trig)} / {len(api_idx)} URLs ({matched_pct}%)")

    unmapped = [u for u in api_idx if u not in trig]
    if unmapped:
        print(f"  unmapped URL 샘플 ({min(args.max_samples, len(unmapped))}):")
        for u in unmapped[:args.max_samples]:
            print(f"    {u}")

    # L3: 모든 파일 스캔 + 이벤트 분류
    files = _scan_dir(fd)
    print(f"L3 react/js 파일 수: {len(files)}")

    buckets: dict[str, list[tuple[str, str]]] = defaultdict(list)
    total_events = 0
    for fp in files:
        try:
            content = _read_file_safe(fp)
        except Exception:
            continue
        events = collect_event_handlers(content)
        if not events:
            continue
        for ev in events:
            handler = ev.get("handler") or "<inline>"
            body = ev.get("body") or ""
            if not body and handler != "<inline>":
                body = _locate_handler_body(content, handler) or ""
            kind = _classify(body)
            rel = os.path.relpath(fp, fd).replace("\\", "/")
            buckets[kind].append((rel, handler))
            total_events += 1

    print(f"L3 이벤트 총: {total_events}")
    labels = {
        "A": "직접 axios/fetch",
        "B": "dispatch / props",
        "C": "import 핸들러 (body 없음)",
        "D": "함수 호출만 있음 (그 외)",
        "F": "알 수 없음",
    }
    for k in ["A", "B", "C", "D", "F"]:
        cnt = len(buckets.get(k, []))
        pct = (cnt * 100 // total_events) if total_events else 0
        print(f"  {k} {labels[k]}: {cnt} ({pct}%)")
        for s in buckets.get(k, [])[:args.max_samples]:
            print(f"    - {s[0]}  handler={s[1]}")

    # L4: app-slug 분포
    slug_apps: set[str] = set()
    slug_stores: set[str] = set()
    for fp in files:
        rel = os.path.relpath(fp, fd).replace("\\", "/")
        parts = rel.split("/")
        for i, p in enumerate(parts):
            pl = p.lower()
            if pl in {"apps", "app"} and i + 1 < len(parts):
                slug_apps.add(parts[i + 1].lower())
            elif pl in {"store", "stores"} and i + 1 < len(parts):
                slug_stores.add(parts[i + 1].lower())
    matched_slugs = slug_apps & slug_stores
    apps_only = slug_apps - slug_stores
    print(f"L4 app-slug: apps={len(slug_apps)}, store={len(slug_stores)}, "
          f"매칭={len(matched_slugs)}, apps만={len(apps_only)}")
    if apps_only:
        print(f"  apps-only 샘플 ({min(args.max_samples, len(apps_only))}):")
        for s in sorted(apps_only)[:args.max_samples]:
            print(f"    apps/{s}/")

    # 판정
    print()
    print("=== 판정 ===")
    c_cnt = len(buckets.get("C", []))
    f_cnt = len(buckets.get("F", []))
    if matched_pct >= 90 and f_cnt < total_events * 0.1:
        print("[P1] ✓ Trigger 매핑 정상 (90%+, F 패턴 적음)")
    elif c_cnt > total_events * 0.2 and matched_pct < 60:
        print("[P2] ⚠ import 핸들러 (C) 가 다수 — saga slug 매칭 추가 점검 필요")
        print("    → C 샘플의 import 출처 확인 (apps/X 의 saga 가 다른 폴더에 있는지)")
    elif f_cnt > total_events * 0.2:
        print("[P3] ⚠ 알 수 없는 패턴 (F) 다수 — 새 detector 추가 필요")
        print("    → F 샘플의 onClick/event 형태 알려주면 패턴 추가 가능")
    elif len(slug_apps) > 0 and len(matched_slugs) == 0:
        print("[P4] ⚠ apps/store slug 매칭 0건 — saga 파일 위치 다름")
        print("    → 실제 saga 가 어느 폴더 (modules/ pages/ etc) 에 있는지 확인 필요")
    else:
        print("[P5] ⚠ 매핑률 낮지만 패턴 분포 정상 — api_idx 자체 누락 의심")

    print()
    print("모바일 회신 형식 예:")
    print(f"  L1={len(api_idx)}, L2={len(trig)}/{len(api_idx)}, "
          f"L3={total_events} (A={len(buckets.get('A', []))} "
          f"B={len(buckets.get('B', []))} C={c_cnt} "
          f"D={len(buckets.get('D', []))} F={f_cnt}), "
          f"L4 apps={len(slug_apps)}/store={len(slug_stores)}/"
          f"매칭={len(matched_slugs)}, 판정=Pn")
    return 0


if __name__ == "__main__":
    sys.exit(main())
