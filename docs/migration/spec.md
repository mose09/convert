# SPEC: SQL Migration (AS-IS → TO-BE 스키마 기반 쿼리 변환)

> 이 문서는 `convert` 프로젝트에 추가할 **SQL Migration 기능**의 구현 스펙이다.
> 기존 `CLAUDE.md` 의 코딩 스타일/파일 배치 규약을 따른다. 이 문서에 기재되지
> 않은 사항은 기존 프로젝트 관행을 우선한다.

## 0. 작업 착수 전 필수 확인

1. `TODO.md` 를 생성하고 아래 "12. 구현 순서" 의 작업 항목들을 체크박스로
   등록한다. 각 항목 완료 시 즉시 `[x]` 로 업데이트한다.
2. **AS-IS XML 형식 확인**: 프로젝트 루트에서 `grep -r "DOCTYPE" src/**/*.xml |
   head` 로 MyBatis 3.x 인지 Apache iBatis 2.x 인지 식별. 본 스펙은
   **MyBatis 3.x 가정**. 2.x 면 작업 시작 전 사용자에게 스펙 확장 요청할 것.
3. 회귀 테스트용 mock 프로젝트가 `/tmp/mock_method_spring` 등에 있다.
   `mybatis_parser.parse_all_mappers` 가 이들을 파싱할 수 있어야 한다
   (기존 기능 회귀 방지).

## 1. 기능 요약

Oracle 11g 기반의 기존 MyBatis 쿼리를 Oracle 23ai 스키마로 자동 변환한다.
스키마 변경에는 컬럼 rename, 테이블 이동, 타입 변환, 컬럼 분할/병합, 값 재매핑
등이 포함된다.

### 1.1 변환 범위

| 항목 | 범위 |
|---|---|
| AS-IS DB | Oracle 11g |
| TO-BE DB | Oracle 23ai |
| SQL 유형 | SELECT / INSERT / UPDATE / DELETE / MERGE |
| 제외 | PL/SQL 블록 (`<procedure>`, `BEGIN...END`), Oracle 힌트(`/*+ ... */`) 재작성 |
| XML 프레임워크 | MyBatis 3.x (iBatis 2.x 는 추후 확장) |
| 동적 SQL | `<if>`, `<choose>/<when>/<otherwise>`, `<where>`, `<set>`, `<trim>`, `<foreach>`, `<include>/<sql>`, `<bind>` |
| OGNL 파라미터명 | **건드리지 않음** (Java DTO/Mapper 파라미터명은 그대로) |

### 1.2 처리 방식 — 3-tier

1. **DSL 기반 변환** (`column_mapping.yaml` → sqlglot AST transform)
2. **LLM fallback** (DSL 로 변환 불가 시)
3. **Manual queue** (LLM confidence < 0.7 또는 UNRESOLVED)

### 1.3 검증 방식 — 2-stage Parse-only

| Stage | 도구 | 검사 항목 |
|---|---|---|
| A (static) | sqlglot parse + schema lookup | 구문, 컬럼/테이블 존재, 타입 호환성 |
| B (remote) | `DBMS_SQL.PARSE` | TO-BE DB 에서 실제 parse 가능 여부 |

실제 실행은 하지 않는다. `WHERE 1=2` 방식은 사용하지 않는다
(실행 자체가 수반되므로 ROW 만 0 일 뿐 lock/parse 비용 발생).

## 2. 디렉토리 / 파일 구조

