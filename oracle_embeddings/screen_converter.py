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
    "title":         {"x": 0.03, "y": 0.03, "w": 0.94, "h": 0.07},
    "search_panel":  {"x": 0.03, "y": 0.12, "w": 0.94, "h": 0.18, "cols": 4},
    "table":         {"x": 0.03, "y": 0.34, "w": 0.94, "h": 0.50},
    "buttons":       {"x": 0.60, "y": 0.86, "w": 0.37, "h": 0.06, "align": "right"},
    "notes":         {"x": 0.03, "y": 0.93, "w": 0.94, "h": 0.05}
  }
}

규칙:
- AS-IS 에 없는 필드/버튼/컬럼은 만들지 마세요 (할루시네이션 금지).
- 모든 라벨은 한국어 원문 유지.
- 비어 있는 키는 빈 배열 [] / 빈 문자열 "" 로.
- JSON 객체 하나만 출력.

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
- bbox 의 기준은 **TEMPLATE 캡처의 영역 위치/크기** (TO-BE 디자인).
  AS-IS 의 위치는 무시하고, 템플릿이 보여주는 레이아웃 비율을 따르세요.
- `search_panel.cols` = 패널 안 필드 그리드의 한 행에 들어가는 컬럼 수
  (보통 3 ~ 6). 필드를 몇 줄로 배치할지 결정.
- `buttons.align` = `"left"` | `"center"` | `"right"`. 보통 오른쪽 정렬.
- 패널/테이블/버튼바가 겹치지 않도록 y 좌표 분리. 화면에 없는 영역은
  생략 가능 (예: 표가 없는 입력 폼은 `table` 키 자체를 빼도 됨).
