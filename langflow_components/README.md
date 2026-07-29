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
{"action":"reset","map_id":101}
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
