# 메뉴 매핑 템플릿

아래 테이블에 프로젝트 메뉴 항목을 작성합니다.

## 작성 규칙

- **1레벨 ~ 5레벨**: 메뉴 계층. 가장 깊은 레벨이 `program_name` 이 됩니다.
- **URL**: 해당 메뉴가 호출하는 backend endpoint URL. URL 이 있는 행만 분석 대상.
- URL 이 빈 행은 컨테이너 노드로 스킵됩니다.
- 헤더는 한글(`1레벨`~`5레벨`) 또는 영문(`level1`~`level5`, `lv1`~`lv5`) 모두 인식합니다.
- URL 헤더: `URL`, `uri`, `경로`, `path`, `link`, `endpoint` 중 아무거나.

## 사용법

```bash
python main.py analyze-legacy \
    --backend-dir <backend project root> \
    --menu-md <이 파일 경로>
```

## 메뉴 테이블

| 1레벨 | 2레벨 | 3레벨 | 4레벨 | 5레벨 | URL |
|-------|-------|-------|-------|-------|-----|
| 주문관리 | 주문조회 | | | | /api/order/list |
| 주문관리 | 주문등록 | | | | /api/order/save |
| 주문관리 | 주문이력 | 삭제이력 | | | /api/order/{id} |
| 설비관리 | 설비조회 | 모델링 | SVID 코드 | | /api/svid/list |
| 설비관리 | 설비조회 | 모델링 | SVID 코드 | 상세 | /api/svid/detail |
| 설비관리 | | | | | |
| 통계 | 리포트 | | | | /api/stats/legacy |
