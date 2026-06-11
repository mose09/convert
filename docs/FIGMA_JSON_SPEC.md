# FIGMA_JSON_SPEC — capture-screens JSON 스키마 명세

> **Python (`legacy_screen_capture.py`) ↔ Figma 플러그인 (`figma_plugin/code.js`)
> 간의 유일한 계약 문서.** 스키마 변경 시 반드시 이 문서를 먼저 갱신한 후
> 양쪽 코드를 맞춘다.

- `schemaVersion`: **1** (현재)
- 직렬화 주체: `oracle_embeddings/assets/dom_serializer.js`
  (`window.__serializeDom(options)`)
- 파일 단위: 화면(라우트) 1개 = JSON 파일 1개
  (`output/figma_capture/<YYYYMMDD>/<slug>.json`)

---

## 1. 최상위 구조

```json
{
  "schemaVersion": 1,
  "meta": { ... },
  "root": { ...노드... }
}
```

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | --- | --- |
| `schemaVersion` | int | ✓ | 항상 `1`. 플러그인이 미지원 버전이면 import 거부 |
| `meta` | object | ✓ | 캡처 메타데이터 (§2) |
| `root` | object | ✓ | 루트 노드 — 항상 `type: "FRAME"` (§3) |

## 2. `meta`

```json
{
  "url": "/order/list",
  "title": "주문 목록",
  "viewport": { "w": 1920, "h": 1080 },
  "capturedAt": "2026-06-18T09:30:00+09:00"
}
```

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | --- | --- |
| `url` | string | ✓ | 캡처한 라우트 (base_url 제외). 플러그인의 루트 프레임 이름으로 사용 |
| `title` | string | ✓ | `document.title`. 빈 문자열 가능 |
| `viewport` | `{w:int, h:int}` | ✓ | 캡처 시 브라우저 viewport 크기 (px) |
| `capturedAt` | string | ✓ | ISO8601 타임스탬프 |

## 3. 노드 (공통 필드)

```json
{
  "type": "FRAME",
  "name": "div#main.order-grid",
  "rect": { "x": 0, "y": 0, "w": 1920, "h": 400 },
  "style": { ... },
  "children": [ ... ]
}
```

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | --- | --- |
| `type` | enum | ✓ | `"FRAME"` \| `"TEXT"` \| `"RECT"` \| `"IMAGE"` |
| `name` | string | ✓ | 레이어명. `tag#id.class1.class2` 형식 (최대 120자, class 최대 3개). placeholder 는 `img-placeholder [원본src]` 형식 |
| `rect` | object | ✓ | **viewport 절대좌표** (px, 정수). `getBoundingClientRect` + scroll offset. 플러그인이 부모 상대좌표로 변환 |
| `style` | object | ✓ (TEXT 제외 시 생략 가능) | 시각 스타일 (§4). 빈 객체 가능 |
| `children` | array | — | `type: "FRAME"` 일 때만 존재. 자식 노드 배열 (출현 순서 유지) |
| `text` | object | TEXT 만 ✓ | §5 |
| `image` | object | IMAGE 만 ✓ | §6 |

### 3.1 노드 타입 판정 규칙 (serializer 측)

| 조건 | 타입 |
| --- | --- |
| 자식 element 또는 직계 텍스트 있는 요소 | `FRAME` |
| 자식 없는 요소 | `RECT` |
| `<img>` (base64 인코딩 성공) | `IMAGE` |
| `<img>` (CORS taint / `--max-image-kb` 초과) | `RECT` (placeholder, name 에 원본 src) |
| 요소의 직계 텍스트 노드 | 부모에서 분리해 별도 `TEXT` 노드 (children 의 첫 항목) |
| 루트 (`document.body`) | 항상 `FRAME` |

### 3.2 직렬화 제외 (노드 자체가 출력 안 됨)

- `display: none` / `visibility: hidden` / `opacity: 0`
- `rect` 의 `w <= 0` 또는 `h <= 0`
- `SCRIPT` / `STYLE` / `META` / `LINK` / `NOSCRIPT` / `TEMPLATE` / `HEAD` / `TITLE` / `BASE` 태그
- 재귀 깊이 60 초과 (비정상 DOM 방어)

## 4. `style`

