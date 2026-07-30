# SQL Conversion Command Tool 사용법

파일: `langflow_components/sql_conversion_command_tool.py`

Langflow 웹 UI에서 Custom Python Component를 만든 뒤 코드를 붙여 넣는다.

## 지원 action

```json
{"action":"test_connection"}
```

```json
{"action":"list_pending","limit":5}
```

```json
{"action":"status","row_id":"AAAR..."}
```

```json
{"action":"status","space_nm":"SFA","sql_id":"selectUser"}
```

```json
{"action":"generate_to_sql_text","row_id":"AAAR..."}
```

`generate_to_sql_text`는 기본 preview 전용이다. DB에 저장하지 않고 생성 결과만 반환한다.

저장이 필요하면 사용자 확인 후에만 실행한다.

```json
{"action":"generate_to_sql_text","row_id":"AAAR...","save":true,"confirm":true}
```

저장 시 `TO_SQL_TEXT`, `LOG`, `UPD_TS`만 업데이트한다. `STATUS_CONVERSION`은 아직 변경하지 않는다.

## 주요 input

```text
db_host
db_port
db_service_name
db_username
db_password

llm_provider
llm_base_url
llm_api_key
llm_model
llm_max_tokens
llm_timeout_seconds=900

to_sql_prompt
system_schema
target_schema
```

DB/LLM 연결 방식은 `Migration Command Tool`과 동일하다.

## TO SQL Prompt 예시

`to_sql_prompt` input에 넣는다.

```text
You are an Oracle/MyBatis SQL migration generator.
Generate one executable Oracle/MyBatis TO-BE SQL statement that preserves the source result set while following TO-BE schema mappings.

[Inputs]
- from_sql:
{from_sql}

- target_schema:
{target_schema}

- mapping_schema_text:
{mapping_schema_text}

- correct_sql_hint_json:
{correct_sql_hint_json}

- last_error:
{last_error}

[Rules]
1. Generate or fix every SQL statement for Oracle 19c syntax.
2. Treat mapping_schema_text as the primary source of table and column mapping truth.
3. If a source table or column has no matching mapping rule, keep the original table or column name unchanged.
4. Preserve the original query structure, filters, aggregation, joins, aliases, MyBatis dynamic tags, and bind parameter names whenever possible.
5. Do not remove or replace MyBatis bind markers such as #{param} or ${param}.
6. Keep existing MyBatis dynamic tags such as <if>, <choose>, <when>, <otherwise>, <where>, <trim>, and <foreach>.
7. Every physical TO-BE table in the output SQL must use target_schema.TABLE_NAME format.
8. Do not add target_schema to DUAL, CTE names, inline view aliases, table aliases, or subquery aliases.
9. Return only one executable Oracle/MyBatis SQL template.
10. Do not include explanations, markdown, JSON, PL/SQL blocks, multiple SQL statements, or a trailing semicolon.
11. Use correct_sql_hint_json only as hints. Current from_sql, mapping_schema_text, target_schema, and last_error take priority.

Return only the TO-BE SQL text.
```
