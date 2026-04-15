# TODO: 메뉴 매핑을 Excel 파일 기반으로 전환 (완료)

- [x] `legacy_menu_loader.py` 에 `load_menu_from_excel` + `_LEVEL_KEYWORDS` (1~5레벨) + `_URL_KEYWORDS` 추가
- [x] `_row_to_entry` 가 가장 깊은 레벨을 `program_name` 으로, 첫 3개를 main/sub/tab 슬롯으로, 전체 레벨을 `menu_path` 로 보존
- [x] `main.py cmd_analyze_legacy` 에 `--menu-xlsx` 옵션 + skip > xlsx > DB 우선순위
- [x] `legacy_analyzer._build_row` row dict 에 `menu_path` 필드 추가
- [x] `legacy_report.py` Markdown / Excel 단일·배치 모드 모두 `Menu path` 컬럼 추가
- [x] `/tmp/menu.xlsx` mock 으로 단위 + end-to-end 검증
- [x] BLOG.md 업데이트 (Excel 옵션 + menu_path 컬럼 설명)
- [x] 커밋 & 푸시

---

# TODO: AS-IS Legacy Source Code Analyzer (완료)

## Phase 1 - 코어 파서
- [x] `legacy_java_parser.py` 신규 (패키지/import/스테레오타입/매핑/autowired/RFC)
- [x] `mybatis_parser.parse_mapper_file` 에 `mapper_path` 필드 추가
- [x] `legacy_analyzer.py` 골격 (컨트롤러→서비스→매퍼→테이블 체인)

## Phase 2 - 메뉴 & URL 양방향 매칭
- [x] `legacy_menu_loader.py` 신규 (DB 메뉴 트리 + URL 인덱스)
- [x] URL 정규화 공유 유틸 (`legacy_util.normalize_url`)
- [x] 양방향 매칭 (matched / unmatched / orphan)

## Phase 3 - React 프레젠테이션 레이어
- [x] `legacy_react_router.py` 신규 (라우트 스캔 + 컴포넌트 인덱스)
- [x] analyzer 에 presentation_layer 연결

## Phase 4 - RFC 추출
- [x] Java parser 의 `_extract_rfc_calls` + 2-pass 상수 해석
- [x] 서비스 체인 트랜지티브 RFC 수집

## Phase 5 - 출력
- [x] `legacy_report.py` 신규 (Markdown)
- [x] Excel 7시트 출력

## CLI 통합
- [x] `main.py` 에 `cmd_analyze_legacy` + 서브커맨드 등록
- [x] `config.yaml` 에 `legacy.menu` 섹션 추가

## 검증
- [x] mock Java/React/XML 디렉토리로 end-to-end 테스트
- [x] 기존 명령(query, erd-group 등) 회귀 없음 확인
- [x] 커밋 & 푸시

---

# TODO: 용어사전 자동 생성에 정의(Definition) 필드 추가 (완료)

## 작업 항목
- [x] terms_llm.py `_enrich_batch` 프롬프트에 정의 규칙/JSON 키 추가
- [x] terms_llm.py `enrich_terms` 응답 매핑에 `definition` 추가
- [x] terms_report.py `_md_escape` 헬퍼 추가
- [x] terms_report.py Markdown 두 테이블(Terminology, DB+FE 공통)에 Definition 컬럼 추가
- [x] terms_report.py Excel 4개 시트(용어사전/DB+FE공통/DB전용/FE전용)에 Definition 컬럼 추가
- [x] 변경 검증 (구문/임포트)
- [x] 커밋 및 푸시

---

# TODO: 버그 수정 (완료)

## Critical 🔴
- [x] Bug #1: mybatis_parser.py:240 - continue 이후 unreachable code로 JOIN 관계 전혀 추출 안 됨

## High 🟠
- [x] Bug #2: terms_collector.py:110 - 기본 dict에 fe_count/db_count 누락
- [x] Bug #3: storage.py:196 - 빈 mappers 리스트 IndexError 가능
- [x] Bug #4: vector_store.py:99 - metadatas/distances 길이 미확인

## Medium 🟡
- [x] Bug #5: sql_reviewer.py:45 - 카티시안 곱 regex 단순화
- [x] Bug #6: sql_reviewer.py:93 - UPDATE/DELETE WHERE 없음 함수 내 특별 처리
- [x] Bug #7: ddl_generator.py:125 - table["columns"] null 체크
- [x] Bug #8: erd_generator.py:53 - data_type None 체크

## Low 🟢
- [x] Bug #9: ddl_generator.py:118 - except 로깅 추가
- [x] Bug #10: erd_generator.py:84 - 중복 할당 제거

## 마무리
- [x] 자체 테스트 (Bug #1, #2, #5, #6, #8)
- [x] Commit and push
