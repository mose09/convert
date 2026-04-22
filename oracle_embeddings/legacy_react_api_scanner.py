"""React/Polymer 소스에서 API URL 호출을 추출해 URL ↔ 파일 인덱스를 구축.

analyze-legacy 2-hop 매칭의 중간 단계. 프론트엔드 컴포넌트 body 에서
``axios.post('/api/...')`` / ``fetch('/api/...')`` / 커스텀 래퍼
``httpClient.post('/api/...')`` 같은 호출을 뽑아, 그 URL 을 각 호출
파일에 역으로 맵핑한다:

    {normalized_api_url: [component_file_path, ...]}

메뉴 → 앱 프론트 버킷 → 이 인덱스 → 컨트롤러 URL 로 이어지는 체인에서
"이 endpoint 를 호출하는 화면(들)" 을 찾는 데 쓰인다.

호출 메서드 목록은 ``patterns.yaml`` 의 ``frontend.api_call_methods``
로 주입된다 (LLM 이 discover-patterns 단계에서 학습). 기본 세트는 가장
흔한 axios/fetch 패턴.

URL 해석은 세 단계:
  1. 리터럴:  ``axios.get('/api/foo')``
  2. 상수 2-pass:  ``const URL = '/api/foo'; axios.get(URL)``
  3. 템플릿 리터럴:  `` axios.get(`/api/foo/${id}`) `` → ``/api/foo/{p}``

추출된 URL 은 ``legacy_util.normalize_url`` 로 정규화해서 백엔드
엔드포인트 쪽 key 와 직접 비교 가능한 형태로 저장.

버튼 라벨(트리거) 추출은 ``extract_button_triggers`` 에서 별도 책임:
컴포넌트 안의 ``<Button>조회</Button>`` 과 ``onClick={handler}`` 를
엮어 handler→label 맵을 만들고, handler body 에서 발견한 API URL 과
연결한다.
"""

from __future__ import annotations

import logging
import os
import re

from .legacy_util import normalize_url
from .mybatis_parser import _read_file_safe

logger = logging.getLogger(__name__)


SKIP_DIRS = {"node_modules", "build", "dist", ".next", ".git", "coverage", "bower_components"}
EXTENSIONS = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".vue", ".html"}


_DEFAULT_API_METHODS = (
    "axios.get", "axios.post", "axios.put", "axios.patch", "axios.delete",
    "axios.request",
    "fetch",
)


# redux-saga effects 래퍼: URL 이 2번째 인자에 오거나 (A), 1번째 인자가
# api 모듈 함수 참조라 URL 이 다른 파일에 있는 경우 (B) 를 잡기 위한 패턴.
# ``call``/``apply`` 는 redux-saga 의 기본 effects 이름. 같은 이름이
# ``Function.prototype.call`` 에서도 쓰이지만 그 경우 1번째 인자가
# ``this`` 문맥 + 2번째 인자가 URL 리터럴인 경우는 실무에서 드물다.
_SAGA_WRAPPERS = ("call", "apply")


# A: ``call(fn, '/api/x', ...)`` / ``apply(fn, '/api/x', ...)``
# 1번째 인자는 식별자 또는 dotted 참조 (axios.get / api.fetchUser).
# 2번째 인자는 URL 리터럴 (리터럴이 없으면 매칭 안 됨 → 기존 regex 와
# 역할 분리가 명확).
_SAGA_CALL_LITERAL_RE = re.compile(
    r"""\b(?P<wrapper>call|apply)\s*\(
         \s*(?P<fn>[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\s*,
         \s*(?P<quote>['"`])(?P<url>(?:https?:/)?/[^'"`\n]+?)(?P=quote)
     """,
    re.VERBOSE,
)


# B: ``call(fn, ...)`` / ``call(X.fn, ...)`` — 1번째 인자의 dotted 참조
# 마지막 segment 만 캡처 (``api.fetchUser`` → ``fetchUser``). api 모듈 함수
# 인덱스 lookup 용.
_SAGA_CALL_INDIRECT_RE = re.compile(
    r"""\b(?P<wrapper>call|apply)\s*\(
         \s*(?:[A-Za-z_$][\w$]*\.)?(?P<fn>[A-Za-z_$][\w$]*)\s*[,)]
     """,
    re.VERBOSE,
)


