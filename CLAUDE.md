# CLAUDE.md

## 작업 규칙

1. 모든 작업 시작 전 `TODO.md` 파일에 할 일 목록을 작성한다.
2. 각 작업 항목은 `- [ ]` (미완료) / `- [x]` (완료) 체크박스를 사용한다.
3. 작업 완료 시 즉시 `TODO.md`에서 해당 항목을 완료 처리한다.
4. 모든 작업이 끝나면 TODO.md를 최종 확인한다.

## 프로젝트 컨텍스트

- Oracle 레거시 DB 스키마 분석 + MyBatis/iBatis 쿼리 분석 + AS-IS 소스코드 분석 도구
- 폐쇄망 환경, 로컬 LLM 사용 (Ollama / vLLM / 사내 LLM 게이트웨이)
- Windows PC에서 실행
- Oracle 11g (thick mode)
- Python CLI 기반
- 레포: `github.com/mose09/convert`, 작업 브랜치: `claude/push-previous-changes-4P5x8`

## 주요 커맨드 (22종)

| 커맨드 | 목적 | LLM | Oracle |
|--------|------|-----|--------|
| `schema` | 테이블/컬럼/PK/FK/Index → Markdown | X | O |
| `query` | MyBatis/iBatis XML → JOIN 관계 + Table Usage | X | X |
| `enrich-schema` | 빈 코멘트에 LLM이 한글 설명 추가 `(LLM추천)` 표기 | O | X |
| `erd` / `erd-md` / `erd-group` / `erd-rag` | Mermaid + 인터랙티브 HTML ERD | 선택 | X |
| `terms` | 스키마 + React 소스에서 용어사전 (약어/영문/한글/Definition 4필드) | O | X |
| `standardize` | 표준화 리포트 (JOIN/타입 불일치, 네이밍 이탈 등 8섹션) | 선택 | 선택 |
| `review-sql` | SQL 안티패턴 리뷰 + LLM 개선안 | 선택 | X |
| `validate-naming` | 신규 DDL/이름이 용어사전 표준 준수하는지 검증 | X | X |
| `gen-ddl` | 자연어 요청 → 표준 CREATE TABLE DDL | O | 선택 |
| `audit-standards` | 기존 스키마 전체를 용어사전 기준으로 전수 감사 | X | X |
| `analyze-legacy` | **AS-IS 소스 분석 (핵심)** — Controller→Service→XML→Table→RFC 체인 | X | 선택 |
| `discover-patterns` | **LLM이 프로젝트 구조/패턴 자동 추출 → patterns.yaml** | O | X |
| `convert-mapping` | **AS-IS↔TO-BE 매핑 .md → column_mapping.yaml** LLM 변환 | 선택 | X |
| `migration-impact` | **SQL Migration 사전 영향분석** — column_mapping.yaml × AS-IS 쿼리 | X | X |
| `migrate-sql` | **AS-IS MyBatis XML → TO-BE 스키마용 일괄 변환 + 5시트 Excel 리포트** | 선택 | X |
| `validate-migration` | **변환 XML 의 TO-BE SQL 을 parse-only 검증 (Stage B)** | X | O |

## 주요 모듈 (analyze-legacy 파이프라인)

