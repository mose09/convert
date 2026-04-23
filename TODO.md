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

### 진행 중: CRUD 판정 body-only — MyBatis 태그 완전히 무시

PR #29 에서 하이브리드 (태그 ∪ 본문) 로 구현했지만 사용자 재확인: "태그
참조 말고 쿼리 분석으로만". 태그는 dev intent 라서 실제 SQL 과 어긋날
수 있다는 우려 반영.

변경:

- [x] `_derive_table_crud` 에서 `_stmt_type_to_crud(...)` 호출 제거.
      `statement_to_body_crud` 만 소스로 사용.
- [x] 이제 불필요해진 `_STMT_TYPE_TO_CRUD` dict + `_stmt_type_to_crud`
      helper + `_build_mybatis_indexes` 의 `statement_to_type` 인덱스
      모두 삭제 (dead code 제거).
- [x] 기존 mock (`/tmp/mock_hybrid_crud`) 재검증 — 태그만의 영향이던
      T2 가 `(RU)` → `(U)` 로 변경됨 (사용자 의도대로). 다른 4 케이스
      (T1 MERGE, T3 procedure, T4 FOR UPDATE) 는 body scan 이 이미
      해당 letters 를 잡고 있어서 변화 없음.
- [x] conventional commit + PR + squash-merge

### 진행 중: CRUD 판정 하이브리드 — 태그 + SQL 본문 키워드 스캔

현재 `_derive_table_crud` 는 MyBatis 태그만 (SELECT→R / INSERT→C 등)
보고 CRUD letter 를 붙여 네 가지 edge case 를 놓침:
1. `<select>` 태그에 `BEGIN proc_that_updates(); END;` — tag 는 R 인데
   실제는 UPDATE
2. `MERGE INTO ...` (INSERT+UPDATE 복합) 이 `<update>` 태그면 `U` 만
   나오고 `C` 누락
3. `<update>` 안에 실제로는 DELETE 만 있는 미스라벨 케이스
4. `<procedure>` / `<statement>` 태그는 분류 불가 → 공백

**방식** (사용자 선택 — 하이브리드):
- 태그 타입 letter 는 항상 포함 (dev intent 신뢰)
- SQL 본문을 regex 로 추가 스캔해 `\b(INSERT|UPDATE|DELETE|MERGE|SELECT)\b`
  발견 시 해당 letter 도 union
- 문자열 리터럴 `'...'` / 주석 `--` · `/* */` 은 스캔 전에 제거 (false
  positive 방지)
- `MERGE` 는 본문에 INSERT/UPDATE 키워드를 자연 포함하므로 union 으로
  자동 C+U 획득, 예외 처리 불필요
- `SELECT ... FOR UPDATE` 의 UPDATE 는 lock 힌트이므로 scan 전에 제거

작업 항목:

- [x] `mybatis_parser.extract_crud_from_sql(sql) → set[letter]` 공개 함수
      (리터럴/주석 스트리핑 + 키워드 스캔 + `FOR UPDATE` 제외)
- [x] `_build_mybatis_indexes` 에 `statement_to_body_crud: {ns.id: set}`
      신규 인덱스. 파일 로드 시점에 본문 스캔 결과 캐시.
- [x] `_derive_table_crud` 수정 — tag letter + body letters union.
      method-scope 경로만 영향 (class-scope fallback 은 기존대로 공백).
- [x] 단위 검증 10/10 PASS (MERGE / PL/SQL BEGIN / SELECT FOR UPDATE /
      single-quote 리터럴 / line comment / block comment / INSERT /
      DELETE / 빈 문자열)
- [x] end-to-end 검증 (`/tmp/mock_hybrid_crud`, 5 시나리오):
      * `<update>` + MERGE (USING SELECT) → `T1(CRU)` ✓
      * `<select>` + 본문 PL/SQL UPDATE → `T2(RU)` ✓
      * `<procedure>` + INSERT → `T3(C)` ✓ (기존엔 공백)
      * `<select>` + FOR UPDATE → `T4(R)` ✓ (U 배제)
      * 리터럴 false positive 는 CRUD scan 은 정상 R 만, 다만 table
        extractor 의 pre-existing 이슈로 T5 미집계 (별개 건)
- [x] conventional commit + PR + squash-merge

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
