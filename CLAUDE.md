# CLAUDE.md

## 작업 규칙

1. 모든 작업 시작 전 `TODO.md` 파일에 할 일 목록을 작성한다.
2. 각 작업 항목은 `- [ ]` (미완료) / `- [x]` (완료) 체크박스를 사용한다.
3. 작업 완료 시 즉시 `TODO.md`에서 해당 항목을 완료 처리한다.
4. 모든 작업이 끝나면 TODO.md를 최종 확인한다.

## 프로젝트 컨텍스트

- Oracle 레거시 DB 스키마 분석 + MyBatis/iBatis 쿼리 분석 도구
- 폐쇄망 환경, 로컬 LLM 사용 (Ollama/vLLM)
- Windows PC에서 실행
- Oracle 11g (thick mode)
- Python CLI 기반

## 주요 모듈 (analyze-legacy 파이프라인)

| 파일 | 역할 |
|------|------|
| `oracle_embeddings/legacy_java_parser.py` | Java 소스 파싱 (메서드 단위 body + body-scope sql/rfc/field call 수집, 어노테이션 stripping, balanced-brace walker) |
| `oracle_embeddings/legacy_analyzer.py` | 통합 오케스트레이터. Controller→Service→XML→Table 체인, `_resolve_endpoint_chain` method-scope resolution + class-scope fallback, 단일/배치 모드 |
| `oracle_embeddings/legacy_frontend.py` | 프론트엔드 React vs Polymer 자동 감지 (`package.json` 의존성 + 콘텐츠 샘플링) + 디스패처 |
| `oracle_embeddings/legacy_react_router.py` | React Router v5/v6/lazy import + 객체형 route 파서 |
| `oracle_embeddings/legacy_polymer_router.py` | Polymer 파서. vaadin-router `setRoutes`, page.js+iron-pages, `<app-route>`, `<dom-module id>`, `Polymer({is})`, `customElements.define`, 파일명 컨벤션 |
| `oracle_embeddings/legacy_menu_loader.py` | 메뉴 소스 로더. DB 테이블 (`load_menu_hierarchy`) 또는 Excel 1~5레벨 (`load_menu_from_excel`) |
| `oracle_embeddings/legacy_report.py` | Markdown + Excel 리포트 (단일 모드 + 배치 모드, 각 7시트) |
| `oracle_embeddings/legacy_util.py` | 공유 유틸 (`normalize_url` 등) |
| `oracle_embeddings/mybatis_parser.py` | MyBatis/iBatis 파싱. `parse_all_mappers` → namespace 인덱스 + statement 인덱스, Oracle comma-FROM + `(+)` outer join 지원 |

## 공유 유틸 (중복 구현 금지)

- `mybatis_parser._read_file_safe(path, limit=None)` — utf-8 → euc-kr → cp949 순서로 fallback 해서 파일 읽기. 한국어 레거시 코드는 반드시 이걸로 읽을 것.
- `legacy_util.normalize_url(url)` — 메뉴/Controller/React/Polymer URL 키 정규화. 후행슬래시 제거, 소문자, `:id`/`{id}`/`*` → `{p}`. 매칭 소스가 4개이므로 여기서만 관리.
- `mybatis_parser.parse_all_mappers(dir)` — 반환 dict 에 `namespace_to_tables`, `namespace_to_xml_files`, `statement_to_tables`, `statement_to_xml_file` 포함. method-scope resolution 은 statement_to_* 를 사용.
- `mybatis_parser.extract_table_usage([stmt])` — 단일 statement 에서 테이블 추출.
- `legacy_java_parser._strip_annotations_balanced`, `_strip_comments` (offset-preserving) — 메서드 body 추출 시 offset drift 방지.

## 출력 / 입력 경로 규약

| 종류 | 경로 | 비고 |
|------|------|------|
| 일반 리포트 | `output/` | schema, query, erd, terms, standardize 등 |
| 레거시 분석서 | `output/legacy_analysis/` | `as_is_analysis_<slug>_TIMESTAMP.{md,xlsx}`. 단일 모드는 backend 디렉토리 이름이 slug, 배치 모드는 `batch` |
| 입력 템플릿 | `input/` | `menu_template.xlsx` (1~5레벨 + URL 헤더 + README 시트) |
| 설정 | `config.yaml` | DB 연결, LLM 엔드포인트, `legacy.menu.*` 테이블 매핑, `legacy.rfc_depth` |

