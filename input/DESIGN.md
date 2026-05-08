# React Screen Dependency Closure — 설계 문서

## 1. 목적 & 범위

레거시 React 프로젝트를 **화면 단위로 코드를 묶어서** LLM에 전달하기 위한 모듈.
레포마다 폴더 구조와 import 컨벤션이 제각각이어도 일괄 분석되도록,
import 그래프를 AST 기반으로 따라 들어가 "그 화면을 구성하는 모든 자체 작성 코드"를
한 묶음으로 만든다. 같은 함수가 **팝업도 별도 화면처럼** 처리한다.

### 이 모듈이 하는 것
- 진입점 파일 1개 → import 그래프 BFS로 자식 컴포넌트/훅/유틸/API 클라이언트 수집
- depth별로 상세도 다르게 (full → signature → meta) 강등해 토큰 예산 안에 맞춤
- closure 안에서 발견된 API 호출(method + URL + handler)을 사실로 정리
- closure 안에서 발견된 팝업 ref를 별도로 정리 (그 자체를 다시 entry로 사용 가능)
- LLM 친화적인 Markdown으로 직렬화

### 이 모듈이 하지 않는 것 (의도적)
- 화면 진입점 식별 — **호출자(기존 분석기)가 결정**
- LLM 호출 — 호출자가 `serialize_for_llm` 결과를 자신의 LLM 클라이언트로 전달
- 라우팅 dialect 분석 — 진입점은 외부에서 주어진다는 가정
- DB/백엔드 매핑 — `analyze-legacy`의 다른 모듈이 담당

## 2. 모듈 구성

```
legacy_react_ast.py          tree-sitter wrapper (의존성 — 변경 거의 없음)
legacy_react_closure.py      메인 모듈 (이번 작업 대상)
```

`legacy_react_ast.py`는 인프라성 모듈이라 그대로 통합. `legacy_react_closure.py`가
이번 설계의 핵심이다.

## 3. 입출력 인터페이스

### 3.1 메인 함수

```python
def build_closure(
    entry_file: str | os.PathLike,
    repo_root: str | os.PathLike,
    patterns: Optional[dict] = None,
    *,
    max_depth: int = 3,
    token_budget: int = 12000,
    verbose: bool = False,
) -> ScreenClosure
```

- **entry_file**: 진입점 파일 절대/상대 경로. **화면이든 팝업이든 동일하게 받음.**
- **repo_root**: 레포 루트 (alias resolver, 상대경로 계산 기준).
- **patterns**: `patterns.yaml`의 dict. `react:` 섹션만 사용. 미주입 시 합리적 기본값.
- **max_depth**: BFS 깊이 제한. depth가 이걸 넘으면 큐에 안 넣음 (기본 3).
- **token_budget**: 전체 closure의 추정 토큰 합계 상한 (chars/4 휴리스틱).

### 3.2 직렬화

```python
def serialize_for_llm(closure: ScreenClosure) -> str
```

LLM의 user message로 그대로 사용 가능한 Markdown. 구조:

1. 헤더 (entry 이름/파일)
2. **API calls (factual)** — 사실 우선 배치 (LLM 환각 차단)
3. **Popups invoked from this screen (factual)**
4. 파일별 코드 (depth/mode 메타 + fenced code block)
5. External imports (excluded — node_modules)
6. (필요 시) truncate 알림

### 3.3 데이터 구조

```python
@dataclass
class ScreenClosure:
    entry_file: Path
    entry_name: str               # default export 이름 (없으면 파일명)
    files: list[ClosureFile]
    api_calls: list[ApiCallSite]
    popup_refs: list[PopupRef]    # 별도 build_closure 의 entry 로 재사용 가능
    skipped_external: list[str]   # node_modules 등
    truncated: bool               # 토큰 예산 초과로 강등 발생 여부
    total_tokens: int

@dataclass
class ClosureFile:
    abs_path: Path
    rel_path: str                 # repo_root 기준
    depth: int                    # BFS 깊이
    mode: str                     # 'full' / 'signature' / 'meta'
    content: str                  # mode 별 내용
    exports: list[str]            # default + named
    estimated_tokens: int

@dataclass
class ApiCallSite:
    file: str                     # rel_path
    line: int
    method: str                   # GET/POST/PUT/DELETE/PATCH/FETCH/UNKNOWN
    url: Optional[str]            # template literal 의 ${X} 는 {p} 로 정규화. 동적이면 None
    expr: str                     # 호출 원본 텍스트 (truncated)
    handler: Optional[str]        # 호출이 위치한 함수 이름 (handleApprove 등)

@dataclass
class PopupRef:
    component_name: str
    component_file: Optional[Path]   # build_closure 의 entry 로 재사용 가능
    invoked_from: str
    line: int
    trigger: str                     # 'jsx_inline' / 'use_hook' / 'open_api'
    expr: str
```

