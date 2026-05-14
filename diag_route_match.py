"""메뉴 URL ↔ react_url_map 매칭 진단 — 한 줄 결론 출력.

사용자 단방향 환경에서 "왜 frontend 매핑이 안 되나" 를 좁히기 위한
진단 도구. ``analyze-legacy`` 의 frontend 단계를 단독 재현해 정확히
어디서 끊기는지 1~3 줄로 emit.

사용법
------

    python diag_route_match.py <frontends_root> "<menu_url>"

예
--

    python diag_route_match.py D:\\workspace\\frontend ^
        "http://workplace.skhynix.com/apps/gipms-unitclass"

출력 예
-------

    [PASS] /apps/gipms-unitclass → bucket=gipms-unitclass base=/apps/gipms-unitclass
    [FAIL] Route 추출 0건 — PR #201 dynamic resolver 가 못 잡음. routes/index.js 의 첫 5 줄을 보여주세요
    [FAIL] .env REACT_APP_NAME 없음 — auto-prefix 불가
    [FAIL] base 등록은 됐는데 (/apps/foo) 메뉴 URL (/apps/gipms-unitclass) 와 mismatch
"""

from __future__ import annotations

import os
import sys


def _list_buckets(root: str) -> list[tuple[str, str]]:
    """root 의 sub-repo enumerate.

    - root 자체가 sub-repo (``.env`` + ``src/`` 보유) 면 root 만 yield
    - 아니면 1-depth child 폴더들을 sub-repo 로 enumerate (analyzer 의
      ``_enumerate_buckets`` 와 동일 정책)
    """
    root = os.path.abspath(root)
    self_env = os.path.isfile(os.path.join(root, ".env")) or any(
        n.startswith(".env") and os.path.isfile(os.path.join(root, n))
        for n in (os.listdir(root) if os.path.isdir(root) else [])
    )
    self_src = os.path.isdir(os.path.join(root, "src"))
    if self_env and self_src:
        return [(os.path.basename(root), root)]
    out: list[tuple[str, str]] = []
    try:
        for name in sorted(os.listdir(root)):
            full = os.path.join(root, name)
            if os.path.isdir(full) and not name.startswith("."):
                out.append((name, full))
    except OSError as e:
        print(f"[ERROR] {root} 읽기 실패: {e}")
        sys.exit(2)
    return out


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    frontends_root = sys.argv[1]
    menu_url = sys.argv[2]

    if not os.path.isdir(frontends_root):
        print(f"[ERROR] frontends_root 폴더 없음: {frontends_root}")
        return 2

    from oracle_embeddings.legacy_util import normalize_url
    from oracle_embeddings.legacy_react_api_scanner import load_react_app_name
    from oracle_embeddings.legacy_frontend import build_frontend_url_map
    from oracle_embeddings.legacy_analyzer import _lookup_react_entry_by_prefix

    norm_menu = normalize_url(menu_url)
    print(f"# 메뉴 URL 정규화: {menu_url}  →  {norm_menu}")

    buckets = _list_buckets(frontends_root)
    if not buckets:
        print(f"[FAIL] sub-repo 0개 — {frontends_root} 안 1-depth child 가 없음")
        return 1
    print(f"# sub-repo {len(buckets)}개 발견")

    merged_map: dict = {}
    found_match: tuple[str, str, dict] | None = None
    for name, path in buckets:
        app_name = load_react_app_name(path)
        try:
            url_map, fw = build_frontend_url_map(path, framework="react")
        except Exception as e:
            print(f"  - {name}: build_frontend_url_map 실패 ({e})")
            continue
        # 정확 매칭
        exact = url_map.get(norm_menu)
        # prefix 매칭 (PR #202 동작 재현)
        prefix = _lookup_react_entry_by_prefix(url_map, norm_menu)
        if exact or prefix:
            via = "exact" if exact else "prefix"
            entry = exact or prefix
            found_match = (name, via, entry or {})
            print(f"  - {name}: REACT_APP_NAME={app_name!r}, routes={len(url_map)}, "
                  f"매칭=YES ({via})")
            break
        else:
            print(f"  - {name}: REACT_APP_NAME={app_name!r}, routes={len(url_map)}, "
                  f"매칭=NO (base 예시: {list(url_map.keys())[:3]})")
        merged_map.update(url_map)

    print()
    if found_match:
        name, via, entry = found_match
        print(f"[PASS] {norm_menu} → bucket={name} via={via} "
              f"frontend_name={entry.get('frontend_name')} "
              f"file={entry.get('file_path') or entry.get('declared_in')}")
        return 0

    # 매칭 실패 — 진단 단서
    print("[FAIL] 어느 sub-repo 의 react_url_map 에서도 매칭 안 됨.")
    print("  진단 단서:")
    print(f"    1. 정규화된 메뉴 URL: {norm_menu}")
    print(f"    2. 위에서 routes=0 인 sub-repo → PR #201 dynamic Route 추출 실패")
    print(f"       해결: routes/index.js 의 <Route ...> 줄을 알려주세요")
    print(f"    3. routes>0 이고 base 가 다른 형태 → REACT_APP_NAME vs menu slug mismatch")
    print(f"       해결: .env REACT_APP_NAME 값과 menu URL slug 일치 여부 확인")
    return 1


if __name__ == "__main__":
    sys.exit(main())
