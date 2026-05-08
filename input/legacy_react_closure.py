"""
legacy_react_closure.py
========================

React 화면 단위 Dependency Closure 수집 + 팝업(Popup) 식별 모듈.

목적
----
화면 진입점 파일 1개를 받아, 그 화면을 구성하는 모든 자체 작성 코드를
import 그래프 BFS 로 묶어 LLM 분석에 적합한 형태로 직렬화한다.
부산물로 closure 안의 팝업 컴포넌트를 식별해, 동일 함수로 팝업 closure 도
빌드할 수 있도록 한다 ("팝업도 별도 화면으로 결과물").

입출력 인터페이스
-----------------
build_closure(entry_file, repo_root, patterns=None, ...) → ScreenClosure
    entry_file 이 화면이든 팝업이든 동일.
    화면 진입점 식별은 호출자(사용자 기존 분석기)가 담당.

serialize_for_llm(closure) → str
    Markdown 직렬화 (LLM user-message 로 그대로 사용 가능).

설계 원칙
---------
1. 정규식이 아닌 AST 기반 — 라우팅/UI 라이브러리 dialect 다양성 흡수.
2. node_modules 차단 — 자체 작성 코드만 closure 에 포함.
3. depth 별 mode 자동 강등 — 토큰 예산 관리 (full → signature → meta).
4. patterns.yaml 슬롯으로 휴리스틱 조정 — 사내 컨벤션 흡수.
5. 사실 우선 직렬화 — API 호출/팝업 ref 를 본문 앞에 박아 LLM 환각 차단.
"""

from __future__ import annotations

import os
import re
import sys
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from legacy_react_ast import (
    AliasMap,
    child_by_field,
    extract_imports,
    find_by_type,
    link_lazy_bindings,
    load_alias_map,
    parse_file,
    text_of,
    walk,
)


# ─────────────────────────────────────────────────────────────────
# 자료구조
# ─────────────────────────────────────────────────────────────────

@dataclass
class ApiCallSite:
    """closure 내 HTTP 호출 한 건."""
    file: str
    line: int
    method: str          # GET / POST / PUT / DELETE / PATCH / FETCH / UNKNOWN
    url: Optional[str]
    expr: str
    handler: Optional[str] = None


@dataclass
class PopupRef:
    """closure 내에서 발견된 팝업 컴포넌트 한 건."""
    component_name: str
    component_file: Optional[Path]   # build_closure 의 entry 로 재사용 가능
    invoked_from: str
    line: int
    trigger: str                     # 'jsx_inline' / 'use_hook' / 'open_api'
    expr: str


@dataclass
class ClosureFile:
    abs_path: Path
    rel_path: str
    depth: int
    mode: str            # 'full' / 'signature' / 'meta'
    content: str
    exports: list[str] = field(default_factory=list)
    estimated_tokens: int = 0


@dataclass
class ScreenClosure:
    entry_file: Path
    entry_name: str
    files: list[ClosureFile]
    api_calls: list[ApiCallSite]
    popup_refs: list[PopupRef]       # build_closure 가 미리 채움
    skipped_external: list[str]
    truncated: bool
    total_tokens: int


# ─────────────────────────────────────────────────────────────────
# 기본값 (patterns.yaml.react.* 미주입 시)
# ─────────────────────────────────────────────────────────────────

DEFAULT_DEPTH_MODE = {0: "full", 1: "full", 2: "signature", 3: "meta", 4: "meta"}
DEFAULT_API_WRAPPERS = ("apiClient", "http", "axios", "request", "api")
DEFAULT_API_METHODS = ("get", "post", "put", "delete", "patch")
DEFAULT_POPUP = {
    "file_suffixes":  ["Modal", "Popup", "Dialog", "Layer"],
    "jsx_components": ["Modal", "Dialog", "Drawer", "Popup", "Sheet", "Layer"],
    "open_hooks":     ["useModal", "useDialog", "openPopup", "useDrawer"],
    "open_apis":      ["ModalManager.open", "showDialog", "openModal"],
}


# ─────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────

