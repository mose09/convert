# Oracle Schema & Query Analyzer for Msty Knowledge Base

Oracle DB 스키마 메타데이터 + MyBatis 쿼리 분석 결과를 Markdown으로 추출하여, Msty Knowledge Base에서 RAG로 활용하는 도구입니다.

FK가 없는 레거시 DB에서도 **쿼리의 JOIN 조건을 분석**하여 테이블 간 관계를 추론합니다.

## 추출 정보

### 1. Schema (Oracle DB)
- 테이블 목록 및 테이블 코멘트
- 컬럼 정보 (이름, 데이터타입, Nullable, 기본값, 코멘트)
- Primary Key, Foreign Key, 인덱스

### 2. Query (MyBatis XML)
- SQL 쿼리 목록 (SELECT/INSERT/UPDATE/DELETE)
- **JOIN 기반 테이블 관계 추론** (FK 없이도 관계 파악)
- 테이블 사용 통계 (어떤 테이블이 어떤 쿼리에서 사용되는지)
- 쿼리 상세 (mapper별 SQL)

## 프로젝트 구조

```
convert/
├── main.py                    # CLI 진입점 (schema / query / all)
├── config.yaml                # 설정 파일
├── .env.example               # 환경변수 템플릿
├── requirements.txt           # Python 의존성
└── oracle_embeddings/
    ├── db.py                  # Oracle DB 연결 (thick mode)
    ├── extractor.py           # 스키마 메타데이터 추출
    ├── mybatis_parser.py      # MyBatis XML 파싱 & JOIN 분석
    └── storage.py             # Markdown 파일 생성
```

## 설치

```bash
pip install -r requirements.txt
```

## 설정

```bash
cp .env.example .env
```

```env
ORACLE_USER=myuser
ORACLE_PASSWORD=changeme
```

`config.yaml`에서 Oracle 접속 정보와 출력 설정을 수정합니다.

## 사용법

### 1. 스키마 추출 (Oracle 접속 필요)

```bash
python main.py schema
python main.py schema --table CUSTOMERS
python main.py schema --owner HR
```

### 2. 쿼리 분석 (Oracle 접속 불필요)

```bash
# MyBatis mapper XML이 있는 디렉토리 경로 지정
python main.py query /path/to/mybatis/mapper

# 예: Java 프로젝트의 resources 디렉토리
python main.py query ./src/main/resources/mapper
```

### 3. 둘 다 실행

```bash
python main.py all /path/to/mybatis/mapper
```

## 출력 예시

### 쿼리 분석 결과 (`output/query_analysis_20260402_120000.md`)

```markdown
# Query Analysis (MyBatis)

- Mapper files: 15
- SQL statements: 87
- Discovered relationships: 23

---

## Inferred Relationships (from JOIN)

FK가 없는 테이블 간의 관계를 쿼리의 JOIN 조건에서 추론한 결과입니다.

| Table A | Column | JOIN | Table B | Column | Type | Source |
|---------|--------|------|---------|--------|------|--------|
| ORDERS | CUSTOMER_ID | <-> | CUSTOMERS | CUSTOMER_ID | INNER JOIN | OrderMapper.xml#selectOrder |
| ORDER_ITEMS | ORDER_ID | <-> | ORDERS | ORDER_ID | LEFT JOIN | OrderMapper.xml#selectItems |
| ORDER_ITEMS | PRODUCT_ID | <-> | PRODUCTS | PRODUCT_ID | INNER JOIN | OrderMapper.xml#selectItems |

## Table Usage Summary

| Table | SELECT | INSERT | UPDATE | DELETE | Mappers |
|-------|--------|--------|--------|--------|---------|
| CUSTOMERS | 12 | 2 | 3 | 1 | CustomerMapper.xml, OrderMapper.xml |
| ORDERS | 8 | 1 | 2 | 0 | OrderMapper.xml |
```

## Msty RAG 활용

1. `python main.py schema` → 스키마 `.md` 생성
2. `python main.py query ./mapper` → 쿼리 분석 `.md` 생성
3. 두 `.md` 파일을 Msty Knowledge Base에 임포트
4. 채팅에서 질의

**질의 예시:**
- "ORDERS 테이블과 연관된 테이블은?"
- "CUSTOMER_ID 컬럼을 사용하는 테이블 관계 보여줘"
- "OrderMapper에서 JOIN하는 테이블 목록은?"
- "가장 많이 조회되는 테이블은?"
- "이 관계 정보로 ERD 그려줘"

## ERD 활용

Msty에 스키마 + 쿼리 분석 결과를 임베딩한 후, 아래와 같이 질의하면 ERD 생성을 도와줍니다:

```
"Inferred Relationships 기반으로 Mermaid ERD 코드 생성해줘"
```

Msty가 관계 정보를 참조하여 Mermaid 다이어그램 코드를 생성하면, 이를 ERD 도구에서 시각화할 수 있습니다.

## 라이선스

MIT
