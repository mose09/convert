# Oracle Schema & Query Analyzer for Msty Knowledge Base

Oracle DB 스키마 메타데이터 + MyBatis 쿼리 분석 결과를 Markdown으로 추출하여, Msty Knowledge Base에서 RAG로 활용하는 도구입니다.

FK가 없고 description이 없는 레거시 DB 환경에서도:
- **쿼리의 JOIN 조건 분석**으로 테이블 간 관계를 추론
- **로컬 LLM 보조**로 컬럼 의미 해석, 누락 관계 발견, 도메인 그룹핑
- **Mermaid ERD 자동 생성**

## 기능 요약

| Command | 설명 | Oracle 접속 | LLM |
|---------|------|:-----------:|:---:|
| `schema` | 테이블/컬럼 스키마 추출 | O | X |
| `query` | MyBatis XML 쿼리 분석 | X | X |
| `erd` | Mermaid ERD 생성 | O | 선택 |
| `all` | 위 3개 전부 실행 | O | 선택 |

## 프로젝트 구조

```
convert/
├── main.py                    # CLI 진입점 (schema / query / erd / all)
├── config.yaml                # 설정 파일
├── .env.example               # 환경변수 템플릿
├── requirements.txt           # Python 의존성
└── oracle_embeddings/
    ├── db.py                  # Oracle DB 연결 (thick mode, 11g 호환)
    ├── extractor.py           # 스키마 메타데이터 추출
    ├── mybatis_parser.py      # MyBatis XML 파싱 & JOIN 분석
    ├── erd_generator.py       # Mermaid ERD 코드 생성
    ├── llm_assist.py          # 로컬 LLM 보조 (컬럼 해석, 관계 추론, 도메인 분류)
    └── storage.py             # Markdown 파일 생성
```

## 설치

```bash
pip install -r requirements.txt

# ERD + LLM 보조 사용 시 추가 설치
pip install openai
```

## 설정

```bash
cp .env.example .env
```

```env
ORACLE_USER=myuser
ORACLE_PASSWORD=changeme
```

`config.yaml`:

```yaml
oracle:
  dsn: "your_host:1521/your_service"
  user: "${ORACLE_USER}"
  schema_owner: "${ORACLE_USER}"
  instant_client_dir: "C:/oracle/instantclient_19_25"  # 11g 필수

storage:
  file_format: "markdown"
  output_dir: "./output"

# LLM 설정 (erd --llm-assist 옵션 사용 시)
llm:
  api_base: "http://localhost:11434/v1"  # Ollama
  api_key: "ollama"
  model: "llama3"
```

## 사용법

### 1. 스키마 추출

```bash
python main.py schema
python main.py schema --table CUSTOMERS
python main.py schema --owner HR
```

### 2. 쿼리 분석 (Oracle 접속 불필요)

```bash
python main.py query /path/to/mybatis/mapper
```

### 3. ERD 생성

```bash
# 스키마만으로 ERD (JOIN 관계 없이)
python main.py erd

# 스키마 + MyBatis JOIN 분석
python main.py erd --mybatis-dir /path/to/mapper

# 스키마 + JOIN + LLM 보조 (컬럼 해석, 누락 관계, 도메인 그룹핑)
python main.py erd --mybatis-dir /path/to/mapper --llm-assist
```

### 4. 전체 실행

```bash
python main.py all /path/to/mapper
python main.py all /path/to/mapper --llm-assist
```

## ERD 생성 파이프라인

```
                                  ┌─────────────────────┐
Oracle DB ──> 스키마 추출          │  --llm-assist 옵션  │
                │                 │                     │
MyBatis XML ──> JOIN 관계 추출  ──>│ 1. 컬럼명 한국어 해석  │──> Mermaid ERD (.md)
                                  │ 2. 누락 관계 추론    │
                                  │ 3. 도메인 그룹핑     │
                                  └─────────────────────┘
```

### LLM이 보조하는 영역

| 영역 | 구조화 데이터 한계 | LLM 보완 |
|------|-------------------|----------|
| 컬럼명 해석 | `CUST_NO`, `ORD_DT` → 의미 불명 | "고객번호", "주문일자" 추론 |
| 관계 카디널리티 | JOIN 존재 여부만 파악 | 1:1, 1:N, N:M 추론 |
| 누락 관계 | 쿼리에 없는 관계 못 찾음 | 컬럼명 유사도로 관계 제안 |
| 도메인 그룹핑 | 나열만 됨 | "고객", "주문", "정산" 등 분류 |

## 출력 예시

### ERD (`output/erd_HR_20260402_120000.md`)

```markdown
# Entity Relationship Diagram

- Owner: HR
- Tables: 5
- Relationships (from JOIN): 4
- Relationships (LLM inferred): 2

## Domain Groups

### 고객관리
- CUSTOMERS - 고객 기본정보 테이블
- CUSTOMER_ADDR - 고객 주소 정보

### 주문관리
- ORDERS - 주문 헤더
- ORDER_ITEMS - 주문 상세 항목

## ERD Diagram

erDiagram
    CUSTOMERS {
        NUMBER CUSTOMER_ID PK "고객 고유 ID"
        VARCHAR2 CUST_NM "고객명"
        VARCHAR2 EMAIL "이메일"
    }
    ORDERS {
        NUMBER ORDER_ID PK "주문 ID"
        NUMBER CUSTOMER_ID FK "고객 ID"
    }
    ORDERS }o--|| CUSTOMERS : "CUSTOMER_ID = CUSTOMER_ID"
```

### 렌더링 방법

생성된 Mermaid 코드를 아래 방법으로 시각화할 수 있습니다:

- **VS Code**: Mermaid 확장 설치 → `.md` 파일 미리보기
- **Msty**: ERD .md 파일 내용을 채팅에 붙여넣기
- **mermaid-cli**: `npm install -g @mermaid-js/mermaid-cli` → `mmdc -i erd.md -o erd.png`

## Msty RAG 활용

생성된 `.md` 파일들을 Msty Knowledge Base에 임포트 후 질의:

- "ORDERS 테이블과 연관된 테이블은?"
- "CUSTOMER_ID 컬럼을 사용하는 테이블 관계 보여줘"
- "가장 많이 조회되는 테이블은?"
- "주문 도메인의 ERD를 Mermaid로 그려줘"

## 라이선스

MIT
