# Session Recap — 2026-05-18 (`hypm-interlockrule` 매핑 시도)

> 9개 PR (#214 ~ #222) 머지 후 매핑이 엉망이 되어 #223 으로 **전부 원복**.
> 같은 문제 다음 시도 시 같은 함정 다시 안 빠지도록 정리.

## 1. 배경

- 사용자 환경: SK Hynix workplace (Windows PC, Oracle 11g, Python CLI, 폐쇄망)
- 보고: `analyze-legacy` 결과 Excel 에서 **`hypm-interlockrule` row 가 빈 채로 남음**
- 시작 시점: PR #213 (a63dbe2) 머지 직후

## 2. 핵심 환경 정보 (뒤늦게 다 드러남)

| 항목 | 값 |
|------|-----|
| React 소스 import | `import App from 'apps/hypm_interlockRule'` (underscore + camelCase) |
| React routes 파일 | `<Route exact path={getRoutePath(basename, '/')} component={App}/>` (path 가 **동적 함수 호출**) |
| 메뉴 URL (menu_md) | `https://workplace.skhynix.com/apps/hypm_interlockrule` (**도메인 host 포함 + slug underscore + 모두 소문자**) |
| `.env REACT_APP_NAME` | `gipms-interlockrule` (deploy slug, 별 용도) |
| patterns.yaml `url_prefix_strip` | `^/apps` 한 줄만 |

즉 **세 곳에서 표기가 모두 다름** — 진짜 truth source 가 어디인지 불명확.

## 3. 시도한 PR 들 (#214 ~ #222)

| # | 변경 | 결과 |
|---|------|------|
| #214 | `import 'apps/<slug>'` 라인에서 slug 추출 → `/apps/<dash-slug>` 형태로 url_map alias 자동 등록 | 사용자 환경 매핑 실패 |
| #215 | trigger label 추출 시 self-closing JSX 의 `}` `/>;` raw chunk reject (영문/한글 글자 1개 이상 조건) | 매핑과 무관, 독립 fix (원복 시 함께 제거) |
| #216 | alias 등록 step 을 `has_route_keyword` 검사 **앞**으로 이동 (동적 path Route 환경 대응) | 사용자 환경 매핑 실패 |
| #217~#221 | 진단 스크립트 `diagnose_url_map.py` 단계별 보강 (raw repr / finditer count / 소스 검증 / `--strip` 옵션 등) | 디버깅 도구 |
| #222 | alias 등록 시 dash + underscore 두 variant 양방향 등록 | 사용자 환경 매핑 실패 (오히려 엉망 매핑 야기 추정) |
| **#223** | **#214~#222 9개 PR 전부 revert** | 원복 완료 |

## 4. 왜 엉망 매핑이 일어났나 (가설)

PR #214 의 동작: 모든 React 파일에서 `import ... from 'apps/<slug>'` 패턴을 찾아 `/apps/<slug>` 를 url_map 의 base 로 자동 등록.

문제:
- SK Hynix monorepo 처럼 `apps/` 폴더 구조에 sub-app 이 수십~수백 개인 환경
- 다양한 wrapper / Layout / utility 파일에서도 `apps/...` import 가 흔함
- 모든 import 가 alias 로 등록 → **url_map base 폭증**
- PR #222 의 underscore + dash 양방향까지 더해져 alias 가 2배로 등록 → **noise 더 커짐**
- 사용자 patterns.yaml 의 `^/apps` strip 까지 적용되면 alias key 들이 짧은 형태 (`/hypm-interlockrule` 등) 로 정규화 → **다른 메뉴 row 와 잘못 매칭** 가능

즉 한 row (`hypm-interlockrule`) 채우려고 한 fix 가 **다른 row 들의 매칭을 망가뜨림**.

## 5. 디버깅 과정의 함정들

1. **사용자가 처음 알려준 정보가 정확하지 않았음** — "메뉴 URL = `/apps/hypm-interlockrule`" 이라 했지만 실제는 `https://workplace.skhynix.com/apps/hypm_interlockrule`. 단방향 환경이라 raw 데이터 확인 어려움
2. **진단 결과의 transcribe 정확성 의존** — 사용자가 결과 일부만 적거나 라인 헷갈리는 경우 정확한 분기 어려움
3. **regex 매칭 디테일에 너무 깊이 들어감** — `_APP_IMPORT_RE` vs `_apps_import_aliases` 결과 모순 의심으로 4회 PR 보강. 정작 root cause 는 **메뉴 URL 의 host prefix + slug 표기 차이** 였음
4. **`url_prefix_strip` 같은 사용자 설정의 영향** — 진단 스크립트가 `strip_patterns=None` 이라 실제 `analyze-legacy` 환경과 달랐음. PR #221 에서야 `--strip` 옵션 추가

## 6. 최종 상태

- 코드: `8463b4b → 1b9d8f7` (PR #213 시점과 동일)
- 사용자 측 추가 액션: `patterns.yaml` 의 `^/+workplace\.skhynix\.com` 직접 제거 필요 (PR #222 와 한 쌍)
- `hypm-interlockrule` row 는 **여전히 빈 상태** (PR #214 이전과 동일)

## 7. 다음 시도 시 권장 접근

### 7.1 처음에 명확히 할 것

1. **메뉴 URL 의 raw 형태** — protocol/host 포함 여부
2. **사용자 patterns.yaml 의 url 섹션 전체** — `url_prefix_strip`, `react_route_prefix`, `app_key` 등
3. **소스 import path 의 정확한 형태** — `repr()` 까지

→ 사용자 단방향 환경에서는 **진단 스크립트가 한 번에 모든 형태 raw 출력** 하도록 해야. 사용자에게 transcribe 부담 최소화.

### 7.2 url_map alias 등록보다 안전한 접근

- alias 자동 등록은 **side effect 크다** (한 row 채우려다 다른 row 노이즈)
- 차라리 `program_name` 기반 fuzzy 매칭 등 **다른 차원** 으로 row 채우기
- 또는 사용자에게 `convert-mapping` 같은 명시적 매핑 정의 받기

### 7.3 진단 스크립트 살리기

PR #217~#221 의 `diagnose_url_map.py` 는 매핑 로직 변경 없는 **read-only 진단 도구**. 같이 revert 되어 사라졌지만, 다음 시도 시 같은 형태로 다시 만들 가치 있음. git log 의 PR #221 시점 (`474a698`) 에 최종 버전 남아있음.

## 8. 참고 — 이번 세션 commit 흔적

```
1b9d8f7  revert: 이번 세션 9개 PR 전부 원복 (#223)         ← 최종 (현재 main)
8463b4b  fix(legacy): apps import alias underscore+dash (#222)
474a698  chore(diag): --strip 옵션 추가 (#221)
f55b0ff  chore(diag): _apps_import_aliases 정의 파일 검증 (#220)
1aeb539  chore(diag): finditer 매칭 수 + content repr (#219)
9872007  chore(diag): 코드 버전 검사 + 수동 alias 시뮬레이션 (#218)
2939264  chore(diag): regex miss 원인 + menu URL 전체 추출 (#217)
2199768  fix(legacy): alias 등록 위치 보강 + 진단 스크립트 (#216)
45a8f31  fix(legacy): trigger label self-closing JSX fix (#215)
5e8bb50  feat(legacy): import 'apps/<slug>' alias 자동 등록 (#214)
a63dbe2  fix(legacy): _enumerate_buckets standalone SPA fix (#213)  ← 시작점
```
