# TODO: DDL 생성 보조 기능

## 핵심 기능
- [x] ddl_generator.py - 자연어 → DDL 변환
- [x] LLM에 컨텍스트 제공 (용어사전 + 기존 스키마 샘플)
- [x] 테이블명/컬럼명 표준 적용
- [x] 네이밍 검증 자동 수행
- [x] DDL 생성 후 검증 결과 표시
- [x] 사용자 컨펌 플로우 (--execute)

## 산출물
- [x] DDL 파일 생성 (output/ddl_TABLE_TIMESTAMP.sql)
- [x] 검증 리포트 동시 출력

## CLI
- [x] main.py에 gen-ddl 커맨드 추가
- [x] README.md 업데이트

## 마무리
- [x] Commit and push