```
convert/
└── oracle_embeddings/
    └── migration/                            # 신규 모듈
        ├── __init__.py
        ├── mapping_model.py                  # dataclass 정의
        ├── mapping_loader.py                 # YAML 로드 + 스키마 검증
        ├── impact_analyzer.py                # 사전 영향분석
        ├── sql_rewriter.py                   # sqlglot AST transformer (핵심)
        ├── transformers/                     # 10 종 transformer
        │   ├── column_rename.py
        │   ├── table_rename.py
        │   ├── type_conversion.py
        │   ├── column_split.py
        │   ├── column_merge.py
        │   ├── value_mapping.py
        │   ├── join_path_rewriter.py
        │   └── dropped_column_checker.py
        ├── dynamic_sql_expander.py           # <if>/<choose>/<foreach> 경로 전개
        ├── xml_rewriter.py                   # MyBatis XML 구조 보존 치환
        ├── bind_dummifier.py                 # #{x}/${x} → dummy
        ├── comment_injector.py               # 한글 주석 삽입 (post-processing)
        ├── validator_static.py               # sqlglot static 검증
        ├── validator_db.py                   # DBMS_SQL.PARSE 검증
        ├── llm_fallback.py                   # LLM 보조 변환
        └── migration_report.py               # Excel + XML 산출물

input/
├── column_mapping.yaml                       # 사용자 작성 (실 매핑)
└── column_mapping_template.yaml              # 템플릿 (예시 포함)

output/sql_migration/
├── converted/                                # 변환된 XML 전체 (경로 구조 유지)
│   └── <original_rel_path>.xml
├── diff/                                     # side-by-side HTML diff (옵션)
├── sql_migration_TIMESTAMP.md
├── sql_migration_TIMESTAMP.xlsx              # 메인 리포트
└── impact_report_TIMESTAMP.xlsx              # 사전 영향분석 (migration-impact)
```

**파일 기존 규약**: `output/` 은 산출물, `input/` 은 사용자 작성물. 템플릿은
프로젝트 내 정적 파일로 제공 (CLI `migrate-sql --init` 으로 복사).

## 3. CLI 커맨드 (3 개 신규)

기존 `main.py` 구조에 subcommand 로 추가.

### 3.1 `migration-impact`
매핑 파일이 AS-IS 쿼리에 얼마나 영향을 주는지 사전 분석. 실제 변환은 안 함.

```
python main.py migration-impact \
  --mybatis-dir <path>                       [필수]
  --mapping input/column_mapping.yaml        [필수]
  --as-is-schema output/스키마.md            [필수]
  --to-be-schema output/to_be_schema.md      [선택 — 있으면 검증 포함]
  --output output/sql_migration/impact_report.xlsx   [선택]
```

### 3.2 `migrate-sql`
실제 변환 + Stage A 검증. 산출물 생성.

```
python main.py migrate-sql \
  --mybatis-dir <path>                       [필수]
  --mapping input/column_mapping.yaml        [필수]
  --to-be-schema output/to_be_schema.md      [필수]
  --terms-md output/terms_dictionary.md      [선택 — 주석 삽입용]
  --output-format excel,xml                  [기본: excel,xml]
  --emit-column-comments                     [주석 삽입 on/off, 기본 off]
  --xml-preserve-as-is                       [AS-IS 주석 보존, 기본 on]
  --llm-fallback                             [NEEDS_LLM 자동 처리, 기본 off]
  --dry-run                                  [변환 시뮬레이션, 파일 안 씀]
```

### 3.3 `validate-migration`
Stage B (TO-BE DB parse) 검증. 이미 변환된 XML 대상.

```
python main.py validate-migration \
  --converted-dir output/sql_migration/converted \
  --dsn <to_be_dsn>                          [필수]
  --parallel 10                              [기본: 10]
  --report output/sql_migration/validation_report.xlsx
```

## 4. 입력 스키마 — `column_mapping.yaml`

최상위 구조:

