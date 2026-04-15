# 레거시 Oracle DB + 레거시 Java 소스를 위한 현행 분석 & ERD 자동화 도구

## 배경

차세대 시스템 전환을 앞두고 현행 시스템의 **데이터 구조** 와 **애플리케이션 구조** 를 모두 파악해야 합니다. Oracle 11g 레거시 환경에서 흔히 겪는 문제들:

- Foreign Key constraint 가 거의 없음 → 테이블 간 관계 파악 불가
- 테이블/컬럼 Description(코멘트) 이 비어있음
- ERD 문서가 없거나 현행화되지 않음
- Oracle 구식 `(+)` outer join 문법으로 된 SQL 이 대부분
- **API 목록 / 각 API 가 쓰는 테이블·RFC 목록 문서** 가 없어서 영향 범위 산정 불가
- MyBatis Mapper interface 없이 `CommonSQL.selectList("namespace.id", ...)` 같은 **문자열 기반 SQL 호출** 이 많은 레거시 코드 관습
- Spring 뿐 아니라 **Vert.x** 같은 비-Spring 백엔드, 프로젝트 로컬 `@RestVerticle` 같은 **커스텀 어노테이션** 사용
- 모노레포 안 여러 backend project 를 따로 따로 분석하기 번거로움

이 도구는 **Oracle 스키마 + MyBatis/iBatis 쿼리 XML + Java 백엔드 소스 + React 프론트 소스 + DB 메뉴 테이블** 을 통합 분석하여, FK 가 없어도 **JOIN 패턴에서 테이블 관계를 자동 추론** 하고, **Controller 의 각 메서드가 실제로 호출하는 서비스 메서드 → SQL statement → 테이블 → RFC 체인을 정밀 추적** 하며, 로컬 LLM 을 활용해 **코멘트 보강, ERD 생성, 용어사전 생성, 표준화 리포트** 까지 한번에 처리합니다. 폐쇄망 환경에서 완전 로컬 동작합니다.

---

## 주요 기능

### 1. 스키마 메타데이터 추출

Oracle 데이터 딕셔너리에서 테이블, 컬럼, PK, FK, 인덱스 정보를 Markdown으로 추출합니다.

```bash
python main.py schema
```

**추출 정보:**
- 테이블 목록 및 코멘트
- 컬럼 정보 (이름, 데이터타입, Nullable, 기본값, 코멘트)
- Primary Key, Foreign Key, 인덱스

### 2. MyBatis/iBatis 쿼리 분석

XML mapper 파일을 파싱하여 SQL에서 JOIN 관계를 자동 추출합니다. **Oracle 접속 없이** XML 파일만으로 동작합니다.

```bash
python main.py query /path/to/mapper --schema-md ./output/스키마.md
```

**분석 내용:**
- SQL 쿼리 목록 (SELECT/INSERT/UPDATE/DELETE)
- JOIN 기반 테이블 관계 추론 (FK 없이도 관계 파악)
- 테이블 사용 통계
- 서브쿼리/CTE/alias 자동 필터링
- MyBatis, iBatis 모두 지원

**지원하는 JOIN 문법:**
- ANSI `JOIN ... ON a.x = b.x AND a.y = b.y` (composite JOIN 전부 수집)
- ANSI `LEFT/RIGHT/FULL/INNER/CROSS JOIN`
- **Oracle 구식 outer join** `WHERE a.col = b.col(+)` (comma-style FROM + `(+)` 마커 자동 해석)
- **Oracle 구식 equi-join** `FROM a, b, c WHERE a.x = b.x AND a.y = c.y` (comma-list FROM 모든 테이블의 alias 를 인식)
- MyBatis dynamic SQL (`<if>` / `<choose>` / `<when>` / `<trim>` / `<foreach>`) 내부 조건도 조인으로 수집
- CDATA 블록 (`<![CDATA[...]]>`)
- MyBatis 파라미터 (`#{...}` / `${...}`) 자동 정규화

**Composite JOIN 표시 개선** — 같은 테이블 쌍이 여러 컬럼으로 조인되면 Relationship Details 표에서 **한 row 에 여러 컬럼 쌍을** 모아서 표시합니다:

