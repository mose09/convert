"""README.md → 기능 카탈로그 형태의 단일 HTML 설명서.

구조:
- **좌측 사이드바**: 카테고리별 커맨드 목록 + 시작하기 / 산출물 경로 토픽
- **우측 메인**: 선택한 항목의 상세 (한 화면에 1개씩만 표시)
- **JS 라우팅**: `#cmd/<name>` / `#topic/<id>` / `#` (home). hashchange 로 본문 교체.

폐쇄망 친화 — stdlib 만 사용, CDN/외부 라이브러리 의존 0.

README 파싱:
1. "## 기능 요약" 표 → 커맨드 카탈로그 (name / desc / oracle / llm)
2. "## 산출물 경로 규약" 표 → 영역별 경로
3. "### N. ..." H3 섹션 → 번호 키 dict
4. SECTION_MAP 으로 command → H3 번호 매핑, CATEGORY_MAP 으로 그룹화
"""

from __future__ import annotations

import html as _html
import json
import os
import re
from datetime import datetime


# ─────────────────────────────────────────────────────────────────
# 인라인 마크다운 변환 (이전 버전 그대로)
# ─────────────────────────────────────────────────────────────────

_INLINE_CODE_RE = re.compile(r"`([^`\n]+?)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_BOLD_RE = re.compile(r"\*\*([^\n*][^\n]*?)\*\*")
_ITALIC_RE = re.compile(r"(?<![*\w])\*([^\n*][^\n]*?)\*(?!\w)")
_AUTOLINK_RE = re.compile(r"(?<![\"'>=])(https?://[^\s)<>\"']+)")


def _inline(text: str) -> str:
    placeholders: list[str] = []

    def _stash(html: str) -> str:
        token = f"\x00P{len(placeholders)}\x00"
        placeholders.append(html)
        return token

    text = _INLINE_CODE_RE.sub(
        lambda m: _stash(f"<code>{_html.escape(m.group(1))}</code>"), text)
    text = _LINK_RE.sub(
        lambda m: _stash(
            f'<a href="{_html.escape(m.group(2), quote=True)}">'
            f'{_html.escape(m.group(1))}</a>'), text)
    text = _AUTOLINK_RE.sub(
        lambda m: _stash(
            f'<a href="{_html.escape(m.group(1), quote=True)}">'
            f'{_html.escape(m.group(1), quote=True)}</a>'), text)
    text = _html.escape(text, quote=False)
    text = _BOLD_RE.sub(lambda m: f"<strong>{m.group(1)}</strong>", text)
    text = _ITALIC_RE.sub(lambda m: f"<em>{m.group(1)}</em>", text)
    for i, html_chunk in enumerate(placeholders):
        text = text.replace(f"\x00P{i}\x00", html_chunk)
    return text


# ─────────────────────────────────────────────────────────────────
# 블록 마크다운 변환 (이전 버전 그대로)
# ─────────────────────────────────────────────────────────────────

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^```(\S*)\s*$")
_HR_RE = re.compile(r"^(?:-{3,}|\*{3,}|_{3,})\s*$")
_TABLE_SEP_RE = re.compile(r"^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$")
_ULIST_RE = re.compile(r"^(\s*)([-*])\s+(.*)$")
_OLIST_RE = re.compile(r"^(\s*)(\d+)\.\s+(.*)$")
_BLOCKQUOTE_RE = re.compile(r"^>\s?(.*)$")


def _slugify(text: str) -> str:
    t = re.sub(r"[^\w가-힯\- ]+", "", text, flags=re.UNICODE)
    t = re.sub(r"\s+", "-", t.strip()).lower()
    return t or "section"


def _split_table_row(row: str) -> list[str]:
    s = row.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _is_table_header(line: str, next_line: str | None) -> bool:
    if "|" not in line or next_line is None:
        return False
    return bool(_TABLE_SEP_RE.match(next_line))


def markdown_to_html(md: str, *, demote: int = 0) -> str:
    """``md`` → HTML. ``demote`` 만큼 heading level 강등 (commands detail
    안에서 README 의 H3 를 H4 로 내리고 싶을 때 사용)."""
    lines = md.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        nxt = lines[i + 1] if i + 1 < len(lines) else None

        m = _FENCE_RE.match(line)
        if m:
            lang = m.group(1) or ""
            i += 1
            buf: list[str] = []
            while i < len(lines) and not _FENCE_RE.match(lines[i]):
                buf.append(lines[i])
                i += 1
            i += 1
            cls = f' class="lang-{_html.escape(lang)}"' if lang else ""
            content = _html.escape("\n".join(buf))
            out.append(f"<pre><code{cls}>{content}</code></pre>")
            continue

        if not line.strip():
            i += 1
            continue

        m = _HEADING_RE.match(line)
        if m:
            level = min(6, len(m.group(1)) + demote)
            text = m.group(2).strip()
            out.append(f"<h{level}>{_inline(text)}</h{level}>")
            i += 1
            continue

        if _HR_RE.match(line):
            out.append("<hr/>")
            i += 1
            continue

        if _is_table_header(line, nxt):
            headers = _split_table_row(line)
            sep = _split_table_row(nxt or "")
            aligns: list[str] = []
            for s in sep:
                left = s.startswith(":")
                right = s.endswith(":")
                aligns.append("center" if (left and right) else "right" if right else "left")
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

        if _ULIST_RE.match(line) or _OLIST_RE.match(line):
            block_lines: list[str] = []
            while i < len(lines) and (
                _ULIST_RE.match(lines[i]) or _OLIST_RE.match(lines[i])
                or (lines[i].startswith("  ") and lines[i].strip())
                or not lines[i].strip()
            ):
                if not lines[i].strip():
                    if (i + 1 < len(lines)
                            and not _ULIST_RE.match(lines[i + 1])
                            and not _OLIST_RE.match(lines[i + 1])
                            and not lines[i + 1].startswith("  ")):
                        break
                block_lines.append(lines[i])
                i += 1
            out.append(_render_list(block_lines))
            continue

        if _BLOCKQUOTE_RE.match(line):
            bq: list[str] = []
            while i < len(lines) and _BLOCKQUOTE_RE.match(lines[i]):
                bq.append(_BLOCKQUOTE_RE.match(lines[i]).group(1))
                i += 1
            inner = " ".join(bq).strip()
            out.append(f"<blockquote><p>{_inline(inner)}</p></blockquote>")
            continue

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

    return "\n".join(out)


def _render_list(block_lines: list[str]) -> str:
    items: list[tuple[int, str, str]] = []
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
        if items:
            indent, marker, text = items[-1]
            items[-1] = (indent, marker, text + " " + ln.strip())

    if not items:
        return ""

    out: list[str] = []
    stack: list[tuple[int, str]] = []
    for indent, marker, text in items:
        while stack and stack[-1][0] > indent:
            out.append(f"</li></{stack[-1][1]}>")
            stack.pop()
        if stack and stack[-1][0] == indent and stack[-1][1] != marker:
            out.append(f"</li></{stack[-1][1]}>")
            stack.pop()
        if not stack or stack[-1][0] < indent:
            out.append(f"<{marker}>")
            stack.append((indent, marker))
        else:
            out.append("</li>")
        out.append(f"<li>{_inline(text)}")
    while stack:
        out.append(f"</li></{stack[-1][1]}>")
        stack.pop()
    return "\n".join(out)


# ─────────────────────────────────────────────────────────────────
# README 파싱 (구조화 데이터 추출)
# ─────────────────────────────────────────────────────────────────

# 카테고리 (사이드바 그룹 순서). 값=커맨드 이름 리스트.
CATEGORY_MAP: dict[str, list[str]] = {
    "스키마 / ERD": [
        "schema", "query", "enrich-schema",
        "erd-md", "erd-group", "erd-rag", "erd", "embed",
    ],
    "용어 / 표준": [
        "terms", "grid-labels", "morpheme", "recommend-names",
        "gen-ddl", "validate-naming", "audit-standards",
        "review-sql", "standardize",
    ],
    "AS-IS 분석": [
        "analyze-legacy", "discover-patterns", "convert-menu",
        "screen-spec", "screen-converter",
    ],
    "마이그레이션": [
        "convert-mapping", "migration-impact",
        "migrate-sql", "validate-migration",
    ],
}

# 커맨드 → 사용법 H3 섹션 번호.
SECTION_MAP: dict[str, int] = {
    "schema": 1, "query": 2, "enrich-schema": 3,
    "erd-md": 4, "erd-group": 4, "erd-rag": 4, "erd": 4,
    "terms": 5, "gen-ddl": 6, "audit-standards": 7,
    "validate-naming": 8, "review-sql": 9, "standardize": 10,
    "analyze-legacy": 11, "discover-patterns": 11, "convert-menu": 11,
    "convert-mapping": 12, "migration-impact": 12,
    "migrate-sql": 12, "validate-migration": 12,
    "morpheme": 13, "screen-converter": 14, "screen-spec": 15,
    "recommend-names": 16,
}


def _find_section(md: str, h2_text: str) -> str:
    """``## <h2_text>`` 부터 다음 ``## `` 직전까지 본문 반환 (헤더 포함 X)."""
    lines = md.splitlines()
    start = -1
    pat = re.compile(rf"^##\s+{re.escape(h2_text)}\s*$")
    for i, ln in enumerate(lines):
        if pat.match(ln):
            start = i + 1
            break
    if start < 0:
        return ""
    end = len(lines)
    for i in range(start, len(lines)):
        if re.match(r"^##\s", lines[i]):
            end = i
            break
    return "\n".join(lines[start:end]).strip("\n")


def _find_numbered_h3(md: str, num: int) -> str:
    """``### <num>. ...`` 부터 다음 ``### `` 또는 ``## `` 직전까지 본문 반환
    (헤더 자체는 포함, 첫 줄로). 같은 num 이 여러 개면 첫 번째."""
    lines = md.splitlines()
    pat = re.compile(rf"^###\s+{num}\.\s")
    start = -1
    for i, ln in enumerate(lines):
        if pat.match(ln):
            start = i
            break
    if start < 0:
        return ""
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if re.match(r"^###?\s", lines[i]):
            end = i
            break
    return "\n".join(lines[start:end]).strip("\n")


def _parse_feature_table(md: str) -> list[dict]:
    """``## 기능 요약`` 표 → [{name, desc_md, oracle, llm}, ...]."""
    section = _find_section(md, "기능 요약")
    rows: list[dict] = []
    in_table = False
    for ln in section.splitlines():
        if ln.startswith("| Command |") or ln.startswith("|Command|"):
            in_table = True
            continue
        if in_table:
            if ln.strip().startswith("|---") or ln.strip().startswith("|:--"):
                continue
            if ln.strip().startswith("|"):
                cells = _split_table_row(ln)
                if len(cells) >= 4:
                    raw = cells[0].strip()
                    m = re.match(r"^`([^`]+)`\s*$", raw)
                    name = m.group(1) if m else raw
                    rows.append({
                        "name": name,
                        "desc_md": cells[1],
                        "oracle": cells[2],
                        "llm": cells[3],
                    })
            else:
                break
    return rows


def _parse_output_paths(md: str) -> dict[str, str]:
    """``## 산출물 경로 규약`` 표 → {커맨드_라벨: 경로}.

    원본 표의 '사용 커맨드' 셀에 여러 커맨드가 콤마/슬래시/플러스 등으로
    묶여 있어 1:N 매핑이 흔함. 각 커맨드 이름 키로 split 해서 풀어둠.
    """
    section = _find_section(md, "산출물 경로 규약")
    paths: dict[str, str] = {}
    in_table = False
    for ln in section.splitlines():
        if ln.strip().startswith("| 영역 폴더"):
            in_table = True
            continue
        if in_table:
            if ln.strip().startswith("|---") or ln.strip().startswith("|:--"):
                continue
            if ln.strip().startswith("|"):
                cells = _split_table_row(ln)
                if len(cells) >= 2:
                    folder = cells[0].strip()
                    cmds_raw = cells[1].strip()
                    # `cmd1` / cmd2 / cmd3+cmd4 같은 표기에서 영문/하이픈 키워드 추출
                    for token in re.findall(r"[A-Za-z][A-Za-z0-9\-]+", cmds_raw):
                        paths.setdefault(token, folder)
            else:
                break
    return paths


# ─────────────────────────────────────────────────────────────────
# 페이지 빌더
# ─────────────────────────────────────────────────────────────────

TOPICS: list[tuple[str, str, str]] = [
    # (id, sidebar label, README 의 H2 텍스트)
    ("install", "📥 설치", "설치"),
    ("config", "⚙ 설정", "설정"),
    ("workflow", "🚀 추천 워크플로우", "추천 워크플로우"),
    ("output-paths", "📁 산출물 경로 규약", "산출물 경로 규약"),
    ("project-structure", "🗂 프로젝트 구조", "프로젝트 구조"),
    ("erd-rendering", "🎨 ERD 렌더링", "ERD 렌더링"),
]


def _build_payload(md: str) -> dict:
    """README 한 번 파싱해서 JS 가 쓸 payload 생성."""
    features = _parse_feature_table(md)
    feat_by_name = {f["name"]: f for f in features}

    # 카테고리에 들어간 커맨드만 표시. 누락된 것은 별도 표시 X (필요 시 추가).
    commands_payload: dict[str, dict] = {}
    for cat, names in CATEGORY_MAP.items():
        for name in names:
            f = feat_by_name.get(name)
            section_num = SECTION_MAP.get(name)
            section_md = _find_numbered_h3(md, section_num) if section_num else ""
            detail_html = markdown_to_html(section_md, demote=2) if section_md else ""
            commands_payload[name] = {
                "name": name,
                "category": cat,
                "desc_html": _inline(f["desc_md"]) if f else "",
                "oracle": f["oracle"] if f else "",
                "llm": f["llm"] if f else "",
                "detail_html": detail_html,
            }

    # 토픽 (## 헤더 들어간 일반 H2 섹션)
    topics_payload: dict[str, dict] = {}
    for tid, label, h2 in TOPICS:
        section = _find_section(md, h2)
        topics_payload[tid] = {
            "id": tid,
            "label": label,
            "title": h2,
            "html": markdown_to_html(section, demote=1) if section else "",
        }

    # 산출물 경로 표는 토픽 안에 이미 들어가 있지만 lookup 용으로 분리.
    paths = _parse_output_paths(md)

    return {
        "commands": commands_payload,
        "topics": topics_payload,
        "categories": [
            {"name": cat, "commands": [n for n in names if n in feat_by_name]}
            for cat, names in CATEGORY_MAP.items()
        ],
        "paths": paths,
        "topic_order": [tid for tid, _, _ in TOPICS],
    }


# ─────────────────────────────────────────────────────────────────
# HTML 템플릿
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
  --border: #d0d7de; --hover: #f0f3f6; --code-bg: #f6f8fa;
  --link: #0969da; --accent: #0969da;
  --sidebar-bg: #f6f8fa; --sidebar-fg: #1f2328;
  --table-stripe: #fafbfc;
  --badge-oracle: #1f883d; --badge-llm: #bf8700; --badge-none: #6e7781;
}}
[data-theme="dark"] {{
  --bg: #0d1117; --fg: #e6edf3; --muted: #8b949e;
  --border: #30363d; --hover: #161b22; --code-bg: #161b22;
  --link: #58a6ff; --accent: #58a6ff;
  --sidebar-bg: #010409; --sidebar-fg: #e6edf3;
  --table-stripe: #0b1118;
  --badge-oracle: #3fb950; --badge-llm: #d29922; --badge-none: #8b949e;
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
  padding: 16px 0 24px;
}}
.brand {{
  font-weight: 700; font-size: 16px; padding: 0 16px 14px;
  border-bottom: 1px solid var(--border); margin: 0 0 12px;
}}
.brand small {{
  display: block; font-weight: 400; font-size: 11px; color: var(--muted);
  margin-top: 4px;
}}
.search-wrap {{ padding: 0 12px 10px; }}
input.search {{
  width: 100%; padding: 8px 10px; border: 1px solid var(--border);
  border-radius: 6px; background: var(--bg); color: var(--fg); font-size: 13px;
}}
input.search:focus {{ outline: 2px solid var(--accent); }}
.nav-section {{ padding: 0 8px 6px; }}
.nav-section .nav-head {{
  font-size: 11px; font-weight: 700; color: var(--muted);
  text-transform: uppercase; letter-spacing: 0.5px;
  padding: 14px 8px 4px;
}}
.nav-item {{
  display: flex; align-items: center; gap: 8px;
  padding: 6px 10px; margin: 1px 0; border-radius: 5px;
  cursor: pointer; color: var(--sidebar-fg); text-decoration: none;
  font-size: 13.5px;
}}
.nav-item:hover {{ background: var(--hover); color: var(--link); }}
.nav-item.active {{
  background: var(--hover); color: var(--link); font-weight: 600;
  border-left: 3px solid var(--accent); padding-left: 7px;
}}
.nav-item code {{
  background: transparent; padding: 0; font-size: 13px;
  font-family: "JetBrains Mono", "D2Coding", Consolas, monospace;
  font-weight: 500;
}}
.nav-item .desc {{
  font-size: 11.5px; color: var(--muted);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  margin-left: auto; max-width: 140px;
}}
.toolbar {{
  display: flex; gap: 6px; margin: 14px 12px 0; padding-top: 12px;
  border-top: 1px solid var(--border);
}}
.toolbar button {{
  flex: 1; padding: 6px 8px; font-size: 12px; cursor: pointer;
  background: var(--bg); color: var(--fg); border: 1px solid var(--border);
  border-radius: 5px;
}}
.toolbar button:hover {{ background: var(--hover); }}
main.content {{
  flex: 1; padding: 32px 48px 80px; max-width: 1040px; margin: 0;
  min-width: 0; overflow-x: auto;
}}
main.content h1 {{ font-size: 26px; margin: 0 0 8px; }}
main.content h2 {{ font-size: 22px; border-bottom: 1px solid var(--border); padding-bottom: 8px; margin-top: 32px; }}
main.content h3 {{ font-size: 18px; margin-top: 24px; }}
main.content h4 {{ font-size: 15px; margin-top: 18px; }}
main.content p {{ margin: 10px 0; }}
main.content a {{ color: var(--link); text-decoration: none; }}
main.content a:hover {{ text-decoration: underline; }}
main.content code {{
  background: var(--code-bg); padding: 1px 5px; border-radius: 4px;
  font-family: "JetBrains Mono", "D2Coding", Consolas, monospace; font-size: 13px;
}}
main.content pre {{
  background: var(--code-bg); border: 1px solid var(--border);
  border-radius: 6px; padding: 14px 16px; overflow-x: auto; margin: 12px 0;
}}
main.content pre code {{ background: transparent; padding: 0; font-size: 13px; }}
main.content .table-wrap {{ overflow-x: auto; margin: 12px 0; }}
main.content table {{ border-collapse: collapse; min-width: 60%; font-size: 13px; }}
main.content th, main.content td {{
  border: 1px solid var(--border); padding: 6px 10px; vertical-align: top;
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
.cmd-header {{
  display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
  margin-bottom: 4px;
}}
.cmd-header .name {{
  font-family: "JetBrains Mono", "D2Coding", Consolas, monospace;
  font-size: 26px; font-weight: 700; padding: 4px 10px;
  background: var(--code-bg); border-radius: 6px; border: 1px solid var(--border);
}}
.cmd-header .cat {{
  font-size: 12px; color: var(--muted); padding: 4px 10px;
  background: var(--hover); border-radius: 999px; border: 1px solid var(--border);
}}
.badges {{ display: flex; gap: 6px; flex-wrap: wrap; margin-top: 8px; }}
.badge {{
  display: inline-block; font-size: 11px; padding: 3px 8px; border-radius: 4px;
  color: #fff; font-weight: 600; letter-spacing: 0.3px;
}}
.badge.oracle {{ background: var(--badge-oracle); }}
.badge.llm {{ background: var(--badge-llm); }}
.badge.none {{ background: var(--badge-none); }}
.lead {{
  font-size: 15px; color: var(--fg); border-left: 4px solid var(--accent);
  padding: 10px 14px; background: var(--hover); border-radius: 0 6px 6px 0;
  margin: 14px 0 22px;
}}
.path-row {{
  display: flex; gap: 10px; align-items: center; flex-wrap: wrap;
  font-size: 13px; margin-bottom: 22px;
}}
.path-row .label {{ color: var(--muted); }}
.empty-detail {{
  color: var(--muted); font-style: italic; margin-top: 18px;
}}
.home-cards {{
  display: grid; gap: 14px; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
  margin-top: 22px;
}}
.home-card {{
  border: 1px solid var(--border); border-radius: 8px;
  padding: 14px 18px; cursor: pointer; background: var(--bg);
  transition: transform 0.05s, border-color 0.1s;
}}
.home-card:hover {{
  border-color: var(--accent); transform: translateY(-1px);
}}
.home-card h3 {{ margin: 0 0 6px; font-size: 16px; }}
.home-card p {{ margin: 0; font-size: 13px; color: var(--muted); }}
.home-card .count {{
  display: inline-block; font-size: 11px; padding: 2px 8px;
  background: var(--hover); color: var(--muted); border-radius: 10px;
  margin-left: 6px;
}}
.btn-menu {{ display: none; }}
@media (max-width: 900px) {{
  .layout {{ flex-direction: column; }}
  aside.sidebar {{
    position: fixed; top: 0; left: 0; height: 100vh; width: 300px;
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
  <div class="search-wrap">
    <input class="search" type="text" id="search" placeholder="커맨드 검색…"/>
  </div>
  <div class="nav-section" id="navHome">
    <a class="nav-item" data-route="" href="#"><span>🏠</span><span>홈</span></a>
  </div>
  <div class="nav-section" id="navTopics">
    <div class="nav-head">시작하기</div>
    {topic_links}
  </div>
  <div class="nav-section" id="navCommands">
    {command_groups}
  </div>
  <div class="toolbar">
    <button id="btnTheme">🌓 테마</button>
    <button id="btnTop">↑ 상단</button>
  </div>
</aside>
<main class="content" id="content"></main>
</div>
<script>
const DOCS = {payload_json};
const TITLE = {title_json};

const $ = (sel, root) => (root || document).querySelector(sel);
const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

function escAttr(s) {{ return String(s).replace(/"/g, '&quot;'); }}

function badge(label, value) {{
  const cls = value === 'O' ? 'oracle' : value === '선택' ? 'llm' : 'none';
  return `<span class="badge ${{cls}}">${{label}}: ${{value || 'X'}}</span>`;
}}

function renderHome() {{
  const cards = DOCS.categories.map(cat => `
    <div class="home-card" data-cat="${{escAttr(cat.name)}}">
      <h3>${{escAttr(cat.name)}} <span class="count">${{cat.commands.length}}</span></h3>
      <p>${{cat.commands.map(c => '<code>' + c + '</code>').join(', ')}}</p>
    </div>
  `).join('');
  const topicCards = DOCS.topic_order.map(tid => {{
    const t = DOCS.topics[tid];
    return `<div class="home-card" data-topic="${{escAttr(tid)}}">
              <h3>${{escAttr(t.label)}}</h3>
              <p>${{escAttr(t.title)}}</p>
            </div>`;
  }}).join('');
  return `
    <h1>${{TITLE}}</h1>
    <p class="lead">좌측 목록에서 커맨드를 선택하면 상세 설명이 여기 표시됩니다. 카드를 눌러 빠르게 카테고리로 이동할 수도 있습니다.</p>
    <h2>📂 카테고리</h2>
    <div class="home-cards">${{cards}}</div>
    <h2>🧭 시작하기 / 참조</h2>
    <div class="home-cards">${{topicCards}}</div>
  `;
}}

function renderCommand(name) {{
  const c = DOCS.commands[name];
  if (!c) return `<p class="empty-detail">알 수 없는 커맨드: <code>${{escAttr(name)}}</code></p>`;
  const path = DOCS.paths[name] || '(별도 명시 없음)';
  const detail = c.detail_html
    ? c.detail_html
    : `<p class="empty-detail">이 커맨드는 README 에 별도 상세 섹션이 없습니다. 추가 옵션은 <code>python main.py ${{escAttr(name)}} --help</code> 로 확인하세요.</p>`;
  return `
    <div class="cmd-header">
      <span class="name">${{escAttr(name)}}</span>
      <span class="cat">${{escAttr(c.category)}}</span>
    </div>
    <div class="badges">
      ${{badge('Oracle', c.oracle)}}
      ${{badge('LLM', c.llm)}}
    </div>
    <div class="lead">${{c.desc_html}}</div>
    <div class="path-row"><span class="label">📁 산출물 경로:</span><code>${{escAttr(path)}}</code></div>
    ${{detail}}
  `;
}}

function renderTopic(tid) {{
  const t = DOCS.topics[tid];
  if (!t) return `<p class="empty-detail">알 수 없는 토픽.</p>`;
  return `<h1>${{escAttr(t.label)}}</h1>${{t.html}}`;
}}

function route() {{
  const hash = location.hash.slice(1);
  const parts = hash.split('/');
  let html;
  let activeKey = '';
  if (parts[0] === 'cmd' && parts[1]) {{
    html = renderCommand(parts[1]);
    activeKey = 'cmd/' + parts[1];
  }} else if (parts[0] === 'topic' && parts[1]) {{
    html = renderTopic(parts[1]);
    activeKey = 'topic/' + parts[1];
  }} else {{
    html = renderHome();
  }}
  const main = $('#content');
  main.innerHTML = html;
  main.scrollTop = 0;
  window.scrollTo({{top: 0}});
  $$('.nav-item').forEach(a => a.classList.remove('active'));
  if (activeKey) {{
    const link = $(`.nav-item[data-route="${{activeKey}}"]`);
    if (link) link.classList.add('active');
  }} else {{
    const homeLink = $('.nav-item[data-route=""]');
    if (homeLink) homeLink.classList.add('active');
  }}
  // 홈 카드 클릭 핸들러
  $$('.home-card[data-topic]').forEach(card => {{
    card.addEventListener('click', () => {{ location.hash = 'topic/' + card.dataset.topic; }});
  }});
  $$('.home-card[data-cat]').forEach(card => {{
    card.addEventListener('click', () => {{
      const cat = card.dataset.cat;
      const first = (DOCS.categories.find(c => c.name === cat) || {{}}).commands || [];
      if (first.length) location.hash = 'cmd/' + first[0];
    }});
  }});
}}

window.addEventListener('hashchange', route);
window.addEventListener('load', () => {{
  route();

  // 테마
  const root = document.documentElement;
  const saved = localStorage.getItem('docs-theme');
  if (saved) root.setAttribute('data-theme', saved);
  $('#btnTheme').addEventListener('click', () => {{
    const cur = root.getAttribute('data-theme') === 'dark' ? '' : 'dark';
    if (cur) root.setAttribute('data-theme', cur); else root.removeAttribute('data-theme');
    localStorage.setItem('docs-theme', cur);
  }});
  $('#btnTop').addEventListener('click', () => window.scrollTo({{top: 0, behavior: 'smooth'}}));

  // 검색
  const search = $('#search');
  search.addEventListener('input', () => {{
    const q = search.value.trim().toLowerCase();
    $$('#navCommands .nav-item').forEach(it => {{
      const text = (it.textContent || '').toLowerCase();
      it.style.display = (!q || text.indexOf(q) !== -1) ? '' : 'none';
    }});
    // 카테고리 헤더는 자식 보이는 게 1개라도 있으면 표시
    $$('.cat-group').forEach(g => {{
      const visible = $$('.nav-item', g).some(it => it.style.display !== 'none');
      g.style.display = visible ? '' : 'none';
    }});
  }});

  // 모바일 햄버거
  $('#btnMenu').addEventListener('click', () => $('#sidebar').classList.toggle('open'));
  $$('.nav-item').forEach(a => {{
    a.addEventListener('click', () => {{
      if (window.innerWidth <= 900) $('#sidebar').classList.remove('open');
    }});
  }});
}});
</script>
</body>
</html>
"""


def _render_topic_links(payload: dict) -> str:
    items: list[str] = []
    for tid in payload["topic_order"]:
        t = payload["topics"][tid]
        if not t["html"]:
            continue
        items.append(
            f'<a class="nav-item" data-route="topic/{tid}" '
            f'href="#topic/{tid}"><span>{_html.escape(t["label"])}</span></a>'
        )
    return "\n    ".join(items)


def _render_command_groups(payload: dict) -> str:
    out: list[str] = []
    for cat in payload["categories"]:
        out.append(f'<div class="cat-group">')
        out.append(f'<div class="nav-head">{_html.escape(cat["name"])}</div>')
        for name in cat["commands"]:
            c = payload["commands"].get(name)
            if not c:
                continue
            desc_short = re.sub(r"<[^>]+>", "", c["desc_html"])[:60]
            out.append(
                f'<a class="nav-item" data-route="cmd/{name}" '
                f'href="#cmd/{name}">'
                f'<code>{name}</code>'
                f'<span class="desc">{_html.escape(desc_short)}</span></a>'
            )
        out.append("</div>")
    return "\n    ".join(out)


def build_docs(md_path: str, out_path: str, title: str | None = None) -> str:
    if not os.path.isfile(md_path):
        raise FileNotFoundError(f"markdown not found: {md_path}")
    with open(md_path, "r", encoding="utf-8") as f:
        md = f.read()

    if title is None:
        m = re.search(r"^#\s+(.+)$", md, re.MULTILINE)
        title = m.group(1).strip() if m else os.path.basename(md_path)

    payload = _build_payload(md)
    payload_json = json.dumps(payload, ensure_ascii=False)
    title_json = json.dumps(title, ensure_ascii=False)

    page = _HTML_TEMPLATE.format(
        title=_html.escape(title),
        built_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        topic_links=_render_topic_links(payload),
        command_groups=_render_command_groups(payload),
        payload_json=payload_json,
        title_json=title_json,
    )

    out_dir = os.path.dirname(os.path.abspath(out_path))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(page)
    return os.path.abspath(out_path)