```yaml
version: "1.0"
default_schema:
  as_is: "LEGACY"
  to_be: "NEW"

options:
  emit_column_comments: true
  comment_scope: [select, update, insert]   # where/join 은 기본 제외
  comment_source: terms_dictionary          # to_be_schema | terms_dictionary | both
  comment_format: "/* {ko_name} */"
  unknown_table_action: warn                # warn | error | drop

tables:
  # 1:1 rename
  - as_is: CUST
    to_be: CUSTOMER_MASTER
    type: rename

  # 분할
  - as_is: ORDER_HIST
    to_be: [ORDER_HEADER, ORDER_ITEM]
    type: split
    discriminator_column: HIST_TYPE
    discriminator_map:
      "H": ORDER_HEADER
      "I": ORDER_ITEM

  # 병합
  - as_is: [USER_BASIC, USER_DETAIL]
    to_be: USER
    type: merge
    join_condition: "USER_BASIC.USER_ID = USER_DETAIL.USER_ID"

  # 삭제
  - as_is: OBSOLETE_TBL
    to_be: null
    type: drop

columns:
  # --- 1:1 단순 rename ---
  - as_is: { table: CUST, column: CUST_NM }
    to_be: { table: CUSTOMER_MASTER, column: CUSTOMER_NAME }

  # --- 타입 변환 + 함수 래핑 ---
  - as_is: { table: CUST, column: REG_DT, type: "VARCHAR2(8)" }
    to_be: { table: CUSTOMER_MASTER, column: REGISTER_DATE, type: "DATE" }
    transform:
      read:  "TO_DATE({src}, 'YYYYMMDD')"
      write: "TO_CHAR({src}, 'YYYYMMDD')"
      where: "TO_DATE({src}, 'YYYYMMDD')"

  # --- 컬럼 분할 (1:N) ---
  - as_is: { table: CUST, column: FULL_NAME }
    to_be:
      - table: CUSTOMER_MASTER
        column: FIRST_NAME
        transform_select: "SUBSTR({src}, 1, INSTR({src}, ' ')-1)"
      - table: CUSTOMER_MASTER
        column: LAST_NAME
        transform_select: "SUBSTR({src}, INSTR({src}, ' ')+1)"
    reverse: "{FIRST_NAME} || ' ' || {LAST_NAME}"

  # --- 컬럼 병합 (N:1) ---
  - as_is:
      - { table: EVT, column: YYYY }
      - { table: EVT, column: MM }
      - { table: EVT, column: DD }
    to_be: { table: EVENT, column: EVENT_DATE, type: "DATE" }
    transform:
      combine: "TO_DATE({YYYY}||{MM}||{DD}, 'YYYYMMDD')"

  # --- 값 재매핑 ---
  - as_is: { table: CUST, column: USE_YN }
    to_be: { table: CUSTOMER_MASTER, column: IS_ACTIVE, type: "NUMBER(1)" }
    value_map: { "Y": 1, "N": 0 }
    default_value: 0

  # --- 삭제된 컬럼 ---
  - as_is: { table: CUST, column: OBSOLETE_FLAG }
    to_be: null
    action: drop_with_warning
```

### 4.1 `transform` 표현식 문법

- `{src}` : AS-IS 컬럼 자체
- `{컬럼명}` : 같은 로우의 다른 AS-IS 컬럼 (병합 케이스)
- Oracle SQL 함수 호출 가능 (sqlglot 파싱 가능해야 함)
- 표현식 유효성은 load 시 sqlglot.parse_one 으로 검증 — 실패 시 에러

### 4.2 로더 검증 항목

- `tables[].as_is` 가 AS-IS 스키마에 존재하는가
- `tables[].to_be` 가 TO-BE 스키마에 존재하는가
- `columns[].as_is.table` 이 `tables[]` 에 참조되는가
- `transform` 표현식의 `{placeholder}` 이 정의된 컬럼을 가리키는가
- 순환 참조 / 중복 정의 / 타입 불일치 감지

## 5. 내부 데이터 모델

### 5.1 `RewriteResult` (`mapping_model.py`)

```python
@dataclass
class RewriteResult:
    # 식별
    xml_file: Path
    namespace: str
    sql_id: str
    sql_type: Literal["SELECT", "INSERT", "UPDATE", "DELETE", "MERGE"]

    # 원본 / 변환
    as_is_sql: str
    to_be_sql: Optional[str]           # UNRESOLVED 면 None

    # 상태
    status: Literal["AUTO", "AUTO_WARN", "NEEDS_LLM", "UNRESOLVED", "PARSE_FAIL"]

    # 변환 과정
    applied_transformers: List[str]    # ["ColumnRename", "TypeConversion"]
    conversion_method: Literal["DSL", "sqlglot-AST", "LLM", "manual"]
    changed_items: List[ChangeItem]    # 치환된 컬럼/테이블 + 횟수
    dynamic_paths_expanded: int        # 전개된 대표 경로 수

    # LLM 관련 (LLM 사용 시만)
    llm_confidence: Optional[float]
    llm_reasoning: Optional[str]

    # 검증
    stage_a_pass: Optional[bool]       # None = 미실행
    stage_b_pass: Optional[bool]
    parse_error: Optional[str]         # ORA-xxxx 포함

    # 메타
    warnings: List[str]
    notes: List[str]                   # 특이사항 (사람 검토용)
    last_modified: datetime
```

