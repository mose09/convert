# Oracle Schema & Query Analyzer

Oracle DB 스키마 + MyBatis 쿼리를 분석하여 Markdown 추출, ERD 자동 생성, 표준화 리포트까지 지원하는 도구입니다.

FK/description이 없는 레거시 DB 환경에서 **쿼리 JOIN 분석 + 로컬 LLM**으로 테이블 관계를 추론합니다.

> 📖 **HTML 사용자 매뉴얼**: 좌측 사이드바 (카테고리 → 커맨드) + 검색 + 다크모드 버전은 루트의 [`user_manual.html`](user_manual.html) — 브라우저로 직접 열기 (CDN 의존 X, 오프라인 OK). 기능 추가·변경 후엔 **반드시 `python main.py docs` 로 재빌드**.

## 기능 요약

| Command | 설명 | Oracle 접속 | LLM |
|---------|------|:-----------:|:---:|
| `schema` | 테이블/컬럼 스키마 .md 추출 | O | X |
| `query` | MyBatis XML 쿼리 분석 .md | X | X |
| `enrich-schema` | 빈 코멘트를 LLM이 추천하여 보강 | X | O |
| `erd-md` | .md 파일에서 Mermaid ERD 생성 | X | X |
| `erd-group` | 관계 기반 주제영역별 ERD 분할 생성 | X | X |
| `terms` | 용어사전 자동 생성 (스키마 + React) | X | O |
| `grid-labels` | AG Grid `columnDefs` 의 `(field, headerName)` 페어 추출 (regex, deterministic) | X | X |
| `morpheme` | 형태소분석 — 속성명 txt → LLM 단어 분해 리포트 (속성명/컨피던스/단어1..12/비고 단일 시트 xlsx + md 요약) | X | O |
| `build-dict` | 단어/용어/도메인사전 Excel → SQLite 적재 (기존 내용 삭제 후 재적재). 1회 적재 후 recommend-names 는 사전 인자 없이 DB 로 수행 | X | 선택 |
| `recommend-names` | AS-IS 스키마 → TO-BE 속성명 추천 (적재된 SQLite 표준사전 기반, Tier1 정확매칭 → Tier2 단어조합 → Tier3 RAG → Tier4 LLM) | X | 선택 |
| `gen-ddl` | 자연어 → 표준 DDL 생성 (+ 검증) | 선택 | O |
| `audit-standards` | 전체 스키마 표준 위반 일괄 검사 | X | X |
| `validate-naming` | 테이블/컬럼명 네이밍 표준 검증 | X | X |
| `review-sql` | SQL 쿼리 정적 분석 + LLM 리뷰 | X | 선택 |
| `standardize` | 표준화 분석 리포트 생성 | 선택 | O |
| `analyze-legacy` | AS-IS 레거시 소스 통합 분석 (Spring/Vert.x/Nexcore + MyBatis/iBatis + React/Polymer + Menu) + **ServiceImpl 비즈니스 로직 LLM 추출** (opt-in `--extract-biz-logic`) | 선택 | 선택 |
| `discover-patterns` | LLM 으로 프로젝트 패턴 자동 발견 (analyze-legacy 사전 단계) | X | O |
| `convert-menu` | 임의 양식의 메뉴 Excel → 표준 menu.md 변환 (LLM 이 헤더 매핑 학습) | X | O |
| `convert-mapping` | AS-IS↔TO-BE 컬럼 매핑 .md → `column_mapping.yaml` (LLM + heuristic 이 kind/transform 추론; **사용자 표준 9-컬럼 flat 포맷 지원** — asis/tobe table/column/type/comment/remark) | 선택 | X |
| `migration-impact` | SQL Migration 사전 영향분석 (매핑 YAML 검증 + AS-IS 쿼리 영향 리포트) | X | X |
| `migrate-sql` | AS-IS MyBatis XML → TO-BE 스키마용 쿼리 일괄 변환 + Excel/XML 산출물 (`--format-only` 로 매핑 없이 포매터만 미리보기 가능) | X | 선택 |
| `validate-migration` | 변환된 XML 의 TO-BE SQL 을 TO-BE DB 에 parse-only 검증 (Stage B) | O | X |
| `screen-converter` | AS-IS 화면 캡처 폴더 → TO-BE PPTX 도형 자동 생성 (Vision LLM 이 DRM 템플릿 캡처를 시각 참조로 layout JSON 추출 → python-pptx 렌더, PoC) | X | O |
| `screen-spec` | React 화면 closure (entry + 자식 컴포넌트 BFS) → AST 패턴으로 검색·그리드·탭·이벤트+flow·검증 deterministic 추출 → 마스터 xlsx (시트=영역, 1열=화면명). 같은 소스 → 같은 산출물 보장 (LLM 0). PPTX 설계서에 시트 단위 복사·붙여넣기 워크플로우용 | X | X |
| `capture-screens` | AS-IS 화면을 Playwright headless 브라우저로 실제 렌더 → DOM 레이아웃 JSON → 사내 Figma 플러그인 (`figma_plugin/`) 으로 편집 가능 레이어 재구성. 외부 SaaS 전송 없음 (폐쇄망) | X | X |
| `embed` | .md를 벡터 DB에 임베딩 | X | X |
| `erd-rag` | RAG로 Mermaid ERD 생성 | X | O |
| `erd` | 직접 DB 접속 ERD 생성 | O | 선택 |

## 산출물 경로 규약

모든 커맨드는 `output/<영역>/<YYYYMMDD>/<파일>` 형태로 떨어집니다 — 영역
폴더 + 일자별 하위 폴더 구조로 통일돼 있어 같은 날 여러 번 돌려도 한
폴더 안에 모이고, 날짜가 바뀌면 자동으로 새 폴더로 분리됩니다.

| 영역 폴더 | 사용 커맨드 |
|----------|------------|
| `output/schema/<날짜>/` | schema |
| `output/query/<날짜>/` | query |
| `output/enrich-schema/<날짜>/` | enrich-schema |
| `output/erd/<날짜>/` | erd / erd-md / erd-group / erd-rag |
| `output/terms/<날짜>/` | terms |
| `output/morpheme/<날짜>/` | morpheme |
| `output/recommend_names/<날짜>/` | recommend-names |
| `output/standardize/<날짜>/` | standardize |
| `output/sql_review/<날짜>/` | review-sql |
| `output/naming_validation/<날짜>/` | validate-naming |
| `output/ddl/<날짜>/` | gen-ddl |
| `output/audit/<날짜>/` | audit-standards |
| `output/legacy_analysis/<날짜>/` | analyze-legacy + discover-patterns |
| `output/migration/<날짜>/` | migration-impact + migrate-sql + validate-migration |
| `output/screen-converter/<날짜>/` | screen-converter |
| `output/screen-spec/<날짜>/` | screen-spec |
| `output/figma_capture/<날짜>/` | capture-screens |

예외:
- `--output` 으로 명시 지정 시 사용자 경로 그대로 사용
- `convert-mapping` / `convert-menu` 두 커맨드는 다음 단계 입력 자료라
  `input/` 으로 출력
- `output/legacy_analysis/.biz_cache/` 는 영구 캐시 — 일자 폴더 밖

## 프로젝트 구조

```
convert/
├── main.py                       # CLI 진입점
├── config.yaml                   # 설정 파일
├── .env.example                  # 환경변수 템플릿
├── requirements.txt
└── oracle_embeddings/
    ├── db.py                     # Oracle DB 연결 (thick mode, 11g 호환)
    ├── extractor.py              # 스키마 메타데이터 추출
    ├── mybatis_parser.py         # MyBatis XML 파싱 & JOIN 분석
    ├── md_parser.py              # .md 파일 → 구조화 데이터 파싱
    ├── schema_enricher.py        # LLM 코멘트 보강
    ├── erd_generator.py          # 구조화 데이터 → Mermaid ERD
    ├── graph_cluster.py          # JOIN 기반 테이블 그룹 클러스터링
    ├── std_analyzer.py           # 표준화 구조 분석
    ├── std_data_validator.py     # 실데이터 검증 (Oracle)
    ├── std_report.py             # 표준화 리포트 + LLM 제안
    ├── vector_store.py           # ChromaDB 벡터 저장소
    ├── rag_erd.py                # RAG 기반 ERD 생성
    ├── llm_assist.py             # LLM 보조 (컬럼 해석, 관계 추론)
    ├── storage.py                # Markdown 파일 생성
    ├── legacy_java_parser.py     # 레거시 Java 정규식 파서 (Controller/Service/Mapper/RFC)
    ├── legacy_frontend.py        # 프론트엔드 React/Polymer 자동 감지 + 디스패처
    ├── legacy_pattern_discovery.py # LLM 기반 프로젝트 패턴 자동 발견
    ├── legacy_react_router.py    # React Router v5/v6 + lazy import 스캐너
    ├── legacy_polymer_router.py  # Polymer (vaadin-router/page.js/dom-module) 파서
    ├── legacy_menu_loader.py     # 메뉴 로더 (DB 테이블 / Excel / Markdown)
    ├── legacy_analyzer.py        # AS-IS 통합 오케스트레이터 (양방향 URL 매칭)
    ├── legacy_report.py          # AS-IS 리포트 Markdown + Excel (최대 8시트)
    ├── legacy_biz_extractor.py   # Phase A: ServiceImpl 비즈니스 로직 LLM 추출 (opt-in)
    ├── legacy_util.py            # URL 정규화 공유 헬퍼
    ├── screen_converter.py       # 화면변환기 PoC: AS-IS 캡처 → TO-BE PPTX (Vision LLM)
    ├── screen_spec/              # 화면 UI 정의서 추출 (AST 패턴, LLM 0, deterministic)
    │   ├── models.py                #   FormField/GridColumn/Tab/ButtonEvent/ValidationRule/ScreenSpec
    │   ├── extractors.py            #   5종 추출기 + extract_screen_spec entry
    │   ├── flow_tracer.py           #   onClick handler → 순서있는 FlowStep 리스트
    │   └── excel_writer.py          #   마스터 xlsx 7시트 (openpyxl)
    └── migration/
        ├── mapping_model.py         # 매핑 dataclass
        ├── mapping_loader.py        # YAML 로드 + 검증
        ├── mapping_converter.py     # md/txt → YAML (9-컬럼 flat 지원)
        ├── sql_rewriter.py          # SQL 재작성 오케스트레이터
        ├── sql_formatter.py         # Korean Legacy 포매터 (리딩-콤마 등)
        ├── comment_injector.py      # /* 한글 */ 자동 삽입
        ├── xml_rewriter.py          # MyBatis XML 변환
        ├── validator_static.py      # Stage A — sqlglot 검증
        ├── validator_db.py          # Stage B — Oracle parse 검증
        ├── dynamic_sql_expander.py  # MyBatis <if>/<choose>/<foreach> 전개
        ├── llm_fallback.py          # NEEDS_LLM 상태 통과 시 LLM 변환
        ├── migration_report.py      # 5시트 Excel 리포트
        ├── impact_analyzer.py       # 사전 영향분석
        ├── bind_dummifier.py        # Stage B 전처리 (#{}/${} 제거)
        └── transformers/            # 8종 변환기 (TableRename/ColumnRename/...)
```

## 설치

```powershell
python -m pip install -r requirements.txt
```

## 설정

```powershell
cp .env.example .env
```

`.env` 파일 수정:
```env
# Oracle DB
ORACLE_USER=myuser
ORACLE_PASSWORD=changeme
ORACLE_DSN=localhost:1521/ORCL
ORACLE_SCHEMA_OWNER=myuser
ORACLE_INSTANT_CLIENT_DIR=C:/oracle/instantclient_19_25

# LLM / 임베딩 (Ollama, vLLM 등)
LLM_API_BASE=http://localhost:11434/v1
LLM_API_KEY=ollama
LLM_MODEL=qwen2.5
EMBEDDING_API_BASE=http://localhost:11434/v1
EMBEDDING_API_KEY=ollama
EMBEDDING_MODEL=Qwen3-Embedding-8B
```

## 사용법

### 1. 스키마 추출 (Oracle 접속 필요)

```powershell
python main.py schema
python main.py schema --owner HR
python main.py schema --table CUSTOMERS
```

### 2. 쿼리 분석 (Oracle 접속 불필요)

MyBatis, iBatis XML 모두 지원합니다.

```powershell
# 기본 실행 — 단일 mapper 폴더
python main.py query /path/to/mybatis/mapper

# 스키마 .md 기반 필터링 (스키마에 없는 테이블 제외, 권장)
python main.py query /path/to/mybatis/mapper --schema-md ./output/스키마.md

# 백엔드 여러 개 — --mapper-dir 반복 (positional 과 같이 사용 가능)
python main.py query `
  C:\workspace\backend\app1\src\main\resources\mapper `
  --mapper-dir C:\workspace\backend\app2\src\main\resources\mapper `
  --mapper-dir C:\workspace\backend\batch\src\main\resources\mapper `
  --schema-md .\output\스키마.md
```

**multi-dir 모드**:
- 결과는 통합 `query.md` 1개 — JOIN 페어 `(table1, table2)` 기준 dedupe
- 같은 JOIN 이 여러 backend 에 나오면 count 합산 + `sources` 에 백엔드명 누적
- `table_usage` 도 합산 (as_main / as_join count + sources)
- statements 각각에 `source_dir` 필드 — 어느 backend mapper 인지 추적

**SET operator (MINUS / INTERSECT) → 관계 자동 인식**:
- `SELECT a, b, c FROM TBL_X MINUS SELECT a, b, c FROM TBL_Y` 같은
  패턴은 두 SELECT 의 같은 위치 컬럼이 키로 비교되는 강한 관계 단서 →
  ERD 에 자동 관계 표시 (`join_type = MINUS` / `INTERSECT`)
- 양쪽 select-list 컬럼 페어를 위치 매칭으로 모두 emit (3 컬럼이면 3
  페어). `SELECT *` 면 `*` placeholder 1 페어로 관계만 표시
- `UNION` / `UNION ALL` 은 단순 결과 합치기 (키 비교 의미 아님) →
  위양성 회피로 skip
- 일반 JOIN 추출 결과와 동일 dedupe / sources 누적 적용

### 3. 스키마 코멘트 보강 (LLM)

빈 테이블/컬럼 코멘트를 LLM이 약어를 해석하여 자동 추천합니다.
확신할 수 없는 약어는 비워둡니다.

```powershell
python main.py enrich-schema --schema-md ./output/스키마.md
```

출력: `output/스키마_enriched_TIMESTAMP.md` (보강된 스키마)

### 4. ERD 생성

```powershell
# .md 파일에서 직접 ERD (LLM 불필요, 즉시 실행)
python main.py erd-md --schema-md ./output/스키마.md --query-md ./output/query.md --related-only

# 특정 테이블 + 관련 테이블만
python main.py erd-md --schema-md ./output/스키마.md --query-md ./output/query.md --tables "ORDERS,CUSTOMERS"

# 주제영역별 그룹 분할 ERD
python main.py erd-group --schema-md ./output/스키마.md --query-md ./output/query.md
python main.py erd-group --schema-md ./output/스키마.md --query-md ./output/query.md --max-size 15
```

### 5. 용어사전 자동 생성

스키마 컬럼명 + React 소스 변수명에서 단어를 수집하고, LLM이 약어/영문명/한글명/한글 정의를 생성합니다.