# B 가 잘못 잡을 만한 일반 이름 — Function.prototype.call 계열. 이 이름들은
# api 함수 인덱스에 있더라도 false positive 방지를 위해 skip.
_SAGA_INDIRECT_SKIP_NAMES = frozenset({
    "this", "self", "bind", "call", "apply", "console",
    "Object", "Array", "String", "Number", "Boolean",
})


def _scan_dir(base: str) -> list[str]:
    out = []
    for root, dirs, names in os.walk(base):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for n in names:
            if os.path.splitext(n)[1].lower() in EXTENSIONS:
                out.append(os.path.join(root, n))
    return out


# ── URL extraction ───────────────────────────────────────────────


# URL-ish string: starts with / or http, reaches end-quote. Templates
# are handled separately by replacing ``${...}`` with ``{p}``.
_STR_LITERAL = r"""(?:'[^']{1,500}'|"[^"]{1,500}"|`[^`]{1,500}`)"""
_URL_LITERAL_RE = re.compile(r"""['"`](?P<url>(?:https?:/)?/[^'"`\s]+?)['"`]""")


def _build_call_regex(methods: list[str]) -> re.Pattern:
    """Build an API-call regex from a methods list.

    Each method is dotted path like ``axios.get`` or single name ``fetch``.
    We match either full dotted form or the leaf call. The URL argument
    has to look like a path (leading ``/`` or ``http``) so we don't pick
    up every ``array.get(idx)`` style call.

    The final regex is case-insensitive so a method declared as
    ``axios.get`` in patterns.yaml also matches ``Axios.get`` / ``AXIOS.GET``
    in source. Mixed-case variants are common in legacy codebases.
    """
    dotted_names = [m for m in methods if "." in m]
    bare_names = [m for m in methods if "." not in m]
    alts = []
    for name in dotted_names:
        alts.append(re.escape(name))
    for name in bare_names:
        alts.append(re.escape(name))
    if not alts:
        return None  # type: ignore[return-value]
    alt = "|".join(sorted(set(alts), key=len, reverse=True))
    return re.compile(
        rf"""\b(?P<method>{alt})\s*\(
             \s*(?:
                   (?P<quote>['"`])(?P<url>(?:https?:/)?/[^'"`\n]+?)(?P=quote)
                 | (?P<var>\w+)
             )
         """,
        re.VERBOSE | re.IGNORECASE,
    )


_URL_CONST_RE = re.compile(
    r"""(?:const|let|var|export\s+const)\s+(?P<name>[A-Z_][A-Z0-9_]{2,})\s*=\s*
        ['"`](?P<url>(?:https?:/)?/[^'"`\n]+?)['"`]""",
    re.VERBOSE,
)


def _collect_url_constants(files: list[str], const_files_hint: list[str]) -> dict[str, str]:
    """Return ``{CONST_NAME: url_literal}`` by scanning candidate files.

    ``const_files_hint`` are preferred — we scan every file whose relative
    path matches any hint substring first, then fall back to the rest.
    """
    const_map: dict[str, str] = {}
    hint_set = {h for h in (const_files_hint or []) if h}

    def _is_hinted(fp: str) -> bool:
        if not hint_set:
            return False
        lowered = fp.replace("\\", "/").lower()
        return any(h.lower() in lowered for h in hint_set)

    # Scan hinted files first so that hinted values win in case of
    # duplicate names.
    ordered = sorted(files, key=lambda p: (0 if _is_hinted(p) else 1, p))
    for fp in ordered:
        try:
            content = _read_file_safe(fp, limit=80000)
        except Exception:
            continue
        for m in _URL_CONST_RE.finditer(content):
            name = m.group("name")
            url = m.group("url")
            const_map.setdefault(name, url)
    return const_map


# ── Global function body index (B) ──────────────────────────────