### 5.2 `ChangeItem`

```python
@dataclass
class ChangeItem:
    kind: Literal["column", "table", "value", "type_wrap", "join_path"]
    as_is: str                          # "CUST.CUST_NM"
    to_be: str                          # "CUSTOMER_MASTER.CUSTOMER_NAME"
    count: int                          # 해당 statement 내 치환 횟수
    transformer: str                    # 담당 transformer 클래스명
```

## 6. 변환 파이프라인 (`sql_rewriter.py`)

```python
def rewrite_sql(sql: str, mapping: Mapping, schema: Schema) -> RewriteResult:
    # Step 1: sqlglot parse (Oracle dialect)
    tree = sqlglot.parse_one(sql, dialect="oracle")

    # Step 2: qualify (모든 컬럼 → table.column 명시화)
    tree = qualify(tree, schema=schema.as_is_sqlglot_schema())

    # Step 3: 10 transformer 순차 적용
    for transformer in TRANSFORMER_PIPELINE:
        tree, changes, needs_llm = transformer.apply(tree, mapping)
        # changes, needs_llm 를 RewriteResult 에 누적

    # Step 4: 재생성
    new_sql = tree.sql(dialect="oracle", pretty=True)

    # Step 5: 상태 결정 + 결과 반환
    ...
```

**transformer 적용 순서 (고정)**:

1. `TableRenameTransformer` — 테이블 이동 먼저
2. `ColumnRenameTransformer` — 컬럼 rename
3. `ColumnSplitTransformer` — 1:N 분할
4. `ColumnMergeTransformer` — N:1 병합
5. `TypeConversionTransformer` — 함수 래핑
6. `ValueMappingTransformer` — 값 재매핑
7. `JoinPathRewriter` — JOIN 경로 재설계 (실험적, LLM 경로로 빠질 수 있음)
8. `DroppedColumnChecker` — 삭제 컬럼 참조 감지 → warning

순서 중요: 테이블 rename 을 먼저 해야 이후 컬럼 치환이 새 테이블 기준으로
동작함.

## 7. 동적 SQL 경로 전개 (`dynamic_sql_expander.py`)

### 전략

```
Level 1 (기본 2 경로):
  - Maximum path: 모든 <if> test=true
  - Minimum path: 모든 <if> test=false (또는 <otherwise> 활성)

Level 2 (컬럼 커버리지 greedy):
  매핑 대상 컬럼이 Level 1 에서 누락되면, 해당 컬럼을 활성화하는 경로 추가
  (MC/DC 테스트 커버리지 유사 접근)

Level 3 (foreach 샘플링):
  <foreach collection="ids"> 에 대해 n=0, n=1, n=2 세 가지만 전개
  실전에서 n≥3 은 패턴 동일하므로 무의미
```

**최대 경로 수 제한**: statement 당 기본 10 개, 옵션으로 조정 가능. 초과 시
Level 1 만 사용.

### 경로 생성 API

```python
def expand_paths(
    stmt_element: ET.Element,
    mapping: Mapping,
    max_paths: int = 10,
) -> List[ExpandedPath]:
    ...

@dataclass
class ExpandedPath:
    rendered_sql: str                   # 정적 SQL (dynamic 태그 제거된)
    activations: Dict[str, bool]        # {<if> xpath: True/False}
    covered_columns: Set[str]           # 이 경로가 커버하는 컬럼
```

## 8. XML 재작성 (`xml_rewriter.py`)

**원칙**: MyBatis XML 의 동적 태그 구조는 **그대로 보존**, 내부 SQL 텍스트만
치환. lxml 사용.

```
원본: <select id="x">
        SELECT CUST_NM FROM CUST
        <if test="active != null">
          AND USE_YN = #{active}
        </if>
      </select>

변환: <select id="x">
        SELECT c.customer_name FROM customer_master c
        <if test="active != null">
          AND c.is_active = #{active}      <!-- value_map: Y/N → 1/0 적용 -->
        </if>
      </select>
```

### 치환 단위

