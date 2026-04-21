# TODO: menu-driven 스캔 — 메뉴 참조 앱/URL 로 프론트·체인 해석 범위 축소 (완료)

- [x] `build_frontend_url_map_multi(allowed_apps=None)` — 주어지면 해당 버킷만 스캔 (skip 로그)
- [x] `analyze_legacy` 에서 `menu_apps` = {app_key_spec 로 뽑은 menu 슬러그} 계산 → `allowed_apps` 로 frontend 빌더에 주입
- [x] `interesting_urls` = 메뉴-참조 앱 버킷들의 api_url 합집합 + 메뉴 직접 URL 집합
- [x] `--menu-only` skip 조건을 "직접 menu_entry" → "interesting_urls 에 포함되는가" 로 교체 → 2-홉 매칭된 endpoint 도 살아남음
- [x] `analyze_legacy_batch` 에서 프론트 인덱싱을 **batch 최상위로 hoist** → 백엔드 29 회 반복 스캔 제거. `precomputed_frontend` 파라미터로 analyze_legacy 에 주입
- [x] stats 카운트 버그 수정: `matched = len(rows) - len(unmatched)` 가 skip-stub 때문에 틀어지던 것을 `sum(r["matched"])` 기반으로 정확화
- [x] mock: 3 앱 중 2 앱만 메뉴에 있을 때 1 앱 스캔 스킵 + Endpoints: 3 (matched: 2, unmatched: 1). batch mock: 프론트 한 번 스캔 + be1 만 매칭
- [x] conventional commit + `claude/push-previous-changes-4P5x8` push

---

# TODO: LLM JSON 파싱 실패 대응 — 2-call 분리 + 대표 레포 지정 (완료)

- [x] `_call_llm` 이 JSON 파싱 실패 시 원본 응답을 `output/legacy_analysis/pattern_llm_raw_<label>.txt` 로 덤프
- [x] JSON 추출 강화: 코드펜스 없으면 첫 `{` ~ 마지막 `}` 사이를 슬라이스
- [x] `discover_patterns` 를 2-call 로 분리 — 백엔드 / url+frontend. 한쪽 실패해도 다른 쪽은 진행
- [x] timeout 180s → 300s 상향
- [x] `_pick_representative_frontend` 신규: frontends_root 하위에서 가장 큰 레포 자동 선택
- [x] `--frontend-dir` CLI 인자 추가: 사용자가 명시적으로 대표 레포 지정 가능. 기본은 frontends-root 에서 auto-pick
- [x] `_sample_frontend_for_pattern` 가 단일 레포만 샘플링 → 29 앱 monorepo 에서도 프롬프트 사이즈 통제
- [x] smoke: LLM down 상태에서도 url heuristic + frontend 기본값으로 patterns.yaml 산출

---

# TODO: 2홉 매칭 (Menu → Frontend → API call → Controller) + frontend 패턴 LLM 학습 (완료)

- [x] `legacy_pattern_discovery._DEFAULT_PATTERNS`에 `frontend` 섹션 (router_files / route_library / api_call_methods / api_url_const_files / button_components / button_label_props)
- [x] `discover-patterns` 가 `--frontends-root` 주어졌을 때 프론트 샘플(package.json, 라우터 후보, 대표 컴포넌트) 을 LLM 에 추가로 던져 `frontend` 섹션 채움
- [x] heuristic fallback: deps 에서 react-router-dom 버전 / 파일명 컨벤션으로 router_files 추정
- [x] `oracle_embeddings/legacy_react_api_scanner.py` 신규 — React 소스에서 API URL 호출 추출 + URL 상수 2-pass + 템플릿 리터럴 정규화 + onClick 핸들러→라벨→URL 페어링
- [x] `build_frontend_url_map_multi` 5-tuple 로 확장: `(merged_map, framework, by_frontend, api_by_frontend, triggers_by_frontend)`
- [x] `legacy_analyzer` 매칭 재설계: endpoint.url 로 api_by_frontend lookup → 해당 app 의 메뉴로 귀속 (virtual matching)
- [x] `_build_row` 에 `frontend_trigger` (버튼 라벨) 필드 추가 + 레포트 컬럼 추가
- [x] mock 통과: menu → app_slug → React 파일 → API 호출 → controller, `Endpoints: 2 (matched: 2)` + `Trigger=조회; 초기화`. 기존 `mock_this` 회귀 정상 (NO_MENU 레이아웃 유지)
- [x] conventional commit + `claude/push-previous-changes-4P5x8` push

---

# TODO: convert-menu 에 DRM 우회용 텍스트 입력(--menu-md-in) 추가 (완료)

