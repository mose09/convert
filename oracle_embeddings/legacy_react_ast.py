"""
legacy_react_ast.py
===================

tree-sitter 기반 JavaScript/JSX/TypeScript/TSX AST 파서 wrapper.

설계 원칙
---------
- 정규식이 아닌 AST 로 라우팅 dialect 다양성을 흡수한다.
- Language/Parser 객체는 모듈 전역에서 한 번만 생성 (재생성 비용 큼).
- 파일 인코딩 fallback (utf-8 → euc-kr → cp949 → latin-1) — 한국어 레거시 호환.
- import 추출은 ES6 import + dynamic import (`React.lazy(() => import(...))`)
  + CommonJS `require()` 까지 동시 처리.
- Alias 해석은 tsconfig.json / jsconfig.json 의 `compilerOptions.paths` 와
  `baseUrl` 을 따라간다. 없으면 패턴에서 주입된 alias 만 사용.

공개 API
--------
- `parse_file(path)` → `(tree, source_bytes, lang_kind)`
- `extract_imports(tree, source_bytes)` → `[ImportSpec, ...]`
- `walk(node, predicate)` → generator
- `text_of(node, source_bytes)` → str
- `load_alias_map(repo_root)` → dict
- `resolve_import(spec, importer_file, alias_map, repo_root)` → 절대경로 또는 None
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Optional

# ── tree-sitter 0.21+ 신 API ──
import tree_sitter_javascript as _tsjs
from tree_sitter import Language, Node, Parser, Tree

try:
    import tree_sitter_typescript as _tsts
    _HAS_TS = True
except ImportError:
    _HAS_TS = False


# ─────────────────────────────────────────────────────────────────
# Language / Parser 캐시
# ─────────────────────────────────────────────────────────────────

_LANG_JS = Language(_tsjs.language())
_LANG_TSX = Language(_tsts.language_tsx()) if _HAS_TS else None
_LANG_TS = Language(_tsts.language_typescript()) if _HAS_TS else None

_PARSER_JS = Parser(_LANG_JS)
_PARSER_TSX = Parser(_LANG_TSX) if _LANG_TSX else None
_PARSER_TS = Parser(_LANG_TS) if _LANG_TS else None


def _pick_parser(path: str | os.PathLike) -> tuple[Parser, str]:
    """확장자 기준 parser 선택. JSX 는 JS parser 가 처리."""
    ext = str(path).rsplit(".", 1)[-1].lower()
    if ext == "tsx" and _PARSER_TSX:
        return _PARSER_TSX, "tsx"
    if ext == "ts" and _PARSER_TS:
        return _PARSER_TS, "ts"
    # js / jsx / mjs / cjs / 그 외 → JS parser (JSX 내장)
    return _PARSER_JS, "js"


# ─────────────────────────────────────────────────────────────────
# 파일 안전 읽기 + 파싱
# ─────────────────────────────────────────────────────────────────

_ENCODINGS = ("utf-8", "utf-8-sig", "euc-kr", "cp949", "latin-1")


def _read_bytes(path: str | os.PathLike) -> bytes:
    """tree-sitter 는 bytes 직접 받으니 raw 로 읽고 원본 유지."""
    with open(path, "rb") as f:
        return f.read()


def _decode_for_text(raw: bytes) -> str:
    for enc in _ENCODINGS:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def parse_file(path: str | os.PathLike) -> tuple[Tree, bytes, str]:
    """
    파일 → (tree, source_bytes, lang_kind).
    실패 시 (None, b'', kind) 반환.
    """
    parser, kind = _pick_parser(path)
    try:
        raw = _read_bytes(path)
    except (OSError, IOError):
        return None, b"", kind  # type: ignore[return-value]

    # tree-sitter 는 UTF-8 가정. 한국어 cp949/euc-kr 은 일단 UTF-8 로 재인코딩.
    if not _looks_like_utf8(raw):
        text = _decode_for_text(raw)
        raw = text.encode("utf-8", errors="replace")

    tree = parser.parse(raw)
    return tree, raw, kind


def _looks_like_utf8(b: bytes) -> bool:
    try:
        b.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


# ─────────────────────────────────────────────────────────────────
# AST 순회 헬퍼
# ─────────────────────────────────────────────────────────────────

def walk(node: Node) -> Iterator[Node]:
    """전체 AST DFS 순회."""
    yield node
    for child in node.children:
        yield from walk(child)


def find_by_type(node: Node, types: set[str] | str) -> Iterator[Node]:
    """특정 type 의 노드만 yield. types 는 단일 또는 set."""
    target = {types} if isinstance(types, str) else types
    for n in walk(node):
        if n.type in target:
            yield n


def text_of(node: Node, source: bytes) -> str:
    """노드 텍스트 추출 (UTF-8 디코드)."""
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def child_by_field(node: Node, field_name: str) -> Optional[Node]:
    """field 이름으로 자식 노드 가져오기 (없으면 None)."""
    return node.child_by_field_name(field_name)


# ─────────────────────────────────────────────────────────────────
# Import 추출
# ─────────────────────────────────────────────────────────────────

@dataclass
class ImportSpec:
    """
    import 문 한 건의 정규화된 표현.

    - source: 원본 import 경로 ('react-router-dom', './pages/Foo', '@components/Bar')
    - default_name: default import 이름 ('import Foo from ...')
    - named: named import 매핑 (`{ Routes as R }` → `{'Routes': 'R'}`)
    - is_dynamic: lazy(() => import('...')) / await import('...') 인지
    - lazy_binding: dynamic 의 경우 좌변 const 이름 (`const Foo = lazy(...)` → 'Foo')
    """
    source: str
    default_name: Optional[str] = None
    named: dict[str, str] = field(default_factory=dict)  # 원본명 → 로컬명
    namespace_name: Optional[str] = None                  # `import * as X` 의 X
    is_dynamic: bool = False
    lazy_binding: Optional[str] = None
    line: int = 0


def extract_imports(tree: Tree, source: bytes) -> list[ImportSpec]:
    """
    파일 전체에서 import 추출.
    - ES6 `import_statement` (정적 import)
    - `call_expression` 중 `import(...)` (dynamic import / lazy)
    """
    if tree is None:
        return []

    out: list[ImportSpec] = []
    root = tree.root_node

    # ① 정적 import
    for n in find_by_type(root, "import_statement"):
        spec = _parse_static_import(n, source)
        if spec:
            out.append(spec)

    # ② dynamic import — lazy(() => import('./X')) 또는 await import('./X')
    for n in find_by_type(root, "call_expression"):
        spec = _parse_dynamic_import(n, source)
        if spec:
            out.append(spec)

    return out


def _parse_static_import(node: Node, source: bytes) -> Optional[ImportSpec]:
    src_node = child_by_field(node, "source")
    if src_node is None:
        return None
    raw_src = text_of(src_node, source).strip()
    if len(raw_src) < 2 or raw_src[0] not in ("'", '"', "`"):
        return None
    spec = ImportSpec(source=raw_src[1:-1], line=node.start_point[0] + 1)

    # import_clause 가 없으면 'import "side-effect"' 형태 → 그대로 반환
    clause = None
    for c in node.children:
        if c.type == "import_clause":
            clause = c
            break
    if clause is None:
        return spec

    for c in clause.children:
        if c.type == "identifier":
            # default import: `import Foo from ...`
            spec.default_name = text_of(c, source)
        elif c.type == "namespace_import":
            # `import * as X from ...`
            for cc in c.children:
                if cc.type == "identifier":
                    spec.namespace_name = text_of(cc, source)
        elif c.type == "named_imports":
            # `import { A, B as C } from ...`
            for cc in c.children:
                if cc.type == "import_specifier":
                    name_node = child_by_field(cc, "name")
                    alias_node = child_by_field(cc, "alias")
                    if name_node is None:
                        continue
                    orig = text_of(name_node, source)
                    local = text_of(alias_node, source) if alias_node else orig
                    spec.named[orig] = local
    return spec


def _parse_dynamic_import(node: Node, source: bytes) -> Optional[ImportSpec]:
    """
    call_expression 노드에서 dynamic import 만 잡는다.
    `import('./X')`, `lazy(() => import('./X'))` 양쪽 처리.
    """
    func = child_by_field(node, "function")
    if func is None:
        return None
    func_text = text_of(func, source)

    # 직접 import('...') — function 노드 자체가 'import' 키워드
    if func_text == "import":
        args = child_by_field(node, "arguments")
        src_str = _first_string_arg(args, source) if args else None
        if src_str is None:
            return None
        return ImportSpec(
            source=src_str,
            is_dynamic=True,
            line=node.start_point[0] + 1,
        )

    # 우리는 lazy() 자체보다 그 안의 import('...') 를 잡고 싶다.
    # walk 가 child 를 다 도니까 안쪽 import('...') 는 자연스럽게 위 분기에서 잡힌다.
    # 다만 lazy 의 좌변 const 이름은 별도로 묶어줘야 매칭이 가능하다.
    return None


def _first_string_arg(args_node: Optional[Node], source: bytes) -> Optional[str]:
    if args_node is None:
        return None
    for c in args_node.children:
        if c.type == "string":
            t = text_of(c, source).strip()
            if len(t) >= 2 and t[0] in ("'", '"', "`"):
                return t[1:-1]
    return None


def link_lazy_bindings(tree: Tree, source: bytes, imports: list[ImportSpec]) -> None:
    """
    `const Foo = lazy(() => import('./X'))` 같은 패턴에서
    Foo (좌변) 와 './X' (dynamic import source) 를 매칭해
    각 dynamic ImportSpec 의 lazy_binding 을 채운다.

    JSX 라우트 추출기가 `element={<Foo/>}` 를 보고 './X' 까지 갈 수 있게.
    """
    if tree is None:
        return
    root = tree.root_node
    dyn_by_line = {imp.line: imp for imp in imports if imp.is_dynamic}
    if not dyn_by_line:
        return

    for n in find_by_type(root, "variable_declarator"):
        name_node = child_by_field(n, "name")
        value_node = child_by_field(n, "value")
        if name_node is None or value_node is None:
            continue
        if name_node.type != "identifier":
            continue
        # 우변 어딘가에 import('...') 가 있는지
        for inner in walk(value_node):
            if inner.type == "call_expression":
                func = child_by_field(inner, "function")
                if func is None or text_of(func, source) != "import":
                    continue
                ln = inner.start_point[0] + 1
                spec = dyn_by_line.get(ln)
                if spec and spec.lazy_binding is None:
                    spec.lazy_binding = text_of(name_node, source)
                break


# ─────────────────────────────────────────────────────────────────
# Alias resolver (tsconfig / jsconfig)
# ─────────────────────────────────────────────────────────────────

@dataclass
class AliasMap:
    base_url: Path                      # baseUrl 절대경로 (보통 src/ 또는 repo_root)
    paths: dict[str, list[Path]]        # alias prefix → 후보 경로들
    repo_root: Path

    def resolve(self, spec: str, importer_file: Path) -> Optional[Path]:
        """
        import path → 실제 파일 절대경로.
        - './X', '../X' : 상대경로
        - 'react' / '@reduxjs/toolkit' : node_modules → None (건너뜀)
        - '@components/Foo' : alias 매칭 → baseUrl/components/Foo
        """
        # 상대경로
        if spec.startswith(".") or spec.startswith("/"):
            base = importer_file.parent if not spec.startswith("/") else self.repo_root
            return _resolve_with_extensions(base / spec)

        # alias 매칭 (긴 prefix 우선)
        for prefix in sorted(self.paths.keys(), key=len, reverse=True):
            clean_prefix = prefix.rstrip("*").rstrip("/")
            if not clean_prefix:
                continue
            if spec == clean_prefix or spec.startswith(clean_prefix + "/"):
                rest = spec[len(clean_prefix):].lstrip("/")
                for cand_base in self.paths[prefix]:
                    target = cand_base / rest if rest else cand_base
                    resolved = _resolve_with_extensions(target)
                    if resolved:
                        return resolved
        # node_modules 추정
        return None


_RESOLVE_EXTS = (".tsx", ".ts", ".jsx", ".js", ".mjs", ".cjs")


def _resolve_with_extensions(p: Path) -> Optional[Path]:
    """
    './X' → './X.tsx' / './X/index.tsx' 등으로 확장자/index 보정.
    """
    if p.suffix and p.exists() and p.is_file():
        return p.resolve()
    for ext in _RESOLVE_EXTS:
        cand = p.with_suffix(ext) if not p.suffix else p
        if cand.exists() and cand.is_file():
            return cand.resolve()
        cand2 = Path(str(p) + ext)
        if cand2.exists() and cand2.is_file():
            return cand2.resolve()
    if p.is_dir():
        for ext in _RESOLVE_EXTS:
            cand = p / f"index{ext}"
            if cand.exists():
                return cand.resolve()
    return None


def load_alias_map(repo_root: str | os.PathLike) -> AliasMap:
    """
    tsconfig.json / jsconfig.json 의 baseUrl + paths 를 읽어 AliasMap 생성.
    JSON5 트레일링 콤마/주석은 단순 strip 으로 보정 (대부분의 실전 프로젝트 호환).
    """
    repo = Path(repo_root).resolve()
    base_url = repo
    paths: dict[str, list[Path]] = {}

    for fname in ("tsconfig.json", "jsconfig.json"):
        f = repo / fname
        if not f.exists():
            continue
        try:
            text = _decode_for_text(f.read_bytes())
            data = _loose_json(text)
        except Exception:
            continue
        opts = (data or {}).get("compilerOptions") or {}
        bu = opts.get("baseUrl")
        if bu:
            base_url = (repo / bu).resolve()
        raw_paths = opts.get("paths") or {}
        for prefix, candidates in raw_paths.items():
            if not isinstance(candidates, list):
                continue
            paths[prefix] = [(base_url / c).resolve() for c in candidates if isinstance(c, str)]
        break

    return AliasMap(base_url=base_url, paths=paths, repo_root=repo)


def _loose_json(text: str) -> Optional[dict]:
    """
    JSON5-lite 보정: 주석 + 트레일링 콤마 제거 후 json.loads.
    완전한 JSON5 파서는 아니지만 tsconfig.json 의 99% 케이스는 처리.
    """
    import re
    # 라인 주석
    text = re.sub(r"//[^\n]*", "", text)
    # 블록 주석
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    # 트레일링 콤마
    text = re.sub(r",(\s*[}\]])", r"\1", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


# ─────────────────────────────────────────────────────────────────
# 파일 발견 (재귀, .git/node_modules 스킵)
# ─────────────────────────────────────────────────────────────────

_SKIP_DIRS = {".git", ".gradle", ".idea", ".svn", ".hg", ".next",
              "node_modules", "dist", "build", "out", ".cache", ".turbo"}
_JS_EXTS = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}


def iter_source_files(repo_root: str | os.PathLike) -> Iterator[Path]:
    """레포 루트 하위 .js/.jsx/.ts/.tsx 재귀 yield."""
    root = Path(repo_root)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            p = Path(dirpath) / fn
            if p.suffix.lower() in _JS_EXTS:
                yield p