```
| Table A         | Columns A              | <-> | Table B          | Columns B              | JOIN Type                  | Sources |
|-----------------|------------------------|-----|------------------|------------------------|----------------------------|---------|
| FAB_SVID_MODELING | SVID, EQ_MST_ID, EQ_ID | <-> | FAB_CBM_MODELING | SVID, EQ_MST_ID, EQ_ID | LEFT OUTER JOIN (Oracle +) | xxx.xml#getList |
```

**`Sources` 컬럼** 은 이 조인이 실제로 등장하는 **모든 statement 를 `;` 로 나열** 합니다. 같은 관계가 여러 쿼리에서 재사용되는 경우 한눈에 확인 가능하고, "엉뚱한 statement 가 source 로 찍히는" 혼선이 생기지 않습니다.

`--schema-md` 옵션으로 스키마에 실제 존재하는 테이블만 필터링할 수 있습니다.

### 3. LLM 스키마 코멘트 보강

빈 테이블/컬럼 코멘트를 로컬 LLM이 약어를 해석하여 자동 추천합니다.

```bash
python main.py enrich-schema --schema-md ./output/스키마.md
```

**특징:**
- `CUST_NO` → "고객번호", `ORD_DT` → "주문일자" 등 약어 해석
- 확신할 수 없는 약어는 비워둠 (억지 추측 방지)
- LLM이 추가한 코멘트에 `(LLM추천)` 표기로 구분
- 기존 코멘트는 유지, 빈 것만 보강

**출력 예시:**
```
| CUST_NO (PK) | NUMBER(10)    | N | | 고객번호 (LLM추천) |
| ORD_DT       | DATE          | Y | | 주문일자 (LLM추천) |
| EMAIL        | VARCHAR2(100) | Y | | 이메일 주소 |        ← 기존 코멘트 유지
```

### 4. ERD 자동 생성

추출한 스키마와 쿼리 분석 결과로 ERD를 자동 생성합니다. **DB 접속, LLM 모두 불필요**합니다.

#### 단일 ERD

```bash
python main.py erd-md --schema-md ./output/스키마.md --query-md ./output/query.md --related-only
```

#### 주제영역별 그룹 분할 ERD

JOIN 관계로 연결된 테이블 그룹을 자동으로 찾아 주제영역별 ERD를 분할 생성합니다.

```bash
python main.py erd-group --schema-md ./output/스키마.md --query-md ./output/query.md
```

**Composite JOIN 시각화**: Mermaid 는 한 테이블 쌍당 관계선 1개만 그릴 수 있으므로, **모든 컬럼 쌍을 label 에 합쳐서** 한번에 표시합니다:

```
TB_ORDER ||--o{ TB_ORDER_ITEM : "ORDER_ID=ORDER_ID, SITE_CD=SITE_CD, LANG_CD=LANG_CD"
```

인터랙티브 HTML ERD 도 같은 방식으로 `sourceCol` / `targetCol` 에 여러 컬럼을 `, ` 로 합쳐 보여주어 3-컬럼 composite PK join 이 한눈에 파악됩니다.

**출력:**
```
output/erd_groups_TIMESTAMP/
├── 00_summary.md                                # 전체 그룹 요약 + 테이블 분류
├── erd_group_01_ORDERS_CUSTOMERS.md              # Mermaid ERD
├── erd_group_01_ORDERS_CUSTOMERS.html            # 인터랙티브 ERD
├── erd_group_02_PRODUCTS_INVENTORY.md
├── erd_group_02_PRODUCTS_INVENTORY.html
└── ...
```

**테이블 분류 (summary):**

| 분류 | 설명 |
|------|------|
| JOIN 관계 테이블 | ERD 그룹에 포함된 테이블 |
| XML에 있지만 JOIN 없음 | 쿼리에서 단독 사용 |
| XML에 없는 테이블 | 스키마에만 있고 쿼리에서 미사용 |
| XML에만 있고 스키마에 없음 | 다른 스키마 소유 또는 뷰 |

**컬럼 구분:**

| 표시 | 의미 |
|------|------|
| PK | Primary Key (스키마 정의) |
| FK | 실제 Foreign Key constraint |
| REF | JOIN 관계 참조 (FK 아님) |

#### 인터랙티브 HTML ERD

Mermaid `.md` 파일과 함께 D3.js 기반 인터랙티브 `.html` ERD도 동시 생성됩니다.

