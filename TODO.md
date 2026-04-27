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

_진행 중 없음_

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

### 진행 중: E1 — xml_rewriter literal/comment/OGNL 보호

`xml_rewriter._apply_subs` 가 word-boundary regex 를 element text/tail 전체에
무차별 적용 → SQL 문자열 리터럴 (`WHERE NAME = 'CUST_NM'`) / 코멘트 / MyBatis
OGNL placeholder 안의 토큰까지 rename 됨 → 운영 데이터 손상 가능. 운영 도입
전 차단급.

- [x] `_apply_subs_outside_literals(text, subs)` state machine 추가 — single-quote
      리터럴 (Oracle `''` escape) / `--` 라인 코멘트 / `/* */` 블록 코멘트 /
      MyBatis `#{...}` `${...}` OGNL 영역은 skip, 코드 영역에만 substitution
- [x] `_apply_subs_to_tree` 가 신규 헬퍼 사용
- [x] 회귀 검증 — unit 12/12 + end-to-end rewrite_xml 11/11 (리터럴/코멘트/OGNL/
      escaped quote/unterminated literal-comment 안전 fallback/word-boundary
      substring 보호 모두 통과)
- [ ] PR squash-merge

### 대기: 코드 리뷰 미해결 항목

🟡 **엣지 케이스** (실환경 드물지만 잠재 버그):
- [x] **E2**. `sql_rewriter.mask_mybatis_placeholders` 의 `MBP_N` prefix
      충돌 위험 → `__MBP_{n}__` 로 교체. `validator_static` MBP_ 필터도
      동기화. 부수 효과로 prefix-of-prefix 버그 (`MBP_1` ⊂ `MBP_10`) 도
      해소 (`__MBP_1__` ⊄ `__MBP_10__`)
- [x] **E3**. `llm_fallback._extract_json_block` 의 brace-in-prose fragile
      → string-aware 브레이스 카운팅 파서 (` ```json` 우선 fenced + 폴백
      brace counter; 문자열 리터럴/이스케이프 quote 처리)
- [ ] **E4**. `validator_static` CTE 본문 컬럼 일괄 warning 정밀도 향상
      (Stage B 가 실 판정이라 현재는 OK)
- [ ] **E5**. `dynamic_sql_expander` Level 2 중첩 `<choose>` 대안 미탐색
      (경로 폭발 우려로 의도적 제한 — 필요 시 제한 해제)

🟢 **코드 품질**:

- [ ] **Q2**. `migration_report._coverage_lookup` O(n×m) → pre-grouping
      으로 O(n+m)
- [ ] **Q3**. `mapping_loader._SENTINEL` → Optional 타입 + explicit None
      비교로 대체
- [ ] **Q4**. `impact_analyzer._scan_statements` 반복 regex → sqlglot AST
      한 번 파싱 후 재사용
- [ ] **Q5**. Stage A 실패 행 빨강 하이라이트 추가 (현재 Stage B 실패만
      빨강)
- [ ] **Q6**. XML 메타데이터 블록 위치 — body text "뒤" 가 아닌 "앞" 으로
      이동 (spec §12.2 예제와 일치)

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
