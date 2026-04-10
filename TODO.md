# TODO: 네이밍룰 검증 엔진

## 핵심 기능
- [x] naming_validator.py - 테이블/컬럼명 검증 엔진
- [x] 용어사전 로드 (terms_dictionary .md/.xlsx)
- [x] 단어 분해 후 각 토큰 검증
- [x] 용어사전에 없는 약어 감지
- [x] 유사 약어 추천 (Levenshtein)
- [x] 길이 제한 체크 (Oracle 30자)
- [x] 대소문자/언더스코어 규칙 검증

## 검증 대상
- [x] 단일 이름 검증 (--name)
- [x] 파일 기반 일괄 검증 (--file)
- [x] DDL 파일 파싱 후 검증 (--ddl)

## 산출물
- [x] 콘솔 출력 (즉시 결과)
- [x] 리포트 .md / .xlsx (대량 검증 시)

## CLI
- [x] main.py에 validate-naming 커맨드 추가
- [x] README.md 업데이트

## 마무리
- [x] Commit and push
