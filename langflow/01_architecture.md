# SmartMigration LangFlow 개발 구조

이 문서는 기존 SmartMigration Python 소스 코드를 LangFlow IDE 기반으로 옮기면서 개발 구조를 어떻게 바꿨는지 설명한다.

초점은 화면 구조가 아니라 코드 구조다.

- 어떤 코드를 합쳤는가
- 어떤 코드를 쪼갰는가
- 왜 import 대신 복사/내장 구조를 택했는가
- 왜 함수명을 LangFlow 친화적으로 바꿨는가
- 기존 서버형 구조와 LangFlow Custom Component 구조의 차이는 무엇인가

## 한 줄 결론

기존 코드는 “서버 프로세스 안에서 package import로 협력하는 구조”였고, LangFlow 버전은 “각 Custom Component가 독립 실행 가능한 command boundary가 되는 구조”다.

```text
기존:
server.agents
server.services
server.repositories
server.tools
server.config
를 import해서 하나의 backend process 안에서 협력

LangFlow:
langflow_components/*.py
각 파일이 LangFlow Custom Component로 독립 로드
각 component가 필요한 DB/LLM/업무 함수를 내부에 보유
```

## 기존 소스 코드의 개발 구조

기존 소스는 일반적인 backend 애플리케이션 구조에 가깝다.

```text
app/
  Streamlit UI

server/
  agents/
    supervisor/
    migration/
    sql_conversion/
    sql_tuning/

  services/
    migration/
    sql/

  repositories/
    DB 접근 계층

  tools/
    Supervisor tool wrapper

  config/
    settings
```

이 구조의 장점은 명확하다.

- 공통 DB 접근 코드를 repository/service로 재사용할 수 있다.
- Agent, Service, Repository 책임이 분리된다.
- backend process가 하나이므로 Python import path가 안정적이다.
- scheduler나 supervisor가 같은 runtime 안에서 계속 살아 있을 수 있다.

하지만 LangFlow Custom Component로 옮기면 단점이 생긴다.

- LangFlow가 component를 개별 파일 단위로 로드한다.
- 기존 `server.*` package import path가 LangFlow runtime에서 항상 보장되지 않는다.
- component를 IDE에서 복사하거나 배포할 때 package dependency가 쉽게 깨진다.
- 여러 custom component가 서로 import하면 LangFlow 배포 단위가 불명확해진다.
- 사용자는 LangFlow 화면에서 component input만 보고 실행하므로 숨은 import 의존성을 추적하기 어렵다.

그래서 LangFlow 버전은 기존 backend layering을 그대로 유지하지 않았다.

## LangFlow 버전의 개발 구조

LangFlow 쪽 핵심 파일은 다음과 같다.

```text
langflow_components/
  dashboard_command_tool.py
  migration_command_tool.py
  sql_conversion_command_tool.py
  batch_agent_command_tool.py
```

각 파일은 LangFlow Custom Component 하나에 대응한다.

```text
DashboardCommandTool
MigrationCommandTool
SqlConversionCommandTool
BatchAgentCommandTool
```

개발 구조상 가장 큰 변화는 “service/repository/agent 계층을 그대로 import하지 않고, command tool 내부 함수로 접는 방식”이다.

```text
기존 구조:
Agent -> Service -> Repository -> DB

LangFlow 구조:
Command Tool class
  -> _load_job()
  -> _build_prompt()
  -> _call_llm()
  -> _execute_sql()
  -> _update_job_status()
  -> _write_log()
```

이렇게 만든 이유는 LangFlow component 하나가 실행 가능한 최소 단위가 되도록 하기 위해서다.

## 왜 Command Tool 하나에 여러 action을 넣었는가

처음 생각할 수 있는 구조는 action마다 component를 따로 만드는 방식이다.

```text
Get DDL Tool
Generate MIG SQL Tool
Execute MIG SQL Tool
Generate VERIFY SQL Tool
Execute VERIFY SQL Tool
Update Status Tool
Write Log Tool
```

이렇게 쪼개면 LangFlow graph는 보기에는 세밀해진다.

하지만 migration 업무는 중간 상태가 강하게 연결되어 있다.

```text
map_id
job row
detail rows
source/target DDL
generated MIG_SQL
generated VERIFY_SQL
last_error
retry_count
failure_status
elapsed_seconds
affected_rows
user_edited
prior_map_id
```

이 값들을 LangFlow edge로 계속 넘기면 문제가 생긴다.

