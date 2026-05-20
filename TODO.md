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

_진행 중 없음_

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
