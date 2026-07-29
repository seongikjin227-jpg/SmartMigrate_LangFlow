# Migration Command Tool 사용법

파일: `langflow_components/migration_command_tool.py`

Langflow 웹 UI에서 Custom Python Component를 만든 뒤, 이 파일의 코드를 붙여 넣는다.

## 먼저 테스트할 command

```json
{"action":"test_connection"}
```

```json
{"action":"list_pending","limit":5}
```

```json
{"action":"status","map_id":101}
```

```json
{"action":"generate_mig_sql","map_id":101}
```

```json
{"action":"generate_verify_sql","map_id":101}
```

```json
{"action":"run_migration_job","map_id":101}
```

## 지원 action

| action | 설명 |
| --- | --- |
| `test_connection` | DB `SELECT 1 FROM DUAL`과 LLM smoke test를 함께 실행 |
| `list_pending` | 대기 중인 migration job 목록 조회 |
| `status` | 특정 map_id 상태/상세 매핑 조회 |
| `get_table_ddl` | `USER_TAB_COLUMNS` 또는 `ALL_TAB_COLUMNS` 기반 테이블 컬럼 메타 조회 |
| `generate_mig_sql` | LLM으로 MIG_SQL preview 생성, DB 저장 없음 |
| `generate_verify_sql` | LLM으로 VERIFY_SQL preview 생성, DB 저장 없음 |
| `reset` | 특정 map_id의 `STATUS`, `RETRY_COUNT`, `BATCH_CNT`를 초기화, SQL은 보존, `confirm=true` 필요 |
| `save_user_sql` | 사용자가 수정한 MIG_SQL/VERIFY_SQL 저장, `confirm=true` 필요 |
| `analyze_failure` | 최근 실패 로그와 저장 SQL 조회 |
| `run_migration_job` | LLM SQL 생성, 저장, 실행, 검증, 상태 저장 전체 사이클 |

## SQL 생성 command

MIG_SQL 생성:

```json
{"action":"generate_mig_sql","map_id":101}
```

VERIFY_SQL 생성:

```json
{"action":"generate_verify_sql","map_id":101}
```

`generate_mig_sql`, `generate_verify_sql`은 preview 전용이다. DB에 SQL을 저장하지 않고 생성 결과만 반환한다.
LLM 생성이 실패하면 fallback 없이 실패한다.

사용자 수정 SQL 보호 정책:

| 상태 | 동작 |
| --- | --- |
| `USER_EDITED=Y`, `MIG_SQL` 있음 | `generate_mig_sql`은 기존 MIG_SQL을 반환하고 새로 생성하지 않음 |
| `USER_EDITED=Y`, `MIG_SQL` 있음, `VERIFY_SQL` 없음 | `generate_verify_sql`만 생성 허용 |
| `USER_EDITED=Y`, `MIG_SQL` 있음, `VERIFY_SQL` 있음 | `generate_verify_sql`은 기존 VERIFY_SQL을 반환하고 새로 생성하지 않음 |
| `USER_EDITED=Y`, `MIG_SQL` 없음 | 생성하지 않고 실패 |

사용자가 명시적으로 재생성을 원할 때만 `force_regenerate=true`를 사용한다.

```json
{"action":"generate_mig_sql","map_id":101,"force_regenerate":true}
```

중요 정책:

- `generate_mig_sql`, `generate_verify_sql`은 DB를 업데이트하지 않고 `USER_EDITED` 값도 변경하지 않는다.
- `USER_EDITED=Y`는 `save_user_sql`로 사용자가 직접 수정 SQL을 저장할 때만 설정한다.
- `PRIOR_MAP_ID`가 있고 선행 작업이 `PASS`가 아니면 SQL 생성도 진행하지 않는다.
- `NEXT_MIG_INFO_DTL.TO_COL`이 비어 있는 매핑은 target insert 컬럼에서 제외한다. 이 값은 스킵되었거나 다른 expression에 합쳐진 컬럼으로 본다.
- LLM 프롬프트는 파일에서 읽지 않는다. Langflow input인 `mig_sql_prompt`, `verify_sql_prompt` 두 개로 받는다.
- `MIG_SQL`에 저장되는 값은 단일 `INSERT` 문이어야 한다.
- `MIG_SQL`에는 `TRUNCATE`, `COMMIT`, `ROLLBACK`, `DELETE`, `UPDATE`, `MERGE`, `DROP`, `ALTER`를 저장하지 않는다.
- `VERIFY_SQL`에 저장되는 값은 단일 `SELECT` 또는 `WITH` 문이어야 한다.
- SQL 값 끝의 세미콜론은 제거해서 저장한다.

프롬프트 input에 넣을 텍스트는 `langflow/06_migration_prompt_inputs.md`를 참고한다.

## 현재 run_migration_job 동작

`run_migration_job`은 LLM 기반 전체 migration 사이클을 실행한다.

```json
{"action":"run_migration_job","map_id":101}
```

실행 순서:

1. job 상태, `USE_YN`, `PRIOR_MAP_ID` 확인
2. `USER_EDITED=Y`이면 기존 `MIG_SQL` 보존
3. `USER_EDITED!=Y`이면 `generate_mig_sql` 실행
4. 내부 실행 helper로 `MIG_SQL` 실행
5. `USER_EDITED=Y`이고 `VERIFY_SQL`이 있으면 기존 SQL 보존
6. 그 외에는 `generate_verify_sql` 실행
7. 내부 검증 helper로 `VERIFY_SQL` 실행
8. 실패 시 DB `STATUS`를 바로 저장하지 않고 retry loop 내부에서 재생성/재실행
9. 최종 성공/실패가 확정되면 `PASS`, `FAIL-INSERT`, `FAIL-TEST`를 DB에 저장

