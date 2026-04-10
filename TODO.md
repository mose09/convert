# TODO: SQL 리뷰/최적화 제안 기능

## 정적 분석 (코드 기반)
- [x] sql_reviewer.py - 비효율 패턴 자동 감지
- [x] SELECT * 사용 감지
- [x] NOT IN → NOT EXISTS 권장 패턴
- [x] LIKE '%...%' 풀스캔 패턴
- [x] 카티시안 곱 (JOIN 조건 없는 CROSS JOIN)
- [x] OR 조건 (인덱스 미사용 가능성)
- [x] 서브쿼리 중복
- [x] 불필요한 DISTINCT
- [x] UPPER/LOWER in WHERE (함수 기반 인덱스 필요)

## LLM 분석
- [x] sql_reviewer_llm.py - 복잡한 쿼리 LLM 분석
- [x] 개선 SQL 제안
- [x] 심각도 평가

## 산출물
- [x] sql_review_report.md - 문제 패턴별 목록
- [x] sql_review_report.xlsx - 매퍼/쿼리별 문제점

## CLI
- [x] main.py에 review-sql 커맨드 추가
- [x] README.md 업데이트

## 마무리
- [x] Commit and push
