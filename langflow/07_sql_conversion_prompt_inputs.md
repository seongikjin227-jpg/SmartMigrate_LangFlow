# SQL Conversion Prompt Inputs

공통 placeholder:
- `{from_sql}`: 변환 대상 AS-IS SQL. `EDIT_FR_SQL`이 있으면 먼저 사용하고, 없으면 `FR_SQL`을 사용한다.
- `{to_sql}`: 변환된 TO-BE SQL. `run_sql_conversion_job`에서는 생성된 `TO_SQL` 또는 `USER_EDITED=Y`인 경우 DB에 저장된 `TO_SQL`을 사용한다.
- `{bind_sql}`: BIND_SET을 만들기 위해 생성된 bind 후보 조회 SQL.
- `{bind_set}`: BIND_SQL 실행 결과 JSON. 예: `[{"param1":"data1"},{"param1":"data2"}]`
- `{mapping_schema_text}`: `NEXT_SQL_INFO.TARGET_TABLE`의 FR_TABLE 기준으로 조회한 `NEXT_MIG_INFO`/`NEXT_MIG_INFO_DTL` mapping rule.
- `{source_schema}`: Langflow component에 설정한 AS-IS source schema. BIND_SQL/TEST_SQL 실행 시 FROM 물리 테이블에 적용한다.
- `{target_schema}`: Langflow component에 설정한 TO-BE target schema. TO_SQL/TEST_SQL 실행 시 TO-BE 물리 테이블에 적용한다.
- `{last_error}`: 같은 run 안에서 재시도할 때 이전 단계의 에러 텍스트. 첫 생성에서는 `None` 또는 빈 값이다.


## TO SQL Prompt

아래 텍스트를 `to_sql_prompt` input에 넣는다.

```text
당신은 Oracle/MyBatis SQL migration generator다.
source result set을 유지하면서 TO-BE schema mapping을 따르는 실행 가능한 Oracle/MyBatis TO-BE SQL 문장 하나를 생성한다.

[입력값]
- from_sql:
{from_sql}

- source_schema:
{source_schema}

- target_schema:
{target_schema}

- mapping_schema_text:
{mapping_schema_text}

- last_error:
{last_error}

[반드시 지켜야 할 규칙]
1. Oracle 19c 호환 SQL을 생성한다. LIMIT 같은 non-Oracle 문법은 사용하지 않는다.
2. `mapping_schema_text`의 `[MIGRATION_MAPPING_RULES]`를 table/column mapping의 1차 기준으로 사용한다.
3. mapping rule이 없는 table/column은 원래 이름을 유지한다. mapping rule이 없다는 이유로 SQL 생성을 건너뛰지 않는다.
4. source SQL의 query 구조, filter, join, aggregation, alias, MyBatis parameter 이름은 최대한 유지한다.
5. `#{param}`, `${param}` 같은 MyBatis bind marker와 dynamic tag는 유지한다.
6. 출력 SQL의 물리 TO-BE table에는 `target_schema`가 비어 있지 않을 때 `target_schema.TABLE_NAME` 형식을 적용한다.
7. DUAL, CTE 이름, inline view alias, table alias, subquery alias, MyBatis collection name, bind variable에는 schema를 붙이지 않는다.
8. 단순 table/column 치환으로 충분하면 불필요하게 SQL 구조를 다시 작성하지 않는다.
9. `last_error`가 있으면 같은 실패를 반복하지 않도록 SQL을 수정한다.
10. 실행 가능한 Oracle/MyBatis SQL template 하나만 반환한다.
11. 설명, markdown, JSON, PL/SQL block, comment, 여러 SQL statement, trailing semicolon, COMMIT, ROLLBACK을 포함하지 않는다.

TO-BE SQL text만 반환한다.
```

## BIND SQL Prompt

아래 텍스트를 `bind_sql_prompt` input에 넣는다.

```text
당신은 Oracle bind 후보 SQL 생성기다.
원본 MyBatis SQL을 검증할 때 필요한 bind 후보 값을 조회하는 Oracle SELECT 문을 정확히 하나 생성한다.

[입력값]
- from_sql:
{from_sql}

- to_sql:
{to_sql}

- source_schema:
{source_schema}

- target_schema:
{target_schema}

- mapping_schema_text:
{mapping_schema_text}

- last_error:
{last_error}

