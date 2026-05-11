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
  "notes": "특이사항 1~2줄"
}

규칙:
- AS-IS 에 없는 필드/버튼/컬럼은 만들지 마세요 (할루시네이션 금지).
- 모든 라벨은 한국어 원문 유지.
- 비어 있는 키는 빈 배열 [] / 빈 문자열 "" 로.
- JSON 객체 하나만 출력."""


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


def _draw_title(slide, text: str, prs) -> None:
    from pptx.util import Cm, Pt
    from pptx.dml.color import RGBColor
    sw = prs.slide_width
    tb = slide.shapes.add_textbox(Cm(0.8), Cm(0.6), sw - Cm(1.6), Cm(1.2))
    tf = tb.text_frame
    tf.word_wrap = True
    p = tf.paragraphs[0]
    p.text = text or "(제목 없음)"
    r = p.runs[0]
    r.font.size = Pt(20)
    r.font.bold = True
    r.font.name = "맑은 고딕"
    r.font.color.rgb = RGBColor(0x1F, 0x3A, 0x5F)


def _draw_section_label(slide, text: str, top_cm: float) -> None:
    from pptx.util import Cm, Pt
    from pptx.dml.color import RGBColor
    tb = slide.shapes.add_textbox(Cm(0.8), Cm(top_cm), Cm(6), Cm(0.6))
    p = tb.text_frame.paragraphs[0]
    p.text = text
    r = p.runs[0]
    r.font.size = Pt(11)
    r.font.bold = True
    r.font.name = "맑은 고딕"
    r.font.color.rgb = RGBColor(0x44, 0x44, 0x44)


def _draw_search(slide, fields: list[dict], top_cm: float, prs) -> float:
    """검색 패널 — 라벨 + 박스 페어 그리드. 사용한 높이(cm) 반환."""
    from pptx.util import Cm, Pt, Emu
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.dml.color import RGBColor

    if not fields:
        return 0.0

    _draw_section_label(slide, "검색 조건", top_cm)
    top = top_cm + 0.7

    # 패널 배경
    sw_cm = prs.slide_width / Emu(Cm(1))
    panel_w = sw_cm - 1.6
    cols = 3
    rows = (len(fields) + cols - 1) // cols
    cell_w = panel_w / cols
    cell_h = 1.0
    panel_h = rows * cell_h + 0.3

    bg = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE,
                                Cm(0.8), Cm(top), Cm(panel_w), Cm(panel_h))
    bg.fill.solid()
    bg.fill.fore_color.rgb = RGBColor(0xF5, 0xF7, 0xFA)
    bg.line.color.rgb = RGBColor(0xCF, 0xD6, 0xE0)
    bg.line.width = Pt(0.5)
    if bg.has_text_frame:
        bg.text_frame.text = ""

    for idx, f in enumerate(fields):
        r, c = divmod(idx, cols)
        x = 0.8 + c * cell_w + 0.2
        y = top + 0.15 + r * cell_h
        # label
        lb = slide.shapes.add_textbox(Cm(x), Cm(y), Cm(cell_w * 0.35), Cm(0.6))
        lp = lb.text_frame.paragraphs[0]
        lp.text = (f.get("label") or "").strip() or "(라벨)"
        lp.runs[0].font.size = Pt(9)
        lp.runs[0].font.name = "맑은 고딕"
        lp.runs[0].font.color.rgb = RGBColor(0x33, 0x33, 0x33)
        # input box
        ib = slide.shapes.add_shape(
            MSO_SHAPE.RECTANGLE,
            Cm(x + cell_w * 0.36), Cm(y + 0.05),
            Cm(cell_w * 0.55), Cm(0.55),
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

    return panel_h + 0.7  # label + panel


def _draw_table(slide, columns: list[str], top_cm: float, prs) -> float:
    """그리드 헤더 + 빈 행 3줄. 사용한 높이(cm) 반환."""
    from pptx.util import Cm, Pt, Emu
    from pptx.dml.color import RGBColor

    if not columns:
        return 0.0

    _draw_section_label(slide, "조회 결과", top_cm)
    top = top_cm + 0.7

    sw_cm = prs.slide_width / Emu(Cm(1))
    table_w = sw_cm - 1.6
    n_rows = 4  # header + 3 empty
    n_cols = max(len(columns), 1)
    table_h = 0.7 * n_rows

    shape = slide.shapes.add_table(n_rows, n_cols,
                                   Cm(0.8), Cm(top),
                                   Cm(table_w), Cm(table_h))
    table = shape.table

    # header
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

    # body — 빈칸이지만 행 스타일만 유지
    for ri in range(1, n_rows):
        for ci in range(n_cols):
            cell = table.cell(ri, ci)
            cell.text = ""
            cell.fill.solid()
            cell.fill.fore_color.rgb = RGBColor(0xFF, 0xFF, 0xFF)

    return table_h + 0.7


def _draw_buttons(slide, buttons: list[str], top_cm: float, prs) -> float:
    from pptx.util import Cm, Pt, Emu
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.dml.color import RGBColor

    if not buttons:
        return 0.0

    btn_w = 2.4
    btn_h = 0.8
    gap = 0.25
    sw_cm = prs.slide_width / Emu(Cm(1))
    total_w = len(buttons) * btn_w + (len(buttons) - 1) * gap
    x0 = sw_cm - 0.8 - total_w  # 우측 정렬

    for i, label in enumerate(buttons):
        x = x0 + i * (btn_w + gap)
        shape = slide.shapes.add_shape(
            MSO_SHAPE.ROUNDED_RECTANGLE,
            Cm(x), Cm(top_cm), Cm(btn_w), Cm(btn_h),
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

    return btn_h + 0.3


def _draw_notes(slide, text: str, top_cm: float, prs) -> None:
    if not text:
        return
    from pptx.util import Cm, Pt, Emu
    from pptx.dml.color import RGBColor
    sw_cm = prs.slide_width / Emu(Cm(1))
    tb = slide.shapes.add_textbox(Cm(0.8), Cm(top_cm),
                                  Cm(sw_cm - 1.6), Cm(1.5))
    tf = tb.text_frame
    tf.word_wrap = True
    p = tf.paragraphs[0]
    p.text = f"※ {text}"
    r = p.runs[0]
    r.font.size = Pt(9)
    r.font.italic = True
    r.font.name = "맑은 고딕"
    r.font.color.rgb = RGBColor(0x55, 0x55, 0x55)


def render_pptx(layouts: list[tuple[str, dict]], output_path: Path) -> None:
    """layout 리스트 → PPTX. 슬라이드당 1화면."""
    try:
        from pptx import Presentation
    except ImportError as e:
        raise SystemExit(
            "python-pptx 미설치. `pip install python-pptx` 또는 폐쇄망 wheel "
            "설치 (requirements.txt 주석 참고)"
        ) from e

    prs = Presentation()  # 기본 4:3 (25.4cm x 19.05cm)
    blank_layout = prs.slide_layouts[6]

    for screen_name, layout in layouts:
        slide = prs.slides.add_slide(blank_layout)

        title = (layout.get("page_title") or "").strip() or screen_name
        _draw_title(slide, title, prs)

        cursor = 2.2
        cursor += _draw_search(slide, layout.get("search_fields") or [],
                               cursor, prs)
        cursor += _draw_table(slide, layout.get("table_columns") or [],
                              cursor, prs)
        cursor += _draw_buttons(slide, layout.get("buttons") or [],
                                cursor, prs)
        _draw_notes(slide, (layout.get("notes") or "").strip(), cursor, prs)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(output_path))


# ── 파이프라인 엔트리 ───────────────────────────────────────────────


def convert(captures_dir: Path, templates_dir: Path,
            output_pptx: Path, config: dict) -> dict[str, Any]:
    asis_imgs = _list_images(captures_dir)
    tmpl_imgs = _list_images(templates_dir)
    if not asis_imgs:
        raise SystemExit(f"AS-IS 캡처 없음 (png/jpg): {captures_dir}")
    if not tmpl_imgs:
        raise SystemExit(f"템플릿 캡처 없음 (png/jpg): {templates_dir}")

    print(f"  AS-IS 캡처 {len(asis_imgs)}장 / 템플릿 {len(tmpl_imgs)}장 참조")

    layouts: list[tuple[str, dict]] = []
    fail = 0
    for img in asis_imgs:
        print(f"  → {img.name}")
        layout = extract_layout(img, tmpl_imgs, config)
        if not layout:
            fail += 1
        layouts.append((img.stem, layout))

    render_pptx(layouts, output_pptx)

    return {
        "total": len(asis_imgs),
        "templates": len(tmpl_imgs),
        "fail": fail,
        "pptx": str(output_pptx),
    }