| 파일 | 역할 |
|------|------|
| `oracle_embeddings/legacy_java_parser.py` | Java 소스 파싱 (메서드 단위 body + body-scope sql/rfc/field call 수집, 어노테이션 stripping, balanced-brace walker). `_METHOD_SIG_RE`는 `@ResponseBody` 등 inline annotation 허용, inner class/enum은 outer에서 분리, `_strip_comments`는 offset-preserving. |
| `oracle_embeddings/legacy_analyzer.py` | 통합 오케스트레이터. Controller→Service→XML→Table 체인, `_resolve_endpoint_chain` method-scope resolution + class-scope fallback, 단일/배치 모드. |
| `oracle_embeddings/legacy_frontend.py` | 프론트엔드 React vs Polymer 자동 감지 (`package.json` 의존성 + 콘텐츠 샘플링) + 디스패처. |
| `oracle_embeddings/legacy_react_router.py` | React Router v5/v6/lazy import + 객체형 route 파서. |
| `oracle_embeddings/legacy_polymer_router.py` | Polymer 파서. vaadin-router `setRoutes`, page.js+iron-pages, `<app-route>`, `<dom-module id>`, `Polymer({is})`, `customElements.define`, 파일명 컨벤션. |
| `oracle_embeddings/legacy_menu_loader.py` | 메뉴 소스 로더. DB 테이블 (`load_menu_hierarchy`) / Excel 1~5레벨 (`load_menu_from_excel`) / **Markdown 테이블 (`load_menu_from_markdown`, DRM 우회용)**. |
| `oracle_embeddings/legacy_report.py` | Markdown + Excel 리포트 (단일/배치, 7시트). **메뉴 유무에 따라 컬럼 동적 선택** (있으면 Menu path 먼저, 없으면 Program 먼저). `--menu-only` 플래그로 매칭된 것만 Program Detail에 표시. |
| `oracle_embeddings/legacy_util.py` | 공유 유틸 (`normalize_url` 등). |
| `oracle_embeddings/legacy_pattern_discovery.py` | **LLM 기반 프로젝트 패턴 추출**. stereotype별 샘플 ~40개 클래스 요약 → `patterns.yaml` 생성. 코드 특화 모델 별도 env (`PATTERN_LLM_*`) 지원. |
| `oracle_embeddings/mybatis_parser.py` | MyBatis/iBatis 파싱. `parse_all_mappers` → namespace/statement 4종 인덱스, Oracle comma-FROM + `(+)` outer join, composite JOIN. `scan_mybatis_dir`는 `.git/.gradle/.idea/.svn/.hg/.next/node_modules`만 스킵 (빌드 산출물명 `target/build/bin/out/dist`는 monorepo에서 실제 프로젝트일 수 있어 스킵 안 함 — `_is_sql_mapper`가 false positive 걸러줌). namespace 변수 2-pass 해석 (`sqlSession.selectList(namespace + "findXxx", ...)`). |

## 주요 모듈 (SQL Migration 파이프라인 — `oracle_embeddings/migration/`)

스펙: `docs/migration/spec.md`. 3-tier 변환 (DSL → LLM → 수동) + 2-stage 검증
(Stage A sqlglot / Stage B DBMS parse). sqlglot + lxml 의존성.

