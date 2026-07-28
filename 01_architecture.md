# SmartMigration Langflow Architecture

목표: 기존 `0609_final-main` Migration Agent를 Langflow 웹 UI에서 Custom Python Component와 Agent/Tool 구조로 재구성한다.

## 결론

최종 구조는 가능하다.

```text
Supervisor Agent
  -> DB Migration Agent as Tool
      -> Migration Command Tool
          -> Oracle DB
          -> LLM
          -> NEXT_MIG_INFO / NEXT_MIG_INFO_DTL / NEXT_MIG_LOG
```

다만 구현 순서는 최종 구조와 반대로 간다.

```text
1. Migration Command Tool 단독 테스트
2. DB Migration Agent + Migration Command Tool 테스트
3. Supervisor Agent가 DB Migration Agent를 Tool로 호출하는 구조 테스트
4. SQL Conversion/Tuning/Formatting Agent 확장
```

처음부터 Supervisor까지 만들면 실패 원인이 Agent 판단 문제인지, Tool 입력 문제인지, DB 로직 문제인지 구분하기 어렵다.

## 왜 Migration Command Tool 하나로 시작하는가

DB Migration은 중간 상태가 강하게 연결된 작업이다.

```text
DDL 조회
-> SQL 생성
-> SQL 저장
-> SQL 실행
-> Verify SQL 생성
-> Verify SQL 저장
-> Verify 실행
-> PASS/FAIL 저장
-> 로그 저장
-> 실패 시 retry
```

이 단계를 Langflow 노드 여러 개로 쪼개면 다음 값을 계속 edge로 넘겨야 한다.

```text
map_id
source_ddl
target_ddl
mig_sql
verify_sql
last_error
retry_count
failure_status
elapsed_time
batch_count
user_edited
prior_map_id
```

이 구조는 Langflow에서 유지보수가 어렵고, Agent가 중간 상태를 잘못 전달할 위험이 크다. 따라서 중간 상태는 Langflow edge가 아니라 Custom Component 내부 로직과 Oracle DB에 둔다.

## 역할 분리

| 영역 | 책임 |
| --- | --- |
| Langflow Supervisor Agent | 사용자 요청 해석, 어떤 Agent/Tool을 부를지 결정 |
| DB Migration Agent | migration 관련 요청만 해석하고 Migration Command Tool 호출 |
| Migration Command Tool | 실제 DB 조회, SQL 생성/실행, 검증, 상태 저장, 로그 저장 |
| Oracle DB | durable state 저장소 |
| LLM | SQL 생성/검증 SQL 생성 보조 |

## 최종 Flow 형태

### 1단계: 단독 테스트 Flow

```text
Chat Input
-> DB Migration Agent
-> Migration Command Tool
-> Chat Output
```

### 2단계: Supervisor 포함 Flow

```text
Chat Input
-> Supervisor Agent
   -> DB Migration Agent as Tool
       -> Migration Command Tool
-> Chat Output
```

## Supervisor가 DB Migration Agent를 Tool로 불러도 되는가

가능하다. 다만 DB Migration Agent 자체는 복잡한 그래프가 아니라 다음 역할로 제한해야 한다.

```text
사용자 요청을 migration command JSON으로 변환
-> Migration Command Tool 호출
-> 결과 해석
```

DB Migration Agent가 직접 SQL 실행 로직을 들고 있으면 Supervisor와 역할 경계가 흐려진다. 실제 실행은 Migration Command Tool에 둔다.

## 권장 구현 순서

| 단계 | 목적 | 성공 기준 |
| --- | --- | --- |
| 1 | Migration Command Tool에서 `status` 실행 | `map_id`로 NEXT_MIG_INFO 조회 가능 |
| 2 | `reset` 실행 | STATUS/MIG_SQL/VERIFY_SQL 초기화 가능 |
| 3 | `save_user_sql` 실행 | USER_EDITED='Y'와 SQL 저장 가능 |
| 4 | `run_migration_job` 실행 | SQL 생성/실행/검증/상태 저장 가능 |
| 5 | DB Migration Agent 연결 | 자연어 요청을 command_json으로 바꿔 Tool 호출 |
| 6 | Supervisor Agent 연결 | Supervisor가 DB Migration Agent를 적절히 호출 |

## 중요한 설계 원칙

1. Tool input은 작게 유지한다.
2. `map_id`를 durable job identifier로 사용한다.
3. 내부 상태는 DB에 저장한다.
4. Agent는 상태머신이 아니라 라우터다.
5. SQL 생성/실행/검증의 retry loop는 Tool 내부에서 처리한다.
6. Langflow 노드는 운영자가 이해할 큰 업무 단위로만 나눈다.