각 text node 와 CDATA 를 독립 단위로 sqlglot parse 시도 → 실패하면 "문장
조각"으로 간주하고 경로 전개본을 통해 전체 SQL 로 재구성 후 변환 → 원래
조각 위치에 되돌려 치환. **조각 경계 오프셋 맵핑이 핵심.**

### `<include refid="...">` 처리

- Pass 1: 모든 `<sql id="...">` 를 인덱싱
- Pass 2: include 참조를 inline expansion (경로 전개 용도로만)
- 변환된 결과는 원본 include 구조 유지 — `<sql>` 본문도 대응 변환

## 9. 주석 삽입 (`comment_injector.py`)

### 동작

```
입력: SELECT c.customer_name, c.register_date FROM customer_master c
출력: SELECT c.customer_name     /* 사용자명 */
          , c.register_date      /* 등록일자 */
      FROM customer_master c
```

### 구현

1. sqlglot tokenize (Oracle dialect)
2. `TokenType.VAR` 토큰 중 identifier 인 것 추출
3. 컨텍스트 (SELECT 절 / WHERE 절 등) 판정 — `comment_scope` 옵션과 비교
4. `comment_source` 에 따라 주석 텍스트 결정
   - `to_be_schema` : TO-BE 스키마의 COLUMN_COMMENT
   - `terms_dictionary` : terms_dictionary.md 의 Korean 필드 lookup
   - `both` : 스키마 우선, 없으면 용어사전
5. 토큰 뒤에 `/* {text} */` 삽입 + 정렬 공백 추가
6. detokenize

### 제약

- WHERE / JOIN 절 기본 제외 (가독성)
- SELECT 절: 컬럼별 개행 + 정렬 (`--format=align` 옵션, 기본 on)
- 주석 텍스트가 SQL comment 로 안전한지 검증 (`*/` 포함 시 제거)
- 한글 포함 시 UTF-8 확인

## 10. LLM fallback (`llm_fallback.py`)

### 호출 조건

다음 중 하나:
- Transformer 가 `needs_llm=True` 반환 (복잡한 JOIN 경로 등)
- `column_split` / `column_merge` 가 SELECT 절에 등장하는 케이스
  (transform_select 만으로 한계 있을 때)
- 사용자가 `--force-llm` 지정

### 프롬프트 구조

```
SYSTEM: You are rewriting Oracle SQL for schema migration.
        Return JSON only. No prose.

USER:
## Context
- AS-IS table/column mappings (only relevant ones):
  {relevant_mappings_yaml}

## AS-IS SQL
{original_sql}

## Pattern-engine partial result
{partial_result}   # 또는 "SKIPPED"

## TO-BE schema (only tables referenced above)
{ddl_snippet}

## Rules
- Keep MyBatis OGNL params (#{x}, ${x}) UNCHANGED
- Keep dynamic tags (<if>, <foreach>, ...) UNCHANGED — operate on SQL text only
- Do not invent columns not in schema
- Preserve all business logic

## Output (JSON)
{
  "converted_sql": "...",
  "confidence": 0.0-1.0,
  "changes": ["...", "..."],
  "needs_human_review": true|false,
  "review_reason": "..."
}
```

### 컨텍스트 최소화

- 매핑 YAML 전체 대신 **현재 statement 에 등장하는 테이블/컬럼 엔트리만**
- TO-BE 스키마 DDL 도 해당 테이블만
- 토큰 수 제한: 8K 이내 유지

### 신뢰도 처리

- `confidence < 0.7` 또는 `needs_human_review=true` → `UNRESOLVED`
  로 마킹, AS-IS 쿼리 활성 유지 + LLM 제안은 주석으로만 병기

## 11. 검증 (`validator_static.py`, `validator_db.py`)

### Stage A — sqlglot static

```python
def validate_static(to_be_sql: str, to_be_schema: Schema) -> ValidationResult:
    # 1. sqlglot.parse_one 성공 여부
    # 2. 모든 exp.Column 이 스키마에 존재
    # 3. 모든 exp.Table 이 스키마에 존재
    # 4. (옵션) 비교식 타입 호환성 (NUMBER vs VARCHAR2 등)
    ...
```

### Stage B — DBMS_SQL.PARSE