- 모르겠으면 위 예시 값을 그대로 사용 (해당 영역이 실제로 존재할 때만)."""


# ── 입력 수집 ────────────────────────────────────────────────────────


def _list_images(folder: Path) -> list[Path]:
    if not folder.is_dir():
        raise SystemExit(f"폴더 없음 또는 디렉토리 아님: {folder}")
    return sorted(p for p in folder.iterdir()
                  if p.is_file() and p.suffix.lower() in _IMG_EXTS)


# ── LLM 추출 ────────────────────────────────────────────────────────


def extract_layout(asis_image: Path, template_images: list[Path],
                   config: dict) -> dict:
    """VLM 1회 호출 → TO-BE 레이아웃 dict. 실패 시 빈 dict 반환."""
    image_paths = [str(asis_image)] + [str(t) for t in template_images]
    result = _call_llm(
        prompt=_USER_PROMPT,
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


def _draw_title(slide, text: str, prs, region: dict | None = None) -> None:
    from pptx.util import Cm, Pt
    from pptx.dml.color import RGBColor
    sw, _ = _slide_dims_cm(prs)
    default = (0.8, 0.6, sw - 1.6, 1.2)
    x, y, w, h = _resolve_bbox(region, default, prs)
    tb = slide.shapes.add_textbox(Cm(x), Cm(y), Cm(w), Cm(h))
    tf = tb.text_frame
    tf.word_wrap = True
    p = tf.paragraphs[0]
    p.text = text or "(제목 없음)"
    r = p.runs[0]
    r.font.size = Pt(20)
    r.font.bold = True
    r.font.name = "맑은 고딕"
    r.font.color.rgb = RGBColor(0x1F, 0x3A, 0x5F)


def _draw_section_label(slide, text: str, x_cm: float, y_cm: float,
                        w_cm: float = 6.0) -> None:
    from pptx.util import Cm, Pt
    from pptx.dml.color import RGBColor
    tb = slide.shapes.add_textbox(Cm(x_cm), Cm(y_cm), Cm(w_cm), Cm(0.6))
    p = tb.text_frame.paragraphs[0]
    p.text = text
    r = p.runs[0]
    r.font.size = Pt(11)
    r.font.bold = True
    r.font.name = "맑은 고딕"
    r.font.color.rgb = RGBColor(0x44, 0x44, 0x44)


def _draw_search(slide, fields: list[dict], prs,
                 region: dict | None = None,
                 fallback_top_cm: float = 2.2) -> float:
    """검색 패널. region 있으면 bbox 사용, 없으면 fallback_top_cm 기준 스택.
    반환: 다음 섹션이 사용할 누적 높이 (cm). region 사용 시 0.0."""
    from pptx.util import Cm, Pt
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.dml.color import RGBColor

    if not fields:
        return 0.0

    sw, _ = _slide_dims_cm(prs)

    if region:
        cols = max(1, int(region.get("cols") or 3))
        # bbox 가 패널 자체 (label 은 패널 위 약 0.7cm 자리 차지)
        default = (0.8, fallback_top_cm + 0.7, sw - 1.6, 2.0)
        panel_x, panel_y, panel_w, panel_h = _resolve_bbox(region, default, prs)
        # label 은 패널 위 0.7cm 자리에
        label_y = max(0.0, panel_y - 0.7)
        _draw_section_label(slide, "검색 조건", panel_x, label_y)
        advance = 0.0
    else:
        cols = 3
        rows = (len(fields) + cols - 1) // cols
        panel_x = 0.8
        panel_w = sw - 1.6
        cell_h = 1.0
        panel_h = rows * cell_h + 0.3
        _draw_section_label(slide, "검색 조건", panel_x, fallback_top_cm)
        panel_y = fallback_top_cm + 0.7
        advance = panel_h + 0.7

    rows = (len(fields) + cols - 1) // cols
    cell_w = panel_w / cols
    cell_h = panel_h / rows if rows > 0 else panel_h

    bg = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE,
                                Cm(panel_x), Cm(panel_y),
                                Cm(panel_w), Cm(panel_h))
    bg.fill.solid()
    bg.fill.fore_color.rgb = RGBColor(0xF5, 0xF7, 0xFA)
    bg.line.color.rgb = RGBColor(0xCF, 0xD6, 0xE0)
    bg.line.width = Pt(0.5)
    if bg.has_text_frame:
        bg.text_frame.text = ""

    for idx, f in enumerate(fields):
        r_idx, c_idx = divmod(idx, cols)
        x = panel_x + c_idx * cell_w + 0.2
        y = panel_y + 0.15 + r_idx * cell_h
        usable_w = cell_w - 0.4
        # label
        lb = slide.shapes.add_textbox(Cm(x), Cm(y),
                                      Cm(usable_w * 0.4), Cm(0.6))
        lp = lb.text_frame.paragraphs[0]
        lp.text = (f.get("label") or "").strip() or "(라벨)"
        lp.runs[0].font.size = Pt(9)
        lp.runs[0].font.name = "맑은 고딕"
        lp.runs[0].font.color.rgb = RGBColor(0x33, 0x33, 0x33)
        # input box
        ib = slide.shapes.add_shape(
            MSO_SHAPE.RECTANGLE,
            Cm(x + usable_w * 0.4 + 0.1), Cm(y + 0.05),
            Cm(usable_w * 0.55), Cm(min(0.55, cell_h - 0.3)),
        )
        ib.fill.solid()
        ib.fill.fore_color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
        ib.line.color.rgb = RGBColor(0xA0, 0xA8, 0xB4)
        ib.line.width = Pt(0.5)
        tf = ib.text_frame
        tf.margin_left = Cm(0.1)
        tf.margin_right = Cm(0.1)
        tp = tf.paragraphs[0]
        tp.text = f"<{(f.get('type') or 'text').strip()}>"
        tp.runs[0].font.size = Pt(8)
        tp.runs[0].font.name = "맑은 고딕"
        tp.runs[0].font.color.rgb = RGBColor(0x99, 0x99, 0x99)

    return advance


def _draw_table(slide, columns: list[str], prs,
                region: dict | None = None,
                fallback_top_cm: float = 0.0) -> float:
    """표 헤더 + 빈 행 3줄. region 있으면 bbox 사용, 없으면 fallback 스택."""
    from pptx.util import Cm, Pt
    from pptx.dml.color import RGBColor

    if not columns:
        return 0.0

    sw, _ = _slide_dims_cm(prs)

    if region:
        default = (0.8, fallback_top_cm + 0.7, sw - 1.6, 5.0)
        tbl_x, tbl_y, tbl_w, tbl_h = _resolve_bbox(region, default, prs)
        label_y = max(0.0, tbl_y - 0.7)
        _draw_section_label(slide, "조회 결과", tbl_x, label_y)
        advance = 0.0
    else:
        n_rows_default = 4
        tbl_x = 0.8
        tbl_w = sw - 1.6
        tbl_h = 0.7 * n_rows_default
        _draw_section_label(slide, "조회 결과", tbl_x, fallback_top_cm)
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
        cell.fill.fore_color.rgb = RGBColor(0x1F, 0x3A, 0x5F)
        for p in cell.text_frame.paragraphs:
            for r in p.runs:
                r.font.size = Pt(10)
                r.font.bold = True
                r.font.name = "맑은 고딕"
                r.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)

    for ri in range(1, n_rows):
        for ci in range(n_cols):
            cell = table.cell(ri, ci)
            cell.text = ""
            cell.fill.solid()
            cell.fill.fore_color.rgb = RGBColor(0xFF, 0xFF, 0xFF)

    return advance


def _draw_buttons(slide, buttons: list[str], prs,
                  region: dict | None = None,
                  fallback_top_cm: float = 0.0) -> float:
    from pptx.util import Cm, Pt
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.dml.color import RGBColor

    if not buttons:
        return 0.0

    sw, _ = _slide_dims_cm(prs)

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
    # button width = use bar_h * 3 (가로:세로 ≈ 3:1) but clamp to bar_w
    btn_w = min(2.4, (bar_w - (n - 1) * gap) / max(n, 1))
    btn_w = max(btn_w, 1.5)  # 최소 가로
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
            MSO_SHAPE.ROUNDED_RECTANGLE,
            Cm(x), Cm(y0), Cm(btn_w), Cm(btn_h),
        )
        shape.fill.solid()
        shape.fill.fore_color.rgb = RGBColor(0x1F, 0x3A, 0x5F)
        shape.line.color.rgb = RGBColor(0x1F, 0x3A, 0x5F)
        tf = shape.text_frame
        p = tf.paragraphs[0]
        from pptx.enum.text import PP_ALIGN
        p.alignment = PP_ALIGN.CENTER
        p.text = str(label)
        r = p.runs[0]
        r.font.size = Pt(11)
        r.font.bold = True
        r.font.name = "맑은 고딕"
        r.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)

    return advance


def _draw_notes(slide, text: str, prs,
                region: dict | None = None,
                fallback_top_cm: float = 0.0) -> None:
    if not text:
        return
    from pptx.util import Cm, Pt
    from pptx.dml.color import RGBColor
    sw, _ = _slide_dims_cm(prs)
    default = (0.8, fallback_top_cm, sw - 1.6, 1.5)
    x, y, w, h = _resolve_bbox(region, default, prs)
    tb = slide.shapes.add_textbox(Cm(x), Cm(y), Cm(w), Cm(h))
    tf = tb.text_frame
    tf.word_wrap = True
    p = tf.paragraphs[0]
    p.text = f"※ {text}"
    r = p.runs[0]
    r.font.size = Pt(9)
    r.font.italic = True
    r.font.name = "맑은 고딕"
    r.font.color.rgb = RGBColor(0x55, 0x55, 0x55)


def render_pptx(layouts: list[tuple[str, dict]], output_path: Path,
                aspect_hint_image: Path | None = None) -> None:
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

    for screen_name, layout in layouts:
        slide = prs.slides.add_slide(blank_layout)
        regions = layout.get("regions") or {}

        title = (layout.get("page_title") or "").strip() or screen_name
        _draw_title(slide, title, prs, region=regions.get("title"))

        # regions 가 있으면 모두 절대 좌표, 없으면 cursor 스택
        if regions:
            _draw_search(slide, layout.get("search_fields") or [], prs,
                         region=regions.get("search_panel"))
            _draw_table(slide, layout.get("table_columns") or [], prs,
                        region=regions.get("table"))
            _draw_buttons(slide, layout.get("buttons") or [], prs,
                          region=regions.get("buttons"))
            _draw_notes(slide, (layout.get("notes") or "").strip(), prs,
                        region=regions.get("notes"))
        else:
            cursor = 2.2
            cursor += _draw_search(slide, layout.get("search_fields") or [],
                                   prs, fallback_top_cm=cursor)
            cursor += _draw_table(slide, layout.get("table_columns") or [],
                                  prs, fallback_top_cm=cursor)
            cursor += _draw_buttons(slide, layout.get("buttons") or [],
                                    prs, fallback_top_cm=cursor)
            _draw_notes(slide, (layout.get("notes") or "").strip(),
                        prs, fallback_top_cm=cursor)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(output_path))


# ── 파이프라인 엔트리 ───────────────────────────────────────────────


def _dump_layout(layout: dict, asis_image: Path, template_images: list[Path],
                 dump_dir: Path) -> Path:
    """Persist the parsed VLM layout dict for post-hoc inspection.

    렌더링 결과가 기대와 다를 때 모델이 실제로 무엇을 추출했는지 확인
    하기 위한 디버그 산출물. ``_call_llm`` 의 raw text dump 는 JSON 파싱
    실패 때만 떨어지므로, 성공 케이스의 추출 결과는 여기서 따로 저장.
    """
    dump_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "asis_image": asis_image.name,
        "template_images": [t.name for t in template_images],
        "layout": layout,
    }
    path = dump_dir / f"{asis_image.stem}.json"
    path.write_text(
        json.dumps(record, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def convert(captures_dir: Path, templates_dir: Path,
            output_pptx: Path, config: dict) -> dict[str, Any]:
    asis_imgs = _list_images(captures_dir)
    tmpl_imgs = _list_images(templates_dir)
    if not asis_imgs:
        raise SystemExit(f"AS-IS 캡처 없음 (png/jpg): {captures_dir}")
    if not tmpl_imgs:
        raise SystemExit(f"템플릿 캡처 없음 (png/jpg): {templates_dir}")

    print(f"  AS-IS 캡처 {len(asis_imgs)}장 / 템플릿 {len(tmpl_imgs)}장 참조")

    dump_dir = output_pptx.parent / "llm_raw"

    layouts: list[tuple[str, dict]] = []
    fail = 0
    for img in asis_imgs:
        print(f"  → {img.name}")
        layout = extract_layout(img, tmpl_imgs, config)
        if not layout:
            fail += 1
        layouts.append((img.stem, layout))
        _dump_layout(layout, img, tmpl_imgs, dump_dir)

    render_pptx(layouts, output_pptx,
                aspect_hint_image=tmpl_imgs[0] if tmpl_imgs else None)

    return {
        "total": len(asis_imgs),
        "templates": len(tmpl_imgs),
        "fail": fail,
        "pptx": str(output_pptx),
        "llm_raw_dir": str(dump_dir),
    }
