"""메뉴 URL ↔ react_url_map 매칭 진단 — 사용자가 그대로 보낼 수 있는
4~6 줄 dump.

사용자 단방향 환경 (수기 타이핑) 친화적: 모든 결정적 정보를 한 화면에
짧게 emit. 추측/객관식 질문 없이, 실제 코드를 그대로 dump.

사용법
------

    python diag_route_match.py REPO_PATH MENU_URL

placeholder 는 꺾쇠 표기를 쓰지 않는다. 실제 입력 시 ``REPO_PATH`` /
``MENU_URL`` 자리에 값을 따옴표로 감싸 넣는다 — ``<...>`` literal 을
포함하면 normalize_url 결과에 ``<`` / ``>`` 가 섞여 매칭이 실패.

예
--

    python diag_route_match.py "D:\\workspace\\frontend\\main" "http://workplace.skhynix.com/apps/gipms-unitclass"

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


_ROUTE_LINE_RE = re.compile(r"<\w*Route\b[^>]*?>", re.DOTALL)


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


def _find_routes_files(repo_root: str) -> list[str]:
    """``repo_root`` 안 ``routes`` / ``Routes`` 가 들어간 모든 js/jsx/ts/tsx
    파일을 walk 로 찾는다. 사용자 환경마다 위치가 달라서 (``src/routes/``,
    ``src/Routes/``, ``app/routes/``, 또는 그냥 ``Routes.jsx``) hardcoded
    candidate 대신 동적 검색.
    """
    out: list[str] = []
    for root, dirs, names in os.walk(repo_root):
        # skip noise
        dirs[:] = [d for d in dirs if d not in {
            "node_modules", "build", "dist", ".next", ".git", "coverage",
        }]
        for n in names:
            stem, ext = os.path.splitext(n)
            if ext.lower() not in {".js", ".jsx", ".ts", ".tsx", ".mjs"}:
                continue
            low = root.lower().replace("\\", "/") + "/" + n.lower()
            if "routes" in low or n.lower().startswith("routes."):
                out.append(os.path.join(root, n))
    return out[:5]  # 최대 5개 — 너무 많으면 noise


def _dump_route_lines(file_path: str, max_lines: int = 5) -> list[str]:
    """파일의 ``<*Route ...>`` 라인 최대 N개 dump (wrapper 포함)."""
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
    except OSError:
        return []
    lines = []
    for m in _ROUTE_LINE_RE.finditer(text):
        line = re.sub(r"\s+", " ", m.group(0)).strip()
        if len(line) > 140:
            line = line[:140] + "..."
        lines.append(line)
        if len(lines) >= max_lines:
            break
    return lines


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

    verdict: str | None = None
    for name, path in buckets:
        app_name = load_react_app_name(path)
        try:
            url_map, _ = build_frontend_url_map(path, framework="react")
        except Exception as e:
            print(f"[BUCKET] {name}: build 실패 ({e})")
            continue
        bases = sorted(url_map.keys())[:3]
        print(f"[BUCKET] {name}: REACT_APP_NAME={app_name!r} "
              f"routes={len(url_map)} bases={bases}")

        # 매칭 시도 (VERDICT 우선순위: 첫 매칭 성공)
        if verdict is None:
            exact = url_map.get(norm_menu)
            prefix = _lookup_react_entry_by_prefix(url_map, norm_menu)
            if exact or prefix:
                via = "exact" if exact else "prefix"
                entry = exact or prefix
                f = entry.get("file_path") or entry.get("declared_in") or "?"
                verdict = f"PASS — bucket={name} via={via} file={f}"

        # Route 파일 raw dump — 추출이 0건이거나 적은 (≤ 3) 경우만, 그리고
        # 매칭 실패 진단에 도움. routes ≥ 4 면 normal 케이스라 dump 생략.
        if len(url_map) <= 3:
            route_files = _find_routes_files(path)
            if not route_files:
                print(f"  [NO_ROUTES_FILE] {name} 안 routes 파일 못 찾음")
            for rf in route_files:
                rel = os.path.relpath(rf, path).replace("\\", "/")
                lines = _dump_route_lines(rf, max_lines=5)
                if not lines:
                    print(f"  [ROUTES_FILE] {rel}: (Route 패턴 0건)")
                else:
                    print(f"  [ROUTES_FILE] {rel}: {len(lines)}건")
                    for ln in lines:
                        print(f"    {ln}")

    print(f"[VERDICT] {verdict or 'FAIL — 어느 bucket 에서도 매칭 안 됨'}")
    return 0 if verdict else 1


if __name__ == "__main__":
    sys.exit(main())
