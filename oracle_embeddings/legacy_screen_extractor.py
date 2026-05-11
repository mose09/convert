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
    parent_handlers: List[str] = field(default_factory=list)  # this.props.X → 부모 함수 이름


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
    flowchart_mermaid: str = ""   # 사용자 액션 흐름 (Mermaid flowchart TB)
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
            flowchart_mermaid=data.get("flowchart_mermaid", ""),
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

**이미지가 첨부된 경우**: 첨부된 이미지는 **출력될 flowchart_mermaid
의 형태/스타일 sample** 입니다 (화면 스크린샷이 아닙니다). 해당 sample
의 노드 모양 / 분기 구조 / 화살표 방향 / 색상 / 그룹화 스타일을 그대로
참고해서 같은 시각 형태로 ``flowchart_mermaid`` 코드를 작성하세요.
화면 layout / search_panel / data_table_columns 추출과는 무관 — 그 필드들은
React 코드 텍스트만 보고 추출.

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
  "flowchart_mermaid": "사용자 액션 흐름 Mermaid flowchart TB 코드. 예시:\nflowchart TB\n    Start((화면 진입)) --> Init[초기 데이터 로드]\n    Init --> Display[그리드 표시]\n    Display --> Search{조회 클릭}\n    Search --> Update[그리드 갱신]\n    Display --> Detail{행 더블클릭}\n    Detail --> Popup[상세 popup 열림]\nMermaid v11 호환 규칙: (a) 라벨에 ``()`` / ``[]`` / ``/`` / ``:`` 등 특수문자 들어가면 반드시 double quote 로 감싸기 — 예: Save[\"저장(POST)\"] / Cond{\"조회 N건\"}. (b) 노드 ID 는 영문/숫자/언더스코어만, 예약어 ``end`` / ``class`` / ``subgraph`` / ``style`` 사용 금지. (c) 백엔드 URL 표시 X (events 표에 별도). 사용자 인터랙션 흐름만. (d) ``%%{init}%%`` 테마 directive 넣지 마. (e) 코드만 (```mermaid 펜스 X).",
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
                       max_chars: int,
                       *,
                       closure_markdown: Optional[str] = None) -> str:
    """LLM 사용자 프롬프트 빌더.

    ``closure_markdown`` 이 주어지면 `_smart_slice(file_content)` 대신
    closure 직렬화 결과 (entry + import 그래프 BFS 로 묶인 facts) 를
    소스 섹션으로 사용. ``--closure-llm`` 옵트인 경로.
    """
    if closure_markdown is not None:
        source_section = (
            "## React closure (import 그래프 BFS + popup 3 신호)\n"
            f"{closure_markdown}"
        )
    else:
        body = _smart_slice(file_content, max_chars)
        if len(file_content) > max_chars:
            body += (f"\n\n// (smart slice 적용: original {len(file_content)} chars "
                     f"→ {len(body)} chars 만 LLM 전달)")
        source_section = f"## React 소스\n```jsx\n{body}\n```"
    url_lines = []
    for handler, urls in sorted(url_map.items()):
        if urls:
            url_lines.append(f"  {handler} → {', '.join(sorted(set(urls)))}")
    url_block = "\n".join(url_lines) if url_lines else "  (없음)"
    return (
        f"파일: {file_rel}\n\n"
        f"## handler ↔ backend URL 매핑 (사전 정적 분석 결과)\n"
        f"{url_block}\n\n"
        f"{source_section}\n\n"
        "위 schema 의 JSON 만 반환하세요. 코드블록/설명 없이 raw JSON 만."
    )


def _build_closure_markdown(rel: str, abs_fp: str, frontend_dir: str,
                            patterns: Dict[str, Any],
                            max_depth: int, token_budget: int) -> Optional[str]:
    """Opt-in closure 빌드 + 직렬화. tree-sitter 미설치 시 None."""
    try:
        from .legacy_react_closure import build_closure, serialize_for_llm
    except Exception as e:
        logger.warning(
            "closure_llm requested but legacy_react_closure import failed "
            "(tree-sitter wheel 미설치?): %s — 기존 smart_slice fallback", e
        )
        return None
    try:
        closure = build_closure(
            entry_file=abs_fp,
            repo_root=frontend_dir,
            patterns=patterns,
            max_depth=max_depth,
            token_budget=token_budget,
            verbose=False,
        )
        return serialize_for_llm(closure)
    except Exception as e:
        logger.warning("closure build failed for %s: %s", rel, e)
        return None


