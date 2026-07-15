# SK DB + Prompt Template + LLM Model 재구성 설계

이 문서는 기존 Python agent를 그대로 호출하지 않고, 기존 로직을 Langflow 컴포넌트로 재구성하기 위한 설계입니다.

핵심 방향:

- DB 조회/검증/저장: `SK DB`
- LLM 호출: `Prompt Template` + `LLM Model`
- Result Table 변환/LLM 출력 파싱: 최소한의 `Python Interpreter`
- 기존 agent Python class 직접 호출: 사용하지 않음

## 전체 캔버스 구조

```text
01 SK DB - Poll Migration
02 SK DB - Poll SQL Conversion
03 SK DB - Poll SQL Tuning
04 SK DB - Poll SQL Formatting
        |
        v
05 Python - Result Tables To Variables
        |
        v
06 Prompt Template - Supervisor Decision
        |
        v
07 LLM Model - Decide Next Stage
        |
        v
08 Python - Parse Decision JSON
        |
        +------------------------------+
        |                              |
        v                              v
SQL Conversion Branch              SQL Tuning Branch
Migration Branch                   SQL Formatting Branch
```

현실적으로 Langflow에서 조건 분기가 제한적이면, 처음에는 branch를 자동 분기하지 말고 사용자가 LLM decision을 보고 해당 branch를 수동 실행하는 방식이 안전합니다.

## Phase 1. Supervisor / Polling

### 01 SK DB - Poll Migration

컴포넌트: `SK DB`

입력:

- Host
- Port
- Service Name
- Username
- Password
- SQL Query

SQL 예시:

```sql
SELECT *
FROM (
    SELECT
        M.MAP_ID,
        M.MAP_TYPE,
        M.FR_TABLE,
        M.TO_TABLE,
        M.PRIORITY,
        M.PRIOR_MAP_ID,
        M.STATUS,
        M.RETRY_COUNT,
        M.BATCH_CNT
    FROM NEXT_MIG_INFO M
    LEFT JOIN NEXT_MIG_INFO P
        ON P.MAP_ID = M.PRIOR_MAP_ID
    WHERE UPPER(TRIM(NVL(M.USE_YN, 'N'))) = 'Y'
      AND M.STATUS IS NULL
      AND (
          M.PRIOR_MAP_ID IS NULL
          OR M.PRIOR_MAP_ID <= 0
          OR UPPER(TRIM(NVL(P.STATUS, ''))) = 'PASS'
      )
    ORDER BY M.PRIORITY ASC, M.MAP_ID ASC
)
WHERE ROWNUM <= 1
```

출력:

- Result Table: `migration_jobs_table`

### 02 SK DB - Poll SQL Conversion

컴포넌트: `SK DB`

SQL 예시:

```sql
SELECT *
FROM (
    SELECT
        ROWIDTOCHAR(ROWID) AS ROW_ID,
        TAG_KIND,
        SPACE_NM,
        SQL_ID,
        FR_SQL_TEXT,
        TARGET_TABLE,
        EDIT_FR_SQL,
        TO_SQL_TEXT,
        STATUS_CONVERSION,
        PRIORITY,
        BATCH_CNT,
        UPD_TS
    FROM NEXT_SQL_INFO
    WHERE (
        UPPER(TRIM(STATUS_CONVERSION)) IN (
            'URGENT', 'READY', 'PENDING', 'FAIL', 'FAIL-TOBE', 'FAIL-BIND', 'FAIL-TEST'
        )
        OR STATUS_CONVERSION IS NULL
    )
      AND (
          TO_SQL_TEXT IS NULL
          OR UPPER(TRIM(STATUS_CONVERSION)) NOT IN ('PASS', 'PASS-CONVERSION')
      )
    ORDER BY
        PRIORITY ASC NULLS LAST,
        UPD_TS NULLS FIRST,
        TO_CHAR(SPACE_NM),
        TO_CHAR(SQL_ID)
)
WHERE ROWNUM <= 1
```

출력:

- Result Table: `conversion_jobs_table`

### 03 SK DB - Poll SQL Tuning

컴포넌트: `SK DB`

SQL 예시:

```sql
SELECT *
FROM (
    SELECT
        ROWIDTOCHAR(ROWID) AS ROW_ID,
        TAG_KIND,
        SPACE_NM,
        SQL_ID,
        TO_SQL_TEXT,
        TUNED_SQL,
        STATUS_CONVERSION,
        STATUS_TUNING,
        BIND_SQL,
        BIND_SET,
        TEST_SQL,
        PRIORITY,
        UPD_TS
    FROM NEXT_SQL_INFO
    WHERE UPPER(TRIM(STATUS_CONVERSION)) IN ('PASS', 'PASS-CONVERSION')
      AND TO_SQL_TEXT IS NOT NULL
      AND UPPER(TRIM(STATUS_TUNING)) IN (
          'URGENT', 'READY', 'FAIL', 'FAIL-TUNED', 'FAIL-BIND', 'FAIL-TEST'
      )
    ORDER BY
        PRIORITY ASC NULLS LAST,
        UPD_TS NULLS FIRST,
        TO_CHAR(SPACE_NM),
        TO_CHAR(SQL_ID)
)
WHERE ROWNUM <= 1
```

