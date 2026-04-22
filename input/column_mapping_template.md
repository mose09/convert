# Column Mapping (Flat 9-Column Format)

> 사용자 워크플로우용 평면(flat) 매핑 템플릿. 한 행 = 한 컬럼 매핑.
> DRM 우회: 그냥 텍스트(.md/.txt)로 작성 → `convert-mapping --md` 로 표준 YAML 변환.

## 컬럼 의미

| 헤더 | 의미 |
|------|------|
| `asis_table` | AS-IS 테이블 (필수) |
| `asis_column` | AS-IS 컬럼 (필수) |
| `asis_column_type` | AS-IS Oracle 타입 (예: `VARCHAR2(8)`) |
| `tobe_table` | TO-BE 테이블 (비우면 asis_table 그대로) |
| `tobe_table_comment` | TO-BE 테이블 한글 코멘트 (자동 주석 삽입 소스) |
| `tobe_column` | TO-BE 컬럼 (`-` / 빈 값 → drop) |
| `tobe_column_type` | TO-BE Oracle 타입 |
| `tobe_column_comment` | TO-BE 컬럼 한글 코멘트 (자동 주석 삽입 소스) |
| `remark` | 비고/특이사항 (split/merge/value_map 같은 복잡 케이스 메모용) |

## 변환 규칙 (heuristic 모드)

- 컬럼 이름 다름 + 타입 같음 → `kind: rename`
- 타입 다름 → `kind: type_convert` + transform 자동 추론 (잘 알려진 페어만)
- `tobe_column` 비우면 → `kind: drop`
- 잘 알려지지 않은 type 페어는 `note` 에 ⚠ 마커 + 사용자 수정 안내

## LLM 모드

`PATTERN_LLM_*` 또는 `LLM_*` 환경변수가 있으면 LLM 이 직접 9-컬럼 → rich YAML 로 변환. heuristic 보다 split/merge/value_map 같은 복잡 케이스를 remark 에서 잘 잡아냄.

## 샘플

| asis_table | asis_column | asis_column_type | tobe_table | tobe_table_comment | tobe_column | tobe_column_type | tobe_column_comment | remark |
|------------|-------------|------------------|------------|--------------------|--------------------|------------------|---------------------|--------|
| CUST | CUST_ID | NUMBER(10) | CUSTOMER | 고객 마스터 | CUSTOMER_ID | NUMBER(10) | 고객ID |  |
| CUST | CUST_NM | VARCHAR2(100) | CUSTOMER | 고객 마스터 | CUSTOMER_NAME | VARCHAR2(200) | 고객명 |  |
| CUST | REG_DT | VARCHAR2(8) | CUSTOMER | 고객 마스터 | REGISTER_DATE | DATE | 등록일자 | YYYYMMDD → DATE 자동변환 |
| CUST | UPD_TS | VARCHAR2(14) | CUSTOMER | 고객 마스터 | UPDATED_AT | TIMESTAMP | 변경일시 | YYYYMMDDHH24MISS |
| CUST | TEL_NO | VARCHAR2(20) | CUSTOMER | 고객 마스터 | PHONE_NUMBER | VARCHAR2(20) | 전화번호 |  |
| CUST | OBSOLETE_FLAG | CHAR(1) | CUSTOMER | 고객 마스터 |  |  |  | TO-BE 에서 삭제 |
| CUST | USE_YN | CHAR(1) | CUSTOMER | 고객 마스터 | IS_ACTIVE | NUMBER(1) | 활성여부 | Y→1, N→0 (value_map; 사용자 수동 수정) |
| EVT | YYYY | VARCHAR2(4) | EVENT | 이벤트 | EVENT_DATE | DATE | 이벤트일자 | YYYY+MM+DD merge → DATE (사용자 수동 수정) |
| EVT | MM | VARCHAR2(2) | EVENT | 이벤트 | EVENT_DATE | DATE | 이벤트일자 | merge 컬럼 |
| EVT | DD | VARCHAR2(2) | EVENT | 이벤트 | EVENT_DATE | DATE | 이벤트일자 | merge 컬럼 |

## 사용법

```bash
# md → column_mapping.yaml 변환 (LLM 사용)
python main.py convert-mapping --md input/column_mapping_template.md \
  --output input/column_mapping.yaml

# heuristic-only (LLM 없음)
python main.py convert-mapping --md input/column_mapping_template.md \
  --output input/column_mapping.yaml --no-llm
```