```powershell
# 스키마 + React 소스 양쪽에서 수집
python main.py terms --schema-md ./output/스키마.md --react-dir /path/to/react/src

# 스키마만
python main.py terms --schema-md ./output/스키마.md

# LLM 없이 단어 수집만
python main.py terms --schema-md ./output/스키마.md --react-dir ./src --skip-llm
```

LLM이 생성하는 필드:
- **Abbreviation** — 표준 DB 약어 (2~5자)
- **English Full** — 영문 Full Name
- **Korean** — 한글명
- **Definition** — 한글 정의 (업무 의미 1~2문장, 50자 내외)

산출물:
```
output/
├── terms_dictionary_TIMESTAMP.md    # 용어사전 Markdown
└── terms_dictionary_TIMESTAMP.xlsx  # 용어사전 Excel
    ├── Sheet: 용어사전      (전체, Definition 포함)
    ├── Sheet: DB+FE공통     (양쪽에서 사용, 표준화 우선)
    ├── Sheet: DB전용        (DB에서만 사용)
    ├── Sheet: FE전용        (프론트에서만 사용)
    └── Sheet: 미식별        (LLM이 해석 못한 단어)
```

### 6. DDL 자동 생성 (자연어)

자연어 요청으로 표준을 준수하는 CREATE TABLE DDL을 자동 생성합니다.

```powershell
# 기본 사용 (용어사전 없이)
python main.py gen-ddl --request "고객 주문 이력 테이블"

# 용어사전 + 스키마 참조 (권장)
python main.py gen-ddl `
  --request "고객 주문 이력 테이블 만들어줘. 고객ID, 주문일자, 금액 포함" `
  --terms-md ./output/terms_dictionary.md `
  --schema-md ./output/스키마.md

# 생성 + 검증 + Oracle 실행 (컨펌 후)
python main.py gen-ddl `
  --request "배송 정보 테이블" `
  --terms-md ./output/terms_dictionary.md `
  --execute
```

**처리 흐름:**
1. 자연어 요청 + 용어사전 + 기존 스키마 샘플을 LLM에 전달
2. LLM이 표준 약어로 DDL 생성
3. 생성된 DDL을 자동으로 네이밍 표준 검증
4. 검증 결과 출력 + DDL 파일 저장
5. `--execute` 옵션 시 사용자 컨펌 후 Oracle 실행

### 7. 표준 위반 자동 감지 (audit-standards)

기존 스키마 전체를 대상으로 네이밍 표준 위반을 일괄 검사합니다.

```powershell
# 기본 약어 사전으로 검사
python main.py audit-standards --schema-md ./output/스키마.md

# 용어사전 기반 검사 (권장)
python main.py audit-standards `
  --schema-md ./output/스키마.md `
  --terms-md ./output/terms_dictionary.md
```

**산출물:**
```
output/
├── audit_standards_TIMESTAMP.md    # Markdown 리포트
└── audit_standards_TIMESTAMP.xlsx  # Excel (4개 시트)
    ├── Sheet: Summary          (전체 집계)
    ├── Sheet: Invalid Tables   (위반 테이블)
    ├── Sheet: Invalid Columns  (위반 컬럼)
    └── Sheet: Pattern Summary  (룰별 위반 빈도)
```

각 위반 항목에 심각도와 유사 약어 추천이 포함되어 마이그레이션/정비 작업 기초자료로 활용할 수 있습니다.

### 8. 네이밍룰 검증

용어사전 기반으로 테이블/컬럼명이 표준을 따르는지 검증합니다.

```powershell
# 단일 이름 검증
python main.py validate-naming --name "TB_CUSTOMER_ORDER" --terms-md ./output/terms_dictionary.md
python main.py validate-naming --name "CUST_NO" --kind column

# 파일의 이름 목록 일괄 검증
python main.py validate-naming --file new_tables.txt --terms-md ./output/terms_dictionary.md

# DDL 파일 파싱 후 검증
python main.py validate-naming --ddl create_tables.sql --terms-md ./output/terms_dictionary.md
```

**검증 항목:**

| 심각도 | 항목 | 설명 |
|--------|------|------|
| CRITICAL | LENGTH | 30자 초과 (Oracle 11g) |
| CRITICAL | SPECIAL_CHAR | 특수문자 사용 |
| CRITICAL | FIRST_CHAR | 첫 글자 숫자 |
| HIGH | CASE | 소문자 사용 (대문자+언더스코어 권장) |
| MEDIUM | UNKNOWN_ABBREVIATION | 용어사전에 없는 약어 (유사 약어 추천) |
| LOW | PREFIX | 테이블 접두어 (TB, TBL 등) 미사용 |

### 9. SQL 리뷰 (정적 분석 + LLM)

MyBatis/iBatis XML의 SQL 쿼리를 분석하여 비효율 패턴을 자동 감지합니다.

```powershell
# 정적 분석만
python main.py review-sql --mybatis-dir /path/to/mapper

# LLM 리뷰 포함 (상위 20개 이슈)
python main.py review-sql --mybatis-dir /path/to/mapper --llm-review

# LLM 리뷰 샘플 수 조절
python main.py review-sql --mybatis-dir /path/to/mapper --llm-review --max-samples 50
```

**감지 패턴:**

| 심각도 | 패턴 | 설명 |
|--------|------|------|
| CRITICAL | 카티시안 곱 | FROM 절 콤마 나열, JOIN 조건 없음 |
| CRITICAL | UPDATE/DELETE WHERE 없음 | 전체 테이블 영향 |
| HIGH | NOT IN | NULL 처리 및 성능 문제 |
| HIGH | LIKE '%...' | 선두 와일드카드 → 풀스캔 |
| MEDIUM | SELECT * | 불필요한 컬럼 조회 |
| MEDIUM | WHERE 컬럼 함수 | 인덱스 미사용 |
| MEDIUM | 스칼라 서브쿼리 | SELECT 절 서브쿼리 |
| MEDIUM | 암시적 형변환 | 타입 불일치 비교 |
| LOW | DISTINCT | 정렬 비용 |
| LOW | WHERE OR | 인덱스 방해 |

**산출물:**
```
output/
├── sql_review_TIMESTAMP.md    # 패턴별 + LLM 리뷰
└── sql_review_TIMESTAMP.xlsx  # Excel
    ├── Sheet: Summary       (심각도 집계)
    ├── Sheet: Issues        (전체 이슈 목록)
    ├── Sheet: Pattern Summary (패턴별 집계)
    └── Sheet: LLM Review    (LLM 개선안, --llm-review 시)
```

### 10. 표준화 분석 리포트

```powershell
# 구조 분석만 (Oracle 불필요, LLM으로 표준안 제안)
python main.py standardize --schema-md ./output/스키마.md --query-md ./output/query.md

# 구조 분석 + 실데이터 검증 (Oracle 접속)
python main.py standardize --schema-md ./output/스키마.md --query-md ./output/query.md --validate-data

# 실데이터 검증하되 컬럼 사용률 체크 스킵 (빠르게)
python main.py standardize --schema-md ./output/스키마.md --query-md ./output/query.md --validate-data --skip-usage
```

표준화 리포트 산출물:

```
output/std_report_TIMESTAMP/
├── 00_summary.md                  # 전체 요약
├── 01_join_column_mismatch.md     # JOIN 컬럼명 불일치 (동일 관계, 다른 이름)
├── 02_type_inconsistency.md       # 동일 컬럼 타입 불일치
├── 03_naming_pattern.md           # 네이밍 패턴 이탈
├── 04_identifier_pattern.md       # PK 접미어 패턴 분석
├── 05_code_columns.md             # 코드 컬럼 + DISTINCT 값
├── 06_yn_columns.md               # Y/N 컬럼 + 이상 데이터
├── 07_column_usage.md             # 미사용/과다할당 컬럼
└── 08_llm_proposals.md            # LLM 표준화 제안
```

### 11. AS-IS 레거시 소스 분석 (analyze-legacy)

