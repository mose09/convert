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
    # Match `(axios.get|fetch|httpClient.post) ( <string-literal> , ...)`
    # Non-URL first-arg (like a plain variable) is treated as a relay and
    # captured with group name 'var' so the 2-pass resolver can fill it in.
    return re.compile(
        rf"""\b(?P<method>{alt})\s*\(
             \s*(?:
                   (?P<quote>['"`])(?P<url>(?:https?:/)?/[^'"`\n]+?)(?P=quote)
                 | (?P<var>\w+)
             )
         """,
        re.VERBOSE,
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
    for fp in files:
        try:
            content = _read_file_safe(fp)
        except Exception:
            continue
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

    logger.info("API URL index for %s: %d urls from %d calls across %d files",
                frontend_dir, len(index), matches_count, len(files))
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


def _locate_handler_body(content: str, handler: str) -> str:
    """Find the declaration of ``handler`` and return a rough body slice.

    Matches ``function handler(...)`` and ``const handler = (...) => ...``
    style. Slice is capped at 4000 chars so a huge function doesn't
    dominate the scan.
    """
    patterns = [
        rf"\bfunction\s+{re.escape(handler)}\s*\([^)]*\)\s*\{{",
        rf"\b(?:const|let|var)\s+{re.escape(handler)}\s*=\s*(?:async\s*)?\([^)]*\)\s*=>\s*\{{?",
        rf"\b{re.escape(handler)}\s*\([^)]*\)\s*\{{",  # class method / object shorthand
    ]
    for pat in patterns:
        m = re.search(pat, content)
        if m:
            start = m.start()
            return content[start : start + 4000]
    return ""