- edge 입력이 많아져 flow가 복잡해진다.
- retry 시 이전 실패 정보와 생성 SQL을 안정적으로 넘기기 어렵다.
- Agent가 중간 값을 잘못 만들거나 누락할 수 있다.
- DB에 이미 저장된 durable state와 LangFlow runtime state가 중복된다.

그래서 action은 command JSON으로 받고, 상태 전이는 Tool 내부에서 처리한다.

```json
{"action":"run_migration_job","map_id":101}
```

Tool 내부는 일반 Python 코드처럼 순차 실행된다.

```text
load job
load details
check dependency
generate SQL
execute SQL
generate verify SQL
execute verify SQL
save final SQL
update status
write logs
retry if needed
```

이 구조가 LangFlow에서는 더 안정적이다.

## 컴포넌트를 너무 쪼개면 생기는 LangFlow 개발 불편

LangFlow IDE에서는 컴포넌트를 잘게 나누는 것이 항상 좋은 구조가 아니다.

기존 Python 코드에서는 함수 호출로 값을 넘기면 된다.

```python
job = load_job(map_id)
details = load_details(map_id)
prompt = build_prompt(job, details)
sql = call_llm(prompt)
save_sql(map_id, sql)
```

하지만 LangFlow에서 이것을 여러 component로 나누면 각 단계가 component input/output으로 연결된다.

```text
Load Job Component
-> Load Detail Component
-> Build Prompt Component
-> LLM Component
-> Save SQL Component
```

이때 다음 문제가 생긴다.

### 1. 컴포넌트마다 같은 변수를 다시 설정해야 한다

DB 접속 정보, schema, LLM endpoint, model, prompt 같은 값은 여러 단계에서 공통으로 필요하다.

컴포넌트를 잘게 쪼개면 각 component마다 같은 input을 반복 설정해야 한다.

```text
db_host
db_port
db_service_name
db_username
db_password
system_schema
source_schema
target_schema
llm_base_url
llm_api_key
llm_model
prompt
```

기존 Python 코드에서는 class instance나 config 객체 하나를 공유하면 된다.

하지만 LangFlow IDE에서는 component마다 input을 다시 연결하거나 다시 입력해야 한다.

이렇게 되면 설정 누락이 쉽게 생긴다.

예:

```text
Generate SQL component에는 source_schema를 넣었는데
Execute SQL component에는 target_schema를 빼먹음
```

이런 오류는 코드 문제가 아니라 flow wiring 문제라서 디버깅이 더 어렵다.

### 2. multi output으로 넘긴 값이 기대처럼 안정적으로 이어지지 않는다

LangFlow component output을 여러 개로 나누면 이론상 구조화된 값을 다음 component로 넘길 수 있다.

```text
output_1 = job
output_2 = details
output_3 = retry_context
output_4 = ddl_info
```

하지만 실제 flow에서는 다음 문제가 생긴다.

- output마다 연결선을 따로 관리해야 한다.
- 일부 output이 비어 있거나 타입이 맞지 않으면 다음 component에서 깨진다.
- 복합 dict/list 값이 다음 component에서 기대한 Python object 그대로 들어오지 않을 수 있다.
- Agent나 Chat node를 거치면 값이 message/text 형태로 바뀌기 쉽다.
- 결국 하나의 message 또는 JSON string으로 넘기고 다시 parsing해야 하는 경우가 생긴다.

즉, Python 함수 호출에서는 자연스러운 값 전달이 LangFlow edge에서는 번거로운 직렬화/역직렬화 문제가 된다.

```text
Python 내부:
dict -> dict
list[dict] -> list[dict]

LangFlow edge:
dict -> Data/Message/text
-> JSON serialize
-> 다음 component에서 JSON parse
```

이 구조가 반복되면 component 간 계약이 복잡해진다.

### 3. 중간 상태를 message로 넘기면 parsing 코드가 늘어난다

multi output이 안정적이지 않거나 Agent/Chat node를 통과해야 하는 경우, 여러 값을 하나의 JSON message로 묶게 된다.

```json
{
  "map_id": 101,
  "job": {},
  "details": [],
  "last_error": "...",
  "retry_count": 1,
  "mig_sql": "..."
}
```

그러면 다음 component는 다시 이 message를 파싱해야 한다.

```python
payload = json.loads(message)
job = payload["job"]
details = payload["details"]
```

이 방식은 결국 Python 함수 인자를 JSON 문자열로 흉내 내는 것이다.

문제:

- parsing 실패 처리 코드가 늘어난다.
- key 이름이 바뀌면 downstream component가 깨진다.
- CLOB/SQL 같은 긴 문자열이 JSON message 안에 들어가면서 escape 문제가 생길 수 있다.
- prompt에 넣을 원문 SQL이 잘리거나 변형되지 않았는지 확인해야 한다.
- 디버깅 시 어느 component에서 값이 변형됐는지 추적하기 어렵다.

### 4. retry loop를 여러 component로 나누면 상태 관리가 불안정해진다

Migration과 SQL Conversion은 실패 시 retry가 중요하다.

retry에는 아래 값이 필요하다.

```text
last_error
last_sql
retry_count
failure_status
generated_sql
elapsed_seconds
```

이 값을 LangFlow node 사이에서 계속 전달하면 flow가 복잡해진다.

반면 Tool 내부 함수로 처리하면 Python local variable로 안정적으로 유지할 수 있다.

```python
for attempt in range(1, max_attempts + 1):
    try:
        ...
    except Exception as exc:
        last_error = str(exc)
        last_sql = generated_sql
        continue
```

그래서 retry가 있는 업무는 component를 잘게 쪼개지 않고 command tool 내부에 둔다.

### 5. 긴 SQL/CLOB 값 전달이 부담스럽다

이 프로젝트는 SQL 원문, 생성 SQL, verify SQL, bind SQL처럼 긴 문자열을 많이 다룬다.

특히 Oracle CLOB은 prompt에 넣을 때 전체 원문이 필요하다.

컴포넌트를 여러 개로 쪼개서 CLOB 값을 edge로 계속 넘기면 다음 부담이 생긴다.

- flow 화면에서 긴 값이 보기 어렵다.
- message 변환 과정에서 잘림 여부를 확인해야 한다.
- JSON escape 때문에 SQL 문자가 변형될 수 있다.
- 로그와 prompt preview가 섞이면 어떤 값이 원문인지 헷갈린다.

그래서 CLOB은 `load_job()`에서 `.read()`로 전체를 읽고, 같은 command tool 내부에서 prompt render까지 이어지게 한다.

### 6. flow wiring 오류가 코드 오류처럼 보인다

컴포넌트를 쪼갤수록 오류 원인이 늘어난다.

```text
코드 버그
DB 데이터 문제
LLM 응답 문제
component input 누락
edge 연결 오류
message parsing 오류
type 변환 오류
prompt input 누락
```

기존 Python 코드에서는 stack trace만 보면 원인을 찾기 쉽다.

LangFlow에서는 flow wiring까지 함께 봐야 하므로 운영자가 문제를 찾기 어렵다.

그래서 이 프로젝트에서는 복잡한 업무 흐름을 하나의 Command Tool 내부에 두고, LangFlow graph는 다음 수준까지만 표현한다.

```text
사용자 요청
-> Agent
-> Command Tool
-> DB/LLM
```

### 선택 기준

쪼개도 되는 것:

```text
서로 상태 의존성이 낮은 큰 업무 단위
read-only dashboard
사용자 요청 라우팅 agent
수동 실행 command tool
background batch control tool
```

쪼개지 않는 것:

```text
retry loop 내부 단계
SQL 생성과 실행 사이의 중간 상태
검증 SQL 생성과 실행
job status update
log write
CLOB 원문 전달이 필요한 prompt render
```

결론적으로 LangFlow에서는 “시각적으로 많은 node”보다 “적은 node와 명확한 command boundary”가 더 안정적이다.

## 왜 함수명을 다시 정리했는가

기존 서버 코드는 모듈 분리가 되어 있으므로 함수명이 짧아도 문맥이 명확했다.

예를 들면 migration module 안에서는 아래 이름만으로도 의미가 충분하다.

```python
load_job()
write_log()
update_job_status()
call_llm()
```

하지만 `BatchAgentCommandTool`은 DB Migration 로직과 SQL Conversion 로직을 한 class 안에 같이 갖고 있다.

따라서 같은 이름을 그대로 쓰면 충돌한다.

```text
Migration write_log
SQL Conversion write_log
Batch write_log
```

그래서 Batch Agent 내부에서는 prefix를 붙였다.

```text
_mig__load_job()
_mig__write_log()
_mig__update_job_status()
_mig__call_llm()

_sql__load_job()
_sql__write_log()
_sql__update_job_status()
_sql__call_llm()
```

이 prefix는 Python private name mangling을 노린 것이 아니라 개발상 namespace 역할이다.

의도는 단순하다.

- `_mig__*`: DB Migration에서 복사/이식한 함수
- `_sql__*`: SQL Conversion에서 복사/이식한 함수
- prefix 없음: Batch Agent 자체 제어 함수

