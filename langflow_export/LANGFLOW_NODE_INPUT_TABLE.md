# Langflow 노드별 최종 입력값 표

이 문서는 `SK DB`, `Prompt Template`, `LLM Model`, `Python Interpreter`를 조합해서 기존 Python agent 로직을 Langflow로 재구성하기 위한 실입력표입니다.

전제:

- `SK DB` 컴포넌트 입력 필드: `Host`, `Port`, `Service Name`, `Username`, `Password`, `SQL Query`
- `SK DB` 출력: `Result Table`
- LLM 호출은 Python 코드가 아니라 `Prompt Template -> LLM Model`로 처리
- Python Interpreter는 Result Table 변환, JSON 파싱, 문자열 조립에만 사용

공통 DB 접속값:

| Field | Value |
| --- | --- |
| Host | 회사 Oracle Host |
| Port | `1521` 또는 사내 설정값 |
| Service Name | Oracle service name |
| Username | DB 사용자 |
| Password | DB 비밀번호 |

공통 변수명 규칙:

| 이름 | 의미 |
| --- | --- |
| `migration_jobs_table` | Migration pending 조회 Result Table |
| `conversion_jobs_table` | SQL Conversion pending 조회 Result Table |
| `tuning_jobs_table` | SQL Tuning pending 조회 Result Table |
| `formatting_jobs_table` | SQL Formatting pending 조회 Result Table |
| `decision_json` | Supervisor LLM 판단 결과 |
| `row_id` | `NEXT_SQL_INFO` ROWID 문자열 |
| `map_id` | `NEXT_MIG_INFO.MAP_ID` |

---

## 0. 전체 노드 순서

```text
01 SK DB - Poll Migration
02 SK DB - Poll SQL Conversion
03 SK DB - Poll SQL Tuning
04 SK DB - Poll SQL Formatting
05 Python - Result Tables To Prompt Variables
06 Prompt Template - Supervisor Decision
07 LLM Model - Supervisor Decision
08 Python/JSON Parser - Parse Decision

SQL Formatting Branch:
09F SK DB - Load Formatting Job Detail
10F Python - Build Formatting Variables
11F Prompt Template - SQL Formatting
12F LLM Model - Format SQL
13F SK DB - Save Formatted SQL

SQL Tuning Branch:
09T SK DB - Load Tuning Job Detail
10T SK DB - Load SQL Tuning RAG
11T Python - Build Tuning Variables
12T Prompt Template - SQL Tuning
13T LLM Model - Generate Tuned SQL JSON
14T Python/JSON Parser - Parse Tuned SQL
15T SK DB - Save Tuned SQL

SQL Conversion Branch:
09C SK DB - Load Conversion Job Detail
10C SK DB - Load Mapping Rules
11C SK DB - Load SQL Conversion RAG
12C Python - Build Conversion Variables
13C Prompt Template - TO-BE SQL
14C LLM Model - Generate TO-BE SQL
15C SK DB - Save TO-BE SQL
```

처음 MVP는 `01~08` + `09F~13F`부터 만드는 것을 권장합니다. Formatting branch가 가장 위험도가 낮습니다.

---

## 1. Supervisor Polling

### 01 SK DB - Poll Migration

| 항목 | 입력값 |
| --- | --- |
| Component | `SK DB` |
| Node Name | `01 SK DB - Poll Migration` |
| Output Name | `migration_jobs_table` |
| SQL Query | 아래 SQL |

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

### 02 SK DB - Poll SQL Conversion

| 항목 | 입력값 |
| --- | --- |
| Component | `SK DB` |
| Node Name | `02 SK DB - Poll SQL Conversion` |
| Output Name | `conversion_jobs_table` |
| SQL Query | 아래 SQL |

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

### 03 SK DB - Poll SQL Tuning

| 항목 | 입력값 |
| --- | --- |
| Component | `SK DB` |
| Node Name | `03 SK DB - Poll SQL Tuning` |
| Output Name | `tuning_jobs_table` |
| SQL Query | 아래 SQL |

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

### 04 SK DB - Poll SQL Formatting

