"""Phase C — Screen Layout 추출 + HTML mockup 생성.

각 React 화면 파일을 LLM 으로 분석해 구조화 JSON (Page Title / Search
Panel / DataTable / Edit Mode / Tabs / Events with backend URLs) 추출 후
정적 HTML mockup 으로 렌더. 폐쇄망에서 외부 의존 0 (인라인 CSS).

옵트인 플래그: ``--extract-screen-layout`` (Phase A/B 의존하지 않음 —
``handlers_by_url`` 만 있으면 됨).

별도 옵션: ``--render-screenshots`` (스텁) — Playwright 등으로 진짜 화면
렌더는 사용자 환경에 React 빌드/실행 가능해야 해서 별도 path.

캐시: ``output/legacy_analysis/.screen_cache/<hash>.json``.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


SCREEN_SCHEMA_VERSION = "v3"   # v3: data_table_columns 가 {title,field,width,hide} 객체 — 캐시 무효화

_DEFAULT_CONFIG = {
    "llm_max_chars": 32000,    # 큰 React 파일 대응 (Qwen 397B 컨텍스트 활용)
    "llm_batch_size": 1,       # 화면당 1 호출 (배치 작음)
    "max_screens": 200,
}


@dataclass
class ScreenField:
    label: str = ""
    component: str = ""
    default: str = ""
    options: str = ""


@dataclass
class ScreenEvent:
    trigger: str = ""        # 버튼/이벤트 라벨
    event: str = ""          # onClick / onChange / componentDidMount ...
    backend_url: str = ""


@dataclass
class TableColumn:
    title: str = ""          # 사용자에게 보여지는 컬럼 헤더 (예: "LOT")
    field: str = ""          # 실제 데이터 키 / dataIndex (예: "lotId")
    width: str = ""          # 폭 (CSS / px / 숫자). 빈 값이면 hide 후보
    hide: bool = False       # 명시적 hidden=true / display:none


@dataclass
class ScreenLayout:
    file: str = ""           # React 파일 상대경로
    page_title: str = ""
    search_panel: List[ScreenField] = field(default_factory=list)
    data_table_columns: List[TableColumn] = field(default_factory=list)
    edit_mode_fields: List[ScreenField] = field(default_factory=list)
    tabs: List[str] = field(default_factory=list)
    events: List[ScreenEvent] = field(default_factory=list)
    summary: str = ""
    source: str = "llm"      # "llm" | "fallback"


# ── 캐시 ──────────────────────────────────────────────────────────────


def _cache_dir(base: str = "output/legacy_analysis/.screen_cache") -> Path:
    p = Path(base)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _cache_key(file_content: str, url_map: Dict[str, List[str]]) -> str:
    payload = SCREEN_SCHEMA_VERSION + "\n" + file_content + "\n" + json.dumps(
        url_map, sort_keys=True
    )
    return hashlib.sha256(payload.encode("utf-8", errors="ignore")).hexdigest()


def _cache_get(key: str, enabled: bool) -> Optional[ScreenLayout]:
    if not enabled:
        return None
    fp = _cache_dir() / f"{key}.json"
    if not fp.exists():
        return None
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
        sp = [ScreenField(**f) for f in data.get("search_panel") or []]
        em = [ScreenField(**f) for f in data.get("edit_mode_fields") or []]
        ev = [ScreenEvent(**e) for e in data.get("events") or []]
        cols = [TableColumn(**c) if isinstance(c, dict) else TableColumn(title=str(c))
                for c in (data.get("data_table_columns") or [])]
        return ScreenLayout(
            file=data.get("file", ""),
            page_title=data.get("page_title", ""),
            search_panel=sp,
            data_table_columns=cols,
            edit_mode_fields=em,
            tabs=list(data.get("tabs") or []),
            events=ev,
            summary=data.get("summary", ""),
            source=data.get("source", "llm"),
        )
    except Exception:
        return None


def _cache_put(key: str, layout: ScreenLayout, enabled: bool) -> None:
    if not enabled:
        return
    fp = _cache_dir() / f"{key}.json"
    try:
        fp.write_text(json.dumps(asdict(layout), ensure_ascii=False, indent=2),
                      encoding="utf-8")
    except Exception as e:
        logger.warning("screen cache 저장 실패 %s: %s", fp, e)


# ── LLM ──────────────────────────────────────────────────────────────


_SYSTEM_PROMPT = """당신은 React 화면 분석 전문가입니다. 주어진 React/JSX
파일을 분석해 화면 구조를 JSON 으로 추출하세요. 추측하지 말고 코드에서
명확히 읽히는 것만 채우세요. 발견 안 된 필드는 빈 배열/문자열로 두세요.