```python
def validate_db(to_be_sql: str, conn) -> ValidationResult:
    # dummy bind 치환
    dummified = bind_dummifier.dummify(to_be_sql, schema_bind_types)
    try:
        cursor_id = conn.cursor().var(int)
        conn.callproc("DBMS_SQL.PARSE",
                      [cursor_id, dummified, 1])  # 1 = v7 language
        return ValidationResult(ok=True)
    except DatabaseError as e:
        return ValidationResult(ok=False,
                                ora_code=e.args[0].code,
                                message=e.args[0].message)
```

### 병렬화

`ThreadPoolExecutor(max_workers=--parallel)`. 기본 10. Oracle 23ai 는 초당
수백 parse 수용 가능하므로 5000 × 대표경로 3 = 15000 parse 가 수 분 내 완료.

## 12. 산출물

### 12.1 Excel (`sql_migration_TIMESTAMP.xlsx`)

5 시트:

| 시트 | 내용 |
|---|---|
| `Summary` | 전체 집계: 총 N, AUTO/AUTO_WARN/NEEDS_LLM/UNRESOLVED 카운트, Stage A/B 통과율, 자동화율 |
| `Conversions` | 메인 시트 (아래 18 컬럼) |
| `Validation Errors` | Stage A/B 실패 statement, ORA 코드 포함 |
| `Unresolved Queue` | 수동 리뷰 대상 |
| `Mapping Coverage` | 매핑 YAML 엔트리별 실제 사용 횟수 |

**`Conversions` 시트 컬럼**:

| # | 컬럼 | 예시 |
|---|---|---|
| 1 | No | 1 |
| 2 | XML File | `mapper/CustomerMapper.xml` |
| 3 | Namespace | `com.acme.CustomerMapper` |
| 4 | SQL ID | `selectActiveCustomers` |
| 5 | SQL Type | SELECT |
| 6 | AS-IS SQL | (wrap text) |
| 7 | TO-BE SQL | (wrap text) |
| 8 | Status | AUTO / AUTO_WARN / NEEDS_LLM / UNRESOLVED / PARSE_FAIL |
| 9 | Applied Transformers | `ColumnRename, TypeConversion` |
| 10 | Conversion Method | DSL / sqlglot-AST / LLM / manual |
| 11 | Changed Items | `CUST_NM→CUSTOMER_NAME(3), REG_DT→REGISTER_DATE(1)` |
| 12 | Dynamic Paths | 3 |
| 13 | Stage A Pass | Y / N / - |
| 14 | Stage B Pass | Y / N / - |
| 15 | ORA Error | `ORA-00904: invalid identifier` |
| 16 | LLM Confidence | 0.85 (LLM 사용 시만) |
| 17 | Notes | 특이사항 |
| 18 | Last Modified | 2026-04-21 15:32 |

**하이라이트**:
- 노란색: AUTO_WARN, NEEDS_LLM
- 빨간색: UNRESOLVED, PARSE_FAIL, Stage B 실패
- 회색: 변환 대상 없음

### 12.2 XML (converted/)

원본 디렉토리 구조 유지. 각 statement 위에 메타데이터 주석 + AS-IS 주석.

```xml
<select id="selectActiveCustomers" resultType="Customer">
  <!-- ========== MIGRATION: selectActiveCustomers ==========
       Status           : AUTO
       Method           : DSL + sqlglot-AST
       Applied          : ColumnRename, TableRename
       Changed          : CUST_NM→CUSTOMER_NAME, CUST→CUSTOMER_MASTER
       Stage A (static) : PASS
       Stage B (parse)  : PASS
       Notes            : -
       ============================================================
  -->

  <!-- AS-IS (original)
  SELECT  CUST_NO
        , CUST_NM
        , REG_DT
    FROM  CUST
   WHERE  USE_YN = 'Y'
  -->

  SELECT  c.customer_no       /* 고객번호 */
        , c.customer_name     /* 사용자명 */
        , c.register_date     /* 등록일자 */
    FROM  customer_master c
   WHERE  c.is_active = 1     /* 활성여부: Y/N → 1/0 */

</select>
```

**UNRESOLVED 경우**:
- AS-IS 쿼리는 활성 유지 (컴파일/실행 보장)
- TO-BE 제안은 주석 블록 내부에만 — 절대 활성화하지 않음
- 메타데이터 주석에 `Status: UNRESOLVED`, `Reason: ...` 명시

