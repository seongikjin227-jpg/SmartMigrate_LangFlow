# 신규 패키지 구조 제안

이 문서는 현재 소스 코드의 패키지/파일/폴더명을 전면 재구성하기 위한 설계안입니다.
이 문서는 계획서이며, 실제 Python import는 아직 변경하지 않습니다.

## 1. 목표

현재 코드는 동작하지만 구조 경계가 읽기 어렵습니다.

- `server.agents.*` 안에 wrapper, LangGraph pipeline, domain agent가 섞여 있습니다.
- `server.services.sql.agents` 한 파일에 여러 agent와 coordinator가 같이 있습니다.
- `server.tools.*` 안에 LangChain tool과 공유 runtime 상태가 같이 있습니다.
- `app/utils/db.py`가 Streamlit 화면용 DB 조회를 직접 수행하면서 repository 역할을 일부 중복합니다.
- standalone scheduler가 Supervisor 경로 옆에 있어 주 실행 경로가 헷갈립니다.

새 구조는 아래 질문에 바로 답할 수 있어야 합니다.

- 시스템은 어디서 시작하는가?
- 어떤 job을 실행할지는 누가 결정하는가?
- migration job 하나는 누가 실행하는가?
- SQL conversion/tuning/formatting job 하나는 누가 실행하는가?
- Oracle 접근은 어디에 격리되는가?
- LLM client와 prompt는 어디에 격리되는가?
- 운영 UI는 어디에 격리되는가?

## 2. 제안하는 최상위 구조

```text
smart_migrate/
+- README.md
+- pyproject.toml
+- main.py
+- docs/
   +- SourceDiagram.md
   +- NewPackagePlan.md
   +- architecture/
+- src/
   +- smart_migrate/
      +- runtime/
      +- supervisor/
      +- pipelines/
      +- repositories/
      +- integrations/
      +- config/
      +- shared/
+- app/
   +- README.md
   +- streamlit_app.py
   +- pages/
   +- services/
+- scripts/
+- tests/
+- table_ddl_comment/
```

`src/` 구조를 꼭 써야 하는 것은 아닙니다. 현재처럼 root에 `server/`, `app/`를 두는 방식도 가능하지만, 장기적으로는 backend package boundary가 분명한 `src/smart_migrate` 구조가 더 낫습니다.

단, Streamlit 프론트는 backend package 안에 넣지 않는 것이 맞습니다. `app/`은 별도 프론트 애플리케이션으로 두고, backend의 repository나 runtime API를 호출하는 방향으로 분리합니다.

## 2.1. 파일명 규칙

리팩토링 후에는 `agent.py`, `graph.py`, `state.py`처럼 파일명만 보고 역할을 알기 어려운 이름을 새로 만들지 않습니다.

권장 규칙:

- Python 파일명은 snake_case로 작성합니다.
- 파일명은 `도메인_역할.py` 형태로 작성합니다.
- 예: `sql_conversion_graph.py`, `sql_conversion_agent.py`, `migration_dependency_checker.py`
- `sqlconversion_Grpah` 같은 CamelCase/오타 혼합명은 쓰지 않고, `sql_conversion_graph.py`처럼 통일합니다.
- 같은 폴더 안에 있더라도 `agent.py`, `graph.py`보다 `supervisor_agent.py`, `supervisor_graph.py`처럼 명시적으로 씁니다.

## 3. 제안하는 package tree