**중요**: events / backend_url 필드는 반환하지 마세요. 이벤트→백엔드 URL
매핑은 외부 정적 분석 결과를 사용하므로 LLM 응답에서 무시됩니다.
임의로 URL 을 추측하지 마세요.

**DataTable 컬럼 추출 규칙**:
- title (헤더에 표시되는 한글 이름) 과 field (dataIndex / key — 실제 데이터
  매핑 키, 영문) 둘 다 채우세요.
- ``width`` 가 명시되지 않은 컬럼, ``hidden: true`` / ``hide: true`` /
  ``visible: false`` / ``display: none`` 인 컬럼은 ``hide: true`` 로 표시.
  나머지는 ``hide: false``.

반환 schema (JSON):
{
  "page_title": "string — 화면 상단 제목",
  "search_panel": [
    {"label": "필드 라벨", "component": "DatePicker | Select | Input | ...",
     "default": "기본값 설명", "options": "옵션/리스트 설명"}
  ],
  "data_table_columns": [
    {"title": "컬럼 헤더 (사용자에게 보이는 이름, 예: LOT)",
     "field": "실제 데이터 키 / dataIndex (예: lotId)",
     "width": "폭 표현 (예: '100', '100px', '20%') — 없으면 빈 문자열",
     "hide": false}
  ],
  "edit_mode_fields": [
    {"label": "...", "component": "...", "default": "...", "options": "..."}
  ],
  "tabs": ["탭1", "탭2", ...],
  "summary": "1-2 줄 화면 설명"
}
"""


_RENDER_METHOD_RE = re.compile(r"^\s*render\s*\(\s*\)\s*\{", re.MULTILINE)
_FUNC_RETURN_JSX_RE = re.compile(r"return\s*\(\s*<", re.MULTILINE)
_IMPORT_LINE_RE = re.compile(r"^\s*import\s+", re.MULTILINE)


def _smart_slice(content: str, max_chars: int) -> str:
    """대용량 React 파일에서 LLM 분석에 필요한 부분만 추출.

    포함:
      1. imports 섹션 (라이브러리 imports — 컴포넌트 종류 단서)
      2. render() 메서드 본문 또는 functional component return JSX

    제외:
      - styled-components (긴 CSS template literals)
      - propTypes / defaultProps
      - render 와 무관한 helper functions
      - 주석 (regex slice 라 일부 남아있을 수 있음)

    파일이 max_chars 이하면 그대로 반환 (회귀 0).
    """
    if len(content) <= max_chars:
        return content

    parts: list[str] = []

    # 1. imports — 첫 import 부터 마지막 연속 import 라인까지
    imp_lines = []
    seen_import = False
    for line in content.split("\n"):
        s = line.lstrip()
        if s.startswith("import "):
            seen_import = True
            imp_lines.append(line)
        elif seen_import and (s.startswith("//") or not s):
            imp_lines.append(line)
        elif seen_import:
            break
    if imp_lines:
        parts.append("\n".join(imp_lines))
        parts.append("\n// ... (styled-components / helpers 생략) ...\n")

    # 2. render() 또는 functional return JSX block (brace walker)
    m = _RENDER_METHOD_RE.search(content)
    if m:
        start = m.start()
        i = content.index("{", m.start())
        depth = 0
        end = len(content)
        while i < len(content):
            c = content[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
            i += 1
        parts.append(content[start:end])
    else:
        # functional: 첫 'return ( <' 부터 적당한 청크
        m = _FUNC_RETURN_JSX_RE.search(content)
        if m:
            chunk_size = max(2000, max_chars // 2)
            parts.append(content[m.start(): m.start() + chunk_size])

    result = "\n".join(parts)
    if len(result) > max_chars:
        result = result[:max_chars] + "\n... (smart slice truncated)"
    return result


def _build_user_prompt(file_rel: str, file_content: str,
                       url_map: Dict[str, List[str]],
                       max_chars: int) -> str:
    body = _smart_slice(file_content, max_chars)
    if len(file_content) > max_chars:
        body += (f"\n\n// (smart slice 적용: original {len(file_content)} chars "
                 f"→ {len(body)} chars 만 LLM 전달)")
    url_lines = []
    for handler, urls in sorted(url_map.items()):
        if urls:
            url_lines.append(f"  {handler} → {', '.join(sorted(set(urls)))}")
    url_block = "\n".join(url_lines) if url_lines else "  (없음)"
    return (
        f"파일: {file_rel}\n\n"
        f"## handler ↔ backend URL 매핑 (사전 정적 분석 결과)\n"
        f"{url_block}\n\n"
        f"## React 소스\n```jsx\n{body}\n```\n\n"
        "위 schema 의 JSON 만 반환하세요. 코드블록/설명 없이 raw JSON 만."
    )


def _call_llm_safe(prompt: str, config: Dict[str, Any],
                   label: str = "screen") -> Optional[Dict[str, Any]]:
    try:
        from .legacy_pattern_discovery import _call_llm
    except Exception:
        return None
    try:
        raw = _call_llm(prompt, config or {}, label=label,
                        system_prompt=_SYSTEM_PROMPT)
    except Exception as e:
        logger.warning("screen LLM 호출 실패: %s", e)
        return None
    if not raw:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list) and raw and isinstance(raw[0], dict):
        return raw[0]
    return None


# ── Fallback (LLM 없을 때) — 정적 분석 결과만 가지고 events 만 채움 ──


def _fallback_layout(file_rel: str, url_map: Dict[str, List[str]]) -> ScreenLayout:
    events: List[ScreenEvent] = []
    for handler, urls in sorted(url_map.items()):
        for u in sorted(set(urls or [])):
            # handler 라벨이 [event] label 형태면 분리
            m = re.match(r"\[(?P<ev>[^\]]+)\]\s*(?P<label>.*)", handler)
            if m:
                events.append(ScreenEvent(
                    trigger=m.group("label").strip() or handler,
                    event=m.group("ev").strip(),
                    backend_url=u,
                ))
            else:
                events.append(ScreenEvent(trigger=handler, event="", backend_url=u))
    return ScreenLayout(
        file=file_rel,
        page_title=os.path.splitext(os.path.basename(file_rel))[0],
        events=events,
        summary="LLM 없이 정적 분석 fallback — events 만 채움",
        source="fallback",
    )


def _parse_layout_dict(file_rel: str, data: Dict[str, Any]) -> ScreenLayout:
    def _fields(key: str) -> List[ScreenField]:
        out = []
        for f in data.get(key) or []:
            if isinstance(f, dict):
                out.append(ScreenField(
                    label=str(f.get("label", "")),
                    component=str(f.get("component", "")),
                    default=str(f.get("default", "")),
                    options=str(f.get("options", "")),
                ))
        return out

    events: List[ScreenEvent] = []
    for e in data.get("events") or []:
        if isinstance(e, dict):
            events.append(ScreenEvent(
                trigger=str(e.get("trigger", "")),
                event=str(e.get("event", "")),
                backend_url=str(e.get("backend_url", "")),
            ))
    cols: List[TableColumn] = []
    for c in data.get("data_table_columns") or []:
        if isinstance(c, dict):
            cols.append(TableColumn(
                title=str(c.get("title", "")),
                field=str(c.get("field", "")),
                width=str(c.get("width", "")),
                hide=bool(c.get("hide", False)),
            ))
        else:
            # 옛 형식 (단순 string) 호환
            cols.append(TableColumn(title=str(c)))
    return ScreenLayout(
        file=file_rel,
        page_title=str(data.get("page_title", "")),
        search_panel=_fields("search_panel"),
        data_table_columns=cols,
        edit_mode_fields=_fields("edit_mode_fields"),
        tabs=[str(t) for t in (data.get("tabs") or [])],
        events=events,
        summary=str(data.get("summary", "")),
        source="llm",
    )


# ── 메인 추출 ─────────────────────────────────────────────────────────


def _group_handlers_by_file(handlers_by_url: Dict[str, List[Dict[str, Any]]]
                            ) -> Dict[str, Dict[str, List[str]]]:
    """``{file: {handler_label: [url, ...]}}`` 으로 변환."""
    out: Dict[str, Dict[str, List[str]]] = {}
    for url, ctx_list in (handlers_by_url or {}).items():
        for ctx in ctx_list or []:
            f = ctx.get("file") or ""
            if not f:
                continue
            handler = ctx.get("handler") or ""
            event_marker = ctx.get("event") or ""
            label = ctx.get("label") or ""
            tag = label or handler or "<inline>"
            full_handler = f"[{event_marker}] {tag}" if event_marker else tag
            out.setdefault(f, {}).setdefault(full_handler, []).append(url)
    return out


def extract_screen_layouts(
    frontend_dir: str,
    handlers_by_url: Dict[str, List[Dict[str, Any]]],
    patterns: Dict[str, Any],
    *,
    max_screens: int = 200,
    use_cache: bool = True,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, ScreenLayout]:
    """파일별로 한 번씩 LLM 호출 + 캐시. ``{file_rel: ScreenLayout}`` 반환."""
    cfg = dict(_DEFAULT_CONFIG)
    cfg.update((config or {}).get("screen_extraction") or {})
    max_chars = int(cfg.get("llm_max_chars", 6000))
    max_screens = max(1, min(max_screens, int(cfg.get("max_screens", 200))))

    by_file = _group_handlers_by_file(handlers_by_url)
    if not by_file:
        print("  screen layout: handler 컨텍스트 0건 — skip")
        return {}

    files = list(by_file.keys())
    if len(files) > max_screens:
        print(f"  screen layout: {len(files)} files > cap {max_screens} — truncate")
        files = files[:max_screens]

    print(f"  screen layout: {len(files)} React 화면 파일 분석 시작")

    out: Dict[str, ScreenLayout] = {}
    cache_hits = 0
    llm_calls = 0
    fallback_calls = 0

    for rel in files:
        url_map = by_file[rel]
        abs_fp = os.path.join(frontend_dir, rel)
        try:
            with open(abs_fp, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            # 주석 제거 — LLM 이 주석된 옛 코드를 보고 page_title /
            # search_panel 등을 잘못 추출하는 케이스 방지.
            from .legacy_react_api_scanner import _strip_comments
            content = _strip_comments(content)
        except Exception:
            content = ""
        cache_key = _cache_key(content, url_map)
        cached = _cache_get(cache_key, use_cache)
        if cached:
            cached.file = rel
            out[rel] = cached
            cache_hits += 1
            continue

        prompt = _build_user_prompt(rel, content, url_map, max_chars)
        data = _call_llm_safe(prompt, config or {}, label=f"screen:{rel[:40]}")
        if data:
            layout = _parse_layout_dict(rel, data)
            llm_calls += 1
            # events 는 항상 정적 분석 결과로 덮어쓰기 — LLM 이 plausible 한
            # 환각 URL 만들어 진짜 호출 URL 가리는 케이스 차단. handlers_by_url
            # 는 collect_handler_contexts 가 JSX/saga 정적 분석으로 추출한
            # ground truth.
            layout.events = _fallback_layout(rel, url_map).events
        else:
            layout = _fallback_layout(rel, url_map)
            fallback_calls += 1
        out[rel] = layout
        _cache_put(cache_key, layout, use_cache)

    print(f"  screen layout: cache_hits={cache_hits}, llm={llm_calls}, "
          f"fallback={fallback_calls}, total={len(out)}")
    return out


# ── HTML mockup 렌더 ──────────────────────────────────────────────────


_HTML_TEMPLATE = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<title>{title}</title>
<style>
  body {{ font-family: -apple-system, "맑은 고딕", sans-serif; margin: 0;
         background: #f4f5f7; color: #222; }}
  header {{ background: #2c3e50; color: #fff; padding: 12px 20px;
            font-size: 18px; font-weight: 600; }}
  main {{ max-width: 1100px; margin: 16px auto; padding: 0 16px; }}
  section {{ background: #fff; border-radius: 4px; padding: 16px;
             margin-bottom: 12px; box-shadow: 0 1px 2px rgba(0,0,0,0.05); }}
  section h2 {{ font-size: 14px; color: #555; margin: 0 0 12px;
                border-bottom: 1px solid #eee; padding-bottom: 6px; }}
  .field {{ display: inline-block; margin: 4px 12px 4px 0; }}
  .field label {{ font-size: 12px; color: #666; display: block; margin-bottom: 2px; }}
  .field input, .field select {{ border: 1px solid #ccc; padding: 4px 8px;
            background: #fafafa; min-width: 140px; }}
  .field .note {{ font-size: 11px; color: #999; margin-left: 4px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th, td {{ border: 1px solid #ddd; padding: 6px 8px; text-align: left; }}
  th {{ background: #f0f0f0; }}
  td.placeholder {{ color: #bbb; font-style: italic; }}
  .tab-bar {{ display: flex; border-bottom: 2px solid #2c3e50; }}
  .tab {{ padding: 8px 16px; cursor: pointer; background: #ecf0f1;
         border: 1px solid #ddd; border-bottom: 0; margin-right: 2px; }}
  .tab.active {{ background: #2c3e50; color: #fff; }}
  .events {{ font-size: 12px; }}
  .events table th {{ background: #fffbe5; }}
  .meta {{ font-size: 11px; color: #999; margin-top: 16px;
           text-align: right; }}
  .empty {{ color: #aaa; font-style: italic; }}
</style></head><body>
<header>{title_html}</header>
<main>
{summary_block}
{search_block}
{tab_block}
{table_block}
{edit_block}
{events_block}
<div class="meta">file: {file_rel} · source: {source}</div>
</main></body></html>
"""