def _find_flowchart_sample(base_dir: str = "input") -> Optional[str]:
    """출력될 flowchart 의 형태/스타일 sample 이미지 — 모든 화면 공통 사용.

    사용자가 ``input/flowchart_sample.{png,jpg,jpeg,webp}`` 에 sample
    flowchart 이미지를 올리면 LLM 한테 첨부 → 같은 스타일로 flowchart_mermaid
    생성. 없으면 text-only 동작 (기존 그대로).
    """
    for name in ("flowchart_sample", "flowchart-sample"):
        for ext in ("png", "jpg", "jpeg", "webp"):
            p = os.path.join(base_dir, f"{name}.{ext}")
            if os.path.isfile(p):
                return p
    return None


def _call_llm_safe(prompt: str, config: Dict[str, Any],
                   label: str = "screen",
                   image_paths: Optional[list] = None) -> Optional[Dict[str, Any]]:
    try:
        from .legacy_pattern_discovery import _call_llm
    except Exception:
        return None
    try:
        raw = _call_llm(prompt, config or {}, label=label,
                        system_prompt=_SYSTEM_PROMPT,
                        image_paths=image_paths)
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
        flowchart_mermaid=str(data.get("flowchart_mermaid", "")),
        summary=str(data.get("summary", "")),
        source="llm",
    )


# ── 메인 추출 ─────────────────────────────────────────────────────────