| 항목 | 입력값 |
| --- | --- |
| Component | `SK DB` |
| Node Name | `04 SK DB - Poll SQL Formatting` |
| Output Name | `formatting_jobs_table` |
| SQL Query | 아래 SQL |

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

### 05 Python - Result Tables To Prompt Variables

| 항목 | 입력값 |
| --- | --- |
| Component | `Python Interpreter` |
| Node Name | `05 Python - Result Tables To Prompt Variables` |
| Inputs | `migration_jobs_table`, `conversion_jobs_table`, `tuning_jobs_table`, `formatting_jobs_table` |
| Outputs | `migration_jobs`, `conversion_jobs`, `tuning_jobs`, `formatting_jobs` |
| Code | 아래 코드 |

```python
import json

def table_to_records(table):
    if table is None:
        return []
    if hasattr(table, "to_dict"):
        try:
            return table.to_dict(orient="records")
        except TypeError:
            return table.to_dict()
    data = getattr(table, "data", table)
    if isinstance(data, dict) and "data" in data:
        data = data["data"]
    return data if isinstance(data, list) else [data]

result = {
    "migration_jobs": json.dumps(table_to_records(migration_jobs_table), ensure_ascii=False, default=str),
    "conversion_jobs": json.dumps(table_to_records(conversion_jobs_table), ensure_ascii=False, default=str),
    "tuning_jobs": json.dumps(table_to_records(tuning_jobs_table), ensure_ascii=False, default=str),
    "formatting_jobs": json.dumps(table_to_records(formatting_jobs_table), ensure_ascii=False, default=str),
}
```

### 06 Prompt Template - Supervisor Decision

| 항목 | 입력값 |
| --- | --- |
| Component | `Prompt Template` |
| Node Name | `06 Prompt Template - Supervisor Decision` |
| Variables | `migration_jobs`, `conversion_jobs`, `tuning_jobs`, `formatting_jobs` |
| Output | Prompt text to LLM Model |

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
1. migration_jobs가 []가 아니면 stage="MIGRATION"
2. migration_jobs가 []이고 conversion_jobs가 []가 아니면 stage="SQL_CONVERSION"
3. migration_jobs와 conversion_jobs가 []이고 tuning_jobs가 []가 아니면 stage="SQL_TUNING"
4. 앞의 세 개가 []이고 formatting_jobs가 []가 아니면 stage="SQL_FORMATTING"
5. 모두 []이면 stage="WAIT"

반드시 JSON만 출력하세요. markdown code block을 쓰지 마세요.

출력 형식:
{
  "stage": "MIGRATION | SQL_CONVERSION | SQL_TUNING | SQL_FORMATTING | WAIT",
  "reason": "선택 이유",
  "selected_job": {}
}
```

### 07 LLM Model - Supervisor Decision

| 항목 | 입력값 |
| --- | --- |
| Component | `LLM Model` |
| Node Name | `07 LLM Model - Supervisor Decision` |
| Input | `06 Prompt Template` output |
| Output Name | `decision_text` |
| Temperature | `0` 권장 |

### 08 Python/JSON Parser - Parse Decision

| 항목 | 입력값 |
| --- | --- |
| Component | `JSON Parser` 또는 `Python Interpreter` |
| Node Name | `08 Parser - Decision JSON` |
| Input | `decision_text` |
| Outputs | `stage`, `selected_job`, `row_id`, `map_id` |

```python
import json
import re

text = decision_text if isinstance(decision_text, str) else str(decision_text)
text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
data = json.loads(text)
selected = data.get("selected_job") or {}

result = {
    "stage": data.get("stage", "WAIT"),
    "reason": data.get("reason", ""),
    "selected_job": selected,
    "row_id": selected.get("ROW_ID") or selected.get("row_id"),
    "map_id": selected.get("MAP_ID") or selected.get("map_id"),
}
```

---

## 2. SQL Formatting Branch

### 09F SK DB - Load Formatting Job Detail

| 항목 | 입력값 |
| --- | --- |
| Component | `SK DB` |
| Node Name | `09F SK DB - Load Formatting Job Detail` |
| Input Variable | `row_id` |
| Output Name | `formatting_job_detail_table` |

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

### 10F Python - Build Formatting Variables

| 항목 | 입력값 |
| --- | --- |
| Component | `Python Interpreter` |
| Inputs | `formatting_job_detail_table` |
| Outputs | `row_id`, `input_sql` |

```python
def first_row(table):
    if hasattr(table, "to_dict"):
        rows = table.to_dict(orient="records")
    else:
        rows = getattr(table, "data", table)
    return rows[0] if rows else {}