예:

```text
_start()
_stop()
_status()
_worker_loop()
_execute_one_job()
_write_batch_log()
```

이렇게 이름을 나눈 이유는 같은 class 안에 합쳐도 어느 업무 영역의 함수인지 바로 보이게 하기 위해서다.

## 왜 Batch Agent는 한 class 안에 다 합쳤는가

LangFlow Custom Component는 class 하나가 하나의 component다.

Batch Agent는 background loop 안에서 다음 일을 해야 한다.

```text
poll migration job
if exists: run migration job
else poll sql conversion job
if exists: run sql conversion job
else no job sleep
```

처음에는 `BatchAgentCommandTool`에서 `MigrationCommandTool`이나 `SqlConversionCommandTool` 함수를 import해서 쓰는 방법도 생각할 수 있다.

하지만 실제로는 문제가 있었다.

- LangFlow custom component 간 import 경로가 안정적이지 않다.
- IDE에서 component 하나만 복사하면 의존 component가 누락된다.
- migration command tool과 batch agent component는 서로 다른 component class라 instance/input 상태를 공유하지 않는다.
- LangFlow input으로 들어온 DB/LLM/prompt 설정을 다른 component instance에 그대로 주입하기 어렵다.

그래서 Batch Agent는 필요한 migration/sql conversion 업무 함수를 같은 class 안에 복사했다.

```text
BatchAgentCommandTool
  Batch control 함수
  Batch log 함수
  Migration 업무 함수 복사본
  SQL Conversion 업무 함수 복사본
```

이 구조는 코드 중복이 있다.

하지만 LangFlow 배포 단위에서는 장점이 더 컸다.

- Batch Agent component 하나만 있으면 batch loop가 실행된다.
- import path 문제를 피한다.
- LangFlow input 값을 같은 instance 안에서 바로 사용할 수 있다.
- background thread가 실행 중일 때 필요한 함수가 모두 같은 class 안에 있다.

## 왜 Agent 로직을 줄이고 Tool 로직을 키웠는가

기존 소스에서는 LangGraph Agent가 상태 전이를 더 많이 담당했다.

LangFlow에서는 Agent가 자연어 판단과 tool routing을 담당하고, 실제 업무 실행은 Tool이 담당한다.

```text
Agent 책임:
사용자 요청 해석
필요한 action 선택
command_json 생성
Tool 결과 요약

Tool 책임:
DB 조회
LLM 호출
SQL 생성
SQL 실행
검증
상태 저장
로그 저장
retry
```

이렇게 나눈 이유:

- Agent가 SQL이나 상태값을 임의로 만들면 위험하다.
- DB 상태 변경은 deterministic Python 코드에서 해야 한다.
- retry loop는 LLM 판단보다 코드로 제어하는 편이 안전하다.
- Tool 결과만 사용자에게 요약하면 디버깅이 쉽다.

## 기존 함수 로직을 얼마나 유지했는가

리팩토링의 기준은 “업무 로직은 유지하고 실행 경계만 바꾼다”였다.

유지한 업무 규칙:

- `NEXT_MIG_INFO` / `NEXT_MIG_INFO_DTL` 기반 migration job 조회
- `NEXT_SQL_INFO` 기반 SQL conversion job 조회
- migration retry attempt 구조
- SQL conversion의 TO_SQL / BIND_SQL / TEST_SQL 단계
- `USER_EDITED='Y'`이면 사용자가 저장한 SQL 우선 사용
- 생성 SQL과 최종 상태를 DB에 저장
- 단계별 로그 저장
- LLM prompt 기반 SQL 생성

LangFlow에 맞게 조정한 부분:

- DB/LLM 설정은 command JSON이 아니라 component input으로 이동
- prompt 본문도 component input으로 이동
- return 값은 LangFlow `Data` 형태로 반환
- action은 JSON command 하나로 통일
- component 내부에서 runtime package auto install 옵션 지원
- CLOB 값은 `.read()`로 전체 원문을 읽어서 prompt에 넣음

## Migration Command Tool 내부 구조

`migration_command_tool.py`는 migration 업무를 수동/채팅형으로 실행하기 위한 component다.

대표 action:

```text
test_connection
list_pending
status
get_table_ddl
generate_mig_sql
generate_verify_sql
preview_mig_prompt
preview_verify_prompt
run_migration_job
save_user_sql
analyze_failure
reset
```

개발 관점의 함수 그룹:

```text
command parsing
DB connection
job load
dependency check
prompt rendering
LLM call
SQL sanitize/extract
SQL execute
verify execute
status update
log write
JSON/Data conversion
```

