"""diag_screen_pattern.py — `--extract-screen-layout` 조회영역/그리드 안 잡힐
때 어떤 프로젝트 패턴인지 자가진단하는 단독 스크립트.

폐쇄망 / 단방향 전송 환경 전제. 사용자가 React 프로젝트 경로 한 번 넣으면
스크립트가 화면 파일을 샘플링해서 다음 4가지 질문에 **알파벳 선택지** 로
정리해 출력. 사용자는 ``Q1=c, Q2=d`` 처럼 단답 회신만 하면 Claude 가
`patterns.yaml` 또는 코드 패치 진행.

긴 dump 금지 — 결론 1-2줄 + 선택지 4-5개씩.

사용법::

    python diag_screen_pattern.py --frontend-dir C:\\work\\frontend
    python diag_screen_pattern.py --frontend-dir <path> --screen-file <path>
    python diag_screen_pattern.py --frontend-dir <path> --limit 100

옵션:
    --frontend-dir   React 프로젝트 루트 (필수)
    --screen-file    특정 화면 1개만 closure 빌드 + 분석
    --limit          스캔 파일 수 cap (default 300)
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from collections import Counter
from pathlib import Path


# JSX 컴포넌트 이름 — 대문자 시작, 영숫자/언더스코어
_JSX_OPEN_RE = re.compile(r"<([A-Z][A-Za-z0-9_]*)\b([^>]*)/?>", re.MULTILINE)

# input-like 휴리스틱 (이름에 포함된 단어)
_INPUT_KEYWORDS = ("input", "select", "dropdown", "picker", "field", "combo",
                   "checkbox", "radio", "switch", "textarea")
# table-like
_TABLE_KEYWORDS = ("table", "grid", "datatable", "datagrid", "list")

# 컬럼 정의 prop 후보 — 라이브러리별 alias union
_COL_PROP_CANDIDATES = ("columns", "columnDefs", "schema", "fields",
                        "headers", "model", "dataFields", "colDefs")

# 라벨 prop 후보 (input-like 컴포넌트에서)
_LABEL_PROP_CANDIDATES = ("label", "placeholder", "title", "aria-label")

# 스캔 제외 폴더
_SKIP_DIRS = {"node_modules", "dist", "build", ".next", ".git", "__tests__",
              "test", "tests", "coverage", ".cache", ".idea", ".vscode"}

# 화면 파일 후보 확장자
_REACT_EXT = (".jsx", ".tsx", ".js", ".ts")


def _walk_react_files(root: Path, limit: int):
    out = []
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in _SKIP_DIRS and not d.startswith(".")]
        for fn in fns:
            if fn.endswith(_REACT_EXT):
                # 테스트/스토리북 제외
                if (".test." in fn or ".spec." in fn or ".stories." in fn
                        or fn.endswith(".d.ts")):
                    continue
                out.append(Path(dp) / fn)
                if len(out) >= limit:
                    return out
    return out


def _strip_block_comments(s: str) -> str:
    return re.sub(r"/\*.*?\*/", "", s, flags=re.DOTALL)


def _read(fp: Path) -> str:
    for enc in ("utf-8", "cp949", "euc-kr", "latin-1"):
        try:
            return fp.read_text(encoding=enc, errors="ignore")
        except Exception:
            continue
    return ""


def _classify_name(name: str) -> str:
    nl = name.lower()
    if any(k in nl for k in _TABLE_KEYWORDS):
        return "table"
    if any(k in nl for k in _INPUT_KEYWORDS):
        return "input"
    return "other"


def _extract_attrs_text(attrs_chunk: str) -> dict[str, str]:
    """JSX opening 의 attr 영역 → {name: 'literal'|'expr'|'true'}.

    regex 기반 best-effort — '=' 없이 boolean prop / 값이 string literal
    또는 ``{...}`` expression 인 경우 모두 처리.
    """
    out: dict[str, str] = {}
    # name="literal"
    for m in re.finditer(r'([A-Za-z][\w-]*)\s*=\s*"([^"]*)"', attrs_chunk):
        out[m.group(1)] = m.group(2)
    for m in re.finditer(r"([A-Za-z][\w-]*)\s*=\s*'([^']*)'", attrs_chunk):
        out[m.group(1)] = m.group(2)
    # name={...}
    for m in re.finditer(r"([A-Za-z][\w-]*)\s*=\s*\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}",
                         attrs_chunk):
        out[m.group(1)] = "{" + m.group(2) + "}"
    # boolean (= 없음). 위 두 regex 가 잡은 것 제외하고 단순 식별자
    for m in re.finditer(r"\b([A-Za-z][\w-]*)\s*(?=\s|/?>|$)", attrs_chunk):
        nm = m.group(1)
        if nm not in out and not re.search(rf"\b{re.escape(nm)}\s*=", attrs_chunk):
            out[nm] = "true"
    return out


def _detect_sibling_label(content: str, jsx_match) -> bool:
    """`<Input.../>` 근처 (앞 200자 / 뒤 50자) 에 `className="...label..."`
    이 포함된 span/div 가 있는지 휴리스틱 검사.

    완벽한 AST 분석은 아니지만 빈도 통계용으로 충분.
    """
    start = max(0, jsx_match.start() - 300)
    end = min(len(content), jsx_match.end() + 100)
    window = content[start:end]
    return bool(re.search(
        r'<(?:span|div|label)[^>]*className\s*=\s*[\"\'][^"\']*label[^"\']*[\"\']',
        window, re.IGNORECASE
    ))


def _detect_text_child_label(content: str, jsx_match) -> bool:
    """`<span>FAB</span>` 처럼 직전에 <span>한글텍스트</span> 가 있는지."""
    start = max(0, jsx_match.start() - 200)
    window = content[start:jsx_match.start()]
    return bool(re.search(
        r"<(?:span|div|label)[^>]*>\s*[가-힣A-Za-z0-9_]{1,30}\s*</(?:span|div|label)>",
        window
    ))


def _closure_level_diagnose(screen_file: Path, frontend_dir: Path) -> int:
    """`--screen-file` 모드 — 특정 화면 entry 에서 closure 빌드 → 그리드/조회영역
    안 잡히는 진짜 원인 식별. tree-sitter 필요.

    체크리스트:
      1. tree-sitter 설치 / closure 빌드 성공 여부
      2. closure 안 파일 N개 (depth 별)
      3. AgGridReact / 그리드 컴포넌트 JSX 발견 위치
      4. columnDefs prop 값 형태 (literal / identifier / function call / 동적)
      5. identifier 인 경우 closure 안에서 정의 찾기 결과
    """
    print(f"=== Closure-level diagnostic ===")
    print(f"entry: {screen_file}")
    print(f"frontend_dir: {frontend_dir}")
    print()

    # 1. tree-sitter
    try:
        from oracle_embeddings.legacy_react_closure import build_closure
        from oracle_embeddings.legacy_react_ast import (
            find_by_type, child_by_field, text_of, parse_file,
        )
    except Exception as e:
        print(f"✗ tree-sitter 미설치 / 모듈 import 실패: {e}")
        print("→ wheel install 필요 (CLAUDE.md 의 tree-sitter 섹션 참고)")
        return 2
    print("✓ tree-sitter 모듈 OK")

    # 2. closure 빌드
    try:
        closure = build_closure(
            entry_file=str(screen_file),
            repo_root=str(frontend_dir),
            patterns={},
            max_depth=3,
            token_budget=12000,
            verbose=False,
        )
    except Exception as e:
        print(f"✗ closure build 실패: {e}")
        return 3
    print(f"✓ closure built: {len(closure.files)}개 파일")

    # 파일 depth/mode 요약
    by_depth: dict[int, list] = {}
    for f in closure.files:
        by_depth.setdefault(f.depth, []).append(f)
    for d in sorted(by_depth.keys()):
        names = [f"{f.rel_path}({f.mode})" for f in by_depth[d][:3]]
        more = f" ... +{len(by_depth[d]) - 3}" if len(by_depth[d]) > 3 else ""
        print(f"  depth {d}: {len(by_depth[d])}개 — {', '.join(names)}{more}")

    # skipped external (alias resolve 실패 가능성 단서)
    if closure.skipped_external:
        ext_sample = closure.skipped_external[:8]
        print(f"  external/skipped: {len(closure.skipped_external)} — {ext_sample}"
              + (" ..." if len(closure.skipped_external) > 8 else ""))
    print()

    # 3. AgGridReact 등 그리드 JSX 위치 + columnDefs 값 형태
    table_names = {"AgGridReact", "Table", "DataTable", "DataGrid", "Grid",
                   "MaterialTable"}
    grid_hits: list[tuple[str, int, str, str]] = []   # (file, line, comp, cols_form)

    for f in closure.files:
        try:
            tree, source, _ = parse_file(f.abs_path)
        except Exception:
            continue
        if tree is None:
            continue
        for el_node in find_by_type(tree.root_node,
                                    {"jsx_opening_element",
                                     "jsx_self_closing_element"}):
            name_node = child_by_field(el_node, "name")
            if name_node is None:
                continue
            tag = text_of(name_node, source).strip()
            if tag not in table_names:
                continue
            line = el_node.start_point[0] + 1
            # columnDefs / columns / schema prop 의 값 노드 형태 식별
            form = "(prop 없음)"
            ident_name = None
            for attr in el_node.children:
                if attr.type != "jsx_attribute":
                    continue
                nc = attr.named_children
                if not nc:
                    continue
                aname = text_of(nc[0], source).strip()
                if aname not in ("columns", "columnDefs", "schema"):
                    continue
                if len(nc) < 2 or nc[1].type != "jsx_expression":
                    form = f"{aname}=(literal string?)"
                    break
                # jsx_expression 안 첫 named child
                inner_nodes = [c for c in nc[1].named_children]
                if not inner_nodes:
                    form = f"{aname}={{}}"
                    break
                inner = inner_nodes[0]
                if inner.type == "array":
                    form = f"{aname}=[inline array]"
                elif inner.type == "identifier":
                    ident_name = text_of(inner, source).strip()
                    form = f"{aname}={ident_name} (identifier)"
                elif inner.type == "call_expression":
                    callee = child_by_field(inner, "function")
                    cn = text_of(callee, source) if callee is not None else "?"
                    form = f"{aname}={cn}(...) (call)"
                elif inner.type == "member_expression":
                    form = f"{aname}={text_of(inner, source)[:30]} (member)"
                else:
                    form = f"{aname}=<{inner.type}>"
                break
            grid_hits.append((f.rel_path, line, tag, form))

    if not grid_hits:
        print("✗ closure 안에서 AgGridReact / Table / DataGrid 등 그리드 JSX 0건")
        print("  → 가능성:")
        print("    a) closure BFS 가 그리드 정의 파일까지 못 도달 (alias 경로 해석 실패)")
        print("    b) 그리드 컴포넌트가 wrap 된 다른 이름 (예: <MyGrid> + 내부 ag-grid)")
        print("    c) entry 가 잘못된 파일 (실제 화면은 다른 entry)")
        return 0

    print(f"✓ 그리드 JSX {len(grid_hits)}건 발견:")
    ident_targets: list[str] = []
    for rel, line, tag, form in grid_hits:
        print(f"    {rel}:{line}  <{tag} ... {form}/>")
        m = re.match(r"\w+=(\w+) \(identifier\)", form)
        if m:
            ident_targets.append(m.group(1))
    print()

    # 4. identifier 인 경우 closure 안 정의 찾기
    if ident_targets:
        print(f"[Q5] columnDefs={{X}} 의 X 가 identifier — closure 안 정의 검색:")
        for ident in set(ident_targets):
            found_files = []
            for f in closure.files:
                try:
                    tree, source, _ = parse_file(f.abs_path)
                except Exception:
                    continue
                if tree is None:
                    continue
                for n in find_by_type(tree.root_node, "variable_declarator"):
                    nm = child_by_field(n, "name")
                    if nm and text_of(nm, source).strip() == ident:
                        val = child_by_field(n, "value")
                        vtype = val.type if val else "?"
                        found_files.append(f"{f.rel_path} (value={vtype})")
                        break
            if found_files:
                print(f"  {ident} 정의: {found_files}")
                # 어떤 value type 인지 — array literal 면 정상 해석, call/member 면 동적
                first = found_files[0]
                if "value=array" in first:
                    print(f"    → 정상 — 우리 파서가 잡아야 함 (의문)")
                else:
                    print(f"    ⚠ 동적 정의 (array 아님) — 우리 파서가 못 잡음")
            else:
                print(f"  {ident}: closure 안에서 정의 못 찾음")
                print(f"    → import 경로 해석 실패 (alias?) — 그 파일이 closure 에 안 들어옴")
    return 0


def diagnose(frontend_dir: Path, limit: int, screen_file: Path | None = None):
    if screen_file is not None:
        # closure-level 진단 모드
        return _closure_level_diagnose(screen_file, frontend_dir)

    files = _walk_react_files(frontend_dir, limit)
    if not files:
        print(f"✗ 스캔 가능한 React 파일 0건 — 경로 확인: {frontend_dir}")
        return 1

    print(f"=== React Screen Pattern Diagnostic ===")
    print(f"frontend_dir: {frontend_dir}")
    print(f"scanned: {len(files)} files")
    print()

    comp_count: Counter[str] = Counter()
    table_props: Counter[tuple[str, str]] = Counter()   # (comp, prop)
    input_label_pattern: Counter[str] = Counter()       # prop / sibling / text_sibling / none
    col_prop_seen: Counter[str] = Counter()             # columns / columnDefs / ...

    for fp in files:
        raw = _read(fp)
        if not raw:
            continue
        content = _strip_block_comments(raw)
        for m in _JSX_OPEN_RE.finditer(content):
            name = m.group(1)
            attrs_chunk = m.group(2) or ""
            comp_count[name] += 1
            cls = _classify_name(name)
            if cls == "table":
                attrs = _extract_attrs_text(attrs_chunk)
                for p in _COL_PROP_CANDIDATES:
                    if p in attrs:
                        table_props[(name, p)] += 1
                        col_prop_seen[p] += 1
            elif cls == "input":
                attrs = _extract_attrs_text(attrs_chunk)
                if any(lp in attrs for lp in _LABEL_PROP_CANDIDATES):
                    input_label_pattern["prop"] += 1
                elif _detect_sibling_label(content, m):
                    input_label_pattern["sibling_label_class"] += 1
                elif _detect_text_child_label(content, m):
                    input_label_pattern["text_sibling"] += 1
                else:
                    input_label_pattern["none"] += 1

    # ── 결론 한 줄 ──
    table_top = [(n, c) for n, c in comp_count.most_common()
                 if _classify_name(n) == "table"][:5]
    input_top = [(n, c) for n, c in comp_count.most_common()
                 if _classify_name(n) == "input"][:5]
    print(f"✓ 그리드 후보 {len(table_top)}종, 검색 컴포넌트 후보 {len(input_top)}종 발견")
    print()

    # ── Q1: 그리드 컴포넌트 ──
    print("[Q1] 그리드 컴포넌트 (빈도 top):")
    opts_q1 = []
    seen_q1 = set()
    for default_name in ("AgGridReact", "DataTable", "DataGrid", "Grid", "Table",
                         "MaterialTable"):
        c = comp_count.get(default_name, 0)
        if c > 0:
            opts_q1.append((default_name, c, True))
            seen_q1.add(default_name)
    for n, c in table_top:
        if n not in seen_q1 and len(opts_q1) < 6:
            opts_q1.append((n, c, False))
    if not opts_q1:
        print("  (table-like 이름의 JSX 컴포넌트 0건 발견)")
    else:
        labels = "abcdefg"
        for i, (n, c, is_default) in enumerate(opts_q1):
            mark = " (default 패턴 포함)" if is_default else ""
            print(f"  {labels[i]}) {n} — {c}건{mark}")
        print(f"  {labels[len(opts_q1)]}) 위에 없음 / 모르겠음")
    print()

    # ── Q2: 컬럼 정의 prop ──
    print("[Q2] 컬럼 정의 prop (그리드 호출 시 가장 자주 쓰인 prop):")
    if col_prop_seen:
        labels = "abcdefg"
        for i, (p, c) in enumerate(col_prop_seen.most_common(6)):
            print(f"  {labels[i]}) {p} — {c}건")
        print(f"  {labels[len(col_prop_seen)]}) 위에 없음 / 모르겠음")
    else:
        print("  (위 그리드 컴포넌트들에서 columns/columnDefs/schema 등 prop 0건)")
        print("  a) 그리드가 children 으로 컬럼 정의 (예: <Grid><Column .../></Grid>)")
        print("  b) 그리드가 외부 wrapper — 실제 정의는 wrapper 내부 다른 컴포넌트")
        print("  c) 컬럼이 props 로 전달되지 않고 동적 fetch / state")
        print("  d) 모르겠음 / 확인 필요")
    print()

    # ── Q3: 라벨 패턴 ──
    print("[Q3] 검색 필드 라벨 패턴:")
    if input_label_pattern:
        total = sum(input_label_pattern.values())
        labels = "abcd"
        order = ["prop", "sibling_label_class", "text_sibling", "none"]
        desc = {
            "prop": "JSX prop (label= / placeholder= / title=)",
            "sibling_label_class": "형제 span className 에 'label' 포함",
            "text_sibling": "형제 span/div 의 text child (예: <span>FAB</span>)",
            "none": "라벨 단서 못 찾음",
        }
        for i, key in enumerate(order):
            c = input_label_pattern.get(key, 0)
            pct = (c * 100 // total) if total else 0
            print(f"  {labels[i]}) {desc[key]} — {c}건 ({pct}%)")
    else:
        print("  (input-like 컴포넌트 0건 발견)")
    print()

    # ── Q4: 화면 entry 구조 (선택 — closure 빌드용) ──
    print("[Q4] 화면 entry 파일 구조 (메인 화면 1개 골라 알려주세요):")
    print("  a) 화면 한 파일 안에 검색/그리드 모두 inline 정의")
    print("  b) entry 가 자식 컴포넌트로 분리 — <SearchSection/> <GridSection/>")
    print("  c) entry 가 라우터 wrapper — <PropsRouter component={Screen}/>")
    print("  d) 기타 (예: HOC / dynamic import / context)")
    print()

    # ── 답변 안내 ──
    print("─" * 60)
    print("답변 양식: Q1=c, Q2=a, Q3=b, Q4=c")
    print("(스크린샷/복붙 불가 환경이라 알파벳만 회신해주세요)")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--frontend-dir", required=True,
                    help="React 프로젝트 루트 경로")
    ap.add_argument("--screen-file", default=None,
                    help="특정 화면 1개만 진단 (옵션)")
    ap.add_argument("--limit", type=int, default=300,
                    help="스캔 파일 수 cap (default 300)")
    args = ap.parse_args()
    fdir = Path(args.frontend_dir).expanduser().resolve()
    if not fdir.is_dir():
        print(f"✗ 디렉토리 아님: {fdir}")
        sys.exit(2)
    sfile = None
    if args.screen_file:
        sfile = Path(args.screen_file).expanduser().resolve()
        if not sfile.is_file():
            print(f"✗ 파일 아님: {sfile}")
            sys.exit(2)
    sys.exit(diagnose(fdir, args.limit, sfile))


if __name__ == "__main__":
    main()
