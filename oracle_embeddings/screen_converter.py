"""화면변환기 PoC — AS-IS 화면 캡처 → TO-BE PPTX (Vision LLM).

기존 자산 재사용:
 - ``legacy_pattern_discovery._call_llm(image_paths=...)`` : OpenAI 호환
   multimodal vision 호출 (base64 인코딩 + JSON 파싱 + 재시도 + raw dump
   인프라). 본 모듈은 system_prompt 만 주입해서 재사용.
 - ``python-pptx`` : TO-BE 슬라이드 도형/표/텍스트박스 렌더.

설계 원칙 (PoC):
 - 캐시 없음. 매번 VLM 호출.
 - 템플릿은 layout 추출용 비주얼 컨텍스트로만 사용 (별도 스타일 스펙
   추출 단계 없음). VLM 이 AS-IS 와 템플릿 캡처들을 동시에 보고 layout
   JSON 만 뽑는다.
 - 도형 스타일 고정 (검정 선/흰 배경/맑은 고딕). 색상/폰트의 템플릿
   반영은 PoC 동작 확인 후 후속.

모델 선택:
 - ``_call_llm`` 우선순위 (``PATTERN_LLM_MODEL`` > ``LLM_MODEL`` >
   config.yaml ``llm.model``) 를 그대로 따른다. 화면변환 전용 env 키는
   두지 않고 기존 키를 공유한다. vision 가능 모델을 쓰려면
   ``PATTERN_LLM_MODEL`` 또는 ``LLM_MODEL`` 을 Qwen3-VL/2.5-VL 등으로
   지정해야 한다.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from .legacy_pattern_discovery import _call_llm

logger = logging.getLogger(__name__)

_IMG_EXTS = (".png", ".jpg", ".jpeg")

_SYSTEM_PROMPT = (
    "당신은 레거시 화면 캡처 분석가입니다. 사용자가 제공한 첫 번째 이미지는 "
    "AS-IS 화면 캡처이고, 그 뒤의 이미지들은 TO-BE 디자인 템플릿 캡처입니다. "
    "AS-IS 의 비즈니스 의미는 유지하면서 템플릿의 시각 스타일을 따르는 "
    "TO-BE 레이아웃을 JSON 으로만 출력하세요. 다른 설명/마크다운/코멘트 금지."
)

_USER_PROMPT = """다음 스키마로 TO-BE 화면 레이아웃 JSON 을 출력해줘. 키:

{
  "page_title": "화면 타이틀 한 줄",
  "search_fields": [{"label": "조건명", "type": "text|select|date|checkbox"}],
  "table_columns": ["컬럼1", "컬럼2"],
  "buttons": ["조회", "초기화", "저장"],
  "notes": "특이사항 1~2줄",
  "regions": {
    "title":         {"x": "<0~1>", "y": "<0~1>", "w": "<0~1>", "h": "<0~1>"},
    "search_panel":  {"x": "<0~1>", "y": "<0~1>", "w": "<0~1>", "h": "<0~1>", "cols": "<int>"},
    "table":         {"x": "<0~1>", "y": "<0~1>", "w": "<0~1>", "h": "<0~1>"},
    "buttons":       {"x": "<0~1>", "y": "<0~1>", "w": "<0~1>", "h": "<0~1>", "align": "left|center|right"},
    "notes":         {"x": "<0~1>", "y": "<0~1>", "w": "<0~1>", "h": "<0~1>"}
  }
}

규칙:
- AS-IS 에 없는 필드/버튼/컬럼은 만들지 마세요 (할루시네이션 금지).
- 모든 라벨은 한국어 원문 유지.
- 비어 있는 키는 빈 배열 [] / 빈 문자열 "" 로.
- JSON 객체 하나만 출력.
- **React 소스가 첨부된 경우 (프롬프트 하단에 `=== React 소스` 블록)**
  → 그 소스가 `search_fields` / `table_columns` / `buttons` 의 **정답**
  (JSX 의 `<input label=...>` / `<Column header=...>` / `<Button>...`
  텍스트 그대로 옮길 것). 이미지에서 다르게 보여도 소스를 우선.
  이미지는 `regions` (위치/크기) 와 `page_title` 추론에만 사용.
- 소스가 첨부되지 않으면 AS-IS 이미지에서 모든 항목 추출.

**순서 규칙 — 매우 중요**:
- `search_fields` 는 화면을 사람이 읽는 자연 순서 = **row-major
  (행 우선)** 로 나열할 것. 즉 같은 행에 있는 필드들을 **왼쪽 → 오른쪽**
  순서로 먼저 나열한 뒤, 다음 행으로 내려가서 같은 방식으로 나열.
  **세로(컬럼) 단위로 위→아래 먼저 훑는 column-major 순서는 절대 금지.**
- `table_columns` 는 표 헤더의 **왼쪽 → 오른쪽** 순서 그대로.
- `buttons` 는 화면에 보이는 **왼쪽 → 오른쪽** (같은 행이면)
  또는 위→아래 순서로 나열.

예시 — 검색 패널이 3열 × 2행 그리드로 다음과 같이 배치되어 있다면:

  [주문번호] [주문일자] [상태]
  [고객사  ] [품목    ] [금액]

올바른 출력 (row-major):
  ["주문번호", "주문일자", "상태", "고객사", "품목", "금액"]

