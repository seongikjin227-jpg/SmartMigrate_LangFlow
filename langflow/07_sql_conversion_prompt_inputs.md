# SQL Conversion Prompt Inputs

공통 placeholder:
- `{from_sql}`: 변환 대상 AS-IS SQL. `EDIT_FR_SQL`이 있으면 먼저 사용하고, 없으면 `FR_SQL_TEXT`를 사용한다.
- `{to_sql_text}`: 검증할 TO-BE SQL. `preview_verify_prompt`에서 command_json으로 받은 값을 우선 사용하고, 없으면 `NEXT_SQL_INFO.TO_SQL_TEXT`를 사용한다.
- `{mapping_schema_text}`: `NEXT_SQL_INFO.TARGET_TABLE`에서 찾은 FR_TABLE 기준의 `NEXT_MIG_INFO`/`NEXT_MIG_INFO_DTL` mapping rule과 `NEXT_MIG_RAG_INFO` SQL conversion guidance.
- `{source_schema}`: Langflow component에 설정한 선택 AS-IS source schema hint.
- `{target_schema}`: Langflow component에 설정한 선택 TO-BE target schema prefix.
- `{correct_sql_hint_json}`: 이전에 검증된 SQL 수정 예시를 담는 JSON hint array. 현재 첫 버전에서는 빈 배열을 전달한다.
- `{last_error}`: 재시도 시 이전 에러 텍스트. 첫 생성에서는 `None`을 전달한다.

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

- correct_sql_hint_json:
{correct_sql_hint_json}

- last_error:
{last_error}

[반드시 지켜야 할 규칙]
1. Oracle 19c 호환 SQL을 생성한다. LIMIT 같은 non-Oracle 문법은 사용하지 않는다.
2. `[MIGRATION_MAPPING_RULES]`를 table과 column mapping의 1차 기준으로 사용한다.
3. `[UNMAPPED_FR_TABLES]`에 있는 table은 mapping rule이 없는 대상이다. 이 table과 column은 원래 이름을 유지한다.
4. mapping rule이 없으면 원래 table명 또는 column명을 유지하고, 그 이유만으로 SQL 생성을 건너뛰지 않는다.
5. `NEXT_MIG_RAG_INFO` guidance는 pattern hint로만 사용한다. 현재 SQL과 충돌하면 복사하지 않는다.
6. `#{param}`, `${param}` 같은 MyBatis bind marker와 dynamic tag는 유지한다.
7. 출력 SQL의 물리 TO-BE table에는 `target_schema`가 비어 있지 않을 때 `target_schema.TABLE_NAME` 형식을 적용한다.
8. DUAL, CTE 이름, alias, subquery alias, MyBatis collection name, bind variable에는 `target_schema`를 붙이지 않는다.
9. 단순 table/column 치환으로 충분하면 불필요하게 SQL 구조를 다시 작성하지 않는다.
10. `last_error`가 있으면 같은 실패를 반복하지 않도록 SQL을 수정한다.
11. 실행 가능한 Oracle/MyBatis SQL template 하나만 반환한다.
12. 설명, markdown, JSON, PL/SQL block, comment, 여러 SQL statement, trailing semicolon, COMMIT, ROLLBACK을 포함하지 않는다.

TO-BE SQL text만 반환한다.
```

## VERIFY SQL Prompt

아래 텍스트를 `verify_sql_prompt` input에 넣는다.

```text
당신은 Oracle/MyBatis SQL conversion reviewer다.
AS-IS SQL과 TO-BE SQL이 같은 result set을 반환하는지 검증하고, 위험한 차이를 짧게 정리한다.

[입력값]
- from_sql:
{from_sql}

- to_sql_text:
{to_sql_text}

- source_schema:
{source_schema}

- target_schema:
{target_schema}

- mapping_schema_text:
{mapping_schema_text}

- correct_sql_hint_json:
{correct_sql_hint_json}

- last_error:
{last_error}

[검증 기준]
1. SELECT list의 의미, row cardinality, join 조건, filter 조건, group by, order by, window function, null 처리, 날짜/문자 함수 변환이 유지되는지 확인한다.
2. mapping rule 기준으로 source table/column이 올바른 target table/column으로 바뀌었는지 확인한다.
3. MyBatis bind marker와 dynamic tag가 깨지지 않았는지 확인한다.
4. `target_schema`가 필요한 물리 TO-BE table에 적용되었는지 확인한다.
5. mapping rule이 없어서 유지된 table/column은 오류로 단정하지 말고, 확인 필요 항목으로 분리한다.
6. `last_error`가 있으면 그 에러가 해결되었는지 확인한다.

[출력 형식]
아래 JSON 하나만 반환한다.

{
  "ok": true 또는 false,
  "summary": "검증 요약",
  "risks": ["위험 또는 확인 필요 항목"],
  "recommended_fix": "필요한 수정 SQL 또는 수정 방향. 없으면 빈 문자열"
}
```

## 디버깅

prompt preview action은 LLM을 호출하지 않고 치환된 최종 prompt만 반환한다.

```text
preview_conversion_prompt
  -> _render_to_sql_prompt()
  -> prompt 반환, db_updated=false, llm_called=false

preview_verify_prompt
  -> _render_verify_prompt()
  -> prompt 반환, db_updated=false, llm_called=false
```