def build_closure(
    entry_file: str | os.PathLike,
    repo_root: str | os.PathLike,
    patterns: Optional[dict] = None,
    *,
    max_depth: int = 3,
    token_budget: int = 12000,
    verbose: bool = False,
) -> ScreenClosure:
    """
    entry_file 부터 import 그래프 BFS → ScreenClosure.

    entry_file 이 일반 화면이든 팝업이든 동작 동일.
    호출자(사용자 기존 분석기)가 진입점을 결정한다.
    """
    repo = Path(repo_root).resolve()
    entry = Path(entry_file).resolve()
    alias = load_alias_map(repo)

    rcfg = (patterns or {}).get("react", {}) or {}
    api_cfg = rcfg.get("api_call", {}) or {}
    wrappers = tuple(api_cfg.get("wrappers") or DEFAULT_API_WRAPPERS)
    methods = tuple(api_cfg.get("methods") or DEFAULT_API_METHODS)

    depth_mode = dict(DEFAULT_DEPTH_MODE)
    for k, v in (rcfg.get("closure_depth_mode") or {}).items():
        try:
            depth_mode[int(k)] = v
        except (TypeError, ValueError):
            continue

    popup_cfg = _merge_popup_cfg(rcfg.get("popup"))

    # ── BFS ──
    visited: set[Path] = set()
    queue: deque[tuple[Path, int]] = deque([(entry, 0)])

    files: list[ClosureFile] = []
    api_calls: list[ApiCallSite] = []
    popup_refs: list[PopupRef] = []
    skipped_external: list[str] = []
    entry_name = entry.stem

    while queue:
        cur, depth = queue.popleft()
        if cur in visited or depth > max_depth:
            continue
        visited.add(cur)

        tree, source, _ = parse_file(cur)
        if tree is None:
            continue

        try:
            rel = str(cur.relative_to(repo))
        except ValueError:
            rel = str(cur)

        mode = depth_mode.get(depth, "meta")
        cf = _build_closure_file(cur, rel, depth, mode, tree, source)
        files.append(cf)
        if cur == entry and cf.exports:
            entry_name = cf.exports[0]

        api_calls.extend(_extract_api_calls(tree, source, rel, wrappers, methods))

        imports = extract_imports(tree, source)
        link_lazy_bindings(tree, source, imports)
        popup_refs.extend(_extract_popup_refs(
            tree, source, rel, imports, alias, cur, popup_cfg))

        if depth + 1 <= max_depth:
            for imp in imports:
                resolved = alias.resolve(imp.source, cur)
                if resolved is None:
                    skipped_external.append(imp.source)
                    continue
                if resolved not in visited:
                    queue.append((resolved, depth + 1))

    truncated = _enforce_token_budget(files, token_budget)
    total = sum(f.estimated_tokens for f in files)

    if verbose:
        by_mode: dict[str, int] = {}
        for f in files:
            by_mode[f.mode] = by_mode.get(f.mode, 0) + 1
        sys.stderr.write(
            f"  [closure] entry={entry_name}: {len(files)} files, "
            f"{len(api_calls)} api_calls, {len(popup_refs)} popup_refs, "
            f"by_mode={by_mode}, tokens={total}/{token_budget}"
            f"{', truncated=on' if truncated else ''}\n"
        )

    return ScreenClosure(
        entry_file=entry,
        entry_name=entry_name,
        files=files,
        api_calls=api_calls,
        popup_refs=_dedupe_popup_refs(popup_refs),
        skipped_external=sorted(set(skipped_external)),
        truncated=truncated,
        total_tokens=total,
    )


def serialize_for_llm(closure: ScreenClosure) -> str:
    """ScreenClosure → Markdown (LLM user-message 로 그대로 사용 가능)."""
    e = closure
    parts: list[str] = []
    parts.append(f"# Screen: `{e.entry_name}`")
    parts.append(f"- entry file: `{e.entry_file.name}`")
    parts.append(f"- files in closure: {len(e.files)}")
    parts.append("")

    if e.api_calls:
        parts.append("## API calls (factual, extracted from AST)")
        for a in e.api_calls:
            handler = f"  handler=`{a.handler}`" if a.handler else ""
            parts.append(f"- `{a.method}` `{a.url or '(dynamic)'}` "
                         f"in `{a.file}:{a.line}`{handler}")
        parts.append("")

    if e.popup_refs:
        parts.append("## Popups invoked from this screen (factual)")
        for p in e.popup_refs:
            file_hint = f" → `{p.component_file}`" if p.component_file else ""
            parts.append(f"- `{p.component_name}` (trigger=`{p.trigger}`) "
                         f"in `{p.invoked_from}:{p.line}`{file_hint}")
        parts.append("")

    for f in e.files:
        parts.append(f"## File: `{f.rel_path}`  (depth={f.depth}, mode={f.mode})")
        if f.exports:
            parts.append(f"_exports_: {', '.join(f.exports)}")
        parts.append("")
        fence = "```jsx" if f.rel_path.endswith((".jsx", ".tsx")) else "```js"
        parts.append(fence)
        parts.append(f.content)
        parts.append("```")
        parts.append("")

    if e.skipped_external:
        parts.append("## External imports (excluded — node_modules)")
        parts.append(", ".join(f"`{x}`" for x in e.skipped_external))
        parts.append("")

    if e.truncated:
        parts.append("> **Note**: closure truncated due to token budget.")
    return "\n".join(parts)


