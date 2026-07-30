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

```json
{"action":"status","space_nm":"SFA","sql_id":"selectUser"}
```

```json
{"action":"generate_to_sql_text","space_nm":"SFA","sql_id":"selectUser"}
```

`generate_to_sql_text` is preview-only by default. It returns generated SQL without updating DB.

Save only after explicit user confirmation:

```json
{"action":"generate_to_sql_text","space_nm":"SFA","sql_id":"selectUser","save":true,"confirm":true}
```

Save updates only `TO_SQL_TEXT`, `LOG`, and `UPD_TS`. It does not update `STATUS_CONVERSION`.

## Supported Actions

| action | Description |
| --- | --- |
| `test_connection` | Runs DB `SELECT 1 FROM DUAL` and an LLM smoke test. |
| `list_pending` | Lists `NEXT_SQL_INFO` rows that need `TO_SQL_TEXT`. |
| `status` | Looks up one SQL conversion job by `space_nm` + `sql_id`. |
| `generate_to_sql_text` | Generates TO-BE SQL. Preview-only unless `save=true` and `confirm=true`. |

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
system_schema
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
{mapping_schema_text}
{target_schema}
{correct_sql_hint_json}
{last_error}
```

The component replaces these placeholders internally before calling the LLM.

## DB Update Policy

- `test_connection`, `list_pending`, and `status` do not update DB.
- `generate_to_sql_text` does not update DB by default.
- `generate_to_sql_text` with only `save=true` returns `CONFIRM_REQUIRED`.
- Actual save requires both `save=true` and `confirm=true`.
- Save does not update `STATUS_CONVERSION`.

## Current Scope

Implemented:

- `TO_SQL_TEXT` generation preview
- optional confirmed save to `NEXT_SQL_INFO.TO_SQL_TEXT`

Not implemented yet:

- bind SQL generation
- test SQL generation
- tuning SQL generation
- full SQL conversion run/retry loop
- `NEXT_SQL_LOG` detailed writes
- prompt preview action