- [x] `menu_converter._load_rows_from_text` 신규: 파이프 테이블 / TSV / CSV 자동 감지 + 행 파싱
- [x] `convert_menu(..., text_path=...)` 분기 + xlsx/text 상호 배타 검증
- [x] `main.py convert-menu --menu-md-in <path>` CLI 인자
- [x] mock 2종 (파이프 테이블 / TSV 붙여넣기) 로 0-base depth_column 변환 통과, xlsx 회귀 유지
- [x] README Step 0 에 DRM 우회 예시·설명 추가
- [x] 커밋 & 푸시

---

# TODO: convert-menu 커맨드 — LLM 기반 메뉴 Excel → 표준 menu.md 변환 (완료)

- [x] `oracle_embeddings/menu_converter.py` 신규: merged cell forward-fill, 헤더 라인 탐지, LLM 1회 매핑(구조화 JSON 검증), heuristic fallback, 3 mode (columns_per_level / depth_column / path_column) emit
- [x] `main.py convert-menu` 서브커맨드 + `--menu-xlsx` / `--output` / `--sheet` / `--no-llm` 인자
- [x] LLM 환경: `PATTERN_LLM_*` > `LLM_*` fallback (discover-patterns와 동일 컨벤션)
- [x] mock 3종 (cols / depth / path) 변환 검증 — 전부 load_menu_from_markdown 라운드트립 통과
- [x] 기존 `input/menu_template.xlsx` 회귀 — 7행 전부 정상 변환
- [x] README에 Step 0 (convert-menu) 섹션 + 기능 요약 표 + 7단계 워크플로우 업데이트 (→ 8단계)
- [x] conventional commit + `claude/push-previous-changes-4P5x8` push

---

# TODO: Oracle MERGE 테이블 추출 버그 (완료)

- [x] 원인: `UPDATE\s+(\w+)`가 `UPDATE SET`의 `SET`을 테이블로 캡처, `MERGE INTO <tbl>`은 아예 추출 규칙 없음
- [x] fix: `_add_table` 헬퍼로 키워드·alias 필터 + INSERT/UPDATE/DELETE 모두 finditer + `MERGE INTO` / `USING` 추가 추출
- [x] 검증: MERGE + 서브쿼리 source / MERGE + 테이블 source / plain UPDATE / multi-UPDATE / INSERT·DELETE 5종 전부 통과, `SET`은 더 이상 테이블에 안 잡힘
- [x] 커밋 & 푸시

---

# TODO: URL-convention LLM 학습으로 menu ↔ React ↔ controller 매칭 고치기 (완료)

- [x] `legacy_util.normalize_url(url, strip_patterns=None)` 키워드 인자 + 컴파일 캐시
- [x] `legacy_react_router` / `legacy_polymer_router` `build_url_to_component_map(..., strip_patterns, route_prefix)`
- [x] `legacy_frontend.build_frontend_url_map_multi` 3-tuple 반환 + value에 `frontend_name` + `by_frontend` 누적
- [x] `legacy_analyzer.analyze_legacy`에서 `patterns["url"]` 로드 → 모든 normalize/builder에 주입 + `_extract_app_key` + by_frontend 우선 lookup
- [x] `legacy_pattern_discovery`: `_DEFAULT_URL_SECTION` + URL 샘플링 (menu_md + frontends_root) + LLM 프롬프트 URL 섹션 + heuristic fallback + 잘못된 regex drop
- [x] `main.py discover-patterns`에 `--menu-md` / `--frontends-root` 추가
- [x] 참고: `legacy_menu_loader`는 normalize_url을 직접 호출하지 않음 (dead한 `build_url_index` 외). analyzer 한 곳에서 strip_patterns 주입하면 충분. 로더 시그니처 변경 불필요.
- [x] mock 시나리오 3종 통과:
  - A (`/tmp/mock_urlstrip`): 메뉴 `/apps/orderweb/order/list` + React `/order/list` + `url_prefix_strip: ["^/apps/[^/]+"]` → matched=1, With React=1
  - B (`/tmp/mock_urlapp`): 멀티 레포 `foo/bar` 동일 라우트, 메뉴 `/apps/foo/...` + `app_key: {source: path_segment, index: 2}` → `foo/src/FooList.jsx` 선택 (글로벌 first-wins는 `bar`가 됨)
  - C (기존 mock_this): url 섹션 없는 patterns.yaml + `--skip-menu` → 기존 결과와 동일, `URL conventions` 로그도 나오지 않음