**기능:**
- 드래그로 테이블 위치 자유 이동
- 마우스 휠 줌/팬
- 테이블 클릭 → 우측 패널에 컬럼 상세 (PK 노란색, FK 민트색, REF 보라색)
- 관계선 hover → JOIN 컬럼 정보 툴팁
- 클릭 시 관련 테이블 하이라이트 (비관련 dimmed)
- 상단 검색창에서 테이블명 검색 → 자동 이동
- Fit All / Reset View 버튼

브라우저에서 `.html` 파일을 열기만 하면 됩니다.

### 5. 표준화 분석 리포트

차세대 전환 시 용어/도메인 표준화를 위한 현행 시스템 분석 리포트를 자동 생성합니다.

```bash
# 구조 분석 + LLM 표준안 제안
python main.py standardize --schema-md ./output/스키마.md --query-md ./output/query.md

# 실데이터 검증 포함 (Oracle 접속)
python main.py standardize --schema-md ./output/스키마.md --query-md ./output/query.md --validate-data
```

**분석 항목:**

| 리포트 | 내용 |
|--------|------|
| JOIN 컬럼명 불일치 | 같은 관계인데 컬럼명이 다른 경우 (CUST_NO vs CUSTOMER_ID) |
| 타입 불일치 | 같은 컬럼명인데 타입이 다른 경우 (DATE vs VARCHAR2) |
| 네이밍 패턴 이탈 | 테이블 내 접두어 규칙에서 벗어난 컬럼 |
| PK 패턴 분석 | _ID, _NO, _CD 등 식별자 접미어 현황 |
| 코드 컬럼 분석 | _CD, _TYPE 컬럼의 실제 DISTINCT 값 조회 |
| Y/N 컬럼 체크 | Y/N 외 이상 데이터 존재 여부 |
| 컬럼 사용률 | NULL 100% 미사용 컬럼, 길이 과다 할당 컬럼 |
| LLM 표준안 | 분석 결과 기반 용어 통일, 타입 통일, 정비 방안 제안 |

**산출물:**
```
output/std_report_TIMESTAMP/
├── 00_summary.md
├── 01_join_column_mismatch.md
├── 02_type_inconsistency.md
├── 03_naming_pattern.md
├── 04_identifier_pattern.md
├── 05_code_columns.md
├── 06_yn_columns.md
├── 07_column_usage.md
└── 08_llm_proposals.md
```

### 6. 벡터 DB 임베딩 + RAG ERD

스키마/쿼리 정보를 로컬 임베딩 모델로 벡터화하여 ChromaDB에 저장하고, RAG 기반으로 LLM이 ERD를 생성합니다.

```bash
# 임베딩
python main.py embed --schema-md ./output/스키마.md --query-md ./output/query.md

# RAG ERD 생성
python main.py erd-rag --tables "ORDERS,CUSTOMERS"
```

대량 테이블(1,000개+)에서는 `erd-md`/`erd-group`이 더 정확하고 빠릅니다.

### 7. 용어사전 자동 생성 (약어/영문명/한글명/**정의**)

Oracle 스키마 컬럼명 + React 소스 변수명에서 단어를 수집하고, 로컬 LLM 이 **표준 약어 / 영문 Full Name / 한글명 / 한글 정의(업무 설명 1~2문장)** 를 자동 생성합니다.

```bash
# 스키마 + React 소스 양쪽에서 수집
python main.py terms --schema-md ./output/스키마.md --react-dir /path/to/react/src

# LLM 없이 단어 수집만
python main.py terms --schema-md ./output/스키마.md --skip-llm
```

**출력 Excel 5 시트**:
- **용어사전**: 전체 (Abbreviation / English Full / Korean / **Definition** / DB 빈도 / FE 빈도)
- **DB+FE공통**: 양쪽에서 쓰이는 단어 — 표준화 최우선 대상
- **DB전용 / FE전용**: 한쪽에서만 쓰는 단어
- **미식별**: LLM이 해석 못한 단어

비개발자 검토용 "한글 정의" 필드 덕분에 업무 담당자도 용어사전 검수가 가능합니다.

### 8. AS-IS 레거시 소스 코드 분석 (analyze-legacy)

**차세대 전환의 가장 큰 난제** — "현행 시스템에 어떤 API 가 있고, 각 API 가 어떤 테이블 / RFC / 서비스 메서드를 쓰는지" 를 소스에서 직접 추출해 **프로그램 단위 메타데이터 시트** 로 뽑습니다. Spring, Vert.x, MyBatis, React, SAP JCo, DB 메뉴 테이블을 **한 번에 통합 분석** 합니다.