# ─────────────────────────────────────────────────────────────────
# 파일 → ClosureFile (depth/mode 별)
# ─────────────────────────────────────────────────────────────────

def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _build_closure_file(abs_path: Path, rel_path: str, depth: int, mode: str,
                        tree, source: bytes) -> ClosureFile:
    sigs, exports = _extract_signatures(tree, source)
    if mode == "full":
        content = source.decode("utf-8", errors="replace")
    elif mode == "signature":
        skel = _extract_jsx_skeleton(tree, source)
        parts = []
        if sigs:
            parts.append("// exports")
            parts.extend(sigs)
        if skel:
            parts.append("\n// JSX skeleton")
            parts.append(skel)
        content = "\n".join(parts) if parts else "// (no exports / JSX detected)"
    else:  # meta
        content = f"// exports: {', '.join(exports) if exports else '(none)'}"
    return ClosureFile(
        abs_path=abs_path, rel_path=rel_path, depth=depth, mode=mode,
        content=content, exports=exports,
        estimated_tokens=_estimate_tokens(content),
    )


def _extract_signatures(tree, source: bytes) -> tuple[list[str], list[str]]:
    if tree is None:
        return [], []
    sigs, names = [], []
    for n in walk(tree.root_node):
        if n.type != "export_statement":
            continue
        first_line = text_of(n, source).split("\n", 1)[0].rstrip()
        if first_line and not first_line.endswith(("{", "(")):
            sigs.append(first_line)
        else:
            txt = text_of(n, source)
            sig_end = txt.find("{")
            sigs.append(txt[: sig_end if sig_end >= 0 else len(txt)].rstrip() + " { ... }")
        for inner in walk(n):
            if inner.type in ("function_declaration", "class_declaration"):
                nm = child_by_field(inner, "name")
                if nm: names.append(text_of(nm, source))
                break
            if inner.type == "variable_declarator":
                nm = child_by_field(inner, "name")
                if nm and nm.type == "identifier":
                    names.append(text_of(nm, source))
    return sigs, list(dict.fromkeys(names))


def _extract_jsx_skeleton(tree, source: bytes, max_lines: int = 30) -> str:
    if tree is None:
        return ""
    candidates = []
    for n in find_by_type(tree.root_node, "return_statement"):
        for c in walk(n):
            if c.type in ("jsx_element", "jsx_self_closing_element", "jsx_fragment"):
                candidates.append(c); break
    if not candidates:
        for c in walk(tree.root_node):
            if c.type in ("jsx_element", "jsx_self_closing_element"):
                candidates.append(c); break
    if not candidates:
        return ""
    skel = _render_jsx_skeleton(candidates[0], source, depth=0)
    lines = skel.split("\n")
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"  ... ({len(lines) - max_lines} more lines)"]
    return "\n".join(lines)


def _render_jsx_skeleton(node, source: bytes, depth: int) -> str:
    indent = "  " * depth
    if node.type == "jsx_self_closing_element":
        n = child_by_field(node, "name")
        return f"{indent}<{text_of(n, source) if n else '?'}{_short_attrs(node, source)} />"
    if node.type == "jsx_fragment":
        children = [
            _render_jsx_skeleton(c, source, depth + 1)
            for c in node.children
            if c.type in ("jsx_element", "jsx_self_closing_element")
        ]
        body = "\n".join(children) if children else f"{'  '*(depth+1)}..."
        return f"{indent}<>\n{body}\n{indent}</>"
    if node.type == "jsx_element":
        opening = next((c for c in node.children if c.type == "jsx_opening_element"), None)
        name, attrs = "?", ""
        if opening is not None:
            n = child_by_field(opening, "name")
            name = text_of(n, source) if n else "?"
            attrs = _short_attrs(opening, source)
        children = []
        for c in node.children:
            if c.type in ("jsx_element", "jsx_self_closing_element", "jsx_fragment"):
                children.append(_render_jsx_skeleton(c, source, depth + 1))
            elif c.type == "jsx_expression":
                for inner in walk(c):
                    if inner.type in ("jsx_element", "jsx_self_closing_element"):
                        children.append(_render_jsx_skeleton(inner, source, depth + 1)); break
        if children:
            return f"{indent}<{name}{attrs}>\n" + "\n".join(children) + f"\n{indent}</{name}>"
        return f"{indent}<{name}{attrs}>...</{name}>"
    return ""


