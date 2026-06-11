#!/usr/bin/env python3
"""capture-screens 회귀 검증 스크립트.

mock 정적 HTML 2장 (/tmp/mock_capture — 한글 텍스트 + 그리드 + 버튼 +
이미지) 을 http.server 로 띄우고 capture_screens 실행 후 JSON 노드 수 /
TEXT 노드 수 / IMAGE 노드 수가 기대 범위인지 assert.

usage:
  PLAYWRIGHT_BROWSERS_PATH=<번들경로> python verify_capture.py

mock 자산이 없으면 자동 생성. 통과 시 1줄 ✓, 실패 시 assert 메시지.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

MOCK_DIR = Path("/tmp/mock_capture")
OUT_DIR = Path("/tmp/figma_capture_verify")
PORT = 8741

_ORDER_LIST_HTML = """<!DOCTYPE html>
<html lang="ko">
<head><meta charset="utf-8"><title>주문 목록</title>
<style>
  body { margin: 0; font-family: sans-serif; }
  .header { background: #1f3a5f; color: #fff; padding: 16px; }
  .search { background: #f5f7fa; border: 1px solid #cfd6e0; padding: 12px; margin: 12px; }
  .search label { font-size: 13px; color: #333; }
  .search input { border: 1px solid #a0a8b4; width: 180px; height: 28px; }
  table { border-collapse: collapse; margin: 12px; width: 600px; }
  th { background: #1f3a5f; color: white; padding: 8px; font-size: 13px; }
  td { border: 1px solid #ddd; padding: 8px; font-size: 13px; }
  .btn { background: #1f3a5f; color: #fff; border: 0; border-radius: 4px; padding: 8px 20px; }
  .hidden-el { display: none; }
</style>
</head>
<body>
  <div class="header"><h1>주문 목록</h1></div>
  <div class="search">
    <label>주문번호</label> <input type="text"/>
    <label>주문일자</label> <input type="text"/>
    <button class="btn">조회</button>
  </div>
  <table>
    <tr><th>주문번호</th><th>고객명</th><th>금액</th></tr>
    <tr><td>ORD-001</td><td>홍길동</td><td>15,000</td></tr>
  </table>
  <div class="hidden-el">안 보여야 함</div>
</body>
</html>
"""

_WITH_IMAGE_HTML = """<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8"><title>이미지 테스트</title></head>
<body>
  <h1>로고 화면</h1>
  <img src="logo.png" width="64" height="64" alt="logo"/>
</body></html>
"""

# 미니멀 8x8 red PNG (base64)
_LOGO_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAYAAADED76LAAAAFklEQVR4nGP8z8DwnwEPYMIn"
    "OUIUAACfkAH/wjqLnQAAAABJRU5ErkJggg=="
)


def _ensure_mock_assets():
    MOCK_DIR.mkdir(parents=True, exist_ok=True)
    (MOCK_DIR / "order_list.html").write_text(_ORDER_LIST_HTML, encoding="utf-8")
    (MOCK_DIR / "with_image.html").write_text(_WITH_IMAGE_HTML, encoding="utf-8")
    import base64
    (MOCK_DIR / "logo.png").write_bytes(base64.b64decode(_LOGO_PNG_B64))


def _walk_counts(node, acc):
    acc[node["type"]] = acc.get(node["type"], 0) + 1
    for c in node.get("children") or []:
        _walk_counts(c, acc)
    return acc


def main() -> int:
    _ensure_mock_assets()

    server = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(PORT)],
        cwd=str(MOCK_DIR),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(1.0)
        from oracle_embeddings.legacy_screen_capture import capture_screens

        summary = capture_screens(
            f"http://localhost:{PORT}",
            ["/order_list.html", "/with_image.html"],
            OUT_DIR,
            viewport=(1280, 800),
        )
        assert summary.captured == 2, f"captured={summary.captured} (기대 2)"

        # 화면 1 — 주문 목록: TEXT 풍부 (10±), 한글 보존, hidden 제외
        doc1 = json.loads(
            (OUT_DIR / "order_list.html.json").read_text(encoding="utf-8"))
        assert doc1["schemaVersion"] == 1
        c1 = _walk_counts(doc1["root"], {})
        total1 = sum(c1.values())
        assert 15 <= total1 <= 60, f"화면1 노드 수 {total1} 기대범위(15~60) 밖"
        assert c1.get("TEXT", 0) >= 8, f"화면1 TEXT={c1.get('TEXT')} (기대 ≥8)"

        texts = []
        def _collect(n):
            if n["type"] == "TEXT":
                texts.append(n["text"]["content"])
            for c in n.get("children") or []:
                _collect(c)
        _collect(doc1["root"])
        joined = " ".join(texts)
        assert "주문 목록" in joined, "한글 타이틀 누락"
        assert "안 보여야 함" not in joined, "display:none 제외 실패"

        # 화면 2 — 이미지: IMAGE 노드 ≥1
        doc2 = json.loads(
            (OUT_DIR / "with_image.html.json").read_text(encoding="utf-8"))
        c2 = _walk_counts(doc2["root"], {})
        assert c2.get("IMAGE", 0) >= 1, f"화면2 IMAGE={c2.get('IMAGE')} (기대 ≥1)"

        print(f"✓ verify_capture PASS — 화면1 {total1}노드 "
              f"(TEXT {c1.get('TEXT')}), 화면2 IMAGE {c2.get('IMAGE')}")
        return 0
    finally:
        server.terminate()
        server.wait(timeout=5)


if __name__ == "__main__":
    sys.exit(main())
