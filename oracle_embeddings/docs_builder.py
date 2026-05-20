"""README.md (또는 임의 markdown) → 단일 HTML 설명서 빌더.

stdlib 만 사용 (폐쇄망 친화 — CDN/외부 라이브러리 의존 X). 출력 HTML 은
좌측 사이드바 (H2/H3 트리) + 우측 컨텐츠 + 검색바 + 다크/라이트 토글을
포함한 단일 파일. README 가 자주 갱신되니까 build 가 빠르고 결정론적
이어야 한다.

마크다운 지원 범위 (README 가 실제로 쓰는 것만):
- ``# H1`` ~ ``#### H4``
- 코드 펜스 ````lang ... ````` (lang 은 단순 라벨로만 사용, syntax
  highlight 는 안 함 — class 만 emit)
- 표 (헤더 + ``|---|`` 구분선 + 본문)
- 리스트 (``-`` / ``*`` / ``1.``) — nested OK
- blockquote (``>`` 시작)
- HR (``---``)
- 인라인 코드 ``backtick``
- 링크 ``[text](url)``
- ``**bold**`` / ``*italic*``
- 인라인 HTML 은 그대로 통과 (이미 본문이 HTML 섞어 쓰는 경우 대비)

GFM 의 모든 코너 케이스를 다 잡진 않지만, 이 프로젝트 README 1685줄을
무리 없이 변환한다. 다른 마크다운 입력에도 일반적으로 동작.
"""

from __future__ import annotations

import html as _html
import os
import re
from datetime import datetime
from typing import Iterable


# ─────────────────────────────────────────────────────────────────
# 인라인 변환
# ─────────────────────────────────────────────────────────────────