```text
src/smart_migrate/
+- runtime/
   +- README.md
   +- runtime_entrypoint.py
   +- runtime_process_control.py
   +- runtime_startup_checks.py
   +- runtime_files.py
+- supervisor/
   +- README.md
   +- supervisor_agent.py
   +- supervisor_graph.py
   +- supervisor_prompt.py
   +- supervisor_state.py
   +- supervisor_cycle_metrics.py
   +- supervisor_job_registry.py
   +- supervisor_job_polling.py
   +- tools/
      +- README.md
      +- supervisor_migration_tools.py
      +- supervisor_sql_conversion_tools.py
      +- supervisor_sql_tuning_tools.py
      +- supervisor_sql_formatting_tools.py
      +- supervisor_cycle_tools.py
+- pipelines/
   +- README.md
   +- migration/
      +- README.md
      +- migration_orchestrator.py
      +- migration_graph.py
      +- migration_state.py
      +- migration_ddl_loader.py
      +- migration_dependency_checker.py
      +- migration_sql_generator.py
      +- migration_executor.py
      +- migration_verifier.py
      +- migration_standalone_scheduler.py
   +- sql_conversion/
      +- README.md
      +- sql_conversion_agent.py
      +- sql_conversion_coordinator.py
      +- sql_conversion_graph.py
      +- sql_conversion_state.py
      +- sql_conversion_to_sql_generator.py
      +- sql_conversion_bind_generator.py
      +- sql_conversion_test_generator.py
      +- sql_conversion_bind_cases.py
      +- sql_conversion_validator.py
      +- sql_conversion_standalone_scheduler.py
   +- sql_tuning/
      +- README.md
      +- sql_tuning_agent.py
      +- sql_tuning_rule_retriever.py
      +- sql_tuning_tuner.py
      +- sql_tuning_validator.py
   +- sql_formatting/
      +- README.md
      +- sql_formatting_agent.py
      +- sql_formatting_formatter.py
   +- xml/
      +- README.md
      +- xml_mapper_parser.py
      +- xml_include_expander.py
      +- xml_schema_cleaner.py
      +- xml_sql_exporter.py
+- repositories/
   +- README.md
   +- oracle_repository.py
   +- migration_job_repository.py
   +- migration_history_repository.py
   +- sql_job_repository.py
   +- sql_log_repository.py
   +- mapping_rule_repository.py
   +- rag_rule_repository.py
   +- agent_metric_repository.py
+- integrations/
   +- README.md
   +- llm/
      +- README.md
      +- llm_client.py
      +- llm_fallback.py
      +- llm_prompt_loader.py
      +- prompts/
   +- oracle/
      +- README.md
      +- oracle_connection.py
      +- oracle_ddl_reader.py
      +- oracle_sql_executor.py
+- config/
   +- README.md
   +- app_settings.py
   +- env_settings.py
+- shared/
   +- README.md
   +- migration_statuses.py
   +- sql_statuses.py
   +- shared_exceptions.py
   +- shared_logging.py
   +- shared_types.py
```

프론트 애플리케이션은 backend package와 분리합니다.

```text
app/
+- README.md
+- streamlit_app.py
+- pages/
   +- README.md
   +- dashboard_page.py
   +- migration_monitor_page.py
   +- sql_monitor_page.py
   +- sql_detail_page.py
   +- user_edited_sql_page.py
   +- tuning_monitor_page.py
   +- rag_rule_manager_page.py
   +- fail_analysis_page.py
   +- xml_export_page.py
   +- system_health_page.py
   +- settings_page.py
   +- agent_metrics_page.py
+- services/
   +- README.md
   +- ui_migration_queries.py
   +- ui_sql_queries.py
   +- ui_fail_analysis_queries.py
   +- ui_job_actions.py
   +- ui_xml_export_queries.py
   +- ui_agent_control.py
   +- ui_env_editor.py
   +- ui_rag_admin.py
```

## 4. 현재 파일에서 신규 파일로의 mapping

### Runtime

| 현재 | 제안 |
| --- | --- |
| `main.py` | `smart_migrate/runtime/runtime_entrypoint.py` + root의 얇은 `main.py` |
| `app/utils/agent_control.py` | `app/services/ui_agent_control.py` + backend 필요 함수는 `smart_migrate/runtime/runtime_process_control.py` |
| 여러 파일에 흩어진 runtime file path | `smart_migrate/runtime/runtime_files.py` |
| `scripts/init_db.py`의 check 함수 | `smart_migrate/runtime/runtime_startup_checks.py` |

### Supervisor

| 현재 | 제안 |
| --- | --- |
| `server/agents/supervisor/agent.py` | `smart_migrate/supervisor/supervisor_agent.py` |
| `server/agents/supervisor/graph.py` | `smart_migrate/supervisor/supervisor_graph.py` |
| `server/agents/supervisor/prompts.py` | `smart_migrate/supervisor/supervisor_prompt.py` |
| `server/agents/supervisor/state.py` | `smart_migrate/supervisor/supervisor_state.py` |
| `server/tools/context.py` | `supervisor_job_registry.py`, `supervisor_cycle_metrics.py`, `runtime_files.py`로 분리 |
| `server/tools/poll.py` | `smart_migrate/supervisor/supervisor_job_polling.py` |
| `server/tools/*.py` job tool | `smart_migrate/supervisor/tools/*.py` |

