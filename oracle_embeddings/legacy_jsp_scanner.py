"""JSP 프론트엔드 스캐너 — 버튼별 백엔드 URL 추출 (React 스캐너 대응).

레거시 SI 화면이 JSP 로 만들어진 경우, ``legacy_react_api_scanner`` 와
**동일한 반환 계약**으로 backend 호출 URL 과 버튼 트리거를 뽑아 기존
analyze-legacy 체인(프론트 → 백엔드)에 그대로 붙는다.

두 공개 함수 (react 스캐너와 시그니처/반환형 동일):
  * :func:`build_api_url_index`  → ``{normalized_url: [source_file, ...]}``
  * :func:`extract_button_triggers` → ``{normalized_url: [button_label, ...]}``

인식하는 JSP → 백엔드 호출 패턴 (한국 SI 흔한 형태):
  - ``<form action="/x.do">`` / ``<form:form action="...">`` / ``<c:url value="...">``
  - jQuery: ``$.ajax({url:'...'})`` / ``$.post('...')`` / ``$.get`` / ``$.getJSON``
    / 임의 wrapper 의 ``url:'...'`` property
  - ``fetch('...')`` / ``axios...('...')`` / ``XMLHttpRequest.open('POST','...')``
  - ``location.href='...'`` / ``location.replace('...')`` / ``window.open('...')``
  - ``<a href="/x.do">`` (정적 리소스/앵커 제외)
  - patterns.yaml ``frontend.api_call_methods`` 의 공통 submit 함수
    (예: ``fnSubmit('/x.do')`` / ``goPage('...')``)

버튼 트리거: ``<button onclick=..>라벨</button>`` / ``<input type=button|submit
value="라벨" onclick=..>`` / ``<a onclick=..>라벨</a>`` 를 찾아, onclick 안의
inline URL 또는 호출 함수 body(같은 파일/형제 .js)의 URL 을 라벨과 연결.
form 안 submit 버튼은 그 form 의 action 과 연결.

heuristic 이라 cross-file 동적 바인딩 등은 놓칠 수 있음 — 첫 패스용.
문제 발견 시 이 파일만 수정하며 개선.
"""
from __future__ import annotations

import logging
import os
import re

from .legacy_util import normalize_url
from .mybatis_parser import _read_file_safe

logger = logging.getLogger(__name__)


# 스캔 대상 확장자. JSP 본문 + include 조각 + 태그파일 + 동봉 스크립트.
_JSP_EXTS = (".jsp", ".jspf", ".jspx", ".tag", ".tagf")
_SCRIPT_EXTS = (".js",)
_SKIP_DIRS = {".git", ".svn", ".hg", "node_modules", "target", "build",
              "dist", "bin", "out", "WEB-INF/lib"}

# 정적 리소스 — 백엔드 호출 아님.
_STATIC_EXT_RE = re.compile(
    r"\.(?:css|js|mjs|png|jpe?g|gif|ico|bmp|svg|woff2?|ttf|eot|otf|map|"
    r"less|scss|swf|pdf|zip|xls[xm]?|docx?|ppt)(?:[?#].*)?$",
    re.IGNORECASE,
)


# ── 코멘트 제거 ────────────────────────────────────────────────────
_JSP_COMMENT_RE = re.compile(r"<%--.*?--%>", re.DOTALL)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT_RE = re.compile(r"(?m)//[^\n]*$")


def _strip_comments(text: str) -> str:
    text = _JSP_COMMENT_RE.sub(" ", text)
    text = _HTML_COMMENT_RE.sub(" ", text)
    text = _BLOCK_COMMENT_RE.sub(" ", text)
    text = _LINE_COMMENT_RE.sub(" ", text)
    return text