_INLINE_CODE_RE = re.compile(r"`([^`\n]+?)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_BOLD_RE = re.compile(r"\*\*([^\n*][^\n]*?)\*\*")
_ITALIC_RE = re.compile(r"(?<![*\w])\*([^\n*][^\n]*?)\*(?!\w)")
_AUTOLINK_RE = re.compile(r"(?<![\"'>=])(https?://[^\s)<>\"']+)")


def _inline(text: str) -> str:
    """인라인 마크다운 → HTML. HTML escape 는 코드 추출 후 적용."""
    placeholders: list[str] = []

    def _stash(html: str) -> str:
        token = f"\x00P{len(placeholders)}\x00"
        placeholders.append(html)
        return token

    # 1. 인라인 코드 먼저 추출 (안의 내용은 escape, 다른 마크다운 처리 회피)
    def _code_sub(m: re.Match) -> str:
        return _stash(f"<code>{_html.escape(m.group(1))}</code>")

    text = _INLINE_CODE_RE.sub(_code_sub, text)

    # 2. 링크 추출 (URL 안 * 같은 거 안 망가지게)
    def _link_sub(m: re.Match) -> str:
        label = _html.escape(m.group(1))
        url = _html.escape(m.group(2), quote=True)
        return _stash(f'<a href="{url}">{label}</a>')

    text = _LINK_RE.sub(_link_sub, text)

    # 3. 자동 링크 (raw URL)
    def _auto_sub(m: re.Match) -> str:
        url = m.group(1)
        safe = _html.escape(url, quote=True)
        return _stash(f'<a href="{safe}">{safe}</a>')

    text = _AUTOLINK_RE.sub(_auto_sub, text)

    # 4. 나머지 텍스트 HTML escape
    text = _html.escape(text, quote=False)

    # 5. bold / italic (escape 후 처리 — `*` 는 escape 영향 없음)
    text = _BOLD_RE.sub(lambda m: f"<strong>{m.group(1)}</strong>", text)
    text = _ITALIC_RE.sub(lambda m: f"<em>{m.group(1)}</em>", text)

    # 6. placeholder 복원
    for i, html_chunk in enumerate(placeholders):
        text = text.replace(f"\x00P{i}\x00", html_chunk)

    return text


# ─────────────────────────────────────────────────────────────────
# 블록 파서
# ─────────────────────────────────────────────────────────────────

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^```(\S*)\s*$")
_HR_RE = re.compile(r"^(?:-{3,}|\*{3,}|_{3,})\s*$")
_TABLE_SEP_RE = re.compile(r"^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$")
_ULIST_RE = re.compile(r"^(\s*)([-*])\s+(.*)$")
_OLIST_RE = re.compile(r"^(\s*)(\d+)\.\s+(.*)$")
_BLOCKQUOTE_RE = re.compile(r"^>\s?(.*)$")


def _slugify(text: str) -> str:
    """Heading anchor id. 한글 + 영문 + 숫자 + - 만 유지."""
    t = re.sub(r"[^\w가-힯\- ]+", "", text, flags=re.UNICODE)
    t = re.sub(r"\s+", "-", t.strip()).lower()
    return t or "section"


def _split_table_row(row: str) -> list[str]:
    s = row.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    # 셀 안 `\|` 은 escape 처리 — 단순 split 으로 무방한 README 패턴
    return [c.strip() for c in s.split("|")]


def _is_table_header(line: str, next_line: str | None) -> bool:
    if "|" not in line or next_line is None:
        return False
    return bool(_TABLE_SEP_RE.match(next_line))


def markdown_to_html(md: str) -> tuple[str, list[dict]]:
    """``md`` 본문 → (HTML body, TOC entries).

    TOC entry: ``{"level": 2|3, "text": str, "id": str}``.
    """
    lines = md.splitlines()
    out: list[str] = []
    toc: list[dict] = []
    i = 0

    while i < len(lines):
        line = lines[i]
        nxt = lines[i + 1] if i + 1 < len(lines) else None

        # 코드 펜스
        m = _FENCE_RE.match(line)
        if m:
            lang = m.group(1) or ""
            i += 1
            buf: list[str] = []
            while i < len(lines) and not _FENCE_RE.match(lines[i]):
                buf.append(lines[i])
                i += 1
            i += 1  # closing fence 통과
            cls = f' class="lang-{_html.escape(lang)}"' if lang else ""
            content = _html.escape("\n".join(buf))
            out.append(f'<pre><code{cls}>{content}</code></pre>')
            continue

        # 빈 줄
        if not line.strip():
            i += 1
            continue

        # Heading
        m = _HEADING_RE.match(line)
        if m:
            level = len(m.group(1))
            text = m.group(2).strip()
            slug = _slugify(text)
            # 중복 슬러그 회피
            base_slug = slug
            n = 2
            while any(t["id"] == slug for t in toc):
                slug = f"{base_slug}-{n}"
                n += 1
            if 2 <= level <= 3:
                toc.append({"level": level, "text": text, "id": slug})
            inline_text = _inline(text)
            out.append(
                f'<h{level} id="{slug}">'
                f'<a class="anchor" href="#{slug}" aria-label="anchor">#</a>'
                f'{inline_text}</h{level}>'
            )
            i += 1
            continue

        # HR
        if _HR_RE.match(line):
            out.append("<hr/>")
            i += 1
            continue

        # 표
        if _is_table_header(line, nxt):
            headers = _split_table_row(line)
            sep = _split_table_row(nxt or "")
            aligns: list[str] = []
            for s in sep:
                left = s.startswith(":")
                right = s.endswith(":")
                if left and right:
                    aligns.append("center")
                elif right:
                    aligns.append("right")
                else:
                    aligns.append("left")
            i += 2
            rows: list[list[str]] = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(_split_table_row(lines[i]))
                i += 1
            out.append('<div class="table-wrap"><table>')
            out.append("<thead><tr>")
            for j, h in enumerate(headers):
                a = aligns[j] if j < len(aligns) else "left"
                out.append(f'<th style="text-align:{a}">{_inline(h)}</th>')
            out.append("</tr></thead><tbody>")
            for r in rows:
                out.append("<tr>")
                for j, c in enumerate(r):
                    a = aligns[j] if j < len(aligns) else "left"
                    out.append(f'<td style="text-align:{a}">{_inline(c)}</td>')
                out.append("</tr>")
            out.append("</tbody></table></div>")
            continue

        # 리스트
        if _ULIST_RE.match(line) or _OLIST_RE.match(line):
            block_lines: list[str] = []
            while i < len(lines) and (
                _ULIST_RE.match(lines[i]) or _OLIST_RE.match(lines[i])
                or (lines[i].startswith("  ") and lines[i].strip())
                or not lines[i].strip()
            ):
                # 다음 비-리스트 블록 만나면 종료. 빈 줄은 1개까지 허용.
                if not lines[i].strip():
                    # 한 줄 빈 줄 → 리스트 안 단락 분리. 두 줄 연속이면 종료.
                    if (i + 1 < len(lines)
                            and not _ULIST_RE.match(lines[i + 1])
                            and not _OLIST_RE.match(lines[i + 1])
                            and not lines[i + 1].startswith("  ")):
                        break
                block_lines.append(lines[i])
                i += 1
            out.append(_render_list(block_lines))
            continue

        # blockquote
        if _BLOCKQUOTE_RE.match(line):
            bq: list[str] = []
            while i < len(lines) and _BLOCKQUOTE_RE.match(lines[i]):
                bq.append(_BLOCKQUOTE_RE.match(lines[i]).group(1))
                i += 1
            inner = " ".join(bq).strip()
            out.append(f"<blockquote><p>{_inline(inner)}</p></blockquote>")
            continue

        # 단락 — 연속 텍스트 라인 모음
        para: list[str] = [line]
        i += 1
        while i < len(lines):
            l = lines[i]
            if (not l.strip()
                    or _HEADING_RE.match(l)
                    or _FENCE_RE.match(l)
                    or _HR_RE.match(l)
                    or _ULIST_RE.match(l)
                    or _OLIST_RE.match(l)
                    or _BLOCKQUOTE_RE.match(l)
                    or _is_table_header(l, lines[i + 1] if i + 1 < len(lines) else None)):
                break
            para.append(l)
            i += 1
        text = " ".join(p.strip() for p in para)
        out.append(f"<p>{_inline(text)}</p>")

    return "\n".join(out), toc


def _render_list(block_lines: list[str]) -> str:
    """들여쓰기 기반 nested 리스트 → <ul>/<ol> HTML.

    각 라인의 leading whitespace 갯수를 보고 트리 구성. 단순 stack 알고.
    """
    items: list[tuple[int, str, str]] = []  # (indent, marker, text)
    for ln in block_lines:
        if not ln.strip():
            continue
        m = _OLIST_RE.match(ln)
        if m:
            items.append((len(m.group(1)), "ol", m.group(3)))
            continue
        m = _ULIST_RE.match(ln)
        if m:
            items.append((len(m.group(1)), "ul", m.group(3)))
            continue
        # 들여쓰기 연속 라인 — 이전 item 의 본문에 합치기
        if items:
            indent, marker, text = items[-1]
            items[-1] = (indent, marker, text + " " + ln.strip())

    if not items:
        return ""

    # 트리 빌드 (indent 기준)
    out: list[str] = []
    stack: list[tuple[int, str]] = []  # (indent, marker)
    for indent, marker, text in items:
        # 현 indent 보다 깊은 stack pop
        while stack and stack[-1][0] > indent:
            out.append(f"</li></{stack[-1][1]}>")
            stack.pop()
        # 같은 indent 인데 다른 marker → 닫고 다시 열기
        if stack and stack[-1][0] == indent and stack[-1][1] != marker:
            out.append(f"</li></{stack[-1][1]}>")
            stack.pop()
        # 새 단계
        if not stack or stack[-1][0] < indent:
            out.append(f"<{marker}>")
            stack.append((indent, marker))
        else:
            out.append("</li>")
        out.append(f"<li>{_inline(text)}")
    # 모두 닫기
    while stack:
        out.append(f"</li></{stack[-1][1]}>")
        stack.pop()
    return "\n".join(out)


# ─────────────────────────────────────────────────────────────────
# HTML 템플릿 + 사이드바
# ─────────────────────────────────────────────────────────────────


_HTML_TEMPLATE = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{title}</title>
<style>
:root {{
  --bg: #ffffff; --fg: #1f2328; --muted: #57606a;
  --border: #d0d7de; --hover: #f6f8fa; --code-bg: #f6f8fa;
  --link: #0969da; --accent: #0969da;
  --sidebar-bg: #f6f8fa; --sidebar-fg: #1f2328;
  --table-stripe: #fafbfc;
}}
[data-theme="dark"] {{
  --bg: #0d1117; --fg: #e6edf3; --muted: #8b949e;
  --border: #30363d; --hover: #161b22; --code-bg: #161b22;
  --link: #58a6ff; --accent: #58a6ff;
  --sidebar-bg: #010409; --sidebar-fg: #e6edf3;
  --table-stripe: #0b1118;
}}
* {{ box-sizing: border-box; }}
html, body {{ margin: 0; padding: 0; background: var(--bg); color: var(--fg); }}
body {{
  font-family: -apple-system, BlinkMacSystemFont, "Pretendard", "Noto Sans CJK KR",
               "맑은 고딕", "Malgun Gothic", "Segoe UI", Helvetica, Arial, sans-serif;
  font-size: 15px; line-height: 1.6;
}}
.layout {{ display: flex; min-height: 100vh; }}
aside.sidebar {{
  width: 320px; min-width: 320px; background: var(--sidebar-bg);
  color: var(--sidebar-fg); border-right: 1px solid var(--border);
  position: sticky; top: 0; height: 100vh; overflow-y: auto;
  padding: 16px 12px 24px;
}}
aside.sidebar .brand {{
  font-weight: 700; font-size: 16px; padding: 4px 8px 12px;
  border-bottom: 1px solid var(--border); margin-bottom: 12px;
}}
aside.sidebar .brand small {{
  display: block; font-weight: 400; font-size: 11px; color: var(--muted);
  margin-top: 4px;
}}
aside.sidebar input.search {{
  width: 100%; padding: 8px 10px; border: 1px solid var(--border);
  border-radius: 6px; background: var(--bg); color: var(--fg);
  font-size: 13px; margin-bottom: 12px;
}}
aside.sidebar input.search:focus {{ outline: 2px solid var(--accent); }}
aside.sidebar .toc {{ list-style: none; padding: 0; margin: 0; }}
aside.sidebar .toc li {{ margin: 0; }}
aside.sidebar .toc a {{
  display: block; padding: 5px 8px; text-decoration: none;
  color: var(--sidebar-fg); border-radius: 4px; font-size: 13px;
}}
aside.sidebar .toc a:hover {{ background: var(--hover); color: var(--link); }}
aside.sidebar .toc a.active {{
  background: var(--hover); color: var(--link); font-weight: 600;
  border-left: 3px solid var(--accent); padding-left: 5px;
}}
aside.sidebar .toc .h2 > a {{ font-weight: 600; }}
aside.sidebar .toc .h3 > a {{ padding-left: 22px; font-size: 12.5px; color: var(--muted); }}
aside.sidebar .toolbar {{
  display: flex; gap: 6px; margin-top: 12px; padding-top: 12px;
  border-top: 1px solid var(--border);
}}
aside.sidebar .toolbar button {{
  flex: 1; padding: 6px 8px; font-size: 12px; cursor: pointer;
  background: var(--bg); color: var(--fg); border: 1px solid var(--border);
  border-radius: 5px;
}}
aside.sidebar .toolbar button:hover {{ background: var(--hover); }}
main.content {{
  flex: 1; padding: 32px 48px 80px; max-width: 980px; margin: 0;
  min-width: 0; overflow-x: auto;
}}
main.content h1, main.content h2, main.content h3,
main.content h4, main.content h5, main.content h6 {{
  position: relative; scroll-margin-top: 8px;
}}
main.content h1 {{ font-size: 28px; border-bottom: 1px solid var(--border); padding-bottom: 10px; margin-top: 0; }}
main.content h2 {{ font-size: 22px; border-bottom: 1px solid var(--border); padding-bottom: 8px; margin-top: 32px; }}
main.content h3 {{ font-size: 18px; margin-top: 24px; }}
main.content h4 {{ font-size: 15px; margin-top: 18px; }}
main.content .anchor {{
  position: absolute; left: -22px; top: 50%; transform: translateY(-50%);
  color: var(--muted); text-decoration: none; opacity: 0;
  font-weight: 400; font-size: 0.8em;
}}
main.content h2:hover .anchor, main.content h3:hover .anchor,
main.content h4:hover .anchor {{ opacity: 1; }}
main.content p {{ margin: 10px 0; }}
main.content a {{ color: var(--link); text-decoration: none; }}
main.content a:hover {{ text-decoration: underline; }}
main.content code {{
  background: var(--code-bg); padding: 1px 5px; border-radius: 4px;
  font-family: "JetBrains Mono", "D2Coding", "Consolas", "Courier New", monospace;
  font-size: 13px;
}}
main.content pre {{
  background: var(--code-bg); border: 1px solid var(--border);
  border-radius: 6px; padding: 14px 16px; overflow-x: auto;
  margin: 12px 0;
}}
main.content pre code {{ background: transparent; padding: 0; font-size: 13px; }}
main.content .table-wrap {{ overflow-x: auto; margin: 12px 0; }}
main.content table {{
  border-collapse: collapse; min-width: 60%;
  font-size: 13px;
}}
main.content th, main.content td {{
  border: 1px solid var(--border); padding: 6px 10px;
  vertical-align: top;
}}
main.content th {{ background: var(--hover); font-weight: 600; }}
main.content tbody tr:nth-child(even) td {{ background: var(--table-stripe); }}
main.content ul, main.content ol {{ padding-left: 28px; margin: 8px 0; }}
main.content li {{ margin: 4px 0; }}
main.content blockquote {{
  border-left: 4px solid var(--border); margin: 12px 0;
  padding: 4px 14px; color: var(--muted); background: var(--hover);
}}
main.content hr {{ border: 0; border-top: 1px solid var(--border); margin: 24px 0; }}

.btn-menu {{ display: none; }}
@media (max-width: 900px) {{
  .layout {{ flex-direction: column; }}
  aside.sidebar {{
    position: fixed; top: 0; left: 0; height: 100vh; width: 280px;
    transform: translateX(-100%); transition: transform 0.2s ease-in-out;
    z-index: 50; box-shadow: 2px 0 8px rgba(0,0,0,0.1);
  }}
  aside.sidebar.open {{ transform: translateX(0); }}
  main.content {{ padding: 56px 20px 80px; max-width: 100%; }}
  .btn-menu {{
    display: block; position: fixed; top: 12px; left: 12px;
    z-index: 60; background: var(--bg); border: 1px solid var(--border);
    padding: 8px 12px; border-radius: 6px; cursor: pointer;
    color: var(--fg); font-size: 14px;
  }}
}}
</style>
</head>
<body>
<button class="btn-menu" id="btnMenu">☰ 목차</button>
<div class="layout">
<aside class="sidebar" id="sidebar">
  <div class="brand">{title}<small>마지막 빌드: {built_at}</small></div>
  <input class="search" type="text" id="search" placeholder="목차 검색…"/>
  <ul class="toc" id="toc">
    {toc_html}
  </ul>
  <div class="toolbar">
    <button id="btnTheme">🌓 테마</button>
    <button id="btnTop">↑ 상단</button>
  </div>
</aside>
<main class="content" id="content">
{body_html}
</main>
</div>
<script>
(function(){{
  // 테마 토글
  const root = document.documentElement;
  const saved = localStorage.getItem('docs-theme');
  if (saved) root.setAttribute('data-theme', saved);
  document.getElementById('btnTheme').addEventListener('click', function(){{
    const cur = root.getAttribute('data-theme') === 'dark' ? '' : 'dark';
    if (cur) root.setAttribute('data-theme', cur); else root.removeAttribute('data-theme');
    localStorage.setItem('docs-theme', cur);
  }});

  // 상단으로
  document.getElementById('btnTop').addEventListener('click', function(){{
    window.scrollTo({{top: 0, behavior: 'smooth'}});
  }});

  // 모바일 메뉴
  const sidebar = document.getElementById('sidebar');
  document.getElementById('btnMenu').addEventListener('click', function(){{
    sidebar.classList.toggle('open');
  }});

  // 사이드바 검색
  const search = document.getElementById('search');
  const tocItems = document.querySelectorAll('#toc li');
  search.addEventListener('input', function(){{
    const q = this.value.trim().toLowerCase();
    tocItems.forEach(function(li){{
      const text = (li.textContent || '').toLowerCase();
      li.style.display = (!q || text.indexOf(q) !== -1) ? '' : 'none';
    }});
  }});

  // 스크롤 위치 → active 표시
  const links = Array.from(document.querySelectorAll('#toc a'));
  const sections = links
    .map(function(a){{ const id = a.getAttribute('href').slice(1); return document.getElementById(id); }})
    .filter(Boolean);
  function highlight() {{
    let i = sections.length - 1;
    const y = window.scrollY + 100;
    for (; i >= 0; i--) {{
      if (sections[i] && sections[i].offsetTop <= y) break;
    }}
    links.forEach(function(a){{ a.classList.remove('active'); }});
    if (i >= 0) {{
      const target = sections[i].id;
      const link = links.find(function(a){{ return a.getAttribute('href') === '#' + target; }});
      if (link) {{
        link.classList.add('active');
        // sidebar 영역 안에서만 보이게 스크롤
        const r = link.getBoundingClientRect();
        const aside = document.getElementById('sidebar');
        const ar = aside.getBoundingClientRect();
        if (r.top < ar.top || r.bottom > ar.bottom) {{
          link.scrollIntoView({{block: 'nearest'}});
        }}
      }}
    }}
  }}
  window.addEventListener('scroll', highlight, {{passive: true}});
  highlight();

  // 모바일에서 TOC 클릭 시 닫기
  links.forEach(function(a){{
    a.addEventListener('click', function(){{
      if (window.innerWidth <= 900) sidebar.classList.remove('open');
    }});
  }});
}})();
</script>
</body>
</html>
"""


def _toc_html(toc: list[dict]) -> str:
    if not toc:
        return ""
    parts: list[str] = []
    for entry in toc:
        cls = f"h{entry['level']}"
        href = f"#{entry['id']}"
        label = _html.escape(entry["text"])
        parts.append(f'<li class="{cls}"><a href="{href}">{label}</a></li>')
    return "\n    ".join(parts)


# ─────────────────────────────────────────────────────────────────
# Public entry
# ─────────────────────────────────────────────────────────────────


def build_docs(md_path: str, out_path: str, title: str | None = None) -> str:
    """``md_path`` 의 markdown → 단일 HTML 파일 ``out_path`` 저장. 반환=절대경로."""
    if not os.path.isfile(md_path):
        raise FileNotFoundError(f"markdown not found: {md_path}")

    with open(md_path, "r", encoding="utf-8") as f:
        md = f.read()

    body_html, toc = markdown_to_html(md)

    # 제목 추출 — 첫 H1 또는 인자 / 파일명
    if title is None:
        m = re.search(r"^#\s+(.+)$", md, re.MULTILINE)
        title = m.group(1).strip() if m else os.path.basename(md_path)

    page = _HTML_TEMPLATE.format(
        title=_html.escape(title),
        built_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        toc_html=_toc_html(toc),
        body_html=body_html,
    )

    out_dir = os.path.dirname(os.path.abspath(out_path))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(page)
    return os.path.abspath(out_path)
