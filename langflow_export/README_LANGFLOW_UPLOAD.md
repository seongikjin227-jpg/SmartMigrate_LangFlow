# Langflow Upload Package

이 폴더는 현재 프로젝트를 Langflow에서 호출할 수 있게 만든 export 패키지입니다.

회사 보안망 운영에서는 Custom Component 방식보다 `README_LANGFLOW_API_REQUEST.md`의 API Request 방식을 우선 권장합니다.

## 포함 파일

- `custom_components/migration_agent/migration_agent_controller.py`
  - Langflow Custom Component입니다.
  - 기존 `C:\Users\11824\Desktop\0609_final-main\main.py`와 `app.utils.agent_control`을 그대로 호출합니다.
- `custom_components/migration_agent/__init__.py`
  - Langflow가 컴포넌트를 로드하기 위한 패키지 파일입니다.
- `migration_agent_controller_flow.json`
  - Langflow 업로드용 단일 노드 플로우 초안입니다.
- `migration_agent_visual_pipeline_flow.json`
  - 다른 PC 배포를 염두에 둔 단계형 Langflow 플로우입니다.
  - `Load -> Poll -> Run` 노드들이 DB migration, SQL conversion, SQL tuning, SQL formatting 순서로 배치됩니다.
- `migration_agent_api_request_flow.json`
  - 회사 보안망 운영에 더 적합한 API Request 기반 플로우 초안입니다.
  - Langflow는 HTTP endpoint만 호출하고, Python agent 코드는 별도 Agent API 서버가 실행합니다.

## 권장 사용법

1. 다른 PC에 업로드할 경우 이 repository 전체를 먼저 복사합니다.
2. 대상 PC에서 Python dependencies를 설치합니다.

   ```powershell
   cd C:\path\to\0609_final-main
   py -m pip install -r requirements.txt
   ```

3. 대상 PC의 `.env`를 Oracle DB, LLM endpoint, model 설정에 맞게 수정합니다.
4. Langflow를 실행하는 환경에서 `LANGFLOW_COMPONENTS_PATH`를 이 폴더의 `custom_components`로 지정합니다.

   PowerShell 예시:

   ```powershell
   cd C:\path\to\0609_final-main
   $env:LANGFLOW_COMPONENTS_PATH="C:\path\to\0609_final-main\langflow_export\custom_components"
   langflow run --port 7860
   ```

5. Langflow 화면을 새로고침합니다.
6. `migration_agent_visual_pipeline_flow.json`을 import합니다.
7. 첫 노드의 `Project Path`를 대상 PC의 실제 repository 경로로 바꿉니다.

## 액션

- `status`: 현재 agent 실행 상태 조회
- `start`: 기존 `python main.py` 기반 agent 시작
- `stop`: agent 프로세스 종료
- `pause`: `runtime/agent.pause` 생성
- `resume`: pause 해제
- `command`: `runtime/chat_command.json`에 명령 저장, 다음 supervisor cycle에서 반영

## 단계형 플로우 노드

- `01 Load Migration Project`: `.env` 로드 및 project import path 설정
- `02 Poll DB Migration Jobs`: pending DB migration 조회
- `03 Run DB Migration Job`: 첫 pending DB migration 실행
- `04 Poll SQL Conversion Jobs`: pending SQL conversion 조회
- `05 Run SQL Conversion Job`: 첫 pending SQL conversion 실행
- `06 Poll SQL Tuning Jobs`: pending SQL tuning 조회
- `07 Run SQL Tuning Job`: 첫 pending SQL tuning 실행
- `08 Poll SQL Formatting Jobs`: pending SQL formatting 조회
- `09 Run SQL Formatting Job`: 첫 pending SQL formatting 실행
- `10 Pipeline Summary`: 실행 기록 요약

## 주의

- Langflow 서버가 이 프로젝트의 Python dependencies를 import할 수 있어야 합니다.
- 현재 프로젝트는 Oracle DB, LLM endpoint, `.env` 설정에 의존합니다.
- Langflow JSON만 업로드하면 커스텀 컴포넌트 코드가 없는 환경에서는 노드가 깨질 수 있습니다. 먼저 `LANGFLOW_COMPONENTS_PATH`로 컴포넌트를 로드하는 방식을 권장합니다.
- Langflow 기본 노드만으로 이 프로젝트의 Oracle repository, LangGraph subgraph, custom retry 로직을 완전히 재현하지 않습니다. 이 패키지는 Langflow에 보이는 노드를 단계별로 나누고, 각 노드가 기존 검증된 Python 구현을 호출하는 구조입니다.