### Migration pipeline

| 현재 | 제안 |
| --- | --- |
| `server/agents/migration/orchestrator.py` | `pipelines/migration/migration_orchestrator.py` |
| `server/agents/migration/graph.py` | `pipelines/migration/migration_graph.py` |
| `server/agents/migration/state.py` | `pipelines/migration/migration_state.py` |
| `server/agents/migration/executor.py` | `pipelines/migration/migration_executor.py` |
| `server/agents/migration/verifier.py` | `pipelines/migration/migration_verifier.py` |
| `server/agents/migration/sql_utils.py` | `pipelines/migration/migration_sql_generator.py` 또는 `shared/sql_text.py` |
| `server/agents/migration/scheduler.py` | `pipelines/migration/migration_standalone_scheduler.py` |
| `server/services/migration/llm_client.py` | `pipelines/migration/migration_sql_generator.py` 또는 `integrations/llm/migration_llm_client.py` |
| `server/services/migration/prompt_service.py` | `integrations/llm/llm_prompt_loader.py` |

### SQL conversion pipeline

| 현재 | 제안 |
| --- | --- |
| `server/agents/sql_conversion/agent.py` | `pipelines/sql_conversion/sql_conversion_agent.py` |
| `server/services/sql/agents.py::TobeMultiAgentCoordinator` | `pipelines/sql_conversion/sql_conversion_coordinator.py` |
| `server/services/sql/agents.py::TobeSqlGenerationAgent` | `sql_conversion_to_sql_generator.py`, `sql_conversion_bind_generator.py`, `sql_conversion_test_generator.py`로 분리 |
| `server/services/sql/workflow/graph.py` | `pipelines/sql_conversion/sql_conversion_graph.py` |
| `server/services/sql/workflow/state.py` | `pipelines/sql_conversion/sql_conversion_state.py` |
| `server/services/sql/binding_service.py` | `pipelines/sql_conversion/sql_conversion_bind_cases.py` |
| `server/services/sql/validation_service.py` | `pipelines/sql_conversion/sql_conversion_validator.py` |
| `server/services/sql/batch_scheduler.py` | `pipelines/sql_conversion/sql_conversion_standalone_scheduler.py` |

### SQL tuning / formatting

| 현재 | 제안 |
| --- | --- |
| `server/agents/sql_tuning/agent.py` | `pipelines/sql_tuning/sql_tuning_agent.py` |
| `server/services/sql/agents.py::SqlTuningAgent` | `pipelines/sql_tuning/sql_tuning_tuner.py` |
| `server/services/sql/tobe_sql_tuning_service.py` | `pipelines/sql_tuning/sql_tuning_rule_retriever.py` |
| `server/agents/sql_formatting/agent.py` | `pipelines/sql_formatting/sql_formatting_agent.py` |
| `server/services/sql/sql_formatting_service.py` | `pipelines/sql_formatting/sql_formatting_formatter.py` |

### Repositories

| 현재 | 제안 |
| --- | --- |
| `server/repositories/migration/repository.py` | `repositories/migration_job_repository.py` |
| `server/repositories/migration/history_repository.py` | `repositories/migration_history_repository.py` |
| `server/repositories/sql/result_repository.py` | `repositories/sql_job_repository.py` |
| `server/repositories/sql/log_repository.py` | `repositories/sql_log_repository.py` |
| `server/repositories/sql/mapper_repository.py` | `repositories/mapping_rule_repository.py` |
| `server/repositories/supervisor/metrics_repository.py` | `repositories/agent_metric_repository.py` |
| `app/utils/rag_db.py` | `repositories/rag_rule_repository.py` 또는 `app/services/ui_rag_admin.py` |

### UI

| 현재 | 제안 |
| --- | --- |
| `app/app.py` | `app/streamlit_app.py` |
| `app/pages/mig_monitor.py` | `app/pages/migration_monitor_page.py` |
| `app/pages/sql_monitor.py` | `app/pages/sql_monitor_page.py` |
| `app/pages/job_detail.py` | `app/pages/sql_detail_page.py` 또는 `app/pages/job_detail_page.py` |
| `app/pages/correct_sql.py` | `app/pages/user_edited_sql_page.py` |
| `app/pages/rag_manager_page.py` | `app/pages/rag_rule_manager_page.py` |
| `app/pages/settings_page.py` | `app/pages/settings_page.py` |
| `app/utils/db.py` | `app/services/ui_sql_queries.py` 등으로 분리 후 repository 재사용으로 축소 |
| `app/utils/env_manager.py` | `app/services/ui_env_editor.py` |