| 파일 | 역할 |
|------|------|
| `mapping_model.py` | dataclass 정의 (ColumnRef/SplitTarget/TableMapping/ColumnMapping/TransformSpec/MappingOptions/Mapping/ChangeItem/RewriteResult) + LoaderError. |
| `mapping_loader.py` | `column_mapping.yaml` 로드 + 다중 에러 collect 후 LoaderErrorGroup. location 경로 포함 (예: `columns[3].transform.read`). sqlglot.parse_one 으로 transform 표현식 검증. |
| `impact_analyzer.py` | **migration-impact** 커맨드 — 매핑 파일 × AS-IS 쿼리 영향 리포트 (5 시트: Summary/Table Impact/Column Impact/Affected Statements/Validation). `load_schema_tables` 공유 유틸. |
| `mapping_converter.py` | **convert-mapping** 커맨드 — 임의 .md 매핑 문서 → column_mapping.yaml. LLM 경로 (`LLM_*` 또는 `PATTERN_LLM_*` env) 에서 kind (rename/type_convert/split/merge/value_map/drop) 자동 분류 + transform 표현식 추론. `--no-llm` 경로는 파이프 테이블 헤더 synonym 매칭 fallback. 결과 YAML 은 곧바로 `mapping_loader.load_mapping_collect` 로 검증 → 에러 리스트 출력. |
| `sql_rewriter.py` | `rewrite_sql(sql, mapping)` → SqlRewriteOutcome. sqlglot AST → 8개 transformer 순차 적용 → re-emit. `mask_mybatis_placeholders` / `unmask_mybatis_placeholders` 로 `#{x}`/`${x}` parse 회피 (공개 API, validator_static/comment_injector 가 재사용). DEFAULT_PIPELINE = [TableRename, ColumnRename, ColumnSplit, ColumnMerge, TypeConversion, ValueMapping, JoinPathRewriter, DroppedColumnChecker]. |
| `transformers/base.py` | Transformer ABC + TransformerResult + RewriteContext. `build_alias_map` (alias/table_name → AS-IS table upper) 는 TableRename 후에도 AS-IS 역해석 가능. |
| `transformers/table_rename.py` | rename kind 테이블 노드 교체 + 같은 이름 qualifier 를 쓰는 Column 도 업데이트. split/merge/drop 은 needs_llm. |
| `transformers/column_rename.py` | exp.Column 순회 (Pass A) + INSERT column list 의 exp.Schema.expressions 내 exp.Identifier (Pass B). rename kind 만 처리. |
| `transformers/type_conversion.py` | `transform.read/write/where` 템플릿을 컨텍스트별로 적용 — WHERE/JOIN ON → where, UPDATE SET LHS/INSERT col list → write, 나머지 → read. |
| `transformers/value_mapping.py` | 컬럼 rename + EQ/NEQ/IN 에서 인접 리터럴 value_map 치환. boolean/number/string 리터럴 타입 보존. |
| `transformers/column_split.py` | SELECT projection 에서만 `reverse` 표현식 치환. 다른 컨텍스트는 needs_llm. |
| `transformers/column_merge.py` | MVP 는 flag-only needs_llm. |
| `transformers/join_path_rewriter.py` | split/merge 테이블 JOIN 감지 시 needs_llm (experimental). |
| `transformers/dropped_column_checker.py` | kind=drop 컬럼 참조 감지 → warning (tree 수정 없음 → AUTO_WARN). |
| `dynamic_sql_expander.py` | MyBatis 동적 태그 (`<if>/<choose>/<where>/<set>/<trim>/<foreach>/<include>/<bind>`) → 정적 SQL 경로 전개. Level 1 (max/min), Level 2 (choose alternatives), Level 3 (foreach n=2). `build_sql_includes(root)` + `expand_paths(stmt, sql_includes, max_paths, level)` 공개 API. |
| `xml_rewriter.py` | MyBatis mapper XML 단위 통합 변환. `rewrite_xml(path, mapping)` → 각 statement 최대경로 전개 → rewrite_sql → 전체 트리에 word-boundary 치환 (type_wrap/value/split 같은 복합 변환은 text substitution 범위 밖, 메타데이터 블록에만 기록). `annotate_statements` 로 MIGRATION 메타 블록 + AS-IS 주석 삽입. `serialize_tree` 로 pretty_print=False 저장. |
| `bind_dummifier.py` | validator_db 전처리. `#{x}` → `:x` (Oracle bind), `${x}` → `DUMMY_IDENT`. jdbcType 힌트 strip, nested path 의 첫 token 사용. |
| `validator_static.py` | Stage A — sqlglot parse + Table/Column 스키마 존재 확인. CTE/pseudocol (ROWNUM/SYSDATE/USER/DUAL) 화이트리스트. qualifier 해석 실패/ambiguous → warning. |
| `validator_db.py` | Stage B — oracledb `cursor.parse()` (lazy import). `validate_db_batch(items, dsn, user, password, parallel)` 는 워커당 conn 1개 병렬, caller 순서대로 재정렬. `write_validation_report` 로 2 시트 xlsx. |
| `comment_injector.py` | `inject_comments(sql, ko_lookup, scopes)` — sqlglot `Column.add_comments()` 로 한글 주석 부착. scope 분류기 (select/update/insert/where/join). INSERT 헤더는 exp.Identifier → exp.Column 으로 swap 후 주석. |
| `llm_fallback.py` | `llm_rewrite(as_is_sql, mapping, partial_outcome, config)` — needs_llm 상태 statement 를 OpenAI 호환 엔드포인트로 JSON 변환. relevant mapping 만 프롬프트에 포함 (token 억제). confidence < 0.7 또는 needs_human_review=true → UNRESOLVED. |
| `migration_report.py` | `write_migration_report(results, mapping, output_path, ...)` — 5 시트 Excel (Summary / Conversions 18컬럼 / Validation Errors / Unresolved Queue / Mapping Coverage). 상태별 하이라이트 (빨강=UNRESOLVED/PARSE_FAIL/Stage B 실패, 노랑=AUTO_WARN/NEEDS_LLM, 회색=AUTO 변경없음). |