row = first_row(formatting_job_detail_table)
input_sql = (row.get("TUNED_SQL") or row.get("tuned_sql") or row.get("TO_SQL_TEXT") or row.get("to_sql_text") or "").strip()

result = {
    "row_id": row.get("ROW_ID") or row.get("row_id"),
    "input_sql": input_sql,
}
```

### 11F Prompt Template - SQL Formatting

| 항목 | 입력값 |
| --- | --- |
| Component | `Prompt Template` |
| Variables | `input_sql` |
| Output | Prompt text to LLM Model |

```text
당신은 Oracle/MyBatis SQL Formatter입니다.

목표:
입력된 Oracle/MyBatis SQL의 의미를 변경하지 않고 줄바꿈과 4칸 들여쓰기만 적용합니다.

입력 SQL:
{input_sql}

규칙:
1. 테이블명, 컬럼명, Alias, JOIN/WHERE 구조, MyBatis 태그, #{{param}}/${{param}} 바인드 파라미터를 절대 변경하지 마세요.
2. SELECT/FROM/WHERE/GROUP BY/HAVING/ORDER BY/JOIN/ON/AND/OR/CASE/MyBatis 태그를 읽기 좋게 줄바꿈하세요.
3. 들여쓰기는 4칸 공백만 사용하세요.
4. 포맷팅된 SQL만 출력하세요.
5. 설명, markdown code block, 주석, trailing semicolon은 출력하지 마세요.
```

### 12F LLM Model - Format SQL

| 항목 | 입력값 |
| --- | --- |
| Component | `LLM Model` |
| Input | `11F Prompt Template` output |
| Output Name | `formatted_sql` |
| Temperature | `0` |

### 13F SK DB - Save Formatted SQL

| 항목 | 입력값 |
| --- | --- |
| Component | `SK DB` |
| Inputs | `formatted_sql`, `row_id` |
| SQL Query | 아래 SQL |

```sql
UPDATE NEXT_SQL_INFO
SET FORMATTED_SQL = :formatted_sql,
    FORMATTING_RETRY_YN = 'N',
    UPD_TS = CURRENT_TIMESTAMP
WHERE ROWID = CHARTOROWID(:row_id)
```

---

## 3. SQL Tuning Branch

### 09T SK DB - Load Tuning Job Detail

| 항목 | 입력값 |
| --- | --- |
| Component | `SK DB` |
| Input Variable | `row_id` |
| Output Name | `tuning_job_detail_table` |

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

### 10T SK DB - Load SQL Tuning RAG

| 항목 | 입력값 |
| --- | --- |
| Component | `SK DB` |
| Output Name | `tuning_rag_table` |

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

### 11T Python - Build Tuning Variables

| 항목 | 입력값 |
| --- | --- |
| Component | `Python Interpreter` |
| Inputs | `tuning_job_detail_table`, `tuning_rag_table` |
| Outputs | `row_id`, `current_tobe_sql`, `universal_tuning_rules`, `tuning_examples_json`, `last_error` |

```python
import json

def rows(table):
    if hasattr(table, "to_dict"):
        return table.to_dict(orient="records")
    data = getattr(table, "data", table)
    return data if isinstance(data, list) else [data]

job = rows(tuning_job_detail_table)[0]
rag_rows = rows(tuning_rag_table)

general = []
examples = []
for r in rag_rows:
    rule_type = str(r.get("RULE_TYPE") or r.get("rule_type") or "").upper()
    if rule_type == "GENERAL":
        general.append(str(r.get("GUIDANCE_TEXT") or r.get("guidance_text") or ""))
    else:
        examples.append({
            "guidance": r.get("GUIDANCE_TEXT") or r.get("guidance_text"),
            "source_sql": r.get("SOURCE_SQL") or r.get("source_sql"),
            "target_sql": r.get("TARGET_SQL") or r.get("target_sql"),
        })