- [x] LLM 미연결 환경에서 heuristic fallback 정상 동작 (샘플 1개일 때 LCP = 전체 URL로 축소), save/load 라운드트립도 OK
- [x] Conventional commit 후 `claude/push-previous-changes-4P5x8`로 push

---

# TODO: this.self-call 체인 추적 (완료)

- [x] 원인: `_resolve_endpoint_chain`가 `body_field_calls`에서 `receiver=="this"` 케이스를 `_resolve_field_type_fqcn`에 통과시켜 빈 FQCN으로 continue — self helper 안의 SQL/RFC 누락
- [x] 패치: `receiver=="this"`면 `_find_method_in_class(owner, target_method_name)`로 같은 클래스 내 메서드 enqueue (depth 증가 없음)
- [x] 검증: mock_this (단일 self-call 2개) + mock_this_cycle (a↔b 순환 + a→c→SQL) + mock_this_mixed (self → field → SQL) 모두 통과, Program Detail에 Tables/XML 정상 표시
- [x] 커밋 & 푸시

---

# TODO: 프로젝트 점검 (완료)

- [x] 프로젝트 구조/파일 구성 확인 (root 10 files + `oracle_embeddings/` 33 modules + `input/` 2 templates)
- [x] 핵심 모듈 상태 점검 (legacy_* / mybatis_parser) — 공유 유틸 전부 존재
- [x] main.py CLI 커맨드 등록 상태 확인 (등록 서브커맨드 17개: 단위 16 + `all`)
- [x] 최신 코드 반영 검증 — `_MYBATIS_SKIP_DIRS = {.git, .gradle, .hg, .idea, .next, .svn, node_modules}` ✔
- [x] import/syntax smoke test — 33개 모듈 중 `oracledb` 미설치 환경상 3개(db/extractor/std_data_validator) import는 불가, `py_compile` 구문 검사는 통과
- [x] 점검 결과 요약 보고

## 점검 결과 요약

- Git: 브랜치 `claude/project-review-sCuTm` 체크아웃됨, working tree clean. 최근 커밋은 `b19de9d Revise project context and command details` (CLAUDE.md 갱신)
- 코드 무결성: 핵심 공유 유틸 (`_read_file_safe`, `normalize_url`, `parse_all_mappers`, `scan_mybatis_dir`, `extract_table_usage`, `apply_patterns`, `_strip_comments`, `_strip_annotations_balanced`) 전부 정상 노출
- 메뉴 로더: Markdown/Excel/DB 3종 함수 모두 존재 (`load_menu_from_markdown`, `load_menu_from_excel`, `load_menu_hierarchy`)
- 프론트엔드: `legacy_frontend.detect_frontend_framework` + `build_frontend_url_map(_multi)` 노출 (CLAUDE.md의 "디스패처" 표현과 이름이 다를 뿐 기능은 존재)
- CLI: 16 단위 커맨드 + `all`. CLAUDE.md 표는 15종만 나열하고 본문은 "18종"으로 적혀 있어 문서-구현 카운트 불일치 (기능 누락은 아님, 문서 수치만 업데이트 필요)
- URL 정규화 smoke: `normalize_url('http://foo/Bar/:id/') == '/bar/{p}'` 정상

---

# TODO: Frontend Polymer 자동 감지 + 파서 (완료)

- [x] `legacy_polymer_router.py` 신규: custom-element 인덱스 (customElements.define / Polymer({is}) / static get is() / dom-module / 파일명 규칙) + 라우트 패턴 (vaadin-router / page.js + iron-pages / app-route)
- [x] `legacy_frontend.py` 신규: package.json 의존성 + 콘텐츠 샘플링 기반 React vs Polymer 자동 감지 + 디스패처
- [x] `legacy_analyzer.analyze_legacy` / `analyze_legacy_batch` 가 디스패처 사용 + `frontend_framework` 통과 + stats 에 기록
- [x] `main.py` 에 `--frontend-framework {auto,react,polymer}` CLI 플래그
- [x] mock Polymer 프론트 (`/tmp/mock_polymer`) 구축 + 단위 라우트 매칭 검증
- [x] mock React (`/tmp/mock_react`) 회귀 검증 + 6 기존 backend mock 회귀 검증
- [x] BLOG.md 업데이트
- [x] 커밋 & 푸시

---

# TODO: 메뉴 매핑을 Excel 파일 기반으로 전환 (완료)