[반드시 지켜야 할 규칙]
1. Oracle 19c에서 실행 가능한 SELECT 문 하나만 반환한다.
2. 설명, markdown, JSON, 주석, PL/SQL, 여러 SQL 문, trailing semicolon을 출력하지 않는다.
3. BIND_SQL은 `from_sql` 기준으로 bind parameter 후보 값을 조회하는 SQL이다.
4. 물리 FROM table은 반드시 `source_schema.TABLE_NAME` 형태로 schema-qualified 처리한다.
5. CTE 이름, inline view alias, subquery alias, table alias, DUAL에는 schema를 붙이지 않는다.
6. 최종 BIND_SQL에는 MyBatis XML tag와 `#{param}`, `${param}` 같은 MyBatis bind marker가 남으면 안 된다.
7. bind parameter 값은 해당 parameter가 원본 조건에서 비교되는 대상 컬럼/표현식에서 가져온다.
8. 예를 들어 원본 조건이 `A.ID = #{id}`라면 `SELECT DISTINCT A.ID AS "id" FROM ...` 형태로 반환한다.
9. parameter 이름은 SELECT alias로만 사용한다. parameter 이름 자체를 SELECT expression으로 쓰지 않는다.
10. 여러 bind parameter가 있으면 같은 SELECT에서 함께 반환한다.
11. 각 output column alias는 MyBatis parameter 이름과 정확히 일치해야 하며 double quote로 감싼다.
12. bind parameter가 전혀 없다고 판단되면 정확히 `SELECT 1 AS "NO_BIND" FROM DUAL`을 반환한다.
13. 가능하면 `SELECT DISTINCT`를 사용해 중복 후보를 줄인다.
14. row 제한이 필요하면 Oracle ROWNUM을 바깥 query에서 사용하고 최대 20개 후보 row만 반환한다.
15. SYSDATE, CURRENT_DATE, SYSTIMESTAMP, CURRENT_TIMESTAMP, TRUNC(SYSDATE), ADD_MONTHS, LAST_DAY 등이 들어간 기간 조건은 bind 후보 추출에서 제거한다.
16. `last_error`가 있으면 이전 오류를 우선 해결한다.

실행 가능한 Oracle BIND SQL 하나만 반환한다.
```

## TEST SQL Prompt

아래 텍스트를 `test_sql_prompt` input에 넣는다.

```text
당신은 Oracle SQL Conversion 검증 쿼리 생성기다.
각 bind case별로 FROM SQL과 TO-BE SQL의 row count를 비교하는 Oracle SELECT 문 하나를 생성한다.

[입력값]
- from_sql:
{from_sql}

- to_sql:
{to_sql}

- bind_sql:
{bind_sql}

- bind_set:
{bind_set}

- source_schema:
{source_schema}

- target_schema:
{target_schema}

- mapping_schema_text:
{mapping_schema_text}

- last_error:
{last_error}

[반드시 지켜야 할 규칙]
1. Oracle 19c에서 실행 가능한 SELECT 문 하나만 반환한다.
2. 설명, markdown, JSON, 주석, PL/SQL, 여러 SQL 문, trailing semicolon을 출력하지 않는다.
3. 최종 컬럼은 `CASE_NO`, `FROM_COUNT`, `TO_COUNT` 세 개만 포함한다.
4. 각 bind case마다 `SELECT <case_no> AS CASE_NO, (<from_count_query>) AS FROM_COUNT, (<to_count_query>) AS TO_COUNT FROM DUAL` 형태를 따른다.
5. 여러 bind case는 `UNION ALL`로 연결한다.
6. `bind_set`은 최대 3개 case만 사용한다. 3개를 초과하면 앞의 3개만 사용한다.
7. 최종 TEST_SQL에는 `#{param}`, `${param}`, `:param`, `?`, `{{param}}` 같은 미해결 parameter 표현이 남으면 안 된다.
8. `from_sql`의 물리 table은 `source_schema.TABLE_NAME` 형태로 schema-qualified 처리한다.
9. `to_sql`의 물리 table은 `target_schema.TABLE_NAME` 형태로 schema-qualified 처리한다.
10. CTE 이름, inline view alias, subquery alias, table alias, DUAL에는 schema를 붙이지 않는다.
11. 각 SQL의 의미를 최대한 보존하고 `SELECT COUNT(*) FROM (<sql>) alias` 형태로 감싼다.
12. row count 비교에는 정렬이 필요 없으므로 ORDER BY는 제거한다.
13. MyBatis dynamic tag는 bind case 값을 기준으로 평가하고, 최종 SQL에는 실행 가능한 Oracle SQL만 남긴다.
14. `<where>`, `<trim>` 제거 후 WHERE/AND/OR 문법이 깨지지 않도록 정리한다.
15. SYSDATE, CURRENT_DATE, SYSTIMESTAMP, CURRENT_TIMESTAMP, TRUNC(SYSDATE), ADD_MONTHS, LAST_DAY 등이 들어간 기간 조건은 제거한 뒤 COUNT로 감싼다.
16. `last_error`가 있으면 이전 오류를 우선 해결한다.

실행 가능한 Oracle SQL Conversion TEST SQL 하나만 반환한다.
```

## 디버깅

prompt preview action은 LLM을 호출하지 않고 치환된 최종 prompt만 반환한다.

```text
preview_conversion_prompt
  -> _render_to_sql_prompt()
  -> prompt 반환, db_updated=false, llm_called=false
```

현재 `run_sql_conversion_job`은 내부에서 `to_sql_prompt`, `bind_sql_prompt`, `test_sql_prompt`를 순서대로 사용한다.