def _esc(s: str) -> str:
    return html.escape(s or "", quote=True)


def _render_field_list(fields: List[ScreenField]) -> str:
    """필드 목록 → 텍스트 bullet 리스트 (사용자 요청 — input mock-form 대신
    설명 형태). 항목당:

      <strong>라벨</strong>
        - 사용 컴포넌트: ...
        - Default: ...
        - Options: ...
    """
    items = []
    for f in fields:
        sub = []
        if f.component:
            sub.append(f"<li>사용 컴포넌트: {_esc(f.component)}</li>")
        if f.default:
            sub.append(f"<li>Default: {_esc(f.default)}</li>")
        if f.options:
            sub.append(f"<li>Options: {_esc(f.options)}</li>")
        sub_html = f"<ul>{''.join(sub)}</ul>" if sub else ""
        items.append(
            f"<li><strong>{_esc(f.label or '(no label)')}</strong>{sub_html}</li>"
        )
    return f"<ol>{''.join(items)}</ol>" if items else ""


def _render_search(fields: List[ScreenField]) -> str:
    if not fields:
        return ""
    return ("<section><h2>Search Panel — 조회 조건 영역</h2>"
            + _render_field_list(fields) + "</section>")


def _render_table(cols: List[TableColumn]) -> str:
    if not cols:
        return ""

    # hide 분류: 명시적 hide=true OR width 빈 값 (사용자 요청).
    visible = [c for c in cols if (not c.hide) and c.width]
    hidden = [c for c in cols if c.hide or not c.width]

    out = "<section><h2>DataTable</h2>"

    if visible:
        title_row = "".join(f"<th>{_esc(c.title or c.field or '?')}</th>" for c in visible)
        field_row = "".join(
            f"<td class='field-row'><code>{_esc(c.field) or '<em>(no field)</em>'}</code></td>"
            for c in visible
        )
        sample = "".join("<td class='placeholder'>...</td>" for _ in visible)
        sample_rows = "".join(f"<tr>{sample}</tr>" for _ in range(3))
        out += (
            f"<table><thead><tr>{title_row}</tr></thead>"
            f"<tbody><tr>{field_row}</tr>{sample_rows}</tbody></table>"
        )

    if hidden:
        items = []
        for c in hidden:
            label_parts = []
            if c.title:
                label_parts.append(_esc(c.title))
            if c.field:
                label_parts.append(f"<code>{_esc(c.field)}</code>")
            note = " ".join(label_parts) or "(unknown)"
            if c.hide and not c.width:
                reason = "hide=true, no width"
            elif c.hide:
                reason = "hide=true"
            else:
                reason = "no width"
            items.append(f"<li>{note} <small>({reason})</small></li>")
        out += (
            "<div class='hidden-cols'><strong>Hide 항목 "
            f"({len(hidden)}개):</strong><ul>"
            + "".join(items) + "</ul></div>"
        )

    out += "</section>"
    return out


