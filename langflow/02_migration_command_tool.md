# Migration Command Tool 설계

`Migration Command Tool`은 Langflow Custom Python Component로 만든다. DB Migration Agent가 사용하는 가장 낮은 실행 Tool이다.

## Tool의 책임

이 Tool은 실제 migration 작업을 수행한다.

```text
DB 조회
DDL 조회
MIG SQL 생성
MIG SQL 저장
MIG SQL 실행
VERIFY SQL 생성
VERIFY SQL 저장
VERIFY SQL 실행
상태 저장
로그 저장
실패 분석 조회
재실행 초기화
사용자 수정 SQL 저장
```

## Tool input 원칙

Tool Mode input을 많이 만들지 않는다. Agent가 조작해야 하는 값만 입력으로 열어둔다.

권장 입력은 `command_json` 하나다.

```json
{
  "action": "status",
  "map_id": 101
}
```

DB 접속 정보, LLM 설정 등은 advanced input 또는 Langflow global variable로 둔다.

## 지원 action

| action | 필수 입력 | 역할 |
| --- | --- | --- |
| `status` | `map_id` | migration job 현재 상태 조회 |
| `reset` | `map_id` | 재실행 가능 상태로 초기화 |
| `save_user_sql` | `map_id`, `mig_sql`, `verify_sql` | 사용자가 수정한 SQL 저장 |
| `run_migration_job` | `map_id` | migration job 하나를 끝까지 실행 |
| `analyze_failure` | `map_id` | 최근 실패 로그와 SQL 조회 |
| `list_pending` | 없음 또는 `limit` | 대기 중인 migration job 목록 조회 |

## command_json 예시

```json
{"action":"status","map_id":101}
```

```json
{"action":"run_migration_job","map_id":101,"force_rerun":false}
```

```json
{"action":"save_user_sql","map_id":101,"mig_sql":"INSERT INTO ...","verify_sql":"SELECT COUNT(*) ..."}
```

```json
{"action":"analyze_failure","map_id":101}
```

## 반환 포맷

항상 JSON 형태로 반환한다.

성공:

```json
{
  "ok": true,
  "action": "run_migration_job",
  "map_id": 101,
  "status": "PASS",
  "message": "Migration completed",
  "elapsed_seconds": 12,
  "retry_count": 0
}
```

실패:

```json
{
  "ok": false,
  "action": "run_migration_job",
  "map_id": 101,
  "status": "FAIL-INSERT",
  "error": "ORA-00001 unique constraint violated",
  "next_recommendation": "Review generated MIG_SQL or save corrected SQL."
}
```

## 내부 함수 구조

`run_migration_job`은 내부적으로 다음 함수들을 호출한다.

```text
_run_migration_job(map_id, command)
  -> _load_job(map_id)
  -> _load_details(map_id)
  -> _check_dependency(job)
  -> _fetch_source_target_ddl(job)
  -> _generate_migration_sql(job, ddl, last_error)
  -> _save_generated_sql(map_id, mig_sql, verify_sql)
  -> _execute_migration_sql(mig_sql, trunc_yn, to_table)
  -> _execute_verify_sql(verify_sql)
  -> _finalize_success_or_failure(map_id, result)
  -> _write_log(map_id, ...)
```

이 내부 함수들은 Langflow 노드로 나누지 않는다. 모두 Custom Component 안에 둔다.

## 기존 소스와의 매핑

| 기존 코드 | Langflow Custom Component 내부 함수 |
| --- | --- |
| `repository.get_pending_jobs` | `_list_pending`, `_load_job` |
| `fetch_table_ddl_node` | `_fetch_source_target_ddl` |
| `check_dependency_node` | `_check_dependency` |
| `generate_sql_node` | `_generate_migration_sql` |
| `execute_sql_node` | `_execute_migration_sql` |
| `verify_sql_node` | `_execute_verify_sql` |
| `finalize_node` | `_finalize_success_or_failure` |
| `history_repository.log_business_history` | `_write_log` |

## Tool Mode 설정

Custom Component에서 Agent가 채울 입력만 `tool_mode=True`로 둔다.

```python
MessageTextInput(
    name="command_json",
    display_name="Command JSON",
    required=True,
    tool_mode=True,
)
```

DB 접속 정보는 `tool_mode=True`로 두지 않는다. Agent가 DB 비밀번호를 건드리면 안 된다.

## 처음 구현할 최소 버전

처음부터 `run_migration_job`을 만들지 말고 아래 3개부터 구현한다.

```text
status
reset
save_user_sql
```

이 3개가 정상 동작하면 DB 연결, 입력 parsing, 반환 포맷, Agent Tool 호출이 검증된다. 그 다음 `run_migration_job`을 구현한다.