# top-level 선언 + 클래스/객체 메서드 shorthand. body 는 선언 시점부터 4000
# 문자까지 slice 해서 가볍게 유지 — 정확한 balanced-brace 파싱은 regex
# 스캐너 범위 밖. 이 slice 안에서 axios/fetch URL 을 찾는 게 목적.
_FN_DECL_RE = re.compile(
    r"""(?:^|[\n;{}])\s*
        (?:export\s+(?:default\s+)?)?
        (?:
            (?:async\s+)?function\s*\*?\s*(?P<fn_func>[A-Za-z_$][\w$]*)\s*\(
          | (?:const|let|var)\s+(?P<fn_arrow>[A-Za-z_$][\w$]*)\s*=\s*
              (?:async\s*)?(?:function\s*\*?\s*\([^)]*\)|\([^)]*\)\s*=>|\w+\s*=>)
        )
    """,
    re.VERBOSE | re.MULTILINE,
)


def _collect_function_bodies(files: list[str]) -> dict[str, list[tuple[str, str]]]:
    """Return ``{fn_name: [(file_path, body_slice), ...]}`` for API resolution.

    Single-pass scan of every candidate file. Name collisions (same ``fn``
    defined in multiple files) keep all entries — the indirect resolver
    walks every candidate body since we can't cheaply resolve imports via
    regex. Over-resolution is harmless: resolved bodies that don't contain
    API calls simply produce no URLs.
    """
    index: dict[str, list[tuple[str, str]]] = {}
    for fp in files:
        try:
            content = _read_file_safe(fp, limit=80000)
        except Exception:
            continue
        for m in _FN_DECL_RE.finditer(content):
            name = m.group("fn_func") or m.group("fn_arrow")
            if not name:
                continue
            start = m.start()
            body = content[start : start + 4000]
            index.setdefault(name, []).append((fp, body))
    return index


def _scan_body_for_urls(body: str, call_re, const_map: dict[str, str],
                         strip_patterns) -> set[str]:
    """Extract normalized URLs from a function body slice.

    Shared between direct-call scanning and indirect saga resolution so
    both paths use the same literal/template/const-var logic.
    """
    out: set[str] = set()
    for m in call_re.finditer(body):
        raw = m.group("url") or ""
        if not raw:
            var = m.group("var") or ""
            if not var:
                continue
            raw = const_map.get(var, "")
            if not raw:
                continue
        canonical = normalize_url(_normalize_template(raw), strip_patterns)
        if canonical:
            out.add(canonical)
    return out


def _normalize_template(url: str) -> str:
    """Replace JS template expressions ``${...}`` with the ``{p}`` token.

    Called before passing to ``normalize_url`` so dynamic segments collapse
    to the same canonical form as Spring ``{id}`` / React Router ``:id``.
    """
    return re.sub(r"\$\{[^}]+\}", "{p}", url)