Retry 정책:

- 내부 retry는 `run_migration_job`에서만 수행한다.
- 개별 preview action인 `generate_mig_sql`, `generate_verify_sql`은 retry 없이 1회만 수행한다.
- retry 중간 실패는 `NEXT_MIG_LOG`에 `ROW_ERROR`로 기록한다.
- retry 중간에는 `NEXT_MIG_INFO.STATUS`를 업데이트하지 않는다.
- 최대 시도 초과 또는 최종 성공 시에만 `NEXT_MIG_INFO.STATUS`와 `RETRY_COUNT`를 저장한다.
- `FAIL-INSERT`이면 다음 attempt에서 `MIG_SQL`을 다시 생성하고 다시 실행한다.
- `FAIL-TEST`이면 `MIG_SQL`은 다시 실행하지 않고 `VERIFY_SQL`만 다시 생성하고 검증한다.

LLM 생성이 실패하면 전체 migration은 중단된다. fallback SQL 생성은 사용하지 않는다.

## 확인이 필요한 DB 변경 command

사용자 수정 SQL 저장:

```json
{"action":"save_user_sql","map_id":101,"mig_sql":"INSERT ...","verify_sql":"SELECT ...","confirm":true}
```

`confirm=true`가 없으면 실행하지 않는다. 이 action은 `USER_EDITED='Y'`를 설정한다.

Reset:

```json
{"action":"reset","map_id":101,"confirm":true}
```

`confirm=true`가 없으면 실행하지 않는다.
`reset`은 `MIG_SQL`, `VERIFY_SQL`, `USER_EDITED`를 변경하지 않는다. `STATUS=NULL`, `RETRY_COUNT=0`, `BATCH_CNT=0`만 저장한다.

## Langflow Tool Mode

`command_json`만 `tool_mode=True`다. DB/LLM 접속 정보는 Langflow 화면에서 사람이 직접 입력하고, Agent가 `command_json`으로 건드리지 않게 한다.

DB 접속 정보 예시:

```text
db_host=10.10.10.10 또는 db.company.local
db_port=1521
db_service_name=ORCLPDB1
db_username=scott
db_password=tiger
system_schema=
source_schema=
target_schema=
```


## DB 연결 방식

컴포넌트는 LangChain `SQLDatabase`를 사용한다.

```python
connection_string = "oracle+oracledb://user:pass@host:port/service"
db = SQLDatabase.from_uri(connection_string)
```

동일 DB 입력값은 cache key로 재사용한다.

```text
cache_key = host|port|service_name|username
```

SELECT 계열 조회는 `db.run(query, include_columns=True)` 패턴을 사용한다. UPDATE/INSERT/TRUNCATE처럼 commit과 rowcount가 필요한 작업은 같은 cached `SQLDatabase`의 SQLAlchemy engine transaction을 사용한다.

## 런타임 패키지 설치 옵션

Langflow 런타임에 필요한 패키지가 없으면 DB 연결 전에 오류가 난다.
필요 패키지:

```text
langchain-community
SQLAlchemy
oracledb
```

사내망에서 Langflow 런타임이 패키지를 직접 설치해야 하는 경우에만 `Auto Install Missing Packages=true`로 켠다.

```text
auto_install_packages=true
pip_trusted_host=사내 PyPI/proxy host 또는 URL
```

주의: `pip --trusted-host`는 URL 전체가 아니라 host 값을 기대한다. 컴포넌트는 URL을 입력해도 hostname만 추출해서 사용한다.

내부 설치 패턴:

```python
subprocess.check_call([
    sys.executable,
    "-m",
    "pip",
    "install",
    package,
    "--trusted-host",
    trusted_host,
])
```

## 기존 소스 코드의 DB 접속 방식

기존 `0609_final-main` 소스는 `oracledb.connect()` 직접 연결을 사용했다.

```python
dsn = f"{DB_HOST}:{DB_PORT}/{DB_SID}"
connection = oracledb.connect(user=DB_USER, password=DB_PASS, dsn=dsn)
```

Langflow Custom Component 버전은 회사 표준에 맞춰 SQLAlchemy URL을 만들고 `SQLDatabase.from_uri()`로 연결한다.

```python
connection_string = "oracle+oracledb://user:pass@host:port/service"
db = SQLDatabase.from_uri(connection_string)
```

조회는 `db.run(query, include_columns=True)`를 사용하고, 쓰기/커밋이 필요한 작업은 같은 cached `SQLDatabase`의 SQLAlchemy engine을 재사용한다.

## LLM 입력값

기존 `.env.example`의 LLM 설정을 Langflow input으로 옮긴다.

```text
llm_provider=openai 또는 anthropic
llm_base_url=사내 LLM gateway URL
llm_api_key=LLM API Key
llm_model=claude-haiku-4-5-20251001 또는 사내 모델명
llm_max_tokens=4096
```

`test_connection`은 DB와 LLM을 모두 점검한다.

```json
{"action":"test_connection"}
```

반환 예시:

```json
{
  "ok": true,
  "db": {"ok": true, "message": "DB connection OK"},
  "llm": {"ok": true, "provider": "openai", "model": "..."}
}
```

LLM provider 동작:

| provider | 호출 방식 |
| --- | --- |
| `openai` | OpenAI-compatible `/chat/completions` |
| `anthropic` | Anthropic `/v1/messages` |

## DDL 조회 command

현재 접속 계정 기준:

```json
{"action":"get_table_ddl","table_name":"NEXT_MIG_INFO"}
```

스키마 지정:

```json
{"action":"get_table_ddl","schema":"SFAADM","table_name":"NEXT_MIG_INFO"}
```

또는:

```json
{"action":"get_table_ddl","table_name":"SFAADM.NEXT_MIG_INFO"}
```
