# CLAUDE.md

> 상세 히스토리 / 모듈 가이드 / 해결된 이슈 표 등 장황한 버전은
> [`docs/CLAUDE-archive-2026-04.md`](docs/CLAUDE-archive-2026-04.md) 참고.

## 작업 규칙

1. 모든 작업 시작 전 `TODO.md` 에 할 일 추가. **13개 기능 카테고리 +
   공통/인프라(0번)** 구조 고정 — 상세 규칙은 `TODO.md` 상단. 카테고리
   헤더/순서 건드리지 말고 `### 진행 중: <제목>` 으로만 추가.
2. 항목은 `- [ ]` / `- [x]`. 완료 시 즉시 체크.
3. PR 머지 후 "진행 중" 섹션 삭제. 히스토리는 git log + PR 이 담당.
4. **항상 `karpathy-guidelines` 스킬 사용**. 코드 작성 / 리뷰 / 리팩토링
   시작 전 `Skill` 도구로 `karpathy-guidelines` 호출 → 4가지 원칙
   (Think Before Coding / Simplicity First / Surgical Changes /
   Goal-Driven Execution) 적용.

## 프로젝트 컨텍스트

- Oracle 레거시 DB 스키마 + MyBatis/iBatis 쿼리 + AS-IS 소스코드 통합 분석 도구
- 폐쇄망 환경, 로컬 LLM (Ollama / vLLM / 사내 LLM 게이트웨이)
- Windows PC, Oracle 11g (thick mode), Python CLI
- 레포: `github.com/mose09/convert`
- **브랜치 전략**: GitHub Flow — `main` 안정. `claude/<task>-<id>` 피처
  브랜치 → PR squash-merge.

## 주요 커맨드 (22종)

| 커맨드 | 목적 | LLM | Oracle |
|--------|------|-----|--------|
| `schema` | 테이블/컬럼/PK/FK/Index → Markdown | X | O |
| `query` | MyBatis/iBatis XML → JOIN 관계 + Table Usage | X | X |
| `enrich-schema` | 빈 코멘트에 LLM 한글 설명 | O | X |
| `erd` / `erd-md` / `erd-group` / `erd-rag` | Mermaid + 인터랙티브 HTML ERD | 선택 | X |
| `terms` | 스키마 + React 소스에서 용어사전 | O | X |
| `morpheme` | 속성명 형태소분석 | O | X |
| `standardize` | 표준화 리포트 (8섹션) | 선택 | 선택 |
| `review-sql` | SQL 안티패턴 + LLM 개선안 | 선택 | X |
| `validate-naming` | DDL/이름 표준 준수 검증 | X | X |
| `gen-ddl` | 자연어 → 표준 CREATE TABLE | O | 선택 |
| `audit-standards` | 기존 스키마 전수 감사 | X | X |
| `analyze-legacy` | **AS-IS 소스 분석 핵심** — Controller→Service→XML→Table→RFC 체인 + biz logic LLM 추출 | 선택 | 선택 |
| `discover-patterns` | LLM 프로젝트 구조 자동 추출 → patterns.yaml | O | X |
| `convert-mapping` | AS-IS↔TO-BE .md → column_mapping.yaml | 선택 | X |
| `migration-impact` | SQL Migration 사전 영향분석 | X | X |
| `migrate-sql` | MyBatis XML 일괄 변환 + 5시트 리포트 | 선택 | X |
| `validate-migration` | 변환 XML parse-only 검증 (Stage B) | X | O |

각 커맨드 상세 옵션: `python main.py <cmd> --help`. 모듈 / 파이프라인
구조 / 회귀 mock / 해결된 이슈 표는 archive 참고.

## 출력 / 입력 경로

`output/<영역>/<YYYYMMDD>/<파일>` (영역 = `schema`/`query`/`erd`/...).
`input/` 은 `convert-mapping` / `convert-menu` 산출물 (다음 커맨드 입력).
설정: `config.yaml`, 벡터 DB: `vectordb/`.

## 환경 설정

### `.env` 핵심
```bash
# Oracle AS-IS (thick mode)
ORACLE_USER / ORACLE_PASSWORD / ORACLE_DSN / ORACLE_SCHEMA_OWNER
ORACLE_INSTANT_CLIENT_DIR=C:/oracle/instantclient_19_25
# Oracle TO-BE (validate-migration Stage B; 미설정 시 AS-IS 로 fallback)
ORACLE_TOBE_DSN / ORACLE_TOBE_USER / ORACLE_TOBE_PASSWORD
# 일반 LLM (terms/enrich-schema/standardize/...)
LLM_API_BASE / LLM_API_KEY / LLM_MODEL
# 임베딩 (erd-rag)
EMBEDDING_API_BASE / EMBEDDING_API_KEY / EMBEDDING_MODEL
# 패턴 발견 + biz extraction 전용 (코딩 특화 모델 권장)
PATTERN_LLM_MODEL=qwen2.5-coder:14b
```

### tree-sitter (옵트인, `--closure-llm`)

`analyze-legacy --closure-llm` 으로 React closure 기반 LLM 분석 활성 시
`tree-sitter` + `tree-sitter-javascript` + `tree-sitter-typescript` 필요.
**미설치여도 기본 동작 (regex 기반) 그대로 작동** — closure 옵션만 비활성.