Java/Spring/Vert.x/**Nexcore** + MyBatis/**iBatis** + React/**Polymer** + 메뉴 (DB/Excel/Markdown)를 **통합 분석**하여
차세대 전환 전 **프로그램 단위 메타데이터**를 일괄 추출합니다.
한 행 = 한 Controller 엔드포인트이며 메뉴 계층 → Controller → Service
→ DAO → Mapper XML → 관련 테이블 → RFC 호출까지 한 번에 매핑됩니다.

경로는 **백엔드/프론트엔드 루트 한 개씩만** 지정하면 됩니다. 백엔드 루트
하위를 재귀 탐색해서 `.java` 와 MyBatis/iBatis `*.xml` 을 모두 찾아내며,
`.git` / `.gradle` / `.idea` / `.svn` / `.hg` / `.next` / `node_modules`
폴더는 자동으로 제외됩니다 (`target` / `build` / `bin` 등 빌드 산출물
이름은 제외하지 않음 — 실제 프로젝트 폴더일 수 있어 `_is_sql_mapper` 가 별도로 필터링).

> **Windows 사용자 안내 (PowerShell 기본)**
>
> 아래 예시는 **PowerShell 그대로 복붙 가능**하도록 백틱 `` ` `` 줄바꿈과
> Windows 경로(`C:\...`)로 작성돼 있습니다. 본인 환경 경로로만 바꾸세요.
> - 여러 줄이 불편하면 모두 **한 줄로 붙여도** 동일하게 동작합니다.
> - **cmd.exe** 를 쓴다면 백틱 `` ` `` 대신 `^` 로 바꾸세요.
> - `python` 이 PATH 에 없으면 `py -m` 또는 `py main.py ...` 로 대체.
> - em-dash(`—`)는 단항 연산자로 오해됩니다 — 반드시 일반 하이픈만 사용.
> - Excel 에 DRM 이 걸려 열리지 않을 땐 `--menu-xlsx` 대신 `--menu-md input\menu_template.md` 사용.
>
> **⚠ 폐쇄망 사용자 주의 — 단방향 전송**:
> 이 도구를 쓰는 기업 내 Windows PC 는 대개 **외부 → 내부 다운로드는
> 가능하지만 내부 → 외부 업로드 / 복사·붙여넣기 는 차단** 돼 있습니다
> (코드/데이터 유출 방지). Claude Code 세션에 명령 결과를 전달하려면
> 사용자가 **수기로 타이핑** 해야 하므로:
> - 진단 / 디버깅 시 긴 로그 전체 붙여달라는 요청 대신, 도구 쪽에서
>   **결론 한 줄** (✓/⚠/✗ + 다음 액션) 을 먼저 찍고 상세는 옵트인.
> - 질문은 **선택지 (A/B/C 중 어느 쪽?)** 형태로. 자유 서술 요구 금지.
> - 산출물 (xlsx / md 리포트) 은 사용자가 로컬에서 직접 열어 확인하므로
>   별도 전달 불필요. Claude 쪽은 요약 / 파일 경로만 확인.
> - 이 제약은 `CLAUDE.md` 의 "사용자 환경 제약" 섹션에도 명시돼 있어
>   다음 Claude 세션도 동일 원칙으로 응대합니다.

**Step 0 (선택) — 메뉴 양식이 다를 때: `convert-menu` 로 표준 menu.md 생성**

프로젝트 메뉴 Excel 의 컬럼 구성이 템플릿과 다르면 먼저 변환합니다. 헤더와
샘플 20 행을 보고 다음 세 가지 유형 중 어느 쪽인지 **매핑을 한 번 결정**해서
표준 `menu.md` 로 쓰면 끝.

| mode | 예시 헤더 |
|---|---|
| `columns_per_level` | `대메뉴 / 중메뉴 / 소메뉴 / 링크` (레벨별 별도 컬럼) |
| `depth_column` | `메뉴명 / 뎁스 / URL` (뎁스 숫자 컬럼, 0-base / 1-base 자동 감지) |
| `path_column` | `경로(A > B > C) / URL` (한 컬럼에 계층 압축) |

**매핑을 누가 결정하나**

| 상황 | 결정 주체 | 비고 |
|---|---|---|
| `PATTERN_LLM_*` 또는 `LLM_*` 설정됨 (기본) | **LLM** | 로그에 `LLM mapping: mode=...` 표시 |
| LLM 응답이 비정상 / 연결 실패 | heuristic | 헤더 synonym (`대메뉴/뎁스/경로/link/...`) 로 폴백 |
| `--no-llm` 플래그 | heuristic | LLM 호출 자체 skip, 폐쇄망·오프라인용 |

LLM/heuristic 어느 쪽이 판단하든 그 다음 단계(병합 셀 forward-fill,
헤더 라인 자동 탐지, 0-base depth 자동 shift 등)는 동일하게 적용됩니다.

```powershell
# LLM 매핑 + 3가지 모드 자동 분류 + menu.md 생성
python main.py convert-menu `
  --menu-xlsx C:\work\menu_원본.xlsx `
  --output input\menu.md

# 시트 지정
python main.py convert-menu --menu-xlsx C:\work\menu.xlsx --sheet "Sheet1" --output input\menu.md

# 폐쇄망 / 오프라인 (LLM 없이 헤더 synonym heuristic 만 사용)
python main.py convert-menu --menu-xlsx C:\work\menu.xlsx --no-llm --output input\menu.md

# DRM 걸린 xlsx 대안: 뷰어에서 셀 전체를 복사해 .md / .txt / .tsv 로 붙여
# 넣은 뒤 --menu-md-in 으로 지정. 파이프 테이블·TSV·CSV 자동 감지.
python main.py convert-menu `
  --menu-md-in C:\work\menu_paste.md `
  --output input\menu.md
```

**DRM 우회 팁**: 원본 xlsx 가 DRM 으로 openpyxl 에서 안 열릴 때는 Excel
뷰어에서 메뉴 표 영역을 선택→복사 한 뒤 VSCode 나 메모장에 붙여넣고
`.md` 로 저장하면 됩니다. 기본은 TSV(탭 구분) 로 붙고, 원하면 직접
파이프 테이블(``| a | b | c |``) 로 바꿔 써도 동일하게 인식합니다.

변환된 `menu.md` 를 이후 Step 1 (`discover-patterns --menu-md ...`) 과
analyze-legacy 양쪽에서 그대로 사용합니다.

**Step 1 — 패턴 발견 (프로젝트당 1회, LLM 필요)**

프로젝트 소스를 샘플링해 LLM 이 프레임워크 패턴을 자동으로 분석합니다.
14B 이상 코딩 특화 모델 권장 (`PATTERN_LLM_MODEL` 환경변수로 별도 지정 가능).

```powershell
# 백엔드만 샘플링 (프레임워크·스테레오타입·SQL/RFC 패턴)
python main.py discover-patterns `
  --backend-dir C:\work\legacy\backend `
  --output output\legacy_analysis\patterns.yaml

# menu.md + 멀티 프론트엔드 레포를 같이 주면 URL 관례 (prefix strip /
# app_key 등) 도 동시에 학습 (LLM 실패 시 메뉴 URL 공통 prefix 기반
# heuristic 으로 fallback)
python main.py discover-patterns `
  --backend-dir C:\workspace\backend\main-app `
  --menu-md input\menu.md `
  --frontends-root C:\workspace\frontend `
  --output output\legacy_analysis\patterns.yaml
```

생성된 `patterns.yaml` 에 다음 슬롯이 채워집니다:

| 슬롯 | 예시 |
|---|---|
| `framework_type` | spring / vertx / nexcore / custom |
| `controller_base_classes` | `AbstractMultiActionBizController` |
| `endpoint_param_types` | `IDataSet`, `IBizServiceContext` |
| `url_suffix` / `http_method_default` | `.do` / `POST` |
| `sql_receivers` / `sql_operations` | `sqlMapClientTemplate` / `queryForList` |
| `rfc_call_methods` | `execute`, `send` (커스텀 RFC 호출 메서드) |
| `service_suffixes` / `dao_suffixes` | `Service`, `Bo` / `Dao`, `Repository` |
| `url.url_prefix_strip` | `^/apps/[^/]+`, `^/api/v\d+` (메뉴·React·컨트롤러 URL 에서 제거할 공통 prefix) |
| `url.react_route_prefix` | `/web` (React 라우트에만 붙는 prefix, 없으면 `null`) |
| `url.menu_url_scheme` | `path_only` / `full_url` / `app_prefixed` |
| `url.app_key` | `{source: path_segment, index: 2}` — 멀티 레포 disambiguation 용 앱 식별자 위치 |

생성 후 수동으로 수정 가능. LLM 없이 직접 작성해도 동일하게 동작합니다.
`url:` 섹션은 하위 호환 — 기존 `patterns.yaml` (url 키 없음) 도 그대로 동작합니다.

**Step 2 — 소스 분석 (LLM 불필요)**

```powershell
# 단일 백엔드 + 단일 프론트엔드
python main.py analyze-legacy `
  --backend-dir C:\work\backend `
  --frontend-dir C:\work\frontend `
  --menu-md input\menu.md `
  --patterns output\legacy_analysis\patterns.yaml

# 여러 백엔드 레포 + 여러 프론트엔드 레포
python main.py analyze-legacy `
  --backends-root C:\workspace\backend `
  --frontends-root C:\workspace\frontend `
  --menu-md input\menu.md `
  --patterns output\legacy_analysis\patterns.yaml

# 메인 레포 + 별도의 공용 서비스 레포 (--library-dir)
# common 레포의 서비스/매퍼가 메인 레포 chain 해석에 참여하지만
# Controller 행으로는 emit 안 됨. 배치 모드에서도 모든 sub-project 이
# 공유 라이브러리를 공통으로 참조.
python main.py analyze-legacy `
  --backend-dir C:\workspace\gipms-main `
  --library-dir C:\workspace\gipms-common `
  --menu-md input\menu.md
python main.py analyze-legacy `
  --backends-root C:\workspace\backends `
  --library-dir C:\workspace\gipms-common `
  --menu-md input\menu.md

# 메뉴 매칭된 endpoint 만 Program Detail 에 표시
python main.py analyze-legacy `
  --backends-root C:\workspace\backend `
  --menu-md input\menu.md `
  --menu-only

# 메뉴 없이 (내부 테스트용)
python main.py analyze-legacy `
  --backend-dir C:\work\backend `
  --skip-menu

# 패턴 파일 없이 (기본 Spring/Vert.x/Nexcore 하드코딩 패턴 사용)
python main.py analyze-legacy `
  --backend-dir C:\work\backend `
  --menu-md input\menu.md
```

**주요 옵션:**

| 옵션 | 설명 |
|------|------|
| `--backend-dir` / `--backends-root` | 단일 백엔드 / 여러 백엔드 레포 상위 |
| `--library-dir` | 추가 라이브러리 레포 경로 (**반복 가능**). 해당 경로의 `.java` / MyBatis XML 은 **service / mapper 인덱스에만** 포함되고 Controller / endpoint 행은 생성 안 함. 별도 레포의 공용 서비스 (예: `gipms-common`) 를 메인 레포의 chain 해석에 붙일 때 사용. 단일 + 배치 모드 양쪽에서 작동 |
| `--frontend-dir` / `--frontends-root` | 단일 프론트엔드 / 여러 프론트엔드 레포 상위. **`--frontend-dir` 은 여러 번 지정 가능** (`--frontend-dir A --frontend-dir B --frontend-dir C`) — 2개 이상이면 explicit multi-bucket 모드 (auto-discovery 없이 명시 list 만 분석). 큰 monorepo 의 일부 sub-app 만 분석할 때 유용. |
| `--patterns` | `discover-patterns` 로 생성한 패턴 파일 (없으면 기본값) |
| `--menu-md` / `--menu-xlsx` / `--menu-table` | 메뉴 소스 (우선순위: skip > md > xlsx > DB) |
| `--menu-only` | Program Detail 에 메뉴 매칭된 endpoint 만 표시 |
| `--frontend-framework` | `auto` / `react` / `polymer` 강제 지정 |
| `--rfc-depth` | Service-of-service 체인 탐색 깊이 (기본 3) |
| **`--extract-biz-logic`** | **비즈니스 로직 LLM 추출 on/off (기본 off, 회귀 없음)** |
| `--biz-scope {backend,frontend,both}` | 추출 범위 (기본 `both`) |
| `--biz-max-methods N` | 백엔드 메서드 LLM 호출 cap (기본 500) |
| `--biz-max-handlers N` | React handler LLM 호출 cap (기본 300) |
| `--no-biz-cache` | 디스크 캐시 끔 (기본 on — 재실행 0 LLM 호출) |
| `--row-per-trigger` | 같은 endpoint 가 여러 trigger 에서 호출될 때 trigger 별 1 row 로 분리 (이벤트 별 1:1 backend chain). 기본 off — 한 셀에 `;\n` join. |
| `--sequence-diagram` | Mermaid sequence diagram 생성 (LLM 불필요, parser-only). |
| `--sequence-diagram-group` | sequence diagram .md 묶음 단위. `main_menu` (default) / `menu_path` / `sub_menu` / `controller_class` / `backend_project` (레포) / `none` (endpoint 별). |
| **`--frontend-only`** | **backend / 메뉴 / 컨트롤러 체인 모두 skip — React frontend 만 스캔**. `--frontend-dir` 또는 `--frontends-root` 필요. `--backend-*` 인자 무시. `--extract-screen-layout` 와 함께 쓰면 빠르게 화면 mockup 만 생성. |
| **`--extract-screen-layout`** | **화면별 LLM 분석 + HTML mockup 생성 (Phase C)**. Page Title / Search Panel / DataTable / Edit Mode / Tabs / 이벤트→백엔드 URL. 산출물: `output/legacy_analysis/<일자>/screens/<file>.html`. |
| **`--export-flowchart-pptx`** | (옵트인, `--extract-screen-layout` 와 함께) 모든 화면 flowchart 를 한 PPTX 로 묶기. 슬라이드/화면. mermaid → `mmdc` → SVG+PNG → `python-pptx` 가 PPT picture 에 **svgBlip extension** 으로 임베드. PowerPoint 슬라이드에서 도형 우클릭 → "도형으로 변환" 으로 mermaid 노드/엣지가 편집 가능한 PPT 도형으로 변환. 산출물: `screens/<ts>/flowcharts.pptx`. 의존: `python-pptx` + `mmdc` (미설치 시 skip + 안내). |
| `--screen-max N` | screen layout 분석 최대 화면 수 cap (기본 200, LLM 비용 통제) |
| `--render-screenshots` | (스텁) Playwright 로 진짜 React 화면 스크린샷. 사용자 PC 에 React 빌드/실행 + Playwright 셋업 가능할 때만. 현재 follow-up 대기. |
| **`--closure-llm`** | (옵트인, tree-sitter 필요) Phase C LLM input 을 raw JSX + smart_slice 대신 **AST closure markdown** (import 그래프 BFS + popup 3 신호 facts box) 으로 보강. 미설치 시 자동 fallback. |
| `--closure-max-depth N` | closure BFS 깊이 (기본 3). `--closure-llm` / `--closure-popup-augment` 일 때만 사용. |
| `--closure-token-budget N` | closure 직렬화 토큰 상한 (기본 12000). |
| **`--closure-popup-augment`** | (옵트인, tree-sitter 필요) AST closure 의 popup_refs 로 popup_set 보강. 메인의 `<Modal>` 안 import 만 보는 기존 휴리스틱이 놓친 popup (`*Dialog` / `*Popup` / `*Layer` suffix 컴포넌트가 `<Modal>` 없이 렌더되는 케이스) 을 잡음. |
| **`--llm-per-trigger`** | (옵트인) 화면 단위 LLM 호출 외에 **trigger 단위로도** LLM 분석 — 이벤트 → handler → helper → action → saga 전체 체인을 한 덩어리로 묶어 cascading / 유효성 / 영향받는 필드 / 비즈 요약 추출. 결과는 `search_panel` 의 action / validation_rule + events 의 narrative 에 머지. 캐시: `output/legacy_analysis/.trigger_cache/`. trigger 당 1회 LLM 호출 — 큰 화면이면 비용 N×M (캐시 무효화 시까지 재호출 없음). FAB→Team→SDPT 같은 cascading dependency, 분기 처리, 비즈 의미 등 parser regex 로는 추론 불가능한 영역에 사용. |

**🧾 Search Panel vs Input Panel 정의서 (9컬럼)**

`--extract-screen-layout` 결과 HTML / `screen-spec` xlsx 에 두 종류의 입력 영역 정의서가 자동 분리됩니다:

- **Search Panel (검색영역)** — `<section className="search-area">` (또는 `search-form` / `filter-area` / `criteria-area`) 안의 `<div className="search-item">` 단위 입력. search-area container 가 없으면 검색영역 추출 자체를 skip.
- **Input Panel (입력영역)** — `<table>` 기반 입력 폼 (한국 SI 흔한 패턴 — `<tr><th>라벨</th><td><Input/></td></tr>`). edit form / modal popup form 등. 검색영역과 별도 섹션으로 출력.

- **Search Panel (검색영역)** 8컬럼: **No / 라벨 / 타입 (keyboard input 만) / 길이 / 필수 / 기본값 (placeholder 우선) / 유효성 규칙 및 비고 / 동작**. (v39+ UI 타입 컬럼 제외)
- **Input Panel (입력영역)** 9컬럼: 위 + **UI 타입** (Search 와 달리 유지).

- **필수** 자동 추출: `onSave` / `handleSave` / `onSubmit` 등 핸들러 body 안 `if (isNull(X)) errorMsg.push('[X]')` 패턴 → X 가 필수
- **유효성 규칙** 자동 추출: `isNumber` / `isNegative` / `< 0` / `.length > N` / `.test()` 등 → "숫자만 허용" / "음수 불가" / "길이 제한 (N)" 등으로 변환
- **UI 타입** (입력영역만) 자동 분류: `Select(Single/Multi)` / `Text Field(Basic/Search Box)` / `DatePicker` / `Date Range` / `Checkbox` / `Radio Group` / `Number Field` / `Password` / `Popover`
- **동작** 자동 추출: 단순 dropdown 은 옵션 값 줄바꿈 (예: `전체\nY\nN`). cascading dependency (FAB→Team 등) 는 `_detect_cascading_clears` 가 setState clear 패턴 분석해서 "변경 시 X, Y 초기화" 자동 채움. LLM (`--llm-per-trigger`) 사용 시 더 풍부한 설명으로 보강.

---

**🧠 비즈니스 로직 + Validation 추출 (Quick Start)**

`--extract-biz-logic` 한 플래그로 **백엔드 ServiceImpl 의 비즈니스 로직**과
**React 프론트엔드의 validation / 조건부 로직**을 LLM 으로 구조화 추출해
별도 시트 + Program Detail 요약 컬럼에 내보냅니다. 기본 off (회귀 없음).

### 실행 예시

```powershell
# 1) 백엔드만 (Spring/Vert.x) — 가장 작은 scope
python main.py analyze-legacy `
  --backend-dir C:\work\backend `
  --skip-menu `
  --extract-biz-logic --biz-scope backend

# 2) 프론트엔드만 (React validation / onClick 조건부 로직)
python main.py analyze-legacy `
  --backend-dir C:\work\backend `
  --frontend-dir C:\work\frontend `
  --skip-menu `
  --extract-biz-logic --biz-scope frontend

# 3) 둘 다 (권장)
python main.py analyze-legacy `
  --backend-dir C:\work\backend `
  --frontend-dir C:\work\frontend `
  --menu-md input\menu.md `
  --patterns output\legacy_analysis\patterns.yaml `
  --extract-biz-logic --biz-scope both

# 4) 멀티 레포 모노레포 (실무 시나리오)
python main.py analyze-legacy `
  --backends-root C:\workspace\backend `
  --frontends-root C:\workspace\frontend `
  --menu-md input\menu.md `
  --patterns output\legacy_analysis\patterns.yaml `
  --menu-only --extract-biz-logic

# 5) Trigger 별 1 row 분리 (이벤트 별 1:1 chain)
python main.py analyze-legacy `
  --backend-dir ... --frontend-dir ... `
  --extract-biz-logic --row-per-trigger

# 6) 배치 데몬도 같이 분석 (Spring Batch + Quartz)
python main.py analyze-legacy `
  --backends-root C:\workspace\backend `
  --frontends-root C:\workspace\frontend `
  --menu-md input\menu.md `
  --analyze-daemons
# → 결과 xlsx 에 새 '데몬' 시트:
#    데몬폴더 / 클래스 / 데몬종류 / 메소드 / 서비스 / 서비스메소드 /
#    DAO / XML / XML메서드 / 테이블(CRUD) / RFC / 파일
# 인식 패턴:
#  Java code 기반 —
#   - Spring Batch: implements Tasklet | ItemReader | ItemProcessor |
#     ItemWriter | ItemStream{Reader,Writer}
#   - Quartz: implements Job (org.quartz.Job) | extends QuartzJobBean |
#     @DisallowConcurrentExecution / @PersistJobDataAfterExecution
#  Quartz XML 정의 기반 (quartz_data.xml / spring-quartz.xml 등) —
#   (a) Quartz native: <job-class>FQCN</job-class>
#   (b) Spring JobDetailFactoryBean: <property name="jobClass" value="FQCN"/>
#   (c) Spring MethodInvokingJobDetailFactoryBean:
#       targetObject=beanRef + targetMethod=methodName
#       → 그 service bean 의 그 메소드를 daemon entry 로 등록
# 데몬폴더 = backend root 의 basename (--backends-root 멀티 시 각 sub-repo).
```

**프론트 트리거 → 백엔드 chain 인식 범위 (자동)**:

| 트리거 종류 | 예시 | 인식 |
|---|---|---|
| JSX 모든 이벤트 | `onClick / onChange / onSubmit / onMouseEnter / onScroll / onKeyDown / ...` | ✓ |
| Hook lifecycle | `useEffect(() => { ... }, [...])` | ✓ |
| Class lifecycle | `componentDidMount / componentDidUpdate` | ✓ |
| dotted handler | `onClick={this.fnX}` (class component method ref) | ✓ |
| class field arrow | `fnX = () => { ... }` | ✓ |
| 직접 axios | `fnX = () => axios.post("/api/...")` | ✓ |
| **redux + saga indirect** | `fnX = () => dispatch(actions.X)` + 같은 폴더 또는 `apps/<X>/` ↔ `store/<X>/` 의 saga.js 가 axios | ✓ (`+saga` 마커) |
| 커스텀 Route wrapper | `<PropsRouter path="..." component={...}/>` 처럼 `<Route>` 를 감싼 wrapper component | ✓ (자동 감지) |

### LLM 연결 (`.env`)

추출은 사내 LLM 게이트웨이 (`PATTERN_LLM_*` env) 를 사용. 미설정 시 `LLM_*` 로 fallback:

```env
PATTERN_LLM_API_BASE=https://사내LLM/v1
PATTERN_LLM_API_KEY=<key>
PATTERN_LLM_MODEL=qwen2.5-coder:14b    # 코드 특화 모델 권장
```

엔드포인트가 죽었거나 네트워크 실패 시에도 분석은 **regex fallback summary**
로 계속 진행 (`"if 3; throw 2; sql 1 (static heuristic)"` 같은 static 한 줄).

### 추출 결과

| 시트 | 컬럼 |
|------|------|
| **Business Logic** (백엔드) | Service#Method \| Validations \| Biz Rules \| State Changes \| Calculations \| External Calls \| Summary \| Source \| Programs |
| **Frontend Logic** (React) | Screen \| Button \| Handler \| URL \| Field Validations \| Pre-checks \| Conditional Calls \| State Reads \| Summary \| Source |
| **Programs** (요약 인라인) | 기존 컬럼 + `Business Logic` + `Frontend Validation` |

`Source=fallback` 은 노랑, `Source=cache` 는 재실행 hit (LLM 호출 0건).

### Scope / 성능 통제

- 백엔드: `_resolve_endpoint_chain` 이 도달한 ServiceImpl 메서드 +
  intra-class self-call 전이 closure (LLM 에 보낼 범위 자동 축소)
- 프론트: endpoint API URL 에 바인딩된 `onClick` handler 만 분석
- `get*` / `set*` / trivial 메서드 static 필터
- SHA-256 기반 디스크 캐시 (`output/legacy_analysis/.biz_cache/`) — 메서드
  body 가 안 바뀌면 재실행 시 LLM 호출 0건
- Batch 6 메서드/handler 당 1 LLM call (토큰 절약)

**메뉴 소스 우선순위**: `--skip-menu` > `--menu-md` > `--menu-xlsx` > DB (`config.yaml`)
- `--menu-md`: Markdown 파이프 테이블 (DRM 환경 권장, `input/menu_template.md` 참조)
- `--menu-xlsx`: Excel 파일 (`input/menu_template.xlsx` 참조)
- 둘 다 1레벨~5레벨 + URL 헤더, URL 있는 행만 프로그램으로 인정

**백엔드 프레임워크 자동 감지 + 분기**

분석을 시작하면 백엔드 루트의 `pom.xml` / `build.gradle` / `build.gradle.kts`
의존성을 스캔해 **Spring** 인지 **Vert.x** 인지 자동으로 판별합니다.
빌드 파일이 없는 경우 최대 200개의 `.java` 파일을 샘플링해 어노테이션/
상속 흔적(`@Controller` / `@RestController` vs `AbstractVerticle` /
`io.vertx`) 을 비교하는 휴리스틱으로 fallback 합니다.

| 감지 결과 | Controller 로 인정되는 클래스 |
|-----------|-------------------------------|
| `spring`  | `@Controller` / `@RestController` 어노테이션 (**Nexcore 포함**) |
| `vertx`   | `extends AbstractVerticle` 만 |
| `mixed`   | 둘 다 (Spring 과 Vert.x 소스가 한 레포에 공존) |
| `unknown` | 둘 다 (fallback — 어느 것도 명확히 감지되지 않음) |

감지 결과는 CLI 로그, Markdown 리포트 헤더, Excel `Summary` 시트의
`Backend framework` 행에 표시됩니다. Spring 프로젝트에 우연히 `extends
AbstractVerticle` 클래스가 섞여 있어도 해당 클래스는 controller 로 취급
되지 않고 서비스/유틸로만 취급됩니다(반대도 동일). 감지 결과가 의도와
다르면 프로젝트 루트에 적절한 `pom.xml` / `build.gradle` 을 두면 됩니다.

**프론트엔드 프레임워크 자동 감지**

`--frontend-dir` 지정 시 `package.json` 의존성 + 파일 콘텐츠 샘플링으로
**React** 인지 **Polymer** 인지 자동 판별합니다. 강제하려면
`--frontend-framework {react,polymer}` 를 지정하세요.

| 감지 신호 | React | Polymer |
|-----------|-------|---------|
| `package.json` | `react`, `react-dom`, `react-router-dom` | `@polymer/*`, `@vaadin/router`, `lit-element` |
| 콘텐츠 | `import React`, `from 'react'` | `customElements.define`, `Polymer({`, `extends LitElement` |

**핵심 설계 — Controller ↔ Menu 양방향 교차 검증**

URL을 정규화하여 (`/user/{id}`, `/user/:id`, `/user/{userNo}` → 동일 키)
양쪽을 인덱싱한 뒤 교집합/차집합으로 세 가지로 분류합니다.

| 분류 | 의미 |
|------|------|
| **Matched** | 메뉴와 Controller 모두 존재 (정상 프로그램 행) |
| **Unmatched Controller** | 코드는 있으나 메뉴에 없음 (내부 API 또는 메뉴 누락) |
| **Orphan Menu** | 메뉴는 있으나 Controller 없음 (미구현 또는 삭제된 기능) |

**설정 (`config.yaml`):**

```yaml
legacy:
  menu:
    table: "TB_MENU"
    columns:
      program_id: "PROGRAM_ID"
      program_nm: "PROGRAM_NM"
      url:        "URL"
      parent_id:  "PARENT_ID"
      level:      "LEVEL"
  rfc_depth: 2
```

메뉴 테이블의 컬럼명이 프로젝트마다 다르므로 각 매핑을 override 할 수
있습니다. 메뉴 트리는 `PARENT_ID` + `LEVEL` 로 구성되고,
leaf 행의 조상을 따라 `main_menu / sub_menu / tab / program_name` 4단계로
평탄화됩니다.

**지원하는 레거시 패턴:**

- **백엔드 프레임워크 — Spring**: `@Controller` / `@RestController` /
  `@Service` / `@Component` / `@Mapper` / `@Repository`, class + method
  레벨 `@RequestMapping` 계열 (배열 `{"/a","/b"}`, 동적 `/{id}`,
  `RequestMethod.GET` 포함)
- **백엔드 프레임워크 — Nexcore (SK C&C)**: `extends AbstractMultiActionBizController`
  / `AbstractSingleActionBizController` / `AbstractBizController` /
  `AbstractCommonBizController`. `@RequestMapping` 없이 **메서드명 컨벤션**으로
  endpoint 매핑 (`getList` → `/getList.do`). 파라미터에 `IDataSet` /
  `IBizServiceContext` / `IOnlineContext` 포함된 public 메서드만 endpoint 로 인정.
  Controller → Service → **DAO** (`@Repository`) → XML 체인 자동 추적.
  iBatis `sqlMapClientTemplate.queryForList("ns.id")` 패턴 인식
- **백엔드 프레임워크 — Vert.x**: 세 가지 패턴 지원:
  - **커스텀 어노테이션 (one-class-per-endpoint)**:
    `@RestVerticle(url = "/api/order/list", method = HttpMethod.POST, isAuth = true)`
    같은 프로젝트 로컬 어노테이션을 클래스 위에서 찾아 endpoint 를 생성.
    `url` 은 필수, `method` 는 `HttpMethod.GET` 등 형태로 선택적 지정
    (없으면 ANY), `isAuth` 등 나머지 속성은 무시
  - **상속 기반**: `extends AbstractVerticle` / `*Verticle` 로 끝나는 모든
    커스텀 base 클래스(`BaseVerticle` / `ReactiveVerticle` / …)
  - **라우팅 DSL**: `router.get("/x").handler(this::foo)` 형태. **`.handler(...)`
    체이닝이 반드시 뒤따라야** endpoint 로 인정하여 `map.get("...")` /
    `config.get("...")` 같은 일반 메서드 호출과의 false positive 를 차단
    - 리터럴: `router.get/post/put/delete/patch/options/head("/x").handler(...)`
    - 체인: `router.route().path("/x").method(HttpMethod.GET).handler(...)`
    - 핸들러 이름은 `this::foo` / `Class::bar` / `ctx -> ...` / `new X()`
      에서 자동 추출
- **의존성 주입**:
  - Spring: `@Autowired` / `@Resource` / `@Inject` 필드, **생성자 주입**,
    **Lombok `@RequiredArgsConstructor` / `@AllArgsConstructor`** (`private
    final` 필드)
  - Vert.x / plain Java: **어노테이션 없는 필드도 자동 수집**
    (`private OrderService orderService;` 형태. `static final` 상수는 제외)
- **MyBatis XML** 은 파일명과 무관하게 내용(`<mapper namespace="...">` +
  SQL 태그) 기준으로 자동 판별 — iBatis `<sqlMap>` 포맷도 동일하게 지원
- **MyBatis namespace** = Mapper 인터페이스 FQCN (기본), 단순 클래스명
  fallback
- 인터페이스 + `*Impl` 패턴 (`OrderService` → `OrderServiceImpl` 자동 추적)
- Abstract Controller 의 `extends` 체인 class-level mapping 상속
- **SAP JCo RFC / 인터페이스 호출**:
  - 표준: `destination.getFunction("Z_...")`, `JCoUtil.getCoFunction("Z_...")`
  - `String FN_XXX = "..."` 상수를 거친 **2-pass 해석**
  - **커스텀 RFC**: `siteService.execute("IF-GERP-180", param, ZMM_FUNC.class)`
    같은 서비스 래퍼 패턴. `patterns.yaml` 의 `rfc_call_methods: [execute, send]`
    로 활성화. 인터페이스 ID + SAP 함수명(.class) 모두 캡처
  - 서비스 → 서비스 체인의 **트랜지티브 수집** (`--rfc-depth`, 기본 3 — 3-hop service-of-service-of-service 까지 SQL/RFC/테이블 추적)
- **SQL namespace 변수**: `sqlSession.selectList(namespace + "findList", param)`
  에서 `String namespace = "com.example."` 상수를 2-pass 로 해석하여
  `com.example.findList` 로 결합
- **React Router** v5 `component={X}` / v6 `element={<X/>}` / 객체 라우트 /
  `React.lazy(() => import("./X"))` / 중첩 Route 의 부모 path 누적 결합
- **Polymer**: vaadin-router `setRoutes([{path, component}])` / page.js + iron-pages
  슬러그 페어링 / `<app-route pattern>` / `<dom-module id>` / `customElements.define` /
  `Polymer({is})` / `static get is()` / 파일명 컨벤션 (`x-tag.html` → `x-tag`)
- **메뉴 소스**: DB 테이블 (`config.yaml`), Excel (`--menu-xlsx`, 1~5레벨),
  **Markdown** (`--menu-md`, DRM 환경 권장). 템플릿: `input/menu_template.md` / `.xlsx`
- **인코딩**: UTF-8 / EUC-KR / CP949 / Latin-1 자동 fallback

**산출물:**

```
output/legacy_analysis/<YYYYMMDD>/
├── patterns.yaml                           # discover-patterns 산출물
├── as_is_analysis_<slug>_TIMESTAMP.md      # Markdown 리포트
└── as_is_analysis_<slug>_TIMESTAMP.xlsx    # Excel (7개 기본 시트 + 최대 3개 opt-in)
    ├── Sheet: Summary                 (전체 집계)
    ├── Sheet: Programs                (메인 — 16개 컬럼)
    ├── Sheet: Menu Hierarchy          (메뉴 계층 + 매칭 여부)
    ├── Sheet: Unmatched Controllers   (메뉴 없는 컨트롤러)
    ├── Sheet: Orphan Menu Entries     (컨트롤러 없는 메뉴)
    ├── Sheet: RFC Calls               (SAP 인터페이스 cross-reference)
    ├── Sheet: Tables Cross-Reference  (테이블별 사용 프로그램)
    ├── Sheet: Business Logic          (opt-in `--extract-biz-logic` Phase A)
    ├── Sheet: Frontend Logic          (opt-in `--extract-biz-logic` Phase B)
    ├── Sheet: Program Specification   (opt-in `--extract-program-spec` Phase II)
    ├── Sheet: Sequence Diagrams       (opt-in `--sequence-diagram`)
    └── screens/<file>.html            (opt-in `--extract-screen-layout` Phase C — 별도 폴더)
```

`--sequence-diagram` 은 리포트 파일명과 같은 이름의 폴더
(`as_is_analysis_<slug>_<ts>/`) 도 같이 만들어 그 안에 **그룹별 `.md`
파일** (Mermaid 코드 포함) 을 저장합니다. 그룹 단위는
`--sequence-diagram-group` 으로 선택:

| 옵션 | 묶음 단위 |
|---|---|
| `main_menu` (default) | 업무 대분류 (메뉴 1뎁스) — 한 화면 안에 여러 endpoint |
| `menu_path` | 메뉴 1+2+3뎁스 합쳐 더 세분화 |
| `sub_menu` | 메뉴 2뎁스 |
| `controller_class` | Java Controller 단위 |
| `backend_project` | **레포 단위** (`--backends-root` 모드 sub-project 별) |
| `none` | endpoint 별 한 파일씩 (legacy 동작) |

상세는 아래 "Sequence Diagram" 섹션 참고.

**Programs 시트 컬럼** — 메뉴 데이터 유무에 따라 schema 자동 전환:

*WITH menu (24열)*: `No, 메뉴1뎁스, 메뉴2뎁스, 메뉴3뎁스, Menu path,
Menu URL, Frontend project, Frontend screen, **Trigger**, Frontend
Validation, Program, HTTP, Controller URL, Controller file, Controller,
Service, Service method, Business Logic, XML, XML method, Tables,
Columns, Procedure, RFC`

*NO menu (`--skip-menu`, 18열)*: `Program, HTTP, URL, File, Frontend
project, React, **Trigger**, Frontend Validation, Controller, Service,
Service method, Business Logic, XML, XML method, Tables, Columns,
Procedure, RFC`

**Trigger 컬럼** 의 라벨 형식:
```
[onClick] 조회
[onClick+saga] 등록           ← redux+saga indirect
[useEffect] mount
[componentDidMount] mount
[onChange] handleSearch
```
`[event]` 가 트리거 종류, 그 뒤가 버튼 텍스트 또는 handler 이름.
`+saga` 는 dispatch → 같은 폴더 또는 같은 app-slug (apps/X ↔ store/X)
의 saga 가 실제 axios 호출하는 indirect handoff 라는 의미.

**다중값 셀** (Trigger / React file / Service / XML method / Tables 등)
은 `;\n` 구분자로 한 셀 안 항목당 한 줄 — Excel/Markdown 가독성.

매칭되지 않은 행(unmatched controller)은 **노란색**, 매퍼 체인이 없는
행은 **회색**으로 하이라이트됩니다.

**컬럼 포맷 — 가독성 개선**:

여러 항목이 들어가는 컬럼은 Excel 셀 안에서 **한 항목당 한 줄씩** 보이도록
구분자에 개행을 넣어 emit 합니다 (`wrap_text=True` 적용). 단일 항목일 때는
개행 없이 그대로 표시.

| 컬럼 | 구분자 | 추가 annotation |
|---|---|---|
| `Tables` | `,\n` | 테이블명 뒤에 `(CRUD)` suffix — `C`(INSERT) / `R`(SELECT) / `U`(UPDATE) / `D`(DELETE) 조합 |
| `Columns` | `,\n` | `TABLE.COLUMN[한글](CRUD)` — sqlglot AST 로 SELECT projection / INSERT column list / UPDATE SET LHS / MERGE WHEN 절 컬럼 단위 CRUD 추출. `--terms-md` 지정 시 용어사전 매칭 컬럼에 `[한글]` 병기, 없으면 생략. `SELECT *` 는 컬럼 열거 불가 → 미표시 (Tables 컬럼에 R 은 여전히 표시) |
| `Procedure` | `,\n` | MyBatis SQL 에서 호출하는 Oracle 스토어드 프로시저 / 패키지 (`CALL` / `{CALL}` / `EXEC` / `EXECUTE` / PL/SQL `BEGIN...END;` / `<procedure>` 태그) |
| `RFC` | `,\n` | — |
| `Service` / `Service method` / `XML` / `XML method` | `;\n` | — |

Tables 컬럼 실제 출력 예 (1 셀 안 4 줄):
```
CMN_BTN_ROLE(R),
CRHD_W(RU),
IFLOT_W(C),
EQUI_W(CRUD)
```
→ `CMN_BTN_ROLE` 은 SELECT 만, `CRHD_W` 는 SELECT+UPDATE, `IFLOT_W` 는
INSERT 만, `EQUI_W` 는 네 가지 모두. STATEMENT / PROCEDURE / 네임스페이스
fallback 처럼 작업 타입을 단정할 수 없는 경우는 letter 없이 테이블명만
표시. `Tables Cross-Reference` 시트는 `(CRUD)` suffix 를 자동 제거해서
bare 테이블명 기준으로 집계합니다.

Columns 컬럼 실제 출력 예 (같은 endpoint, 용어사전 매칭):
```
CRHD_W.STATUS[상태](U),
EQUI_W.V(U),
IFLOT_W.ID[식별자](C),
IFLOT_W.NAME[이름](C)
```
→ UPDATE SET / INSERT column 리스트 기반. sqlglot 파싱 실패 (PL/SQL 블록,
Oracle hint 일부 구문 등) 시 해당 statement 만 skip — Tables 컬럼의
table-level CRUD 는 그대로 유지되므로 정보 손실 없음.

**Program Specification 시트 — endpoint narrative 자동 생성 (`--extract-program-spec`)**:

"프론트 버튼 클릭 → validation → 비즈니스 로직 → DML 컬럼" 을 한 줄 narrative
로 LLM 에서 자동 생성. Phase A (`--extract-biz-logic` 백엔드 biz summary) +
Phase B (React handler summary) 결과 + Phase I 컬럼 CRUD 를 **원본 body 없이
요약만 재조립** 해서 LLM 에 전달 → 토큰 절감 + 중복 호출 회피.

```powershell
python main.py analyze-legacy `
  --backend-dir C:\work\backend `
  --frontend-dir C:\work\frontend `
  --menu-md input\menu.md `
  --extract-biz-logic `
  --extract-program-spec
```

옵트인 플래그. `--extract-biz-logic` 없이는 에러 (narrative 는 Phase A/B 결과
가 입력).

Program Specification 시트 컬럼 (15개):
- Main / Sub / Tab / Program / HTTP / URL — 메뉴 + endpoint 식별
- Trigger label / Trigger type — 버튼 label (React 에서 추출) + READ /
  CREATE / UPDATE / DELETE / COMPOSITE / OTHER 분류
- Input fields / Validations / Business flow — 프론트 수집 field, 검증,
  서비스 chain narrative
- Read targets / Write targets — `TABLE.col(R)` / `TABLE.col(C/U/D)` 나열.
  LLM 이 column_crud 외 임의 컬럼 만들면 후처리에서 drop (hallucination
  차단)
- Purpose / Source — 한 문장 목적, `llm` / `fallback` / `cache`

LLM 호출 당 endpoint 10개 배치, 캐시 `output/legacy_analysis/.spec_cache/`.
재실행 시 변경 없는 endpoint 는 0 cost. LLM endpoint down 시 `fallback`
source 로 trigger_type + write_targets 는 채워 주고 narrative 필드는
공백 (노란색 하이라이트).

**Frontend-only 모드 (`--frontend-only`)**:

backend / 메뉴 / 컨트롤러 체인 모두 skip 하고 React frontend 만 스캔.
프론트만 빠르게 분석하고 싶을 때 (mock backend 같은 거 없이) 사용.

```powershell
python main.py analyze-legacy `
  --frontend-only `
  --frontends-root C:\work\frontend `
  --skip-menu `
  --extract-screen-layout `
  --screen-max 50
```

**산출물**:
- `output/legacy_analysis/<YYYYMMDD>/frontend_only_summary.txt` — URL ↔
  Trigger 매핑 요약
- `output/legacy_analysis/<YYYYMMDD>/screens/<file>.html` — 화면 mockup
  (`--extract-screen-layout` 사용 시)

`--backend-*` 인자는 무시됨 (실수로 줘도 경고 후 진행).

**Screen Layout — 화면 구조 추출 + HTML mockup (`--extract-screen-layout`, Phase C)**:

각 React 화면 파일을 LLM 으로 분석해 **Page Title / Search Panel / DataTable
/ Edit Mode / Tabs / 이벤트→백엔드 URL 매핑** 을 구조화 JSON 으로 추출 후
정적 HTML mockup 으로 렌더. 폐쇄망 외부 의존 0 (인라인 CSS, Bootstrap 미사용).

```powershell
python main.py analyze-legacy `
  --backends-root C:\work\backend `
  --frontends-root C:\work\frontend `
  --menu-md input\menu.md --menu-only `
  --extract-biz-logic `
  --extract-screen-layout `
  --screen-max 50
```

**산출물**: `output/legacy_analysis/<YYYYMMDD>/screens/<file>.html` — 화면별
HTML 한 개. 사용자 PC 에서 브라우저로 더블클릭하면 와이어프레임 형태로:
- 헤더 (Page Title)
- Search Panel — 필드 라벨 / 컴포넌트 종류 (DatePicker/Select/Input/...) /
  default 값 / 옵션
- Tabs — 탭 이름 리스트
- DataTable — 컬럼 헤더 + 3 줄 placeholder
- Edit Mode — 편집 폼 필드들
- **이벤트 → 백엔드 URL 매핑 표** — 각 버튼/이벤트가 호출하는 endpoint
  (정적 분석 + LLM 보강 결과)

**LLM 호출**: 화면당 1 회 (`--screen-max` 로 cap). 디스크 캐시
`output/legacy_analysis/.screen_cache/` — 재실행 시 0 cost. LLM 엔드포인트
down 시 fallback 으로 events 는 정적 분석 결과만 채움.

**옵션 G — 진짜 스크린샷 (`--render-screenshots`)**: 현재 stub. Playwright
+ React 빌드/실행 환경 갖춰지면 follow-up 에서 활성화.

**옵션 H — AST closure 보강 (`--closure-llm` / `--closure-popup-augment`)**:

regex 기반 기본 분석으로 잡히지 않는 케이스를 **tree-sitter AST closure**
로 보강하는 옵트인 옵션 (PR #171/#172). 두 플래그 독립 사용 가능.

| 플래그 | 효과 |
|--------|------|
| `--closure-llm` | Phase C 화면 LLM 분석 시 raw JSX 슬라이스 대신 **import 그래프 BFS + popup 3 신호 (JSX 태그 / open hook / open API) facts box** 를 LLM 에 입력 → 화면 의존성 / popup 흐름을 LLM 이 더 정확히 파악 |
| `--closure-popup-augment` | popup 검색에 AST 신호 추가 → `<Modal>` 안 import 만 보는 기존 휴리스틱이 놓친 popup (`*Dialog` / `*Popup` / `*Layer` 등 이름 suffix 매칭 컴포넌트가 `<Modal>` 없이 직접 렌더되는 케이스) 을 잡음 |

**필수 조건**: `tree-sitter` + `tree-sitter-javascript` + `tree-sitter-typescript`
설치 (`requirements.txt` 참고). **미설치 시 자동 skip + warning 로그**, 기존
regex 분석은 그대로 동작 — 회귀 위험 없음.

```powershell
# 폐쇄망 wheel install (Windows + Python 3.11 가정)
pip download tree-sitter tree-sitter-javascript tree-sitter-typescript ^
  -d .\wheels --platform win_amd64 --python-version 311 --only-binary=:all:
python -m pip install --no-index --find-links=.\wheels ^
  tree-sitter tree-sitter-javascript tree-sitter-typescript

# 두 옵션 동시 사용 (LLM input 보강 + popup 보강)
python main.py analyze-legacy `
  --backends-root C:\work\backend `
  --frontends-root C:\work\frontend `
  --menu-md input\menu.md --menu-only `
  --extract-screen-layout `
  --closure-llm `
  --closure-popup-augment
```

**조정 옵션**: `--closure-max-depth` (기본 3, BFS 깊이) /
`--closure-token-budget` (기본 12000, closure 직렬화 토큰 상한). 일반적으로
기본값으로 충분.

**Sequence Diagram — Mermaid 시퀀스 다이어그램 자동 생성 (`--sequence-diagram`)**:

endpoint 당 **Controller → Service → Mapper → DB / RFC** 호출 체인을
Mermaid `sequenceDiagram` 으로 자동 렌더. **LLM 불필요** — 파서만으로
source offset 기반 호출 순서 + 제어 블록 (if/else/for/while/switch/try)
을 결정적으로 추출해서 `alt/loop/opt/end` 래핑까지 emit.

```powershell
python main.py analyze-legacy `
  --backend-dir C:\work\backend `
  --frontend-dir C:\work\frontend `
  --menu-md input\menu.md `
  --sequence-diagram
```

**출력 위치** (리포트 파일과 같은 prefix 로 폴더 생성):

```
output/legacy_analysis/
├── as_is_analysis_myapp_20260424_123456.md       # 통합 리포트 (Mermaid 섹션 포함)
├── as_is_analysis_myapp_20260424_123456.xlsx     # Excel (+ Sequence Diagrams 시트)
└── as_is_analysis_myapp_20260424_123456/         # 그룹별 .md 폴더
    ├── 001_주문관리.md       # main_menu 그룹 (default)
    ├── 002_재고관리.md
    └── ...
```

`--sequence-diagram-group <option>` 으로 묶음 단위 선택:
- `main_menu` (default), `menu_path`, `sub_menu`, `controller_class`,
  `backend_project` (레포 단위), `none` (endpoint 별 1 파일).

각 `.md` 파일은 그룹의 모든 endpoint 별 ` ## 1. <program>` 헤더 +
메타데이터 (Controller / Service / Tables / Columns / RFC / procedures)
+ ```` ```mermaid ```` 코드블럭. GitHub / VSCode Mermaid Preview 에서
즉시 렌더, 아니면 <https://mermaid.live> 에 복붙.

**다이어그램 구조 — 고정 참가자 순서**:

```
User → Controller → Service (체인 순) → Mapper → DB → SAP
```

등장 안 하는 카테고리는 선언 생략 (RFC 없으면 SAP 생략 등).

**제어 블록 → Mermaid 매핑 (Phase B, 파서 기반)**:

| Java | Mermaid | 예시 |
|---|---|---|
| `if` / `else if` / `else` | `alt IF <cond>` / `else ELSE IF <cond>` / `else ELSE` | `alt IF param != null` |
| `for` / `while` / `do-while` | `loop FOR` / `loop WHILE` / `loop DO-WHILE` | `loop FOR Order o : orders` |
| `switch` | `alt SWITCH <cond>` | `alt SWITCH type` |
| `try` / `catch` / `finally` | `opt TRY` / `else CATCH <ex>` / `else FINALLY` | `else CATCH Exception e` |

Mermaid 자체 키워드는 `alt/loop/opt/else/end` 로 고정이지만, 조건 앞에
`IF` / `FOR` / `TRY` 같은 접두어를 붙여 Java 원래 구조가 한눈에
들어오게 함.

**실제 출력 예**:

````
```mermaid
sequenceDiagram
    actor User
    participant C as OrderController
    participant S as OrderServiceImpl
    participant Mapper as Mapper
    participant DB as DB
    User->>C: GET /orders
    Note over C: handle()
    C->>S: go()
    loop FOR int i = 0, i ＜ list.size(), i++
        S->>Mapper: SELECTONE OrderMapper.findById
        Mapper->>DB: TB_ORDER
    end
    alt IF list.size() ＞ 0
        S->>Mapper: INSERT OrderMapper.log
        Mapper->>DB: TB_ORDER_LOG
    end
```
````

**특수 문자 처리**:

- `<` → `＜` (U+FF1C 전각), `>` → `＞` (U+FF1E 전각) — Mermaid 가
  화살표 문법 (`->>`, `<<-`) 으로 오해하는 것 방지. 시각은 부등호와
  동일
- `;` → `,` — statement separator 오인 방지 (for-loop 조건 안전)
- `:` → ` `, `"` → `'` — Mermaid label 구분자 회피
- 80자 초과 조건 → `…` 절단 — 장문 Java 조건이 block label 깨는 케이스 방지

**Phase II 와 독립**: `--extract-program-spec` 없이 단독 사용 가능 (LLM
불필요). `--extract-biz-logic` / `--extract-program-spec` 과 함께 쓰면
비즈니스 narrative + 시퀀스 다이어그램 양쪽 다 생성됨.

### 12. SQL Migration — AS-IS → TO-BE 스키마 기반 쿼리 변환

Oracle 11g 에서 Oracle 23ai 로 스키마가 바뀐 환경에 맞춰 기존 MyBatis 쿼리를
일괄 변환합니다. 3-tier 구조: **DSL 매핑 (우선) → LLM fallback (복잡 케이스) →
수동 큐 (신뢰도 < 0.7)**. 검증도 2-stage: **Stage A** (sqlglot static),
**Stage B** (TO-BE DB 에서 parse-only). 스펙은 [`docs/migration/spec.md`](docs/migration/spec.md).

**0) (선택) 기존 매핑 .md → YAML 자동 변환**: 팀에 이미 AS-IS↔TO-BE 테이블/컬럼
매핑 문서가 markdown 으로 있다면 LLM + heuristic 이 kind (rename / type_convert /
split / merge / value_map / drop) 를 추론해 YAML 로 변환:

```powershell
python main.py convert-mapping `
  --mapping-md .\docs\as_is_to_be_mapping.md `
  --output .\input\column_mapping.yaml
# --no-llm 로 heuristic 만 사용 (pipe table 헤더 synonym 매칭)
```

**권장: 9-컬럼 flat 포맷** (사용자 표준, DRM-safe txt/md). 샘플은
`input/column_mapping_template.md` 참고. 한 행 = 한 컬럼 매핑:

```
| asis_table | asis_column | asis_column_type | tobe_table | tobe_table_comment
| tobe_column | tobe_column_type | tobe_column_comment | remark |
```

헤더 synonym 매칭 + type pair (VARCHAR2(8)↔DATE, VARCHAR2(14)→TIMESTAMP,
VARCHAR2↔NUMBER) 자동 transform 추론 + tobe_comment 는 rich YAML 의
`comment` 필드로 보존되어 다음 단계에서 **변환 SQL 에 자동 인라인 주석**
소스로 사용 (`options.comment_source: mapping` / `mapping_first`).

**1) 매핑 파일 작성** (`input/column_mapping.yaml` — 템플릿 제공):

```yaml
tables:
  - as_is: CUST
    to_be: CUSTOMER_MASTER
    type: rename

columns:
  - as_is: { table: CUST, column: CUST_NM }
    to_be: { table: CUSTOMER_MASTER, column: CUSTOMER_NAME }
  - as_is: { table: CUST, column: REG_DT, type: "VARCHAR2(8)" }
    to_be: { table: CUSTOMER_MASTER, column: REGISTER_DATE, type: "DATE" }
    transform:
      read:  "TO_DATE({src}, 'YYYYMMDD')"
      where: "TO_DATE({src}, 'YYYYMMDD')"
  - as_is: { table: CUST, column: USE_YN }
    to_be: { table: CUSTOMER_MASTER, column: IS_ACTIVE, type: "NUMBER(1)" }
    value_map: { "Y": 1, "N": 0 }
```

지원 케이스: **1:1 rename / 타입 변환 / 컬럼 분할 (1:N) / 컬럼 병합 (N:1) /
값 재매핑 / 삭제**.

**2) 사전 영향분석 (실제 변환 안 함)**:

```powershell
python main.py migration-impact `
  --mybatis-dir C:\work\mapper `
  --mapping input\column_mapping.yaml `
  --as-is-schema .\output\스키마.md `
  --to-be-schema .\output\to_be_schema.md
```

출력: `output/migration/<날짜>/impact_report_TIMESTAMP.xlsx` (5 시트 — Summary /
Table Impact / Column Impact / Affected Statements / Validation).

**3) 실제 변환 + Stage A 검증**:

```powershell
python main.py migrate-sql `
  --mybatis-dir C:\work\mapper `
  --mapping input\column_mapping.yaml `
  --to-be-schema .\output\to_be_schema.md `
  --terms-md .\output\terms_dictionary.md `
  --emit-column-comments `
  --llm-fallback
```

출력:
- `output/migration/<날짜>/sql_migration_TIMESTAMP.xlsx` — 5 시트 (Summary /
  Conversions (18컬럼) / Validation Errors / Unresolved Queue / Mapping Coverage)
- `output/migration/<날짜>/converted/<원본 경로>.xml` — 구조 보존 치환된 XML.
  각 statement 위에 `MIGRATION` 메타데이터 블록 + `AS-IS (original)` 주석.

주요 플래그:
- `--emit-column-comments`: `SELECT CUSTOMER_NAME /* 사용자명 */` 식 한글 주석 삽입
- `--llm-fallback`: NEEDS_LLM 상태 statement 를 사내 LLM 으로 보조 변환 시도
- `--no-xml-preserve-as-is`: AS-IS 주석 블록 skip
- `--dry-run`: 리포트만 생성, 파일 쓰지 않음
- `--format-only`: 매핑 / TO-BE 스키마 없이 **포매터만** 적용 — 줄맞춤 / 메타블록
  양식 사전 검토용 (아래 §3.5 참고)

**3.5) 매핑 작성 전 포매터 양식만 미리보기 (`--format-only`)**:

매핑 yaml / TO-BE 스키마 .md 가 아직 없을 때 **AS-IS XML 만 던져서**
변환기가 어떤 양식으로 출력할지 미리 visual 검토할 수 있습니다.
KoreanLegacy 포매터의 줄맞춤 / 리딩 콤마 / 키워드 우측정렬을 마음에 들어
하는지 먼저 확인하고, 그 뒤 매핑 yaml 작성으로 진행하는 흐름.

```powershell
# 디렉토리 전체
python main.py migrate-sql `
  --mybatis-dir C:\work\mapper `
  --format-only `
  --output-format xml

# 한 파일만 빠르게 — --mybatis-dir 에 .xml 경로 직접 지정 가능
python main.py migrate-sql `
  --mybatis-dir C:\work\mapper\CustomerMapper.xml `
  --format-only `
  --output-format xml