def _group_handlers_by_file(handlers_by_url: Dict[str, List[Dict[str, Any]]]
                            ) -> Dict[str, Dict[str, List[str]]]:
    """``{file: {handler_label: [url, ...]}}`` 으로 변환.

    parent_handlers 가 있으면 (다른 파일에서 prop binding 으로 도달한 부모
    함수) event_marker 에 ``→ parent.handleX`` 추가.
    """
    out: Dict[str, Dict[str, List[str]]] = {}
    for url, ctx_list in (handlers_by_url or {}).items():
        for ctx in ctx_list or []:
            f = ctx.get("file") or ""
            if not f:
                continue
            handler = ctx.get("handler") or ""
            event_marker = ctx.get("event") or ""
            parents = ctx.get("parent_handlers") or []
            if parents:
                event_marker += " → " + ", ".join(f"parent.{p}" for p in parents)
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
    closure_llm: bool = False,
    closure_max_depth: int = 3,
    closure_token_budget: int = 12000,
) -> Dict[str, ScreenLayout]:
    """파일별로 한 번씩 LLM 호출 + 캐시. ``{file_rel: ScreenLayout}`` 반환.

    ``closure_llm=True`` (옵트인) 시 LLM input 을 raw JSX + smart_slice 대신
    AST 기반 closure markdown (import 그래프 BFS + popup 3 신호) 로 보강.
    tree-sitter 미설치/closure 빌드 실패 시 자동 smart_slice fallback.
    """
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
    closure_used = 0
    closure_failed = 0

    # 출력될 flowchart 형태 sample 이미지 — 한 번 lookup, 모든 화면 공통.
    sample_image = _find_flowchart_sample()
    if sample_image:
        print(f"  flowchart sample image: {sample_image}")

    if closure_llm:
        print(f"  closure_llm=ON (max_depth={closure_max_depth}, "
              f"token_budget={closure_token_budget})")

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

        closure_md: Optional[str] = None
        if closure_llm:
            closure_md = _build_closure_markdown(
                rel, abs_fp, frontend_dir, patterns or {},
                closure_max_depth, closure_token_budget,
            )
            if closure_md:
                closure_used += 1
            else:
                closure_failed += 1

        # 캐시 키 — closure markdown 사용 시 raw content 대신 markdown 해시
        # (다른 입력 → 다른 LLM 결과). closure off 경로는 회귀 0 유지.
        cache_key = _cache_key(closure_md or content, url_map)
        cached = _cache_get(cache_key, use_cache)
        if cached:
            cached.file = rel
            out[rel] = cached
            cache_hits += 1
            continue

        prompt = _build_user_prompt(
            rel, content, url_map, max_chars,
            closure_markdown=closure_md,
        )
        data = _call_llm_safe(
            prompt, config or {}, label=f"screen:{rel[:40]}",
            image_paths=[sample_image] if sample_image else None,
        )
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

    closure_stats = (
        f", closure_used={closure_used}, closure_failed={closure_failed}"
        if closure_llm else ""
    )
    print(f"  screen layout: cache_hits={cache_hits}, llm={llm_calls}, "
          f"fallback={fallback_calls}, total={len(out)}{closure_stats}")
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
  pre.mermaid {{ background: #f8f9fa; border: 1px solid #e0e0e0;
                  padding: 12px; font-size: 12px; overflow-x: auto;
                  font-family: Consolas, "Courier New", monospace; }}
  .meta {{ font-size: 11px; color: #999; margin-top: 16px;
           text-align: right; }}
  .empty {{ color: #aaa; font-style: italic; }}
</style>
<script src="https://cdn.jsdelivr.net/npm/mermaid/dist/mermaid.min.js"></script>
<script>
// Mermaid 11.x 는 strict + parse 실패 시 throw 대신 에러 SVG 를 그려 반환.
// → suppressErrorRendering: true 로 throw 강제 + parse() 로 사전 검증 → catch
// 가 raw 텍스트 + 에러 메시지를 노출 (사용자 환경 단방향이라 디버깅 단서 필수).
if (window.mermaid) {{
  mermaid.initialize({{
    startOnLoad: false,
    securityLevel: 'loose',
    suppressErrorRendering: true
  }});
  window.addEventListener('DOMContentLoaded', async function () {{
    var blocks = document.querySelectorAll('pre.mermaid');
    var ver = (typeof mermaid.version === 'function')
              ? mermaid.version() : (mermaid.version || '?');
    for (var i = 0; i < blocks.length; i++) {{
      var el = blocks[i];
      var src = el.textContent;
      var lastErr = null;
      try {{
        await mermaid.parse(src);   // parse 가 명확한 syntax error throw
        var id = 'm' + i + '_' + Math.random().toString(36).slice(2);
        var out = await mermaid.render(id, src);
        // render 가 어쩌다 success 하지만 SVG 안에 에러를 그린 케이스 방어 —
        // SVG 가 'aria-roledescription="error"' 또는 'mermaid-error' 클래스
        // 포함하면 실패로 간주.
        if (/aria-roledescription="error"|class="error-/.test(out.svg)) {{
          throw new Error('mermaid render returned error SVG');
        }}
        el.innerHTML = out.svg;
        continue;
      }} catch (e) {{
        lastErr = e;
      }}
      var msg = (lastErr && (lastErr.message || lastErr.str)) || String(lastErr);
      el.innerHTML =
        '<div style="color:#c0392b;font-weight:600;margin-bottom:6px;">' +
        'Mermaid parse error (v' + ver + '): ' + msg + '</div>' +
        '<pre style="background:#fff;color:#333;border:1px dashed #c0392b;' +
        'padding:8px;white-space:pre-wrap;font-family:Consolas,monospace;">' +
        src.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;') +
        '</pre>';
    }}
  }});
}}
</script>
</head><body>
<header>{title_html}</header>
<main>
{summary_block}
{search_block}
{tab_block}
{table_block}
{flowchart_block}
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


def _event_sort_rank(event: str) -> int:
    """이벤트 정렬 우선순위 (사용자 요청 — 화면오픈 → onChange → onClick → 그 외).

    낮을수록 먼저 표시.
    """
    e = (event or "").lower()
    if any(s in e for s in ("mount", "useeffect", "didmount", "willmount")):
        return 0   # 화면 최초 오픈
    if "didupdate" in e:
        return 1   # 업데이트
    if "change" in e:
        return 2   # onChange / onValueChange
    if "submit" in e:
        return 3   # form submit
    if "click" in e:
        return 4   # 버튼 클릭
    return 5       # 그 외 (onBlur, onFocus, onKeyDown, ...)


def _render_events(events: List[ScreenEvent]) -> str:
    """Trigger + Event 별로 그룹화 후 한 row 에 backend URL 들을 줄바꿈으로
    join. 사용자 요청: 화면오픈 (mount) → onChange → onClick 순으로 정렬.
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
    # 정렬: event rank → trigger 알파벳
    sorted_keys = sorted(
        grouped.keys(),
        key=lambda k: (_event_sort_rank(k[1]), k[1].lower(), k[0])
    )
    rows = []
    for key in sorted_keys:
        trigger, event = key
        urls = grouped[key]
        url_html = "<br>".join(f"<code>{_esc(u)}</code>" for u in urls) or "—"
        rows.append(
            f"<tr><td>{_esc(trigger)}</td><td>{_esc(event)}</td>"
            f"<td>{url_html}</td></tr>"
        )
    return (f"<section class='events'><h2>이벤트 → 백엔드 URL</h2>"
            f"<table><thead><tr><th>Trigger</th><th>Event</th>"
            f"<th>Backend URL</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></section>")


_MERMAID_DIRECTIVE_RE = re.compile(r"%%\{.*?\}%%", re.DOTALL)
# Shape 별 매칭. 안 brackets 허용 (단 같은 종류 brackets nesting 만 차단).
# [...] 사각 / {...} 마름모 / {{...}} 육각.  Round/circle (...) / ((...)) 는
# Mermaid 가 inner `()` 자체를 reject 하므로 sanitize 불가 — 그대로 둠.
_MERMAID_SHAPE_RES = [
    (re.compile(r"(\b[A-Za-z_][\w\-]*)\{\{([^{}\n]+?)\}\}"), "{{", "}}"),
    (re.compile(r"(\b[A-Za-z_][\w\-]*)\[([^\[\]\n]+?)\]"), "[", "]"),
    (re.compile(r"(\b[A-Za-z_][\w\-]*)\{([^{}\n]+?)\}"), "{", "}"),
]
# Mermaid 11.x reserved keywords — 노드 ID 로 쓰면 parse 실패.
_MERMAID_RESERVED_IDS = {"end", "class", "subgraph", "style", "default", "linkStyle"}


def _sanitize_mermaid_flowchart(code: str) -> str:
    """LLM 이 생성한 Mermaid 코드를 11.x parser 친화적으로 정리.

    - ```mermaid 펜스 제거
    - %%{init: ...}%% 테마 directive 제거 (LLM 이 잘못 inject 하는 경우 방어)
    - 라벨 안 특수문자 (``()`` / ``/`` / ``:`` 등) 가 있으면 double-quote 로 감쌈
    - 예약어 노드 ID 충돌 회피 (``end`` → ``endX`` 등)
    - flowchart directive 누락 시 ``flowchart TB`` 자동 prepend
    """
    if not code:
        return ""
    s = code.strip()
    if s.startswith("```"):
        s = s.strip("`")
        first_nl = s.find("\n")
        if first_nl > 0 and "mermaid" in s[:first_nl].lower():
            s = s[first_nl + 1:]
        s = s.rstrip("` \n")
    s = _MERMAID_DIRECTIVE_RE.sub("", s)

    risky_chars = set("()[]{}:;,/<>")

    def _make_wrap(open_b: str, close_b: str):
        def _wrap(m: re.Match) -> str:
            node, label = m.group(1), m.group(2)
            # 이미 quoted 면 skip (idempotent)
            stripped = label.strip()
            if stripped.startswith('"') and stripped.endswith('"'):
                return m.group(0)
            if any(c in label for c in risky_chars):
                safe = label.replace('"', "'")
                return f'{node}{open_b}"{safe}"{close_b}'
            return m.group(0)
        return _wrap

    for pat, open_b, close_b in _MERMAID_SHAPE_RES:
        s = pat.sub(_make_wrap(open_b, close_b), s)

    # 예약어 노드 ID 치환 — 라벨 시작 / 화살표 양옆 / 줄끝 직전 모두 cover.
    def _rename_reserved(text: str) -> str:
        for kw in _MERMAID_RESERVED_IDS:
            text = re.sub(
                rf"(?<![\w]){kw}(?=\s*[\[\(\{{]|\s*-->|\s*--|\s*$)",
                f"{kw}_", text, flags=re.MULTILINE,
            )
        return text
    s = _rename_reserved(s)

    # flowchart 헤더 보장
    lines = [ln for ln in s.splitlines() if ln.strip()]
    if not lines:
        return ""
    first = lines[0].strip().lower()
    if not (first.startswith("flowchart") or first.startswith("graph")):
        lines.insert(0, "flowchart TB")
    return "\n".join(lines)


def _render_flowchart(mermaid_code: str) -> str:
    """사용자 액션 흐름 — Mermaid flowchart. <pre class='mermaid'> 안에
    sanitize 한 코드. 페이지 JS 가 mermaid.render() 로 그리며 parse 실패 시
    raw 코드 + 에러를 그대로 노출 (디버깅용).
    LLM 이 ``flowchart_mermaid`` 빈 값 반환하면 섹션 자체 미생성.
    """
    code = _sanitize_mermaid_flowchart(mermaid_code or "")
    if not code:
        return ""
    return ("<section><h2>화면 흐름 (사용자 액션)</h2>"
            f"<pre class='mermaid'>{_esc(code)}</pre></section>")


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
        flowchart_block=_render_flowchart(layout.flowchart_mermaid),
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