잘못된 출력 (column-major, 사용 금지):
  ["주문번호", "고객사", "주문일자", "품목", "상태", "금액"]

**regions 규칙 — 위치/크기 정확도의 핵심**:
- 모든 좌표는 슬라이드 전체를 1.0 으로 한 **normalized 값 (0.0 ~ 1.0)**.
  top-left origin (x=0 가 왼쪽, y=0 가 위).
- `x`, `y` = 영역의 좌상단 모서리. `w`, `h` = 영역의 가로/세로 크기.
  스키마의 `<0~1>` 은 자리 표시일 뿐 — **반드시 템플릿 캡처를 직접 보고
  실제 관찰된 좌표값(float)을 넣어라**. 스키마 placeholder 를 그대로
  복사하면 안 됨.
- bbox 의 기준은 **TEMPLATE 캡처의 영역 위치/크기** (TO-BE 디자인).
  AS-IS 의 위치는 무시하고, 템플릿이 보여주는 레이아웃 비율을 따르세요.
- `search_panel.cols` = 패널 안 필드 그리드의 한 행에 들어가는 컬럼 수
  (보통 3 ~ 6). 필드를 몇 줄로 배치할지 결정.
- `buttons.align` = `"left"` | `"center"` | `"right"`. 템플릿 보고 결정.

**buttons.y 결정 규칙 — 매우 중요 (가장 많이 틀리는 부분)**:
- 버튼의 y 는 **템플릿에서 버튼 행이 실제로 위치한 자리** 를 따라가야
  한다. 화면 종류마다 다르다:
  * 조회/필터 버튼이 **검색 패널 위 또는 같은 행 오른쪽** 에 있으면
    → `buttons.y` 는 0.05 ~ 0.20 (작은 값, 화면 윗부분)
  * 조회 버튼이 **검색 패널 안 마지막 칸** 에 있으면
    → search_panel 의 y 범위 안에 위치 (보통 0.20 ~ 0.30)
  * 저장/삭제 등이 **표 아래** 에 있으면 → 0.80 ~ 0.95 (큰 값, 아랫부분)
- **절대 기본값 0.86 등 큰 y 를 자동으로 쓰지 마라.** 템플릿을 직접 보고
  실제 버튼 행의 세로 위치를 측정해 넣어라.

- 패널/테이블/버튼바가 겹치지 않도록 y 좌표 분리. 화면에 없는 영역은
  생략 가능 (예: 표가 없는 입력 폼은 `table` 키 자체를 빼도 됨)."""


# ── 입력 수집 ────────────────────────────────────────────────────────


def _list_images(folder: Path) -> list[Path]:
    if not folder.is_dir():
        raise SystemExit(f"폴더 없음 또는 디렉토리 아님: {folder}")
    return sorted(p for p in folder.iterdir()
                  if p.is_file() and p.suffix.lower() in _IMG_EXTS)


# ── 스타일 프로파일 ──────────────────────────────────────────────────

# 템플릿 미제공 또는 추출 실패 시 사용되는 기본값 — 기존 하드코드와 동일.
_DEFAULT_STYLE: dict[str, str] = {
    "primary_color": "#1F3A5F",          # 헤더/주요 강조
    "title_color": "#1F3A5F",
    "section_label_color": "#444444",
    "panel_bg": "#F5F7FA",
    "panel_border": "#CFD6E0",
    "input_bg": "#FFFFFF",
    "input_border": "#A0A8B4",
    "field_label_color": "#333333",
    "input_placeholder_color": "#999999",
    "table_header_bg": "#1F3A5F",
    "table_header_text": "#FFFFFF",
    "table_row_bg": "#FFFFFF",
    "button_bg": "#1F3A5F",
    "button_text": "#FFFFFF",
    "button_shape": "rounded",           # rounded | square
    "font_family": "맑은 고딕",
    "notes_color": "#555555",
}

_STYLE_SYSTEM_PROMPT = (
    "당신은 UI 디자인 스타일 추출기입니다. 첨부된 디자인 템플릿 캡처 "
    "이미지들을 보고 그 시각 스타일 (색·폰트·버튼 모양) 을 JSON 으로만 "
    "출력합니다. 설명/마크다운/코멘트 없이 JSON 객체 하나만."
)

_STYLE_PROMPT = """첨부된 템플릿 캡처들의 시각 스타일을 다음 스키마로 추출.
모든 화면에 일관 적용할 거니까 가장 빈번한/대표적인 값을 선택.

스키마 (모든 색은 "#RRGGBB" 6자리 hex):
{
  "primary_color":          "#RRGGBB",   // 강조 (헤더/주요 버튼)
  "title_color":            "#RRGGBB",   // 페이지 타이틀 글자
  "section_label_color":    "#RRGGBB",   // "검색 조건" 등 섹션 라벨
  "panel_bg":               "#RRGGBB",   // 검색/입력 패널 배경
  "panel_border":           "#RRGGBB",   // 패널 보더
  "input_bg":               "#RRGGBB",   // 입력 박스 배경
  "input_border":           "#RRGGBB",   // 입력 박스 보더
  "field_label_color":      "#RRGGBB",   // 필드 라벨 글자
  "input_placeholder_color":"#RRGGBB",   // placeholder/타입 힌트 글자
  "table_header_bg":        "#RRGGBB",   // 표 헤더 배경
  "table_header_text":      "#RRGGBB",   // 표 헤더 글자
  "table_row_bg":           "#RRGGBB",   // 표 본문 행 배경
  "button_bg":              "#RRGGBB",   // 버튼 배경
  "button_text":            "#RRGGBB",   // 버튼 글자
  "button_shape":           "rounded|square",  // 버튼 모서리
  "font_family":            "<폰트명>",  // 본문 폰트 (한글 화면이면 맑은 고딕/나눔고딕 등)
  "notes_color":            "#RRGGBB"    // 노트/캡션 글자
}