```

각 statement 위에 3 코멘트 블록이 emit 됩니다:

| 블록 | 내용 |
|---|---|
| **MIGRATION 메타블록** | Applied / Changed / Stage A / Stage B / ORA / Notes 모두 `-` (placeholder) — 매핑이 없으니 변환 0건이라 자연스럽게 비어 있음 |
| **AS-IS (original)** | 입력 SQL 원본 (max-path 평탄화) |
| **SUGGESTED TO-BE** | KoreanLegacy 포매터 결과 — leading comma / 6-char keyword 우측정렬 / 동적 태그 평탄화된 형태. 본문은 활성화되지 않는 코멘트라 실행 안전 |

본문 (실제 statement body) 은 원본 layout + `<if>`/`<choose>` 등 동적 태그
**그대로 보존**. Stage A 검증 (sqlglot 스키마 lookup) 도 자동 skip 됩니다 —
스키마 dict 가 비어있어 의미 없으므로.

용도:
- 사용자가 마음에 드는 포매터 옵션 (`leading_comma`, `keyword_case`, etc.) 을
  결정한 뒤 column_mapping.yaml `options.output_format` 에 옮기기
- 회사 표준 SQL 양식과 비교해서 변환기 출력이 적합한지 사전 합의용
- 차세대 전환 PoC 단계에서 매핑 데이터 없이도 산출물 샘플 시연용

**column_mapping.yaml 의 `options` 로 제어 가능한 UX 옵션**:

```yaml
options:
  emit_column_comments: true
  comment_source: mapping          # mapping | mapping_first | to_be_schema | terms_dictionary | both
  output_format:
    style: korean_legacy            # none (기본, 단일 라인) | korean_legacy | ansi
    leading_comma: true
    normalize_comment_width: true
    table_comment_prefix: "T:"
