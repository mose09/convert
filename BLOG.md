# 레거시 Oracle DB를 위한 스키마 분석 & ERD 자동 생성 도구

## 배경

차세대 시스템 전환을 앞두고 현행 시스템의 데이터 구조를 파악해야 하는데, Oracle 11g 레거시 환경에서 흔히 겪는 문제들이 있습니다.

- Foreign Key constraint가 거의 없음
- 테이블/컬럼 Description(코멘트)이 비어있음
- ERD 문서가 없거나 현행화되지 않음
- 테이블 간 관계를 파악하려면 소스코드를 일일이 뒤져야 함

이 도구는 **Oracle 스키마 + MyBatis/iBatis 쿼리 XML**을 분석하여, FK가 없어도 **JOIN 패턴에서 테이블 관계를 자동 추론**하고, 로컬 LLM을 활용해 **코멘트 보강, ERD 생성, 표준화 리포트**까지 한번에 처리합니다.

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

---

## 추천 워크플로우

```
Step 1. python main.py schema                              # 스키마 추출
Step 2. python main.py query /mapper --schema-md schema.md  # 쿼리 분석
Step 3. python main.py enrich-schema --schema-md schema.md  # LLM 코멘트 보강
Step 4. python main.py erd-group --schema-md enriched.md --query-md query.md  # ERD 생성
Step 5. python main.py standardize --schema-md enriched.md --query-md query.md --validate-data  # 표준화 리포트
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
| 쿼리 파싱 | MyBatis / iBatis XML |

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
| FK 없어서 테이블 관계 파악 불가 | JOIN 분석으로 관계 자동 추론 |
| ERD 없음 / 현행화 안 됨 | 주제영역별 ERD 자동 생성 (Mermaid + 인터랙티브 HTML) |
| 컬럼 코멘트 비어있음 | LLM이 약어 해석하여 코멘트 자동 추천 |
| 표준화 현황 파악에 수작업 소요 | 용어/타입 불일치, 이상 데이터 자동 분석 |
| 소스코드 일일이 분석 | MyBatis/iBatis XML 자동 파싱 |