```bash
# 단일 프로젝트
python main.py analyze-legacy \
  --backend-dir /path/to/backend \
  --frontend-dir /path/to/frontend

# 모노레포 배치 (여러 backend project 를 한 번에)
python main.py analyze-legacy \
  --backends-root /path/to/monorepo/backend \
  --frontend-dir /path/to/frontend
```

**자동 감지**:
- 백엔드 프레임워크 — `pom.xml` / `build.gradle` 의존성 또는 소스 휴리스틱으로 **Spring / Vert.x 자동 판별**
- Spring: `@RestController` / `@RequestMapping` / `@PostMapping(value="/x")` / `@Autowired` / Lombok `@RequiredArgsConstructor`
- **Vert.x** (표준이 아닌 레거시 관습까지):
  - `extends AbstractVerticle` / `BaseVerticle` / `ReactiveVerticle` 같은 **프로젝트 로컬 wrapper**
  - `@RestVerticle(url = "/api/x", method = HttpMethod.POST, isAuth = true)` 같은 **커스텀 어노테이션**
  - `router.get("/x").handler(this::foo)` DSL
  - `router.route().path("/x").method(HttpMethod.GET).handler(...)` 체인
- MyBatis XML 은 파일명 규칙 상관 없이 내용 기반으로 판별 (`*Mapper.xml`, `*_sql.xml` 등 모두 지원)

**추출 메타데이터 (한 row = 한 endpoint)**:
- Backend project / Framework (모노레포에서 어느 서비스인지)
- File / Controller / URL / HTTP / Program
- **Service → Service method** (메서드 단위 정밀 추적)
- **XML → XML method (SQL ID)** (해당 statement 만)
- **Table** (그 SQL 이 실제로 쓰는 테이블만)
- **RFC** (그 메서드 body 안에서만 호출하는 SAP JCo 함수)

**메서드 단위 call-graph 해상도** — 이 기능의 핵심

기존 도구들은 "Controller 하나에 연결된 Service 의 모든 SQL / RFC / 테이블" 을 union 해서 한 행에 모두 붙이는 방식입니다. 그래서 같은 서비스를 공유하는 10개의 API 가 모두 **똑같은 테이블/RFC 목록을** 가진 것처럼 보입니다. 이 도구는 다릅니다.

```
OrderController.list()    → orderService.findAll()    → order.findAll   → TB_ORDER, TB_CUSTOMER
OrderController.save()    → orderService.createOrder() → order.save     → TB_ORDER + ZPM_ORDER_CREATE
OrderController.delete()  → orderService.deleteById() → order.delete   → TB_ORDER_HISTORY + ZPM_ORDER_DELETE
```

같은 `OrderService` 를 쓰는 3개 API 가 각자 **정말로 호출하는 메서드 body 안의 SQL/RFC 만** 정확히 분리됩니다. 이것이 가능한 이유는 파서가 Java method body 단위로 `field.method()` 호출, `commonSQL.selectList("ns.id", ...)` SQL 호출, `JCoUtil.getJCoFunction("Z_NAME")` RFC 호출을 모두 body-scope 로 수집하고, analyzer 가 call-graph 를 따라 `body_sql_calls` → statement-level 테이블 인덱스로 resolve 하기 때문입니다.

**메뉴 ↔ Controller 양방향 매칭** (DB 테이블 또는 Excel 파일):

메뉴 정의는 프로젝트마다 스키마가 달라 두 가지 소스를 모두 지원합니다:

1. **DB 메뉴 테이블** (기본): Oracle 의 `TB_MENU` 같은 트리 테이블. `PARENT_ID` / `LEVEL` 을 따라가 ancestry 를 평탄화.
2. **Excel 파일** (`--menu-xlsx menu.xlsx`): 프로젝트별 메뉴를 Excel 로 정리한 경우. 헤더는 `1레벨 / 2레벨 / 3레벨 / 4레벨 / 5레벨 / URL` (한글 또는 `level1..5` / `lv1..5` 영문 헤더 모두 인식). URL 이 있는 행만 "호출 가능한 메인 페이지" 로 인정하고, URL 없는 행은 순수 컨테이너로 스킵.

파싱된 컨트롤러 URL 과 메뉴 URL 을 **정규화된 key** 로 교차 검증합니다:

| 분류 | 의미 |
|------|------|
| **Matched** | 메뉴 + 컨트롤러 양쪽 존재 (정상 프로그램) |
| **Unmatched Controller** | 코드는 있지만 메뉴 없음 (내부 API / 메뉴 누락) |
| **Orphan Menu** | 메뉴는 있지만 코드 없음 (미구현 / 삭제된 기능) |

가장 깊은 레벨이 `program_name`, 첫 세 레벨이 legacy `main_menu / sub_menu / tab` 슬롯에 매핑되며, **모든 비어있지 않은 레벨**은 `menu_path` 컬럼에 ` > ` 로 join 되어 보존됩니다 (예: `설비관리 > 설비조회 > 모델링 > SVID 코드 > 상세`). 따라서 4단계, 5단계 메뉴도 정보 손실 없이 그대로 보존됩니다.

**특수 패턴 지원 (실제 현업 코드에서 검증)**:
- MyBatis **Mapper interface 없는 프로젝트** — `CommonSQL.selectList("namespace.id", params)` 같은 문자열 기반 호출을 인식해 XML namespace 로 직접 매칭
- **Service interface + 하위 폴더 `service/impl/*ServiceImpl`** 자동 추적. Impl 네이밍이 `*Bo` / `*BoImpl` / `*Manager` / `*Facade` 등 레거시 관습이어도 인식
- **Lombok `@RequiredArgsConstructor`** + `private final XxxService svc` 형태의 생성자 주입
- **SAP JCo RFC**: `destination.getFunction("Z_...")`, `JCoUtil.getCoFunction(...)`, `getJCoFunction(...)` 등 **`get*Function` 계열 전부** + `String FN_XXX = "Z_..."` 상수 2-pass 해석 + local variable 해석 + **multi-arg 호출** (`getJCoFunction("Z_X", timeout, session)`)
- **Javadoc `{@link Class#method(Type)}`** 같은 stray brace 가 메서드 body 추출을 방해하지 않도록 offset-preserving comment stripping
- **Inner class / nested interface / enum** 안의 메서드가 outer class 의 top-level 메서드로 흡수되지 않음
- **진단 로그**: stereotype 분포, 파서가 감지한 endpoint / SQL / RFC 수, **method-scope 해상도 성공률** (`N/M endpoints, fallback: K`), RFC hint 와 실제 매칭률을 모두 출력해 사용자가 자가 진단 가능

**산출물 (Markdown + Excel)**:

```
output/legacy_analysis/
├── as_is_analysis_<service_name>_TIMESTAMP.md      # 단일 프로젝트
├── as_is_analysis_<service_name>_TIMESTAMP.xlsx
└── as_is_analysis_batch_TIMESTAMP.{md,xlsx}        # 배치 (monorepo)
```

Excel 의 `Programs` 시트는 다음 컬럼 순서입니다:

**Backend project / Backend framework / Menu path / File / Controller / URL / HTTP / Program / Service / Service method / XML / XML method / Table / RFC**

Monorepo 의 여러 서비스 분석 결과가 한 파일에 통합되어 **서비스 간 공유 테이블 / 공유 RFC** 를 한눈에 볼 수 있습니다. `Summary` 시트에는 전체 집계 + method-scope 해상도 성공률이, `Per Project` 시트에는 프로젝트별 breakdown 이 기록됩니다.

---

## 추천 워크플로우

```
# === 현행 데이터 구조 분석 ===
Step 1. python main.py schema                                # 스키마 추출
Step 2. python main.py query /mapper --schema-md schema.md    # 쿼리 분석 + JOIN 추론
Step 3. python main.py enrich-schema --schema-md schema.md    # LLM 코멘트 보강
Step 4. python main.py terms --schema-md enriched.md \
                            --react-dir /path/to/react        # 용어사전 (한글 정의 포함)

# === ERD 자동 생성 ===
Step 5. python main.py erd-group --schema-md enriched.md \
                                 --query-md query.md          # 주제영역별 ERD

# === 표준화 진단 ===
Step 6. python main.py standardize --schema-md enriched.md \
                                   --query-md query.md \
                                   --validate-data            # 표준화 리포트

# === AS-IS 소스 분석 (차세대 전환 입력 자료) ===
Step 7. python main.py analyze-legacy \
          --backends-root /path/to/monorepo/backend \
          --frontend-dir /path/to/frontend \
          --menu-xlsx /path/to/menu.xlsx                      # 프로그램 단위 메타데이터
```