def build_api_url_index(frontend_dir: str, patterns: dict | None = None,
                         strip_patterns=None) -> dict[str, list[str]]:
    """Return ``{normalized_api_url: [source_file, ...]}`` for the dir.

    Keys are deduplicated case-insensitively (``normalize_url`` lowercases);
    values are a **sorted list of source files** (relative to
    ``frontend_dir`` for readability) so a report can show every screen
    that issues the same API call.

    ``patterns["frontend"].api_call_methods`` extends the built-in
    ``axios.*`` / ``fetch`` set with project-specific wrappers learned by
    ``discover-patterns``.
    """
    if not frontend_dir or not os.path.isdir(frontend_dir):
        return {}

    fe = (patterns or {}).get("frontend") or {}
    methods = list(_DEFAULT_API_METHODS) + list(fe.get("api_call_methods") or [])
    const_files_hint = fe.get("api_url_const_files") or []

    call_re = _build_call_regex(methods)
    if call_re is None:
        return {}

    files = _scan_dir(frontend_dir)
    const_map = _collect_url_constants(files, const_files_hint)

    index: dict[str, set[str]] = {}
    matches_count = 0
    saga_literal_count = 0
    saga_indirect_count = 0

    # 각 파일의 내용을 한 번만 읽어 재사용 — B (간접 호출 해석) 에서도
    # 같은 content 를 다시 훑기 때문.
    file_contents: dict[str, str] = {}
    for fp in files:
        try:
            file_contents[fp] = _read_file_safe(fp)
        except Exception:
            continue

    # ── Phase 0: 직접 호출 (axios.get / fetch / custom wrapper) ──
    for fp, content in file_contents.items():
        rel = os.path.relpath(fp, frontend_dir)
        for m in call_re.finditer(content):
            raw = m.group("url") or ""
            if not raw:
                var = m.group("var") or ""
                if not var:
                    continue
                raw = const_map.get(var, "")
                if not raw:
                    continue
            canonical = normalize_url(_normalize_template(raw), strip_patterns)
            if not canonical:
                continue
            index.setdefault(canonical, set()).add(rel)
            matches_count += 1

    # ── Phase A: redux-saga ``call(fn, '/url')`` — URL 이 2번째 인자 ──
    # 1번째 인자의 leaf name 이 ``Function.prototype.call`` 계열 (this/bind
    # 등) 이면 skip. ``arr.call(this, '/path')`` 같은 false positive 방지.
    for fp, content in file_contents.items():
        if "call(" not in content and "apply(" not in content:
            continue
        rel = os.path.relpath(fp, frontend_dir)
        for m in _SAGA_CALL_LITERAL_RE.finditer(content):
            fn_ref = m.group("fn") or ""
            leaf = fn_ref.rsplit(".", 1)[-1]
            if leaf in _SAGA_INDIRECT_SKIP_NAMES:
                continue
            raw = m.group("url") or ""
            if not raw:
                continue
            canonical = normalize_url(_normalize_template(raw), strip_patterns)
            if not canonical:
                continue
            index.setdefault(canonical, set()).add(rel)
            saga_literal_count += 1

    # ── Phase B: redux-saga ``call(api.fn)`` — 간접 호출 해석 ──
    # 전역 함수 body 인덱스를 빌드해서 saga 파일에서 참조하는 함수 이름을
    # 본체까지 따라간다. import 해석 없이 이름 기반 global lookup — 같은
    # 이름이 여러 파일에 있으면 모두 후보로 스캔. 후보 본체에 실제로
    # axios/fetch 호출이 없으면 자연스럽게 매칭 없음.
    fn_index = _collect_function_bodies(files)
    for fp, content in file_contents.items():
        if "call(" not in content and "apply(" not in content:
            continue
        rel = os.path.relpath(fp, frontend_dir)
        seen_fns_here: set[str] = set()
        for m in _SAGA_CALL_INDIRECT_RE.finditer(content):
            fn_name = m.group("fn") or ""
            if not fn_name or fn_name in _SAGA_INDIRECT_SKIP_NAMES:
                continue
            if fn_name in seen_fns_here:
                continue
            seen_fns_here.add(fn_name)
            bodies = fn_index.get(fn_name) or []
            for (_body_fp, body) in bodies:
                for url in _scan_body_for_urls(body, call_re, const_map, strip_patterns):
                    index.setdefault(url, set()).add(rel)
                    saga_indirect_count += 1

    logger.info("API URL index for %s: %d urls from %d calls across %d files "
                "(saga literal=%d, saga indirect=%d)",
                frontend_dir, len(index), matches_count, len(files),
                saga_literal_count, saga_indirect_count)
    return {k: sorted(v) for k, v in index.items()}


# ── Button trigger extraction (Phase E) ──────────────────────────


_BUTTON_LABEL_RE = re.compile(
    # <Button ... onClick={handler}>조회</Button> 형태. 속성 순서에 관계없이
    # onClick + 라벨(children)을 함께 포착. 라벨이 매우 짧고 공백 정도면 허용.
    r"<(?P<tag>[A-Z]\w*|button)\b(?P<attrs>[^>]*?)>\s*(?P<label>[^<]{1,40}?)\s*</(?P=tag)>",
    re.DOTALL,
)
_ON_HANDLER_RE = re.compile(
    r"""\bon(?:Click|Submit|Change)\s*=\s*\{\s*
        (?:
            (?P<name>\w+)
          | \(?\s*\)\s*=>\s*(?P<arrow>\w+)\s*\(
        )
    """,
    re.VERBOSE,
)