규칙:
- 정확한 hex 색을 자신할 수 없으면 가장 가까운 추정치라도 출력.
- 모르는 키는 생략 가능 (생략된 키는 기본값 사용).
- JSON 객체 하나만, 설명 없이."""


def _parse_hex_color(s, default_rgb: tuple[int, int, int]):
    """`#RRGGBB` → RGBColor. 파싱 실패 시 default_rgb 로 fallback."""
    from pptx.dml.color import RGBColor
    if isinstance(s, str):
        cleaned = s.strip().lstrip("#")
        if len(cleaned) == 6:
            try:
                r = int(cleaned[0:2], 16)
                g = int(cleaned[2:4], 16)
                b = int(cleaned[4:6], 16)
                return RGBColor(r, g, b)
            except ValueError:
                pass
    return RGBColor(*default_rgb)


def _hex_to_rgb_tuple(s: str) -> tuple[int, int, int]:
    """Default RGB 추출용 헬퍼 (테스트용)."""
    cleaned = s.lstrip("#")
    return (int(cleaned[0:2], 16), int(cleaned[2:4], 16), int(cleaned[4:6], 16))


def _resolve_style(style: dict | None) -> dict:
    """사용자 style 과 기본값 병합. 빈/None 이면 전부 기본값."""
    merged = dict(_DEFAULT_STYLE)
    if isinstance(style, dict):
        for k, v in style.items():
            if v is None:
                continue
            if isinstance(v, str) and not v.strip():
                continue
            merged[k] = v
    return merged


def extract_style_profile(template_images: list[Path], config: dict) -> dict:
    """템플릿 캡처들로부터 1회 VLM 호출 → style dict. 실패 시 빈 dict.

    convert() 시작 시 한 번만 호출. 화면별 추출과 분리되어 모든 슬라이드에
    일관 적용된다.
    """
    if not template_images:
        return {}
    print("  스타일 프로파일 추출 (템플릿 1회 분석)...")
    result = _call_llm(
        prompt=_STYLE_PROMPT,
        config=config,
        label="screen_style_profile",
        system_prompt=_STYLE_SYSTEM_PROMPT,
        image_paths=[str(t) for t in template_images],
    )
    if not isinstance(result, dict):
        logger.warning("style 응답이 dict 가 아님 — 기본 스타일 사용")
        return {}
    return result


# ── 소스 매칭 ────────────────────────────────────────────────────────

_SOURCE_EXTS = (".tsx", ".jsx", ".ts", ".js", ".vue")
_SOURCE_EXCLUDE_SEGMENTS = (
    "node_modules", "/dist/", "\\dist\\",
    "/build/", "\\build\\", "/.next/", "\\.next\\",
    "/coverage/", "/.git/", "/__tests__/",
    ".test.", ".spec.",               # 테스트 파일 매칭 회피
    ".stories.", ".story.",           # storybook
    ".d.ts",                          # 타입 선언 파일
)
_SOURCE_SCREEN_DIR_HINTS = ("/pages/", "/screens/", "/views/", "/routes/")
_SOURCE_MAX_CHARS = 8000          # VLM 컨텍스트 보호용 상한
_SOURCE_MIN_SCORE = 4             # 이 이상이어야 매칭으로 인정 (오매칭 방지)
_SOURCE_TOP_N_DIAG = 3            # 매칭 실패 시 진단용으로 보존할 상위 후보 수
_TOKEN_RE = re.compile(r"[^a-zA-Z0-9가-힣]+")


def _normalize_tokens(s: str) -> list[str]:
    """alphanumeric (+ 한글) 토큰을 소문자로 분리."""
    return [t for t in _TOKEN_RE.split(s.lower()) if t]


def _build_source_index(frontend_dir: Path | None) -> list[Path]:
    """frontend_dir 하위의 React/Vue/TS 파일을 1회 수집. 없으면 빈 리스트."""
    if frontend_dir is None or not frontend_dir.is_dir():
        return []
    files: list[Path] = []
    for ext in _SOURCE_EXTS:
        files.extend(frontend_dir.rglob(f"*{ext}"))
    out = []
    for p in files:
        sp = str(p).replace("\\", "/").lower()
        if any(seg.lower() in sp for seg in _SOURCE_EXCLUDE_SEGMENTS):
            continue
        out.append(p)
    return out


def _score_match(stem_tokens: list[str], cand: Path) -> int:
    """캡처 stem 토큰과 소스 파일 path/basename 의 매치 점수.
    - 전체 stem 이 basename 의 substring 이면 +10
    - 각 토큰이 basename 에 있으면 +3, path 에만 있으면 +1
    - 파일이 pages/screens/views/routes 하위면 +2 (화면 컴포넌트 우대)
    """
    if not stem_tokens:
        return 0
    base = cand.stem.lower()
    path_text = str(cand.parent).replace("\\", "/").lower() + "/" + base
    full = "".join(stem_tokens)
    score = 0
    if full and full in re.sub(_TOKEN_RE, "", base):
        score += 10
    for t in stem_tokens:
        if len(t) < 2:
            continue
        if t in base:
            score += 3
        elif t in path_text:
            score += 1
    if any(hint in path_text for hint in _SOURCE_SCREEN_DIR_HINTS):
        score += 2
    return score


def _rank_sources(asis_stem: str,
                  source_index: list[Path]) -> list[tuple[Path, int]]:
    """캡처 stem 에 대한 소스 후보 (path, score) 정렬 리스트 (점수 내림차순)."""
    stem_tokens = _normalize_tokens(asis_stem)
    if not stem_tokens or not source_index:
        return []
    scored = [(cand, _score_match(stem_tokens, cand)) for cand in source_index]
    scored = [(p, s) for p, s in scored if s > 0]
    scored.sort(key=lambda ps: -ps[1])
    return scored


def _match_source(asis_stem: str, source_index: list[Path],
                  mapping: dict[str, Path] | None = None
                  ) -> tuple[Path | None, list[tuple[Path, int]]]:
    """매핑 우선 → 휴리스틱 fallback. (best_path|None, top_candidates) 반환.

    - 명시 매핑(mapping[asis_stem]) 이 있으면 그걸 사용 (점수 무시).
    - 없으면 _rank_sources 의 top1 점수가 임계 이상이면 채택.
    - top_candidates 는 진단용 — 매칭 실패해도 상위 N개 보존.
    """
    if mapping and asis_stem in mapping:
        return mapping[asis_stem], []
    ranked = _rank_sources(asis_stem, source_index)
    top_n = ranked[:_SOURCE_TOP_N_DIAG]
    if not ranked:
        return None, top_n
    best_path, best_score = ranked[0]
    if best_score >= _SOURCE_MIN_SCORE:
        return best_path, top_n
    return None, top_n


def _load_source_mapping(mapping_path: Path | None,
                         frontend_dir: Path | None) -> dict[str, Path]:
    """수기 매핑 YAML 로드. {capture_stem: Path}.

    YAML 값이 절대 경로면 그대로, 상대 경로면 frontend_dir 기준으로
    resolve. 존재하지 않는 파일은 경고 후 스킵.
    """
    if mapping_path is None:
        return {}
    if not mapping_path.is_file():
        raise SystemExit(f"--source-mapping 파일 없음: {mapping_path}")
    try:
        import yaml
    except ImportError as e:
        raise SystemExit("PyYAML 필요 — `pip install pyyaml`") from e
    raw = yaml.safe_load(mapping_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise SystemExit(
            f"--source-mapping YAML 은 {{capture_stem: 경로}} dict 여야 함: "
            f"{mapping_path}"
        )
    out: dict[str, Path] = {}
    for stem, rel in raw.items():
        if rel is None:
            continue
        p = Path(str(rel))
        if not p.is_absolute() and frontend_dir is not None:
            p = frontend_dir / p
        if not p.is_file():
            logger.warning("매핑 파일 미존재 — 스킵: %s → %s", stem, p)
            continue
        out[str(stem)] = p
    return out


def _read_source_snippet(path: Path, max_chars: int = _SOURCE_MAX_CHARS) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        logger.warning("소스 읽기 실패 (%s): %s", path, e)
        return ""
    if len(text) > max_chars:
        text = text[:max_chars] + "\n/* ... (이하 생략) */"
    return text


# ── LLM 추출 ────────────────────────────────────────────────────────


def extract_layout(asis_image: Path, template_images: list[Path],
                   config: dict,
                   source_path: Path | None = None) -> dict:
    """VLM 1회 호출 → TO-BE 레이아웃 dict.

    source_path 가 주어지면 그 파일을 읽어 프롬프트 하단에 첨부
    (`=== React 소스 첨부 (<파일명>) ===` 블록). 모델에게 search/table/
    button 의 정답으로 사용하도록 안내. 실패 시 빈 dict 반환.
    """
    prompt = _USER_PROMPT
    if source_path is not None:
        snippet = _read_source_snippet(source_path)
        if snippet:
            rel = source_path.name
            prompt = (
                _USER_PROMPT
                + f"\n\n=== React 소스 첨부 ({rel}) ===\n"
                + "이 소스가 search_fields / table_columns / buttons 의 정답.\n"
                + "이미지는 regions (위치/크기) 와 page_title 추론에만 사용.\n\n"
                + snippet
            )
            print(f"    소스 첨부: {rel} ({len(snippet)} chars)")

    image_paths = [str(asis_image)] + [str(t) for t in template_images]
    result = _call_llm(
        prompt=prompt,
        config=config,
        label=f"screen_{asis_image.stem}",
        system_prompt=_SYSTEM_PROMPT,
        image_paths=image_paths,
    )
    if not isinstance(result, dict):
        logger.warning("LLM 응답이 dict 가 아님: %s — 빈 레이아웃으로 대체", asis_image.name)
        return {}
    return result


# ── PPTX 렌더 ────────────────────────────────────────────────────────


def _slide_dims_cm(prs) -> tuple[float, float]:
    """슬라이드 가로/세로 cm. EMU 가 (1cm = 360000 EMU) 정확."""
    from pptx.util import Cm, Emu
    one_cm = int(Cm(1))
    return (prs.slide_width / one_cm, prs.slide_height / one_cm)


def _clamp01(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return max(0.0, min(1.0, f))


def _resolve_bbox(region: dict | None, default_cm: tuple[float, float, float, float],
                  prs) -> tuple[float, float, float, float]:
    """region 의 normalized bbox → cm 튜플. 부재/오류 시 default_cm 그대로."""
    if not isinstance(region, dict):
        return default_cm
    x = _clamp01(region.get("x"))
    y = _clamp01(region.get("y"))
    w = _clamp01(region.get("w"))
    h = _clamp01(region.get("h"))
    if None in (x, y, w, h) or w <= 0 or h <= 0:
        return default_cm
    # clamp w/h so x+w, y+h stay within slide
    w = min(w, 1.0 - x)
    h = min(h, 1.0 - y)
    sw, sh = _slide_dims_cm(prs)
    return (x * sw, y * sh, w * sw, h * sh)


def _detect_slide_aspect_inches(template_image: Path | None) -> tuple[float, float]:
    """첫 템플릿 이미지의 가로:세로 비율 → 슬라이드 가로/세로 (inch).
    >= 1.5 → 16:9 (13.333 x 7.5), 미만 → 4:3 (10.0 x 7.5).
    이미지 없거나 읽기 실패면 16:9 기본.
    """
    if template_image is None or not Path(template_image).is_file():
        return (13.333, 7.5)
    try:
        from PIL import Image
        with Image.open(template_image) as img:
            w, h = img.size
        if h <= 0:
            return (13.333, 7.5)
        return (13.333, 7.5) if (w / h) >= 1.5 else (10.0, 7.5)
    except Exception:
        return (13.333, 7.5)


def _draw_title(slide, text: str, prs, region: dict | None = None,
                style: dict | None = None) -> None:
    from pptx.util import Cm, Pt
    style = _resolve_style(style)
    sw, _ = _slide_dims_cm(prs)
    default = (0.8, 0.6, sw - 1.6, 1.2)
    x, y, w, h = _resolve_bbox(region, default, prs)
    tb = slide.shapes.add_textbox(Cm(x), Cm(y), Cm(w), Cm(h))
    tf = tb.text_frame
    tf.word_wrap = True
    p = tf.paragraphs[0]
    p.text = text or "(제목 없음)"
    r = p.runs[0]
    r.font.size = Pt(14)
    r.font.bold = True
    r.font.name = style["font_family"]
    r.font.color.rgb = _parse_hex_color(style.get("title_color"), (0x1F, 0x3A, 0x5F))


def _draw_section_label(slide, text: str, x_cm: float, y_cm: float,
                        style: dict, w_cm: float = 6.0) -> None:
    from pptx.util import Cm, Pt
    tb = slide.shapes.add_textbox(Cm(x_cm), Cm(y_cm), Cm(w_cm), Cm(0.6))
    p = tb.text_frame.paragraphs[0]
    p.text = text
    r = p.runs[0]
    r.font.size = Pt(9)
    r.font.bold = True
    r.font.name = style["font_family"]
    r.font.color.rgb = _parse_hex_color(style.get("section_label_color"),
                                        (0x44, 0x44, 0x44))


def _draw_search(slide, fields: list[dict], prs,
                 region: dict | None = None,
                 fallback_top_cm: float = 2.2,
                 style: dict | None = None) -> float:
    """검색 패널. region 있으면 bbox 사용, 없으면 fallback_top_cm 기준 스택.
    반환: 다음 섹션이 사용할 누적 높이 (cm). region 사용 시 0.0."""
    from pptx.util import Cm, Pt
    from pptx.enum.shapes import MSO_SHAPE

    if not fields:
        return 0.0

    style = _resolve_style(style)
    sw, _ = _slide_dims_cm(prs)
    panel_bg = _parse_hex_color(style.get("panel_bg"), (0xF5, 0xF7, 0xFA))
    panel_border = _parse_hex_color(style.get("panel_border"), (0xCF, 0xD6, 0xE0))
    input_bg = _parse_hex_color(style.get("input_bg"), (0xFF, 0xFF, 0xFF))
    input_border = _parse_hex_color(style.get("input_border"), (0xA0, 0xA8, 0xB4))
    field_label_color = _parse_hex_color(style.get("field_label_color"),
                                         (0x33, 0x33, 0x33))
    input_placeholder_color = _parse_hex_color(
        style.get("input_placeholder_color"), (0x99, 0x99, 0x99))
    font_name = style["font_family"]

    if region:
        cols = max(1, int(region.get("cols") or 3))
        default = (0.8, fallback_top_cm + 0.7, sw - 1.6, 2.0)
        panel_x, panel_y, panel_w, panel_h = _resolve_bbox(region, default, prs)
        label_y = max(0.0, panel_y - 0.7)
        _draw_section_label(slide, "검색 조건", panel_x, label_y, style)
        advance = 0.0
    else:
        cols = 3
        rows = (len(fields) + cols - 1) // cols
        panel_x = 0.8
        panel_w = sw - 1.6
        cell_h = 1.0
        panel_h = rows * cell_h + 0.3
        _draw_section_label(slide, "검색 조건", panel_x, fallback_top_cm, style)
        panel_y = fallback_top_cm + 0.7
        advance = panel_h + 0.7

    rows = (len(fields) + cols - 1) // cols
    cell_w = panel_w / cols
    cell_h = panel_h / rows if rows > 0 else panel_h

    bg = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE,
                                Cm(panel_x), Cm(panel_y),
                                Cm(panel_w), Cm(panel_h))
    bg.fill.solid()
    bg.fill.fore_color.rgb = panel_bg
    bg.line.color.rgb = panel_border
    bg.line.width = Pt(0.5)
    if bg.has_text_frame:
        bg.text_frame.text = ""

    for idx, f in enumerate(fields):
        r_idx, c_idx = divmod(idx, cols)
        x = panel_x + c_idx * cell_w + 0.2
        y = panel_y + 0.15 + r_idx * cell_h
        usable_w = cell_w - 0.4
        lb = slide.shapes.add_textbox(Cm(x), Cm(y),
                                      Cm(usable_w * 0.4), Cm(0.6))
        lp = lb.text_frame.paragraphs[0]
        lp.text = (f.get("label") or "").strip() or "(라벨)"
        lp.runs[0].font.size = Pt(8)
        lp.runs[0].font.name = font_name
        lp.runs[0].font.color.rgb = field_label_color
        ib = slide.shapes.add_shape(
            MSO_SHAPE.RECTANGLE,
            Cm(x + usable_w * 0.4 + 0.1), Cm(y + 0.05),
            Cm(usable_w * 0.55), Cm(min(0.55, cell_h - 0.3)),
        )
        ib.fill.solid()
        ib.fill.fore_color.rgb = input_bg
        ib.line.color.rgb = input_border
        ib.line.width = Pt(0.5)
        tf = ib.text_frame
        tf.margin_left = Cm(0.1)
        tf.margin_right = Cm(0.1)
        tp = tf.paragraphs[0]
        tp.text = f"<{(f.get('type') or 'text').strip()}>"
        tp.runs[0].font.size = Pt(7)
        tp.runs[0].font.name = font_name
        tp.runs[0].font.color.rgb = input_placeholder_color

    return advance


def _draw_table(slide, columns: list[str], prs,
                region: dict | None = None,
                fallback_top_cm: float = 0.0,
                style: dict | None = None) -> float:
    """표 헤더 + 빈 행 3줄. region 있으면 bbox 사용, 없으면 fallback 스택."""
    from pptx.util import Cm, Pt

    if not columns:
        return 0.0

    style = _resolve_style(style)
    sw, _ = _slide_dims_cm(prs)
    header_bg = _parse_hex_color(style.get("table_header_bg"), (0x1F, 0x3A, 0x5F))
    header_text = _parse_hex_color(style.get("table_header_text"),
                                   (0xFF, 0xFF, 0xFF))
    row_bg = _parse_hex_color(style.get("table_row_bg"), (0xFF, 0xFF, 0xFF))
    font_name = style["font_family"]

    if region:
        default = (0.8, fallback_top_cm + 0.7, sw - 1.6, 5.0)
        tbl_x, tbl_y, tbl_w, tbl_h = _resolve_bbox(region, default, prs)
        label_y = max(0.0, tbl_y - 0.7)
        _draw_section_label(slide, "조회 결과", tbl_x, label_y, style)
        advance = 0.0
    else:
        n_rows_default = 4
        tbl_x = 0.8
        tbl_w = sw - 1.6
        tbl_h = 0.7 * n_rows_default
        _draw_section_label(slide, "조회 결과", tbl_x, fallback_top_cm, style)
        tbl_y = fallback_top_cm + 0.7
        advance = tbl_h + 0.7

    n_rows = 4  # header + 3 empty
    n_cols = max(len(columns), 1)

    shape = slide.shapes.add_table(n_rows, n_cols,
                                   Cm(tbl_x), Cm(tbl_y),
                                   Cm(tbl_w), Cm(tbl_h))
    table = shape.table

    for ci, col in enumerate(columns):
        cell = table.cell(0, ci)
        cell.text = str(col)
        cell.fill.solid()
        cell.fill.fore_color.rgb = header_bg
        for p in cell.text_frame.paragraphs:
            for r in p.runs:
                r.font.size = Pt(9)
                r.font.bold = True
                r.font.name = font_name
                r.font.color.rgb = header_text

    for ri in range(1, n_rows):
        for ci in range(n_cols):
            cell = table.cell(ri, ci)
            cell.text = ""
            cell.fill.solid()
            cell.fill.fore_color.rgb = row_bg

    return advance


def _draw_buttons(slide, buttons: list[str], prs,
                  region: dict | None = None,
                  fallback_top_cm: float = 0.0,
                  style: dict | None = None) -> float:
    from pptx.util import Cm, Pt
    from pptx.enum.shapes import MSO_SHAPE

    if not buttons:
        return 0.0

    style = _resolve_style(style)
    sw, _ = _slide_dims_cm(prs)
    btn_bg = _parse_hex_color(style.get("button_bg"), (0x1F, 0x3A, 0x5F))
    btn_text = _parse_hex_color(style.get("button_text"), (0xFF, 0xFF, 0xFF))
    font_name = style["font_family"]
    shape_kind = (style.get("button_shape") or "rounded").strip().lower()
    btn_shape_enum = (MSO_SHAPE.RECTANGLE if shape_kind == "square"
                      else MSO_SHAPE.ROUNDED_RECTANGLE)

    if region:
        default = (sw - 8.0, fallback_top_cm, 7.5, 0.8)
        bar_x, bar_y, bar_w, bar_h = _resolve_bbox(region, default, prs)
        align = (region.get("align") or "right").lower()
        advance = 0.0
    else:
        bar_x = 0.8
        bar_y = fallback_top_cm
        bar_w = sw - 1.6
        bar_h = 0.8
        align = "right"
        advance = bar_h + 0.3

    n = len(buttons)
    gap = 0.25
    btn_w = min(2.4, (bar_w - (n - 1) * gap) / max(n, 1))
    btn_w = max(btn_w, 1.5)
    btn_h = min(bar_h, 0.9)
    total_w = n * btn_w + (n - 1) * gap

    if align == "left":
        x0 = bar_x
    elif align == "center":
        x0 = bar_x + (bar_w - total_w) / 2
    else:  # right
        x0 = bar_x + bar_w - total_w

    y0 = bar_y + (bar_h - btn_h) / 2

    for i, label in enumerate(buttons):
        x = x0 + i * (btn_w + gap)
        shape = slide.shapes.add_shape(
            btn_shape_enum,
            Cm(x), Cm(y0), Cm(btn_w), Cm(btn_h),
        )
        shape.fill.solid()
        shape.fill.fore_color.rgb = btn_bg
        shape.line.color.rgb = btn_bg
        tf = shape.text_frame
        p = tf.paragraphs[0]
        from pptx.enum.text import PP_ALIGN
        p.alignment = PP_ALIGN.CENTER
        p.text = str(label)
        r = p.runs[0]
        r.font.size = Pt(9)
        r.font.bold = True
        r.font.name = font_name
        r.font.color.rgb = btn_text

    return advance


def _draw_notes(slide, text: str, prs,
                region: dict | None = None,
                fallback_top_cm: float = 0.0,
                style: dict | None = None) -> None:
    if not text:
        return
    from pptx.util import Cm, Pt
    style = _resolve_style(style)
    sw, _ = _slide_dims_cm(prs)
    default = (0.8, fallback_top_cm, sw - 1.6, 1.5)
    x, y, w, h = _resolve_bbox(region, default, prs)
    tb = slide.shapes.add_textbox(Cm(x), Cm(y), Cm(w), Cm(h))
    tf = tb.text_frame
    tf.word_wrap = True
    p = tf.paragraphs[0]
    p.text = f"※ {text}"
    r = p.runs[0]
    r.font.size = Pt(7)
    r.font.italic = True
    r.font.name = style["font_family"]
    r.font.color.rgb = _parse_hex_color(style.get("notes_color"),
                                        (0x55, 0x55, 0x55))


def render_pptx(layouts: list[tuple[str, dict]], output_path: Path,
                aspect_hint_image: Path | None = None,
                style: dict | None = None) -> None:
    """layout 리스트 → PPTX. 슬라이드당 1화면.

    aspect_hint_image 가 주어지면 그 이미지의 가로:세로 비율로 슬라이드
    크기를 설정 (16:9 or 4:3). 미지정 시 16:9 기본.
    """
    try:
        from pptx import Presentation
        from pptx.util import Inches
    except ImportError as e:
        raise SystemExit(
            "python-pptx 미설치. `pip install python-pptx` 또는 폐쇄망 wheel "
            "설치 (requirements.txt 주석 참고)"
        ) from e

    prs = Presentation()
    w_in, h_in = _detect_slide_aspect_inches(aspect_hint_image)
    prs.slide_width = Inches(w_in)
    prs.slide_height = Inches(h_in)
    blank_layout = prs.slide_layouts[6]

    style = _resolve_style(style)

    for screen_name, layout in layouts:
        slide = prs.slides.add_slide(blank_layout)
        regions = layout.get("regions") or {}

        title = (layout.get("page_title") or "").strip() or screen_name
        _draw_title(slide, title, prs, region=regions.get("title"),
                    style=style)

        # regions 가 있으면 모두 절대 좌표, 없으면 cursor 스택
        if regions:
            _draw_search(slide, layout.get("search_fields") or [], prs,
                         region=regions.get("search_panel"), style=style)
            _draw_table(slide, layout.get("table_columns") or [], prs,
                        region=regions.get("table"), style=style)
            _draw_buttons(slide, layout.get("buttons") or [], prs,
                          region=regions.get("buttons"), style=style)
            _draw_notes(slide, (layout.get("notes") or "").strip(), prs,
                        region=regions.get("notes"), style=style)
        else:
            cursor = 2.2
            cursor += _draw_search(slide, layout.get("search_fields") or [],
                                   prs, fallback_top_cm=cursor, style=style)
            cursor += _draw_table(slide, layout.get("table_columns") or [],
                                  prs, fallback_top_cm=cursor, style=style)
            cursor += _draw_buttons(slide, layout.get("buttons") or [],
                                    prs, fallback_top_cm=cursor, style=style)
            _draw_notes(slide, (layout.get("notes") or "").strip(),
                        prs, fallback_top_cm=cursor, style=style)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        prs.save(str(output_path))
    except PermissionError as e:
        raise SystemExit(
            f"PPTX 저장 실패 — {output_path}\n"
            f"  원인: 파일이 다른 프로그램(PowerPoint 등)에 열려있어 잠긴 상태.\n"
            f"  해결: 1) 해당 PowerPoint 창 닫고 재실행, 또는\n"
            f"        2) --output <다른경로.pptx> 로 다른 파일명 지정.\n"
            f"  상세: {e}"
        ) from e


# ── 파이프라인 엔트리 ───────────────────────────────────────────────


def _dump_layout(layout: dict, asis_image: Path, template_images: list[Path],
                 dump_dir: Path,
                 matched_source: Path | None = None,
                 source_candidates: list[tuple[Path, int]] | None = None
                 ) -> Path:
    """Persist the parsed VLM layout dict for post-hoc inspection.

    렌더링 결과가 기대와 다를 때 모델이 실제로 무엇을 추출했는지 확인
    하기 위한 디버그 산출물. ``_call_llm`` 의 raw text dump 는 JSON 파싱
    실패 때만 떨어지므로, 성공 케이스의 추출 결과는 여기서 따로 저장.
    """
    dump_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "asis_image": asis_image.name,
        "template_images": [t.name for t in template_images],
        "matched_source": str(matched_source) if matched_source else None,
        "source_match_candidates": [
            {"path": str(p), "score": s}
            for p, s in (source_candidates or [])
        ],
        "layout": layout,
    }
    path = dump_dir / f"{asis_image.stem}.json"
    path.write_text(
        json.dumps(record, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def convert(captures_dir: Path, templates_dir: Path,
            output_pptx: Path, config: dict,
            frontend_dir: Path | None = None,
            source_mapping_path: Path | None = None) -> dict[str, Any]:
    asis_imgs = _list_images(captures_dir)
    tmpl_imgs = _list_images(templates_dir)
    if not asis_imgs:
        raise SystemExit(f"AS-IS 캡처 없음 (png/jpg): {captures_dir}")
    if not tmpl_imgs:
        raise SystemExit(f"템플릿 캡처 없음 (png/jpg): {templates_dir}")

    print(f"  AS-IS 캡처 {len(asis_imgs)}장 / 템플릿 {len(tmpl_imgs)}장 참조")

    source_index = _build_source_index(frontend_dir)
    if frontend_dir is not None:
        if source_index:
            print(f"  프론트 소스 인덱싱: {frontend_dir} ({len(source_index)} 파일)")
        else:
            print(f"  ⚠ 프론트 소스 인덱스 비어있음: {frontend_dir}")

    mapping = _load_source_mapping(source_mapping_path, frontend_dir)
    if mapping:
        print(f"  수기 매핑 로드: {source_mapping_path} ({len(mapping)} 항목)")

    # 템플릿에서 1회 style profile 추출 → 모든 슬라이드에 일관 적용
    style_raw = extract_style_profile(tmpl_imgs, config)
    style = _resolve_style(style_raw)
    style_path = output_pptx.parent / "style_profile.json"
    style_path.parent.mkdir(parents=True, exist_ok=True)
    style_path.write_text(
        json.dumps({"extracted": style_raw, "resolved": style},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"    스타일 저장: {style_path.name} "
          f"({len(style_raw)} 키 추출, {len(style)} 키 적용)")

    dump_dir = output_pptx.parent / "llm_raw"

    layouts: list[tuple[str, dict]] = []
    fail = 0
    matched = 0
    matched_via_mapping = 0
    for img in asis_imgs:
        print(f"  → {img.name}")
        source_path, candidates = _match_source(img.stem, source_index,
                                                mapping=mapping)
        if source_path is not None:
            matched += 1
            if mapping and img.stem in mapping:
                matched_via_mapping += 1
        elif source_index:
            # 휴리스틱 매칭 실패 시 진단: 최고 점수 표시
            top_score = candidates[0][1] if candidates else 0
            print(f"    소스 매칭 실패 (최고 점수 {top_score}/{_SOURCE_MIN_SCORE}, "
                  f"상위 후보는 llm_raw/{img.stem}.json 의 "
                  f"source_match_candidates 참고)")
        layout = extract_layout(img, tmpl_imgs, config,
                                source_path=source_path)
        if not layout:
            fail += 1
        layouts.append((img.stem, layout))
        _dump_layout(layout, img, tmpl_imgs, dump_dir,
                     matched_source=source_path,
                     source_candidates=candidates)

    render_pptx(layouts, output_pptx,
                aspect_hint_image=tmpl_imgs[0] if tmpl_imgs else None,
                style=style)

    return {
        "total": len(asis_imgs),
        "templates": len(tmpl_imgs),
        "fail": fail,
        "source_matched": matched,
        "source_matched_via_mapping": matched_via_mapping,
        "source_indexed": len(source_index),
        "style_keys_extracted": len(style_raw),
        "pptx": str(output_pptx),
        "llm_raw_dir": str(dump_dir),
        "style_profile": str(style_path),
    }