폐쇄망 wheel install:
```powershell
pip download tree-sitter tree-sitter-javascript tree-sitter-typescript ^
  -d .\wheels --platform win_amd64 --python-version 311 --only-binary=:all:
python -m pip install --no-index --find-links=.\wheels ^
  tree-sitter tree-sitter-javascript tree-sitter-typescript
```

### Windows 실행 주의
- 명령 multi-line 시 `\` 대신 `^` 또는 한 줄로 (PowerShell 의 backtick `` ` `` 도 OK 단 line 끝 공백/탭 없어야 함)
- `pip` 대신 `python -m pip` / `py -m pip`
- DRM 걸린 Excel 은 `--menu-md` 로 우회

### ⚠ 사용자 환경 — 단방향 전송만 (복붙 ⇄ 불가)

사용자 PC 는 **git pull / 파일 다운로드는 되지만 결과를 Claude 세션
으로 다시 올릴 수 없음** (터미널 복사 / 스크린샷 / 첨부 차단). 유일
채널은 **수기 타이핑**. 따라서:

- **진단 / 디버깅 도구는 출력량 최소화**. 긴 섹션 6~7개 dump 금지.
  자동 판정 1~2줄 결론 emit.
- **사용자에게 "출력 전체 붙여달라" 요청 금지**. "조건 A/B/C 중 어느
  쪽?" 처럼 **선택지 형태** 로 질문.
- 진단 스크립트는 결론 한 줄 (✓/⚠/✗ + 다음 액션) 먼저, 상세는
  접거나 옵트인 플래그.
- 반복 진단은 **스크립트 자체가 자동 분류** 해서 결과만 알려주는 구조.
- output 디렉토리 산출물 (xlsx/md) 은 사용자가 로컬에서 확인 — OK.

## 추천 워크플로우 (7단계)

```bash
# 1. 스키마 추출 (Oracle)
python main.py schema

# 2. 쿼리 분석 (MyBatis XML)
python main.py query /path/to/mapper --schema-md ./output/스키마.md

# 3. 스키마 코멘트 보강 (LLM)
python main.py enrich-schema --schema-md ./output/스키마.md

# 4. 용어사전
python main.py terms --schema-md ./output/스키마_enriched.md --react-dir ./src

# 5. ERD 그룹별
python main.py erd-group --schema-md ./output/스키마_enriched.md \
  --query-md ./output/query_xxx.md

# 6. 프로젝트 패턴 발견 (LLM, 1회)
python main.py discover-patterns --backend-dir /workspace/backend/<one-project>

# 7. AS-IS 분석 (멀티 레포 + biz + sequence)
python main.py analyze-legacy \
  --backends-root /workspace/backend \
  --frontends-root /workspace/frontend \
  --library-dir /workspace/backend/common \
  --menu-md input/menu.md \
  --patterns output/legacy_analysis/patterns.yaml \
  --menu-only \
  --extract-biz-logic \
  --sequence-diagram --sequence-diagram-frontend \
  --sequence-diagram-group program_name
```

## 커밋 규칙

- **Conventional Commits**: `feat(scope):` / `fix(scope):` / `docs(scope):`
  / `refactor(scope):` / `chore(scope):`. scope = `legacy`/`erd`/`query`/...
- 제목 50~70자, 본문은 **왜** + **검증 방법** 요약. 멀티라인 메시지는
  HEREDOC `git commit -m "$(cat <<'EOF' ... EOF)"`.
- `--no-verify` / `--amend` 는 사용자가 명시적으로 요청한 경우만.
- 직푸시 금지. **PR squash-merge 만**. Auto-delete head branch on.

## 다중 에이전트 동시 작업

`main` 이 빠르게 갱신됨 → push 직전 **반드시 rebase**:

```bash
git fetch origin main
git rebase origin/main
# 충돌 시 해결 → git add → git rebase --continue
git push -u origin claude/<task>     # 첫 push
# 이미 push 된 브랜치에 rebase 덮어쓰기:
git push --force-with-lease           # 절대 --force 단독 금지
```

원칙:
- **rebase > merge** (히스토리 깨끗)
- **`--force-with-lease` 만** (다른 에이전트 commit 덮어쓰기 사고 방지)
- **main 직푸시 금지** — 모든 변경은 PR → squash-merge
- TODO.md 는 본인 카테고리만 수정 (충돌 회피)
- PR not-mergeable 시: `git rebase origin/main` → 충돌 해결 →
  `git push --force-with-lease` → PR 재머지

## 재개 시 체크리스트

```bash
git fetch origin --prune
git checkout main && git pull origin main
git log --oneline -10                  # 최근 커밋
git checkout -b claude/<task>-<id>     # 새 작업
```

세션 종료 시:
```bash
git push -u origin claude/<task>-<id>
# Claude 가 GitHub MCP 로 PR 생성 + squash-merge
git fetch origin --prune
git checkout main && git pull
git branch -D claude/<task>-<id>       # squash 는 -D 필요
```

## 코딩 스타일

- CLI / 로그 메시지: 한글 + 영문 혼용 (사용자가 한국어).
- 새 파서: 기존 패턴 따라가기 — `_*_RE` regex 상수 + 함수 추출기 +
  공개 `build_*` / `parse_*`.
- 기존 필드는 **추가**. 제거 금지 (하위 호환).
- patterns.yaml 주입: 기본값에 합집합으로 추가, 기본값 제거 금지.
- 진단 로그는 count 기반 요약 (어디까지 작동했는지 시각화).