def _short_attrs(elem_node, source: bytes, max_per: int = 30) -> str:
    parts = []
    for c in elem_node.children:
        if c.type != "jsx_attribute" or not c.children:
            continue
        nm = c.children[0]
        if nm.type != "property_identifier":
            continue
        name = text_of(nm, source)
        val_node = next((cc for cc in c.children[1:] if cc.type in ("string", "jsx_expression")), None)
        if val_node is None:
            parts.append(f" {name}")
        elif val_node.type == "string":
            v = text_of(val_node, source)
            parts.append(f" {name}={v[:max_per] + '...\"' if len(v) > max_per else v}")
        else:
            parts.append(f" {name}={{...}}")
        if len(parts) >= 5:
            parts.append(" ..."); break
    return "".join(parts)


# ─────────────────────────────────────────────────────────────────
# API 호출 추출
# ─────────────────────────────────────────────────────────────────

def _extract_api_calls(tree, source: bytes, rel: str,
                       wrappers: tuple, methods: tuple) -> list[ApiCallSite]:
    if tree is None:
        return []
    out: list[ApiCallSite] = []
    method_set = {m.lower() for m in methods}
    wrapper_set = set(wrappers)

    for n in find_by_type(tree.root_node, "call_expression"):
        func = child_by_field(n, "function")
        if func is None:
            continue
        method, recognized = None, False

        if func.type == "member_expression":
            obj = child_by_field(func, "object")
            prop = child_by_field(func, "property")
            if obj is None or prop is None:
                continue
            obj_name = text_of(obj, source).split(".")[-1]
            prop_name = text_of(prop, source).lower()
            if prop_name in method_set:
                if obj_name in wrapper_set or any(w in obj_name.lower() for w in ("client", "api", "http", "axios")):
                    method, recognized = prop_name.upper(), True
        elif func.type == "identifier" and text_of(func, source) == "fetch":
            method, recognized = "FETCH", True

        if not recognized:
            continue

        args = child_by_field(n, "arguments")
        url, _ = _first_url_arg(args, source) if args else (None, "")
        expr_text = text_of(n, source)
        if len(expr_text) > 200:
            expr_text = expr_text[:200] + "..."

        out.append(ApiCallSite(
            file=rel, line=n.start_point[0] + 1,
            method=method or "UNKNOWN", url=url,
            expr=expr_text.replace("\n", " "),
            handler=_enclosing_function_name(n, source),
        ))
    return out


def _first_url_arg(args_node, source: bytes) -> tuple[Optional[str], str]:
    for c in args_node.children:
        if c.type == "string":
            t = text_of(c, source).strip()
            if len(t) >= 2 and t[0] in ("'", '"'):
                return t[1:-1], t
        if c.type == "template_string":
            t = text_of(c, source)
            return re.sub(r"\$\{[^}]+\}", "{p}", t.strip("`")), t
    return None, ""


def _enclosing_function_name(node, source: bytes) -> Optional[str]:
    cur = node.parent
    while cur is not None:
        if cur.type == "function_declaration":
            nm = child_by_field(cur, "name")
            if nm: return text_of(nm, source)
        if cur.type == "method_definition":
            nm = child_by_field(cur, "name")
            if nm: return text_of(nm, source)
        if cur.type == "variable_declarator":
            nm = child_by_field(cur, "name")
            val = child_by_field(cur, "value")
            if val is not None and val.type in ("arrow_function", "function_expression") and nm is not None:
                return text_of(nm, source)
        cur = cur.parent
    return None


# ─────────────────────────────────────────────────────────────────
# 팝업 식별
# ─────────────────────────────────────────────────────────────────