---

## 기술 스택

| 구분 | 기술 |
|------|------|
| 언어 | Python 3.9+ |
| DB | Oracle 11g+ (oracledb thick mode) |
| LLM | Ollama / vLLM 등 OpenAI 호환 API |
| 임베딩 | Qwen3-Embedding-8B (또는 nomic-embed-text, bge-m3) |
| 벡터 DB | ChromaDB |
| ERD 렌더링 | Mermaid + D3.js (인터랙티브 HTML) |
| 쿼리 파싱 | MyBatis / iBatis XML (CDATA / dynamic `<if>` / `<choose>` / `<foreach>` / `<trim>`) |
| SQL 분석 | Oracle comma-FROM + `(+)` outer-join, multi-column JOIN, composite PK |
| 소스 분석 | Java (Spring / Vert.x, Lombok, 어노테이션 기반 라우팅), MyBatis, SAP JCo RFC |
| 리포트 | Markdown, Excel (openpyxl), HTML ERD (D3.js) |

---

## 환경 요구사항

- Python 3.9 이상
- Oracle Instant Client (11g 호환)
- 로컬 LLM 서버 (enrich-schema, standardize, erd-rag 사용 시)
- 폐쇄망 환경 지원 (모든 기능 로컬 동작)

---

## 설치 및 설정

```bash
# 설치
python -m pip install -r requirements.txt

# 환경변수 설정
cp .env.example .env
# .env 파일에서 Oracle 접속 정보, LLM 서버 주소 수정
```

`.env` 설정 예시:
```env
ORACLE_USER=myuser
ORACLE_PASSWORD=changeme
ORACLE_DSN=localhost:1521/ORCL
ORACLE_SCHEMA_OWNER=myuser
ORACLE_INSTANT_CLIENT_DIR=C:/oracle/instantclient_19_25

LLM_API_BASE=http://localhost:11434/v1
LLM_API_KEY=ollama
LLM_MODEL=qwen2.5
EMBEDDING_API_BASE=http://localhost:11434/v1
EMBEDDING_API_KEY=ollama
EMBEDDING_MODEL=Qwen3-Embedding-8B
```

---

## 활용 효과

| 기존 | 도구 적용 후 |
|------|-------------|
| FK 없어서 테이블 관계 파악 불가 | JOIN 분석으로 관계 자동 추론 (Oracle `(+)` outer join 포함) |
| ERD 없음 / 현행화 안 됨 | 주제영역별 ERD 자동 생성 (Mermaid + 인터랙티브 HTML) |
| Composite JOIN 의 여러 컬럼 쌍이 한 눈에 안 보임 | Relationship Details 표를 **테이블 쌍 단위** 로 그룹핑해 모든 컬럼 쌍을 한 row 에 통합 표시 |
| 컬럼 코멘트 비어있음 | LLM이 약어 해석하여 코멘트 자동 추천 |
| 용어사전 수작업 작성 | LLM이 약어 / 영문명 / 한글명 / **한글 정의 (업무 설명)** 자동 생성 |
| 표준화 현황 파악에 수작업 소요 | 용어/타입 불일치, 이상 데이터 자동 분석 |
| 소스코드 일일이 분석 | MyBatis/iBatis XML 자동 파싱 + Java 소스 call-graph 추적 |
| 차세대 전환 시 API 목록 / 영향 범위 수작업 | `analyze-legacy` 로 **프로그램 단위 메타데이터 시트** (Controller → Service → SQL → Table → RFC) 자동 추출 |
| 같은 서비스를 공유하는 여러 API 가 모두 동일한 테이블/RFC 로 합쳐짐 | **메서드 단위 call-graph 해상도** 로 각 API 가 실제로 호출하는 메서드 body 안의 SQL/RFC 만 정확히 분리 |
| Spring 만 지원하는 도구 | **Spring + Vert.x** 모두 지원 (프로젝트 구조 자동 감지) |
| 모노레포에서 여러 backend project 를 따로 분석 | `--backends-root` 배치 모드로 한 번에 통합 분석 + 프로젝트별 breakdown |
| `@RestVerticle`, `CommonSQL.selectList("ns.id", ...)` 같은 **레거시 관습** 인식 불가 | 어노테이션 / 문자열 기반 SQL 호출 / field 주입을 **regex 휴리스틱** 으로 인식 + namespace 역매칭 |