```

- `comment_source: mapping` → 위 9-컬럼 flat 매핑의 `tobe_*_comment` 가 바로
  SQL 주석 소스. 별도 terms/schema 파일 불필요
- `output_format.style: korean_legacy` → 변환 SQL 이 리딩-콤마 + 6-char
  keyword 우측정렬 + 블록 내 컬럼/주석 폭 통일 + 테이블 `T:` prefix 로 emit.
  샘플:
  ```
  SELECT CUSTOMER_ID                                  /* 고객ID     */
       , CUSTOMER_NAME                                /* 고객명      */
       , TO_DATE(CUSTOMER.REGISTER_DATE, 'YYYYMMDD')
    FROM CUSTOMER                                     /* T:고객 마스터 */
   WHERE CUSTOMER_ID = #{id}
  ```

**4) Stage B 검증 — TO-BE DB 에서 parse-only**:

```powershell
python main.py validate-migration `
  --converted-dir .\output\sql_migration\converted `
  --dsn to_be_host:1521/newdb `
  --parallel 10
```

`cursor.parse()` 로 실행 없이 구문 + 스키마 검증만. `--dry-run` 으로 DB 없이
statement 수집 구조만 확인 가능.

---

### 13. 형태소분석 (morpheme)

속성명 텍스트 파일 (예: 한 줄당 `BACKEND지역값`, `LOT_ID_LIST`, `설비가동률현황` 등
~2만개) 을 LLM 으로 **단어 경계 단위로 분해** 해 표준화 우선순위 판단에 쓸
리포트를 만듭니다. `terms` 가 약어/영문명/한글명의 "의미 해석" 이라면 `morpheme`
은 그 앞 단계의 "단어 분리" 에 특화된 커맨드입니다.

