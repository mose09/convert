/**
 * capture-screens Figma 플러그인 — JSON → Figma 노드 렌더러.
 *
 * 입력: docs/FIGMA_JSON_SPEC.md 스키마 (schemaVersion 1).
 * ui.html 이 postMessage 로 docs(JSON 배열) 전달 → 각 doc 을
 * 루트 프레임 1개로 렌더.
 *
 * 외부 의존성 0 (순수 JS — Figma Plugin API 만 사용). 폐쇄망 안전.
 */
"use strict";

var SUPPORTED_SCHEMA_VERSION = 1;

figma.showUI(__html__, { width: 420, height: 380 });

figma.ui.onmessage = function (msg) {
  if (!msg || msg.type !== "import") return;
  importDocs(msg.docs || []);
};

async function importDocs(docs) {
  try {
    var totalNodes = 0;
    var fontFallbacks = 0;
    var skipped = 0;
    var offsetX = 0;

    for (var i = 0; i < docs.length; i++) {
      var doc = docs[i];
      // schemaVersion 검증
      if (!doc || doc.schemaVersion !== SUPPORTED_SCHEMA_VERSION) {
        figma.ui.postMessage({
          type: "error",
          text: "지원하지 않는 schemaVersion: " +
            (doc && doc.schemaVersion) + " (지원: " +
            SUPPORTED_SCHEMA_VERSION + ")",
        });
        return;
      }
      if (!doc.root || !doc.meta) {
        figma.ui.postMessage({
          type: "error",
          text: "root / meta 필드 누락 (" + (i + 1) + "번째 문서)",
        });
        return;
      }

      figma.ui.postMessage({
        type: "progress",
        text: (i + 1) + "/" + docs.length + " 렌더 중: " +
          (doc.meta.url || "?"),
      });

      // 노드 수 상한 경고 — 대형 화면 Figma freeze 사전 확인
      var estimate = countNodes(doc.root);
      if (estimate > 3000) {
        figma.ui.postMessage({
          type: "progress",
          text: "⚠ " + estimate + " 노드 — 큰 화면이라 렌더에 시간이 " +
            "걸릴 수 있습니다 (" + (doc.meta.url || "?") + ")",
        });
      }

      var stats = await renderDoc(doc, offsetX);
      totalNodes += stats.nodes;
      fontFallbacks += stats.fontFallbacks;
      skipped += stats.skipped;
      offsetX += (doc.meta.viewport && doc.meta.viewport.w
        ? doc.meta.viewport.w : 1920) + 100; // 화면 간 100px 간격
    }

    var summary = totalNodes + " nodes, " + fontFallbacks +
      " font fallbacks" + (skipped ? ", " + skipped + " skipped" : "");
    figma.notify(summary);
    figma.ui.postMessage({ type: "done", text: "완료 — " + summary });
  } catch (e) {
    figma.ui.postMessage({ type: "error", text: String(e && e.message || e) });
  }
}

// ── 폰트 매핑 ──────────────────────────────────────────────────────
// 캡처 환경 (Windows 한글) 폰트 → Figma 에 흔히 있는 폰트.
// 미존재 시 Inter Regular 폴백 + 카운트.
var FONT_MAP = {
  "Malgun Gothic":      { family: "Noto Sans KR", style: "Regular" },
  "맑은 고딕":           { family: "Noto Sans KR", style: "Regular" },
  "Apple SD Gothic Neo": { family: "Noto Sans KR", style: "Regular" },
  "Nanum Gothic":       { family: "Noto Sans KR", style: "Regular" },
  "나눔고딕":            { family: "Noto Sans KR", style: "Regular" },
  "Gulim":              { family: "Noto Sans KR", style: "Regular" },
  "굴림":               { family: "Noto Sans KR", style: "Regular" },
  "Dotum":              { family: "Noto Sans KR", style: "Regular" },
  "돋움":               { family: "Noto Sans KR", style: "Regular" },
  "Noto Sans KR":       { family: "Noto Sans KR", style: "Regular" },
  "Arial":              { family: "Inter", style: "Regular" },
  "Helvetica":          { family: "Inter", style: "Regular" },
  "sans-serif":         { family: "Inter", style: "Regular" },
};
var FALLBACK_FONT = { family: "Inter", style: "Regular" };

// weight ≥ 600 이면 Bold 스타일 시도용 suffix
function fontForText(textSpec) {
  var mapped = FONT_MAP[textSpec.fontFamily] || null;
  var base = mapped || FALLBACK_FONT;
  var bold = (textSpec.fontWeight || 400) >= 600;
  return {
    family: base.family,
    style: bold ? "Bold" : base.style,
    usedFallback: !mapped,
  };
}

