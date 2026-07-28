# Migration Command Tool 사용법

파일: `langflow_components/migration_command_tool.py`

Langflow 웹 UI에서 Custom Python Component를 만든 뒤, 이 파일의 코드를 붙여 넣는다.

## 먼저 테스트할 command

```json
{"action":"list_pending","limit":5}
```

```json
{"action":"status","map_id":101}
```

```json
{"action":"reset","map_id":101}
```

```json
{"action":"run_migration_job","map_id":101}
```

## 지원 action

| action | 설명 |
| --- | --- |
| `list_pending` | 대기 중인 migration job 목록 조회 |
| `status` | 특정 map_id 상태/상세 매핑 조회 |
| `reset` | 특정 map_id 재실행 가능 상태로 초기화 |
| `save_user_sql` | 사용자가 수정한 MIG_SQL/VERIFY_SQL 저장 |
| `analyze_failure` | 최근 실패 로그와 저장 SQL 조회 |
| `run_migration_job` | 매핑 정보 기반 SQL 생성, 실행, 검증, 상태 저장 |

## 현재 run_migration_job 동작

이 버전은 LLM으로 SQL을 만들지 않는다. `NEXT_MIG_INFO_DTL` 매핑을 기준으로 deterministic SQL을 만든다.

```sql
INSERT INTO TO_TABLE (TO_COL...)
SELECT FR_COL...
FROM FR_TABLE
```

검증 SQL은 source count와 target count 차이가 0인지 확인한다.

```sql
SELECT ABS((SELECT COUNT(*) FROM source) - (SELECT COUNT(*) FROM target)) AS DIFF FROM DUAL
```

LLM 기반 SQL 생성은 이 deterministic 실행이 검증된 다음 별도 확장하는 것을 권장한다.

## Langflow Tool Mode

`command_json`만 `tool_mode=True`다. DB 접속 정보는 Advanced input으로 두고 Agent가 건드리지 않게 한다.

DB 접속 정보 예시:

```text
db_dsn=localhost:1521/xe
db_user=scott
db_password=tiger
system_schema=
source_schema=
target_schema=
```