## 4. 알고리즘

### 4.1 BFS

```
queue = deque([(entry_file, 0)])
visited = set()
while queue:
    file, depth = queue.popleft()
    if file in visited or depth > max_depth: continue
    visited.add(file)

    1. AST 파싱
    2. depth → mode 매핑으로 ClosureFile 생성
    3. API 호출 추출 (모든 depth의 모든 파일에서)
    4. 팝업 ref 추출 (모든 depth의 모든 파일에서)
    5. import 들 alias resolver 통과 → 로컬 파일이면 큐에 (depth+1)
       → node_modules / 미해결은 skipped_external 로
```

### 4.2 Depth → Mode 매핑 (기본값)

| Depth | Mode | 내용 |
|---|---|---|
| 0 | `full` | 진입점 — 전체 소스 |
| 1 | `full` | 직접 import한 자식 — 전체 소스 |
| 2 | `signature` | 손자 — export 시그니처 + JSX skeleton |
| 3+ | `meta` | export 이름만 |

`patterns.react.closure_depth_mode`로 오버라이드 가능. 예: `{0: full, 1: signature}`로 가면 진입점만 full로.

### 4.3 토큰 예산 강제

토큰 합계가 예산을 넘으면 **가장 깊은 depth의 파일부터** mode 강등 (full→signature→meta).
강등 후에도 초과면 다시 다음 후보를 강등. 더 이상 강등할 게 없으면 멈춤(`truncated=True`).

## 5. 팝업 식별 (3가지 신호)

closure 안의 모든 파일에서 동시 적용.

### 신호 1 — JSX 태그 매칭

태그명이 다음 중 하나에 해당하면 PopupRef 생성:
- `popup.file_suffixes` 중 하나로 끝남 (`OrderDetailModal`, `LoginPopup`, `ConfirmDialog`)
- `popup.jsx_components`에 정확 일치 (`Modal`, `Drawer`, `Dialog` 등 라이브러리)

→ `trigger: 'jsx_inline'`. import에서 component_file 해석 시도.

### 신호 2 — Hook 호출

함수 호출의 함수명이 `popup.open_hooks`에 일치 (`useModal`, `openPopup` 등).
→ `trigger: 'use_hook'`. component_file은 None (hook이라 정적으로 결정 불가).

### 신호 3 — 명시적 Open API

member expression 전체 텍스트가 `popup.open_apis`에 일치 (`ModalManager.open`).
→ `trigger: 'open_api'`. component_file은 None.

### 중복 제거

`(component_name, invoked_from, line, trigger)` 키로 dedupe.

## 6. `patterns.yaml.react` 슬롯

기본값에 합집합으로 추가 (제거 안 함, 하위 호환). 미주입 시 모든 슬롯이 기본값.

```yaml
react:
  api_call:
    wrappers: [apiClient, http, axios, request, api]
    methods:  [get, post, put, delete, patch]

  popup:
    file_suffixes:  [Modal, Popup, Dialog, Layer]
    jsx_components: [Modal, Dialog, Drawer, Popup, Sheet, Layer]
    open_hooks:     [useModal, useDialog, openPopup, useDrawer]
    open_apis:      [ModalManager.open, showDialog, openModal]

  closure_depth_mode:
    0: full
    1: full
    2: signature
    3: meta
```

## 7. 통합 가이드 (호출자 코드 패턴)

```python
# 사용자 기존 분석기에서 화면 진입점들을 결정한 뒤:
from legacy_react_closure import build_closure, serialize_for_llm

for screen_entry in my_existing_screen_finder(repo_root):
    closure = build_closure(
        entry_file=screen_entry.file_path,
        repo_root=repo_root,
        patterns=loaded_patterns_yaml,
        max_depth=3,
        token_budget=12000,
    )

    # LLM 호출 (사용자 기존 시스템)
    llm_input = serialize_for_llm(closure)
    result = my_existing_llm_client.analyze(llm_input)
    save_screen_analysis(screen_entry, result)

    # 팝업도 동일하게 — 별도 화면으로 결과 뽑기
    for popup in closure.popup_refs:
        if popup.component_file is None:
            continue   # use_hook / open_api 는 정적 추적 불가, skip
        popup_closure = build_closure(
            entry_file=popup.component_file,
            repo_root=repo_root,
            patterns=loaded_patterns_yaml,
        )
        popup_result = my_existing_llm_client.analyze(serialize_for_llm(popup_closure))
        save_popup_analysis(popup, popup_result, parent_screen=screen_entry)
```