def extract_button_triggers(frontend_dir: str, api_index: dict[str, list[str]],
                              patterns: dict | None = None,
                              strip_patterns=None) -> dict[str, list[str]]:
    """Return ``{normalized_api_url: [button_label, ...]}``.

    Heuristic: for each source file that contains an API call, scan for
    ``<Button onClick={handler}>조회</Button>`` pairs. Then look for the
    ``handler`` body in the same file and collect any API URLs it
    references (including URL constants via a 2-pass resolver). Associate
    those URLs with the button's label.

    Extremely imperfect — misses cross-file handlers, HOC-wrapped
    buttons, dynamically-bound handlers. Still gives a usable first
    pass; anything unmapped stays blank.
    """
    if not frontend_dir or not os.path.isdir(frontend_dir) or not api_index:
        return {}

    fe = (patterns or {}).get("frontend") or {}
    methods = list(_DEFAULT_API_METHODS) + list(fe.get("api_call_methods") or [])
    const_files_hint = fe.get("api_url_const_files") or []
    call_re = _build_call_regex(methods)
    if call_re is None:
        return {}

    # Reverse index: file → set of URLs it calls (to quickly iterate files
    # that actually matter).
    files_with_calls: set[str] = set()
    for url, files in api_index.items():
        for f in files:
            files_with_calls.add(os.path.join(frontend_dir, f))

    all_files = _scan_dir(frontend_dir)
    const_map = _collect_url_constants(all_files, const_files_hint)

    triggers: dict[str, set[str]] = {}

    for fp in files_with_calls:
        try:
            content = _read_file_safe(fp)
        except Exception:
            continue

        # Collect handler → label pairs.
        handler_label: dict[str, str] = {}
        for m in _BUTTON_LABEL_RE.finditer(content):
            attrs = m.group("attrs") or ""
            label = (m.group("label") or "").strip()
            if not label or len(label) > 30:
                continue
            hm = _ON_HANDLER_RE.search(attrs)
            if not hm:
                continue
            handler = hm.group("name") or hm.group("arrow")
            if handler:
                handler_label.setdefault(handler, label)

        if not handler_label:
            continue

        # For each handler, grab the function body (def or arrow) and
        # scan for API calls. We keep the scan heuristic: from the
        # handler definition to the next top-level closing brace at the
        # same indent-ish, capped at 4000 chars.
        for handler, label in handler_label.items():
            body = _locate_handler_body(content, handler)
            if not body:
                continue
            for m in call_re.finditer(body):
                raw = m.group("url") or ""
                if not raw:
                    var = m.group("var") or ""
                    if not var:
                        continue
                    raw = const_map.get(var, "")
                    if not raw:
                        continue
                canonical = normalize_url(_normalize_template(raw), strip_patterns)
                if not canonical:
                    continue
                triggers.setdefault(canonical, set()).add(label)

    return {k: sorted(v) for k, v in triggers.items()}


def collect_handler_contexts(
    frontend_dir: str,
    api_index: dict[str, list[str]],
    patterns: dict | None = None,
    strip_patterns=None,
) -> dict[str, list[dict]]:
    """Phase B biz extraction 전용 수집기.

    Returns ``{normalized_api_url: [{file, handler, label, body, jsx_slice,
    validation_props}, ...]}``. ``extract_button_triggers`` 와 같은 pass
    를 살짝 확장해 LLM 에 필요한 컨텍스트 (긴 handler body + 앞뒤 JSX
    + pre-annotated JSX validation props) 까지 쌓아서 반환.
    """
    if not frontend_dir or not os.path.isdir(frontend_dir) or not api_index:
        return {}

    fe = (patterns or {}).get("frontend") or {}
    methods = list(_DEFAULT_API_METHODS) + list(fe.get("api_call_methods") or [])
    const_files_hint = fe.get("api_url_const_files") or []
    call_re = _build_call_regex(methods)
    if call_re is None:
        return {}

    files_with_calls: set[str] = set()
    for url, files in api_index.items():
        for f in files:
            files_with_calls.add(os.path.join(frontend_dir, f))

    all_files = _scan_dir(frontend_dir)
    const_map = _collect_url_constants(all_files, const_files_hint)

    out: dict[str, list[dict]] = {}

    for fp in files_with_calls:
        try:
            content = _read_file_safe(fp)
        except Exception:
            continue

        handler_label: dict[str, str] = {}
        for m in _BUTTON_LABEL_RE.finditer(content):
            attrs = m.group("attrs") or ""
            label = (m.group("label") or "").strip()
            if not label or len(label) > 30:
                continue
            hm = _ON_HANDLER_RE.search(attrs)
            if not hm:
                continue
            handler = hm.group("name") or hm.group("arrow")
            if handler:
                handler_label.setdefault(handler, label)

        if not handler_label:
            continue

        rel = os.path.relpath(fp, frontend_dir)

        for handler, label in handler_label.items():
            start = _locate_handler_start(content, handler)
            if start is None:
                continue
            body = content[start: start + 8000]
            jsx = _locate_enclosing_jsx(content, start)
            validation_props = extract_validation_props(jsx)

            urls_in_handler: set[str] = set()
            for m in call_re.finditer(body):
                raw = m.group("url") or ""
                if not raw:
                    var = m.group("var") or ""
                    if not var:
                        continue
                    raw = const_map.get(var, "")
                    if not raw:
                        continue
                canonical = normalize_url(_normalize_template(raw), strip_patterns)
                if canonical:
                    urls_in_handler.add(canonical)

            ctx = {
                "file": rel,
                "handler": handler,
                "label": label,
                "body": body,
                "jsx_slice": jsx,
                "validation_props": validation_props,
            }
            for u in urls_in_handler:
                out.setdefault(u, []).append(ctx)

    return out