**매핑 템플릿 / 산출물**:
- `input/column_mapping_template.yaml`: 스펙 §4 의 7 예제 (rename/type/split/merge/value_map/drop/split-discriminator) 그대로
- `output/sql_migration/converted/<rel>.xml`: 변환 XML (구조 보존)
- `output/sql_migration/sql_migration_TIMESTAMP.xlsx`: 5 시트 리포트
- `output/sql_migration/impact_report_TIMESTAMP.xlsx`: 사전 영향분석 리포트
- `output/sql_migration/validation_report_TIMESTAMP.xlsx`: Stage B 리포트

## 공유 유틸 (중복 구현 금지)

- `mybatis_parser._read_file_safe(path, limit=None)` — utf-8 → euc-kr → cp949 순서 fallback. 한국어 레거시 코드는 반드시 이걸로 읽을 것.
- `legacy_util.normalize_url(url)` — 메뉴/Controller/React/Polymer URL 키 정규화. 후행슬래시 제거, 소문자, `:id`/`{id}`/`*` → `{p}`. 매칭 소스가 4개이므로 여기서만 관리.
- `mybatis_parser.parse_all_mappers(dir)` — 반환 dict에 `namespace_to_tables`, `namespace_to_xml_files`, `statement_to_tables`, `statement_to_xml_file` 포함. method-scope resolution은 statement_to_*를 사용.
- `mybatis_parser.extract_table_usage([stmt])` — 단일 statement에서 테이블 추출.
- `legacy_java_parser._strip_annotations_balanced`, `_strip_comments` (offset-preserving) — 메서드 body 추출 시 offset drift 방지.
- `legacy_java_parser.apply_patterns(patterns)` — patterns.yaml 로드 후 파서에 주입 (커스텀 base class, RFC call 메서드, SQL receivers 등 동적 regex 재빌드).

## 출력 / 입력 경로 규약

| 종류 | 경로 | 비고 |
|------|------|------|
| 일반 리포트 | `output/` | schema, query, erd, terms, standardize 등 |
| 레거시 분석서 | `output/legacy_analysis/` | `as_is_analysis_<slug>_TIMESTAMP.{md,xlsx}`. 단일 모드는 backend 디렉토리 이름이 slug, 배치 모드는 `batch` |
| 패턴 파일 | `output/legacy_analysis/patterns.yaml` | `discover-patterns` 생성, `analyze-legacy --patterns`로 주입 |
| 입력 템플릿 | `input/` | `menu_template.xlsx` (1~5레벨 + URL + README 시트), `menu_template.md` (DRM 우회용) |
| 설정 | `config.yaml` | DB 연결, LLM 엔드포인트, `legacy.menu.*` 테이블 매핑, `legacy.rfc_depth` |
| 벡터 DB | `vectordb/` | ChromaDB (erd-rag용) |

## 지원 매트릭스 (변경 시 회귀 확인 필수)