## 지원 매트릭스 (변경 시 회귀 확인 필수)

- **Backend framework**: Spring `@Controller`/`@RestController` / Vert.x `AbstractVerticle` / 사용자 정의 `@RestVerticle`. `pom.xml`/`build.gradle` 의존성 + 소스 휴리스틱으로 자동 감지.
- **Frontend framework**: React (Router v5/v6/lazy) / Polymer (vaadin-router, page.js + iron-pages, Polymer 1/2/3, LitElement). `package.json` 의존성 + 콘텐츠 샘플링으로 자동 감지. `--frontend-framework {auto,react,polymer}` 로 override.
- **MyBatis**: namespace-level + statement-level 테이블 인덱스, CDATA / dynamic `<if>` / `<choose>` / `<foreach>` / `<trim>` / iBatis 포함. Oracle comma-FROM + `(+)` outer join, multi-column composite JOIN.
- **SAP JCo RFC**: `destination.getFunction("Z_...")`, `JCoUtil.getCoFunction(...)`, `String FN_XXX = "..."` 상수 2-pass 해석, multi-arg 호출.
- **서비스 주입**: `@Autowired`, 생성자 주입, Lombok `@RequiredArgsConstructor` + `private final`. `*ServiceImpl`/`*Bo`/`*BoImpl`/`*Manager`/`*Facade` 등 Impl 네이밍 fallback.
- **MyBatis Mapper 없는 프로젝트**: `CommonSQL.selectList("namespace.id", ...)` 같은 문자열 기반 SQL 호출 인식.
- **메뉴 소스**: DB 테이블 (`config.yaml` `legacy.menu`) 또는 Excel (`--menu-xlsx`, 1~5레벨 + URL, `input/menu_template.xlsx` 참조). 우선순위: `--skip-menu` > `--menu-xlsx` > DB.
- **한국어 레거시 코드**: utf-8 / euc-kr / cp949 인코딩 fallback (`_read_file_safe`).
- **회귀 테스트 자산** (개발 컨테이너): `/tmp/mock_method_spring`, `/tmp/mock_commonsql`, `/tmp/mock_spring_real`, `/tmp/mock_vertx`, `/tmp/mock_restvert`, `/tmp/mock_method_scope`, `/tmp/mock_polymer`, `/tmp/mock_react`, `/tmp/menu.xlsx`. 주요 분석 로직 변경 시 최소 6개 backend mock 을 모두 실행해 endpoint / matched / method-scope 수치 확인.

## 커밋 규칙

- **Conventional Commits** 사용: `feat(scope):`, `fix(scope):`, `docs(scope):`, `refactor(scope):`, `chore(scope):` 형식. scope 는 `legacy`, `erd`, `query`, `mybatis`, `terms`, `blog` 등 모듈/도메인 단어.
- 제목 50~70자, 본문은 **왜** 중심 + **검증 방법** 요약. 변경 목록은 bullet 로.
- 멀티라인 메시지는 항상 `git commit -m "$(cat <<'EOF' ... EOF)"` HEREDOC 형식으로 작성해 마크다운/개행 깨짐 방지.
- `--no-verify`, `--amend` 는 사용자가 명시적으로 요청한 경우에만.
- 커밋 후 반드시 `git push -u origin <branch>` 로 원격에 반영. 네트워크 실패 시에만 재시도.

## 코딩 스타일 관행

- CLI help 메시지와 로그는 **영문 + 한글 혼용** 이 기본. 사용자가 한국 사용자라 핵심 설명은 한글로.
- 한글 주석은 docstring 본문에 자연스럽게 섞여 있어도 OK. 파일 상단 docstring 은 영문 권장.
- 새로운 파서는 항상 기존 패턴 따라가기: `_*_RE` regex 상수 + 함수 단위 추출기 + 상위 레벨 `build_*` / `parse_*` 공개 API.
- 기존 필드를 제거하지 말고 **추가**. 하위 호환 (method-scope resolution 실패 시 class-scope fallback 등) 유지.
- 불필요한 로깅 금지. 진단 로그는 "몇 개 파싱했는지 / 몇 개 매칭했는지 / fallback 얼마" 같은 **count 기반** 요약.