**1) 지침 템플릿 복사 후 프로젝트 도메인에 맞게 수정**:

```powershell
copy input\morpheme_guide_template.md input\morpheme_guide.md
# morpheme_guide.md 를 열어 원칙/Few-shot/업계 약어 리스트를 도메인에 맞춰 조정
# (gitignore 가 *_template.* 외 input/ 파일은 자동 제외합니다)
```

지침 템플릿에 기본 포함되는 것:

- 역할 + 원칙 6개 (언어 경계 자동 분리 / 구분자 처리 / CamelCase 분리 /
  업계 표준 약어 보존 / 숫자 suffix 규칙 / 한글 과분해 금지)
- Few-shot 예시 7개 (원칙 1~6 각각 커버 + 엣지 케이스 1. 권장 5~8)
- 배치 처리 규칙 (자동 조정 공식, JSON 실패 시 축소 재시도 정책)
- 속도 추정표 (2만건 기준, Ollama / vLLM / 사내 게이트웨이별)

**2) 속성명 파일 준비** (`attrs.txt` — 한 줄당 1속성, `#` 주석 허용):

```
# 반도체 제조 + 공급망 속성 2만건
BACKEND지역값
WAFER_LOT_ID
설비가동률현황
FABRunRate2024
PPID관리번호
...
```

**3) 실행**:

```powershell
python main.py morpheme `
  --input C:\work\attrs.txt `
  --guide input\morpheme_guide.md

# 배치/병렬 튜닝 (기본 자동)
python main.py morpheme --input attrs.txt --guide input\morpheme_guide.md `
  --batch-size 30 --parallel 4 --timeout 120
