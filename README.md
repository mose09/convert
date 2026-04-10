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
| `review-sql` | SQL 쿼리 정적 분석 + LLM 리뷰 | X | 선택 |
| `standardize` | 표준화 분석 리포트 생성 | 선택 | O |
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
    └── storage.py                # Markdown 파일 생성
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

스키마 컬럼명 + React 소스 변수명에서 단어를 수집하고, LLM이 약어/영문명/한글명을 생성합니다.

```bash
# 스키마 + React 소스 양쪽에서 수집
python main.py terms --schema-md ./output/스키마.md --react-dir /path/to/react/src

# 스키마만
python main.py terms --schema-md ./output/스키마.md

# LLM 없이 단어 수집만
python main.py terms --schema-md ./output/스키마.md --react-dir ./src --skip-llm
```

산출물:
```
output/
├── terms_dictionary_TIMESTAMP.md    # 용어사전 Markdown
└── terms_dictionary_TIMESTAMP.xlsx  # 용어사전 Excel
    ├── Sheet: 용어사전      (전체)
    ├── Sheet: DB+FE공통     (양쪽에서 사용, 표준화 우선)
    ├── Sheet: DB전용        (DB에서만 사용)
    ├── Sheet: FE전용        (프론트에서만 사용)
    └── Sheet: 미식별        (LLM이 해석 못한 단어)
```

### 6. SQL 리뷰 (정적 분석 + LLM)

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

### 7. 표준화 분석 리포트

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
```

## ERD 렌더링

- **VS Code**: Markdown Preview Mermaid Support 확장 설치 → `Ctrl+Shift+V`
- **mermaid-cli**: `mmdc -i erd.md -o erd.png`

## 라이선스

MIT