# ── URL 추출 정규식 ───────────────────────────────────────────────
# 각 패턴은 group(1) 에 URL 문자열을 담는다.
_URL_PATTERNS = [
    # <form action="..."> / <form:form action="..."> (submit 대상)
    re.compile(r"<form[^>]*?\baction\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE),
    # <c:url value="..."> / <c:redirect url="...">
    re.compile(r"<c:(?:url|redirect)[^>]*?\b(?:value|url)\s*=\s*[\"']([^\"']+)[\"']",
               re.IGNORECASE),
    # jQuery 축약 호출의 첫 인자 URL: $.post('..'), $.get, $.getJSON, $.load
    re.compile(r"\$\.(?:post|get|getJSON|load)\s*\(\s*[\"']([^\"']+)[\"']",
               re.IGNORECASE),
    # ajax config / 임의 wrapper 의 url 프로퍼티: url : '...'
    re.compile(r"\burl\s*:\s*[\"']([^\"']+)[\"']", re.IGNORECASE),
    # fetch('...') / axios('...') / axios.get('...')
    re.compile(r"\b(?:fetch|axios(?:\.\w+)?)\s*\(\s*[\"']([^\"']+)[\"']",
               re.IGNORECASE),
    # location.href = '...' / location.replace('...') / location.assign('...')
    re.compile(r"\blocation\s*\.\s*(?:href|replace|assign)\s*(?:=|\()\s*[\"']([^\"']+)[\"']",
               re.IGNORECASE),
    # form.action = '...'
    re.compile(r"\.action\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE),
    # window.open('...')
    re.compile(r"\bwindow\.open\s*\(\s*[\"']([^\"']+)[\"']", re.IGNORECASE),
    # XMLHttpRequest.open('POST', '...')
    re.compile(r"\.open\s*\(\s*[\"'][A-Za-z]+[\"']\s*,\s*[\"']([^\"']+)[\"']",
               re.IGNORECASE),
]

# <a href="..."> 는 별도 (정적/앵커가 많아 엄격 필터 후 채택)
_HREF_RE = re.compile(r"<a\b[^>]*?\bhref\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)


def _looks_like_endpoint(url: str) -> bool:
    """백엔드 호출 URL 로 볼만한지. 정적/앵커/외부스킴 제외."""
    if not url:
        return False
    u = url.strip()
    low = u.lower()
    if not u or low.startswith(("#", "javascript:", "mailto:", "tel:",
                                "data:", "{{", "//")):
        return False
    if _STATIC_EXT_RE.search(low):
        return False
    # 순수 EL/JSP 표현식만으로 된 값은 정적 판별 불가 → 채택하되 normalize.
    if u.startswith("/"):
        return True
    if low.startswith(("http://", "https://")):
        return True  # normalize_url 이 host 제거
    # 액션 확장자 (.do/.act/.jsp/...) 는 상대경로여도 endpoint
    if re.search(r"\.(?:do|act|action|jsp|jspx|json|ajax|nex|sc|svc|cmd|"
                 r"exec|proc)\b", low):
        return True
    # 슬래시 포함 상대경로도 대체로 endpoint
    if "/" in u:
        return True
    return False


def _custom_method_res(patterns: dict | None):
    """patterns.yaml frontend.api_call_methods → ``NAME('URL'...)`` 정규식들."""
    fe = (patterns or {}).get("frontend") or {}
    out = []
    for name in (fe.get("api_call_methods") or []):
        n = str(name).strip()
        if not n or not re.match(r"^[A-Za-z_$][\w$.]*$", n):
            continue
        # fnSubmit('/x.do', ...) 형태의 첫 문자열 인자
        out.append(re.compile(
            r"\b" + re.escape(n) + r"\s*\(\s*[\"']([^\"']+)[\"']"))
    return out


# 서비스 ID 기반 호출 (URL 이 아니라 ID 문자열이 첫 인자):
#   httpSend("fabCBMDataList", paramJson, onSuccess, onFail, opt)
# ID 는 service 정의 XML (<service id=.. serviceClass=..>) 로 컨트롤러와
# 매핑된다 (legacy_service_registry). 여기서는 "/<id>" pseudo-URL 로 emit
# 해 analyze-legacy 가 붙인 합성 엔드포인트(/<id>)와 정규화 키가 일치.
_SERVICE_ID_METHODS = ("httpSend",)


def _service_method_res(patterns: dict | None):
    """서비스ID 호출 함수 정규식. 기본 httpSend + patterns
    ``frontend.service_call_methods`` 로 확장 (합집합, 기본값 유지)."""
    fe = (patterns or {}).get("frontend") or {}
    names = list(_SERVICE_ID_METHODS)
    for n in (fe.get("service_call_methods") or []):
        n = str(n).strip()
        if n and n not in names:
            names.append(n)
    out = []
    for n in names:
        if not re.match(r"^[A-Za-z_$][\w$.]*$", n):
            continue
        # 첫 문자열 인자가 bare 식별자(슬래시 없는 서비스 ID)여도 OK
        out.append(re.compile(
            r"\b" + re.escape(n) + r"\s*\(\s*[\"']([A-Za-z_][\w.$-]*)[\"']"))
    return out


def _urls_in_text(text: str, custom_res, service_res=()) -> list[str]:
    """text 에서 URL 후보(정규화 전 원문) 리스트를 순서 보존/중복 제거로.

    ``service_res`` 매칭(서비스 ID 호출)은 URL 형태 검사 없이 ``/<id>``
    pseudo-URL 로 채택 — service registry 합성 엔드포인트와 키가 맞는다."""
    seen: dict[str, None] = {}
    for pat in _URL_PATTERNS:
        for m in pat.finditer(text):
            raw = m.group(1)
            if _looks_like_endpoint(raw):
                seen.setdefault(raw, None)
    for m in _HREF_RE.finditer(text):
        raw = m.group(1)
        if _looks_like_endpoint(raw):
            seen.setdefault(raw, None)
    for pat in custom_res:
        for m in pat.finditer(text):
            raw = m.group(1)
            if _looks_like_endpoint(raw):
                seen.setdefault(raw, None)
    for pat in service_res:
        for m in pat.finditer(text):
            sid = m.group(1)
            if sid:
                seen.setdefault("/" + sid.lstrip("/"), None)
    return list(seen.keys())


def _clean_el(url: str) -> str:
    """``${ctx}/x.do`` / ``<%=path%>/x.do`` 의 동적 prefix 를 제거해
    normalize 가 경로 부분만 남기게 한다."""
    u = url.strip()
    # 선행 EL/스크립틀릿 표현식 (contextPath 등) 제거
    u = re.sub(r"^\s*(?:\$\{[^}]*\}|<%=.*?%>)", "", u)
    # 중간 EL 은 {p} 로 (동적 세그먼트)
    u = re.sub(r"\$\{[^}]*\}", "{p}", u)
    u = re.sub(r"<%=.*?%>", "{p}", u)
    if not u.startswith("/") and "/" in u and not u.lower().startswith("http"):
        u = "/" + u.lstrip("/")
    return u


def _scan_files(frontend_dir: str, exts: tuple) -> list[str]:
    """frontend_dir 하위에서 exts 확장자 파일의 절대경로 리스트."""
    out: list[str] = []
    for root, dirs, names in os.walk(frontend_dir):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS
                   and not d.startswith(".")]
        for n in names:
            if n.lower().endswith(exts):
                out.append(os.path.join(root, n))
    return out


def count_jsp_files(frontend_dir: str, cap: int = 200) -> int:
    """frontend_dir 하위 JSP 파일 개수 (cap 에서 조기 종료)."""
    if not frontend_dir or not os.path.isdir(frontend_dir):
        return 0
    cnt = 0
    for root, dirs, names in os.walk(frontend_dir):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS
                   and not d.startswith(".")]
        for n in names:
            if n.lower().endswith(_JSP_EXTS):
                cnt += 1
                if cnt >= cap:
                    return cnt
    return cnt


def build_api_url_index(frontend_dir: str, patterns: dict | None = None,
                        strip_patterns=None,
                        backend_name_map: dict[str, str] | None = None,
                        repo_index_out: dict[str, set[str]] | None = None,
                        ) -> dict[str, list[str]]:
    """``{normalized_api_url: [source_file, ...]}`` 반환 (react 스캐너 동일 계약).

    JSP + 조각 + .js 파일을 스캔해 백엔드 호출 URL 을 뽑고 normalize_url 로
    정규화한다. ``backend_name_map`` / ``repo_index_out`` 은 계약 유지용
    (JSP 는 getBackendUrl(KEY,..) 관례가 없어 현재 미사용)."""
    if not frontend_dir or not os.path.isdir(frontend_dir):
        return {}
    custom_res = _custom_method_res(patterns)
    service_res = _service_method_res(patterns)
    files = _scan_files(frontend_dir, _JSP_EXTS + _SCRIPT_EXTS)
    index: dict[str, set[str]] = {}
    for path in files:
        try:
            text = _strip_comments(_read_file_safe(path))
        except Exception:
            continue
        rel = os.path.relpath(path, frontend_dir).replace(os.sep, "/")
        for raw in _urls_in_text(text, custom_res, service_res):
            canonical = normalize_url(_clean_el(raw), strip_patterns)
            if not canonical or canonical == "/":
                continue
            index.setdefault(canonical, set()).add(rel)
    return {u: sorted(files) for u, files in index.items()}


# ── 버튼 트리거 ───────────────────────────────────────────────────
# 함수 정의: ``function NAME( ... ) {`` — body 는 brace 매칭으로 슬라이스.
_FUNC_DEF_RE = re.compile(r"function\s+([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{")
# onclick 안에서 호출되는 함수 이름들
_CALL_NAME_RE = re.compile(r"\b([A-Za-z_$][\w$]*)\s*\(")

# 버튼류 요소
_BUTTON_RE = re.compile(
    r"<button\b([^>]*)>(.*?)</button>", re.IGNORECASE | re.DOTALL)
_INPUT_BTN_RE = re.compile(
    r"<input\b([^>]*\btype\s*=\s*[\"'](?:button|submit|image)[\"'][^>]*)/?>",
    re.IGNORECASE)
_ANCHOR_RE = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.IGNORECASE | re.DOTALL)
_FORM_RE = re.compile(r"<form\b([^>]*)>(.*?)</form>", re.IGNORECASE | re.DOTALL)

