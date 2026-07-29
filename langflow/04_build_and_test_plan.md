# Build and Test Plan

Langflow 웹 UI에서만 개발한다는 제약을 기준으로 한 구현 순서다.

## 0. 전제

- Custom Python Component 사용 가능해야 한다.
- `LANGFLOW_ALLOW_CUSTOM_COMPONENTS`가 false이면 안 된다.
- Oracle 접속 패키지 `oracledb`가 Langflow runtime에 설치되어 있어야 한다.
- LLM 호출에 필요한 API key/base URL/model 설정이 있어야 한다.
- Migration Agent용 Oracle 테이블이 생성되어 있어야 한다.

필수 테이블:

```text
NEXT_MIG_INFO
NEXT_MIG_INFO_DTL
NEXT_MIG_LOG
MIGRATION_LOG_SEQ
```

권장 공용 테이블:

```text
NEXT_SQL_INFO
NEXT_SQL_LOG
NEXT_MIG_RAG_INFO
AG_AGENT_RUN_METRICS
```

## 1. Migration Command Tool 단독 테스트

Langflow Custom Component 하나를 만든다.

처음 구현 action:

```text
status
list_pending
reset
save_user_sql
```

테스트 입력:

```json
{"action":"status","map_id":101}
```

성공 기준:

```text
NEXT_MIG_INFO에서 row를 조회하고 JSON으로 반환한다.
```

## 2. DB Migration Agent 연결

Flow:

```text
Chat Input
-> DB Migration Agent
-> Migration Command Tool
-> Chat Output
```

테스트 문장:

```text
MAP_ID 101 상태 조회해줘
```

성공 기준:

```text
Agent가 command_json={"action":"status","map_id":101} 형태로 Tool을 호출한다.
```

## 3. save_user_sql 테스트

사용자 입력:

```text
MAP_ID 101에 이 MIG SQL과 VERIFY SQL 저장해줘: ...
```

성공 기준:

```text
NEXT_MIG_INFO.USER_EDITED='Y'
NEXT_MIG_INFO.MIG_SQL 저장
NEXT_MIG_INFO.VERIFY_SQL 저장
```

## 4. run_migration_job 구현

이 단계에서 기존 Python graph 로직을 Custom Component 내부 함수로 이식한다.

처음에는 retry 없이 단순 happy path만 구현한다.

```text
_load_job
_load_details
_fetch_ddl
_generate_migration_sql
_save_generated_sql
_execute_migration_sql
_execute_verify_sql
_mark_pass_or_fail
_write_log
```

성공 기준:

```text
MAP_ID 101 실행 시 대상 테이블에 데이터가 들어가고 STATUS='PASS'가 저장된다.
```

## 5. retry와 실패 상태 추가

추가할 상태:

```text
FAIL-TRUNCATE
FAIL-INSERT
FAIL-TEST
SKIP
WAITING
PASS
```

성공 기준:

```text
실패 stage별로 상태가 구분되어 NEXT_MIG_INFO와 NEXT_MIG_LOG에 저장된다.
```

## 6. Supervisor Agent 추가

Flow:

```text
Chat Input
-> Supervisor Agent
   -> DB Migration Agent as Tool
      -> Migration Command Tool
-> Chat Output
```

테스트 문장:

```text
101번 마이그레이션 실행해줘
```

성공 기준:

```text
Supervisor가 DB Migration Agent를 호출하고, DB Migration Agent가 Migration Command Tool을 호출한다.
```

## 추천 검증 순서

1. Tool 직접 실행으로 `status` 검증
2. Tool 직접 실행으로 `reset` 검증
3. Tool 직접 실행으로 `save_user_sql` 검증
4. DB Migration Agent 자연어 라우팅 검증
5. `run_migration_job` happy path 검증
6. 실패 케이스 검증
7. Supervisor 연결

## 하지 말아야 할 것

- `Generate SQL Tool`, `Execute SQL Tool`, `Verify SQL Tool`처럼 내부 단계를 잘게 Tool로 나누지 않는다.
- Agent에게 `mig_sql`, `verify_sql`, `last_error`, `retry_count` 운반을 맡기지 않는다.
- Supervisor부터 만들지 않는다.
- DB 비밀번호를 tool_mode input으로 열지 않는다.
- 한번에 SQL Conversion/Tuning/Formatting까지 확장하지 않는다.
