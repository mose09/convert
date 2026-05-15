#!/usr/bin/env python3
"""React URL map 진단 — 5줄 결론.

특정 키워드 (예: interlockrule) 가 메뉴 URL → routes 파일 → url_map
alias → 매칭 까지 어느 단계에서 끊기는지 1줄씩 emit. analyze-legacy
전체 (수십초~분) 안 돌려도 url_map 빌드만 (보통 수초) 확인 가능.

사용:
  python diagnose_url_map.py <frontend_dir> [--keyword interlock] [--menu-md input/menu.md]

예시 출력:
  [scan]  483 React files, 'interlock' 등장 1 파일
  [file]  src/routes/index.js: Route=Y, apps/import=Y, slug=['hypm-interlockrule']
  [alias] url_map 매칭 키: ['/apps/hypm-interlockrule']
  [menu]  '/apps/hypm-interlockrule' (1개)
  [match] EXACT ✓
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("frontend_dir", help="프론트엔드 src 경로")
    p.add_argument("--keyword", default="interlock",
                   help="찾을 키워드 (default: interlock)")
    p.add_argument("--menu-md", default=None,
                   help="메뉴 .md 경로 (옵션) — URL 매칭까지 확인")
    args = p.parse_args()

    if not os.path.isdir(args.frontend_dir):
        print(f"[FATAL] frontend_dir 가 디렉터리가 아님: {args.frontend_dir}")
        return 1

    from oracle_embeddings.legacy_react_router import (
        scan_react_dir, build_url_to_component_map, _apps_import_aliases,
    )

    kw = args.keyword.lower()

    # 1) scan
    files = scan_react_dir(args.frontend_dir)
    kw_files = []
    for fp in files:
        try:
            with open(fp, encoding="utf-8", errors="ignore") as f:
                content = f.read()
        except Exception:
            continue
        if kw in content.lower():
            kw_files.append((fp, content))
    print(f"[scan]  {len(files)} React files, '{kw}' 등장 {len(kw_files)} 파일")

    # 2) 키워드 등장 파일 별 detail (상위 3개)
    for fp, content in kw_files[:3]:
        rel = os.path.relpath(fp, args.frontend_dir)
        has_route = "Route" in content or "path:" in content
        aliases = _apps_import_aliases(content)
        kw_aliases = [a for a in aliases if kw in a.lower()]
        # 이 파일의 import 라인 중 키워드 포함된 raw 도 출력 (regex 미스 진단용)
        raw_imports = re.findall(
            r"""import\s+[^;]+from\s+['"][^'"]*""" + re.escape(kw) + r"""[^'"]*['"]""",
            content, flags=re.IGNORECASE,
        )
        print(f"[file]  {rel}: Route={'Y' if has_route else 'N'}, "
              f"apps_import_match={kw_aliases or 'NONE'}, "
              f"raw_kw_import={raw_imports[:1] or 'NONE'}")

    # 3) url_map 등록 확인
    url_map = build_url_to_component_map(args.frontend_dir)
    matching = sorted(k for k in url_map if kw in k.lower())
    print(f"[alias] url_map 매칭 키: {matching or 'NONE'}")

    # 4) menu_md 매칭 (옵션)
    if args.menu_md:
        if not os.path.exists(args.menu_md):
            print(f"[menu]  파일 없음: {args.menu_md}")
        else:
            with open(args.menu_md, encoding="utf-8", errors="ignore") as f:
                lines = f.read().splitlines()
            menu_urls = []
            for line in lines:
                if "|" not in line or kw not in line.lower():
                    continue
                # row 의 첫 URL-like token 추출
                m = re.search(r"/[A-Za-z0-9_/\-]{2,}", line)
                if m:
                    menu_urls.append(m.group())
            menu_urls = list(dict.fromkeys(menu_urls))
            print(f"[menu]  {menu_urls or 'NONE'} ({len(menu_urls)}개)")

            # 5) 판정
            if not menu_urls:
                print("[match] menu_md 에 키워드 row 없음 — menu 자체에 등록 안 됨")
            elif not matching:
                print("[match] url_map 에 alias 없음 — Routes scan 단계에서 미스")
            else:
                aliases_lc = [k.lower() for k in matching]
                hit = None
                for mu in menu_urls:
                    if mu.lower() in aliases_lc:
                        hit = ("EXACT", mu)
                        break
                if not hit:
                    for mu in menu_urls:
                        for k in matching:
                            if (mu.lower().endswith(k.lower())
                                    or k.lower().endswith(mu.lower())):
                                hit = ("PARTIAL", f"menu={mu} vs alias={k}")
                                break
                        if hit:
                            break
                if hit:
                    print(f"[match] {hit[0]} ✓ ({hit[1]})")
                else:
                    print(f"[match] MISMATCH — menu={menu_urls} vs alias={matching}")
    else:
        print("[match] --menu-md 미지정 — menu URL 비교 skip")
    return 0


if __name__ == "__main__":
    sys.exit(main())