`run_migration_job`은 외부에서 여러 node를 호출하는 대신 내부에서 end-to-end로 처리한다.

이유는 retry 때문이다.

```text
attempt 1
  generate mig sql
  execute
  generate verify sql
  verify

fail
  last_error 저장
  retry prompt에 반영

attempt 2
...
```

retry에 필요한 `last_error`, `last_sql`, `retry_count`를 LangFlow edge로 넘기지 않고 함수 내부에서 유지한다.

## SQL Conversion Command Tool 내부 구조

`sql_conversion_command_tool.py`는 SQL conversion 업무를 수동/채팅형으로 실행하기 위한 component다.

대표 action:

```text
test_connection
list_pending
status
preview_to_sql_prompt
preview_bind_sql_prompt
preview_test_sql_prompt
generate_to_sql
generate_bind_sql
generate_test_sql
run_sql_conversion_job
```

개발 관점의 함수 그룹:

```text
job load
TO_SQL prompt render
BIND_SQL prompt render
TEST_SQL prompt render
LLM call
runtime SQL prepare
BIND_SQL execute
TEST_SQL execute
final SQL save
status update
log write
```

실패 상태는 기존 상태 체계에 맞춰 세 단계로만 저장한다.

```text
FAIL-TOBE
FAIL-BIND
FAIL-TEST
```

generic한 `FAIL-CONVERSION`은 상태 체계와 맞지 않으므로 제거했다.

## Batch Agent Command Tool 내부 구조

`batch_agent_command_tool.py`는 단순 command tool이 아니라 background worker를 포함한다.

함수 그룹:

```text
batch action
  _start()
  _stop()
  _status()

batch control
  _start_control()
  _request_stop_control()
  _get_control_status()
  _is_control_running()
  _finish_control()

worker loop
  _worker_loop()
  _run_batch_supervisor_cycle()
  _execute_one_job()
  _interruptible_sleep()

batch log
  _write_batch_log()
  _write_batch_log_safe()

migration copy
  _mig__*

sql conversion copy
  _sql__*
```

현재 batch cycle의 기본 실행 경로는 기존 Supervisor와 유사한 LangChain tool calling 방식이다.

```text
_worker_loop()
-> _run_batch_supervisor_cycle()
-> LLM.bind_tools([
     poll_jobs,
     run_data_migration,
     run_sql_conversion,
     no_job
   ])
-> LLM이 이번 cycle에 호출할 tool을 선택
```

다만 운영 안전장치는 Python 코드가 강제한다.

- `poll_jobs`가 먼저 호출되어야 한다.
- 한 cycle에서는 job tool을 최대 1건만 실행한다.
- DB Migration이 SQL Conversion보다 우선한다.
- loop 시작, sleep 중, job 내부 주요 단계 사이에서 `NEXT_BATCH_CONTROL`을 확인한다.
- tool calling이 실패하면 기존 순차 실행으로 fallback하지 않고 `LOOP_ERROR`로 남긴다.

기존 서버 구조에서는 scheduler/supervisor process가 계속 떠 있었다.

LangFlow Playground 요청은 기본적으로 request/response 구조라 return 후에는 flow 실행이 끝난다.

그래서 `start` action이 background thread를 만들고 즉시 return하는 구조가 필요했다.

```text
사용자: 배치 시작
-> start action
-> background thread 생성
-> chat output 반환
-> thread는 LangFlow server process 안에서 계속 poll
```

## NEXT_BATCH_CONTROL을 추가한 이유

초기 batch 구현은 memory `_thread`와 `NEXT_BATCH_LOG`로 실행 여부를 판단했다.

문제:

- LangFlow가 component class를 reload하면 기존 `_thread` handle을 잃는다.
- Playground에서 flow를 삭제해도 server process 안의 thread는 살아 있을 수 있다.
- `NEXT_BATCH_LOG`는 이력 테이블이라 실행 lock으로 쓰기 어렵다.
- 여러 `RUN_ID`가 동시에 돌 수 있다.

그래서 실행 제어를 `NEXT_BATCH_CONTROL`로 분리했다.

```text
NEXT_BATCH_CONTROL
  CONTROL_NAME = 'BATCH_AGENT'
  STATUS = RUNNING / STOP_REQUESTED / STOPPED
  RUN_ID
  STOP_REQUESTED_YN
  HEARTBEAT_AT
```