def _render_edit(fields: List[ScreenField]) -> str:
    if not fields:
        return ""
    return ("<section><h2>Edit Mode — 편집 영역</h2>"
            + _render_field_list(fields) + "</section>")


def _render_tabs(tabs: List[str]) -> str:
    if not tabs:
        return ""
    items = []
    for i, t in enumerate(tabs):
        cls = "tab active" if i == 0 else "tab"
        items.append(f"<div class='{cls}'>{_esc(t)}</div>")
    return f"<section><h2>Tabs</h2><div class='tab-bar'>{''.join(items)}</div></section>"


def _render_events(events: List[ScreenEvent]) -> str:
    """Trigger + Event 별로 그룹화 후 한 row 에 backend URL 들을 줄바꿈으로
    join. 사용자 요청: "트리거 기준으로 한줄에 backend url 여러줄 (한칸에
    줄바꿈으로 구분 콤마제거)".
    """
    if not events:
        return ""
    # (trigger, event) → ordered unique URLs
    grouped: dict[tuple[str, str], list[str]] = {}
    for e in events:
        key = (e.trigger or "<inline>", e.event or "")
        urls = grouped.setdefault(key, [])
        if e.backend_url and e.backend_url not in urls:
            urls.append(e.backend_url)
    rows = []
    for (trigger, event), urls in grouped.items():
        url_html = "<br>".join(f"<code>{_esc(u)}</code>" for u in urls) or "—"
        rows.append(
            f"<tr><td>{_esc(trigger)}</td><td>{_esc(event)}</td>"
            f"<td>{url_html}</td></tr>"
        )
    return (f"<section class='events'><h2>이벤트 → 백엔드 URL</h2>"
            f"<table><thead><tr><th>Trigger</th><th>Event</th>"
            f"<th>Backend URL</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></section>")