## 13. 구현 순서 (TODO.md 생성 기준)

리스크 낮은 순서로 단계화. 각 단계마다 회귀 테스트 mock 프로젝트 확인.

1. [ ] `mapping_model.py`, `mapping_loader.py` + `column_mapping_template.yaml`
2. [ ] `migration-impact` 커맨드 — 매핑 파일 검증 + 영향 리포트만 (변환 X)
3. [ ] `sql_rewriter.py` + `ColumnRenameTransformer` + `TableRenameTransformer`
4. [ ] `dynamic_sql_expander.py` — Level 1 (2 경로) 만
5. [ ] `xml_rewriter.py` — lxml 기반 구조 보존 치환
6. [ ] `validator_static.py` — sqlglot 기반
7. [ ] `migration_report.py` — Excel 5 시트
8. [ ] `bind_dummifier.py` + `validator_db.py` + `validate-migration` 커맨드
9. [ ] 남은 transformer 6 종 (`TypeConversion`, `ColumnSplit`, `ColumnMerge`,
   `ValueMapping`, `JoinPathRewriter`, `DroppedColumnChecker`)
10. [ ] `dynamic_sql_expander.py` — Level 2 (컬럼 커버리지), Level 3 (foreach)
11. [ ] `comment_injector.py` — 한글 주석 삽입
12. [ ] XML 산출물 (AS-IS 주석 보존 + 메타데이터 블록)
13. [ ] `llm_fallback.py` + `--llm-fallback` 옵션
14. [ ] `migrate-sql` 커맨드 통합 + 회귀 테스트
15. [ ] README / CLAUDE.md 업데이트

## 14. 회귀 테스트 체크리스트

각 단계 완료 후 아래 커맨드가 기존처럼 동작해야 함:

```bash
python main.py schema --help
python main.py query /tmp/mock_method_spring
python main.py analyze-legacy --backend-dir /tmp/mock_method_spring --skip-menu
python main.py standardize --schema-md output/스키마.md --query-md output/query.md
```

신규 기능 동작 검증용 샘플:

```bash
python main.py migration-impact \
  --mybatis-dir /tmp/mock_method_spring \
  --mapping input/column_mapping_template.yaml \
  --as-is-schema /tmp/sample_schema.md

python main.py migrate-sql \
  --mybatis-dir /tmp/mock_method_spring \
  --mapping input/column_mapping_template.yaml \
  --to-be-schema /tmp/sample_schema_tobe.md \
  --dry-run
```

## 15. 수용 기준

- 5000 statement 기준:
  - `migrate-sql` 전체 런타임 < 5 분 (LLM 제외)
  - `validate-migration` 런타임 < 10 분 (parallel=10)
  - 자동화율 ≥ 70% (AUTO + AUTO_WARN)
- 기존 커맨드 (schema/query/erd/terms/analyze-legacy 등) 회귀 없음
- `column_mapping.yaml` 문법 오류 시 **구현 전 단계에서** 명확한 에러
  메시지 (어느 라인, 어떤 필드, 기대 타입) 반환
- LLM 실패/타임아웃 시 `UNRESOLVED` 로 graceful fallback, 전체 프로세스
  중단 없음

## 16. 스타일 / 규약 (기존 CLAUDE.md 준수)

- CLI help 는 **영문 + 한글 혼용**
- 진단 로그는 **count 기반** 요약 ("Converted 4823/5000 statements (AUTO: 3600,
  AUTO_WARN: 400, NEEDS_LLM: 600, UNRESOLVED: 400)")
- 새 파서는 기존 패턴 준수: `_*_RE` regex 상수 + 함수 단위 추출기 + 상위
  `build_*` / `parse_*` 공개 API
- 기존 필드 **제거 금지**, 추가만
- `mybatis_parser._read_file_safe` 재사용 (utf-8 → euc-kr → cp949 fallback)
- `legacy_util.normalize_url` 는 URL 전용이므로 테이블/컬럼 정규화는 별도
  헬퍼 (`_normalize_identifier`) 추가
- Conventional Commits: `feat(migration):`, `fix(migration):`, `docs(migration):`
