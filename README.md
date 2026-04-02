# Oracle Schema to Msty Knowledge Base Converter

Oracle DB의 테이블/컬럼 스키마 메타데이터를 Markdown 파일로 추출하여, Msty Knowledge Base에 임포트하고 RAG로 활용하는 도구입니다.

## 추출 정보

- 테이블 목록 및 테이블 코멘트
- 컬럼 정보 (이름, 데이터타입, Nullable, 기본값, 코멘트)
- Primary Key
- Foreign Key 관계 (테이블 간 참조)
- 인덱스 정보
- 전체 FK 관계 요약 (Relationship Summary)

## 프로젝트 구조

```
convert/
├── main.py                    # CLI 진입점
├── config.yaml                # 설정 파일
├── .env.example               # 환경변수 템플릿
├── requirements.txt           # Python 의존성
└── oracle_embeddings/
    ├── db.py                  # Oracle DB 연결 (thick mode)
    ├── extractor.py           # 스키마 메타데이터 추출
    └── storage.py             # Markdown/TXT 파일 생성
```

## 설치

```bash
pip install -r requirements.txt
```

## 설정

### 1. 환경변수

```bash
cp .env.example .env
```

```env
ORACLE_USER=myuser
ORACLE_PASSWORD=changeme
```

### 2. config.yaml

```yaml
oracle:
  dsn: "your_host:1521/your_service"
  user: "${ORACLE_USER}"
  schema_owner: "${ORACLE_USER}"
  instant_client_dir: "C:/oracle/instantclient_19_25"  # Oracle 11g는 필수

# 특정 테이블만 추출 (생략 시 전체 테이블)
# tables:
#   - "CUSTOMERS"
#   - "ORDERS"

storage:
  file_format: "markdown"  # markdown | txt
  output_dir: "./output"
```

## 사용법

```bash
# 전체 테이블 스키마 추출
python main.py

# 특정 테이블만 추출
python main.py --table CUSTOMERS

# 특정 스키마 소유자 지정
python main.py --owner HR

# TXT 포맷으로 출력
python main.py --format txt
```

## 출력 예시

### Markdown (`output/HR_schema_20260402_120000.md`)

```markdown
# HR Database Schema

Total tables: 3

---

## CUSTOMERS

> 고객 정보 관리 테이블

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| CUSTOMER_ID (PK) | NUMBER(10) | N | | 고객 고유 ID |
| NAME | VARCHAR2(100) | N | | 고객명 |
| EMAIL | VARCHAR2(200) | Y | | 이메일 주소 |

**Primary Key**: CUSTOMER_ID

---

## ORDERS

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| ORDER_ID (PK) | NUMBER(10) | N | | 주문 ID |
| CUSTOMER_ID | NUMBER(10) | N | | 고객 ID |

**Foreign Keys**:
- `CUSTOMER_ID` -> `CUSTOMERS.CUSTOMER_ID`

---

## Relationship Summary

| Source Table | Column | -> | Target Table | Column |
|-------------|--------|-----|-------------|--------|
| ORDERS | CUSTOMER_ID | -> | CUSTOMERS | CUSTOMER_ID |
```

## Msty 연동

1. `python main.py` 실행
2. `output/` 폴더의 `.md` 파일을 Msty Knowledge Base에 드래그 앤 드롭
3. 채팅에서 Knowledge Base 선택 후 질의

**질의 예시:**
- "CUSTOMERS 테이블에 어떤 컬럼이 있어?"
- "ORDERS 테이블과 연관된 테이블은?"
- "고객 ID를 참조하는 테이블 목록 알려줘"
- "VARCHAR2 타입인 컬럼 목록 보여줘"

## 라이선스

MIT