def render_screen_html(layout: ScreenLayout) -> str:
    title = layout.page_title or os.path.basename(layout.file) or "Screen"
    summary_block = (f"<section><h2>요약</h2><div>{_esc(layout.summary)}</div></section>"
                     if layout.summary else "")
    return _HTML_TEMPLATE.format(
        title=_esc(title),
        title_html=_esc(title),
        summary_block=summary_block,
        search_block=_render_search(layout.search_panel),
        tab_block=_render_tabs(layout.tabs),
        table_block=_render_table(layout.data_table_columns),
        edit_block=_render_edit(layout.edit_mode_fields),
        events_block=_render_events(layout.events),
        file_rel=_esc(layout.file),
        source=_esc(layout.source),
    )


def write_screen_html_files(out_dir: str,
                             layouts: Dict[str, ScreenLayout]) -> Dict[str, str]:
    """``{file_rel: html_path}`` 반환. 화면별 .html 파일 저장.

    레이아웃: ``out_dir/<top-folder>/<safe_rest>.html``. ``top-folder`` 는
    file_rel 의 첫 segment (대개 repo / bucket / app slug). 같은 폴더에
    수백 개 화면이 평탄하게 쌓이지 않도록 1단계 분리.
    """
    os.makedirs(out_dir, exist_ok=True)
    written: Dict[str, str] = {}
    for rel, layout in layouts.items():
        norm = rel.replace("\\", "/")
        parts = [p for p in norm.split("/") if p]
        if len(parts) > 1:
            repo_dir = re.sub(r"[^A-Za-z0-9_.-]+", "_", parts[0]) or "_root"
            rest = "/".join(parts[1:])
        else:
            repo_dir = "_root"
            rest = parts[0] if parts else "screen"
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", rest.replace("/", "__"))
        sub = os.path.join(out_dir, repo_dir)
        os.makedirs(sub, exist_ok=True)
        path = os.path.join(sub, safe + ".html")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(render_screen_html(layout))
            written[rel] = path
        except Exception as e:
            logger.warning("screen html write 실패 %s: %s", path, e)
    return written


# ── 옵션 G stub: Playwright 스크린샷 ──────────────────────────────────


def render_screenshots_via_playwright(
    layouts: Dict[str, ScreenLayout],
    *,
    base_url: Optional[str] = None,
    out_dir: str = "output/legacy_analysis/screenshots",
) -> Dict[str, str]:
    """별도 옵트인 (`--render-screenshots`). 사용자 PC 에 React 빌드/실행
    + Playwright 설치된 환경에서만 동작.

    현재는 스텁 — 실제 구현은 follow-up. 호출 시 안내 메시지 emit.
    """
    print("  screenshot rendering: --render-screenshots 옵션은 현재 stub.")
    print("  실제 구현은 사용자 PC 에 React 빌드 + Playwright 셋업 필요.")
    print("  follow-up PR 에서 base_url 기반 자동 스크린샷 추가 예정.")
    return {}