result = {
    "row_id": job.get("ROW_ID") or job.get("row_id"),
    "current_tobe_sql": job.get("TO_SQL_TEXT") or job.get("to_sql_text") or "",
    "universal_tuning_rules": "\n".join(general),
    "tuning_examples_json": json.dumps(examples[:5], ensure_ascii=False, default=str),
    "last_error": job.get("LOG") or job.get("log") or "",
}
```

### 12T Prompt Template - SQL Tuning

| 항목 | 입력값 |
| --- | --- |
| Component | `Prompt Template` |
| Variables | `current_tobe_sql`, `universal_tuning_rules`, `tuning_examples_json`, `last_error` |

```text
당신은 Oracle/MyBatis TO-BE SQL tuning assistant입니다.

현재 TO-BE SQL:
{current_tobe_sql}

공통 튜닝 규칙:
{universal_tuning_rules}

검색된 튜닝 예시 JSON:
{tuning_examples_json}

이전 오류:
{last_error}

규칙:
1. 반드시 JSON 객체 하나만 출력하세요.
2. JSON key는 tuned_sql, tuned_result만 사용하세요.
3. tuned_sql은 Oracle/MyBatis SQL 템플릿 하나여야 합니다.
4. 설명, markdown code block, trailing semicolon, 여러 SQL 문장을 출력하지 마세요.
5. SQL 의미, 테이블명, 컬럼명, alias, join 의미, MyBatis 태그, #{{param}}/${{param}} 바인드 파라미터를 보존하세요.
6. 안전한 튜닝이 없으면 tuned_sql은 current_tobe_sql 그대로, tuned_result는 "NO TUNING"으로 반환하세요.

출력 형식:
{
  "tuned_sql": "SELECT ...",
  "tuned_result": "적용한 튜닝 요약 또는 NO TUNING"
}
```

### 13T LLM Model - Generate Tuned SQL JSON

| 항목 | 입력값 |
| --- | --- |
| Component | `LLM Model` |
| Input | `12T Prompt Template` output |
| Output Name | `tuning_llm_output` |
| Temperature | `0` |

### 14T Python/JSON Parser - Parse Tuned SQL

| 항목 | 입력값 |
| --- | --- |
| Component | `JSON Parser` 또는 `Python Interpreter` |
| Inputs | `tuning_llm_output`, `row_id` |
| Outputs | `tuned_sql`, `tuned_result`, `row_id` |

```python
import json
import re

text = tuning_llm_output if isinstance(tuning_llm_output, str) else str(tuning_llm_output)
text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
data = json.loads(text)

result = {
    "row_id": row_id,
    "tuned_sql": data.get("tuned_sql", ""),
    "tuned_result": data.get("tuned_result", ""),
}
```

### 15T SK DB - Save Tuned SQL

| 항목 | 입력값 |
| --- | --- |
| Component | `SK DB` |
| Inputs | `row_id`, `tuned_sql`, `tuned_result` |

```sql
UPDATE NEXT_SQL_INFO
SET TUNED_SQL = :tuned_sql,
    TUNED_RESULT = :tuned_result,
    STATUS_TUNING = 'PASS-TUNING',
    UPD_TS = CURRENT_TIMESTAMP
WHERE ROWID = CHARTOROWID(:row_id)
```

---

## 4. SQL Conversion Branch - TO-BE SQL 생성 MVP

### 09C SK DB - Load Conversion Job Detail

| 항목 | 입력값 |
| --- | --- |
| Component | `SK DB` |
| Input Variable | `row_id` |
| Output Name | `conversion_job_detail_table` |

```sql
SELECT
    ROWIDTOCHAR(ROWID) AS ROW_ID,
    TAG_KIND,
    SPACE_NM,
    SQL_ID,
    FR_SQL_TEXT,
    EDIT_FR_SQL,
    TARGET_TABLE,
    TO_SQL_TEXT,
    TOBE_CORRECT_SQL,
    BIND_CORRECT_SQL,
    TEST_CORRECT_SQL,
    STATUS_CONVERSION,
    LOG
