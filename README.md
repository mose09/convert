# Oracle Schema & Query Analyzer

Oracle DB 스키마 + MyBatis 쿼리를 분석하여 Markdown 추출, ERD 자동 생성, 표준화 리포트까지 지원하는 도구입니다.

FK/description이 없는 레거시 DB 환경에서 **쿼리 JOIN 분석 + 로컬 LLM**으로 테이블 관계를 추론합니다.

## 기능 요약

| Command | 설명 | Oracle 접속 | LLM |
|---------|------|:-----------:|:---:|
| `schema` | 테이블/컬럼 스키마 .md 추출 | O | X |
| `query` | MyBatis XML 쿼리 분석 .md | X | X |
| `enrich-schema` | 빈 코멘트를 LLM이 추천하여 보강 | X | O |
| `erd-md` | .md 파일에서 Mermaid ERD 생성 | X | X |
| `erd-group` | 관계 기반 주제영역별 ERD 분할 생성 | X | X |
| `terms` | 용어사전 자동 생성 (스키마 + React) | X | O |
| `gen-ddl` | 자연어 → 표준 DDL 생성 (+ 검증) | 선택 | O |
| `audit-standards` | 전체 스키마 표준 위반 일괄 검사 | X | X |
| `validate-naming` | 테이블/컬럼명 네이밍 표준 검증 | X | X |
| `review-sql` | SQL 쿼리 정적 분석 + LLM 리뷰 | X | 선택 |
| `standardize` | 표준화 분석 리포트 생성 | 선택 | O |
| `analyze-legacy` | AS-IS 레거시 소스 통합 분석 (Spring/Vert.x + MyBatis + React + Menu) | 선택 | X |
| `embed` | .md를 벡터 DB에 임베딩 | X | X |
| `erd-rag` | RAG로 Mermaid ERD 생성 | X | O |
| `erd` | 직접 DB 접속 ERD 생성 | O | 선택 |

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
    ├── legacy_react_router.py    # React Router v5/v6 + lazy import 스캐너
    ├── legacy_menu_loader.py     # DB 메뉴 테이블 로드 + 계층 평탄화
    ├── legacy_analyzer.py        # AS-IS 통합 오케스트레이터 (양방향 URL 매칭)
    ├── legacy_report.py          # AS-IS 리포트 Markdown + Excel (7시트)
    └── legacy_util.py            # URL 정규화 공유 헬퍼
```

## 설치

```bash
python -m pip install -r requirements.txt
```

## 설정

```bash
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

```bash
python main.py schema
python main.py schema --owner HR
python main.py schema --table CUSTOMERS
```

### 2. 쿼리 분석 (Oracle 접속 불필요)

MyBatis, iBatis XML 모두 지원합니다.

```bash
# 기본 실행
python main.py query /path/to/mybatis/mapper

# 스키마 .md 기반 필터링 (스키마에 없는 테이블 제외, 권장)
python main.py query /path/to/mybatis/mapper --schema-md ./output/스키마.md
```

### 3. 스키마 코멘트 보강 (LLM)

빈 테이블/컬럼 코멘트를 LLM이 약어를 해석하여 자동 추천합니다.
확신할 수 없는 약어는 비워둡니다.

```bash
python main.py enrich-schema --schema-md ./output/스키마.md
```

출력: `output/스키마_enriched_TIMESTAMP.md` (보강된 스키마)

### 4. ERD 생성

```bash
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

```bash
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

```bash
# 기본 사용 (용어사전 없이)
python main.py gen-ddl --request "고객 주문 이력 테이블"

# 용어사전 + 스키마 참조 (권장)
python main.py gen-ddl \
  --request "고객 주문 이력 테이블 만들어줘. 고객ID, 주문일자, 금액 포함" \
  --terms-md ./output/terms_dictionary.md \
  --schema-md ./output/스키마.md

# 생성 + 검증 + Oracle 실행 (컨펌 후)
python main.py gen-ddl \
  --request "배송 정보 테이블" \
  --terms-md ./output/terms_dictionary.md \
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

```bash
# 기본 약어 사전으로 검사
python main.py audit-standards --schema-md ./output/스키마.md

# 용어사전 기반 검사 (권장)
python main.py audit-standards \
  --schema-md ./output/스키마.md \
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

```bash
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

```bash
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

```bash
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

Java/Spring + MyBatis + React + DB 메뉴 테이블을 **통합 분석**하여
차세대 전환 전 **프로그램 단위 메타데이터**를 일괄 추출합니다.
한 행 = 한 Controller 엔드포인트이며 메뉴 계층 → Controller → Service
→ Mapper XML → 관련 테이블 → RFC 호출까지 한 번에 매핑됩니다.

경로는 **백엔드/프론트엔드 루트 한 개씩만** 지정하면 됩니다. 백엔드 루트
하위를 재귀 탐색해서 `.java` 와 MyBatis `*.xml` 을 모두 찾아내며,
`target` / `build` / `.git` / `.gradle` / `.idea` / `bin` / `out` /
`node_modules` 등 빌드/VCS 폴더는 자동으로 제외됩니다.

```bash
# 전체 분석 (메뉴 테이블 접속 포함)
python main.py analyze-legacy \
  --backend-dir /path/to/legacy/backend \
  --frontend-dir /path/to/legacy/frontend