- [x] `legacy_menu_loader.py` 에 `load_menu_from_excel` + `_LEVEL_KEYWORDS` (1~5레벨) + `_URL_KEYWORDS` 추가
- [x] `_row_to_entry` 가 가장 깊은 레벨을 `program_name` 으로, 첫 3개를 main/sub/tab 슬롯으로, 전체 레벨을 `menu_path` 로 보존
- [x] `main.py cmd_analyze_legacy` 에 `--menu-xlsx` 옵션 + skip > xlsx > DB 우선순위
- [x] `legacy_analyzer._build_row` row dict 에 `menu_path` 필드 추가
- [x] `legacy_report.py` Markdown / Excel 단일·배치 모드 모두 `Menu path` 컬럼 추가
- [x] `/tmp/menu.xlsx` mock 으로 단위 + end-to-end 검증
- [x] BLOG.md 업데이트 (Excel 옵션 + menu_path 컬럼 설명)
- [x] 커밋 & 푸시

---

# TODO: AS-IS Legacy Source Code Analyzer (완료)

## Phase 1 - 코어 파서
- [x] `legacy_java_parser.py` 신규 (패키지/import/스테레오타입/매핑/autowired/RFC)
- [x] `mybatis_parser.parse_mapper_file` 에 `mapper_path` 필드 추가
- [x] `legacy_analyzer.py` 골격 (컨트롤러→서비스→매퍼→테이블 체인)

## Phase 2 - 메뉴 & URL 양방향 매칭
- [x] `legacy_menu_loader.py` 신규 (DB 메뉴 트리 + URL 인덱스)
- [x] URL 정규화 공유 유틸 (`legacy_util.normalize_url`)
- [x] 양방향 매칭 (matched / unmatched / orphan)

## Phase 3 - React 프레젠테이션 레이어
- [x] `legacy_react_router.py` 신규 (라우트 스캔 + 컴포넌트 인덱스)
- [x] analyzer 에 presentation_layer 연결

## Phase 4 - RFC 추출
- [x] Java parser 의 `_extract_rfc_calls` + 2-pass 상수 해석
- [x] 서비스 체인 트랜지티브 RFC 수집

## Phase 5 - 출력
- [x] `legacy_report.py` 신규 (Markdown)
- [x] Excel 7시트 출력

## CLI 통합
- [x] `main.py` 에 `cmd_analyze_legacy` + 서브커맨드 등록
- [x] `config.yaml` 에 `legacy.menu` 섹션 추가

## 검증
- [x] mock Java/React/XML 디렉토리로 end-to-end 테스트
- [x] 기존 명령(query, erd-group 등) 회귀 없음 확인
- [x] 커밋 & 푸시

---

# TODO: 용어사전 자동 생성에 정의(Definition) 필드 추가 (완료)

## 작업 항목
- [x] terms_llm.py `_enrich_batch` 프롬프트에 정의 규칙/JSON 키 추가
- [x] terms_llm.py `enrich_terms` 응답 매핑에 `definition` 추가
- [x] terms_report.py `_md_escape` 헬퍼 추가
- [x] terms_report.py Markdown 두 테이블(Terminology, DB+FE 공통)에 Definition 컬럼 추가
- [x] terms_report.py Excel 4개 시트(용어사전/DB+FE공통/DB전용/FE전용)에 Definition 컬럼 추가
- [x] 변경 검증 (구문/임포트)
- [x] 커밋 및 푸시

---

# TODO: 버그 수정 (완료)

## Critical 🔴
- [x] Bug #1: mybatis_parser.py:240 - continue 이후 unreachable code로 JOIN 관계 전혀 추출 안 됨

## High 🟠
- [x] Bug #2: terms_collector.py:110 - 기본 dict에 fe_count/db_count 누락
- [x] Bug #3: storage.py:196 - 빈 mappers 리스트 IndexError 가능
- [x] Bug #4: vector_store.py:99 - metadatas/distances 길이 미확인

## Medium 🟡
- [x] Bug #5: sql_reviewer.py:45 - 카티시안 곱 regex 단순화
- [x] Bug #6: sql_reviewer.py:93 - UPDATE/DELETE WHERE 없음 함수 내 특별 처리
- [x] Bug #7: ddl_generator.py:125 - table["columns"] null 체크
- [x] Bug #8: erd_generator.py:53 - data_type None 체크

## Low 🟢
- [x] Bug #9: ddl_generator.py:118 - except 로깅 추가
- [x] Bug #10: erd_generator.py:84 - 중복 할당 제거