FROM NEXT_SQL_INFO
WHERE ROWID = CHARTOROWID(:row_id)
```

### 10C SK DB - Load Mapping Rules

| 항목 | 입력값 |
| --- | --- |
| Component | `SK DB` |
| Output Name | `mapping_rules_table` |

```sql
SELECT
    M.MAP_ID,
    M.MAP_TYPE,
    M.FR_TABLE,
    M.TO_TABLE,
    M.CONDITION,
    D.FR_COL,
    D.TO_COL
FROM NEXT_MIG_INFO M
LEFT JOIN NEXT_MIG_INFO_DTL D
    ON D.MAP_ID = M.MAP_ID
WHERE UPPER(TRIM(NVL(M.USE_YN, 'N'))) = 'Y'
ORDER BY M.MAP_ID, D.FR_COL
```

### 11C SK DB - Load SQL Conversion RAG

| 항목 | 입력값 |
| --- | --- |
| Component | `SK DB` |
| Output Name | `conversion_rag_table` |

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

### 12C Python - Build Conversion Variables

| 항목 | 입력값 |
| --- | --- |
| Component | `Python Interpreter` |
| Inputs | `conversion_job_detail_table`, `mapping_rules_table`, `conversion_rag_table` |
| Outputs | `row_id`, `from_sql`, `mapping_schema_text`, `target_schema`, `correct_sql_hint_json`, `last_error` |

```python
import json

TARGET_SCHEMA = "SFAADM"  # 환경에 맞게 변경

def rows(table):
    if hasattr(table, "to_dict"):
        return table.to_dict(orient="records")
    data = getattr(table, "data", table)
    return data if isinstance(data, list) else [data]

job = rows(conversion_job_detail_table)[0]
mapping_rows = rows(mapping_rules_table)
rag_rows = rows(conversion_rag_table)

from_sql = (job.get("EDIT_FR_SQL") or job.get("edit_fr_sql") or job.get("FR_SQL_TEXT") or job.get("fr_sql_text") or "").strip()

mapping_lines = ["[MIGRATION_MAPPING_RULES]"]
for r in mapping_rows:
    mapping_lines.append(
        f"- MAP_ID={r.get('MAP_ID')}, MAP_TYPE={r.get('MAP_TYPE')}, "
        f"{r.get('FR_TABLE')}.{r.get('FR_COL')} -> {r.get('TO_TABLE')}.{r.get('TO_COL')}, "
        f"CONDITION={r.get('CONDITION') or ''}"
    )

general_rag = []
search_rag = []
for r in rag_rows:
    rule_type = str(r.get("RULE_TYPE") or "").upper()
    if rule_type == "GENERAL":
        general_rag.append(r.get("GUIDANCE_TEXT") or "")
    else:
        search_rag.append({
            "guidance": r.get("GUIDANCE_TEXT"),
            "source_sql": r.get("SOURCE_SQL"),
            "target_sql": r.get("TARGET_SQL"),
        })

mapping_lines.append("\n[SQL_CONVERSION_GENERAL_RAG_GUIDANCE]")
mapping_lines.extend(general_rag)
mapping_lines.append("\n[SQL_CONVERSION_SEARCH_RAG_TOP_K_BY_SQL_BLOCK]")
mapping_lines.append(json.dumps(search_rag[:5], ensure_ascii=False, default=str))

result = {
    "row_id": job.get("ROW_ID") or job.get("row_id"),
    "from_sql": from_sql,
    "mapping_schema_text": "\n".join(mapping_lines),
    "target_schema": TARGET_SCHEMA,
    "correct_sql_hint_json": json.dumps([], ensure_ascii=False),
    "last_error": job.get("LOG") or job.get("log") or "",
}
```

### 13C Prompt Template - TO-BE SQL

| 항목 | 입력값 |
| --- | --- |
| Component | `Prompt Template` |
| Variables | `from_sql`, `mapping_schema_text`, `target_schema`, `correct_sql_hint_json`, `last_error` |

```text
당신은 Oracle/MyBatis SQL migration generator입니다.