// 로드 성공한 폰트 캐시 — loadFontAsync 중복 호출 방지
var _loadedFonts = {};

async function loadFontSafe(font) {
  var key = font.family + "/" + font.style;
  if (_loadedFonts[key] !== undefined) return _loadedFonts[key];
  try {
    await figma.loadFontAsync({ family: font.family, style: font.style });
    _loadedFonts[key] = { family: font.family, style: font.style };
  } catch (e) {
    // Bold 미존재 → Regular 시도 → 최종 Inter Regular
    if (font.style !== "Regular") {
      var reg = await loadFontSafe({ family: font.family, style: "Regular" });
      _loadedFonts[key] = reg;
    } else if (font.family !== FALLBACK_FONT.family) {
      var fb = await loadFontSafe(FALLBACK_FONT);
      _loadedFonts[key] = fb;
    } else {
      _loadedFonts[key] = null; // Inter 조차 없음 — 호출측 skip
    }
  }
  return _loadedFonts[key];
}

// ── 색상 헬퍼 ──────────────────────────────────────────────────────
function hexToRgb(hex) {
  if (!hex || hex.charAt(0) !== "#" || hex.length < 7) return null;
  return {
    r: parseInt(hex.slice(1, 3), 16) / 255,
    g: parseInt(hex.slice(3, 5), 16) / 255,
    b: parseInt(hex.slice(5, 7), 16) / 255,
  };
}

function solidFill(hex, opacity) {
  var rgb = hexToRgb(hex);
  if (!rgb) return null;
  var paint = { type: "SOLID", color: rgb };
  if (typeof opacity === "number" && opacity < 1) paint.opacity = opacity;
  return paint;
}

// ── base64 → Uint8Array (atob 없는 plugin sandbox 대응) ────────────
var B64_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
function base64ToBytes(b64) {
  b64 = b64.replace(/[^A-Za-z0-9+/]/g, "");
  var bytes = new Uint8Array(Math.floor((b64.length * 3) / 4));
  var p = 0;
  // 4자 그룹 단위. 마지막 partial 그룹 (2~3자) 도 처리 — padding(=) 은
  // 위 replace 에서 이미 제거됨.
  for (var i = 0; i < b64.length; i += 4) {
    var a = B64_CHARS.indexOf(b64.charAt(i));
    var b = B64_CHARS.indexOf(b64.charAt(i + 1));
    var c = i + 2 < b64.length ? B64_CHARS.indexOf(b64.charAt(i + 2)) : -1;
    var d = i + 3 < b64.length ? B64_CHARS.indexOf(b64.charAt(i + 3)) : -1;
    if (a === -1 || b === -1) break;
    bytes[p++] = (a << 2) | (b >> 4);
    if (c !== -1) bytes[p++] = ((b & 15) << 4) | (c >> 2);
    if (d !== -1) bytes[p++] = ((c & 3) << 6) | d;
  }
  return bytes.slice(0, p);
}

// ── 메인 렌더 ──────────────────────────────────────────────────────

async function renderDoc(doc, offsetX) {
  var stats = { nodes: 0, fontFallbacks: 0, skipped: 0, _yield: 0 };
  var meta = doc.meta || {};
  var rootSpec = doc.root;

  var rootFrame = figma.createFrame();
  rootFrame.name = meta.url || "captured-screen";
  var vw = (meta.viewport && meta.viewport.w) || (rootSpec.rect && rootSpec.rect.w) || 1920;
  var vh = Math.max(
    (meta.viewport && meta.viewport.h) || 0,
    (rootSpec.rect && rootSpec.rect.h) || 0
  ) || 1080;
  rootFrame.resize(vw, vh);
  rootFrame.x = offsetX;
  rootFrame.y = 0;
  rootFrame.fills = [solidFill("#ffffff") || { type: "SOLID", color: { r: 1, g: 1, b: 1 } }];
  figma.currentPage.appendChild(rootFrame);
  stats.nodes++;

  // 루트의 절대 원점 — 자식들은 (abs - rootAbs) 상대좌표
  var rootAbs = rootSpec.rect || { x: 0, y: 0 };

  var children = rootSpec.children || [];
  for (var i = 0; i < children.length; i++) {
    await renderNode(children[i], rootFrame, rootAbs, stats);
  }
  return stats;
}

// 노드 수 카운트 (상한 경고용)
function countNodes(spec) {
  if (!spec) return 0;
  var n = 1;
  var children = spec.children || [];
  for (var i = 0; i < children.length; i++) n += countNodes(children[i]);
  return n;
}

