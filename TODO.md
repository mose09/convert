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

### 진행 중: redux-saga 패턴 API 호출 추출 (A + B)

`legacy_react_api_scanner` 가 redux-saga 파일 (`*Saga.js` / `sagas/*.js`)
안의 axios 호출을 놓치는 문제. 두 패턴 동시 커버:

- **A** — `yield call(axios.get, '/api/x')`: URL 이 2번째 인자. 현재 regex
  는 호출 바로 뒤 첫 인자만 매칭 → 실패.
- **B** — `yield call(api.fetchUser, payload)`: URL 이 saga 파일에 없고
  별도 `api.js` 모듈의 `fetchUser` 안에 있음. 전역 함수 body 인덱스 →
  2-pass 해석으로 saga 파일에 URL 귀속.

작업 항목:

- [ ] A 구현 — `_SAGA_CALL_LITERAL_RE` 추가 (call/apply 의 2번째 인자 URL
      리터럴 매칭). 1번째 인자는 식별자 / dotted 참조만 허용.
- [ ] B 구현 — `_collect_function_bodies` 전역 인덱스 + saga 파일에서
      `call(X.fn)` / `call(fn)` 감지 → 인덱스 lookup → 본체에서 axios URL
      추출 → saga 파일에 귀속.
- [ ] `/tmp/mock_saga` 신규: (1) saga 에 `call(axios.get, '/api/a')`,
      (2) saga 에 `call(api.fetchUser)` + `api.js` 에 `axios.get('/api/b')`,
      (3) 기본 `axios.post('/api/c')` 3 케이스 종합.
- [ ] 기존 `mock_react` 회귀 — 직접 호출 추출에 영향 없는지 확인.
- [ ] conventional commit + push

---

## 12. SQL Migration — `convert-mapping` / `migration-impact` / `migrate-sql` / `validate-migration`

스펙: `docs/migration/spec.md`. DSL 우선 → LLM fallback → 수동 큐 3-tier
+ Stage A (sqlglot static) / Stage B (TO-BE DB parse) 2-stage 검증.

### 대기: 코드 리뷰 미해결 항목

🟡 **엣지 케이스** (실환경 드물지만 잠재 버그):

- [ ] **E1**. `xml_rewriter.py` 텍스트 치환이 SQL 문자열 리터럴 내부에도
      적용 → sqlglot token 단위로 쪼갠 뒤 identifier 토큰만 치환
- [ ] **E2**. `sql_rewriter.mask_mybatis_placeholders` 의 `MBP_N` prefix
      충돌 위험 → 더 희박한 `__MBP_{n}__` 로 교체
- [ ] **E3**. `llm_fallback._extract_json_block` 의 brace-in-prose fragile
      → 브레이스 카운팅 파서 도입
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

_진행 중 없음_