목표:
AS-IS SQL을 TO-BE Oracle/MyBatis SQL 하나로 변환하세요.

FROM SQL:
{from_sql}

Mapping / RAG:
{mapping_schema_text}

Target schema:
{target_schema}

Correct SQL hint JSON:
{correct_sql_hint_json}

Last error:
{last_error}

규칙:
1. Oracle 19c 문법으로 생성하세요.
2. mapping_schema_text의 MIGRATION_MAPPING_RULES를 테이블/컬럼 매핑의 우선 진실로 사용하세요.
3. 매핑이 없는 source table/column은 원래 이름을 유지하세요.
4. 기존 query 구조, filters, aggregation, joins, aliases, MyBatis dynamic tags, bind parameter names를 최대한 보존하세요.
5. #{{param}}, ${{param}} 같은 MyBatis bind marker를 제거하거나 이름 변경하지 마세요.
6. <if>, <choose>, <when>, <otherwise>, <where>, <trim>, <foreach>를 유지하세요.
7. 모든 물리 TO-BE table은 {target_schema}.TABLE_NAME 형식으로 출력하세요.
8. DUAL, CTE 이름, inline view alias, table alias에는 schema를 붙이지 마세요.
9. SQL 하나만 출력하세요.
10. 설명, markdown, JSON, PL/SQL, 여러 SQL 문장, trailing semicolon을 출력하지 마세요.
```

### 14C LLM Model - Generate TO-BE SQL

| 항목 | 입력값 |
| --- | --- |
| Component | `LLM Model` |
| Input | `13C Prompt Template` output |
| Output Name | `tobe_sql` |
| Temperature | `0` |

### 15C SK DB - Save TO-BE SQL

| 항목 | 입력값 |
| --- | --- |
| Component | `SK DB` |
| Inputs | `row_id`, `tobe_sql` |

```sql
UPDATE NEXT_SQL_INFO
SET TO_SQL_TEXT = :tobe_sql,
    STATUS_CONVERSION = 'READY',
    LOG = 'TO-BE SQL generated by Langflow',
    UPD_TS = CURRENT_TIMESTAMP
WHERE ROWID = CHARTOROWID(:row_id)
```

`STATUS_CONVERSION='READY'`로 둔 이유:

- 이 MVP는 TO-BE SQL 생성까지만 처리합니다.
- 기존 Python pipeline의 bind/test 검증까지 완료한 것이 아니므로 바로 `PASS-CONVERSION`으로 두면 위험합니다.
- 이후 bind/test branch를 추가한 뒤 최종 검증 성공 시 `PASS-CONVERSION`으로 업데이트하는 것이 맞습니다.

---

## 5. 다음에 추가할 Branch

아래는 아직 Langflow MVP 표에는 포함하지 않은 후속 작업입니다.

| Branch | 필요 노드 |
| --- | --- |
| Bind SQL 생성 | SK DB Load Job -> Prompt Template Bind SQL -> LLM Model -> SK DB Execute Bind SQL |
| Bind Set 생성 | Bind SQL Result Table -> Python JSON 변환 -> SK DB Save BIND_SET |
| Test SQL 생성 | Prompt Template Test SQL -> LLM Model -> SK DB Execute Test SQL |
| Conversion 최종 저장 | Test Result 비교 -> SK DB Update PASS-CONVERSION/FAIL-* |
| Tuned Test SQL 생성 | Prompt Template Tuned Test -> LLM Model -> SK DB Execute -> Save |
| DB Migration 실행 | Generate migration SQL -> 승인 -> SK DB Execute DML/DDL -> Verify -> Save |

---

## 6. 권장 제작 순서

1. `01~08` Supervisor polling/decision
2. `09F~13F` SQL Formatting
3. `09T~15T` SQL Tuning
4. `09C~15C` SQL Conversion TO-BE 생성
5. Bind/Test validation branch
6. DB Migration branch

이 순서가 가장 안전합니다. DML/DDL 실행이 들어가는 DB Migration은 마지막에 붙이세요.