```json
{
  "background": "#ffffff",
  "backgroundOpacity": 0.5,
  "borderColor": "#dddddd",
  "borderWidth": 1,
  "borderRadius": 4,
  "opacity": 1
}
```

| 필드 | 타입 | 생략 조건 | 설명 |
| --- | --- | --- | --- |
| `background` | `#rrggbb` | 배경 투명/없음 | `backgroundColor` 의 hex |
| `backgroundOpacity` | float 0~1 | 1.0 일 때 | rgba 의 alpha (1 미만일 때만) |
| `borderColor` | `#rrggbb` | border 없음 | `borderTopColor` 기준 |
| `borderWidth` | float (px) | border 없음 | `borderTopWidth` 기준. `borderColor` 와 함께만 출현 |
| `borderRadius` | int (px) | 0 일 때 | `borderTopLeftRadius` 기준 |
| `opacity` | float 0~1 | 1.0 일 때 | 요소 자체 opacity (1 미만일 때만) |

**색상 규칙**: 모두 소문자 6자리 hex (`#rrggbb`). alpha 는 별도 필드.
`transparent` / `rgba(...,0)` 은 필드 자체 생략.

## 5. `text` (type=TEXT 전용)

```json
{
  "content": "주문번호",
  "fontFamily": "Malgun Gothic",
  "fontSize": 14,
  "fontWeight": 400,
  "color": "#333333",
  "textAlign": "left",
  "lineHeight": 20
}
```

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | --- | --- |
| `content` | string | ✓ | 공백 정규화된 텍스트 (`\s+` → 한 칸). 빈 문자열이면 노드 자체 미출력 |
| `fontFamily` | string | ✓ | `font-family` 의 첫 번째 폰트 (따옴표 제거). 플러그인이 `FONT_MAP` 으로 매핑 |
| `fontSize` | int (px) | ✓ | 반올림 |
| `fontWeight` | int | ✓ | 100~900. `normal`→400, `bold`→700 |
| `color` | `#rrggbb` | ✓ | 글자색. 파싱 실패 시 `#000000` |
| `textAlign` | string | ✓ | `left` \| `center` \| `right` \| `justify`. `start` 는 `left` 로 정규화 |
| `lineHeight` | int (px) | ✓ | `normal` 이면 `fontSize × 1.4` 반올림 |

**TEXT 노드의 `rect`**: 1차 구현은 부모 요소의 rect 를 그대로 사용
(개별 텍스트 bbox 측정은 Range API 후속). 플러그인은 rect 안에서
`textAlign` 으로 정렬.

## 6. `image` (type=IMAGE 전용)

```json
{
  "base64": "iVBORw0KGgo...",
  "format": "png"
}
```

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | --- | --- |
| `base64` | string | ✓ | data URL 의 base64 본문 (`data:image/png;base64,` 접두 제외) |
| `format` | string | ✓ | 항상 `"png"` (canvas `toDataURL("image/png")` 재인코딩) |

**제약**:
- 동일 출처 `<img>` 만 canvas 인코딩 가능. CORS taint 시 → placeholder RECT.
- `--max-image-kb` (기본 500) 초과 → placeholder RECT.
- placeholder RECT: `style.background = "#cccccc"`, `name = "img-placeholder [원본 src 200자]"`.
- CSS `background-image` 는 1차에서 placeholder 색 (`#e8e8e8`) RECT/FRAME 으로 표현,
  `name` 에 ` [bg-image]` suffix.

## 7. 좌표계

- **serializer 출력**: viewport 절대좌표 (스크롤 offset 포함).
  `x = rect.left + window.pageXOffset` (y 동일).
- **플러그인 변환**: Figma 노드는 부모 상대좌표 — `child.x = abs.x - parent.abs.x`.
- 루트 프레임은 `(0, 0)` 배치, 크기는 `meta.viewport` 또는 root.rect 중 큰 값.

## 8. 버전 정책

| schemaVersion | 변경 |
| --- | --- |
| 1 | 최초 — FRAME/TEXT/RECT/IMAGE, 절대좌표, style/text/image 필드 |

- 필드 **추가** 는 같은 버전에서 허용 (플러그인은 미지식 필드 무시).
- 필드 의미 변경 / 제거 / 좌표계 변경은 버전 증가 + 양쪽 동시 갱신.
