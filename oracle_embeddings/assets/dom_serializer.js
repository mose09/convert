/**
 * dom_serializer.js — 페이지에 주입되어 DOM 레이아웃을 JSON 으로 직렬화.
 *
 * 공개 API: window.__serializeDom(options)
 *   options = {
 *     maxImageKb: 500,   // 이미지 base64 최대 크기 (KB). 초과 시 placeholder RECT
 *     maxDepth:   60,    // 재귀 깊이 상한 (순환/비정상 DOM 방어)
 *   }
 *
 * 반환: docs/FIGMA_JSON_SPEC.md 의 root 노드 (schemaVersion 필드는
 * 호출측 Python 이 meta 와 함께 감싼다). 브라우저 콘솔에서 단독 실행
 * 가능 — Node/번들러 의존 0, 순수 ES5+ (Chromium 환경 가정).
 *
 * 노드 타입 판정:
 *   - 텍스트 직계 자식 → 별도 TEXT 노드로 분리
 *   - <img> 또는 background-image → IMAGE
 *   - 자식 있는 요소 → FRAME / 자식 없는 요소 → RECT
 *
 * 제외: display:none, visibility:hidden, 0×0, script/style/meta 류.
 */
(function () {
  "use strict";

  var SKIP_TAGS = {
    SCRIPT: 1, STYLE: 1, META: 1, LINK: 1, NOSCRIPT: 1, TEMPLATE: 1,
    HEAD: 1, TITLE: 1, BASE: 1,
  };

  // ── 색상 헬퍼 ─────────────────────────────────────────────────────
  // "rgb(31, 58, 95)" / "rgba(31, 58, 95, 0.5)" → {hex: "#1f3a5f", alpha: 0.5}
  // transparent / 빈 값 → null
  function parseColor(cssColor) {
    if (!cssColor || cssColor === "transparent") return null;
    var m = cssColor.match(/rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+)\s*)?\)/);
    if (!m) {
      // 이미 #hex 형태면 그대로
      if (cssColor.charAt(0) === "#") return { hex: cssColor, alpha: 1 };
      return null;
    }
    var a = m[4] === undefined ? 1 : parseFloat(m[4]);
    if (a === 0) return null; // 완전 투명 → 배경 없음 취급
    function h(n) {
      var s = parseInt(n, 10).toString(16);
      return s.length === 1 ? "0" + s : s;
    }
    return { hex: "#" + h(m[1]) + h(m[2]) + h(m[3]), alpha: a };
  }

  // ── 가시성 판정 ───────────────────────────────────────────────────
  function isHidden(el, cs, rect) {
    if (cs.display === "none" || cs.visibility === "hidden") return true;
    if (rect.width <= 0 || rect.height <= 0) return true;
    var op = parseFloat(cs.opacity);
    if (!isNaN(op) && op === 0) return true;
    return false;
  }

  // ── 레이어 이름 (tag + class 기반) ────────────────────────────────
  function layerName(el) {
    var name = el.tagName.toLowerCase();
    if (el.id) name += "#" + el.id;
    var cls = (typeof el.className === "string" ? el.className : "")
      .trim().split(/\s+/).filter(Boolean).slice(0, 3);
    if (cls.length) name += "." + cls.join(".");
    return name.slice(0, 120);
  }

  // ── rect (viewport 절대좌표 + scroll offset) ──────────────────────
  function absRect(rect) {
    var sx = window.pageXOffset || 0;
    var sy = window.pageYOffset || 0;
    return {
      x: Math.round(rect.left + sx),
      y: Math.round(rect.top + sy),
      w: Math.round(rect.width),
      h: Math.round(rect.height),
    };
  }

  // ── style 추출 ────────────────────────────────────────────────────
  function extractStyle(cs) {
    var style = {};
    var bg = parseColor(cs.backgroundColor);
    if (bg) {
      style.background = bg.hex;
      if (bg.alpha < 1) style.backgroundOpacity = bg.alpha;
    }
    var bw = parseFloat(cs.borderTopWidth);
    if (!isNaN(bw) && bw > 0) {
      var bc = parseColor(cs.borderTopColor);
      if (bc) {
        style.borderColor = bc.hex;
        style.borderWidth = bw;
      }
    }
    var br = parseFloat(cs.borderTopLeftRadius);
    if (!isNaN(br) && br > 0) style.borderRadius = Math.round(br);
    var op = parseFloat(cs.opacity);
    if (!isNaN(op) && op < 1) style.opacity = op;
    return style;
  }

  // ── 텍스트 스타일 ─────────────────────────────────────────────────
  function extractTextStyle(cs) {
    var color = parseColor(cs.color);
    var family = (cs.fontFamily || "").split(",")[0]
      .replace(/["']/g, "").trim();
    var lineH = parseFloat(cs.lineHeight);
    return {
      fontFamily: family || "sans-serif",
      fontSize: Math.round(parseFloat(cs.fontSize) || 14),
      fontWeight: parseInt(cs.fontWeight, 10) || 400,
      color: color ? color.hex : "#000000",
      textAlign: cs.textAlign === "start" ? "left" : (cs.textAlign || "left"),
      lineHeight: isNaN(lineH) ? Math.round((parseFloat(cs.fontSize) || 14) * 1.4)
                               : Math.round(lineH),
    };
  }

  // ── 이미지 → base64 (canvas 경유; CORS 실패 시 null) ──────────────
  function imageToBase64(imgEl, maxKb) {
    try {
      var canvas = document.createElement("canvas");
      canvas.width = imgEl.naturalWidth || imgEl.width;
      canvas.height = imgEl.naturalHeight || imgEl.height;
      if (!canvas.width || !canvas.height) return null;
      var ctx = canvas.getContext("2d");
      ctx.drawImage(imgEl, 0, 0);
      var dataUrl = canvas.toDataURL("image/png"); // CORS taint → throws
      var b64 = dataUrl.split(",")[1] || "";
      // base64 길이 → byte 환산 (약 3/4)
      if ((b64.length * 3) / 4 > maxKb * 1024) return null;
      return b64;
    } catch (e) {
      return null; // CORS taint 등 — placeholder 폴백
    }
  }

  // ── 직계 텍스트 추출 (자식 element 의 텍스트는 제외) ──────────────
  function directText(el) {
    var parts = [];
    for (var i = 0; i < el.childNodes.length; i++) {
      var n = el.childNodes[i];
      if (n.nodeType === 3) { // TEXT_NODE
        var t = n.textContent.replace(/\s+/g, " ").trim();
        if (t) parts.push(t);
      }
    }
    return parts.join(" ");
  }

  // ── 메인 재귀 ─────────────────────────────────────────────────────
  function serializeElement(el, opts, depth) {
    if (depth > opts.maxDepth) return null;
    if (SKIP_TAGS[el.tagName]) return null;

    var cs = window.getComputedStyle(el);
    var bcr = el.getBoundingClientRect();
    if (isHidden(el, cs, bcr)) return null;

    var rect = absRect(bcr);
    var name = layerName(el);

    // IMAGE — <img>
    if (el.tagName === "IMG") {
      var b64 = imageToBase64(el, opts.maxImageKb);
      if (b64) {
        return {
          type: "IMAGE", name: name, rect: rect,
          style: extractStyle(cs),
          image: { base64: b64, format: "png" },
        };
      }
      // placeholder RECT — name 에 원본 src 기록
      var src = (el.getAttribute("src") || "").slice(0, 200);
      return {
        type: "RECT", name: "img-placeholder [" + src + "]", rect: rect,
        style: { background: "#cccccc" },
      };
    }

    // background-image 가 있는 요소 — 단순 RECT + name 에 url 표시
    // (canvas 재인코딩은 <img> 만; bg-image 는 1차에서 placeholder)
    var bgImage = cs.backgroundImage;
    var hasBgImage = bgImage && bgImage !== "none" &&
      bgImage.indexOf("url(") !== -1;

    // 자식 직렬화
    var children = [];
    for (var i = 0; i < el.children.length; i++) {
      var c = serializeElement(el.children[i], opts, depth + 1);
      if (c) children.push(c);
    }

    // 직계 텍스트 → 별도 TEXT 노드 (부모 rect 안에 배치)
    var text = directText(el);
    if (text) {
      children.unshift({
        type: "TEXT",
        name: "text: " + text.slice(0, 40),
        rect: rect, // 직계 텍스트의 개별 bbox 는 Range 측정 — 1차는 부모 rect
        text: (function () {
          var ts = extractTextStyle(cs);
          ts.content = text;
          return ts;
        })(),
      });
    }

    var node = {
      type: children.length ? "FRAME" : "RECT",
      name: hasBgImage ? name + " [bg-image]" : name,
      rect: rect,
      style: extractStyle(cs),
    };
    if (hasBgImage && !node.style.background) {
      node.style.background = "#e8e8e8"; // bg-image placeholder 색
    }
    if (children.length) node.children = children;
    return node;
  }

  // ── 공개 API ──────────────────────────────────────────────────────
  window.__serializeDom = function (options) {
    var opts = options || {};
    if (typeof opts.maxImageKb !== "number") opts.maxImageKb = 500;
    if (typeof opts.maxDepth !== "number") opts.maxDepth = 60;

    var root = serializeElement(document.body, opts, 0);
    if (!root) {
      // body 가 hidden 인 비정상 케이스 — 최소 빈 FRAME
      root = {
        type: "FRAME", name: "body", style: {},
        rect: { x: 0, y: 0, w: window.innerWidth, h: window.innerHeight },
      };
    }
    // 루트는 항상 FRAME (Figma 루트 프레임)
    root.type = "FRAME";
    return root;
  };
})();
