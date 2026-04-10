# TODO: 표준 위반 자동 감지 (audit-standards)

## 핵심 기능
- [x] standards_auditor.py - 기존 스키마 일괄 검사
- [x] 스키마 .md의 모든 테이블/컬럼 → NamingValidator로 검증
- [x] 심각도별 집계
- [x] 테이블별 위반 건수 집계
- [x] 표준 위반 재발 방지용 패턴 분석

## 산출물
- [x] audit_report.md - 전체 위반 요약 + 상세
- [x] audit_report.xlsx - 시트별 분류 (Summary, 테이블, 컬럼, 패턴)
- [x] 수정 권장 테이블/컬럼 매핑 (유사 약어 추천 포함)

## CLI
- [x] main.py에 audit-standards 커맨드 추가
- [x] README.md 업데이트

## 마무리
- [x] 자체 테스트 (Pattern 분류, Severity 집계, 리포트 생성 확인)
- [x] Commit and push