```

기본 배치 크기: `max(10, min(50, 1200 // 평균 속성길이))` 자동. 사내 LLM
게이트웨이에서 rate limit 이 여유 있다면 `--parallel 4` 정도로 올려도 됩니다.

**산출물** (`output/morpheme/<YYYYMMDD>/`):

```
output/morpheme/<YYYYMMDD>/
├── morpheme_TIMESTAMP.md    # Summary + 저신뢰/실패/잘림 상위 20 샘플
└── morpheme_TIMESTAMP.xlsx  # 단일 시트 "형태소분석" (15 컬럼)
    └── 속성명 | 컨피던스 | 단어1 | 단어2 | ... | 단어12 | 비고
```

xlsx 행 하이라이트:
- **노랑** — `confidence < 0.7` 저신뢰도 (수동 검토 큐)
- **빨강** — 파싱 실패 (LLM 응답 누락/형식 오류, 2단계 재시도 후 실패 건)
- **파랑** — 단어 13개 이상 → 단어12 까지만 저장, 비고에 "13번째 이후 N개
  생략: ..." 기록

**처리 시간 추정 (2만 건 기준)**:

| LLM 환경 | 스루풋 | 순차 | 4병렬 |
|---------|--------|------|-------|
| Ollama CPU | 30 tok/s | 7.4h | 1.9h |
| vLLM 중급 GPU | 80 tok/s | 2.8h | 42분 |
| 사내 게이트웨이 | 200+ tok/s | 67분 | 17분 |

---

### 14. 화면변환기 (screen-converter) — AS-IS 캡처 → TO-BE PPTX (PoC)

소스 코드가 없는 화면 (외주 모듈, 레거시 ASP/JSP, 외부 시스템 캡처) 의
AS-IS 화면 캡처 이미지를 **Vision LLM (VLM) 이 DRM 잠긴 PPT 템플릿 캡처와
함께 보고** TO-BE 화면 레이아웃을 자동 생성합니다. 결과는 편집 가능한
python-pptx 도형 (제목/검색 패널/표/버튼/노트) 으로 떨어집니다.

**전제**:
- `LLM_API_BASE` / `LLM_API_KEY` 에 vision-capable 모델 엔드포인트 연결
  (예: Qwen3-VL 계열, LLaVA, Qwen2.5-VL 등). 모델 선택 우선순위는
  `PATTERN_LLM_MODEL` > `LLM_MODEL` > config.yaml `llm.model` 순
  (`legacy_pattern_discovery._call_llm` 공유) — 화면변환 전용 키는
  없으므로, `PATTERN_LLM_MODEL` 을 vision 모델로 두거나 미설정 시
  `LLM_MODEL` 이 vision 모델이어야 합니다. `discover-patterns` /
  `analyze-legacy --extract-biz-logic` 도 같은 키를 공유하므로, vision
  모델 하나로 양쪽을 함께 다룰 수 있는지 검토 후 결정하세요.
- DRM 으로 PPT 템플릿 파일 자체는 못 열어도, **템플릿 슬라이드를 화면
  캡처한 png/jpg** 가 1장 이상 있으면 됩니다 (여러 장 권장 — 표지/본문/
  표 슬라이드 등).

**실행**:

```powershell
# 1) 폴더 준비 — 파일명이 곧 화면명 (PPTX 슬라이드 타이틀 기본값)
mkdir input\asis_captures
mkdir input\template_captures
# (AS-IS 화면 캡처들 → input\asis_captures\*.png
#  DRM 템플릿 캡처들 → input\template_captures\*.png)

# 2) 변환
python main.py screen-converter `
  --captures-dir input\asis_captures `
  --templates-dir input\template_captures

# 출력: output\screen-converter\<YYYYMMDD>\screens.pptx
```

**옵션**:
- `--output <path>` — PPTX 출력 경로 명시 지정 (기본:
  `output/screen-converter/<YYYYMMDD>/screens_<HHMMSS>.pptx` —
  파일명에 시각 stamp 가 들어가서, 이전 결과를 PowerPoint 에 열어둔
  채 재실행해도 잠금 충돌 없이 새 파일로 저장됨)
- `--frontend-dir <path>` — (선택) React/Vue 소스 루트. 캡처 파일명에
  매칭되는 entry 컴포넌트 파일을 찾고, **`legacy_react_closure.build_closure`
  를 호출해 entry + import 그래프 BFS 로 자식 컴포넌트까지 한 덩어리로
  번들** 한 뒤 VLM 프롬프트에 Markdown 으로 첨부. 즉 `index.tsx` 의
  `render()` 안에 `<SearchPanel/>` / `<OrderTable/>` 같이 분리된 자식
  들도 같이 LLM 에 던져짐 (max_depth=3, token_budget=20K). 라벨/컬럼/
  버튼 정확도가 크게 향상 (VLM 이 픽셀에서 한글 읽는 대신 JSX 의
  텍스트를 그대로 옮김). 이미지는 `regions` (위치/크기) 추론에만 사용.
  - **tree-sitter 필수** (`pip install tree-sitter tree-sitter-javascript
    tree-sitter-typescript`). 미설치 시 자동 fallback — 단일 entry 파일을
    8000자까지 잘라서 첨부 (자식 컴포넌트 미포함). 콘솔에 `(closure 미사용:
    ModuleNotFoundError ...)` 표시됨. 폐쇄망 wheel install 가이드는
    `CLAUDE.md` 의 "tree-sitter (옵트인)" 참고.
- `--source-mapping <yaml>` — (선택) 휴리스틱 매칭이 실패하는 캡처
  (예: 한글 캡처명 vs 영문 컴포넌트명) 를 수기로 매핑. 값이 상대 경로면
  `--frontend-dir` 기준 resolve:
  ```yaml
  # input/screen_source_mapping.yaml
  주문조회: src/pages/order/OrderInquiry.tsx
  사용자관리: src/pages/admin/UserMgmt.tsx
  ```
- `--export-html` — (선택) PPTX 외에 **TO-BE HTML 도 추가 생성**.
  `--style-css` 의 CSS 와 React 소스를 LLM 에 넘겨 body 마크업을 받고
  `<link rel=stylesheet href=tobe_style.css>` 로 묶어 화면별 html +
  index.html 산출. 브라우저로 열어 TO-BE 디자인 그대로 확인 가능
  (PPTX 도형 재구성 한계 우회).
  - **소스 매칭된 화면**: **text-only LLM 호출** (이미지 0). React JSX
    가 정답 — 코딩 모델로도 동작 + vision 모델보다 빠르고 안정. CSS 가
    스타일 정답이라 템플릿 이미지도 불필요.
  - **소스 매칭 실패한 화면 (legacy 외주 등)**: AS-IS 이미지 1장 +
    CSS 로 vision fallback.
  - 콘솔에 `source N장, vision M장` 으로 표시.
  - VLM 출력이라 매 실행 변동성 있음 — **시각 검토용** (deterministic
    정의서는 `screen-spec` 의 xlsx 가 담당).
  - 출력: `output/screen-converter/<날짜>/html/`.

**Figma 친화 출력 (`--export-html` 의 권장 워크플로우)** — PPTX 도형이
복잡한 레이아웃 표현 한계가 있어서, **HTML → Figma 플러그인**으로 옮겨
디자이너가 정제하는 흐름을 권장:
- 각 `<화면>.html` 은 **single-file inline CSS + viewport 1440px + 한글
  폰트 fallback**. 외부 fetch 없이 한 파일만 import 하면 됨 (사내 망
  안전).
- Figma 의 다음 플러그인 중 하나로 import (Figma Community 검색):
  1. **html.to.design** — 가장 인기, single-file 정확도 좋음.
  2. **Anima** — 가져온 뒤 component / Auto-layout 자동 변환 지원.
  3. **Builder.io Visual Copilot** — AI 기반 변환.
- 사용 흐름: 플러그인 실행 → "Import from file" → `<화면>.html` 선택
  → Figma frame 으로 자동 변환 → 디자이너가 색/폰트/spacing 정제.
- `index.html` 에 인덱스 + 플러그인 안내 cheat-sheet 포함.

**`--export-svg` 옵션 — Figma paste 가장 안정적인 경로** — HTML 의 LLM 환각
(엉뚱한 화면 생성) 위험이 부담스러우면 SVG 가 더 안전:

| 항목 | HTML | SVG |
| --- | --- | --- |
| LLM 역할 | layout JSON 추출 + **마크업 직접 생성** | layout JSON 추출 만 |
| 도형 그림 | LLM 의 마크업/CSS 결과 | **코드가 deterministic 하게 rect/text** |
| Figma 호환 | 플러그인 (html.to.design 등) 필요 | **네이티브 paste/drag-drop** (플러그인 X) |
| 폐쇄망 호환 | △ (일부 플러그인 외부 fetch) | ✓ |
| 환각 위험 | 큼 | **거의 없음** (layout 인식만) |

```powershell
python main.py screen-converter `
  --captures-dir <캡처 폴더> `
  --style-css <CSS> `
  --export-svg
# 출력: output\screen-converter\<일자>\svg\<화면>.svg
```

**Figma 가져오기 (가장 단순):**
1. SVG 파일 → Figma canvas 에 **drag-drop**, 또는
2. SVG 파일 열고 `Ctrl+A` / `Ctrl+C` → Figma 에 `Ctrl+V`

→ 네이티브 SVG import 라 플러그인 불필요. 텍스트/색/도형 모두 Figma
안에서 편집 가능. `index.html` 에 모든 SVG 인덱스 + paste 안내
cheat-sheet 자동 포함.

---

**`--html-vision-only` 옵션** — `--frontend-dir` 와 같이 줬는데 HTML 결과가
캡처와 다른 화면으로 변환되면 false-positive 소스 매칭 의심. 이 flag 를
추가하면 HTML 만 source matching 비활성, **캡처 이미지만** VLM 에 넘김
(PPTX 와 같은 입력 — 캡처가 ground truth). PPTX 정상이고 HTML 만 엉뚱
하면 이 옵션 권장:

```powershell
python main.py screen-converter `
  --captures-dir <캡처 폴더> `
  --style-css <CSS> `
  --frontend-dir <React root> `
  --export-html `
  --html-vision-only
```

콘솔에 매칭된 React 소스 경로가 한 줄씩 표시되어 false-positive 즉시
확인 가능:
```
→ MaterialMaster.png  (source (src/pages/material/MaterialMaster/index.jsx, 5234 chars))
→ OrderList.png       (vision (소스 매칭 실패 또는 비활성))
```
캡처와 다른 컴포넌트로 매칭됐다 싶으면 `--source-mapping` 으로 수동 지정
또는 `--html-vision-only` 로 전체 비활성.

**소스 매칭 휴리스틱** (`--frontend-dir` 사용 시):
- 캡처 파일명 토큰 (예: `M_ORDER_LIST.png` → `m`/`order`/`list`) 과
  소스 파일 경로/basename 토큰의 교집합으로 점수화.
- 전체 stem 이 basename 의 substring → +10 / 토큰이 basename 에 있으면
  +3 / path 에만 있으면 +1 / `pages/screens/views/routes/` 하위면 +2.
- `_test.`, `.spec.`, `.stories.`, `.d.ts`, `node_modules/`, `dist/`,
  `build/`, `.next/`, `__tests__/` 는 인덱스에서 제외.
- 임계점수 미만이면 매칭 없음으로 처리 → 해당 화면은 이미지 only.
- 매칭 결과는 `llm_raw/<화면>.json` 의 `matched_source` 필드에 기록 —
  잘못 매칭됐는지 사전 확인 가능.

**LLM 호출 흐름** (시작 시 style 1회 + 캡처 1장당 layout 1회):
1. **Style profile 추출 (1회)** — `extract_style_profile(template_images,
   config)` 가 템플릿 캡처 N장을 한 번에 VLM 에 첨부, 색·폰트·버튼 모양
   JSON 추출. `output/screen-converter/<날짜>/style_profile.json` 저장.
   ```json
   {
     "primary_color": "#0067A6", "title_color": "#003366",
     "panel_bg": "#F2F8FF", "panel_border": "#B0CDE8",
     "table_header_bg": "#0067A6", "table_header_text": "#FFFFFF",
     "button_bg": "#FF6600", "button_text": "#FFFFFF",
     "button_shape": "rounded",
     "font_family": "나눔고딕"
   }
   ```
   모든 슬라이드가 이 값을 일관 적용 — 출력이 템플릿 비주얼과 맞도록.
   누락된 키는 `_DEFAULT_STYLE` (짙은 네이비 + 맑은 고딕) 로 fallback.
2. **Layout 추출 (캡처당 1회)** — `extract_layout(asis_image,
   template_images, config, source_path, frontend_dir)` 가 AS-IS + 템플릿
   + (있으면) **closure 번들 React 소스** 를 첨부, 다음 JSON 추출:
   - `--frontend-dir` 와 매칭된 entry 가 모두 있으면, `build_closure` 가
     entry 컴포넌트 + import 자식 컴포넌트들을 BFS 로 묶어 Markdown 으로
     직렬화 (`# Screen / ## File: ...` 헤더 + `\`\`\`jsx` fences) → 그대로
     프롬프트에 첨부. 콘솔에 `소스 첨부 (closure): N 파일, ~T tokens` 표시.
   - 매칭 자체가 없거나 tree-sitter 미설치면 단일 파일 8000자 fallback.
   - `llm_raw/<화면>.json` 의 `source_attachment` 필드에 `mode`/`file_count`/
     `total_tokens`/`files[]` (depth, mode) 가 기록됨.
   ```json
   {
     "page_title": "주문 조회",
     "search_fields": [{"label": "주문번호", "type": "text"}, ...],
     "table_columns": ["주문번호", "주문일자", ...],
     "buttons": ["조회", "초기화", "엑셀"],
     "notes": "...",
     "regions": { /* 각 섹션의 normalized bbox */ }
   }
   ```
3. **렌더링** — `render_pptx` 가 슬라이드당 제목 → 검색 패널 → 표 →
   버튼 → 노트 순으로 도형 배치. 위치는 `regions`, 색·폰트·버튼 모양은
   style profile, 텍스트는 layout content (또는 React 소스).

**디버깅 산출물** (렌더 결과가 기대와 다를 때 가장 먼저 확인):
- `output/screen-converter/<YYYYMMDD>/style_profile.json` — 템플릿에서
  추출한 색·폰트·버튼 모양 (`extracted` = VLM 출력 그대로, `resolved` =
  기본값 병합 후 실제 적용 값). 출력이 템플릿과 색이 안 맞으면 여기서
  확인.
- `output/screen-converter/<YYYYMMDD>/llm_raw/<화면명>.json` — 매 호출의
  파싱된 VLM layout dict (`asis_image` / `template_images` /
  `matched_source` / `source_match_candidates` / `layout` 포함).
  모델이 search 조건/컬럼/버튼을 실제로 뭐라고 추출했는지 + React 소스
  어느 파일이 매칭됐는지 확인.
- `output/legacy_analysis/<YYYYMMDD>/pattern_llm_raw_screen_<화면명>.txt`
  — JSON 파싱 실패 시에만 떨어지는 raw text (vision 미지원 모델/프롬프트
  누락 진단용).

**PoC 범위 제한 (의도적, 동작 확인 후 후속)**:
- 캐시 없음 (매번 VLM 호출 — 화면당 1회 + 시작 시 style 1회)
- 템플릿 픽셀을 슬라이드 배경으로 깔지는 않음 (도형으로 재구성).
  픽셀 단위 동일성이 필요하면 옵션 B (raster 배경 + 텍스트 오버레이)
  검토 필요 — 편집/접근성 떨어짐
- `analyze-legacy --extract-screen-layout` 의 ScreenLayout JSON 직접 입력
  모드 미지원 (지금은 항상 이미지 → VLM 경로)
- `convert-menu` 산출물 `input/menu.md` 메뉴 계층 매칭 미연동

---

### 15. 화면 UI 정의서 추출 (screen-spec) — AST 패턴, deterministic, PPTX 복붙용

`screen-converter` 가 캡처 + 템플릿으로 TO-BE 시안 PPTX 를 생성한다면,
`screen-spec` 은 **React 소스에서 화면 UI 정의서를 100% 결정론적으로
추출** 합니다 — LLM 호출 0, 같은 소스 → 같은 산출물 (byte-level
identical). 실제 설계서 PPTX 에 **시트 단위로 복사·붙여넣기** 하는
워크플로우 전제.

**전제**:
- `tree-sitter` + `tree-sitter-javascript` + `tree-sitter-typescript` 설치
- `--frontend-dir` (React/Vue/TS 소스 루트)
- `--captures-dir` (캡처 파일명을 화면 ID 로 사용)

**실행**:

```powershell
python main.py screen-spec `
  --captures-dir input\asis_captures `
  --frontend-dir C:\workspace\frontend `
  [--patterns output\legacy_analysis\patterns.yaml] `
  [--source-mapping input\screen_source_mapping.yaml]

# 출력: output\screen-spec\<YYYYMMDD>\screen_spec_<HHMMSS>.xlsx
```

**파이프라인**:
1. 캡처 파일명 → React entry 컴포넌트 매칭 (`screen-converter` 와 동일
   휴리스틱; cross-language 는 `--source-mapping` YAML 으로 수기)
2. `legacy_react_closure.build_closure` 로 entry + import 자식 컴포넌트
   까지 한 덩어리로 묶음 (`<SearchPanel/>` `<OrderGrid/>` `<Buttons/>`
   가 분리돼 있어도 통합)
3. 5종 추출기 (LLM 0):
   - **검색 필드**: `<input>` / `<Select>` / `<DatePicker>` 등 props
     (label, name, type, required, validation 인라인)
   - **그리드 컬럼**: `<Table columns={...}/>` 의 array → header /
     dataIndex(물리명) / type / width / hidden / sortable. const 변수
     references 자동 해석 (`columns={COLUMNS}` 도 OK)
   - **탭**: `<Tab>` `<TabPanel>` props
   - **이벤트 + 플로우**: `<Button onClick={fn}>` → fn 본체 traversal
     → API 호출 (`axios.post('/api/...')` 등) / 화면 호출 (`navigate`
     `window.open` `history.push` 등) 순서대로 step 리스트. inline arrow
     `() => fn(x)` 도 fn 본체 다시 해석.
   - **검증규칙**: 인라인 props (`required`/`pattern`/`min/max`) +
     yup/zod/joi schema chain (`.required('msg').matches(/.../, 'msg2')`)
4. 모든 화면 → 마스터 xlsx 1개 (openpyxl). 시트 = 영역, 1열 = 화면명.

**출력 xlsx 시트 구조**:

| 시트 | 컬럼 |
|------|------|
| **개요** | 화면명 / entry 파일 / closure 파일수 / closure tokens / truncated / 각 영역 row 수 |
| **검색조건** | 화면명 / 순번 / 라벨 / 필드명 / 타입 / 필수 / 기본값 / 검증규칙 / 소스파일 |
| **그리드컬럼** | 화면명 / 순번 / 헤더 / **물리명** / 데이터타입 / 너비 / **표시** / 정렬 / 소스파일 |
| **탭** | 화면명 / 순번 / 탭명 / 컨텐츠 컴포넌트 / 소스파일 |
| **이벤트** | 화면명 / 트리거(라벨) / 종류 / 핸들러 / API 호출 / 화면 호출 / 비고 / 소스파일 |
| **검증규칙** | 화면명 / 필드 / 규칙 / 상세 / 메시지 / 출처 (jsx_prop / yup / zod / joi) / 소스파일 |
| **이벤트플로우** | 화면명 / 이벤트 / step# / 동작 (api/navigate) / 상세 / 조건 |

PPTX 설계서에 슬라이드별로 해당 시트를 그대로 복사 → 표 붙여넣기 가능.

**사내 컨벤션 흡수**: `patterns.yaml` 의 `react.screen_spec` 슬롯으로
default 패턴 확장:

```yaml
react:
  screen_spec:
    input_components: ["MyInput", "MyDate"]
    table_components: ["MyDataTable"]
    button_components: ["MyButton", "PrimaryButton"]
    tab_item_components: ["MyTab"]
```

`discover-patterns` 가 자동 발견하도록 슬롯이 이미 있어서 그대로 인식.

**범위 제한**:
- 동적으로 계산되는 컬럼 (`isAdmin ? COLS_A : COLS_B`) 은 한쪽만 추출
  될 수 있음 (보수적)
- 다른 파일에 정의된 `columns` const 는 closure 안에 있어야 해석 가능
- 메시지가 `t('orderNo.required')` 같은 i18n key 면 key 만 추출
  (resolve 안 함)

---

### 16. TO-BE 속성명 추천 (recommend-names)

AS-IS 스키마(`schema` 산출물 .md)의 컬럼을 **표준 단어사전 / 용어사전**
기준으로 분석해 TO-BE 표준 물리명·도메인·데이터유형을 추천한다. 두 사전은
Excel 로 받아 **SQLite 캐시**(`<vectordb>/standard_dict.sqlite`)로 1회
데이터화하고, 원본 Excel 의 mtime 이 바뀌면 자동 재빌드한다.

**사전 Excel 양식** (헤더 자동 인식 — 괄호 부가설명/공백 무시):

- 단어사전: `논리명 / 물리명 / 물리의미(영문풀네임) / 표준여부 /
  속성분류어 / 동의어 / 설명 / 만료일자 / 출처구분`
- 용어사전: `논리명 / 물리명 / 구성정보 / 물리의미 / 도메인명 /
  데이터유형 / 길이 / 소수점 / 표준여부 / 개인정보구분 / 암호화여부 /
  설명 / 만료일자 / 출처구분`
- 도메인사전(선택): `도메인그룹명 / 도메인명 / 데이터유형 / 길이 / 소수점 /
  개인정보구분 / 암호화여부 / 설명 / 만료일자 / 출처구분`. 동일 도메인명이
  그룹별로 여러 개 존재할 수 있어 중복을 보존하며, 데이터유형 추론 보정에 쓴다.

`만료일자` 지난 항목은 매칭 인덱스에서 제외된다. `표준여부` 는 `N/X/×/비표준`
등 명시적 부정 표기만 비표준으로 보고, 그 외(`Y/○/표준/공백`)는 표준으로
견고하게 처리한다.

헤더는 유니코드 NFC 정규화(NFD 조합형 한글 흡수)·숨은문자(zero-width/BOM)·
괄호 부가설명·공백·개행·앞 번호를 무시하고 자동 인식하며, 표지/설명 시트가
앞에 있어도 `논리명`+`물리명` 이 잡히는 데이터 시트를 전체 시트에서 자동
선택한다. 인식에 실패하면 시트별 헤더 진단을 출력하니 `--word-sheet` /
`--term-sheet` 로 시트를 지정하면 된다.

**추천 4계층** (위에서 실패하면 다음 단계):

1. **정확매칭(용어)** — 컬럼 코멘트(한글 논리명)를 용어사전 `논리명` 과
   1:1 해시 조회 → 표준 `물리명` + 도메인 + 데이터유형. 코멘트가 없으면
   AS-IS `물리명` 을 용어사전 물리명과 대조(이미표준).
2. **단어조합** — 단어사전으로 코멘트/물리명을 분해해 영문약어를 `_` 로
   조합. 단어 매칭 우선순위는 **표준Y[논리명 > 동의어 > 물리의미] >
   표준N[논리명 > 동의어 > 물리의미]** (자기 논리명이 남의 동의어보다,
   표준이 비표준보다 우선). 한글 코멘트는 최장일치로, **영문 코멘트(예:
   `Detail Description`)는 단어 분리 후 물리명/물리의미로** 매칭. 마지막
   **속성분류어**로 도메인·데이터유형 추론(도메인사전 있으면 보정).
3. **RAG** — 위에서 미해결인 free-text 코멘트만 용어사전 임베딩에서
   유사 표준용어 top-k 후보 검색 (임베딩은 `build-dict` 시 생성됨).
4. **LLM** — 미매칭/미분해 코멘트를 LLM 이 **표준단어(논리명) 리스트로
   분해·변환**(RAG 후보 참고) → 그 단어들을 **사전에서 다시 조회**해 물리명·
   도메인·데이터유형을 가져온다. **물리명은 항상 사전 권위**(LLM 자유생성
   아님). 사전에 못 맞춘 단어만 `«»` 로 남거나, 최후수단으로 LLM 생성
   물리명(저신뢰 `LLM(생성)`)을 쓴다.

**절차는 2단계로 분리** — ① `build-dict` 로 사전을 SQLite 에 1회 적재 →
② `recommend-names` 로 사전 인자 없이 분석.

```powershell
# ── 1단계: 적재 (최초 1회, 또는 사전 갱신 시) ──
# 기존 적재 내용을 전부 삭제하고 다시 적재한다.
python main.py build-dict ^
  --word-dict ./input/단어사전.xlsx ^
  --term-dict ./input/용어사전.xlsx ^
  --domain-dict ./input/도메인사전.xlsx
# 적재 시 용어사전 임베딩(RAG용)도 함께 생성된다(기본).
# 임베딩 엔드포인트가 없거나 결정적 매칭만 쓰면:
python main.py build-dict --word-dict ... --term-dict ... --no-embed

# ── 2단계: 분석 (이후 반복, 사전 인자 불필요) ──
python main.py recommend-names --schema-md ./output/schema/.../ASIS_schema.md

# 결정적 사전매칭만 (LLM/RAG 미사용, 폐쇄망 빠른 검토)
python main.py recommend-names --schema-md ... --no-rag --no-llm
```

> `recommend-names` 에 `--word-dict`/`--term-dict` 를 직접 넘기면 적재가
>필요할 때(캐시 없음/Excel 변경) 자동 적재하는 단축 실행도 지원하지만,
> 권장 흐름은 위처럼 `build-dict` 로 적재 단계를 명시적으로 분리하는 것이다.

`build-dict` 옵션: `--word-dict` / `--term-dict` / `--domain-dict` /
`--word-sheet` / `--term-sheet` / `--domain-sheet` / `--dict-db` /
`--no-embed`(임베딩 생략). 임베딩은 **적재(build-dict) 시점에** 생성되며,
`recommend-names` 는 분석만 하고 임베딩하지 않는다(있으면 RAG 후보검색에
활용, 없으면 RAG 자동 비활성).

**임베딩 전략** (사전성 데이터 — 일반 문서 RAG 와 다름):

- **1 용어 = 1 벡터** (원자적 레코드). 사전은 짧은 키-값 레코드라 청킹하지
  않는다. 문서 텍스트는 **논리명 우선 + 설명**(영문명은 한글 쿼리를
  희석하므로 메타데이터로만 보관).
- **cosine** 거리로 컬렉션 생성, 재적재 시 컬렉션을 비우고 재생성(stale 방지).
- 메타데이터에 논리명/물리명/도메인/데이터유형/영문명 보관 → LLM 후보로
  바로 제시.
- 쿼리(코멘트)도 노이즈를 정리해 문서와 대칭으로 임베딩.
- 모델별 prefix: `config.yaml` 의 `embedding.doc_prefix` /
  `embedding.query_prefix` (e5/bge 계열은 각각 `passage: ` / `query: `
  필요, nomic 등은 빈값).

**산출물** (`output/recommend_names/<날짜>/`):

- `tobe_recommend_*.md` — 요약 + 추천 결과 표 + 미매칭 단어 목록
- `tobe_recommend_*.xlsx` — `추천결과`(저신뢰·미매칭 색상 강조) /
  `미매칭단어` / `요약` 3시트

추천결과 컬럼: `테이블 / AS-IS 컬럼 / 코멘트 / 기준 / 분해 단어 /
TO-BE 물리명 / 도메인 / 데이터유형 / 신뢰도 / 단계 / 비고`.

| 옵션 | 설명 |
| --- | --- |
| `--word-dict` / `--term-dict` | 단어사전 / 용어사전 Excel (최초 1회 필수) |
| `--word-sheet` / `--term-sheet` | 데이터 시트명 (미지정 시 `논리명`+`물리명` 헤더가 잡히는 시트 자동 선택 — 표지/설명 시트가 앞에 있어도 OK) |
| `--dict-db` | SQLite 캐시 경로 (기본 `<vectordb>/standard_dict.sqlite`) |
| `--rebuild-dict` | mtime 무관 캐시 강제 재빌드 |
| `--no-rag` | Tier3 RAG 비활성 |
| `--no-llm` | Tier4 LLM 비활성 (결정적 사전매칭만) |
| `--top-k` | RAG 후보 개수 (기본 5) |
| `--probe` | 사전 참조 진단. `;` 로 구분한 단어가 사전에서 어떻게 매칭되는지 출력 (스키마 분석 건너뜀). 예: `--probe "시설;USL VAL"` |

> **참조 진단 (`--probe`)**: 추천 결과가 이상할 때 특정 단어가 실제로
> 사전에서 매칭되는지 1줄로 확인한다. `등재=단어사전 / 물리명=FACI →
> 추천=FACI` 처럼 나오면 정상 참조, `등재=사전에 없음` 이면 해당 단어가
> 적재 안 된 것(헤더/시트/표기 확인). 코멘트가 **영문**(예: `USL VAL`)이면
> 한글 형태소 분해를 건너뛰고 물리명 기준으로 매칭한다.

---

### 17. AS-IS 화면 Figma 변환 (capture-screens)

AS-IS 프론트엔드 (React 등) 화면을 **Playwright headless 브라우저로 실제
렌더링**한 뒤 DOM 레이아웃을 JSON 으로 추출하고, 사내 **Figma 플러그인**
(`figma_plugin/`) 이 그 JSON 을 읽어 `createFrame / createText /
createRectangle` 로 **편집 가능한 디자인 레이어**를 재구성한다.

- Vision LLM 추측 없음 — 렌더링된 실제 화면 기준 (deterministic).
- html.to.design 같은 외부 SaaS 로 HTML 전송하지 않음 (폐쇄망/보안).
- JSON 스키마 계약: [`docs/FIGMA_JSON_SPEC.md`](docs/FIGMA_JSON_SPEC.md)
  (`schemaVersion: 1` — Python ↔ 플러그인의 유일한 계약 문서).

**전제조건**: AS-IS 프론트가 **실행 가능한 상태** (dev 서버 또는 운영
URL 접근). 정적 소스 분석이 아니라 렌더링된 화면을 캡처한다.

```powershell
# 1) 라우트 자동 추출 (React Router 스캔) — dry-run 으로 목록 먼저 확인
python main.py capture-screens `
  --frontend-dir C:\work\frontend `
  --patterns output\legacy_analysis\patterns.yaml `
  --list-only

# 2) 캡처 실행
python main.py capture-screens `
  --base-url http://localhost:3000 `
  --frontend-dir C:\work\frontend `
  --patterns output\legacy_analysis\patterns.yaml `
  --storage-state auth.json `
  --viewport 1920x1080
# 출력: output\figma_capture\<YYYYMMDD>\<라우트슬러그>.json (+ _failed.md)

# 3) 라우트 수동 목록 / 단일 URL 모드
python main.py capture-screens --base-url http://localhost:3000 --routes-file routes.txt
python main.py capture-screens --base-url http://localhost:3000 --url /order/list
```

| 옵션 | 설명 |
| --- | --- |
| `--base-url` | AS-IS 프론트 베이스 URL. 미지정 시 `.env` 의 `FIGMA_CAPTURE_BASE_URL` |
| `--routes-file` / `--frontend-dir` / `--url` | 라우트 소스 (우선순위 순). `--frontend-dir` 는 React Router 자동 추출 |
| `--patterns` | patterns.yaml 의 `url.url_prefix_strip` / `url.react_route_prefix` 를 라우트 변환에 적용 (analyze-legacy 와 동일) |
| `--storage-state` | Playwright storage_state JSON — 로그인 세션 주입 (아래 참고) |
| `--wait-selector` / `--wait-ms` | 렌더 완료 대기. 미지정 시 networkidle |
| `--viewport` | 기본 1920x1080 |
| `--max-image-kb` | 이미지 base64 최대 크기 (기본 500). 초과/CORS 시 회색 placeholder |
| `--param-fill key=value` | 동적 세그먼트 (`:id`/`{id}`) 치환. 미치환 동적 라우트는 skip + `_failed.md` 기록 |
| `--list-only` | dry-run — 캡처 대상 라우트 목록만 출력 |

**폐쇄망 Playwright 오프라인 설치** (Windows):

```powershell
# 인터넷 PC — wheel + 브라우저 번들 다운로드
python -m pip download playwright -d .\wheels `
  --platform win_amd64 --python-version 311 --only-binary=:all:
python -m pip install playwright
python -m playwright install chromium
# → %USERPROFILE%\AppData\Local\ms-playwright 폴더 통째로 USB 복사

# 폐쇄망 PC — wheel 설치 + 번들 경로 지정
python -m pip install --no-index --find-links=.\wheels playwright
set PLAYWRIGHT_BROWSERS_PATH=C:\tools\ms-playwright
```

**storage_state (로그인 세션) 만드는 법** — 인터넷/사내망 PC 에서 1회:

```powershell
python -c "from playwright.sync_api import sync_playwright; p = sync_playwright().start(); b = p.chromium.launch(headless=False); ctx = b.new_context(); pg = ctx.new_page(); pg.goto('http://your-asis-url/login'); input('브라우저에서 로그인 완료 후 Enter...'); ctx.storage_state(path='auth.json'); b.close()"
# → 생성된 auth.json 을 --storage-state 로 전달
```

**Figma 플러그인 로드** (Figma 데스크톱):

1. `Plugins` → `Development` → `Import plugin from manifest...`
2. repo 의 `figma_plugin/manifest.json` 선택
3. 플러그인 실행 → JSON 파일 선택 (여러 개 가능) 또는 textarea 붙여넣기 → `Import`
4. 화면당 프레임 1개씩 생성 — 텍스트는 편집 가능한 텍스트 레이어,
   한글 폰트는 `FONT_MAP` (맑은 고딕 → Noto Sans KR) 매핑, 미존재 시
   Inter 폴백 + 카운트 표시

**Figma 웹만 가능한 경우 — `--export-svg`** (데스크톱 앱 / 플러그인 불필요):

```powershell
python main.py capture-screens --url /order/list --export-svg
# 출력: output\figma_capture\<일자>\order_list.json + order_list.svg
```

→ SVG 파일을 **Figma 캔버스에 drag-drop** 또는 **SVG 열고 Ctrl+A/Ctrl+C → Figma 에 Ctrl+V**.
실제 렌더된 DOM 을 rect/text/image SVG 요소로 1:1 변환 (VLM 추측 0 —
deterministic). 텍스트는 편집 가능한 텍스트 레이어, rect 는 색/border
편집 가능. 한글 폰트는 SVG `font-family` 속성에 그대로 전달되어 Figma
가 알아서 fallback.

**텍스트가 다른 객체 대비 작아 보이면** `--svg-text-scale 1.5` (또는 2.0)
추가: Figma 의 SVG import 가 `<text>` 의 `font-size` 만 viewBox 비율과
다르게 해석하는 케이스 보정 (rect/image 좌표는 무영향).

```powershell
# 캡처 + SVG (text-scale 보정)
python main.py capture-screens --url /order/list --export-svg --svg-text-scale 1.5

# 캡처 이미 했고 SVG 만 다른 scale 로 빠르게 재시도
python main.py capture-screens --rebuild-svg-only output\figma_capture\<일자> --svg-text-scale 2.0
```

**검증**: `python verify_capture.py` — mock 2장 캡처 후 노드 수 assert.

> 1차 비범위: Auto Layout 자동 추론 / Figma Component 생성 / Polymer
> 라우트 자동 / 스크롤 전체 캡처 (`--full-page`) / API mocking.

---

### 6. 벡터 DB 임베딩 + RAG ERD

```powershell
# 임베딩
python main.py embed --schema-md ./output/스키마.md --query-md ./output/query.md

# RAG 기반 ERD
python main.py erd-rag
python main.py erd-rag --tables "ORDERS,CUSTOMERS"
```

## 추천 워크플로우

```powershell
# 1. 스키마 추출
python main.py schema

# 2. 쿼리 분석 (스키마 필터링 권장)
python main.py query C:\work\mapper --schema-md .\output\스키마.md

# 3. 스키마 보강 (LLM 코멘트 추천, 추천된 코멘트에 'LLM추천' 표기)
python main.py enrich-schema --schema-md .\output\스키마.md

# 4. 주제영역별 ERD 생성
python main.py erd-group --schema-md .\output\스키마_enriched.md --query-md .\output\query.md

# 5. 표준화 리포트
python main.py standardize --schema-md .\output\스키마_enriched.md --query-md .\output\query.md --validate-data

# === AS-IS 레거시 소스 통합 분석 (차세대 전환 대비) ===

# 6. (선택) 메뉴 Excel 양식이 템플릿과 다르면 먼저 표준 menu.md 로 변환
python main.py convert-menu `
  --menu-xlsx C:\work\menu_원본.xlsx `
  --output input\menu.md

# 7. 프로젝트 패턴 발견 (LLM 사용, 프로젝트당 1회)
#    menu.md + frontends-root 를 같이 주면 URL 관례(prefix strip, app_key)도
#    같은 patterns.yaml 에 학습됩니다.
python main.py discover-patterns `
  --backend-dir C:\work\legacy\backend `
  --menu-md input\menu.md `
  --frontends-root C:\workspace\frontend

# 8. AS-IS 소스 분석 (패턴 + 메뉴 기반)
python main.py analyze-legacy `
  --backends-root C:\workspace\backend `
  --frontends-root C:\workspace\frontend `
  --menu-md input\menu.md `
  --patterns output\legacy_analysis\patterns.yaml `
  --menu-only

# 9. (선택) 화면 캡처로 TO-BE PPTX 자동 생성 (Vision LLM)
#    --frontend-dir 지정 시 React 소스에서 라벨/컬럼/버튼을 정확히 추출
python main.py screen-converter `
  --captures-dir input\asis_captures `
  --templates-dir input\template_captures `
  --frontend-dir C:\workspace\frontend
```

## ERD 렌더링

- **VS Code**: Markdown Preview Mermaid Support 확장 설치 → `Ctrl+Shift+V`
- **mermaid-cli**: `mmdc -i erd.md -o erd.png`

## 라이선스

MIT