# onclick 은 바깥 따옴표 안에 반대 따옴표가 흔히 중첩된다
# (onclick="location.href='/x.do'"). 바깥 따옴표를 backref 로 매칭해
# 내부 반대 따옴표를 그대로 포함. group(2) 가 핸들러 본문.
_ONCLICK_RE = re.compile(r"\bonclick\s*=\s*([\"'])(.*?)\1",
                         re.IGNORECASE | re.DOTALL)
_VALUE_RE = re.compile(r"\bvalue\s*=\s*[\"']([^\"']*)[\"']", re.IGNORECASE)
_ALT_RE = re.compile(r"\balt\s*=\s*[\"']([^\"']*)[\"']", re.IGNORECASE)
_ACTION_ATTR_RE = re.compile(r"\baction\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)
_TAG_STRIP_RE = re.compile(r"<[^>]+>")


def _slice_body(text: str, brace_open: int, max_len: int = 4000) -> str:
    """brace_open(‘{’ 위치)부터 매칭되는 ‘}’ 까지 body 문자열."""
    depth = 0
    end = min(len(text), brace_open + max_len)
    for i in range(brace_open, end):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[brace_open:i + 1]
    return text[brace_open:end]


def _func_bodies(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in _FUNC_DEF_RE.finditer(text):
        name = m.group(1)
        body = _slice_body(text, m.end() - 1)
        # 같은 이름 중복 시 첫 정의 유지
        out.setdefault(name, body)
    return out


def _clean_label(raw: str) -> str:
    """버튼 내부 HTML/EL 을 벗겨 사람이 읽는 라벨로."""
    s = _TAG_STRIP_RE.sub("", raw or "")
    s = re.sub(r"\$\{[^}]*\}", "", s)
    s = re.sub(r"<%=.*?%>", "", s)
    s = re.sub(r"&[a-zA-Z#0-9]+;", " ", s)   # HTML 엔티티
    s = re.sub(r"\s+", " ", s).strip()
    return s[:40]


def _urls_from_onclick(handler: str, func_bodies: dict[str, str],
                       custom_res, strip_patterns, service_res=()) -> list[str]:
    """onclick 문자열에서 URL 들. inline URL + 호출 함수 body 안 URL."""
    canon: list[str] = []

    def _add_from(text: str):
        for raw in _urls_in_text(text, custom_res, service_res):
            c = normalize_url(_clean_el(raw), strip_patterns)
            if c and c != "/" and c not in canon:
                canon.append(c)

    _add_from(handler)
    # onclick 안에서 호출한 함수 body 도 1-hop 따라감
    for m in _CALL_NAME_RE.finditer(handler):
        body = func_bodies.get(m.group(1))
        if body:
            _add_from(body)
    return canon


def _extract_triggers_detailed(frontend_dir: str,
                               patterns: dict | None = None,
                               strip_patterns=None
                               ) -> dict[str, list[tuple[str, str]]]:
    """``{normalized_url: [(jsp_rel_path, button_label), ...]}`` 수집.

    버튼이 **어느 화면(jsp)에 있는지**까지 보존한다 — 프론트 기준 행 분리
    (화면·버튼당 1행)의 데이터 소스. 순서 보존 + (file,label) 중복 제거.
    """
    if not frontend_dir or not os.path.isdir(frontend_dir):
        return {}
    custom_res = _custom_method_res(patterns)
    service_res = _service_method_res(patterns)
    jsp_files = _scan_files(frontend_dir, _JSP_EXTS)
    script_files = _scan_files(frontend_dir, _SCRIPT_EXTS)

    # 전역 함수 body 인덱스 (JSP 인라인 <script> + 형제 .js). cross-file
    # 핸들러 (공통 .js) 도 이름으로 lookup 가능하게.
    global_bodies: dict[str, str] = {}
    for path in jsp_files + script_files:
        try:
            txt = _strip_comments(_read_file_safe(path))
        except Exception:
            continue
        for name, body in _func_bodies(txt).items():
            global_bodies.setdefault(name, body)

    detailed: dict[str, list[tuple[str, str]]] = {}
    rel = ""

    def _assoc(label: str, urls) -> None:
        label = _clean_label(label)
        if not label:
            return
        for u in urls:
            pairs = detailed.setdefault(u, [])
            if (rel, label) not in pairs:
                pairs.append((rel, label))

    for path in jsp_files:
        try:
            text = _strip_comments(_read_file_safe(path))
        except Exception:
            continue
        rel = os.path.relpath(path, frontend_dir).replace(os.sep, "/")
        # 파일 로컬 함수 우선 + 전역 보강
        local_bodies = dict(global_bodies)
        local_bodies.update(_func_bodies(text))

        # 1) <form action="URL"> 안 submit 버튼 → action
        for fm in _FORM_RE.finditer(text):
            attrs, body = fm.group(1), fm.group(2)
            am = _ACTION_ATTR_RE.search(attrs)
            if not am or not _looks_like_endpoint(am.group(1)):
                continue
            action_url = normalize_url(_clean_el(am.group(1)), strip_patterns)
            if not action_url or action_url == "/":
                continue
            # form 내부 submit 성 버튼들
            for bm in _BUTTON_RE.finditer(body):
                b_attrs, b_text = bm.group(1), bm.group(2)
                b_type = re.search(r"\btype\s*=\s*[\"'](\w+)[\"']", b_attrs, re.I)
                if (b_type is None) or (b_type.group(1).lower() == "submit"):
                    _assoc(b_text, [action_url])
            for im in _INPUT_BTN_RE.finditer(body):
                i_attrs = im.group(1)
                if re.search(r"type\s*=\s*[\"']submit[\"']", i_attrs, re.I):
                    v = _VALUE_RE.search(i_attrs)
                    _assoc(v.group(1) if v else "submit", [action_url])

        # 2) onclick 핸들러가 URL 을 직접/함수경유로 참조하는 버튼들
        # <button ...>label</button>
        for bm in _BUTTON_RE.finditer(text):
            b_attrs, b_text = bm.group(1), bm.group(2)
            oc = _ONCLICK_RE.search(b_attrs)
            if not oc:
                continue
            urls = _urls_from_onclick(oc.group(2), local_bodies,
                                      custom_res, strip_patterns,
                                      service_res)
            if urls:
                _assoc(b_text, urls)
        # <input type=button|submit|image value="label" onclick=..>
        for im in _INPUT_BTN_RE.finditer(text):
            i_attrs = im.group(1)
            oc = _ONCLICK_RE.search(i_attrs)
            if not oc:
                continue
            urls = _urls_from_onclick(oc.group(2), local_bodies,
                                      custom_res, strip_patterns,
                                      service_res)
            if not urls:
                continue
            v = _VALUE_RE.search(i_attrs) or _ALT_RE.search(i_attrs)
            _assoc(v.group(1) if v else "button", urls)
        # <a onclick=..>label</a>
        for am in _ANCHOR_RE.finditer(text):
            a_attrs, a_text = am.group(1), am.group(2)
            oc = _ONCLICK_RE.search(a_attrs)
            if not oc:
                continue
            urls = _urls_from_onclick(oc.group(2), local_bodies,
                                      custom_res, strip_patterns,
                                      service_res)
            if urls:
                _assoc(a_text, urls)

    return detailed


def extract_button_triggers(frontend_dir: str, api_index: dict[str, list[str]],
                            patterns: dict | None = None,
                            strip_patterns=None) -> dict[str, list[str]]:
    """``{normalized_api_url: [button_label, ...]}`` 반환 (react 스캐너 동일 계약)."""
    detailed = _extract_triggers_detailed(frontend_dir, patterns, strip_patterns)
    out: dict[str, list[str]] = {}
    for u, pairs in detailed.items():
        labels: list[str] = []
        for _f, lbl in pairs:
            if lbl not in labels:
                labels.append(lbl)
        out[u] = sorted(labels)
    return out


def extract_button_triggers_detailed(frontend_dir: str,
                                     api_index: dict[str, list[str]],
                                     patterns: dict | None = None,
                                     strip_patterns=None
                                     ) -> dict[str, list[tuple[str, str]]]:
    """``{normalized_url: [(jsp_rel_path, button_label), ...]}`` 반환.

    프론트 기준 행 분리용 — 같은 서비스ID/URL 을 여러 화면이 호출해도
    (화면, 버튼) 쌍이 보존돼 화면당 1행으로 정확히 나눌 수 있다."""
    return _extract_triggers_detailed(frontend_dir, patterns, strip_patterns)
