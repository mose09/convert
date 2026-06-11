"""capture-screens JSON → SVG 변환기.

Playwright 가 캡처한 DOM 트리 JSON (docs/FIGMA_JSON_SPEC.md 스키마) 을
SVG XML 로 1:1 변환. Figma 웹에 drag-drop / Ctrl+V 만으로 편집 가능한
vector 레이어로 import 가능 (데스크톱 앱 / 플러그인 불필요).

VLM 추측 0 — DOM 트리 그대로 옮기는 deterministic 변환.

공개 API: :func:`render_svg_from_capture`
"""
from __future__ import annotations

import json
from pathlib import Path

# textAlign → SVG text-anchor 매핑
_TEXT_ANCHOR = {
    "left": "start",
    "start": "start",
    "center": "middle",
    "right": "end",
    "end": "end",
    "justify": "start",
}


def _esc(s: str) -> str:
    """XML 속성 / 텍스트 노드 안전한 escape."""
    return (str(s)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


def _style_attrs(style: dict) -> str:
    """style dict → SVG fill/stroke/opacity 속성 문자열."""
    out: list[str] = []
    bg = style.get("background")
    if bg:
        out.append(f'fill="{_esc(bg)}"')
        opa = style.get("backgroundOpacity")
        if isinstance(opa, (int, float)) and opa < 1:
            out.append(f'fill-opacity="{opa}"')
    else:
        out.append('fill="none"')
    bc = style.get("borderColor")
    bw = style.get("borderWidth")
    if bc and bw:
        out.append(f'stroke="{_esc(bc)}" stroke-width="{bw}"')
    op = style.get("opacity")
    if isinstance(op, (int, float)) and op < 1:
        out.append(f'opacity="{op}"')
    return " ".join(out)


def _render_node(node: dict, parts: list[str], text_scale: float = 1.0) -> None:
    if not isinstance(node, dict):
        return
    kind = node.get("type")
    rect = node.get("rect") or {}
    x = int(rect.get("x", 0))
    y = int(rect.get("y", 0))
    w = int(rect.get("w", 0))
    h = int(rect.get("h", 0))
    style = node.get("style") or {}

    if kind == "TEXT":
        ts = node.get("text") or {}
        content = ts.get("content", "")
        if not content:
            return
        font = ts.get("fontFamily") or "sans-serif"
        size = int(ts.get("fontSize") or 14)
        # text_scale — Figma SVG import 가 <text> font-size 만 viewBox
        # 비율과 다르게 해석해서 작아지는 케이스 보정 (default 1.0 = 캡처
        # 값 그대로, 1.5~2.0 = Figma 가시성 보정).
        if text_scale and text_scale != 1.0:
            size = max(1, int(round(size * text_scale)))
        weight = int(ts.get("fontWeight") or 400)
        color = ts.get("color") or "#000000"
        align = ts.get("textAlign") or "left"
        anchor = _TEXT_ANCHOR.get(align, "start")
        if anchor == "middle":
            tx = x + w // 2
        elif anchor == "end":
            tx = x + w
        else:
            tx = x
        baseline = y + int(size * 0.85)
        parts.append(
            f'<text x="{tx}" y="{baseline}" '
            f'font-family="{_esc(font)}" font-size="{size}" '
            f'font-weight="{weight}" fill="{_esc(color)}" '
            f'text-anchor="{anchor}">{_esc(content)}</text>'
        )
        return

    if kind == "IMAGE":
        img = node.get("image") or {}
        b64 = img.get("base64", "")
        fmt = img.get("format", "png")
        if b64:
            parts.append(
                f'<image x="{x}" y="{y}" width="{w}" height="{h}" '
                f'href="data:image/{fmt};base64,{b64}"/>'
            )
        return

    visible = bool(style.get("background") or
                   (style.get("borderColor") and style.get("borderWidth")))
    if visible:
        radius = style.get("borderRadius") or 0
        radius_attr = f' rx="{int(radius)}"' if radius else ""
        attrs = _style_attrs(style)
        parts.append(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}"{radius_attr} '
            f'{attrs}/>'
        )

    children = node.get("children") or []
    if children:
        # FRAME 의 자식들을 <g> 로 감싸기 — Figma 가 Group 으로 인식해서
        # 레이어 트리 보존 (사용자 보고: paste 결과가 flat 평면이라
        # 컴포넌트 단위 선택/이동 어려움 fix). id 에 노드 이름 (tag#id.
        # class 형태) → Figma 가 layer 이름으로 사용.
        group_id = _esc(node.get("name") or "frame")
        parts.append(f'<g id="{group_id}">')
        for child in children:
            _render_node(child, parts, text_scale=text_scale)
        parts.append("</g>")


def render_svg_from_capture(doc: dict, output_path: Path,
                             text_scale: float = 1.0) -> int:
    """capture-screens 산출 JSON (1 화면) → SVG 파일 저장.

    text_scale: <text> font-size 보정 계수 (default 1.0). Figma 의 SVG
    import 가 텍스트만 작게 해석하는 케이스에 1.5~2.0 권장.

    Returns: SVG 요소 카운트 (`<rect>`/`<text>`/`<image>` 합).
    """
    meta = doc.get("meta") or {}
    root = doc.get("root") or {}
    vw = int((meta.get("viewport") or {}).get("w") or 1920)
    vh = int((meta.get("viewport") or {}).get("h") or 1080)
    title = meta.get("url") or meta.get("title") or "capture"

    parts: list[str] = []
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'xmlns:xlink="http://www.w3.org/1999/xlink" '
        f'viewBox="0 0 {vw} {vh}" width="{vw}" height="{vh}">'
    )
    parts.append(f'<title>{_esc(title)}</title>')
    parts.append(
        f'<rect x="0" y="0" width="{vw}" height="{vh}" fill="#ffffff"/>'
    )
    _render_node(root, parts, text_scale=text_scale)
    parts.append("</svg>")
    svg = "\n".join(parts)
    output_path.write_text(svg, encoding="utf-8")
    return svg.count("<rect ") + svg.count("<text ") + svg.count("<image ")


def convert_json_to_svg(json_path: Path, svg_path: Path | None = None,
                         text_scale: float = 1.0) -> Path:
    """capture-screens JSON 파일 경로 → SVG 파일 저장.

    svg_path 미지정 시 ``<stem>.svg`` 같은 디렉토리에 생성.
    """
    json_path = Path(json_path)
    if svg_path is None:
        svg_path = json_path.with_suffix(".svg")
    svg_path = Path(svg_path)
    doc = json.loads(json_path.read_text(encoding="utf-8"))
    render_svg_from_capture(doc, svg_path, text_scale=text_scale)
    return svg_path