- **Backend framework**: Spring `@Controller`/`@RestController` / Vert.x `AbstractVerticle` / 사용자 정의 `@RestVerticle` / **Nexcore (SK C&C)** `Abstract*BizController`. `pom.xml`/`build.gradle` 의존성 + 소스 휴리스틱으로 자동 감지. Nexcore는 `@RequestMapping` 없이 메서드명 컨벤션으로 endpoint 매핑 (`getList` → `/getList.do`, URL 접미사 커스터마이즈 가능).
- **Frontend framework**: React (Router v5/v6/lazy) / Polymer (vaadin-router, page.js + iron-pages, Polymer 1/2/3, LitElement). `package.json` 의존성 + 콘텐츠 샘플링으로 자동 감지. `--frontend-framework {auto,react,polymer}` override. **`--frontends-root`로 멀티 레포 지원**.
- **MyBatis/iBatis**: namespace-level + statement-level 테이블 인덱스, CDATA / dynamic `<if>` / `<choose>` / `<foreach>` / `<trim>` / iBatis `<sqlMap>` 포함. Oracle comma-FROM + `(+)` outer join, multi-column composite JOIN. `sqlMapClientTemplate.queryForList("ns.id")` 같은 iBatis 호출 지원. **Namespace 변수 해석** (`sqlSession.selectList(namespace + "findXxx", ...)` 2-pass).
- **SAP JCo RFC**: 기본 `destination.getFunction/getCoFunction/getJCoFunction/getRfcFunction`, `String FN_XXX = "..."` 상수 2-pass 해석, multi-arg 호출. **커스텀 RFC 패턴** (patterns.yaml `rfc_call_methods`): `siteService.execute("IF-GERP-180", paramTI, ZMM_GPS.class)` 같은 형태, 변수 인터페이스 ID도 2-pass 해석.
- **서비스 주입**: `@Autowired`, 생성자 주입, Lombok `@RequiredArgsConstructor` + `private final`, `@Inject` (JSR-330). `*ServiceImpl`/`*Bo`/`*BoImpl`/`*Manager`/`*Facade`/`*Helper`/`*Delegate` 등 Impl 네이밍 fallback.
- **MyBatis Mapper 없는 프로젝트**: `CommonSQL.selectList("namespace.id", ...)` 같은 문자열 기반 SQL 호출 인식, DAO (`*Dao`/`*Repository`) 체인도 Service→DAO→XML 추적.
- **Method-scope resolution**: Controller method body → `field.method()` 추적 → Service method body → `sql_calls`/`rfc_calls`만 귀속. 진단 로그 `Method-scope resolution: N/M endpoints (fallback: K)`. 실패 시 class-scope fallback으로 회귀 방지.
- **메뉴 소스 (4종, 우선순위 순)**: `--skip-menu` > `--menu-md` (Markdown 테이블, DRM 우회용) > `--menu-xlsx` (Excel 1~5레벨 + URL) > DB 테이블 (`config.yaml` `legacy.menu`).
- **리포트 양식 자동 전환**: 메뉴 있음 → `Menu path | Main | Sub | Tab | Program | HTTP | URL | ...` / 메뉴 없음 → `Program | HTTP | URL | File | Controller | ...`. `--menu-only`로 메뉴 매칭된 것만 Program Detail + 체인 해석 자체를 skip (속도 최적화).
- **패턴 주입** (analyze-legacy): `--patterns patterns.yaml` 13슬롯 (`framework_type`, `controller_base_classes`, `controller_annotations`, `endpoint_param_types`, `url_suffix`, `http_method_default`, `sql_receivers`, `sql_operations`, `rfc_patterns`, `rfc_call_methods`, `service_suffixes`, `dao_suffixes`, `di_annotations`). 기본값에 합집합으로 추가되어 하위 호환.
- **한국어 레거시 코드**: utf-8 / euc-kr / cp949 인코딩 fallback (`_read_file_safe`).
- **회귀 테스트 자산** (개발 컨테이너): `/tmp/mock_method_spring`, `/tmp/mock_commonsql`, `/tmp/mock_spring_real`, `/tmp/mock_vertx`, `/tmp/mock_restvert`, `/tmp/mock_method_scope`, `/tmp/mock_polymer`, `/tmp/mock_react`, `/tmp/mock_nexcore`, `/tmp/mock_heavy_doc`, `/tmp/menu.xlsx`, `/tmp/menu.md`. 주요 분석 로직 변경 시 최소 8개 backend mock을 모두 실행해 endpoint / matched / method-scope 수치 확인.

## 환경 설정 (Windows + 폐쇄망)