def _merge_popup_cfg(user: Optional[dict]) -> dict:
    out = {k: list(v) for k, v in DEFAULT_POPUP.items()}
    user = user or {}
    for k in out:
        extra = user.get(k) or []
        if isinstance(extra, list):
            out[k] = list(dict.fromkeys(out[k] + extra))
    return out


def _is_popup_component_name(name: str, cfg: dict) -> bool:
    if not name:
        return False
    if name in cfg["jsx_components"]:
        return True
    return any(name.endswith(suf) for suf in cfg["file_suffixes"])


def _extract_popup_refs(
    tree, source: bytes, rel: str,
    imports: list, alias: AliasMap, importer: Path, cfg: dict,
) -> list[PopupRef]:
    """
    세 가지 신호로 팝업 호출 site 식별:
    (1) JSX 태그   — <FooModal /> / <Dialog /> 등 (suffix 또는 jsx_components 매칭)
    (2) Hook 호출  — useModal()/useDialog() (open_hooks)
    (3) Open API   — ModalManager.open(...) (open_apis)
    """
    if tree is None:
        return []
    out: list[PopupRef] = []
    open_hooks = set(cfg["open_hooks"])
    open_apis = set(cfg["open_apis"])

    # (1) JSX 태그
    for n in walk(tree.root_node):
        if n.type not in ("jsx_self_closing_element", "jsx_element"):
            continue
        if n.type == "jsx_self_closing_element":
            name_n = child_by_field(n, "name")
        else:
            opening = next((c for c in n.children if c.type == "jsx_opening_element"), None)
            name_n = child_by_field(opening, "name") if opening else None
        if name_n is None:
            continue
        comp_name = text_of(name_n, source)
        if not _is_popup_component_name(comp_name, cfg):
            continue
        comp_file = _resolve_component_file(comp_name, imports, importer, alias)
        out.append(PopupRef(
            component_name=comp_name, component_file=comp_file,
            invoked_from=rel, line=n.start_point[0] + 1,
            trigger="jsx_inline",
            expr=text_of(n, source).split("\n", 1)[0][:120],
        ))

    # (2) + (3) 호출 패턴
    for n in find_by_type(tree.root_node, "call_expression"):
        func = child_by_field(n, "function")
        if func is None:
            continue
        full = text_of(func, source)
        if func.type == "identifier" and full in open_hooks:
            out.append(PopupRef(
                component_name=full, component_file=None,
                invoked_from=rel, line=n.start_point[0] + 1,
                trigger="use_hook", expr=text_of(n, source)[:120],
            ))
        elif func.type == "member_expression" and full in open_apis:
            out.append(PopupRef(
                component_name=full, component_file=None,
                invoked_from=rel, line=n.start_point[0] + 1,
                trigger="open_api", expr=text_of(n, source)[:120],
            ))
    return out


def _dedupe_popup_refs(refs: list[PopupRef]) -> list[PopupRef]:
    seen = set()
    out = []
    for r in refs:
        key = (r.component_name, r.invoked_from, r.line, r.trigger)
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def _resolve_component_file(name: str, imports, importer: Path, alias: AliasMap) -> Optional[Path]:
    """팝업 컴포넌트 이름 → 실제 파일 경로 (default / lazy / named import 모두 매칭)."""
    for imp in imports:
        if imp.default_name == name or imp.lazy_binding == name:
            return alias.resolve(imp.source, importer)
    for imp in imports:
        for orig, local in imp.named.items():
            if local == name or orig == name:
                return alias.resolve(imp.source, importer)
    return None


# ─────────────────────────────────────────────────────────────────
# 토큰 예산 강제
# ─────────────────────────────────────────────────────────────────

def _enforce_token_budget(files: list[ClosureFile], budget: int) -> bool:
    truncated = False
    while sum(f.estimated_tokens for f in files) > budget:
        target = None
        for f in sorted(files, key=lambda x: -x.depth):
            if f.mode == "full":
                f.mode = "signature"
                lines = f.content.split("\n")
                f.content = "\n".join(lines[:30]) + "\n// ... (truncated)"
                target = f; break
            if f.mode == "signature":
                f.mode = "meta"
                f.content = f"// exports: {', '.join(f.exports) if f.exports else '(none)'}"
                target = f; break
        if target is None:
            break
        target.estimated_tokens = _estimate_tokens(target.content)
        truncated = True
    return truncated
