# Langflow API Request 방식

회사 보안망에서는 Langflow 서버에 repository 전체와 `.env`를 올리는 Custom Component 방식보다, 별도 Agent API 서버를 두고 Langflow가 HTTP로만 호출하는 방식이 안전합니다.

## 구조

```text
Langflow
  -> API Request Node
  -> Migration Agent API Server
  -> 기존 Python agent 코드
  -> Oracle DB / LLM
```

Langflow 서버는 Python agent 코드를 import하지 않습니다. DB/LLM 접속정보도 Langflow가 아니라 Agent API 서버의 `.env`에만 둡니다.

## Agent API 서버 실행

Agent API 서버를 띄울 컴퓨터에서:

```powershell
cd C:\Users\11824\Desktop\0609_final-main
py -m pip install -r requirements.txt
py -m uvicorn server.api.langflow_api:app --host 0.0.0.0 --port 8010
```

브라우저 또는 curl에서 확인:

```text
http://127.0.0.1:8010/health
```

## 인증

`.env`에 `LANGFLOW_API_KEY`를 설정하면 API는 인증을 요구합니다.

```env
LANGFLOW_API_KEY=change-this-key
```

Langflow API Request 헤더:

```text
X-API-Key: change-this-key
```

또는:

```text
Authorization: Bearer change-this-key
```

## Endpoint

기본 base URL 예시:

```text
http://agent-api.company.local:8010
```

로컬 테스트:

```text
http://127.0.0.1:8010
```

### Health / Agent Control

```text
GET  /health
GET  /agent/status
POST /agent/start
POST /agent/stop
POST /agent/pause
POST /agent/resume
POST /agent/command
```

`POST /agent/command` body:

```json
{
  "command": "다음 사이클에 실패 작업을 점검해줘"
}
```

### Pipeline Jobs

```text
GET  /jobs/migration/pending
POST /jobs/migration/run
GET  /jobs/sql-conversion/pending
POST /jobs/sql-conversion/run
GET  /jobs/sql-tuning/pending
POST /jobs/sql-tuning/run
GET  /jobs/sql-formatting/pending
POST /jobs/sql-formatting/run
```

run endpoint body는 비워두면 첫 pending job을 실행합니다.

특정 migration:

```json
{
  "map_id": 1001
}
```

특정 SQL job:

```json
{
  "row_id": "AAAZ9xAAEAAABrXAAA"
}
```

## Langflow에서 노드 구성

API Request 컴포넌트를 단계별로 배치합니다.

```text
GET  /health
GET  /jobs/migration/pending
POST /jobs/migration/run
GET  /jobs/sql-conversion/pending
POST /jobs/sql-conversion/run
GET  /jobs/sql-tuning/pending
POST /jobs/sql-tuning/run
GET  /jobs/sql-formatting/pending
POST /jobs/sql-formatting/run
GET  /agent/status
```

업로드용 초안:

```text
langflow_export/migration_agent_api_request_flow.json
```

Langflow 버전에 따라 API Request 컴포넌트 내부 필드명이 달라질 수 있습니다. import 후 각 노드의 URL, Method, Headers를 화면에서 한 번 확인하세요.
