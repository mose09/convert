# TODO: 용어사전 자동 생성에 정의(Definition) 필드 추가

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