### .env (핵심 값, 실제 값은 개인 환경에)
```bash
# Oracle AS-IS (11g → thick mode 필수). schema/analyze-legacy/migration-impact 사용
ORACLE_USER=<user>
ORACLE_PASSWORD=<password>
ORACLE_DSN=host:1521/service
ORACLE_SCHEMA_OWNER=<owner>
ORACLE_INSTANT_CLIENT_DIR=C:/oracle/instantclient_19_25

# Oracle TO-BE (validate-migration 의 Stage B parse 검증). 미설정 시 위 ORACLE_* 로 fallback
ORACLE_TOBE_DSN=newhost:1521/NEWDB
ORACLE_TOBE_USER=<tobe_user>
ORACLE_TOBE_PASSWORD=<tobe_password>

# 일반 LLM (terms, enrich-schema, standardize 등)
LLM_API_BASE=<사내 LLM 게이트웨이>/v1
LLM_API_KEY=<key>
LLM_MODEL=<대형 범용 모델>

# 임베딩 (erd-rag, vectordb)
EMBEDDING_API_BASE=<사내 LLM 게이트웨이>/v1
EMBEDDING_API_KEY=<key>
EMBEDDING_MODEL=Qwen3-Embedding-8B

# 패턴 발견 전용 (discover-patterns, 코딩 특화 모델 권장)
PATTERN_LLM_MODEL=qwen2.5-coder:14b
# PATTERN_LLM_* 미설정 시 LLM_* 로 fallback
```

### Windows 실행 주의사항
- **줄바꿈**: 명령어에 `\` 말고 `^` 쓰거나 한 줄로. em-dash(`—`)는 PowerShell이 단항 연산자로 오해함
- **pip**: `pip` 대신 `python -m pip` 또는 `py -m pip`
- **DRM**: Excel에 DRM 걸리면 `--menu-md input/menu_template.md` 로 대체

## 추천 워크플로우 (7단계)

```bash
# 1. 스키마 추출 (Oracle 접속)
python main.py schema

# 2. 쿼리 분석 (MyBatis XML)
python main.py query /path/to/mapper --schema-md ./output/스키마.md

# 3. 스키마 보강 (LLM로 빈 코멘트 채움)
python main.py enrich-schema --schema-md ./output/스키마.md

# 4. 용어사전 생성 (스키마 + React 소스, Definition 포함)
python main.py terms --schema-md ./output/스키마_enriched.md --react-dir ./src

# 5. ERD 그룹별 생성 (업무 도메인 단위 분리)
python main.py erd-group --schema-md ./output/스키마_enriched.md --query-md ./output/query_xxx.md

# 6. 프로젝트 패턴 발견 (LLM, 프로젝트당 1회)
python main.py discover-patterns --backend-dir /workspace/backend/<project-one>

# 7. AS-IS 소스 분석 (패턴 + 메뉴 기반, 멀티 레포)
python main.py analyze-legacy \
  --backends-root /workspace/backend \
  --frontends-root /workspace/frontend \
  --menu-md input/menu.md \
  --patterns output/legacy_analysis/patterns.yaml \
  --menu-only
```

## 해결된 주요 이슈 (재발 방지용)

| 증상 | 원인 | 해결 |
|------|------|------|
| JOIN 관계 0개 추출 | `continue` 아래 `results.append`가 unreachable (들여쓰기 실수) | code review 수정 |
| Oracle `(+)` 쿼리에서 0 joins | comma-FROM alias 미등록 + `(+)` 미지원 | `_parse_joins_from_sql` 확장 |
| Composite JOIN 컬럼 한 개만 표시 | renderer에서 table-pair dedupe 후 첫 컬럼만 사용 | pair bucket + 컬럼 합치기 |
| 같은 Service 쓰는 여러 Controller가 RFC/Table 공유 | 클래스 단위 union aggregation | method-scope `_resolve_endpoint_chain` |
| Inner class method가 outer로 새어나감 | nested class skip 로직 없음 | `_NESTED_TYPE_DECL_RE` + brace walker |
| Javadoc `{@link}`가 class brace로 오인 | `_strip_comments`가 offset 파괴 | offset-preserving strip |
| XML 2560개 → 142개로 급감 | scan_mybatis_dir가 target/build/bin/out/dist 스킵 | skip 리스트 축소 |
| Nexcore endpoint 0개 | `@RequestMapping` 없이 `Abstract*BizController` 상속 | 상속 기반 Nexcore 감지 |
| 메서드 필드만 있고 값이 비어있음 | `_METHOD_SIG_RE`가 `public @ResponseBody List` 매칭 실패 | inline annotation 허용 |
| `--menu-only`가 시간 절약 안 됨 | 전체 분석 후 필터링 방식 | 체인 해석 자체 skip |
| namespace 변수 SQL 호출 미추출 | 리터럴만 잡는 regex | 상수 2-pass 해석 |
| 변수 RFC 인터페이스 ID 미추출 | 커스텀 RFC는 리터럴만 | 변수 regex + 2-pass 해석 |

## 미해결 / 다음 작업

### 🔴 진행 중
**전체 URL로 소스 추적**: 메뉴에 `http://<도메인>/apps/<app-name>` 같은 **전체 URL**이 있을 때 프론트/백엔드 추적. 현재 `normalize_url`은 path만 비교 → `--url-prefix-strip` 플래그 또는 자동 도메인+앱prefix strip 로직 필요. 구조 확인 후 결정.