단, `RUN_ID`는 로그 추적과 실행 이력 구분을 위한 값이다. 실행/중지 판단은 `RUN_ID`별로 하지 않는다.
LangFlow runtime에서 여러 background worker가 동시에 남을 수 있기 때문에, 어떤 worker든 같은 control row 하나를 보고 멈춰야 한다.

worker loop는 이제 이 조건일 때만 돈다.

```text
CONTROL_NAME = 'BATCH_AGENT'
AND STATUS = 'RUNNING'
AND STOP_REQUESTED_YN = 'N'
```

즉, DB에서 `CONTROL_NAME = 'BATCH_AGENT'` 한 row를 `STOP_REQUESTED`나 `STOPPED`로 바꾸면 다음 체크 지점에서 모든 batch loop가 끝난다.

DB 연결 또는 control table 접근이 실패하면 fatal error로 보고 loop를 종료한다.

## NEXT_BATCH_LOG의 역할 변경

초기에는 `NEXT_BATCH_LOG`를 실행 여부 판단에도 사용하려고 했다.

하지만 로그는 append-only 이력에 가깝다.

```text
START
LOOP_START
JOB_SUCCESS
JOB_FAIL
NO_JOB
STOP_REQUESTED
STOPPED
```

이런 이벤트가 계속 쌓이기 때문에 “최신 로그가 무엇인가”로 실행 제어를 하면 race condition이 생긴다.

예:

```text
STOP_REQUESTED 기록
현재 job 종료 후 JOB_SUCCESS 기록
최신 로그가 STOP_REQUESTED가 아니게 됨
loop가 계속 돎
```

그래서 `NEXT_BATCH_LOG`는 이력/감사용으로만 사용하고, 실행 제어는 `NEXT_BATCH_CONTROL`로 옮겼다.

## 개발자가 알아야 할 trade-off

### 코드 중복을 허용했다

Batch Agent 안에 migration/sql conversion 함수 복사본이 있다.

일반적인 backend 설계라면 중복을 줄이기 위해 import하거나 service로 분리하는 것이 맞다.

하지만 LangFlow Custom Component에서는 단일 component 실행성과 배포 안정성이 더 중요했다.

### class가 커졌다

특히 `BatchAgentCommandTool`은 크다.

이유는 background worker가 migration/sql conversion을 직접 호출해야 하고, LangFlow component 간 import를 피했기 때문이다.

대신 prefix로 영역을 구분했다.

```text
_mig__*
_sql__*
batch/control 함수
```

### 강제 kill은 어렵다

현재 batch는 Python thread 기반이다.

이미 실행 중인 Oracle SQL execute나 LLM HTTP call을 외부에서 즉시 kill할 수 없다.

대신 단계 사이마다 stop/control 상태를 확인한다.

진짜 process-level kill이 필요하면 thread가 아니라 별도 process worker 구조로 다시 설계해야 한다.

## 왜 이렇게 만들었는가

개발자 관점에서 답하면 다음과 같다.

1. LangFlow component는 독립 실행 단위라서 기존 backend package import 구조를 그대로 가져가기 어렵다.
2. migration/sql conversion은 중간 상태가 많아서 node 단위로 쪼개면 flow가 불안정해진다.
3. Agent에게 상태 전이를 맡기면 SQL 실행/상태 저장 같은 위험 작업에서 예측 가능성이 떨어진다.
4. 그래서 Agent는 router로 줄이고, Command Tool이 deterministic workflow를 실행하게 했다.
5. Batch Agent는 LangFlow request가 끝난 뒤에도 돌아야 하므로 background thread를 사용했다.
6. thread 중복 실행 문제 때문에 실행 제어를 memory/log가 아니라 `NEXT_BATCH_CONTROL`로 옮겼다.
7. `NEXT_BATCH_LOG`는 control이 아니라 관찰 가능한 이력으로 남겼다.
8. 코드 중복은 생겼지만, LangFlow IDE에서 component 단독 실행성과 배포 안정성을 얻었다.

## 리팩토링 개발 순서

이번 리팩토링은 기존 코드를 한 번에 LangFlow graph로 옮기는 방식이 아니다.

먼저 LangFlow에서 안정적으로 실행될 수 있는 component boundary를 정하고, 그 boundary 안으로 기존 업무 함수를 이식하는 순서로 진행한다.

전체 순서는 다음과 같다.

