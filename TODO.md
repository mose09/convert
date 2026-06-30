# TODO

작업은 **13개 기능 카테고리**별로 분리해 관리한다. 두 세션이 서로 다른
카테고리를 동시에 수정해도 머지 충돌이 없도록, 카테고리 경계 (H2 헤더)
와 순서는 고정한다.

## 사용 규칙

1. **카테고리 순서 / 헤더 변경 금지**. 새 작업은 해당 카테고리 아래
   `### 진행 중: <제목>` 서브섹션으로 추가한다. 동일 카테고리에 두 세션이
   동시에 작업하지 않도록 시작 전 확인 — 필요하면 한 세션이 다른
   카테고리로 분리한다.
2. **체크박스**: 작업 항목은 `- [ ]` / `- [x]` 로 표시. 완료 즉시 체크.
3. **완료된 섹션은 PR 머지 후 즉시 삭제**. 히스토리는 git log / GitHub PR
   이 담당하고, 재발 방지가 필요한 교훈은 `CLAUDE.md` 의 "해결된 주요
   이슈" 표에 요약해 남긴다.
4. **카테고리 간 걸치는 작업**은 대표 카테고리 하나를 골라 거기 배치하고
   다른 카테고리의 `_참조_` 에 한 줄만 링크로 남긴다.
5. **공통/인프라 작업** (CLAUDE.md, 워크플로우, CI, 공유 유틸) 은
   `## 0. 공통 / 인프라` 에 모은다.

---

## 0. 공통 / 인프라

### 진행 중: `docs` — 기능 카탈로그 형태로 재설계