출력:

- Result Table: `tuning_jobs_table`

### 04 SK DB - Poll SQL Formatting

컴포넌트: `SK DB`

SQL 예시:

```sql
SELECT *
FROM (
    SELECT
        ROWIDTOCHAR(ROWID) AS ROW_ID,
        SPACE_NM,
        SQL_ID,
        TO_SQL_TEXT,
        TUNED_SQL,
        FORMATTED_SQL,
        STATUS_TUNING,
        FORMATTING_RETRY_YN,
        UPD_TS
    FROM NEXT_SQL_INFO
    WHERE UPPER(TRIM(STATUS_TUNING)) IN ('PASS', 'PASS-TUNING')
      AND UPPER(TRIM(FORMATTING_RETRY_YN)) = 'Y'
    ORDER BY UPD_TS NULLS FIRST, TO_CHAR(SPACE_NM), TO_CHAR(SQL_ID)
)
WHERE ROWNUM <= 1
```

출력:

- Result Table: `formatting_jobs_table`

## Phase 2. Result Table -> Prompt Variables

### 05 Python - Result Tables To Variables

컴포넌트: `Python Interpreter`

역할:

- `SK DB` Result Table들을 JSON string으로 변환
- Prompt Template에 넣을 변수 생성

출력 변수:

```text
migration_jobs
conversion_jobs
tuning_jobs
formatting_jobs
```

개념 코드:

```python
import json

def table_to_json(table):
    if table is None:
        return "[]"
    if hasattr(table, "to_dict"):
        return json.dumps(table.to_dict(orient="records"), ensure_ascii=False, default=str)
    if isinstance(table, list):
        return json.dumps(table, ensure_ascii=False, default=str)
    data = getattr(table, "data", table)
    return json.dumps(data, ensure_ascii=False, default=str)

result = {
    "migration_jobs": table_to_json(migration_jobs_table),
    "conversion_jobs": table_to_json(conversion_jobs_table),
    "tuning_jobs": table_to_json(tuning_jobs_table),
    "formatting_jobs": table_to_json(formatting_jobs_table),
}
```

Langflow의 Python Interpreter 입출력 명칭은 회사 환경마다 다를 수 있으니, 실제 컴포넌트에서 Result Table input 변수명을 맞춰야 합니다.

## Phase 3. Supervisor Decision

### 06 Prompt Template - Supervisor Decision

컴포넌트: `Prompt Template`

Variables:

```text
migration_jobs
conversion_jobs
tuning_jobs
formatting_jobs
```

Template:

```text
당신은 DB migration pipeline supervisor입니다.

현재 pending DB migration jobs:
{migration_jobs}

현재 pending SQL conversion jobs:
{conversion_jobs}

현재 pending SQL tuning jobs:
{tuning_jobs}

현재 pending SQL formatting jobs:
{formatting_jobs}

우선순위:
1. migration_jobs가 비어있지 않으면 stage="MIGRATION"
2. migration_jobs가 비어 있고 conversion_jobs가 비어있지 않으면 stage="SQL_CONVERSION"
3. migration_jobs와 conversion_jobs가 비어 있고 tuning_jobs가 비어있지 않으면 stage="SQL_TUNING"
4. 앞의 세 개가 비어 있고 formatting_jobs가 비어있지 않으면 stage="SQL_FORMATTING"
5. 모두 비어 있으면 stage="WAIT"

반드시 JSON만 출력하세요.

출력 형식:
{
  "stage": "MIGRATION | SQL_CONVERSION | SQL_TUNING | SQL_FORMATTING | WAIT",
  "reason": "선택 이유",
  "selected_job": {}
}
```

### 07 LLM Model - Decide Next Stage

컴포넌트: `LLM Model`

역할:

- Prompt Template 결과를 입력받아 다음 stage 판단

주의:

- 운영 안정성만 보면 이 decision은 LLM보다 deterministic Python/조건 컴포넌트가 낫습니다.
- 다만 Langflow 시각화와 설명 가능성을 위해 LLM decision node로 둬도 됩니다.

### 08 Python - Parse Decision JSON

컴포넌트: `Python Interpreter`

역할:

- LLM 출력 JSON 파싱
- `stage`, `selected_job` 추출
- 후속 branch에서 사용할 변수 생성

