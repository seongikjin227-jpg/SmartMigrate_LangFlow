# SQL Conversion Command Tool Guide

File: `langflow_components/sql_conversion_command_tool.py`

Create a Langflow Custom Python Component and paste the Python source from this file.

## Job Identifier

SQL conversion jobs are identified by this pair:

```json
{"space_nm":"SFA","sql_id":"selectUser"}
```

Do not ask users for `row_id`. `row_id` is hard to identify in conversation and should not be used as the command key.

## First Commands

```json
{"action":"test_connection"}
```

```json
{"action":"list_pending","limit":5}
```

`limit`을 생략하면 기본 20건을 조회한다. 최소 1건, 최대 100건으로 제한된다.

```json
{"action":"status","space_nm":"SFA","sql_id":"selectUser"}
```

```json
{"action":"generate_to_sql_text","space_nm":"SFA","sql_id":"selectUser"}
```

`generate_to_sql_text` is preview-only by default. It returns generated SQL without updating DB.

Generated SQL is returned to chat only. It is not written to `NEXT_SQL_INFO.TO_SQL_TEXT`.

```json
{"action":"preview_conversion_prompt","space_nm":"SFA","sql_id":"selectUser"}
```

```json
{"action":"preview_verify_prompt","space_nm":"SFA","sql_id":"selectUser","to_sql_text":"SELECT ..."}
```

## Supported Actions

| action | Description |
| --- | --- |
| `test_connection` | Runs DB `SELECT 1 FROM DUAL` and an LLM smoke test. |
| `list_pending` | Lists `NEXT_SQL_INFO` rows that need `TO_SQL_TEXT`. |
| `status` | Looks up one SQL conversion job by `space_nm` + `sql_id`. |
| `generate_to_sql_text` | Generates TO-BE SQL and returns it without updating DB. |
| `preview_conversion_prompt` | Returns the final TO-BE SQL generation prompt without calling LLM. |
| `preview_verify_prompt` | Returns the final conversion verification prompt without calling LLM. |

## Component Inputs

```text
db_host
db_port
db_service_name
db_username
db_password

llm_base_url
llm_api_key
llm_model
llm_max_tokens
llm_timeout_seconds=900

to_sql_prompt
verify_sql_prompt
system_schema
source_schema
target_schema
```

DB and LLM connection behavior matches `Migration Command Tool`.
LLM calls use OpenAI-compatible `/chat/completions` only.

## Prompt Input

Use this file for the `to_sql_prompt` input:

```text
langflow/07_sql_conversion_prompt_inputs.md
```

Supported placeholders:

```text
{from_sql}
{to_sql_text}
{mapping_schema_text}
{source_schema}
{target_schema}
{correct_sql_hint_json}
{last_error}
```

The component replaces these placeholders internally before calling the LLM.
`source_schema` is an AS-IS source table matching hint. It is not applied to TO-BE output SQL.

## DB Update Policy

- `test_connection`, `list_pending`, and `status` do not update DB.
- `generate_to_sql_text` does not update DB.
- `preview_conversion_prompt` and `preview_verify_prompt` do not update DB and do not call LLM.
- `generate_to_sql_text` ignores DB save flow; `run_sql_conversion` will handle final persistence when it is implemented.

## Current Scope

Implemented:

- `TO_SQL_TEXT` generation preview
- conversion prompt preview
- verification prompt preview
- mapping context lookup from `NEXT_SQL_INFO.TARGET_TABLE` -> `NEXT_MIG_INFO.FR_TABLE`
- SQL conversion RAG lookup from `NEXT_MIG_RAG_INFO` where `CATEGORY='SQL_CONVERSION'` and `SOURCE_TABLES` matches the FR_TABLE hint

Not implemented yet:

- bind SQL generation
- test SQL generation
- tuning SQL generation
- full SQL conversion run/retry loop
- final save to `NEXT_SQL_INFO.TO_SQL_TEXT`
- `NEXT_SQL_LOG` detailed writes