### 🟡 보류 (추후 진행)
- 프로그램 설계서 자동 생성 (`gen-design`) — 이전에 보류로 합의
- LLM 활용 확장: 마이그레이션 복잡도 라벨링 / Unmatched 분류 / Endpoint 업무 요약
- DA AI Agent 로드맵: 네이밍룰 검증 엔진 → DDL 생성 보조 → 표준 위반 자동 감지 (일부 이미 구현됨: `validate-naming`, `gen-ddl`, `audit-standards`)

## 커밋 규칙

- **Conventional Commits** 사용: `feat(scope):`, `fix(scope):`, `docs(scope):`, `refactor(scope):`, `chore(scope):` 형식. scope는 `legacy`, `erd`, `query`, `mybatis`, `terms`, `blog` 등 모듈/도메인 단어.
- 제목 50~70자, 본문은 **왜** 중심 + **검증 방법** 요약. 변경 목록은 bullet로.
- 멀티라인 메시지는 항상 `git commit -m "$(cat <<'EOF' ... EOF)"` HEREDOC 형식으로 작성해 마크다운/개행 깨짐 방지.
- `--no-verify`, `--amend`는 사용자가 명시적으로 요청한 경우에만.
- 커밋 후 반드시 `git push -u origin <branch>`로 원격에 반영. 네트워크 실패 시에만 재시도.
- 회귀 테스트 mock 최소 6~8개 돌려서 method-scope / endpoint 수치 확인 후 커밋.

## 코딩 스타일 관행

- CLI help 메시지와 로그는 **영문 + 한글 혼용**이 기본. 사용자가 한국 사용자라 핵심 설명은 한글로.
- 한글 주석은 docstring 본문에 자연스럽게 섞여 있어도 OK. 파일 상단 docstring은 영문 권장.
- 새로운 파서는 항상 기존 패턴 따라가기: `_*_RE` regex 상수 + 함수 단위 추출기 + 상위 레벨 `build_*` / `parse_*` 공개 API.
- 기존 필드를 제거하지 말고 **추가**. 하위 호환 (method-scope resolution 실패 시 class-scope fallback 등) 유지.
- 불필요한 로깅 금지. 진단 로그는 "몇 개 파싱했는지 / 몇 개 매칭했는지 / fallback 얼마" 같은 **count 기반** 요약.
- patterns.yaml 주입 구조: 기본값에 합집합으로 추가, 기본값 제거 금지 (하위 호환).

## 재개 시 체크리스트

새 세션 시작 시:
```bash
cd /path/to/convert
git fetch origin
git checkout claude/push-previous-changes-4P5x8
git pull
git log --oneline -10          # 최근 커밋 확인

# 최신 코드 반영 검증
python -c "from oracle_embeddings.mybatis_parser import _MYBATIS_SKIP_DIRS; print(_MYBATIS_SKIP_DIRS)"
# → {'.git', '.gradle', '.idea', '.svn', '.hg', '.next', 'node_modules'} 가 나와야 최신
```

이전 세션이 400 에러로 중단됐어도 **모든 작업이 커밋·푸시 완료된 상태**이므로 이 CLAUDE.md + `git log` + `TODO.md`만 있으면 끊김 없이 이어갈 수 있음.