# 메뉴 테이블 없이 (내부 테스트용)
python main.py analyze-legacy \
  --backend-dir /path/to/legacy/backend \
  --skip-menu

# 메뉴 테이블 이름 오버라이드 + RFC 탐색 깊이 조절 + 포맷 지정
python main.py analyze-legacy \
  --backend-dir /path/to/legacy/backend \
  --frontend-dir /path/to/legacy/frontend \
  --menu-table SYS_MENU --rfc-depth 3 --format excel
```

**백엔드 프레임워크 자동 감지 + 분기**

분석을 시작하면 백엔드 루트의 `pom.xml` / `build.gradle` / `build.gradle.kts`
의존성을 스캔해 **Spring** 인지 **Vert.x** 인지 자동으로 판별합니다.
빌드 파일이 없는 경우 최대 200개의 `.java` 파일을 샘플링해 어노테이션/
상속 흔적(`@Controller` / `@RestController` vs `AbstractVerticle` /
`io.vertx`) 을 비교하는 휴리스틱으로 fallback 합니다.

| 감지 결과 | Controller 로 인정되는 클래스 |
|-----------|-------------------------------|
| `spring`  | `@Controller` / `@RestController` 어노테이션만 |
| `vertx`   | `extends AbstractVerticle` 만 |
| `mixed`   | 둘 다 (Spring 과 Vert.x 소스가 한 레포에 공존) |
| `unknown` | 둘 다 (fallback — 어느 것도 명확히 감지되지 않음) |

감지 결과는 CLI 로그, Markdown 리포트 헤더, Excel `Summary` 시트의
`Backend framework` 행에 표시됩니다. Spring 프로젝트에 우연히 `extends
AbstractVerticle` 클래스가 섞여 있어도 해당 클래스는 controller 로 취급
되지 않고 서비스/유틸로만 취급됩니다(반대도 동일). 감지 결과가 의도와
다르면 프로젝트 루트에 적절한 `pom.xml` / `build.gradle` 을 두면 됩니다.

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
- **SAP JCo RFC**:
  - 표준: `destination.getFunction("Z_...")`
  - 프로젝트 유틸: `JCoUtil.getCoFunction("Z_...")` 등 `.getFunction` /
    `.getCoFunction` 메서드 호출 모두 인식
  - `String FN_XXX = "..."` 상수를 거친 **2-pass 해석**
  - 서비스 → 서비스 체인의 **트랜지티브 수집** (`--rfc-depth`, 기본 2)
- **React Router** v5 `component={X}` / v6 `element={<X/>}` / 객체 라우트 /
  `React.lazy(() => import("./X"))` / 중첩 Route 의 부모 path 누적 결합
- **인코딩**: UTF-8 / EUC-KR / CP949 / Latin-1 자동 fallback

**산출물:**

```
output/
├── as_is_analysis_TIMESTAMP.md      # Markdown 리포트
└── as_is_analysis_TIMESTAMP.xlsx    # Excel (7개 시트)
    ├── Sheet: Summary                 (전체 집계)
    ├── Sheet: Programs                (메인 — 14개 컬럼)
    ├── Sheet: Menu Hierarchy          (메뉴 계층 + 매칭 여부)
    ├── Sheet: Unmatched Controllers   (메뉴 없는 컨트롤러)
    ├── Sheet: Orphan Menu Entries     (컨트롤러 없는 메뉴)
    ├── Sheet: RFC Calls               (SAP 인터페이스 cross-reference)
    └── Sheet: Tables Cross-Reference  (테이블별 사용 프로그램)
```

**Programs 시트 컬럼 (14개):**

`No, Main, Sub, Tab, Program, HTTP, URL, File, React, Controller, Service,
Query XML, Tables, RFC`

매칭되지 않은 행(unmatched controller)은 **노란색**, 매퍼 체인이 없는
행은 **회색**으로 하이라이트됩니다.

### 6. 벡터 DB 임베딩 + RAG ERD

```bash
# 임베딩
python main.py embed --schema-md ./output/스키마.md --query-md ./output/query.md

# RAG 기반 ERD
python main.py erd-rag
python main.py erd-rag --tables "ORDERS,CUSTOMERS"
```

## 추천 워크플로우

```bash
# 1. 스키마 추출
python main.py schema

# 2. 쿼리 분석 (스키마 필터링 권장)
python main.py query /path/to/mapper --schema-md ./output/스키마.md

# 3. 스키마 보강 (LLM 코멘트 추천, 추천된 코멘트에 'LLM추천' 표기)
python main.py enrich-schema --schema-md ./output/스키마.md

# 4. 주제영역별 ERD 생성
python main.py erd-group --schema-md ./output/스키마_enriched.md --query-md ./output/query.md

# 5. 표준화 리포트
python main.py standardize --schema-md ./output/스키마_enriched.md --query-md ./output/query.md --validate-data

# 6. 차세대 전환 대비 - AS-IS 레거시 소스 통합 분석 (선택)
python main.py analyze-legacy \
  --backend-dir /path/to/legacy/backend \
  --frontend-dir /path/to/legacy/frontend
```

## ERD 렌더링

- **VS Code**: Markdown Preview Mermaid Support 확장 설치 → `Ctrl+Shift+V`
- **mermaid-cli**: `mmdc -i erd.md -o erd.png`

## 라이선스

MIT