기존 빌더가 README 를 그냥 한 페이지에 다 쏟아붓는 형태였는데 사용자가
**기능 목록 → 클릭 → 상세** 의 진짜 설명서 UX 를 원함. 좌측 사이드바
카테고리별 커맨드 + 우측 상세 패널 + JS 라우팅 (#cmd/<name>, #topic/<id>)
형태로 리팩토링.

- [x] README 의 `## 기능 요약` 표 파싱 → 25개 커맨드 메타 (name / desc /
      oracle / llm)
- [x] `## 산출물 경로 규약` 표 파싱 → 커맨드별 경로 lookup
- [x] `### N. ...` H3 섹션 번호 별 본문 추출 → 커맨드 detail 매핑
      (`SECTION_MAP`)
- [x] 카테고리 그룹 (`CATEGORY_MAP`) — 스키마 / 용어 / AS-IS / 마이그레
      이션 4 종 분류
- [x] 토픽 (설치 / 설정 / 워크플로우 / 산출물 / 프로젝트 구조 / ERD 렌더링)
      별도 사이드바 그룹
- [x] HTML 템플릿: brand / search / nav / 메인 패널 (배지·산출물경로
      ·detail HTML) / 홈 카드 / 모바일 햄버거
- [x] JS hash routing + 검색 필터링 + 테마 토글
- [x] embed/grid-labels 같이 README 에 별도 섹션 없는 커맨드는 graceful
      fallback (`--help` 안내 1줄)

### 진행 중: 출력 경로 규약 통일 — `output/<영역>/<일자>/<파일>`

각 커맨드가 산출물을 다른 깊이/이름으로 떨어뜨려서 (`output/스키마.md` /
`output/morpheme/...` / `output/sql_migration/...`) 일관성 부족. 영역별
폴더 + 일자 (YYYYMMDD) 하위 폴더로 일괄 통일.

- [x] `main.py` 에 `_build_dated_output_dir(base, area)` 헬퍼 추가
- [x] cmd 별 영역 폴더 매핑 적용 (총 13 영역):
      `schema` / `query` / `enrich-schema` / `erd` (4개 erd cmd 통합) /
      `terms` / `morpheme` / `standardize` / `sql_review` /
      `naming_validation` / `ddl` / `audit` /
      `legacy_analysis` (analyze-legacy + discover-patterns + raw LLM
      덤프) / `migration` (3개 cmd 통합, 기존 `sql_migration` 이름 단순화)
- [x] 제외 대상 유지: `convert-mapping` / `convert-menu` → `input/`
- [x] 영구 캐시 유지: `output/legacy_analysis/.biz_cache/`
      (`legacy_biz_extractor._cache_dir` 가 영역 루트 직속에 생성)
- [x] `legacy_report._legacy_output_dir` 가 자체 `legacy_analysis/<date>`
      서브폴더 생성하도록 변경 (main.py 가 base_output 만 넘기면 됨)
- [x] `legacy_pattern_discovery._dump_raw` 도 dated 경로로 갱신
- [x] `--output` 명시 지정 시 사용자 경로 그대로 (모든 cmd 일관)
- [x] CLAUDE.md 출력/입력 경로 규약 표 + Migration 산출물 경로 덤프 갱신
- [x] README 상단에 "산출물 경로 규약" 섹션 신규 + morpheme/legacy/migration
      산출물 트리 갱신
- [x] smoke test:
      * `_build_dated_output_dir` 13 영역 전부 정상 mkdir
      * `morpheme` 실 실행 → `./output/morpheme/20260428/morpheme_*.{md,xlsx}` 생성
      * `legacy_report._legacy_output_dir` → `legacy_analysis/20260428/` 반환
- [ ] PR + squash-merge + local cleanup

---

## 1. schema — Oracle 스키마 추출

_진행 중 없음_

---

## 2. query — MyBatis 쿼리 분석

_진행 중 없음_

---

## 3. enrich-schema — LLM 코멘트 보강

_진행 중 없음_

---

## 4. ERD 생성 — `erd` / `erd-md` / `erd-group` / `erd-rag`

_진행 중 없음_

---

## 5. terms — 용어사전 자동 생성

### 진행 중: `recommend-names` — AS-IS 스키마 → TO-BE 속성명 추천

표준 단어사전/용어사전(Excel)을 SQLite 로 데이터화하고, AS-IS 컬럼을
Tier1 정확매칭 → Tier2 단어조합 → Tier3 RAG → Tier4 LLM 으로 TO-BE
표준 물리명·도메인·데이터유형 추천.

- [x] `oracle_embeddings/std_dict.py` — Excel(단어/용어) → SQLite 캐시
      (헤더 자동인식, 만료/표준여부 처리, mtime 재빌드) + 인메모리 인덱스
- [x] `oracle_embeddings/tobe_recommender.py` — 4계층 추천 엔진
      (결정적 코어는 임베딩/LLM 없이 독립 동작) + 용어사전 임베딩
- [x] `oracle_embeddings/tobe_report.py` — Markdown + 3시트 Excel
- [x] `main.py` — `recommend-names` subparser + `cmd_recommend_names()`
- [x] `build-dict` subparser — 적재 단계 분리 (기존 삭제 후 재적재, `--embed`)
- [x] 도메인사전 적재 (`--domain-dict`, 동일 도메인명 다중 보존) +
      데이터유형 추론 보정
- [x] 매칭 정확도: 표준여부 N 단어도 약어 사용(logical_to_abbr),
      표준여부 표기 견고화, (LLM추천) 코멘트 노이즈 제거, 미매칭 «» 마커
- [x] 출력: `output/recommend_names/<YYYYMMDD>/tobe_recommend_*.{md,xlsx}`
- [x] README §16 + 기능표/경로표 + docs_builder 매핑 + user_manual 재빌드
- [x] 로더 견고화: 전체 시트 자동스캔 / NFC·숨은문자 / read_only 해제
      (깨진 dimension) / 셀별 repr 진단
- [ ] 사용자 실제 단어/용어사전 Excel 로 매칭률 1차 검증

---

### 진행 중: `grid-labels` — AG Grid 의 (field, headerName) 페어 추출 커맨드

`analyze-legacy` / `screen-spec` 의 closure 기반 그리드 추출은 화면 단위.
별도로 **repo 통째로** scan 해서 `columnDefs` 의 (physical_name, label) 만
모아주는 deterministic 커맨드가 필요. AG Grid 의 `headerName + field`
관용에 맞춰 regex 로 추출 (AST·LLM 의존 없음).

- [x] `oracle_embeddings/grid_labels_extractor.py` — walk + regex pair
- [x] `main.py` — `grid-labels` subparser + `cmd_grid_labels()`
- [x] 출력: `output/grid-labels/<YYYYMMDD>/grid_labels_<HHMMSS>.xlsx`
- [x] README — 명령 표 한 줄 추가

---

## 6. gen-ddl — 자연어 DDL 생성

_진행 중 없음_

---

## 7. audit-standards — 표준 위반 전수 감사

_진행 중 없음_

---

## 8. validate-naming — 네이밍룰 검증

_진행 중 없음_

---

## 9. review-sql — SQL 안티패턴 리뷰

_진행 중 없음_

---

## 10. standardize — 표준화 분석 리포트

_진행 중 없음_

---

## 11. analyze-legacy — AS-IS 소스 통합 분석

`analyze-legacy` 본체 + 보조 커맨드 (`discover-patterns`, `convert-menu`)
+ React/Polymer 스캐너 / Java 파서 / 메뉴 로더 전부 포함.

### 진행 중: capture-screens — AS-IS 화면 → Figma 편집 가능 레이어 변환

작업지시서 (SPEC) 기반. Playwright 로 AS-IS React 화면 렌더 → DOM 레이아웃
JSON 추출 → 사내 Figma 플러그인이 createFrame/createText/createRectangle 로
편집 가능 레이어 재구성. 외부 SaaS 전송 없음 (폐쇄망/보안). STEP 순서대로
하나씩 진행 (묶음 구현 금지).

Phase A — Python 캡처 레이어
- [x] STEP 1. TODO.md 작성 + 의존성 준비 (requirements.txt playwright,
      .env.example FIGMA_CAPTURE_BASE_URL, 컨테이너 playwright 1.56 +
      번들 chromium-1194 매칭 확인)
- [x] STEP 2. `oracle_embeddings/assets/dom_serializer.js` —
      `window.__serializeDom(options)`, body 재귀, TEXT 분리 / IMAGE /
      FRAME / RECT 판정, rgb→hex, display:none/0×0 제외.
      검증: mock 페이지 17 FRAME + 10 TEXT + 2 RECT, 한글/hidden OK
- [x] STEP 3. `docs/FIGMA_JSON_SPEC.md` — 스키마 정식 명세
      (schemaVersion: 1). Python ↔ 플러그인 유일 계약
- [x] STEP 4. `legacy_screen_capture.py` 골격 + 단일 화면 캡처 —
      `capture_screens()` 공개 API, slug 는 legacy_util.normalize_url,
      진단 1줄 + `_failed.md`. 검증: mock 캡처 1건 + 동적 라우트 skip
- [x] STEP 5. 라우트 소스 연동 — ① --routes-file ② --frontend-dir
      (legacy_react_router.build_url_to_component_map) ③ --url.
      patterns.yaml url_prefix_strip / react_route_prefix 적용. 동적
      세그먼트 skip + --param-fill. 검증: mock React 3 라우트 추출
- [x] STEP 6. 인증/대기/이미지 옵션 — capture_screens 시그니처에 모두
      포함. 검증: 이미지 mock 페이지 base64 IMAGE 노드 1건
- [x] STEP 7. main.py `capture-screens` 커맨드 통합 (26종 → 27종).
      검증: --help + --list-only + mock e2e CLI 1건

Phase B — Figma 플러그인
- [x] STEP 8. `figma_plugin/` 골격 — manifest.json (networkAccess none)
      / ui.html (textarea + 멀티 파일 선택 + 진행 카운터) / code.js
      (onmessage + schemaVersion 검증).
      ※ [ ] Figma 데스크톱 "Import plugin from manifest" 로드는 사용자
      수동 확인 항목
- [x] STEP 9. 노드 렌더러 — FRAME/RECT/TEXT/IMAGE → Figma API, 절대→상대
      좌표, FONT_MAP (맑은고딕→Noto Sans KR) + Inter 폴백 + 카운트,
      200노드 yield, base64→Uint8Array 자체 구현.
      ※ [ ] mock JSON import 레이어 트리 생성은 사용자 수동 확인 항목
- [x] STEP 10. 렌더러 견고화 — isInvalidSpec (rect 음수/NaN/빈 텍스트
      skip), >3000 노드 경고, figma.notify 요약. 검증: 순수 함수 Node
      테스트 (base64 4 길이 / 깨진 spec 7종 skip)

Phase C — 검증 / 문서 / 마감
- [x] STEP 11. `/tmp/mock_capture/` 정적 HTML 2장 (한글 UTF-8 + 이미지)
      + `verify_capture.py` (mock 자산 자동 생성 + 노드 수 assert).
      검증: ✓ 화면1 29노드 (TEXT 10), 화면2 IMAGE 1
- [x] STEP 12. README §17 capture-screens 섹션 (폐쇄망 Playwright 오프라인
      설치 + PLAYWRIGHT_BROWSERS_PATH + 플러그인 로드법 + storage_state)
      + 기능 요약 표 + 경로 규약 표 + CLAUDE.md 커맨드 표 27종 +
      docs_builder CATEGORY_MAP/SECTION_MAP + user_manual.html 재빌드
- [x] STEP 13. 회귀 확인 (기존 6 커맨드 --help + analyze-legacy mock
      endpoint=1/method-scope=1 변동 없음) + 커밋/푸시
      (claude/capture-screens → PR squash-merge)

### 진행 중: 데몬(배치) 분석 — Spring Batch + Quartz

기존 Controller → Service → DAO → XML → Table → RFC 체인 추출이 웹 컨트
롤러만 인식. 사용자 요청: 배치 데몬 (Spring Batch Tasklet/ItemReader/
Processor/Writer, Quartz Job) entry 도 같은 체인 추적해서 별도 시트
"데몬" 으로 emit.

- [x] `legacy_java_parser._extract_daemon_entries` + `parse_java_file` —
      Spring Batch (`implements Tasklet|ItemReader|ItemProcessor|ItemWriter|
      ItemStream{Reader,Writer}`) + Quartz (`implements Job`,
      `extends QuartzJobBean`, `@DisallowConcurrentExecution` /
      `@PersistJobDataAfterExecution`)
- [x] `_build_indexes` 에 `daemons_by_fqcn` 인덱스 추가
- [x] `legacy_analyzer._build_daemon_row` + `analyze_legacy` daemon
      iteration — `_resolve_endpoint_chain` BFS 재사용 (controller 와
      같은 체인)
- [x] `legacy_report` save_legacy_excel / save_legacy_batch_excel 양쪽에
      새 시트 "데몬" 12컬럼 (사용자 명시 8컬럼 + 보조 메타)
- [x] `analyze_legacy_batch` daemon_rows aggregation
- [x] `main.py` `--analyze-daemons` opt-in flag + analyze_legacy /
      analyze_legacy_batch 양쪽 wiring
- [x] README + user_manual.html 재빌드
- [x] Quartz XML 정의 인식 (`quartz_data.xml`, `spring-quartz.xml` 등) —
      `extract_quartz_xml_jobs` + `_attach_xml_daemons`. 3 패턴 cover:
      Quartz native ``<job-class>``, Spring ``JobDetailFactoryBean``,
      Spring ``MethodInvokingJobDetailFactoryBean`` (targetObject +
      targetMethod 으로 service bean 의 메소드를 daemon entry 로 등록)

### 진행 중: trigger 단위 LLM 분석 — Phase 2+3 (LLM 호출 + 머지)

Phase 1 (PR #266) 의 bundle 을 받아 LLM 호출 + 응답 캐시 + screen layout 머지.
CLI 옵션 `--llm-per-trigger` (옵트인) 추가.

- [x] `analyze_trigger_with_llm(bundle, config, *, cache_dir)` — OpenAI client
      호출, JSON schema (action_description / validation_rule /
      affected_fields / backend_calls / business_summary), parser
      factual_urls 강제 적용 (환각 방지)
- [x] 캐시 — MD5 of serialized bundle. hit 시 0 LLM round-trip
- [x] `analyze_triggers_batch()` — 일괄 분석 + 진행률 print
- [x] `_TRIGGER_LLM_SYSTEM` 프롬프트 — 사용자 화면 관점, 추측 금지,
      facts 신뢰, cascading 명시
- [x] `_parse_llm_json()` — plain / ```fence / surrounded 3 변종 처리
- [x] `_llm_analyze_triggers_for_screen()` — 화면 1개의 모든 trigger →
      bundle build → LLM 호출
- [x] `_merge_trigger_llm_into_layout()` — search_panel.action /
      validation_rule + events.narrative 에 머지. parser 결과 보존 +
      LLM 보강 prepend
- [x] `extract_screen_layouts` 시그니처 + 메인 루프에 hook
- [x] `analyze_legacy` / `analyze_legacy_batch` plumbing
- [x] `main.py` CLI 옵션 + 3 호출처 wiring
- [x] README + user_manual 갱신
- [x] LLM 호출 / 캐시 hit / merge 동작 unit test 통과

### 진행 중: trigger 단위 LLM 분석 — Phase 1 (bundle builder)

사용자 제안: 이벤트 → handler → helper → action → saga 전체 체인을
한 덩어리로 묶어서 LLM 한 번 호출 (백엔드 `--extract-biz-logic` 와
대칭 패턴). cascading / 분기 / setState clear / 검증 등 trigger-specific
의미 추론을 균일하게 처리.

Phase 1 (이번 PR): bundle builder + 직렬화. LLM 호출/캐시 (Phase 2),
응답 머지 (Phase 3) 는 후속.

- [x] `oracle_embeddings/legacy_trigger_bundler.py` 신규 모듈
- [x] `build_trigger_bundle(trigger, file_content, ...)` → bundle dict
      (trigger_jsx / event_type / handler_name / label / source_file /
      handler_chain / setstate_writes / factual_urls)
- [x] handler chain follow — 같은 파일 → fn_index 순으로 helper /
      action body 따라감 (max_depth 3, cycle 방지)
- [x] Redux/saga chain — `_DISPATCH_ACTION_RE` + `_THIS_PROPS_CALL_LEAF_RE`
      + destructured + propTypes → mDTP → action body. type_key 같이 노출.
- [x] setState writes / factual URLs — scanner facts 그대로 노출
      (LLM 환각 방지용 ground truth)
- [x] `serialize_bundle_for_llm()` — Markdown 형식 LLM user-message body
- [x] `bundle_cache_key()` — MD5 캐시 키 (Phase 2 에서 사용)
- [x] `_slice_trigger_jsx()` — self-closing 처리 fix (outer `</div>` 안 잡힘)
- [x] 사용자 FAB 케이스 E2E: handler + helper (handleCleanEQID) + action
      (loadingDefaultParam) 3-chain + setState 5 writes + URL 1개 정확

### 진행 중: search panel — cascading clear 검출 (FAB→Team→SDPT 계층)

사용자 보고: FAB / Team / SDPT 참조관계가 동작·유효성 컬럼에 안 잡힘.
실제 onChange handler 안 setState 가 다른 field 들을 undefined 로 초기화
하는 패턴이라 parser 가 deterministic 하게 추론 가능.

예::

    handleFabChange = (event) => {
      handleLoadingDefaultParam(event, '', '', '');
      this.setState({
        fab: event,
        team: undefined, sdpt: undefined, fl: undefined, model: undefined,
      });
      this.handleCleanEQID();
    }

→ FAB.action: "변경 시 Team, SDPT, FL, Model 초기화"
→ Team/SDPT/FL/Model.validation_rule: "FAB 변경 시 자동 초기화 (의존)"

- [x] `FormField.change_handler` 필드 추가 (내부용 — onChange leaf name)
- [x] `_extract_handler_leaf()` — `{this.X}` / `{X}` / `{X.bind(this)}` /
      `{(e)=>this.X(e)}` 4 변종 처리
- [x] `_SETSTATE_BODY_RE` + `_CLEAR_KV_RE` — undefined/null/''/false/[]
      검출
- [x] `_detect_cascading_clears()` — 모든 field 의 onChange handler 찾아
      cleared field 자동 매핑. parent.action / child.validation_rule
      양쪽에 cascading 정보 채움.
- [x] Phase 2a / 2b 끝에 post-process 호출 (file_sources dict 같이 넘김)
- [x] helper 7케이스 + setState 검출 unit test 전부 ✓

### 진행 중: search panel 9컬럼 — LLM 값 보존 + Popover UI + 옵션 prop 추출

PR #263 후속, 사용자 보고 3가지:
1. 유효성 규칙 / 동작 LLM 칸이 결과에서 빈 채 — `_parser_fill_layout`
   이 layout.search_panel 통째로 덮어쓰면서 LLM 값 손실.
2. Popover UI 타입이 Select 로 잘못 분류.
3. Select(Single) 의 `options=[{value, label}]` prop 형태에서 옵션 값
   추출 안 됨 — children `<Option>` 만 보던 한계.

Fix:
- `_parser_fill_layout` 에 LLM 값 라벨 매칭 보존 — `validation_rule` 은
  LLM 만 채울 수 있는 값이라 그대로 사용. `action` 은 LLM cascading 설명
  있으면 우선 + 옵션 list 보강.
- `_infer_form_ui_type` 에 `_POPOVER_TAG_KEYWORDS` 분기 (Select 보다 먼저
  매칭) — Popover / Popconfirm / PopoverSelect 등.
- `_is_keyboard_input` Popover 도 false (타입·길이 비움).
- `_compose_form_action` Popover 도 옵션 list 채움.
- `_extract_dropdown_options` 보강:
  * namespaced `<Select.Option>` (Ant Design) suffix 매칭
  * children 으로 못 잡으면 `options=[{value, label},...]` prop array
    literal 도 시도 (label 우선, 없으면 value)

검증:
- helper 8종 (Popover x3 + Popover+옵션 + options prop array 4종) 모두 ✓

### 진행 중: search panel 9컬럼 화면정의서 양식 (grid 와 parallel)

사용자 요청 — 그리드 9컬럼 (#232) 와 같은 화면정의서 표를 검색 영역에도.
컬럼: No / 라벨 / 타입 (keyboard input 만) / 길이 (keyboard input 만) /
필수 (필수·선택) / 기본값 (placeholder 우선) / 유효성 규칙 및 비고 (LLM) /
UI 타입 (Select(Single) 등) / 동작 (단순 dropdown=옵션 줄바꿈, cascading=LLM).

- [x] `FormField` 모델 — placeholder / max_length / input_data_type /
      ui_type / action / validation_rule 6 필드 추가 (default "")
- [x] `_infer_form_ui_type()` — Select(Single/Multi) / Text Field(Basic/
      Search Box) / DatePicker / Date Range / Checkbox / Radio Group /
      Number Field / Password / Text Area 휴리스틱
- [x] `_input_data_type()` — keyboard input 만 String / Number / Date
- [x] `_compose_form_action()` — 단순 dropdown 이면 옵션 줄바꿈 (전체\nY\nN)
- [x] `_extract_field_from_item()` + sibling-label 경로 — 새 필드 채움
- [x] `excel_writer._rows_for_form_fields` — 12컬럼 (화면명 + 9 양식 +
      필드명 + 소스파일)
- [x] `ScreenField` (legacy_screen_extractor) — 6 필드 추가
- [x] `_SYSTEM_PROMPT` — search_panel JSON schema 9컬럼 가이드 + cascading
      검증 규칙 / 동작 LLM 가이드
- [x] `_parse_layout_dict` — LLM 응답 새 필드 파싱
- [x] `_parser_fill_layout` — 파서 결과로 새 필드 덮어쓰기
- [x] `_render_search_table()` — HTML 9컬럼 표 (bullet list 와 같이 emit)
- [x] `SCREEN_SCHEMA_VERSION v8 → v9` (캐시 무효화)
- [x] helper unit test 11종 모두 ✓
- [x] `user_manual.html` 재빌드 (CLAUDE.md rule 5)

### 진행 중: wrapper 컴포넌트 event + nested Button 라벨 추출

사용자 보고: ``<Upload onFileUploaded={...}><Button>Upload 생성</Button>
</Upload>`` 패턴에서 라벨 빈 채로 emit. 한국 SI 흔한 패턴 — wrapper
컴포넌트에 event 가 달리고 실제 표시 버튼은 children. ``_extract_event_label``
의 step 1 이 직속 children 텍스트만 보고 nested ``<Button>...</Button>``
은 안 봐서 빈 라벨.

- [x] `_extract_nested_label()` 헬퍼 — opening tag 따라 최대 depth 3 까지
      들어가서 leaf text 추출. closing tag / self-closing / depth 초과
      시 빈 문자열 반환.
- [x] step 1 (직속 children) 이 빈 경우 fallback 으로 `_extract_nested_label`
      호출 — aria-label/label prop 보다 먼저 (가시 텍스트 우선).
- [x] fixture: 사용자 케이스 + depth 2 (div 중간) + 회귀 직접 onClick +
      빈 wrapper 모두 정확.

### 진행 중: action body 안 nested `type:` 짧은 형 우선 매칭 버그

사용자 진단 결과 saga chain step 5 에서 매핑 못 찾음. 원인: action 함수
body 안에 nested ``meta: { type: 'X' }`` 같은 짧은 type 이 외곽 ``type:
constants.X_SAGA`` 보다 먼저 등장하면 `_ACTION_TYPE_RE.search()` 가 첫
매치만 반환해서 짧은 형이 잡힘. saga 는 긴 형을 listen 중이라 매핑
끊김.

- [x] `_collect_action_to_type` 가 multi-value (set) 로 변경 — body 안
      모든 type 후보 수집. resolver 가 saga 에 hit 하는 것 하나라도
      있으면 URL 가져옴 (false positive 는 자동 필터).
- [x] `_resolve_saga_urls_for_handler` 시그니처 / 매칭 로직 set 호환.
- [x] `diag_saga_chain.py` Step 4 도 multi-value 표시.
- [x] `_ACTION_TYPE_RE` / `_SAGA_TAKE_RE` prefix 에 `Constants` /
      `Types` / `ActionType` 등 PascalCase 도 인식 (한국 SI 자체 모듈
      이름 대문자 사용 케이스 대응).
- [x] fixture: nested meta.type + 외곽 type_SAGA → 최종 URL 정확
      추출. 기존 3 패턴 (this.props.X / destructure / propTypes) 회귀 OK.

### 진행 중: propTypes 선언 + 직접 호출 패턴 (`X(param)`) saga chain 인식

사용자 보고: Search 버튼 backend URL 빠짐. 진단 결과 `const {X} = this.props;
X(param)` 또는 `X(param)` 직접 호출 (propTypes 에만 선언) 패턴에서 chain
끊김. resolver 의 `_extract_destructured_props` 는 `const {X} = this.props`
만 잡고 propTypes-only 패턴은 못 잡음.

- [x] `_PROPTYPES_BLOCK_RE` + `_PROPTYPES_KEY_RE` regex 추가 (top-level
      key 만, nested `shape({...})` 등 1-depth mask 로 false 방지)
- [x] `_extract_proptypes_names()` 함수
- [x] `_resolve_saga_urls_for_handler` 에서 destructured + propTypes
      union 으로 prop_candidates 구성
- [x] `diag_saga_chain.py` Step 2 도 같은 로직 — destructure / propTypes
      / this.props.X 3종 분리 표시
- [x] fixture 3종 E2E (this.props.X / destructure / propTypes-only)
      모두 ✓ — URL 정확히 추출

### 진행 중: sibling label depth 5 + 우선순위 + JSX tag 보존

PR #228/#232 후속 — 사용자 환경에서 검색 라벨 여전히 "Select 하세요"
로 나옴. 두 가지 원인:
1. `_sibling_label` ancestor depth 가 2 라 `<div.search-item ><span.search
   -label> + <div.search-input-wrap><span.search-select><Select/>` 같이
   3단계 wrap 케이스 못 잡음.
2. placeholder 가 sibling label 보다 우선이라 `<Input placeholder="사번
   입력"/>` 옆에 `<span className="search-label">사번</span>` 정확한
   라벨이 무시됨.
3. ScreenField.component 에 `field_type` (소문자 분류) 가 들어가서 화면
   에 "select" 로 표시. 사용자가 보고 싶은 건 원본 JSX tag "Select".

- [x] `_sibling_label` ancestor depth 2 → 5
- [x] 라벨 우선순위 재정렬 — label prop > sibling label > placeholder /
      title / aria-label
- [x] `FormField.jsx_tag` 신규 — 원본 JSX 컴포넌트 이름 보존
- [x] `_parser_fill_layout` 변환 — ScreenField.component 에 jsx_tag
      우선 매핑 (fallback: field_type)
- [x] fixture (depth 3 wrap + sibling > placeholder + placeholder
      fallback) E2E + 기존 회귀 OK
- [ ] PR + squash-merge

### 진행 중: 그리드 HTML/xlsx 9컬럼 화면정의서 양식

PR #231 후속 — 그리드 추출은 되는데 사용자가 화면정의서 표 양식
요청. 9컬럼: NO / 필드명(영문) / 필드설명 / 타입 / 필수여부 / 속성 /
UI타입 / 설명 / 동작.

- [x] `GridColumn` 모델 + `extract_grid_columns` 매핑에 required /
      editable / ui_type / description / action 5개 필드 추가 (default
      값으로 backwards-compat)
- [x] `_infer_ui_type()` — cellRenderer / cellEditor / type / cellDataType
      휴리스틱 union 매핑 (Dropdown / DatePicker / Number Field /
      Checkbox / Link-Button / Text Field(Basic))
- [x] `_compose_attribute()` — visible + editable → I/O/R/E/H 조합
      (기본 "O/R", editable=true → "O/E", hide → "H")
- [x] `_is_visible` 에 ag-grid `hide` 키도 인식 추가
- [x] `_COL_DESC_KEYS` / `_COL_ACTION_KEYS` alias union
- [x] `legacy_screen_extractor.TableColumn` 7개 필드 추가
- [x] `_render_table` — 9컬럼 표 (NO/영문/한글/타입/필수/속성/UI/설명/동작)
- [x] `_SYSTEM_PROMPT` 9컬럼 schema + ag-grid prop 매핑 가이드
- [x] `_parse_layout_dict` LLM 응답 새 필드 파싱
- [x] `screen-spec` xlsx "그리드컬럼" 시트 13컬럼 (기존 9 → 새 양식)
- [x] `SCREEN_SCHEMA_VERSION v4 → v5` 캐시 무효화
- [x] fixture (`org/score/evalDate/remark` 4컬럼 RichScreen) E2E +
      sibling label / identifier const / state.columnDefs 회귀 OK
- [ ] PR + squash-merge

### 진행 중: React class state.columnDefs 패턴 해석

PR #229/#230 진단 스크립트로 사용자 케이스 확정 — `<AgGridReact
columnDefs={this.state.columnDefs}/>` 패턴. React class component 의
``state = { columnDefs: [...] }`` 또는 ``constructor`` 의 ``this.state
= { columnDefs: [...] }`` 안에서 array literal 을 추출해야 함. 우리
파서가 array literal / identifier 만 잡고 member_expression 미지원이라
0건.

- [x] `_member_chain()` — member_expression chain → 식별자 list
- [x] `_resolve_class_state_key()` — class field 또는 constructor 의
      assignment 에서 state 안 key 의 RHS array literal 노드
- [x] `_object_pair_value()` — object literal key 검색 헬퍼
- [x] `_resolve_array_in_closure` 에 member_expression 분기 추가
- [x] fixture 2종 (class field state / constructor this.state) E2E OK
- [x] 회귀 (sibling label / identifier const) OK
- [ ] PR + squash-merge

### 진행 중: `diag_screen_pattern.py` — 화면 패턴 자가진단 스크립트

PR #228 후속. 사용자 환경에서 PR #226~#228 fix 후에도 그리드 / 조회영역
0건 — 단방향 폐쇄망이라 진단 출력 받기 어려움. 사용자가 진단 스크립트
한 번 돌리면 프로젝트 패턴을 알파벳 선택지 (a/b/c/d) 로 정리, 사용자가
`Q1=c, Q2=a, Q3=b` 같은 단답만 회신.

- [x] root 에 `diag_screen_pattern.py` — argparse + 프로젝트 walk + JSX
      컴포넌트 frequency + table/input 분류 + 컬럼 prop alias 통계 +
      라벨 패턴 (prop / sibling-class / text-sibling / none) frequency
- [x] Q1 그리드 / Q2 컬럼 prop / Q3 라벨 / Q4 entry 구조 4개 질문
- [x] fixture 3종 (sibling-label / ag-grid columnDefs / RealGridReact +
      커스텀 MyInput) 진단 결과 정확 식별 확인
- [ ] PR + squash-merge

### 진행 중: 한국 SI sibling-label 패턴 + 진단 로그

PR #226/#227 후속. 사용자 추가 케이스 — 검색 라벨이 별도 형제 span
(`<span className="search-label">FAB</span> + <Select defaultValue="Select
하세요." .../>`) 에 있을 때 파서가 label="" 로 추출 → LLM 이 default 값을
라벨로 오인. 그리드 못 잡는 화면에서 어디서 빠지는지 visibility 부족.

- [x] `extract_form_fields` — `_sibling_label()` 휴리스틱 추가 (1-2단계
      ancestor 안에서 ``className`` 에 'label' 포함된 형제 element 의
      text child 를 라벨로). props label 비어있을 때만.
- [x] LLM `_SYSTEM_PROMPT` — 한국 SI 형제 라벨 컨벤션 hint + ag-grid
      columnDefs / RealGrid schema 등 컬럼 prop alias 명시 + default 를
      라벨로 혼동 금지 룰
- [x] `_dump_screen_diagnostic` — search/grid 둘 다 0 인 화면에서 closure
      안 발견된 table-like / input-like 후보 + 커스텀 대문자 컴포넌트
      top5 frequency 콘솔 1줄 dump (사용자가 patterns.yaml 에 추가할
      후보 즉시 식별)
- [x] 통계 로그에 `empty=N` 추가
- [ ] PR + squash-merge

### 진행 중: `extract-screen-layout` 출력 경로 — `<reponame>_<시분초>/` 평탄화

`screens/<시분초>/src/*.html` → `screens/<reponame>_<시분초>/*.html`.
`write_screen_html_files` 의 top-folder 분리 (file_rel 첫 segment) 제거 +
frontend_dir basename 을 timestamp dir 에 prefix.

- [x] `main.py` `_run_frontend_only` — `screens_dir` 에 reponame 합성
- [x] `oracle_embeddings/legacy_analyzer.py` — 동일 합성
- [x] `write_screen_html_files` — top-folder 분리 제거, flat 저장

### 진행 중: `--extract-screen-layout` 조회영역/그리드 빠짐 — 파서 기반 추출

`--extract-screen-layout` 가 search_panel / data_table_columns 를 LLM 응답에만
의존해서, 자식 컴포넌트 분할 화면(예: `index.js` 가 `<PropsRouter
component={Sviddeling}/>` 만 렌더, Sviddeling 이 다시 `<SearchSection/>` /
`<DataGrid/>` 로 분할) 에서 통째로 빈 배열. `--closure-llm` 켜도 depth 2+ 가
signature 모드라 JSX skeleton (`<Table columns={...}/>`) 만 LLM 한테 도달 →
컬럼/필드 손실.

- [x] closure depth 완화 — `DEFAULT_DEPTH_MODE` 의 depth 2 도 full 로
      (token budget 으로 자연 cap)
- [x] `extract_screen_layouts()` 에 파서 기반 fill 추가:
      - tree-sitter 있으면 closure 빌드(이미 있으면 재사용)
      - `screen_spec.extractors.extract_form_fields` /
        `extract_grid_columns` 호출 → 결과로 search_panel /
        data_table_columns 덮어쓰기 (events 와 동일 원칙)
      - 파서 0건이면 LLM 결과 유지 (회귀 회피)
      - tree-sitter 미설치면 기존 LLM-only 경로 (회귀 0)
- [x] `_resolve_array_in_closure` 신규 — `columns={X}` 의 X 가 별도 파일
      const 일 때 closure 전체에서 해석 (사용자 실무 패턴)
- [x] 통계 로그 — `parser_screens / parser_fields / parser_grids` count
- [ ] PR + squash-merge

### 진행 중: catch-all SPA — Layer 2 base prefix 매칭 fallback

사용자 환경 4: 라우터가 메인 (Layer 2 routes/index.js) 에만 1개 존재하고
sub-route 가 없는 catch-all SPA. ``<Route path={fn(basename, '/')}>`` 한
줄이 sub-app build 1개의 모든 화면을 컴포넌트 안에서 routing. react_url_map
에 base ``/apps/<name>`` 만 등록 → ``/apps/<name>/list`` 같은 메뉴 URL 은
정확 매칭 실패. PR #200/#201 fix 후에도 일부 row 의 frontend_project /
presentation_layer 가 비어있던 진짜 원인.

- [x] `_lookup_react_entry_by_prefix(react_url_map, menu_url_norm)` 헬퍼
      신규 — longest base prefix match. ``/`` base 는 false-match 위험으로
      제외
- [x] `_menu_only_row` (line 1497) 에 fallback: 정확 매칭 실패 시
      longest-prefix lookup
- [x] `_build_row` 호출 site (line 2268~) 에 fallback 2 곳 — react_url_map
      및 by_frontend bucket map 둘 다
- [x] mock 검증 6 케이스: 정확 매칭 / sub-route / longest-match
      (hypm-foo vs hypm-foo-bar) / 매칭 없음 / `/` base 제외
- [ ] PR + squash-merge + local cleanup

### 진행 중: `<Route path={fn(...)}>` dynamic JSX expression 매칭

사용자 환경 3: `routes/index.js` 가
`<Route path={getRoutePath(basename, '/')} component={Main}>` 형태로 path 를
JSX expression 으로 만든다. 기존 `_ROUTE_JSX_RE` 는 `path="literal"` /
`path='literal'` 만 매칭 → dynamic path 0건 추출 → 메뉴 매칭 전부 실패.
PR #200 의 auto route_prefix 도 prepend 할 대상이 없어 무용지물.

- [x] `_ROUTE_JSX_RE` + `_build_route_jsx_re` 에 dynamic 분기 추가:
      `path={...}` 안 JSX expression 캡처 (`path_expr` 그룹). 중첩
      brace 없는 단순 expression 만 — 정적 해석 안전성 우선
- [x] `_resolve_path_expr(expr)` 헬퍼 — 함수 호출 (`fn(arg, '/list')`)
      의 마지막 path-like quoted / template literal (`` `/x/${id}` `` →
      `/x/{p}`) / 변수만 (해석 불가) 케이스 처리
- [x] `_extract_routes_from_content` 후처리: literal 미매칭 시
      resolver 호출, 해석 실패면 skip
- [x] mock 검증: dynamic `getRoutePath(basename, '/')` + dynamic
      `getRoutePath(basename, '/detail/:id')` + literal `/static/literal`
      모두 추출, `.env` REACT_APP_NAME 으로 합성된 prefix 적용 OK
- [x] resolver 단위: 함수호출 path / template literal / 변수만 3 케이스
- [ ] PR + squash-merge + local cleanup

### 진행 중: SPA basename 동적 합성 — `.env REACT_APP_NAME` 자동 prefix

사용자 환경 2: src/index.js 에서 `basename = `/apps/${REACT_APP_NAME}` `
형태로 react-router basename 을 `.env` 값으로 동적 합성하는 SPA. scanner
는 `<Route path="/">` literal 만 보므로 메뉴 URL (`/apps/<slug>/page`) 과
매칭 실패. patterns.yaml 의 `url.react_route_prefix` 수동 설정은 멀티
레포에서 번거로움 — `.env` 자동 추출이 frictionless.

작업 항목:

- [x] `legacy_react_api_scanner.load_react_app_name(root)` 추가 —
      `.env*` 의 `REACT_APP_NAME=<slug>` 한 줄 추출 (없으면 None)
- [x] `legacy_frontend.build_frontend_url_map` 에 auto-prefix:
      `route_prefix=None` 이면 `.env` 의 REACT_APP_NAME 으로
      `/apps/<name>` 자동 합성. patterns 의 명시 prefix 가 있으면
      그게 우선 (override 가능)
- [x] multi 모드는 별도 처리 없음 — bucket loop 가 각 child 의
      `build_frontend_url_map(child, route_prefix=None)` 을 부르면
      bucket 별 `.env` 자동 추출
- [x] mock fixture 검증: `.env` 있는 SPA + Route `/` → 합성된
      `/apps/<slug>` 매칭 / 명시 override 우선 / `.env` 없는 케이스
      literal 그대로 (회귀 없음)
- [ ] PR squash-merge + local cleanup

### 진행 중: 화면 UI 정의서 추출 — `screen-spec` (AST 패턴, deterministic)

`screen-converter` 가 LLM 변동성으로 매번 다른 결과를 내는 한계를
해결하기 위해, 화면 정의서 (검색조건/그리드/탭/이벤트+플로우/검증)
는 LLM 없이 AST 패턴만으로 추출. 같은 소스 → 같은 산출물 보장.
PPTX 설계서에 **시트 단위 복사·붙여넣기** 워크플로우 전제.

- [x] `oracle_embeddings/screen_spec/` 패키지 신규 — models / extractors
      / flow_tracer / excel_writer / __init__
- [x] `extract_screen_spec(closure)` — closure 모든 파일에서 5종 패턴
      추출 (LLM 0):
      * 검색 필드: `<input>`/`<Select>`/`<DatePicker>` props
      * 그리드 컬럼: `<Table columns={...}>` array (const reference 해석),
        hidden / sortable / width 인식
      * 탭: `<Tab>` `<TabPanel>` props
      * 이벤트: `<Button onClick={fn}>` → handler 본체 traversal →
        API 호출 + navigate / window.open / history.push 순서 step 리스트
      * 검증: 인라인 props + yup/zod/joi schema chain
- [x] `flow_tracer.trace_flow_in_node` — named handler OR inline arrow
      OR `() => fn(x)` 패턴 모두 처리
- [x] `excel_writer.write_master_xlsx` — openpyxl 7시트 (개요 / 검색조건
      / 그리드컬럼 / 탭 / 이벤트 / 검증규칙 / 이벤트플로우), freeze panes,
      header 스타일, 자동 컬럼 너비, PermissionError 친절 메시지
- [x] main.py `screen-spec` 커맨드 — `--captures-dir` / `--frontend-dir`
      / `--patterns` / `--source-mapping` / `--output`
- [x] e2e 합성 React tree (index + SearchPanel + OrderTable + Buttons) →
      3/4/3/7 추출 + 7시트 xlsx 생성 검증
- [x] determinism: 동일 입력 3회 → JSON byte-identical 확인
- [x] README 15번 + CLAUDE.md 커맨드 표 갱신 (23→24)
- [ ] 사용자 PC 실 React 레포로 검증
- [ ] PR + squash-merge + local cleanup

후속:
- (선택) Layer 3: `--narrative` 로 LLM 이 step list → 자연 문장 (deterministic
  structured input 으로 variance 최소화)
- patterns.yaml 의 `react.screen_spec` 슬롯에 사내 컴포넌트명 자동 발견
  (discover-patterns 확장)
- i18n key resolution (`t('orderNo.required')` → 실제 메시지)
- 조건부 컬럼 두 분기 모두 추출

### 진행 중: 화면변환기 PoC — `screen-converter` (AS-IS 캡처 → TO-BE PPTX)

소스 없는 화면 (외주 모듈/레거시 ASP/JSP) 대상 캡처본 → TO-BE PPTX
도형 자동 생성. DRM 잠긴 PPT 템플릿은 캡처 이미지로 VLM 에 첨부.
복잡한 파이프라인 통합/캐시/메뉴매핑 없는 단순 PoC — 동작 확인 후 후속.

- [x] `requirements.txt` 에 `python-pptx>=0.6.23` 추가 (폐쇄망 wheel
      가이드 주석)
- [x] `oracle_embeddings/screen_converter.py` 신규 — `extract_layout`
      (VLM 1회 호출, `legacy_pattern_discovery._call_llm(image_paths=)`
      재사용) + `render_pptx` (도형/표/버튼 헬퍼) + `convert` 엔트리
- [x] `main.py` 에 `cmd_screen_converter` + 서브파서 (`--captures-dir`
      / `--templates-dir` / `--output`) + 디스패처 등록
- [x] 렌더 smoke test: 2 슬라이드 mock layout → 17/9 shapes 정상
- [ ] 사용자 PC 에서 실 캡처로 E2E 확인 (AS-IS + 템플릿 캡처)
- [ ] PR + squash-merge + local cleanup

후속 (PoC 동작 확인 후 분리 작업):
- 해시 기반 캐시 (vision 호출 비용 절감)
- 템플릿 스타일 별도 1회 분석 → 색상/폰트 PPTX 반영
- `analyze-legacy --extract-screen-layout` ScreenLayout JSON 직접 입력
  모드 (React 소스 보유 화면은 VLM 재호출 불필요)
- `convert-menu` 산출물 `input/menu.md` 의 메뉴 계층 매칭 (PROGRAM_ID
  → TO-BE 화면 타이틀)

### 진행 중: Mermaid 시퀀스 다이어그램 — Phase B (alt/else/loop 블록)

Phase A 위에서 제어 블록 (if/else/switch/for/while/do-while/try-catch-finally)
을 brace walker 로 추출해 Mermaid ``alt/else/loop/opt/end`` 래핑 자동
생성. LLM 없이 파서만으로.

작업 항목:

- [x] `legacy_java_parser._extract_control_blocks(body)` — 재귀적 블록
      추출. chain_id (if-else 체인 / try-catch 체인) + chain_index +
      depth 포함. 5/5 단위 테스트 PASS (if-elseif-else / nested / try-
      catch-finally / do-while / switch)
- [x] `_extract_method_bodies` 에서 method dict 에 `body_control_blocks`
      필드 부착. 파싱 실패 시 빈 리스트로 fallback (Phase A 회귀 없음)
- [x] `trace_chain_events._context_for(off)` — offset 을 감싸는 블록
      리스트 반환 + method_key prefix 로 block_id unique 화 (method
      경계 넘어도 sibling 오판 방지)
- [x] 각 event 에 `context_stack` 필드 부착 (call / sql / rfc 모두)
- [x] `legacy_mermaid._emit_transition(prev, curr, lines)` — context
      전환 감지 + close-sibling-open 3단 처리. sibling 체인 유지 시
      `end` 대신 `else <label>` emit
- [x] 블록 kind → Mermaid 매핑: if→alt, else_if/else→else, for/while
      →loop, do_while→loop do-while, switch→alt switch(..), try→opt
      try, catch→else catch, finally→else finally
- [x] Phase A 회귀: 제어 블록 없는 mock_crud 에서 기존 출력 동일
- [x] Phase B end-to-end: if/else + for 있는 새 mock 에서
      ``alt cond / MyService->>Mapper / else / ... / end / loop cond / ...``
      정상 emit 확인
- [x] conventional commit + PR + squash-merge

### 진행 중: Mermaid 시퀀스 다이어그램 — Phase A (call offset + 순서)

사용자 요구: analyze-legacy 결과에 endpoint 별 Mermaid sequenceDiagram
을 추가. 컨트롤러 메서드 → 서비스 메서드 → RFC / XML / 테이블까지.
LLM 없이 파서만으로 3단계 (A/B/C) 로 구현하기로 합의.

Phase A (이 섹션) — 호출 순서 먼저:

작업 항목:

- [x] `legacy_java_parser._collect_body_field_calls` / `_body_sql_calls` /
      `_body_rfc_calls` 에 `offset` 필드 추가 (`m.start()` 저장)
- [x] `legacy_analyzer.trace_chain_events(endpoint, controller, indexes,
      mybatis_idx, rfc_depth)` 신규 — `_resolve_endpoint_chain` 과 병렬로
      체인을 walk 하면서 호출마다 event 를 발행 (kind=call/sql/rfc,
      source offset 정렬 + depth-first recurse)
- [x] `legacy_mermaid.py` 신규 모듈 — event 리스트 → Mermaid
      ``sequenceDiagram`` 텍스트. participant 자동 alias + 충돌 회피,
      DB/Mapper/SAP 는 공용 participant (이벤트에 등장 시 lazy 선언)
- [x] Markdown 리포트에 endpoint 당 ```mermaid 코드블럭 (GitHub/VSCode
      즉시 렌더) + Excel 에 `Sequence Diagrams` 시트 (6 컬럼)
- [x] CLI `--sequence-diagram` 플래그 (기본 off, Phase II 독립)
- [x] `analyze_legacy` / `analyze_legacy_batch` 에 `emit_sequence_diagram`
      파라미터 + `_build_row` 까지 forwarding. row 에 `sequence_diagram`
      필드 추가
- [x] mock 검증: `/tmp/mock_crud` 에서 User→Controller→Service→Mapper→DB
      체인이 6개 SQL 순서 보존해서 올바로 렌더. 옵트아웃 시 시트 미생성
      (회귀 없음)
- [x] conventional commit + PR + squash-merge

### 진행 중: self-call 체인에서 callee 의 body_sql_calls 유실 — 다른 agent 인수인계

**증상**: ServiceImpl 내부 `this.saveDpPubNotiInfo(param)` 같은 자기호출
이 있을 때, 최종 Programs 시트의 Tables/Columns 컬럼에 saveDpPubNotiInfo
가 만지는 테이블이 반영 안 됨. Phase A (ServiceImpl biz 추출) 가 먼저
실패해서 Phase II (Program Specification) 도 불완전.

**11번에 걸친 diag (PR #33~#46, 전부 이미 rollback/삭제됨) 로 확인된 사실**:

단일 파일 `parse_java_file(target)` 결과:
- ✓ callee (`saveDpPubNotiInfo`) methods 에 등재됨
- ✓ body_sql_calls = **14 건** (직접 SQL 호출)
- ✓ caller 의 body_field_calls 안에 `this.saveDpPubNotiInfo` 존재
- ✓ `_find_method_in_class(cls, "saveDpPubNotiInfo")` resolve OK

MyBatis 인덱스 / config:
- ✓ namespace `scp-mailing` 매칭 성공 (hyphen 이름 정상 — PR #39 로 diag
  버그 잡은 뒤 재확인)
- ✓ config.yaml legacy.rfc_depth 는 self-call 에 영향 없음
- ✓ target/classes 빌드 산출물 중복 XML 359 건 스킵됐지만 이슈와 무관
  (PR #40)

전체 backend 인덱스 (`parse_all_java` + `_build_indexes`):
- ✓ target_fqcn 이 `services_by_fqcn` 에 등재
- ✓ walker 의 class dict 안에 callee 이름 존재
- ✓ walker 의 `_find_method_in_class` trace — callee 를 찾고 method dict
  를 반환함
- 🔴 **그러나 반환된 method dict 의 body_sql_calls = 2 건** (단일파일 14 건
  과 불일치)
- ✓ callee 가 동일 이름으로 여러 개 (오버로딩) 아님 — **B 확정**
- ✓ 동일 FQCN 을 가진 class 중복 파싱도 없음 — **B 확정**

**남은 모순**: 같은 파일, 같은 FQCN, 오버로딩 없음, 중복 파싱 없음인데
단일 parse 14 vs 전체 parse 2. 이론상 불가능해 보이는 조합. 제가 놓친
state / global variable / parsing side-effect 가 있을 가능성.

**다른 agent 가 확인해볼 가설들** (우선순위 순):

1. `parse_all_java` 호출 중간에 전역 state (`_active_patterns` /
   `_NEXCORE_BASE_CLASSES` 등) 가 변하면서 같은 파일의 후속 파싱이 달라지는
   가능성. 실제 analyze-legacy 흐름은 `apply_patterns()` 를 호출하는데
   diag 는 호출 안 함 — 그래도 14 vs 2 관찰됐으므로 아닐 수도 있지만
   재확인.

2. 단일파일 `parse_java_file` vs 전체 `parse_all_java` 에서 호출되는
   `parse_java_file` 결과가 객체 단위로 다를 수 있는지. 동일 파일
   경로를 양쪽에서 호출해 `id(result)` / body_sql_calls 길이를 직접
   비교.

3. `_build_indexes` 가 class dict 를 저장하면서 methods 리스트를 어딘가
   필터링/치환할 가능성. 저장 전/후 `id(c)` 및 `id(c["methods"])` 비교.

4. saveDpPubNotiInfo 가 호출되는 라인 주변에 `_extract_method_bodies`
   의 brace walker 를 속이는 특수 문자 조합 (유니코드 이스케이프,
   text block, 에스케이프된 문자열 안의 `{/}/"/'`). diag [5] 는
   300자만 스캔해서 놓쳤을 가능성.

5. `_collect_body_sql_calls` 의 `_SQL_CALL_RE` 가 method body 의 SQL
   14 건 중 12 건을 놓치는 특정 문법 패턴 (예: ``sqlSession.selectList(
   namespace + ".id")`` 가 아니라 ``selectListSafe()`` 같은 커스텀 메서드
   호출). apply_patterns 로 주입되는 커스텀 receivers 가 단일파일에는
   적용되지만 전체 파싱엔 안 될 가능성.

**재현 환경**: 실제 사용자 PC (폐쇄망 Windows). 사용자 PC 접근 필요.
사용자 PC 에서 `parse_java_file(해당_Service.java)` 와
`parse_all_java(backend_root)` 두 호출을 같은 Python 프로세스에서 연속
실행하고 같은 파일의 method dict 객체 비교 (`id()`, methods[i].body_sql_calls
len) 해서 어느 경로가 body_sql_calls 를 탈락시키는지 직접 확인 필요.

### 진행 중: Phase II — endpoint narrative LLM (Program Specification 시트)

사용자 원문 요구 직접 해소: "프론트 조회 버튼 클릭 → 비즈니스 로직 →
어떤 테이블·컬럼 / 저장 버튼 → validation → DML column" narrative 를
endpoint 당 한 줄로 자동 생성.

Phase A (backend ServiceImpl biz summary) + Phase B (frontend handler
summary) 결과와 Phase I 의 column_crud / frontend_trigger 를 **원본 body
재전송 없이 조립** 해서 LLM 에 구조화 JSON 요청. 중복 LLM 호출 회피 +
token 절감.

옵트인 플래그 `--extract-program-spec` (`--extract-biz-logic` 의존).
결과는 신규 `Program Specification` 시트에 endpoint 당 한 행.

작업 항목:

- [x] `legacy_biz_extractor.py` 하단에 Phase II 섹션 추가 (~300 라인):
      `EndpointSpec` dataclass / `ENDPOINT_SPEC_SCHEMA_VERSION` /
      `_ENDPOINT_SPEC_SYSTEM_PROMPT` / `_ENDPOINT_SPEC_USER_PROMPT_TEMPLATE` /
      `_make_spec_key` (SHA-256) / `_parse_column_crud_cell` /
      `_build_spec_batch_prompt` / `_filter_spec_targets` (column_crud
      부분집합 강제) / `_parse_spec_batch` / `_format_targets` /
      `_infer_trigger_type_from_row` (LLM down fallback) /
      `_spec_cache_get/put` (디스크 캐시) / `extract_endpoint_narrative` /
      `enrich_rows_with_endpoint_spec` / `program_spec_sheet_rows`
- [x] `analyze_legacy` / `analyze_legacy_batch` 에 `extract_program_spec`
      파라미터 추가. Phase A/B 실행 직후 Phase II 호출. 결과 dict 에
      `endpoint_spec_map` 노출 (batch 은 `all_endpoint_spec_map` 로 merge)
- [x] `main.py` CLI `--extract-program-spec` 신규 플래그 +
      `--extract-biz-logic` 없이 사용 시 에러 + 양 경로 (single / batch)
      forwarding
- [x] `legacy_report.py` `_write_program_spec_sheet` 신규 — 15 컬럼
      `(Main, Sub, Tab, Program, HTTP, URL, Trigger label, Trigger type,
      Input fields, Validations, Business flow, Read targets, Write
      targets, Purpose, Source)`. single + batch 양쪽 call site 에
      emit. `endpoint_spec_map` 비었으면 시트 생성 skip (회귀 없음)
- [x] pilot mock (`/tmp/mock_crud` + terms):
      * LLM down → `source=fallback`, trigger_type=COMPOSITE 추론,
        write_targets 는 column_crud 에서 deterministic 채움, narrative
        공백, Program Specification 시트 1행 생성 ✓
      * LLM mocked (mocked `_call_llm` 반환) → `source=llm`, 모든 narrative
        필드 정상 채움, hallucinated `FAKE.FAKE(U)` / `CMN_BTN_ROLE.ROLE(R)`
        는 후처리 filter 에서 drop 확인 ✓
      * `--extract-program-spec` 없이 돌리면 "Program Specification"
        시트 미생성 (회귀 없음) ✓
      * CLI 가드: `--extract-program-spec` 단독 사용 시 에러 exit code 2 ✓
- [x] README: Sheet 목록에 "Business Logic / Frontend Logic / Program
      Specification" (opt-in) 추가 + Program Specification 사용 예시 +
      15 컬럼 설명 + LLM down fallback 동작 설명
- [x] conventional commit + PR + squash-merge

---

## 12. SQL Migration — `convert-mapping` / `migration-impact` / `migrate-sql` / `validate-migration`

스펙: `docs/migration/spec.md`. DSL 우선 → LLM fallback → 수동 큐 3-tier
+ Stage A (sqlglot static) / Stage B (TO-BE DB parse) 2-stage 검증.

### 진행 중: WHERE `=` 정렬 + `<bind/>` self-closing 유지

- [x] `_realign_equals` — 연속 `컬럼 = 값` 줄의 `=` 를 표시폭 정렬 (컬럼
      rename 으로 lhs 폭 바뀐 경우 교정). `<=`/`>=`/`<>`/`!=` 제외.
      `_emit_sql_fragment` + `_reindent_body` 양쪽 적용
- [x] `<bind>`/`<include>` 를 `_INLINE_TAGS` 로 분리 — 블록 동적태그처럼
      `.text` 설정 안 함 → `<bind .../>` self-closing 유지 (기존엔
      `<bind></bind>` 로 펼쳐짐), SQL 본문 기준(4칸) 들여쓰기
- [x] `_emit_mixed` — 혼합 콘텐츠 일반 처리 (동적/인라인 자식 + 그 사이
      SQL 조각 전부 재들여쓰기). annotate 트리거를 "element 자식 존재" 로
      확장 (bind-only statement 도 본문 재들여쓰기)
- [x] 회귀: 단위 (=정렬/연산자제외/1줄, bind self-closing/4칸/bind.tail) +
      동적·CDATA·realign·reindent 회귀

### 보류: 다른 안전망이 있는 엣지 케이스

🟡 (실환경 드물 + 다른 검증 단계가 받쳐줌 → 운영 차단급 아님):

- [ ] **E4**. `validator_static` CTE 본문 컬럼 일괄 warning 정밀도 향상
      (Stage B `cursor.parse()` 가 실 판정이라 진짜 오타도 ORA-00904 로 잡힘
      → Stage A 리포트의 색깔 정확도 이슈만 남음)
- [ ] **E5**. `dynamic_sql_expander` Level 2 중첩 `<choose>` 대안 미탐색
      (경로 폭발 우려로 의도적 제한 — 외부 + 내부 분기 모두 컬럼 분할 매핑이
      걸리는 드문 케이스만 영향)

---

## 13. morpheme — 형태소분석

### 진행 중: 지침 템플릿 — 실제 오분해 7종 반영 + 원칙 하위규칙화

실제 LLM 돌려본 결과 아래 케이스를 기존 원칙이 못 잡음 → 원칙을 하위
규칙으로 세분화하고 Few-shot 엣지 케이스 7 추가.

수정된 오분해:

1. `1:계획, 2:요청` — 속성 전체가 코드 리스트 → **미변환** (tokens=[])
2. `1차BP담당자명` — 접미사 `명` 독립 토큰
3. `신청시작일` — `일` → `일자` 정규화
4. `협력사추천서or1-2차간거래금액증빙서류(첨부파일)` — 괄호 보충 포함 +
   `1-2차간` 복합 토큰
5. `180도소모전력전(kwh)` — `전` → `직전` + 단위 대문자 표준화
6. `3RDPARTY여부` → `제3자` (ordinal 패턴)
7. `Aging구분코드` → `에이징` (영문 음차)

작업 항목:

- [x] 원칙 1 (괄호) 5 하위 규칙 (1-A 대괄호 / 1-B 단순 코드 / 1-C 코드
      리스트 + `코드` 추가 / 1-D 단위·보충 포함 / 1-E 속성 전체 코드 리스트
      → 미변환)
- [x] 원칙 2 (영문) 4 하위 규칙 (2-A 번역 / 2-B 음차 / 2-C ordinal /
      2-D 업계 약어 원본). 업계 약어 리스트에 BP, ERP, SCM, CRM, SAP, JCO 추가
- [x] 원칙 3 (정규화) 4 하위 규칙 — 시간/날짜 대폭 확장 (일→일자, 시→시간,
      전→직전, 후→직후, 굳은 합성어 예외 명시)
- [x] 원칙 5 (한글) 3 하위 규칙 — 일반 접미사 (명, 번호, 일자, 시간, 량,
      여부, 구분, 파일, 서류, 이력 등) 독립 토큰
- [x] 원칙 6 단위 대문자 표준화 (`kwh` → `KWH`) 추가
- [x] Few-shot 13개 (기존 6 + 신규 7). 각 예시에 하위 규칙 태그 부착
- [x] Few-shot 선정원칙 — 15 하위 규칙 커버리지 매핑표 갱신
- [x] smoke test: 11,204자 / 13 예시 / 15 하위 규칙 / 프롬프트 조립 OK
- [ ] PR squash-merge + local cleanup