```text
1. 업무 실행 단위 정하기
2. 각 component의 inputs / outputs 정의하기
3. command_json action 계약 정의하기
4. 기존 소스 코드에서 필요한 메인 함수 식별하기
5. 메인 함수가 의존하는 유틸리티 함수 가져오기
6. import 의존성을 component 내부 함수로 치환하기
7. DB/LLM/prompt 설정을 component input으로 연결하기
8. 상태 저장과 로그 저장 위치를 DB 기준으로 고정하기
9. 단독 component로 먼저 테스트하기
10. Agent prompt를 붙여 자연어 routing 연결하기
11. Supervisor에 Agent Tool로 연결하기
12. Batch Agent처럼 background 실행이 필요한 경우 control table을 추가하기
```

### 1. 업무 실행 단위 정하기

가장 먼저 정한 것은 “무엇을 LangFlow component 하나로 볼 것인가”다.

기준은 함수 크기가 아니라 운영자가 이해하는 업무 단위다.

```text
DB Migration
SQL Conversion
Dashboard
Batch Control
```

그래서 아래 component로 나눴다.

```text
Migration Command Tool
SQL Conversion Command Tool
Dashboard Command Tool
Batch Agent Command Tool
```

이 단계에서 DDL 조회, SQL 생성, SQL 실행, 상태 저장을 각각 component로 쪼개지 않기로 결정했다.

이유는 중간 상태가 많고 retry loop가 있기 때문이다.

### 2. component inputs / outputs 정의하기

그 다음 각 component가 LangFlow IDE에서 어떤 값을 입력받고 어떤 값을 반환할지 정했다.

공통 input:

```text
db_host
db_port
db_service_name
db_username
db_password
system_schema
source_schema
target_schema
llm_base_url
llm_api_key
llm_model
llm_timeout_seconds
auto_install_packages
```

업무별 prompt input:

```text
Migration:
  mig_sql_prompt
  verify_sql_prompt

SQL Conversion:
  to_sql_prompt
  bind_sql_prompt
  test_sql_prompt

Batch:
  mig_sql_prompt
  verify_sql_prompt
  to_sql_prompt
  bind_sql_prompt
  test_sql_prompt
```

output은 복잡하게 나누지 않고 기본적으로 하나의 `Data` output으로 통일했다.

이유:

- LangFlow multi output 연결은 복잡한 dict/list 전달에 불안정할 수 있다.
- 다음 component가 Python object를 그대로 받는다고 가정하기 어렵다.
- 하나의 result dict를 `Data`로 반환하면 Chat Output과 Agent 요약이 단순해진다.
- 복수 output보다 command 결과의 JSON 구조를 유지하는 편이 디버깅이 쉽다.

### 3. command_json action 계약 정의하기

component input을 전부 command JSON으로 받지 않는다.

DB 접속 정보, API key, prompt 본문은 component input으로 고정한다.

command JSON에는 action과 job identifier만 넣는다.

예:

```json
{"action":"run_migration_job","map_id":101}
```

```json
{"action":"run_sql_conversion_job","space_nm":"A","sql_id":"selectUser"}
```

```json
{"action":"start"}
```

이렇게 한 이유:

- 비밀값이 Agent message나 chat history에 노출되지 않는다.
- Agent가 DB password나 prompt 본문을 생성하지 않아도 된다.
- command 계약이 작아져 routing 오류가 줄어든다.
- 사용자가 볼 수 있는 action surface가 명확해진다.

### 4. 기존 소스 코드에서 메인 함수 식별하기

그 다음 기존 소스에서 실제 업무 흐름의 entry point를 찾았다.

Migration 기준:

```text
run_migration_job
generate_mig_sql
generate_verify_sql
execute migration sql
execute verify sql
update job status
write migration log
```

SQL Conversion 기준:

```text
run_sql_conversion_job
generate_to_sql
generate_bind_sql
generate_test_sql
execute bind sql
execute test sql
update conversion status
write sql log
```

Batch 기준:

```text
start
stop
status
poll migration job
poll sql conversion job
run one job
write batch log
update batch control
```

먼저 메인 함수 흐름을 잡고, 그 함수가 호출하는 하위 함수를 따라가며 필요한 코드만 가져온다.

### 5. 유틸리티 함수 가져오기

메인 함수만 가져오면 동작하지 않는다.

아래 유틸리티도 함께 필요하다.

```text
DB connection
table qualify
CLOB to text
JSON safe conversion
SQL sanitize
prompt render
LLM post_json
retry context build
dependency check
log insert
status update
```

이 단계에서 기존 소스의 utility 함수를 LangFlow component 내부 method로 옮긴다.

Batch Agent처럼 migration과 sql conversion을 한 class 안에 합친 경우에는 이름 충돌을 피하기 위해 prefix를 붙인다.

```text
_mig__to_text()
_mig__write_log()
_mig__update_job_status()

_sql__to_text()
_sql__write_log()
_sql__update_job_status()
```

