# Dashboard Command Tool 사용법

파일: `langflow_components/dashboard_command_tool.py`

Langflow 웹 UI에서 Custom Python Component를 만든 뒤, 이 파일의 코드를 붙여 넣는다.

## 먼저 테스트할 command

```json
{"action":"summary"}
```

`command_json`이 비어 있으면 기본으로 `summary`를 실행한다.

## 지원 action

| action | 설명 |
| --- | --- |
| `summary` | DB migration, SQL conversion, SQL tuning, SQL formatting 작업 대상 현황을 요약 |

## summary 반환 내용

응답은 read-only 요약이다. DB를 업데이트하지 않는다.

```json
{
  "ok": true,
  "action": "summary",
  "recommendations": [],
  "agents": {
    "db_migration": {},
    "sql_conversion": {},
    "sql_tuning": {},
    "sql_formatting": {}
  }
}
```

각 agent 요약에는 다음 값이 들어간다.

| 필드 | 설명 |
| --- | --- |
| `target_count` | 현재 작업 대상 건수 |
| `target_condition` | 작업 대상으로 본 조건 |
| `status_counts` | 상태별 전체 분포 |
| `next_jobs` | 우선순위 기준 다음 후보 샘플 |

## 작업 대상 조건

DB migration:

```sql
USE_YN = 'Y'
AND STATUS IS NULL
```

SQL conversion:

```sql
STATUS_CONVERSION IS NULL
OR STATUS_CONVERSION = 'READY'
```

`TO_SQL_TEXT` 존재 여부는 보지 않는다.

SQL tuning:

```sql
STATUS_TUNING IN ('READY', 'URGENT', 'FAIL', 'FAIL-TUNED', 'FAIL-BIND', 'FAIL-TEST')
AND TO_SQL_TEXT IS NOT NULL
AND STATUS_CONVERSION IN ('PASS-CONVERSION', 'PASS')
```

SQL formatting:

```sql
FORMATTING_RETRY_YN = 'Y'
```

`STATUS_TUNING`, `FORMATTING_RETRY_YN`, `FORMATTED_SQL` 같은 컬럼이 없는 환경에서는 해당 agent를 `available=false`로 반환한다.

## Supervisor 사용 방향

Supervisor가 사용자와 첫 대화를 시작할 때 `{"action":"summary"}`를 먼저 호출하면 된다.

추천 기준은 현재 단순 우선순위다.

```text
DB_MIGRATION -> SQL_CONVERSION -> SQL_TUNING -> SQL_FORMATTING
```

응답의 `recommendations[0]`를 보고 다음 실행 후보를 사용자에게 제안한다.

## DB 연결 입력값

```text
db_host=10.10.10.10 또는 db.company.local
db_port=1521
db_service_name=ORCLPDB1
db_username=scott
db_password=tiger
system_schema=
list_limit=5
```
