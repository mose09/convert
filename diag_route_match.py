"""메뉴 URL ↔ react_url_map 매칭 진단 — 사용자가 그대로 보낼 수 있는
4~6 줄 dump.

사용자 단방향 환경 (수기 타이핑) 친화적: 모든 결정적 정보를 한 화면에
짧게 emit. 추측/객관식 질문 없이, 실제 코드를 그대로 dump.

사용법
------

    python diag_route_match.py <main_repo_or_frontends_root> "<menu_url>"

예
--

    python diag_route_match.py D:\\workspace\\frontend\\main "http://workplace.skhynix.com/apps/gipms-unitclass"

출력 예
-------

    [MODE] SINGLE — main (.env=Y src/=Y)
    [MENU_NORM] /apps/gipms-unitclass
    [ENV] REACT_APP_NAME=gipms-unitclass
    [ROUTES] 1건 — bases=['/apps/gipms-unitclass']
    [ROUTE_RAW] src/routes/index.js: <Route path={getRoutePath(basename, '/')} component={Main}/>
    [VERDICT] PASS via=prefix file=src/pages/Main.jsx

매칭 실패 시 [VERDICT] FAIL <원인 한 줄>.
"""

from __future__ import annotations

import os
import re
import sys


_ROUTE_LINE_RE = re.compile(r"<Route\b[^>]*?>", re.DOTALL)


def _has_env(root: str) -> bool:
    if not os.path.isdir(root):
        return False
    try:
        for n in os.listdir(root):
            if n.startswith(".env") and os.path.isfile(os.path.join(root, n)):
                return True
    except OSError:
        return False
    return False


def _list_buckets(root: str) -> tuple[str, list[tuple[str, str]]]:
    """Return ``(mode, [(name, path), ...])`` — SINGLE 또는 MULTI."""
    root = os.path.abspath(root)
    has_env = _has_env(root)
    has_src = os.path.isdir(os.path.join(root, "src"))
    if has_env and has_src:
        return "SINGLE", [(os.path.basename(root) or root, root)]
    out: list[tuple[str, str]] = []
    try:
        for name in sorted(os.listdir(root)):
            full = os.path.join(root, name)
            if os.path.isdir(full) and not name.startswith("."):
                out.append((name, full))
    except OSError as e:
        print(f"[ERROR] {root} 읽기 실패: {e}")
        sys.exit(2)
    return "MULTI", out


def _dump_route_lines(repo_root: str, max_lines: int = 2) -> tuple[str, list[str]]:
    """첫 발견된 routes 파일에서 ``<Route ...>`` 라인 max_lines 개 dump."""
    candidates = [
        "src/routes/index.js", "src/routes/index.jsx",
        "src/routes/index.ts", "src/routes/index.tsx",
        "src/routes/Routes.js", "src/routes/Routes.jsx",
        "src/Routes.js", "src/Routes.jsx",
        "routes/index.js", "routes/index.jsx",
        "src/index.js", "src/index.jsx",
        "src/index.ts", "src/index.tsx",
    ]
    for c in candidates:
        full = os.path.join(repo_root, c)
        if not os.path.isfile(full):
            continue
        try:
            with open(full, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read()
        except OSError:
            continue
        lines = []
        for m in _ROUTE_LINE_RE.finditer(text):
            line = re.sub(r"\s+", " ", m.group(0)).strip()
            if len(line) > 140:
                line = line[:140] + "..."
            lines.append(line)
            if len(lines) >= max_lines:
                break
        if lines:
            return c, lines
    return "", []


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    root = sys.argv[1]
    menu_url = sys.argv[2]

    if not os.path.isdir(root):
        print(f"[ERROR] path 없음: {root}")
        return 2

    from oracle_embeddings.legacy_util import normalize_url
    from oracle_embeddings.legacy_react_api_scanner import load_react_app_name
    from oracle_embeddings.legacy_frontend import build_frontend_url_map
    from oracle_embeddings.legacy_analyzer import _lookup_react_entry_by_prefix

    norm_menu = normalize_url(menu_url)
    print(f"[MENU_NORM] {norm_menu}")

    mode, buckets = _list_buckets(root)
    has_env = _has_env(root)
    has_src = os.path.isdir(os.path.join(root, "src"))
    print(f"[MODE] {mode} — {os.path.basename(root) or root} "
          f"(.env={'Y' if has_env else 'N'} src/={'Y' if has_src else 'N'}) "
          f"buckets={len(buckets)}")

    if not buckets:
        print(f"[VERDICT] FAIL — buckets 0개")
        return 1

    # 라우터 보유 sub-repo 만 highlight (routes>0)
    router_repos: list[tuple[str, str, str | None, dict]] = []
    for name, path in buckets:
        app_name = load_react_app_name(path)
        try:
            url_map, _ = build_frontend_url_map(path, framework="react")
        except Exception as e:
            print(f"[BUCKET] {name}: build 실패 ({e})")
            continue
        if url_map:
            router_repos.append((name, path, app_name, url_map))

    if not router_repos:
        # 모든 sub-repo 가 routes=0 — 라우터 없음
        print(f"[ENV] (routes=0 인 sub-repo 만)")
        # routes 파일 자체가 있는지 dump 시도 (메인 레포 후보 찾기)
        for name, path in buckets[:3]:
            file, lines = _dump_route_lines(path)
            if file:
                print(f"[ROUTE_RAW] {name}/{file}: {lines[0]}")
                break
        print(f"[VERDICT] FAIL — 어느 sub-repo 에서도 Route 추출 0건. "
              f"routes/index.js 패턴이 PR #201 regex 와 다름")
        return 1

    # 매칭 시도
    for name, path, app_name, url_map in router_repos:
        bases = sorted(url_map.keys())[:3]
        print(f"[BUCKET] {name}: REACT_APP_NAME={app_name!r} "
              f"routes={len(url_map)} bases={bases}")
        exact = url_map.get(norm_menu)
        prefix = _lookup_react_entry_by_prefix(url_map, norm_menu)
        if exact or prefix:
            via = "exact" if exact else "prefix"
            entry = exact or prefix
            f = entry.get("file_path") or entry.get("declared_in") or "?"
            print(f"[VERDICT] PASS — bucket={name} via={via} file={f}")
            return 0

    # 매칭 실패 — Route 라인 raw dump (가장 큰 router_repo 우선)
    name, path, _, _ = max(router_repos, key=lambda r: len(r[3]))
    file, lines = _dump_route_lines(path)
    if file:
        for ln in lines[:2]:
            print(f"[ROUTE_RAW] {name}/{file}: {ln}")
    print(f"[VERDICT] FAIL — Route 추출은 됐는데 base 가 메뉴 URL 와 mismatch. "
          f"위 ROUTE_RAW + bases 를 보여주세요")
    return 1


if __name__ == "__main__":
    sys.exit(main())