// 이상 필드 방어 — 렌더 불가 노드면 true (skip)
function isInvalidSpec(spec) {
  if (!spec || typeof spec !== "object") return true;
  if (!spec.type) return true;
  var r = spec.rect;
  if (!r || typeof r.x !== "number" || typeof r.y !== "number" ||
      typeof r.w !== "number" || typeof r.h !== "number") return true;
  if (isNaN(r.x) || isNaN(r.y) || isNaN(r.w) || isNaN(r.h)) return true;
  if (r.w <= 0 || r.h <= 0) return true;
  if (spec.type === "TEXT") {
    var t = spec.text;
    if (!t || !t.content || !String(t.content).trim()) return true;
  }
  return false;
}

async function renderNode(spec, parent, parentAbs, stats) {
  // 이상 필드 방어 — 크래시 없이 skip
  if (isInvalidSpec(spec)) {
    stats.skipped++;
    return;
  }

  // 200노드마다 yield — 대형 화면 Figma freeze 방지
  stats._yield++;
  if (stats._yield % 200 === 0) {
    await Promise.resolve();
    figma.ui.postMessage({ type: "progress", text: stats.nodes + " nodes..." });
  }

  var rect = spec.rect;
  var relX = rect.x - parentAbs.x;
  var relY = rect.y - parentAbs.y;
  var w = Math.max(1, rect.w);
  var h = Math.max(1, rect.h);
  var style = spec.style || {};
  var node = null;

  if (spec.type === "TEXT") {
    var ts = spec.text || {};
    var font = fontForText(ts);
    var loaded = await loadFontSafe(font);
    if (!loaded) { stats.skipped++; return; }
    if (font.usedFallback) stats.fontFallbacks++;

    var textNode = figma.createText();
    textNode.fontName = loaded;
    textNode.characters = ts.content || "";
    textNode.fontSize = Math.max(1, ts.fontSize || 14);
    var color = solidFill(ts.color || "#000000");
    if (color) textNode.fills = [color];
    var align = (ts.textAlign || "left").toUpperCase();
    if (align === "LEFT" || align === "CENTER" || align === "RIGHT" ||
        align === "JUSTIFIED") {
      textNode.textAlignHorizontal = align === "JUSTIFY" ? "JUSTIFIED" : align;
    }
    if (ts.lineHeight) {
      textNode.lineHeight = { value: ts.lineHeight, unit: "PIXELS" };
    }
    textNode.resize(w, Math.max(h, ts.lineHeight || ts.fontSize || 14));
    node = textNode;
  } else if (spec.type === "IMAGE" && spec.image && spec.image.base64) {
    var rectNode = figma.createRectangle();
    try {
      var img = figma.createImage(base64ToBytes(spec.image.base64));
      rectNode.fills = [{ type: "IMAGE", imageHash: img.hash, scaleMode: "FILL" }];
    } catch (e) {
      rectNode.fills = [solidFill("#cccccc")];
    }
    rectNode.resize(w, h);
    node = rectNode;
  } else if (spec.type === "FRAME" && spec.children && spec.children.length) {
    var frame = figma.createFrame();
    frame.resize(w, h);
    frame.clipsContent = false; // 자식이 살짝 넘쳐도 안 잘리게
    applyStyle(frame, style);
    node = frame;
  } else {
    // RECT (또는 children 없는 FRAME)
    var r = figma.createRectangle();
    r.resize(w, h);
    applyStyle(r, style);
    node = r;
  }

  node.name = spec.name || spec.type.toLowerCase();
  parent.appendChild(node);
  node.x = relX;
  node.y = relY;
  stats.nodes++;

  // FRAME 자식 재귀 — 자식 상대좌표 기준은 이 노드의 절대 rect
  if (spec.type === "FRAME" && spec.children) {
    for (var i = 0; i < spec.children.length; i++) {
      await renderNode(spec.children[i], node, rect, stats);
    }
  }
}

function applyStyle(node, style) {
  var fills = [];
  if (style.background) {
    var f = solidFill(style.background, style.backgroundOpacity);
    if (f) fills.push(f);
  }
  node.fills = fills;
  if (style.borderColor && style.borderWidth) {
    var s = solidFill(style.borderColor);
    if (s) {
      node.strokes = [s];
      node.strokeWeight = style.borderWidth;
    }
  }
  if (style.borderRadius) node.cornerRadius = style.borderRadius;
  if (typeof style.opacity === "number" && style.opacity < 1) {
    node.opacity = style.opacity;
  }
}