가능하면 Langflow에 JSON Parser 컴포넌트가 있으면 그걸 쓰는 게 더 좋습니다.

## Branch A. SQL Conversion

SQL Conversion은 기존 코드 기준으로 세부 단계가 있습니다.

```text
1. pending job 조회
2. mapping rules 조회
3. conversion RAG 조회
4. TO-BE SQL 생성
5. bind SQL 생성
6. bind set 생성
7. test SQL 생성
8. test SQL 실행
9. NEXT_SQL_INFO 업데이트
```

### A1 SK DB - Load Conversion Job Detail

입력:

- `row_id`

SQL:

```sql
SELECT
    ROWIDTOCHAR(ROWID) AS ROW_ID,
    TAG_KIND,
    SPACE_NM,
    SQL_ID,
    FR_SQL_TEXT,
    EDIT_FR_SQL,
    TARGET_TABLE,
    TOBE_CORRECT_SQL,
    BIND_CORRECT_SQL,
    TEST_CORRECT_SQL,
    STATUS_CONVERSION,
    LOG
FROM NEXT_SQL_INFO
WHERE ROWID = CHARTOROWID(:row_id)
```

### A2 SK DB - Load Mapping Rules

SQL:

```sql
SELECT
    M.MAP_ID,
    M.MAP_TYPE,
    M.FR_TABLE,
    M.TO_TABLE,
    D.FR_COL,
    D.TO_COL
FROM NEXT_MIG_INFO M
LEFT JOIN NEXT_MIG_INFO_DTL D
    ON D.MAP_ID = M.MAP_ID
WHERE UPPER(TRIM(NVL(M.USE_YN, 'N'))) = 'Y'
ORDER BY M.MAP_ID, D.FR_COL
```

### A3 SK DB - Load SQL Conversion RAG

SQL:

```sql
SELECT
    RAG_ID,
    RULE_TYPE,
    SOURCE_TABLES,
    GUIDANCE_TEXT,
    SOURCE_SQL,
    TARGET_SQL
FROM NEXT_MIG_RAG_INFO
WHERE CATEGORY = 'SQL_CONVERSION'
  AND USE_YN = 'Y'
ORDER BY
    CASE WHEN RULE_TYPE = 'GENERAL' THEN 1 ELSE 2 END,
    RAG_ID
```

### A4 Python - Build Conversion Prompt Variables

역할:

- `FR_SQL_TEXT` 또는 `EDIT_FR_SQL` 선택
- mapping result table을 `mapping_schema_text`로 변환
- RAG result table을 JSON/string으로 변환
- Prompt Template 변수 생성

출력:

```text
from_sql
mapping_schema_text
target_schema
correct_sql_hint_json
last_error
```

### A5 Prompt Template - TO-BE SQL

기존 prompt:

```text
server/config/prompts/tobe_sql_prompt.json
```

Prompt Template에는 JSON 내용을 그대로 복사하되, 입력 변수는 아래로 둡니다.

```text
from_sql
mapping_schema_text
target_schema
correct_sql_hint_json
last_error
```

### A6 LLM Model - Generate TO-BE SQL

출력:

```text
tobe_sql
```

### A7 SK DB - Save TO-BE SQL

SQL:

```sql
UPDATE NEXT_SQL_INFO
SET TO_SQL_TEXT = :tobe_sql,
    STATUS_CONVERSION = 'PASS-TOBE',
    LOG = 'TOBE SQL generated by Langflow',
    UPD_TS = CURRENT_TIMESTAMP
WHERE ROWID = CHARTOROWID(:row_id)
```

실제 기존 코드에서는 이후 bind/test 검증까지 끝나야 `PASS-CONVERSION`이 됩니다. 따라서 `PASS-TOBE` 같은 중간 상태를 쓸지, 기존 상태 체계에 맞춰 별도 컬럼/로그만 쓸지 결정해야 합니다.

## Branch B. SQL Tuning

### B1 SK DB - Load Tuning Job Detail

SQL:

```sql
SELECT
    ROWIDTOCHAR(ROWID) AS ROW_ID,
    SPACE_NM,
    SQL_ID,
    TO_SQL_TEXT,
    TUNED_SQL,
    TUNED_RESULT,
    STATUS_TUNING,
    LOG
FROM NEXT_SQL_INFO
WHERE ROWID = CHARTOROWID(:row_id)
```

### B2 SK DB - Load SQL Tuning RAG

SQL:

```sql
SELECT
    RAG_ID,
    RULE_TYPE,
    SOURCE_TABLES,
    GUIDANCE_TEXT,
    SOURCE_SQL,
    TARGET_SQL
FROM NEXT_MIG_RAG_INFO
WHERE CATEGORY = 'SQL_TUNING'
  AND USE_YN = 'Y'
ORDER BY
    CASE WHEN RULE_TYPE = 'GENERAL' THEN 1 ELSE 2 END,
    RAG_ID
```