## 5. 각 패키지 README.md에 들어갈 내용

각 패키지 폴더에는 `README.md`를 둡니다. README는 “이 패키지가 무엇을 책임지고, 무엇을 책임지지 않는지”를 설명해야 합니다.

### `runtime/README.md`

- batch process 시작 방식
- `runtime/agent.pid`, `runtime/agent.pause`, `runtime/active_job.json` 역할
- start/stop/pause/resume 동작 방식
- startup check 책임

### `supervisor/README.md`

- Supervisor cycle lifecycle
- LLM tool-calling 계약
- 한 cycle당 하나의 job만 실행하는 guard
- job family 우선순위
- callback 주입 구조
- cycle metrics 저장 구조

### `supervisor/tools/README.md`

- 이 폴더는 LangChain/LangGraph tool wrapper임
- business logic을 직접 가지면 안 됨
- registry에서 job을 꺼내 callback 호출, metric 기록, registry refresh만 담당

### `pipelines/README.md`

- 실제 job 실행 로직은 pipelines에 둠
- pipelines는 Streamlit을 몰라야 함
- global scheduling priority를 결정하지 않아야 함
- repositories, integrations, shared를 사용할 수 있음

### `pipelines/migration/README.md`

- DB migration graph node 설명
- dependency check 정책
- `USER_EDITED` 동작
- retry/finalize 규칙
- 읽고 쓰는 테이블

### `pipelines/sql_conversion/README.md`

- `NEXT_SQL_INFO` conversion lifecycle
- `FR_SQL -> TO_SQL -> BIND_SQL/BIND_SET -> TEST_SQL`
- `USER_EDITED=Y` 동작
- retry/status 규칙
- tuning을 이 패키지가 소유하지 않는 이유

### `pipelines/sql_tuning/README.md`

- `NEXT_MIG_RAG_INFO` rule retrieval
- `TO_SQL -> TUNED_TO_SQL`
- SELECT validation과 non-SELECT pass 처리
- conversion 이후 continuation 계약

### `pipelines/sql_formatting/README.md`

- formatting 입력 우선순위: `TUNED_TO_SQL`, 없으면 `TO_SQL`
- `FORMATTED_SQL` 출력
- XML export와의 관계

### `pipelines/xml/README.md`

- mapper XML import pipeline
- include expansion
- schema cleanup
- `NEXT_SQL_INFO` upsert 컬럼
- XML export 방향

### `repositories/README.md`

- domain table SELECT/DML은 repository가 소유
- env var와 table name mapping
- schema compatibility 정책
- transaction 규칙

### `integrations/README.md`

- 외부 시스템 연결 코드를 둠
- Oracle connection과 LLM client는 domain workflow가 아니라 infrastructure임

### `integrations/llm/README.md`

- prompt template 위치
- LLM fallback
- JSON parsing
- model call logging
- 각 prompt의 소유 pipeline

### `integrations/oracle/README.md`

- Oracle connection 생성
- DDL metadata 조회
- SQL execution helper
- schema qualification helper

### `app/README.md`

- Streamlit app은 운영 콘솔임
- UI는 DB 상태 조회와 background process 제어 담당
- UI는 domain pipeline을 직접 실행하지 않아야 함
- UI data service는 점진적으로 repository를 재사용해야 함

### `app/pages/README.md`

- page naming convention
- 각 page가 조회하는 주 테이블
- SQL 컬럼명 표시 규칙

### `app/services/README.md`

- process control, env editing, dashboard query용 UI adapter
- batch job 실행 로직 금지

### `config/README.md`

- `.env` 변수
- runtime default
- prompt/config path
- env var rename migration 정책

### `shared/README.md`

- status, exception, logging, 공통 type
- Oracle/LLM 호출 금지

## 6. 추천 rename 순서

한 번에 전부 옮기면 import 충돌과 운영 장애 가능성이 큽니다. 아래 순서를 권장합니다.

1. 새 package folder와 README만 먼저 만든다.
2. 순수 shared 코드부터 이동한다.
   - statuses
   - exceptions
   - logging
   - runtime file constants