## 8. 의존성 & 환경

### Python 패키지
- `tree-sitter` (>= 0.21)
- `tree-sitter-javascript` (>= 0.21, JSX 내장)
- `tree-sitter-typescript` (선택, .ts/.tsx 처리)

### 폐쇄망 wheel 다운로드 (Windows + Python 3.11)

```powershell
pip download tree-sitter tree-sitter-javascript tree-sitter-typescript ^
  -d .\wheels --platform win_amd64 --python-version 311 --only-binary=:all:
```

사내망에서:
```powershell
python -m pip install --no-index --find-links=.\wheels ^
  tree-sitter tree-sitter-javascript tree-sitter-typescript
```

### 인코딩 (한국어 레거시 코드)
파싱 시 utf-8 → euc-kr → cp949 → latin-1 fallback (`legacy_react_ast._decode_for_text`).
tree-sitter는 utf-8 가정이므로 비-utf-8 파일은 utf-8로 재인코딩 후 파싱.

## 9. 검증

`verify.py` 스크립트로 standalone 검증 가능. 사용자 자신의 레포에서 화면 1개를
직접 골라 closure 결과를 눈으로 보고 patterns.yaml 슬롯을 조정.

```powershell
python verify.py path/to/repo path/to/repo/src/pages/SomeScreen.jsx
python verify.py path/to/repo path/to/repo/src/pages/SomeScreen.jsx --patterns my_patterns.yaml
python verify.py path/to/repo path/to/repo/src/pages/SomeScreen.jsx --output ./out
```

## 10. 알려진 한계 & 확장 포인트

### 현재 한계
- **HOC / 동적 컴포넌트**: `withAuth(MyScreen)` 같은 wrapper는 import 그래프로 추적되지만,
  실제 렌더링 컴포넌트 결정은 런타임 — closure에는 양쪽 모두 들어감 (오버 inclusion).
- **Hook 기반 팝업**: `useModal()` 호출 site는 잡지만 어떤 팝업 컴포넌트를 실제로
  여는지는 정적으로 추적 불가. component_file이 None으로 표시.
- **dynamic import 변수**: `` import(`./pages/${name}`) `` 같은 변수 path는 미추적.
- **JSX skeleton의 표현식 props**: `onClick={complex_logic}` 같은 표현식은 `{...}`로 축약.

### 확장 가능한 포인트
- **mode 추가**: `summary` (LLM 요약) / `bytecode` (AST 노드 카운트만) 등.
- **호출 그래프 정밀화**: PopupRef의 `use_hook`/`open_api` trigger에 대해 같은 파일 내
  `setOpen(true)`/`setX(<Y/>)` 같은 state 호출까지 추적해 component_file 후보 추가.
- **i18n 라벨 추출**: `<Button>{t('search')}</Button>`의 `t('search')` 같은 i18n 키 수집해
  화면 의미를 LLM에 더 명확히 전달.

## 11. Claude Code에 작업 지시할 때 추천 프롬프트

```
이 설계 문서(DESIGN.md)와 legacy_react_ast.py / legacy_react_closure.py 를 받아서,
우리 분석기 (oracle_embeddings/ 패키지) 에 통합해줘.

요구사항:
1. 두 모듈을 oracle_embeddings/ 디렉토리로 옮기고
2. legacy_react_closure.py 의 import 를 'from .legacy_react_ast import ...' 로 수정
3. 우리의 기존 화면 진입점 식별 코드 [위치 명시] 와 build_closure 를 연결
4. patterns.yaml 의 react: 섹션을 추가하고, discover-patterns 가 LLM 으로 채울 수 있게 슬롯 노출
5. 기존 LLM 분석 호출 코드 [위치 명시] 가 closure 결과를 받도록 어댑터 추가
6. 화면 closure 분석 후 popup_refs 를 순회하며 동일 LLM 분석을 별도로 실행
   (parent_screen 메타데이터를 LLM 결과에 함께 저장)
7. 회귀 테스트: mock 레포 [위치] 4~5개에서 closure 빌드 결과의 count 변화 없는지 확인
```