def _locate_handler_body(content: str, handler: str, max_chars: int = 4000) -> str:
    """Find the declaration of ``handler`` and return a rough body slice.

    Matches ``function handler(...)`` and ``const handler = (...) => ...``
    style. Slice is capped at ``max_chars`` chars (default 4000 for the
    trigger-extraction path; Phase B biz extraction passes 8000 to catch
    longer handlers).
    """
    start = _locate_handler_start(content, handler)
    if start is None:
        return ""
    return content[start : start + max_chars]


def _locate_handler_start(content: str, handler: str) -> int | None:
    """Return start offset of a handler declaration, or None if not found."""
    patterns = [
        rf"\bfunction\s+{re.escape(handler)}\s*\([^)]*\)\s*\{{",
        rf"\b(?:const|let|var)\s+{re.escape(handler)}\s*=\s*(?:async\s*)?\([^)]*\)\s*=>\s*\{{?",
        rf"\b{re.escape(handler)}\s*\([^)]*\)\s*\{{",  # class method / object shorthand
    ]
    for pat in patterns:
        m = re.search(pat, content)
        if m:
            return m.start()
    return None


def _locate_enclosing_jsx(content: str, handler_start: int, max_chars: int = 2000) -> str:
    """Best-effort slice of the JSX that encloses / follows a handler.

    Heuristic: return up to ``max_chars`` characters **before** the handler
    declaration (field validation props usually live in the form JSX above
    the submit handler) plus up to ``max_chars`` chars **after** the
    handler (where the Button calling it lives). Not a real AST parse —
    the biz extractor only uses this as extra context for the LLM.
    """
    if handler_start <= 0:
        before = ""
    else:
        before = content[max(0, handler_start - max_chars): handler_start]
    after = content[handler_start: handler_start + max_chars + 1000]
    return before + "\n/* --- HANDLER BELOW --- */\n" + after


_VALIDATION_PROP_RE = re.compile(
    r"""(?P<prop>required|pattern|minLength|maxLength|min|max|type)
        \s*=\s*
        (?:\{(?P<curly>[^}]*)\}|"(?P<dqs>[^"]*)"|'(?P<sqs>[^']*)')
    """,
    re.VERBOSE,
)


def extract_validation_props(jsx_slice: str) -> list[dict]:
    """Extract JSX validation attributes from a text slice.

    Returns a list of ``{"prop": ..., "value": ...}`` (order preserved, no
    dedup — LLM gets raw context). Designed as a regex pre-annotator that
    rides alongside the handler body in the Phase B LLM prompt so the
    model doesn't have to re-discover common React prop patterns.
    """
    if not jsx_slice:
        return []
    out: list[dict] = []
    for m in _VALIDATION_PROP_RE.finditer(jsx_slice):
        prop = m.group("prop")
        val = m.group("curly") or m.group("dqs") or m.group("sqs") or ""
        out.append({"prop": prop, "value": val.strip()})
    return out
