# TODO: 용어사전 자동 생성 기능

## 단어 수집
- [x] terms_collector.py - 스키마 .md에서 테이블/컬럼명 단어 분리 수집
- [x] terms_collector.py - React 소스에서 컴포넌트/변수/함수명 단어 분리 수집
- [x] 수집된 단어 중복 제거 + 빈도 집계 (DB 출현 횟수, FE 출현 횟수)

## LLM 처리
- [x] terms_llm.py - 수집된 단어를 LLM에 전달하여 약어, 영문 Full Name, 한글명 생성
- [x] 확신 없는 단어는 빈칸 처리

## 산출물
- [x] 용어사전 .md 파일 생성
- [x] 용어사전 .xlsx 파일 생성

## CLI
- [x] main.py에 terms 커맨드 추가
- [x] README.md 업데이트

## 마무리
- [ ] Commit and push
