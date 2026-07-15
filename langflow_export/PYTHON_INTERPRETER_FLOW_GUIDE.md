# Langflow Python Interpreter 방식

이 방식은 Langflow의 `Python Interpreter` 컴포넌트만 사용합니다. API 서버나 Custom Component 없이, 각 노드에 Python 코드를 붙여넣어 기존 migration agent 코드를 직접 호출합니다.

## 먼저 판단해야 할 것

Python Interpreter 코드는 **Langflow 서버의 Python 런타임에서 실행**됩니다.

따라서 아래 조건이 필요합니다.

- Langflow 서버에서 `0609_final-main` 프로젝트 폴더에 접근 가능해야 합니다.
- Langflow 서버 Python 환경에 `requirements.txt` 의존성이 설치되어 있어야 합니다.
- Langflow 서버에서 `.env`를 읽을 수 있어야 합니다.
- Langflow 서버에서 Oracle DB와 LLM endpoint에 접근 가능해야 합니다.

이 조건이 어렵다면 Python Interpreter 방식도 실패합니다. 그 경우에는 API Request 방식이 더 맞습니다.

## 전체 Flow

Langflow 캔버스에는 아래 순서로 `Python Interpreter` 컴포넌트를 배치합니다.

```text
01 Bootstrap / Health Check
  -> 02 Poll DB Migration
  -> 03 Run DB Migration
  -> 04 Poll SQL Conversion
  -> 05 Run SQL Conversion
  -> 06 Poll SQL Tuning
  -> 07 Run SQL Tuning
  -> 08 Poll SQL Formatting
  -> 09 Run SQL Formatting
  -> 10 Final Status
```

각 노드에 넣을 코드는 `langflow_export/python_interpreter_snippets/`에 있습니다.

```text
01_bootstrap_health.py
02_poll_migration.py
03_run_migration.py
04_poll_sql_conversion.py
05_run_sql_conversion.py
06_poll_sql_tuning.py
07_run_sql_tuning.py
08_poll_sql_formatting.py
09_run_sql_formatting.py
10_final_status.py
```

## 공통 수정 지점

각 snippet 상단에 아래 값이 있습니다.

```python
PROJECT_PATH = r"C:\Users\11824\Desktop\0609_final-main"
```

Langflow 서버에서 실제 프로젝트가 있는 경로로 바꾸세요.

예:

```python
PROJECT_PATH = r"D:\deploy\SmartMigrate_LangFlow"
```

Linux 서버라면:

```python
PROJECT_PATH = "/opt/smartmigrate"
```

## 실행 원칙

처음에는 전체를 한 번에 실행하지 말고, 한 노드씩 확인하세요.

1. `01 Bootstrap / Health Check`
   - 프로젝트 경로, `.env`, import 가능 여부 확인
2. `02 Poll DB Migration`
   - migration pending job 조회
3. `03 Run DB Migration`
   - 첫 pending migration job 실행
4. `04 Poll SQL Conversion`
   - conversion pending job 조회
5. `05 Run SQL Conversion`
   - 첫 pending conversion job 실행
6. `06 Poll SQL Tuning`
   - tuning pending job 조회
7. `07 Run SQL Tuning`
   - 첫 pending tuning job 실행
8. `08 Poll SQL Formatting`
   - formatting pending job 조회
9. `09 Run SQL Formatting`
   - 첫 pending formatting job 실행
10. `10 Final Status`
    - background supervisor 상태 확인

## 노드별 역할

| 순서 | 노드명 | 역할 |
| --- | --- | --- |
| 01 | Bootstrap / Health Check | `.env` 로드, import 확인 |
| 02 | Poll DB Migration | `NEXT_MIG_INFO` pending 조회 |
| 03 | Run DB Migration | `MigrationOrchestrator().process_job(job)` 실행 |
| 04 | Poll SQL Conversion | `NEXT_SQL_INFO` conversion pending 조회 |
| 05 | Run SQL Conversion | `SqlConversionAgent().process_job(job)` 실행 |
| 06 | Poll SQL Tuning | tuning pending 조회 |
| 07 | Run SQL Tuning | `SqlTuningAgent().process_job(job)` 실행 |
| 08 | Poll SQL Formatting | formatting pending 조회 |
| 09 | Run SQL Formatting | `SqlFormattingAgent().process_job(job)` 실행 |
| 10 | Final Status | agent process 상태 확인 |

## 주의

- 이 방식은 Langflow의 Python 실행 권한이 강해야 합니다. 회사 보안 정책에서 Python Interpreter를 제한하면 동작하지 않습니다.
- 각 Run 노드는 DB 상태를 변경합니다.
- 운영 전에는 반드시 Poll 노드로 대상 job을 확인한 뒤 Run 노드를 실행하세요.
- snippet은 기본적으로 “첫 번째 pending job 1건”만 처리합니다.
- 여러 건 batch 실행은 안전 확인 후 별도 loop를 추가하는 것이 좋습니다.