3. repositories를 이동하고 import를 수정한다.
4. LLM/Oracle integrations를 이동한다.
5. SQL pipeline 파일을 분리한다.
6. migration pipeline 파일을 이동한다.
7. supervisor tools와 registry를 이동한다.
8. Streamlit UI를 마지막에 이동한다.
9. 구 import 경로는 한동안 shim으로 유지한다.

## 7. 임시 import shim 전략

큰 rename 중에는 기존 import를 바로 없애지 말고 얇은 shim을 둡니다.

```python
# server/services/sql/agents.py
from smart_migrate.pipelines.sql_conversion.coordinator import TobeMultiAgentCoordinator
from smart_migrate.pipelines.sql_conversion.to_sql_generator import TobeSqlGenerationAgent
from smart_migrate.pipelines.sql_tuning.tuner import SqlTuningAgent
```

이 방식이면 scripts, UI, tests를 단계적으로 옮길 수 있습니다.

## 8. 리팩터링 후 의존성 방향

```text
app
  -> runtime
  -> repositories

supervisor
  -> pipelines
  -> repositories
  -> integrations
  -> shared

pipelines
  -> repositories
  -> integrations
  -> shared

repositories
  -> integrations.oracle
  -> shared

integrations
  -> config
  -> shared

shared
  -> 다른 프로젝트 package 의존 없음
```

규칙:

- app 프론트는 pipeline agent를 직접 import하지 않는다.
- repository는 UI나 supervisor를 import하지 않는다.
- domain pipeline은 Streamlit state나 runtime control file을 읽지 않는다.
- supervisor tool은 SQL을 직접 만들지 않는다.
- LLM prompt 코드는 Oracle table을 직접 update하지 않는다.
- retry/status 정책은 repository가 아니라 pipeline에 둔다.

## 9. 우선 분리해야 할 파일

### `server/services/sql/agents.py`

현재 책임:

- TO SQL 생성
- bind SQL 생성
- test SQL 생성
- tuning
- coordinator retry loop
- SQL execution logging

제안 분리:

```text
pipelines/sql_conversion/sql_conversion_coordinator.py
pipelines/sql_conversion/sql_conversion_to_sql_generator.py
pipelines/sql_conversion/sql_conversion_bind_generator.py
pipelines/sql_conversion/sql_conversion_test_generator.py
pipelines/sql_tuning/sql_tuning_tuner.py
```

### `server/tools/context.py`

현재 책임:

- stop event
- pause/wake/active job files
- job registries
- callback registry
- metrics aggregation

제안 분리:

```text
runtime/runtime_files.py
runtime/runtime_process_control.py
supervisor/supervisor_job_registry.py
supervisor/supervisor_callbacks.py
supervisor/supervisor_cycle_metrics.py
```

### `app/utils/db.py`

현재 책임:

- migration monitor query
- SQL monitor query
- fail analysis query
- rerun/reset command
- user edited SQL 저장
- XML export query

제안 분리:

```text
app/services/ui_migration_queries.py
app/services/ui_sql_queries.py
app/services/ui_fail_analysis_queries.py
app/services/ui_job_actions.py
app/services/ui_xml_export_queries.py
```

장기적으로는 이 함수들이 `repositories/*`를 재사용하도록 줄이는 것이 좋습니다.

## 10. 구현 전 결정해야 할 질문

1. root package 이름을 `smart_migrate`로 할지, 기존 `server/app` 구조를 유지할지?
2. standalone APScheduler 경로를 계속 지원할지?
3. UI가 Oracle을 직접 읽을지, repository 함수만 호출하게 할지?
4. 구 import shim을 몇 release 동안 유지할지?
5. conversion 성공 후 tuning/formatting 자동 continuation을 유지할지?
6. 패키지 README는 한국어로 통일할지, 영어로 통일할지?
7. prompt JSON은 `integrations/llm/prompts`로 옮길지, `config/prompts`에 둘지?

## 11. 목표 상태

최종적으로 아래 문장이 코드 구조에서 드러나야 합니다.

```text
Supervisor는 결정한다.
Tools는 호출한다.
Pipelines는 실행한다.
Repositories는 저장한다.
Integrations는 외부 시스템에 연결한다.
UI는 관찰하고 제어한다.
Shared는 공통 언어를 정의한다.
```

이 분리가 되어야 이후 DB schema 변경, agent 변경, UI 변경의 비용이 줄어듭니다.