### 6. import 의존성 제거

기존 코드의 import 의존성을 그대로 두면 LangFlow component 단독 실행성이 떨어진다.

그래서 다음 import는 가능한 component 내부 코드로 치환한다.

```text
server.services.*
server.repositories.*
server.agents.*
server.config.*
```

단, 표준 라이브러리와 runtime package는 유지한다.

```text
json
re
time
urllib
oracledb
sqlalchemy
langchain_community
```

runtime package는 LangFlow 환경에 없을 수 있으므로 `auto_install_packages` 옵션을 둔다.

### 7. DB를 상태 기준으로 고정하기

중간 상태를 LangFlow edge로 넘기지 않고 DB 기준으로 둔다.

```text
NEXT_MIG_INFO
NEXT_MIG_INFO_DTL
NEXT_MIG_LOG
NEXT_SQL_INFO
NEXT_SQL_LOG
NEXT_BATCH_CONTROL
NEXT_BATCH_LOG
```

이 단계에서 중요한 설계 결정을 한다.

- 어떤 값은 job table에 저장할 것인가
- 어떤 값은 log table에만 저장할 것인가
- 어떤 값은 함수 local variable로만 유지할 것인가
- 어떤 값은 Agent response에만 요약할 것인가

예:

```text
MIG_SQL / VERIFY_SQL: job table에 저장
TO_SQL / BIND_SQL / BIND_SET / TEST_SQL: job table에 저장
단계별 실패 원인: log table에 저장
retry_count: job table과 result에 저장
Batch 실행 여부: NEXT_BATCH_CONTROL에 저장
Batch loop 이력: NEXT_BATCH_LOG에 저장
```

### 8. 단독 component 먼저 테스트하기

Supervisor나 Agent를 붙이기 전에 command tool 단독으로 테스트한다.

순서:

```text
test_connection
list_pending
status
preview prompt
generate SQL
run full job
reset
```

이렇게 해야 실패 원인이 명확하다.

```text
DB 연결 문제인지
prompt 문제인지
LLM 문제인지
SQL 실행 문제인지
Agent routing 문제인지
LangFlow wiring 문제인지
```

처음부터 Supervisor에 붙이면 실패 지점이 섞여서 디버깅이 어렵다.

### 9. Agent prompt 연결

단독 component가 동작하면 Agent를 붙인다.

Agent의 역할은 command를 실행하는 것이 아니라 command JSON을 만드는 것이다.

```text
사용자 자연어
-> Agent system prompt 기준으로 action 판단
-> command_json 생성
-> Command Tool 호출
-> 결과 요약
```

이 단계에서 system prompt에 금지 사항을 명확히 넣는다.

```text
DB password를 command_json에 넣지 말 것
LLM API key를 말하지 말 것
지원하지 않는 action을 만들지 말 것
Batch Agent에서 run_migration_job을 직접 만들지 말 것
```

### 10. Supervisor에 연결

마지막으로 Supervisor Agent에 각 Agent Tool을 연결한다.

Supervisor는 직접 migration/sql conversion command를 만들지 않는다.

```text
Supervisor
-> Dashboard Agent
-> Batch Agent
-> DB Migration Agent
-> SQL Conversion Agent
```

Supervisor의 책임은 routing이다.

```text
dashboard 요청 -> Dashboard Agent
batch start/stop/status -> Batch Agent
map_id migration 요청 -> DB Migration Agent
space_nm/sql_id conversion 요청 -> SQL Conversion Agent
```

### 11. Batch Agent는 별도 control 설계를 추가한다

Batch Agent는 일반 command tool과 다르다.

`start`가 return된 뒤에도 background thread가 계속 돌아야 한다.

그래서 추가 설계가 필요했다.

```text
NEXT_BATCH_CONTROL
NEXT_BATCH_LOG
background thread
heartbeat
stop flag
fatal DB error handling
```

이 단계는 다른 command tool이 안정화된 뒤에 진행하는 것이 맞다.

Batch Agent는 내부에서 migration/sql conversion job을 직접 실행하므로, 앞선 command tool의 업무 로직 이식이 먼저 끝나 있어야 한다.

## 현재 구조의 기준

```text
Agent = 자연어 라우터
Command Tool = 업무 실행 단위
Oracle DB = durable state
NEXT_BATCH_CONTROL = batch 실행 lock/control
NEXT_BATCH_LOG = batch 이력
LangFlow input = DB/LLM/prompt 설정
command_json = action과 job identifier만 전달
```