### B3 Python - Build Tuning Prompt Variables

출력:

```text
current_tobe_sql
universal_tuning_rules
tuning_examples_json
last_error
```

### B4 Prompt Template - SQL Tuning

기존 prompt:

```text
server/config/prompts/tobe_sql_tuning_prompt.json
```

Variables:

```text
current_tobe_sql
universal_tuning_rules
tuning_examples_json
last_error
```

### B5 LLM Model - Generate Tuned SQL JSON

출력 JSON:

```json
{
  "tuned_sql": "SELECT ...",
  "tuned_result": "..."
}
```

### B6 Python or JSON Parser - Parse Tuned SQL

출력:

```text
tuned_sql
tuned_result
```

### B7 SK DB - Save Tuned SQL

SQL:

```sql
UPDATE NEXT_SQL_INFO
SET TUNED_SQL = :tuned_sql,
    TUNED_RESULT = :tuned_result,
    STATUS_TUNING = 'PASS-TUNING',
    UPD_TS = CURRENT_TIMESTAMP
WHERE ROWID = CHARTOROWID(:row_id)
```

## Branch C. SQL Formatting

### C1 SK DB - Load Formatting Job Detail

SQL:

```sql
SELECT
    ROWIDTOCHAR(ROWID) AS ROW_ID,
    SPACE_NM,
    SQL_ID,
    TO_SQL_TEXT,
    TUNED_SQL,
    FORMATTED_SQL
FROM NEXT_SQL_INFO
WHERE ROWID = CHARTOROWID(:row_id)
```

### C2 Python - Select Formatting Input SQL

로직:

```text
input_sql = TUNED_SQL if exists else TO_SQL_TEXT
```

### C3 Prompt Template - SQL Formatting

기존 prompt:

```text
server/config/prompts/sql_indent_format_prompt.json
```

Variable:

```text
input_sql
```

### C4 LLM Model - Format SQL

출력:

```text
formatted_sql
```

### C5 SK DB - Save Formatted SQL

SQL:

```sql
UPDATE NEXT_SQL_INFO
SET FORMATTED_SQL = :formatted_sql,
    FORMATTING_RETRY_YN = 'N',
    UPD_TS = CURRENT_TIMESTAMP
WHERE ROWID = CHARTOROWID(:row_id)
```

## Branch D. DB Migration

DB Migration은 SQL Conversion/Tuning보다 Langflow 재구성이 더 어렵습니다.

기존 코드의 migration branch는 다음을 포함합니다.

```text
DDL 조회
Migration SQL 생성
Migration SQL 실행
검증 SQL 실행
retry
상태 업데이트
```

Langflow로 옮기려면 최소 구성은 아래와 같습니다.

```text
SK DB - Load Migration Job
SK DB - Load Mapping Details
Prompt Template - Generate Migration SQL
LLM Model - Migration SQL
SK DB - Execute Migration SQL
SK DB - Execute Verify SQL
SK DB - Update NEXT_MIG_INFO status
```

주의:

- `SK DB`가 DML/DDL 실행을 허용하는지 확인해야 합니다.
- 검증 결과 row count 비교를 Prompt Template으로 넘기려면 Result Table 변환 노드가 필요합니다.
- 운영에서는 Migration SQL 실행 전 승인 단계를 넣는 것이 안전합니다.

## 권장 MVP

처음부터 전체 자동화를 만들지 말고 아래 순서로 만드세요.

1. Polling Dashboard
   - SK DB 4개
   - Python Result Table 변환
   - Prompt Template + LLM 요약

2. SQL Formatting Branch
   - 가장 위험도가 낮음
   - `TUNED_SQL/TO_SQL_TEXT -> Prompt Template -> LLM Model -> SK DB UPDATE`

3. SQL Tuning Branch
   - LLM JSON output parsing 필요
   - RAG rule SK DB 조회 필요

4. SQL Conversion Branch
   - mapping/RAG/bind/test까지 길어서 가장 복잡함

5. DB Migration Branch
   - DML/DDL 실행이 있어 가장 위험함

## 결론

사용자 말이 맞습니다.

```text
SK DB 여러 개
-> Result Table 변환
-> Prompt Template
-> LLM Model
-> Parser
-> SK DB Update/Execute
```

이 구조가 “기존 Python 코드를 실행”하는 방식보다 Langflow에 더 자연스럽습니다.

Python Interpreter는 agent 실행용이 아니라 다음 보조 역할로만 제한하는 것이 좋습니다.

- Result Table to JSON/string
- LLM JSON output parsing
- 긴 문자열 조립
- null/empty 분기 처리