## 마무리
- [x] 자체 테스트 (Bug #1, #2, #5, #6, #8)
- [x] Commit and push

---

# TODO: SQL Migration (AS-IS → TO-BE) — docs/migration/spec.md §13 순서

스펙: `docs/migration/spec.md`. 리스크 낮은 순서로 15 단계.

- [x] Step 1: `oracle_embeddings/migration/{mapping_model.py, mapping_loader.py}` + `input/column_mapping_template.yaml` + `requirements.txt` 에 sqlglot 추가
- [x] Step 2: `migration-impact` 커맨드 — 매핑 파일 검증 + 영향 리포트만 (변환 X)
- [x] Step 3: `sql_rewriter.py` + `ColumnRenameTransformer` + `TableRenameTransformer`
- [x] Step 4: `dynamic_sql_expander.py` — Level 1 (max/min 2 경로)
- [x] Step 5: `xml_rewriter.py` — lxml 기반 구조 보존 치환
- [x] Step 6: `validator_static.py` — sqlglot static 검증
- [x] Step 7: `migration_report.py` — Excel 5 시트
- [x] Step 8: `bind_dummifier.py` + `validator_db.py` + `validate-migration` 커맨드
- [x] Step 9: 나머지 transformer 6 종 (TypeConversion / ColumnSplit / ColumnMerge / ValueMapping / JoinPathRewriter / DroppedColumnChecker)
- [x] Step 10: dynamic_sql_expander — Level 2 (컬럼 커버리지), Level 3 (foreach n=0,1,2 샘플링)
- [x] Step 11: `comment_injector.py` — 한글 주석 삽입
- [x] Step 12: XML 산출물 (AS-IS 주석 보존 + 메타데이터 블록)
- [x] Step 13: `llm_fallback.py` + `--llm-fallback` 옵션
- [x] Step 14: `migrate-sql` 커맨드 통합 + 회귀 테스트
- [x] Step 15: README / CLAUDE.md 업데이트

---

# TODO: SQL Migration — 코드 리뷰 미해결 항목

스펙 반영은 끝났지만 리뷰에서 발견된 개선 사항. 우선순위 순.

## 🔴 실질 버그 (기능 영향 있음)

- [ ] **B2. `transformers/type_conversion.py` — UPDATE SET / INSERT VALUES 의
      write template 을 LHS 컬럼이 아닌 RHS 값에 적용**
      현재: `UPDATE T SET TO_CHAR(NEW_COL, 'YYYYMMDD') = #{dt}` (문법 에러)
      기대: `UPDATE T SET NEW_COL = TO_DATE(#{dt}, 'YYYYMMDD')`
      구현 노트:
      - Pass A — UPDATE SET: `eq.left` 가 col 이면 rename 만 + `eq.right`
        에 `write` template 적용 (`{src}` → 원 RHS sql())
      - Pass B — INSERT: Schema.expressions 컬럼 rename + 각 Tuple 의 같은
        인덱스 값에 `write` template 적용. VALUES 가 SELECT subquery 면
        skip + warning (position 매칭 불가)
      - Pass C — 나머지 (SELECT/WHERE) 는 기존대로 col 을 wrap
      - `_classify_context` 는 'write' 분기 제거 — A/B 가 모두 처리

## 🟡 엣지 케이스 (실환경 드물)

- [ ] **E1. `xml_rewriter.py` 텍스트 치환이 SQL 문자열 리터럴 내부에도 적용**
      → sqlglot token 단위로 쪼갠 뒤 identifier 토큰만 치환
- [ ] **E2. `sql_rewriter.mask_mybatis_placeholders` 의 `MBP_N` prefix 충돌**
      → 더 희박한 prefix (`__MBP_{n}__`) 로 교체
- [ ] **E3. `llm_fallback._extract_json_block` brace-in-prose fragile**
      → 브레이스 카운팅 파서 도입
- [ ] **E4. `validator_static` CTE 본문 컬럼 일괄 warning — 정밀도 향상 여지**
      (Stage B 가 실 판정이라 현재는 OK)
- [ ] **E5. `dynamic_sql_expander` Level 2 중첩 `<choose>` 대안 미탐색**
      (경로 폭발 우려로 의도적 제한)

## 🟢 코드 품질

- [ ] **Q2. `migration_report._coverage_lookup` O(n×m)** → pre-grouping 으로
      O(n+m)
- [ ] **Q3. `mapping_loader._SENTINEL`** → Optional 타입 + explicit None
      비교로 대체
- [ ] **Q4. `impact_analyzer._scan_statements` 반복 regex** → sqlglot AST
      한 번 파싱 후 재사용
- [ ] **Q5. Stage A 실패 행 빨강 하이라이트 추가** (현재 Stage B 실패만 빨강)
- [ ] **Q6. XML 메타데이터 블록 위치** — body text "뒤" 가 아닌 "앞" 으로 이동
      (spec §12.2 예제와 일치시키기)

