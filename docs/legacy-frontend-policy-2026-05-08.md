# Legacy Frontend 분석 정책 (snapshot, 2026-05-08)

이번 세션 (PR #131 ~ #169) 동안 사용자 보고 기반으로 발전시킨
`analyze-legacy` 의 frontend (React) 분석 정책. 사용자 다양한 레포 환경
대응 누적한 결과 — 추후 새 환경에서 회귀 발견 시 이 문서를 시작점으로.

미해결 사항 / follow-up 도 함께 명시.

## 1. 분석 대상 (entry file)

`apps/<...>/index.{js,jsx,ts,tsx}` 가 메인 entry. 단 nested 도 cover:

- `apps/X/index.js` — 메인
- `apps/X/Y/index.js` — 메인 (Y 가 실제 앱이고 X 가 카테고리 폴더 케이스)
- `apps/X/Y/Body/index.js` — sub-area, **메인 (`apps/X/Y/index.js`) 으로 통합**
- `apps/X/Y/components/popup/InstallNousePopUp.js` — popup, **별개 화면**

**메인 자동 감지** (`_collect_main_entries`):
모든 `apps/.../index.*` 중 다른 `index.*` 의 ancestor 인 것만 메인.
즉 `apps/X/Y/index.js` (parent: `apps/X/Y/`) 가 `apps/X/Y/Body/index.js`
의 prefix → `apps/X/Y/index.js` 가 메인. depth 무관.

## 2. Sub-area vs Popup 판정

**Popup (별개 화면)** — 셋 중 하나 매칭:
1. 폴더명에 popup 키워드 (`popup`/`modal`/`dialog`/`drawer`/`window`)
   포함 (path 어느 segment 든, 대소문자 무관)
2. 메인 entry 가 render 안 `<Modal>...</Modal>` 안에 import 한 컴포넌트
3. main_files set 에 직접 등록 (메인 자체)

**Sub-area (메인 통합)** — popup 아닌 sub-component. 가장 가까운 ancestor
메인의 events 표에 통합.

**예시 (사용자 환경)**:
- `apps/hypm_installMng/InstallManage/Body/index.js` → 메인 통합 (`<div>` 안)
- `apps/hypm_installMng/InstallManage/components/popup/InstallNousePopUp.js`
  → popup 별개 (path 안 popup 키워드)
- `apps/hypm_Y/InstallScreen/index.js` (폴더명 popup 키워드 X but 메인이
  `<Modal><InstallScreen/></Modal>` 사용) → popup 별개 (Modal import 매칭)

## 3. Handler URL 추출 정책

**fallback 제거** (사용자 명시 — 정확도 우선):
- folder-scope / app-slug-scope fallback 없음
- chain follow 결과만 사용 → 못 따라가면 누락 (1:N 노이즈 방지)

**Chain follow 깊이**: `_scan_body_with_chain(depth=5)` — handler body +
5 단계 추가 호출 (총 6 단계 깊이) 까지 axios URL 추적.

**Helper 식별**: handler 이름이 다른 trigger event 의 body 에서 호출
되면 chain 중간 함수로 분류 → trigger 결과에서 제외 (URL 은 호출자에
자동 매핑).

## 4. Trigger 표시

**기본 형식**: `[onClick] 조회`, `[onChange] 다중권한`

**Cross-file parent marker**: 자식 popup 의 `this.props.X(...)` chain 이
**다른 파일** 의 부모 함수까지 도달 → `[onClick → parent.handleSubmit] 확인`
표시 (PR #164 — same-file self-binding 제외).

**Label 추출 우선순위** (`_extract_event_label`):
1. 같은 tag 의 `label` / `placeholder` / `title` attr
2. tag children 텍스트 (`<Button>조회</Button>`)
3. 부모 `<Form.Item label="...">`
4. handler tag 직전 600자 안 형제 텍스트 노드 (`<span class="search-label">다중권한</span>`)

**Dedup**: 같은 handler 의 다른 ctx 가 채워진 label 가지면 `label=""` ctx
는 emit skip (handler 이름 노출 노이즈 차단).

## 5. Event 정렬

`_render_events` 표시 순서:
- 0: lifecycle (`mount` / `useEffect` / `didMount` / `willMount`)
- 1: `didUpdate`
- 2: `onChange`
- 3: `onSubmit`
- 4: `onClick`
- 5: 그 외 (`onBlur` / `onFocus` 등)

같은 우선순위 안에서는 trigger 라벨 알파벳 순.

## 6. Regex / 추출 디테일

**JSX event regex** (`_ANY_JSX_EVENT_RE`):
- `onXxx={fnName}` (직접 reference)
- `onXxx={() => fnName(...)}` (inline arrow + 호출)
- `onXxx={() => { body }}` (brace body) — PR #166

**bind/call/apply 후처리** (`_resolve_handler_name`):
`onClick={() => {this.fnX.bind(this)();}}` 같은 케이스:
- 매칭 결과 arrow="bind" → 직전 segment "fnX" 로 교체

**Class method 인덱스** (`_CLASS_METHOD_RE`):
ES6 `fnSearch(args) { ... }` (no `=`, no `function`) 도 fn_index 등록.

**Comment strip**: 모든 read 단계에서 `//` / `/* */` 제거 (string literal
보존). 주석 안 옛 axios / 함수 호출이 매칭되지 않게.

**Smart slice** (`_smart_slice`): 큰 React 파일 (5000+ 라인) 에서 imports +
render() body 만 추출 → LLM 에 32K 자 안에 들어가게. styled-components /
helper functions / propTypes 제외.

## 7. Screen Layout (Phase C, `--extract-screen-layout`)

**LLM 추출** (Qwen 397B 멀티모달):
- page_title
- search_panel (필드 / 컴포넌트 종류 / default / options)
- data_table_columns (title / field / width / hide)
- edit_mode_fields
- tabs
- flowchart_mermaid (사용자 액션 흐름)
- summary

**HTML 출력 구조** (`render_screen_html`):
1. 요약
2. Search Panel — 텍스트 bullet 리스트 (사용자 요청, 박스 mock-form X)
3. Tabs
4. **DataTable** — title (1행) + field (2행) + placeholder rows + Hide 컬럼 별도 섹션 하단
5. **Flowchart** (`<pre class="mermaid">`)
6. Edit Mode — 텍스트 bullet
7. **이벤트 → 백엔드 URL** 표 — trigger 별 그룹화, URL `<br>` join

**`<head>` 에 mermaid.min.js CDN script** — 폐쇄망이면 raw 코드만 노출.

## 8. Sample 이미지 (vision LLM)

`input/flowchart_sample.{png,jpg,jpeg,webp}` — 출력 flowchart 의 **스타일/형태
sample**. 모든 화면 공통 사용. 화면 스크린샷 X.

`_call_llm` 의 `image_paths` 인자 — OpenAI vision 호환 multimodal content
형식 (base64 data URL).

## 9. Cache 정책

**Screen layout cache OFF** (사용자 명시 — "어차피 한번 제대로 돌린건
다시 안돌릴거야"). 매번 새 LLM 호출.

`extract_screen_layouts(use_cache=False)` 양쪽 caller (`analyze_legacy`,
`_run_frontend_only`) hardcoded.

## 10. Frontend-only 모드

`--frontend-only` 플래그 — backend / 메뉴 매칭 / 컨트롤러 체인 모두 skip
하고 React frontend 만 분석. backend 인자 없이도 실행.

```
python main.py analyze-legacy \
  --frontend-only \
  --frontends-root <path> \
  --skip-menu \
  --extract-screen-layout \
  --screen-max 50
```

## 11. 산출물 폴더 구조

```
output/legacy_analysis/<YYYYMMDD>/
  reports/...
  screens/<HHMMSS>/<repo>/<file>.html  (실행마다 timestamp + repo subfolder)
  frontend_only_summary.txt            (frontend-only 모드)
  .biz_cache/                          (Phase A/B 영구 cache)
  .screen_cache/                       (Phase C — 현재 미사용, off)
```

## 미해결 / Follow-up

사용자 "포기" 시점 — 사용자 환경 다양성으로 인한 케이스가 더 있을 수
있음. 추후 발견 시 이 정책을 시작점으로 추가 보강.

알려진 한계:
- redux-saga 패턴 (`this.props.X → mapDispatchToProps → dispatch(action)
  → saga.takeLatest`) 의 chain follow 는 action creator 단계에서 끊김 —
  사용자 정책 (정확도 우선) 상 누락 OK
- conditional / portal / functional component (no class) 의 popup 패턴
  은 `<Modal>` regex 로 못 잡을 수 있음
- 사용자 환경 레포마다 폴더 컨벤션 다름 — 새 패턴 발견 시 휴리스틱 추가
- `<Modal>` 외 다른 popup container (Drawer / Dialog / etc) 가 사용자
  환경에 있으면 `_POPUP_CONTAINER_TAGS` 확장 필요 (사용자 명시: 일단
  Modal 만)

## 관련 PR 시퀀스 (참고)

- PR #131 — Trigger 컬럼 saga.js import 핸들러 누락 fix
- PR #132~#135 — diag_trigger.py / probe-route-file
- PR #137 — Route component={X} regex camelCase
- PR #138~#141, #146 — Phase C screen layout (text bullets, DataTable
  title+field+hide, mermaid flowchart, frontend-only, screens 폴더 구조)
- PR #144, #150 — events 정확도 (LLM 환각 차단, fallback 제거)
- PR #145, #154 — Chain follow (multi-level, ES6 class method, depth 5)
- PR #148 — JS/JSX comment strip
- PR #149 — Lifecycle URL 격리
- PR #157, #164~#165 — Prop binding chain follow + parent.X marker
- PR #167~#169 — Popup vs sub-area 분류 (folder keyword + Modal import +
  nested main + path-segment popup)
- PR #166 — bind/call/apply 후처리

상세 변경은 git log 참조.
