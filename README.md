# Oracle Table Columns to LLM Embeddings Converter

Oracle 테이블의 컬럼 데이터를 LLM 임베딩 벡터로 변환하는 Python 도구입니다.

## 주요 기능

- Oracle DB 테이블에서 컬럼 데이터 자동 추출 (LOB/BLOB 타입 자동 제외)
- 행 데이터를 커스터마이징 가능한 텍스트 템플릿으로 변환
- OpenAI 호환 API를 통한 배치 임베딩 생성 (OpenAI, Azure, vLLM, Ollama 등)
- Parquet / JSONL 파일 저장 또는 Oracle 테이블에 직접 저장

## 프로젝트 구조

```
convert/
├── main.py                    # CLI 진입점
├── config.yaml                # 설정 파일
├── .env.example               # 환경변수 템플릿
├── requirements.txt           # Python 의존성
└── oracle_embeddings/         # 핵심 패키지
    ├── db.py                  # Oracle DB 연결
    ├── extractor.py           # 컬럼 메타데이터 및 데이터 추출
    ├── textifier.py           # 행 데이터 → 텍스트 변환
    ├── embedder.py            # LLM API 임베딩 생성
    └── storage.py             # 결과 저장 (파일/Oracle)
```

## 설치

```bash
pip install -r requirements.txt
```

> `oracledb`는 thin mode(순수 Python)를 기본으로 사용하므로 Oracle Instant Client 설치가 필요 없습니다.

## 설정

### 1. 환경변수

`.env.example`을 복사하여 `.env` 파일을 생성합니다.

```bash
cp .env.example .env
```

```env
ORACLE_USER=myuser
ORACLE_PASSWORD=changeme
OPENAI_API_KEY=sk-...
```

### 2. config.yaml

```yaml
oracle:
  dsn: "localhost:1521/FREEPDB1"
  user: "${ORACLE_USER}"
  thick_mode: false

tables:
  - name: "CUSTOMERS"
    columns: ["NAME", "EMAIL", "ADDRESS"]  # 생략 시 전체 컬럼 자동 탐색
  - name: "PRODUCTS"
    # columns 생략 = LOB/BLOB 제외 전체 컬럼

embedding:
  api_base: "https://api.openai.com/v1"
  model: "text-embedding-3-small"
  batch_size: 100
  dimensions: 1536

storage:
  file_format: "parquet"       # parquet | jsonl
  output_dir: "./output"
  write_to_oracle: false
  oracle_target_table: "EMBEDDINGS_STORE"

processing:
  row_limit: null              # null = 전체 행
  text_template: "{column_name}: {value}"
  row_separator: " | "
```

## 사용법

### 기본 실행

```bash
python main.py
```

### 옵션

```bash
# 특정 설정 파일 사용
python main.py --config my_config.yaml

# 특정 테이블만 처리
python main.py --table CUSTOMERS

# 드라이런 (임베딩 생성 없이 텍스트 변환 결과만 확인)
python main.py --dry-run
```

### 출력 예시

**텍스트 변환 (dry-run):**
```
[0] NAME: John Smith | EMAIL: john@example.com | ADDRESS: 123 Main St
[1] NAME: Jane Doe | EMAIL: jane@example.com | ADDRESS: 456 Oak Ave
```

**Parquet 출력:** `output/CUSTOMERS_20260401_120000.parquet`

| row_index | NAME       | EMAIL            | embedding          |
|-----------|------------|------------------|--------------------|
| 0         | John Smith | john@example.com | [0.012, -0.034, …] |

**JSONL 출력:** `output/CUSTOMERS_20260401_120000.jsonl`
```json
{"row_index": 0, "source_table": "CUSTOMERS", "data": {"NAME": "John Smith", "EMAIL": "john@example.com"}, "embedding": [0.012, -0.034, ...]}
```

## 처리 파이프라인

```
Oracle DB → 컬럼 추출 → 텍스트 변환 → 임베딩 생성 → 저장
              │              │              │            │
        extractor.py    textifier.py   embedder.py   storage.py
```

1. **추출**: `all_tab_columns`에서 메타데이터를 조회하고 데이터를 SELECT
2. **텍스트 변환**: 각 행을 템플릿 기반으로 문자열로 변환 (NULL 값 자동 제외)
3. **임베딩 생성**: 배치 단위로 API 호출 (실패 시 최대 3회 재시도)
4. **저장**: Parquet/JSONL 파일 또는 Oracle 테이블에 저장

## 라이선스

MIT
