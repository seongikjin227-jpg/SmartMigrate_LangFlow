from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from urllib.parse import quote_plus, urlparse
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import BoolInput, IntInput, MessageTextInput, SecretStrInput, StrInput, Output
from lfx.schema.data import Data


class MigrationCommandTool(Component):
    # Langflow exposes this class as one custom component. The Agent only fills
    # command_json; DB, LLM, schema, and prompt values stay as component inputs.
    display_name = "Migration Command Tool"
    description = "Controls SmartMigration DB migration jobs through Oracle metadata tables."
    name = "MigrationCommandTool"
    icon = "Database"

    _db_cache: dict[str, Any] = {}

    # Keep only command_json in tool mode. This prevents the Agent from seeing or
    # modifying credentials and prompt text during normal tool calls.
    inputs = [
        MessageTextInput(
            name="command_json",
            display_name="Command JSON",
            required=True,
            tool_mode=True,
            info='JSON command. Example: {"action":"status","map_id":101}',
        ),
        StrInput(
            name="db_host",
            display_name="DB Host",
            required=True,
            info="Oracle host or scan address. Example: 10.10.10.10 or db.company.local",
        ),
        IntInput(
            name="db_port",
            display_name="DB Port",
            value=1521,
            required=True,
            info="Oracle listener port. Default: 1521",
        ),
        StrInput(
            name="db_service_name",
            display_name="Service Name",
            required=True,
            info="Oracle service name. Example: ORCLPDB1",
        ),
        StrInput(
            name="db_username",
            display_name="Username",
            required=True,
        ),
        SecretStrInput(
            name="db_password",
            display_name="Password",
            required=True,
        ),
        StrInput(
            name="llm_base_url",
            display_name="LLM Base URL",
            required=False,
            info="OpenAI-compatible LLM gateway base URL. Only OpenAI-compatible chat/completions is supported.",
        ),
        SecretStrInput(
            name="llm_api_key",
            display_name="LLM API Key",
            required=False,
        ),
        StrInput(
            name="llm_model",
            display_name="LLM Model",
            value="claude-haiku-4-5-20251001",
            required=False,
        ),
        IntInput(
            name="llm_max_tokens",
            display_name="LLM Max Tokens",
            value=4096,
            required=False,
        ),
        IntInput(
            name="llm_timeout_seconds",
            display_name="LLM Timeout Seconds",
            value=900,
            required=False,
            info="HTTP timeout for LLM API calls. Default: 900 seconds.",
        ),
        MessageTextInput(
            name="mig_sql_prompt",
            display_name="MIG SQL Prompt",
            required=False,
            info="Prompt template for generate_mig_sql. Use placeholders: {ddl_info_block}, {from_table}, {to_table}, {mapping_info}, {condition}, {source_kind}, {source_query}, {source_from_clause}, {complex_source_note}, {retry_context}, {last_error}, {last_sql}.",
        ),
        MessageTextInput(
            name="verify_sql_prompt",
            display_name="VERIFY SQL Prompt",
            required=False,
            info="Prompt template for generate_verify_sql. Use placeholders: {ddl_info_block}, {from_table}, {to_table}, {mapping_info}, {condition}, {source_kind}, {source_query}, {source_from_clause}, {complex_source_note}, {retry_context}, {last_error}, {last_sql}.",
        ),
        StrInput(
            name="system_schema",
            display_name="System Schema",
            required=False,
            info="Schema containing NEXT_MIG_INFO/NEXT_MIG_INFO_DTL/NEXT_MIG_LOG. Leave blank for current user.",
        ),
        StrInput(
            name="source_schema",
            display_name="Source Schema",
            required=False,
            info="Optional schema prefix for source tables in FR_TABLE.",
        ),
        StrInput(
            name="target_schema",
            display_name="Target Schema",
            required=False,
            info="Optional schema prefix for target TO_TABLE.",
        ),
        IntInput(
            name="default_max_attempts",
            display_name="Default Max Attempts",
            value=3,
            required=False,
        ),
        BoolInput(
            name="auto_install_packages",
            display_name="Auto Install Missing Packages",
            value=False,
            required=False,
            info="If true, installs missing runtime packages with pip before DB connection. Keep false unless Langflow runtime lacks dependencies.",
        ),
        StrInput(
            name="pip_trusted_host",
            display_name="Pip Trusted Host",
            required=False,
            info="Optional trusted host for internal PyPI/proxy. Hostname is extracted if a full URL is entered.",
        ),
    ]

    outputs = [
        Output(display_name="Result", name="result", method="run_command"),
    ]

    def run_command(self) -> Data:
        # Single-tool/multi-action router. Langflow calls this method once for
        # every tool invocation, and the action field selects the internal path.
        try:
            command = self._parse_command()
            action = (command.get("action") or "").strip().lower()
            map_id = command.get("map_id")

            # action 값에 따라 실제 내부 함수로 매핑한다.
            # 예: {"action":"status"} -> _status(), {"action":"run_migration_job"} -> _run_migration_job()
            if action == "test_connection":
                # DB 연결과 LLM 연결을 동시에 확인한다.
                result = self._test_connection()
            elif action == "status":
                # map_id 기준으로 NEXT_MIG_INFO master와 detail 매핑을 조회한다.
                result = self._status(map_id)
            elif action == "list_pending":
                # STATUS IS NULL인 실행 후보 migration job 목록을 조회한다.
                result = self._list_pending(command.get("limit", 10))
            elif action == "get_table_ddl":
                # Oracle USER_TAB_COLUMNS/ALL_TAB_COLUMNS에서 컬럼 메타데이터를 조회한다.
                result = self._get_table_ddl(command.get("table_name"), command.get("schema"))
            elif action == "generate_mig_sql":
                # MIG_SQL을 LLM으로 생성한다. 일반 호출은 DB 저장 없는 preview다.
                result = self._generate_mig_sql(map_id, command)
            elif action == "generate_verify_sql":
                # VERIFY_SQL을 LLM으로 생성한다. 일반 호출은 DB 저장 없는 preview다.
                result = self._generate_verify_sql(map_id, command)
            elif action == "preview_mig_prompt":
                # MIG_SQL 생성용 prompt가 실제 값으로 치환된 결과만 반환한다.
                result = self._preview_sql_prompt(map_id, command, prompt_kind="mig")
            elif action == "preview_verify_prompt":
                # VERIFY_SQL 생성용 prompt가 실제 값으로 치환된 결과만 반환한다.
                result = self._preview_sql_prompt(map_id, command, prompt_kind="verify")
            elif action == "reset":
                # 재실행을 위해 STATUS/RETRY_COUNT/BATCH_CNT를 초기화한다. confirm=true 필요.
                result = self._reset(map_id)
            elif action == "save_user_sql":
                # 사용자가 직접 수정한 SQL을 저장하고 USER_EDITED='Y'로 표시한다. confirm=true 필요.
                result = self._save_user_sql(map_id, command)
            elif action == "analyze_failure":
                # 최신 실패 로그와 저장된 MIG_SQL/VERIFY_SQL을 조회한다.
                result = self._analyze_failure(map_id)
            elif action == "run_migration_job":
                # SQL 생성 -> 저장 -> MIG_SQL 실행 -> VERIFY_SQL 생성/실행 -> 최종 STATUS 저장 전체 사이클.
                result = self._run_migration_job(map_id, command)
            else:
                result = {"ok": False, "error": f"Unsupported action: {action}"}

            self.status = result
            return Data(data=result)
        except Exception as exc:
            result = {"ok": False, "error": str(exc)}
            self.status = result
            return Data(data=result)

    def _parse_command(self) -> dict[str, Any]:
        # Langflow Agent가 넘긴 command_json 문자열을 dict로 변환한다.
        # 여기서 action/map_id/confirm 같은 실행 파라미터가 처음 해석된다.
        raw = self.command_json
        if isinstance(raw, dict):
            return raw
        text = str(raw or "").strip()
        if not text:
            raise ValueError("command_json is required")
        return json.loads(text)

    def _connection_string(self) -> str:
        # Company runtime uses SQLAlchemy URLs through SQLDatabase.from_uri().
        host = str(self.db_host or "").strip()
        port = int(self.db_port or 1521)
        service_name = str(self.db_service_name or "").strip()
        username = str(self.db_username or "").strip()
        password = str(self.db_password or "")
        if not host:
            raise ValueError("DB Host is required")
        if not service_name:
            raise ValueError("Service Name is required")
        if not username:
            raise ValueError("Username is required")
        return f"oracle+oracledb://{quote_plus(username)}:{quote_plus(password)}@{host}:{port}/{service_name}"

    def _get_db(self):
        # Cache SQLDatabase per connection identity so repeated tool calls in the
        # same Langflow process do not rebuild the SQLAlchemy engine every time.
        self._ensure_runtime_dependencies()
        from langchain_community.utilities import SQLDatabase

        # DB 캐시 키는 이 함수에서만 쓰이므로 별도 helper로 빼지 않고 여기서 만든다.
        cache_key = "|".join(
            [
                str(self.db_host or "").strip(),
                str(self.db_port or 1521),
                str(self.db_service_name or "").strip(),
                str(self.db_username or "").strip(),
            ]
        )
        if cache_key not in self._db_cache:
            self._db_cache[cache_key] = SQLDatabase.from_uri(self._connection_string())
        self.db = self._db_cache[cache_key]
        return self.db

    def _ensure_runtime_dependencies(self) -> None:
        # Langflow deployments may not have Oracle/SQLAlchemy packages installed.
        # Auto install is opt-in because production runtimes often block pip.
        missing_packages: list[str] = []
        try:
            import langchain_community  # noqa: F401
        except ModuleNotFoundError:
            missing_packages.append("langchain-community")
        try:
            import sqlalchemy  # noqa: F401
        except ModuleNotFoundError:
            missing_packages.append("SQLAlchemy")
        try:
            import oracledb  # noqa: F401
        except ModuleNotFoundError:
            missing_packages.append("oracledb")

        if not missing_packages:
            return

        if not bool(self.auto_install_packages):
            raise ModuleNotFoundError(
                "Missing packages: "
                + ", ".join(missing_packages)
                + ". Enable Auto Install Missing Packages or install them in the Langflow runtime."
            )

        for package in missing_packages:
            self._pip_install(package)

    def _pip_install(self, package: str) -> None:
        command = [sys.executable, "-m", "pip", "install", package]
        trusted_host = self._normalize_trusted_host(self.pip_trusted_host)
        if trusted_host:
            command.extend(["--trusted-host", trusted_host])
        subprocess.check_call(command)

    def _normalize_trusted_host(self, value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        parsed = urlparse(text if "://" in text else f"//{text}")
        host = parsed.hostname or text.split("/")[0]
        return host.strip()

    # action="test_connection": DB 연결과 LLM 연결을 한 번에 점검한다.
    def _test_connection(self) -> dict[str, Any]:
        # 연결 테스트는 DB와 LLM을 둘 다 확인한다.
        # 하나라도 실패하면 ok=false로 반환된다.
        try:
            rows = self._normalize_query_rows(self._get_db().run("SELECT 1 AS OK FROM DUAL", include_columns=True))
            db_result = {"ok": True, "message": "DB connection OK", "result": rows}
        except Exception as exc:
            db_result = {"ok": False, "message": "DB connection failed", "error": str(exc)}

        # LLM은 OpenAI-compatible chat/completions 방식만 지원한다.
        # provider 분기는 운영상 불필요해서 두지 않는다.
        api_key = str(self.llm_api_key or "").strip()
        model = str(self.llm_model or "").strip()
        if not api_key:
            llm_result = {"ok": False, "message": "LLM API key is empty"}
        elif not model:
            llm_result = {"ok": False, "message": "LLM model is empty"}
        else:
            try:
                base_url = str(self.llm_base_url or "https://api.openai.com/v1").strip().rstrip("/")
                url = base_url if base_url.endswith("/chat/completions") else f"{base_url}/chat/completions"
                payload = {
                    "model": model,
                    "messages": [{"role": "user", "content": "Return OK only."}],
                    "max_tokens": 8,
                    "temperature": 0,
                }
                data = self._post_json(url, payload, {"Authorization": f"Bearer {api_key}"})
                content = ""
                try:
                    content = data["choices"][0]["message"].get("content", "")
                except Exception:
                    content = ""
                llm_result = {"ok": True, "provider": "openai-compatible", "model": model, "url": url, "response_preview": str(content)[:200]}
            except Exception as exc:
                llm_result = {"ok": False, "provider": "openai-compatible", "model": model, "error": str(exc)}

        return {
            "ok": bool(db_result.get("ok")) and bool(llm_result.get("ok")),
            "db": db_result,
            "llm": llm_result,
        }

    def _post_json(self, url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        req_headers = {"Content-Type": "application/json", **headers}
        request = urllib.request.Request(url, data=body, headers=req_headers, method="POST")
        try:
            timeout_seconds = max(1, int(self.llm_timeout_seconds or 900))
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                raw = response.read().decode("utf-8", errors="ignore")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")[:1000]
            raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc

    def _normalize_query_rows(self, raw: Any) -> list[dict[str, Any]]:
        if raw is None or raw == "":
            return []
        if isinstance(raw, list):
            if not raw:
                return []
            if isinstance(raw[0], dict):
                return raw
            return [{str(i): value for i, value in enumerate(row)} for row in raw]
        if isinstance(raw, tuple):
            return [{str(i): value for i, value in enumerate(raw)}]
        if isinstance(raw, str):
            text = raw.strip()
            if not text:
                return []
            try:
                parsed = ast.literal_eval(text)
            except Exception:
                return [{"text": text}]
            return self._normalize_query_rows(parsed)
        return [{"value": raw}]

    # action="get_table_ddl": Oracle 컬럼 메타데이터를 DDL 참고용으로 조회한다.
    def _get_table_ddl(self, table_name: Any, schema: Any = None) -> dict[str, Any]:
        clean_table = str(table_name or "").strip().upper()
        clean_schema = str(schema or "").strip().upper()
        if not clean_table:
            raise ValueError("table_name is required")
        if "." in clean_table and not clean_schema:
            clean_schema, clean_table = clean_table.split(".", 1)
        self._validate_identifier(clean_table, "table_name")
        if clean_schema:
            self._validate_identifier(clean_schema, "schema")
            query = f"""
                SELECT COLUMN_ID, COLUMN_NAME, DATA_TYPE, DATA_LENGTH, DATA_PRECISION,
                       DATA_SCALE, NULLABLE
                FROM ALL_TAB_COLUMNS
                WHERE OWNER = '{clean_schema}'
                  AND TABLE_NAME = '{clean_table}'
                ORDER BY COLUMN_ID
            """
        else:
            query = f"""
                SELECT COLUMN_ID, COLUMN_NAME, DATA_TYPE, DATA_LENGTH, DATA_PRECISION,
                       DATA_SCALE, NULLABLE
                FROM USER_TAB_COLUMNS
                WHERE TABLE_NAME = '{clean_table}'
                ORDER BY COLUMN_ID
            """
        rows = self._normalize_query_rows(self._get_db().run(query, include_columns=True))

        # get_table_ddl 전용 row 값 추출 함수.
        # SQLDatabase.run() 결과 key 대소문자가 환경마다 달라질 수 있어 여기서만 보정한다.
        def column_value(row: dict[str, Any], key: str) -> Any:
            if key in row:
                return row[key]
            for candidate_key, value in row.items():
                if str(candidate_key).upper() == key.upper():
                    return value
            return None

        columns = [
            {
                "column_id": column_value(row, "COLUMN_ID"),
                "column_name": self._to_text(column_value(row, "COLUMN_NAME")),
                "data_type": self._to_text(column_value(row, "DATA_TYPE")),
                "data_length": column_value(row, "DATA_LENGTH"),
                "data_precision": column_value(row, "DATA_PRECISION"),
                "data_scale": column_value(row, "DATA_SCALE"),
                "nullable": self._to_text(column_value(row, "NULLABLE")),
            }
            for row in rows
        ]
        return {
            "ok": True,
            "schema": clean_schema or "CURRENT_USER",
            "table_name": clean_table,
            "column_count": len(columns),
            "columns": columns,
        }

    @contextmanager
    def _connect(self):
        # SQLDatabase is used for cached engine management; raw_connection is used
        # where rowcount/commit behavior matters.
        db = self._get_db()
        engine = getattr(db, "_engine", None) or getattr(db, "engine", None)
        if engine is None:
            raise ValueError("SQLDatabase engine is not available")
        conn = engine.raw_connection()
        try:
            yield conn
        finally:
            conn.close()

    # action="status": map_id 한 건의 master/detail 현재 상태를 조회한다.
    def _status(self, map_id: Any) -> dict[str, Any]:
        # map_id 기준으로 NEXT_MIG_INFO 1건과 NEXT_MIG_INFO_DTL 매핑 목록을 조회한다.
        # 상태 판단이 필요할 때 Agent는 이 action 결과를 기준으로 답해야 한다.
        map_id = self._require_map_id(map_id)
        job = self._load_job(map_id)
        if not job:
            return {"ok": False, "map_id": map_id, "error": "job not found"}
        details = self._load_details(map_id)
        return {"ok": True, "job": job, "details": details}

    # action="list_pending": 실행 가능한 migration 후보 목록을 조회한다.
    def _list_pending(self, limit: Any) -> dict[str, Any]:
        # STATUS IS NULL이고 USE_YN='Y'인 작업만 실행 후보로 조회한다.
        # 실제 실행 순서는 PRIORITY, MAP_ID 기준으로 정렬된다.
        safe_limit = max(1, min(int(limit or 10), 50))
        map_table = self._system_table("NEXT_MIG_INFO")
        sql = f"""
            SELECT * FROM (
                SELECT MAP_ID, MAP_TYPE, FR_TABLE, TO_TABLE, USE_YN, TRUNC_YN,
                       PRIORITY, STATUS, USER_EDITED, PRIOR_MAP_ID, RETRY_COUNT, UPD_TS
                FROM {map_table}
                WHERE UPPER(TRIM(NVL(USE_YN, 'N'))) = 'Y'
                  AND STATUS IS NULL
                ORDER BY PRIORITY ASC, MAP_ID ASC
            ) WHERE ROWNUM <= {safe_limit}
        """
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(sql)
            rows = cur.fetchall()
        jobs = [
            {
                "map_id": r[0],
                "map_type": self._to_text(r[1]),
                "fr_table": self._to_text(r[2]),
                "to_table": self._to_text(r[3]),
                "use_yn": self._to_text(r[4]),
                "trunc_yn": self._to_text(r[5]),
                "priority": r[6],
                "status": self._to_text(r[7]),
                "user_edited": self._to_text(r[8]),
                "prior_map_id": r[9],
                "retry_count": r[10],
                "upd_ts": self._to_text(r[11]),
            }
            for r in rows
        ]
        return {"ok": True, "count": len(jobs), "jobs": jobs}

    # action="reset": 재실행 전 STATUS/RETRY/BATCH 상태를 초기화한다. confirm=true 필요.
    def _reset(self, map_id: Any) -> dict[str, Any]:
        # Reset is intentionally narrow: it makes the job runnable again but
        # preserves generated/user SQL so the user can inspect or overwrite it.
        map_id = self._require_map_id(map_id)
        command = self._parse_command()
        if not self._as_bool(command.get("confirm", False)):
            return {
                "ok": False,
                "map_id": map_id,
                "status": "CONFIRM_REQUIRED",
                "error": "reset requires confirm=true because it changes DB state.",
            }
        map_table = self._system_table("NEXT_MIG_INFO")
        sql = f"""
            UPDATE {map_table}
            SET STATUS = NULL,
                RETRY_COUNT = 0,
                BATCH_CNT = 0,
                UPD_TS = CURRENT_TIMESTAMP
            WHERE MAP_ID = :1
        """
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(sql, [map_id])
            rowcount = cur.rowcount
            conn.commit()
        self._write_log(map_id, "RESET", "INFO", "RESET", "PASS", "Job reset. SQL values preserved.")
        return {"ok": rowcount > 0, "map_id": map_id, "updated_rows": rowcount}

    # action="save_user_sql": 사용자가 직접 수정한 MIG_SQL/VERIFY_SQL을 저장한다. confirm=true 필요.
    def _save_user_sql(self, map_id: Any, command: dict[str, Any]) -> dict[str, Any]:
        # This is the only path that sets USER_EDITED=Y. LLM generation must not
        # mark a row as user edited.
        map_id = self._require_map_id(map_id)
        if not self._as_bool(command.get("confirm", False)):
            return {
                "ok": False,
                "map_id": map_id,
                "status": "CONFIRM_REQUIRED",
                "error": "save_user_sql requires confirm=true because it changes DB state and sets USER_EDITED=Y.",
            }
        mig_sql = command.get("mig_sql") or ""
        verify_sql = command.get("verify_sql") or ""
        if not str(mig_sql).strip():
            return {"ok": False, "map_id": map_id, "error": "mig_sql is required"}

        map_table = self._system_table("NEXT_MIG_INFO")
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                f"""
                UPDATE {map_table}
                SET MIG_SQL = :1,
                    VERIFY_SQL = :2,
                    USER_EDITED = 'Y',
                    STATUS = NULL,
                    UPD_TS = CURRENT_TIMESTAMP
                WHERE MAP_ID = :3
                """,
                [str(mig_sql), str(verify_sql), map_id],
            )
            rowcount = cur.rowcount
            conn.commit()
        self._write_log(map_id, "SAVE_USER_SQL", "INFO", "USER_SQL", "PASS", "User SQL saved", generate_sql=str(mig_sql))
        return {"ok": rowcount > 0, "map_id": map_id, "updated_rows": rowcount}

    # action="analyze_failure": 최신 실패 로그와 저장 SQL을 읽어서 실패 분석용 데이터를 반환한다.
    def _analyze_failure(self, map_id: Any) -> dict[str, Any]:
        # Failure analysis is read-only. It returns the latest failure-looking log
        # first, then recent logs for supporting context.
        map_id = self._require_map_id(map_id)
        log_table = self._system_table("NEXT_MIG_LOG")
        map_table = self._system_table("NEXT_MIG_INFO")
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                f"""
                SELECT STATUS, MIG_SQL, VERIFY_SQL, RETRY_COUNT, ELAPSED_SECONDS, UPD_TS
                FROM {map_table}
                WHERE MAP_ID = :1
                """,
                [map_id],
            )
            job_row = cur.fetchone()
            cur.execute(
                f"""
                SELECT LOG_ID, LOG_TYPE, LOG_LEVEL, STEP_NAME, STATUS, MESSAGE,
                       GENERATE_SQL,
                       TO_CHAR(CREATED_AT, 'YYYY-MM-DD HH24:MI:SS') AS LOG_TIME
                FROM {log_table}
                WHERE MAP_ID = :1
                ORDER BY CREATED_AT DESC, LOG_ID DESC
                FETCH FIRST 10 ROWS ONLY
                """,
                [map_id],
            )
            rows = cur.fetchall()

        recent_logs = [
            {
                "log_id": r[0],
                "log_type": self._to_text(r[1]),
                "log_level": self._to_text(r[2]),
                "step_name": self._to_text(r[3]),
                "status": self._to_text(r[4]),
                "message": self._to_text(r[5]),
                "generate_sql": self._to_text(r[6]),
                "log_time": self._to_text(r[7]),
            }
            for r in rows
        ]
        latest_failure_log = next(
            (
                log
                for log in recent_logs
                if log["log_level"].upper() == "ERROR"
                or log["status"].upper().startswith("FAIL")
                or log["log_type"].upper() in {"ROW_ERROR", "JOB_FAIL"}
            ),
            None,
        )

        return {
            "ok": True,
            "map_id": map_id,
            "job": None
            if not job_row
            else {
                "status": self._to_text(job_row[0]),
                "mig_sql": self._to_text(job_row[1]),
                "verify_sql": self._to_text(job_row[2]),
                "retry_count": job_row[3],
                "elapsed_seconds": job_row[4],
                "upd_ts": self._to_text(job_row[5]),
            },
            "latest_failure_log": latest_failure_log,
            "recent_logs": recent_logs,
        }

    # action="generate_mig_sql": MIG_SQL을 생성한다. public 호출은 preview, 내부 호출은 저장까지 수행한다.
    def _generate_mig_sql(self, map_id: Any, command: dict[str, Any]) -> dict[str, Any]:
        # Public calls are preview-only. run_migration_job passes _internal_run so
        # generated SQL can be saved as part of the controlled execution cycle.
        map_id = self._require_map_id(map_id)
        job = self._load_job(map_id)
        if not job:
            return {"ok": False, "map_id": map_id, "error": "job not found"}

        force_regenerate = self._as_bool(command.get("force_regenerate", False))
        internal_run = self._as_bool(command.get("_internal_run", False))
        save = internal_run
        user_edited = str(job.get("user_edited") or "").strip().upper() == "Y"
        existing_mig_sql = str(job.get("mig_sql") or "").strip()
        # USER_EDITED=Y이면 사용자가 직접 넣은 SQL을 우선한다.
        # force_regenerate가 없으면 LLM으로 MIG_SQL을 다시 만들지 않는다.
        if user_edited and not force_regenerate:
            if existing_mig_sql:
                return {
                    "ok": True,
                    "map_id": map_id,
                    "status": "MIG_SQL_SKIPPED_USER_EDITED",
                    "message": "USER_EDITED=Y. Existing MIG_SQL was preserved.",
                    "generation_source": "user_edited",
                    "mig_sql": existing_mig_sql,
                }
            return {"ok": False, "map_id": map_id, "error": "USER_EDITED=Y but MIG_SQL is empty"}

        dep = self._check_dependencies(job)
        # 선행 MAP_ID 또는 같은 TO_TABLE의 낮은 PRIORITY 작업이 준비되지 않으면 생성도 중단한다.
        if dep["status"] != "READY":
            final_status = "SKIP" if dep["status"] in {"FAIL", "SKIP"} else "WAITING"
            return {"ok": False, "map_id": map_id, "status": final_status, "message": dep["message"]}

        details = self._load_details(map_id)
        if not details:
            return {"ok": False, "map_id": map_id, "error": "No mapping details found"}

        generation_source = "llm"
        llm_error = ""

        try:
            # 1. Langflow input prompt에 DDL/매핑/조건/retry context를 치환한다.
            # 2. LLM을 호출한다.
            # 3. JSON 응답에서 migration_sql만 꺼내고 INSERT 단일문인지 검증한다.
            prompt = self._render_sql_prompt(
                template=self._require_prompt("mig_sql_prompt", "MIG SQL Prompt"),
                job=job,
                details=details,
                command=command,
            )
            mig_sql = self._sanitize_migration_sql(
                self._extract_sql(self._call_llm(prompt), expected="insert", key="migration_sql")
            )
        except Exception as exc:
            llm_error = str(exc)
            return {"ok": False, "map_id": map_id, "error": llm_error, "generation_source": generation_source}

        if save:
            # preview action에서는 save=false다.
            # run_migration_job 내부 호출일 때만 생성 SQL을 NEXT_MIG_INFO.MIG_SQL에 저장한다.
            self._save_mig_sql(map_id, mig_sql)
            self._write_log(
                map_id,
                "GENERATE_SQL",
                "INFO",
                "GENERATE_MIG_SQL",
                "PASS",
                f"MIG_SQL generated by {generation_source}",
                generate_sql=mig_sql,
            )

        return {
            "ok": True,
            "map_id": map_id,
            "status": "MIG_SQL_GENERATED",
            "generation_source": generation_source,
            "llm_error": llm_error,
            "mig_sql": mig_sql,
        }

    # action="generate_verify_sql": VERIFY_SQL을 생성한다. public 호출은 preview, 내부 호출은 저장까지 수행한다.
    def _generate_verify_sql(self, map_id: Any, command: dict[str, Any]) -> dict[str, Any]:
        # VERIFY_SQL follows the same preview/internal-save split as MIG_SQL.
        # USER_EDITED rows preserve existing SQL unless force_regenerate is set.
        map_id = self._require_map_id(map_id)
        job = self._load_job(map_id)
        if not job:
            return {"ok": False, "map_id": map_id, "error": "job not found"}

        force_regenerate = self._as_bool(command.get("force_regenerate", False))
        internal_run = self._as_bool(command.get("_internal_run", False))
        save = internal_run
        user_edited = str(job.get("user_edited") or "").strip().upper() == "Y"
        existing_mig_sql = str(job.get("mig_sql") or "").strip()
        existing_verify_sql = str(job.get("verify_sql") or "").strip()
        # 사용자 수정 SQL이 있는 경우 VERIFY_SQL도 기존 값을 보존한다.
        # 단, MIG_SQL은 있는데 VERIFY_SQL이 비어 있으면 VERIFY_SQL 생성은 허용된다.
        if user_edited and not force_regenerate:
            if not existing_mig_sql:
                return {"ok": False, "map_id": map_id, "error": "USER_EDITED=Y but MIG_SQL is empty"}
            if existing_verify_sql:
                return {
                    "ok": True,
                    "map_id": map_id,
                    "status": "VERIFY_SQL_SKIPPED_USER_EDITED",
                    "message": "USER_EDITED=Y. Existing VERIFY_SQL was preserved.",
                    "generation_source": "user_edited",
                    "verify_sql": existing_verify_sql,
                }

        dep = self._check_dependencies(job)
        if dep["status"] != "READY":
            final_status = "SKIP" if dep["status"] in {"FAIL", "SKIP"} else "WAITING"
            return {"ok": False, "map_id": map_id, "status": final_status, "message": dep["message"]}

        details = self._load_details(map_id)
        generation_source = "llm"
        llm_error = ""

        try:
            # verify prompt를 최종 문자열로 렌더링하고 LLM 응답에서 verification_sql만 추출한다.
            # VERIFY_SQL은 SELECT/WITH 단일문이어야 하고 데이터 변경 SQL은 허용하지 않는다.
            prompt = self._render_sql_prompt(
                template=self._require_prompt("verify_sql_prompt", "VERIFY SQL Prompt"),
                job=job,
                details=details,
                command=command,
            )
            verify_sql = self._sanitize_verify_sql(
                self._extract_sql(self._call_llm(prompt), expected="select", key="verification_sql")
            )
        except Exception as exc:
            llm_error = str(exc)
            return {"ok": False, "map_id": map_id, "error": llm_error, "generation_source": generation_source}

        if save:
            # run_migration_job 내부 호출에서만 NEXT_MIG_INFO.VERIFY_SQL에 저장한다.
            self._save_verify_sql(map_id, verify_sql)
            self._write_log(
                map_id,
                "GENERATE_SQL",
                "INFO",
                "GENERATE_VERIFY_SQL",
                "PASS",
                f"VERIFY_SQL generated by {generation_source}",
                generate_sql=verify_sql,
            )

        return {
            "ok": True,
            "map_id": map_id,
            "status": "VERIFY_SQL_GENERATED",
            "generation_source": generation_source,
            "llm_error": llm_error,
            "verify_sql": verify_sql,
        }

    # action="preview_mig_prompt" / "preview_verify_prompt": 치환 완료 prompt만 확인한다.
    def _preview_sql_prompt(self, map_id: Any, command: dict[str, Any], prompt_kind: str) -> dict[str, Any]:
        # Debug helper: return the fully rendered prompt without calling the LLM
        # and without updating DB.
        map_id = self._require_map_id(map_id)
        job = self._load_job(map_id)
        if not job:
            return {"ok": False, "map_id": map_id, "error": "job not found"}
        details = self._load_details(map_id)
        if prompt_kind == "mig":
            prompt = self._render_sql_prompt(
                template=self._require_prompt("mig_sql_prompt", "MIG SQL Prompt"),
                job=job,
                details=details,
                command=command,
            )
            action = "preview_mig_prompt"
        elif prompt_kind == "verify":
            prompt = self._render_sql_prompt(
                template=self._require_prompt("verify_sql_prompt", "VERIFY SQL Prompt"),
                job=job,
                details=details,
                command=command,
            )
            action = "preview_verify_prompt"
        else:
            return {"ok": False, "map_id": map_id, "error": f"Unsupported prompt_kind: {prompt_kind}"}

        source_context = self._build_source_context(job)
        return {
            "ok": True,
            "action": action,
            "map_id": map_id,
            "prompt_kind": prompt_kind,
            "source_kind": source_context["source_kind"],
            "prompt_length": len(prompt),
            "prompt": prompt,
            "db_updated": False,
            "llm_called": False,
        }

    def _render_sql_prompt(
        self,
        template: str,
        job: dict[str, Any],
        details: list[dict[str, Any]],
        command: dict[str, Any],
    ) -> str:
        # Prompt text is supplied from Langflow input. This method injects DB
        # metadata, mapping details, dependency context, and retry context.
        source_context = self._build_source_context(job)
        # Langflow prompt input에 있는 {placeholder}들을 아래 값으로 치환한다.
        to_table = self._qualify_table(job.get("to_table", ""), self.target_schema)
        from_table = source_context["from_table"]
        mapping_info = self._format_mapping_info(details)
        ddl_info_block = self._build_ddl_info_block(from_table, to_table)
        last_error = str(command.get("last_error") or "").strip()
        last_sql = str(command.get("last_sql") or "").strip()
        retry_context = self._build_retry_context(last_error, last_sql, command.get("retry_count"))
        rendered = str(template or "")
        prompt_values = {
            "ddl_info_block": ddl_info_block,
            "from_table": from_table,
            "to_table": to_table,
            "mapping_info": mapping_info,
            "condition": str(job.get("condition") or "").strip(),
            "source_kind": source_context["source_kind"],
            "source_query": source_context["source_query"],
            "source_from_clause": source_context["source_from_clause"],
            "complex_source_note": source_context["complex_source_note"],
            "retry_context": retry_context,
            "last_error": last_error,
            "last_sql": last_sql,
        }
        for key, value in prompt_values.items():
            rendered = rendered.replace("{" + key + "}", str(value))
        return rendered

    def _build_retry_context(self, last_error: str, last_sql: str, retry_count: Any = None) -> str:
        # Retry context is deliberately plain text so any prompt template can
        # include it without needing JSON parsing.
        if not last_error and not last_sql:
            return ""
        retry_label = ""
        if retry_count is not None:
            retry_label = f"Retry count: {retry_count}\n"
        return (
            "[Retry context]\n"
            f"{retry_label}"
            f"Previous error:\n{last_error or '(none)'}\n\n"
            f"Previous SQL:\n{last_sql or '(none)'}\n\n"
            "Regenerate SQL by fixing the previous error. Do not repeat the same failing SQL.\n"
            "If the previous SQL contains duplicate WHERE clauses such as WHERE WHERE, remove the duplicate keyword.\n"
            "When applying the source filter condition, add WHERE only if the condition text does not already start with WHERE."
        )

    def _require_prompt(self, attr_name: str, display_name: str) -> str:
        value = str(getattr(self, attr_name, "") or "").strip()
        if not value:
            raise ValueError(f"{display_name} input is required for SQL generation")
        return value

    def _format_mapping_info(self, details: list[dict[str, Any]]) -> str:
        lines = []
        for detail in details:
            fr_col = str(detail.get("fr_col") or "").strip()
            to_col = str(detail.get("to_col") or "").strip()
            if to_col:
                lines.append(f"  - {fr_col} -> {to_col}")
            else:
                lines.append(f"  - {fr_col} -> <skip target column; source expression may be used only as part of another mapped expression>")
        return "\n".join(lines) if lines else "  - No mapping details"

    def _build_ddl_info_block(self, from_table: str, to_table: str) -> str:
        blocks = ["[DDL information]"]
        for label, table_name in [("Source", from_table), ("Target", to_table)]:
            try:
                columns = self._table_columns_for_prompt(table_name)
            except Exception as exc:
                columns = f"Unable to load columns: {exc}"
            blocks.append(f"- {label} {table_name}:\n{columns}")
        return "\n".join(blocks)

    def _build_source_context(self, job: dict[str, Any]) -> dict[str, str]:
        # COMPLEX mappings store a full source SELECT/WITH in FR_TABLE. Wrap it as
        # an inline view so generated SQL can refer to mapped columns via SRC.
        map_type = str(job.get("map_type") or "").strip().upper()
        raw_source = str(job.get("fr_table") or "").strip()
        qualified_source = self._qualify_source_expression(raw_source)
        if map_type == "COMPLEX":
            source_query = str(qualified_source or "").strip()
            while source_query.endswith(";"):
                source_query = source_query[:-1].rstrip()
            source_from_clause = f"(\n{source_query}\n) SRC"
            return {
                "source_kind": "COMPLEX_QUERY",
                "source_query": source_query,
                "source_from_clause": source_from_clause,
                "from_table": source_from_clause,
                "complex_source_note": (
                    "MAP_TYPE=COMPLEX. FR_TABLE is a complete source SELECT/WITH query, not a physical table. "
                    "Use it as an inline view exactly once in the FROM clause, and reference mapped FR_COL values from alias SRC. "
                    "Do not rebuild the source query or search for physical source columns outside this query."
                ),
            }
        return {
            "source_kind": "TABLE_OR_JOIN",
            "source_query": qualified_source,
            "source_from_clause": qualified_source,
            "from_table": qualified_source,
            "complex_source_note": "",
        }

    def _table_columns_for_prompt(self, table_name: str) -> str:
        clean = str(table_name or "").strip()
        if not clean or any(token in clean.upper() for token in [" JOIN ", " SELECT ", " WITH "]):
            return "Complex source expression. Use mapping rules as the source of truth."
        schema = None
        table = clean
        if "." in clean:
            schema, table = clean.split(".", 1)
        meta = self._get_table_ddl(table, schema)
        columns = meta.get("columns", [])
        if not columns:
            return "No columns found."
        return "\n".join(
            f"  - {col.get('column_name')} {col.get('data_type')}"
            + (f"({col.get('data_precision')},{col.get('data_scale')})" if col.get("data_precision") else f"({col.get('data_length')})")
            + f" nullable={col.get('nullable')}"
            for col in columns[:200]
        )

    def _call_llm(self, prompt: str) -> str:
        # SQL 생성도 OpenAI-compatible chat/completions 한 경로만 사용한다.
        api_key = str(self.llm_api_key or "").strip()
        model = str(self.llm_model or "").strip()
        max_tokens = int(self.llm_max_tokens or 4096)
        if not api_key:
            raise ValueError("LLM API key is empty")
        if not model:
            raise ValueError("LLM model is empty")
        base_url = str(self.llm_base_url or "https://api.openai.com/v1").strip().rstrip("/")
        url = base_url if base_url.endswith("/chat/completions") else f"{base_url}/chat/completions"
        payload = {
            "model": model,
            "messages": [
                {"role": "user", "content": prompt},
            ],
            "max_tokens": max_tokens,
            "temperature": 0,
        }
        data = self._post_json(url, payload, {"Authorization": f"Bearer {api_key}"})
        return str(data["choices"][0]["message"].get("content", ""))

    def _extract_sql(self, value: Any, expected: str, key: str | None = None) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("LLM returned empty SQL")
        fence = re.search(r"```(?:sql)?\s*(.*?)```", text, flags=re.I | re.S)
        if fence:
            text = fence.group(1).strip()
        if key:
            parsed = self._parse_llm_json(text)
            text = str(parsed.get(key) or "").strip()
        text = text.rstrip(";").strip()
        first_word = text.split(None, 1)[0].upper() if text.split(None, 1) else ""
        allowed = {"insert": {"INSERT"}, "select": {"SELECT", "WITH"}}
        if first_word not in allowed.get(expected, set()):
            raise ValueError(f"Expected {expected.upper()} SQL but got: {first_word or text[:40]}")
        return text

    def _parse_llm_json(self, text: str) -> dict[str, Any]:
        clean = str(text or "").strip()
        fence = re.search(r"```(?:json)?\s*(.*?)```", clean, flags=re.I | re.S)
        if fence:
            clean = fence.group(1).strip()
        try:
            parsed = json.loads(clean)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", clean, flags=re.S)
            if not match:
                raise
            parsed = json.loads(match.group(0))
        if not isinstance(parsed, dict):
            raise ValueError("LLM JSON response must be an object")
        return parsed

    def _sanitize_migration_sql(self, sql: str) -> str:
        # MIG_SQL stored/executed by this tool must be exactly one INSERT. This
        # keeps destructive SQL out of generated migration content.
        cleaned = str(sql or "").strip().rstrip(";").strip()
        if not cleaned:
            raise ValueError("MIG_SQL is empty")
        upper = cleaned.upper()
        forbidden = ["TRUNCATE", "COMMIT", "ROLLBACK", "DELETE", "UPDATE", "MERGE", "DROP", "ALTER"]
        for token in forbidden:
            if re.search(rf"\b{token}\b", upper):
                raise ValueError(f"MIG_SQL must not contain {token}")
        statements = self._split_sql_script(cleaned)
        if len(statements) != 1:
            raise ValueError("MIG_SQL must contain exactly one INSERT statement")
        statement = statements[0].strip().rstrip(";").strip()
        if not statement.upper().startswith("INSERT"):
            raise ValueError("MIG_SQL must start with INSERT")
        return statement

    def _sanitize_verify_sql(self, sql: str) -> str:
        # VERIFY_SQL must be read-only. Verification passes only when every
        # returned value is interpreted as zero.
        cleaned = str(sql or "").strip().rstrip(";").strip()
        if not cleaned:
            raise ValueError("VERIFY_SQL is empty")
        upper = cleaned.upper()
        forbidden = ["TRUNCATE", "COMMIT", "ROLLBACK", "INSERT", "DELETE", "UPDATE", "MERGE", "DROP", "ALTER"]
        for token in forbidden:
            if re.search(rf"\b{token}\b", upper):
                raise ValueError(f"VERIFY_SQL must not contain {token}")
        statements = self._split_sql_script(cleaned)
        if len(statements) != 1:
            raise ValueError("VERIFY_SQL must contain exactly one SELECT statement")
        statement = statements[0].strip().rstrip(";").strip()
        first_word = statement.split(None, 1)[0].upper() if statement.split(None, 1) else ""
        if first_word not in {"SELECT", "WITH"}:
            raise ValueError("VERIFY_SQL must start with SELECT or WITH")
        return statement

    # action="run_migration_job": SQL 생성, 저장, 실행, 검증, 최종 STATUS 업데이트까지 전체 수행한다.
    def _run_migration_job(self, map_id: Any, command: dict[str, Any]) -> dict[str, Any]:
        # Full controlled lifecycle:
        # generate MIG_SQL -> execute MIG_SQL -> generate VERIFY_SQL -> verify.
        # Intermediate retry failures are logged, but NEXT_MIG_INFO.STATUS is
        # updated only once the final success/failure is known.
        map_id = self._require_map_id(map_id)
        started = time.perf_counter()
        max_attempts = max(1, int(command.get("max_attempts") or self.default_max_attempts or 1))

        job = self._load_job(map_id)
        if not job:
            return {"ok": False, "map_id": map_id, "error": "job not found"}

        if str(job.get("use_yn") or "").upper() != "Y":
            return {"ok": False, "map_id": map_id, "status": "SKIP", "error": "USE_YN is not Y"}

        # 실행 가능한 상태는 STATUS NULL뿐이다.
        # FAIL/PASS 등 이미 상태가 있으면 사용자가 reset을 먼저 해야 한다.
        current_status = str(job.get("status") or "").strip().upper()
        # A non-null status requires an explicit reset first. This prevents
        # accidental duplicate inserts from casual "run again" chat requests.
        if current_status == "PASS":
            return {"ok": True, "map_id": map_id, "status": "PASS", "message": "Job already passed"}
        if current_status:
            return {
                "ok": False,
                "map_id": map_id,
                "status": current_status,
                "error": "Full migration is allowed only when STATUS is NULL.",
            }

        dep = self._check_dependencies(job)
        # 실행 전 의존성 체크:
        # 1. PRIOR_MAP_ID가 PASS인지
        # 2. 같은 TO_TABLE에서 PRIORITY가 더 낮은 작업들이 PASS인지
        if dep["status"] != "READY":
            final_status = "SKIP" if dep["status"] in {"FAIL", "SKIP"} else "WAITING"
            if final_status == "SKIP":
                self._update_job_status(map_id, "SKIP", 0, int(job.get("retry_count") or 0))
            self._write_log(map_id, "DEPENDENCY", "WARN", "DEP_CHECK", final_status, dep["message"])
            return {"ok": final_status == "WAITING", "map_id": map_id, "status": final_status, "message": dep["message"]}

        steps: list[dict[str, Any]] = []

        try:
            job = self._load_job(map_id) or job
            user_edited = str(job.get("user_edited") or "").upper() == "Y"

            # 전체 실행을 시작한 횟수만 증가시킨다.
            # retry attempt마다 증가시키는 값이 아니다.
            self._increment_batch_count(map_id)
            generation_command = {
                "force_regenerate": command.get("force_regenerate", False),
                "_internal_run": True,
            }
            last_failure: dict[str, Any] = {}
            mig_executed = False
            last_retry_count = 0

            for attempt in range(1, max_attempts + 1):
                # retry_count is zero-based to match the existing UI/DB examples:
                # first attempt=0, second attempt=1, etc.
                retry_count = attempt - 1
                last_retry_count = retry_count
                job = self._load_job(map_id) or job
                user_edited = str(job.get("user_edited") or "").upper() == "Y"

                if not mig_executed:
                    # On FAIL-INSERT, retry regenerates and re-executes MIG_SQL.
                    # After MIG_SQL succeeds, later retries only regenerate/verify
                    # VERIFY_SQL and never execute the insert again.
                    if user_edited:
                        # 사용자가 저장한 MIG_SQL이 있으면 LLM 생성 단계를 건너뛴다.
                        mig_sql = str(job.get("mig_sql") or "").strip()
                        if not mig_sql:
                            raise ValueError("USER_EDITED=Y but MIG_SQL is empty")
                        steps.append({"step": "generate_mig_sql", "attempt": attempt, "status": "SKIPPED_USER_EDITED"})
                    else:
                        # LLM으로 MIG_SQL 생성.
                        # 직전 실패가 있으면 last_error/last_sql이 prompt에 포함된다.
                        mig_command = {
                            **generation_command,
                            "retry_count": retry_count,
                            "last_error": last_failure.get("error", ""),
                            "last_sql": str(job.get("mig_sql") or ""),
                        }
                        mig_result = self._generate_mig_sql(map_id, mig_command)
                        steps.append({"step": "generate_mig_sql", "attempt": attempt, **self._summary_result(mig_result)})
                        if not mig_result.get("ok"):
                            last_failure = {"status": "FAIL-INSERT", "error": mig_result.get("error") or "MIG_SQL generation failed"}
                            self._write_retry_log(map_id, "GENERATE_MIG_SQL", "FAIL-INSERT", str(last_failure["error"]), retry_count)
                            if attempt < max_attempts:
                                continue
                            break

                    job = self._load_job(map_id) or job
                    try:
                        # MIG_SQL 실제 실행.
                        # 여기서 성공해도 STATUS를 SUCCESS-MIG로 저장하지 않는다.
                        # 최종 PASS/FAIL은 verify 이후에만 NEXT_MIG_INFO.STATUS에 저장한다.
                        mig_exec_result = self._execute_mig_sql_once(job, retry_count)
                        steps.append({"step": "execute_mig_sql", "attempt": attempt, **self._summary_result(mig_exec_result)})
                        mig_executed = True
                    except Exception as exc:
                        # INSERT 실패 시 최종 실패로 바로 저장하지 않고 retry 가능하면 다음 attempt로 간다.
                        # 다음 attempt에서는 MIG_SQL을 다시 생성하고 다시 실행한다.
                        last_failure = {"status": "FAIL-INSERT", "error": str(exc)}
                        steps.append({"step": "execute_mig_sql", "attempt": attempt, "ok": False, **last_failure})
                        self._write_retry_log(map_id, "SQL_EXEC", "FAIL-INSERT", str(exc), retry_count, str(job.get("mig_sql") or ""))
                        if attempt < max_attempts:
                            continue
                        break

                job = self._load_job(map_id) or job
                user_edited = str(job.get("user_edited") or "").upper() == "Y"
                verify_sql = str(job.get("verify_sql") or "").strip()
                if user_edited and verify_sql:
                    # User-supplied VERIFY_SQL is trusted when present.
                    steps.append({"step": "generate_verify_sql", "attempt": attempt, "status": "SKIPPED_USER_EDITED"})
                else:
                    # LLM으로 VERIFY_SQL 생성.
                    # MIG_SQL 실행은 이미 성공했으므로 이후 retry에서도 INSERT는 다시 하지 않는다.
                    verify_command = {
                        **generation_command,
                        "retry_count": retry_count,
                        "last_error": last_failure.get("error", ""),
                        "last_sql": str(job.get("verify_sql") or ""),
                    }
                    verify_result = self._generate_verify_sql(map_id, verify_command)
                    steps.append({"step": "generate_verify_sql", "attempt": attempt, **self._summary_result(verify_result)})
                    if not verify_result.get("ok"):
                        last_failure = {"status": "FAIL-TEST", "error": verify_result.get("error") or "VERIFY_SQL generation failed"}
                        self._write_retry_log(map_id, "GENERATE_VERIFY_SQL", "FAIL-TEST", str(last_failure["error"]), retry_count)
                        if attempt < max_attempts:
                            continue
                        break

                job = self._load_job(map_id) or job
                try:
                    # VERIFY_SQL 실행 결과의 모든 값이 0이면 PASS로 본다.
                    verify_exec_result = self._execute_verify_sql_once(job)
                    steps.append({"step": "execute_verify_sql", "attempt": attempt, **self._summary_result(verify_exec_result)})
                    if verify_exec_result.get("ok"):
                        # 최종 성공 시점에만 NEXT_MIG_INFO.STATUS='PASS'를 저장한다.
                        elapsed = int(time.perf_counter() - started)
                        self._update_job_status(map_id, "PASS", elapsed, retry_count)
                        self._write_log(map_id, "VERIFY_SQL", "INFO", "VERIFY", "PASS", "Migration Success", retry_count, verify_exec_result.get("verify_sql"))
                        return {
                            "ok": True,
                            "map_id": map_id,
                            "status": "PASS",
                            "message": "Migration completed",
                            "elapsed_seconds": elapsed,
                            "retry_count": retry_count,
                            "steps": steps,
                        }

                    last_failure = {"status": "FAIL-TEST", "error": verify_exec_result.get("message") or "Verification failed"}
                    # 검증 실패는 FAIL-TEST로 기록하고 retry 가능하면 VERIFY_SQL만 다시 생성/검증한다.
                    self._write_retry_log(map_id, "VERIFY", "FAIL-TEST", str(last_failure["error"]), retry_count, verify_exec_result.get("verify_sql"))
                    if attempt < max_attempts:
                        continue
                    break
                except Exception as exc:
                    last_failure = {"status": "FAIL-TEST", "error": str(exc)}
                    steps.append({"step": "execute_verify_sql", "attempt": attempt, "ok": False, **last_failure})
                    self._write_retry_log(map_id, "VERIFY", "FAIL-TEST", str(exc), retry_count, str(job.get("verify_sql") or ""))
                    if attempt < max_attempts:
                        continue
                    break

            final_status = str(last_failure.get("status") or "FAIL")
            elapsed = int(time.perf_counter() - started)
            # 모든 retry를 소진한 뒤 최종 실패 상태를 한 번만 저장한다.
            self._update_job_status(map_id, final_status, elapsed, last_retry_count)
            self._write_log(
                map_id,
                "JOB_FAIL",
                "ERROR",
                "FINAL",
                final_status,
                str(last_failure.get("error") or "Max attempts reached")[:3900],
                last_retry_count,
                str((self._load_job(map_id) or {}).get("verify_sql" if final_status == "FAIL-TEST" else "mig_sql") or ""),
            )
            return {
                "ok": False,
                "map_id": map_id,
                "status": final_status,
                "error": last_failure.get("error") or "Max attempts reached",
                "elapsed_seconds": elapsed,
                "retry_count": last_retry_count,
                "steps": steps,
            }
        except Exception as exc:
            elapsed = int(time.perf_counter() - started)
            # 예상 밖의 예외는 일반 FAIL로 저장한다.
            self._update_job_status(map_id, "FAIL", elapsed, int(job.get("retry_count") or 0))
            self._write_log(map_id, "ROW_ERROR", "ERROR", "RUN_FULL", "FAIL", str(exc)[:3900])
            return {
                "ok": False,
                "map_id": map_id,
                "status": "FAIL",
                "error": str(exc),
                "elapsed_seconds": elapsed,
                "steps": steps,
            }

    def _load_job(self, map_id: int) -> dict[str, Any] | None:
        # Master row snapshot. Long-running actions reload this between steps so
        # generated SQL saved in DB is read back before execution.
        # NEXT_MIG_INFO 단건 조회 결과를 Python dict로 변환한다.
        map_table = self._system_table("NEXT_MIG_INFO")
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                f"""
                SELECT MAP_ID, MAP_TYPE, FR_TABLE, TO_TABLE, USE_YN, TRUNC_YN,
                       PRIORITY, STATUS, USER_EDITED, PRIOR_MAP_ID, CONDITION,
                       MIG_SQL, VERIFY_SQL, BATCH_CNT, ELAPSED_SECONDS, RETRY_COUNT,
                       CREATED_AT, UPD_TS
                FROM {map_table}
                WHERE MAP_ID = :1
                """,
                [map_id],
            )
            row = cur.fetchone()
        if not row:
            return None
        return {
            "map_id": row[0],
            "map_type": self._to_text(row[1]),
            "fr_table": self._to_text(row[2]),
            "to_table": self._to_text(row[3]),
            "use_yn": self._to_text(row[4]),
            "trunc_yn": self._to_text(row[5]),
            "priority": row[6],
            "status": self._to_text(row[7]),
            "user_edited": self._to_text(row[8]),
            "prior_map_id": row[9],
            "condition": self._to_text(row[10]),
            "mig_sql": self._to_text(row[11]),
            "verify_sql": self._to_text(row[12]),
            "batch_cnt": row[13],
            "elapsed_seconds": row[14],
            "retry_count": row[15],
            "created_at": self._to_text(row[16]),
            "upd_ts": self._to_text(row[17]),
        }

    def _load_details(self, map_id: int) -> list[dict[str, Any]]:
        # Column mapping order matters because it becomes the INSERT target column
        # order and SELECT expression order in generated SQL.
        # NEXT_MIG_INFO_DTL의 FR_COL -> TO_COL 목록을 MAP_DTL 순서로 가져온다.
        detail_table = self._system_table("NEXT_MIG_INFO_DTL")
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                f"""
                SELECT MAP_DTL, MAP_ID, FR_COL, TO_COL
                FROM {detail_table}
                WHERE MAP_ID = :1
                ORDER BY MAP_DTL ASC
                """,
                [map_id],
            )
            rows = cur.fetchall()
        return [
            {"map_dtl": r[0], "map_id": r[1], "fr_col": self._to_text(r[2]), "to_col": self._to_text(r[3])}
            for r in rows
        ]

    def _check_dependencies(self, job: dict[str, Any]) -> dict[str, str]:
        # Two dependency layers are enforced: explicit PRIOR_MAP_ID and all
        # lower-priority jobs for the same TO_TABLE.
        # 반환 status가 READY가 아니면 SQL 생성/실행 단계로 넘어가지 않는다.
        prior_map_id = job.get("prior_map_id")
        try:
            prior_map_id_int = int(prior_map_id) if prior_map_id is not None and str(prior_map_id).strip() else 0
        except (TypeError, ValueError):
            return {"status": "PENDING", "message": f"Invalid PRIOR_MAP_ID={prior_map_id}"}

        if prior_map_id_int > 0:
            prior = self._load_job(prior_map_id_int)
            if not prior:
                return {"status": "PENDING", "message": f"Prior MAP_ID={prior_map_id} not found"}
            prior_status = str(prior.get("status") or "").upper()
            if prior_status != "PASS":
                return {"status": prior_status or "PENDING", "message": f"Prior MAP_ID={prior_map_id} status={prior_status or 'NULL'}"}

        target_dep = self._check_same_target_priority_dependencies(job)
        if target_dep["status"] != "READY":
            return target_dep

        return {"status": "READY", "message": "Dependencies passed"}

    def _check_same_target_priority_dependencies(self, job: dict[str, Any]) -> dict[str, str]:
        # 같은 TO_TABLE 안에서는 PRIORITY 숫자가 더 작은 작업이 먼저 PASS여야 한다.
        to_table = str(job.get("to_table") or "").strip()
        priority = job.get("priority")
        map_id = int(job.get("map_id") or 0)
        if not to_table or priority is None:
            return {"status": "READY", "message": "No same-target priority dependency"}

        map_table = self._system_table("NEXT_MIG_INFO")
        with self._connect() as conn:
            cur = conn.cursor()
            try:
                cur.execute(
                    f"""
                    SELECT MAP_ID, STATUS
                    FROM {map_table}
                    WHERE DBMS_LOB.SUBSTR(TO_TABLE, 200, 1) = :1
                      AND PRIORITY < :2
                      AND MAP_ID != :3
                    ORDER BY PRIORITY DESC, MAP_ID DESC
                    """,
                    [to_table, priority, map_id],
                )
            except Exception:
                cur.execute(
                    f"""
                    SELECT MAP_ID, STATUS
                    FROM {map_table}
                    WHERE TO_TABLE = :1
                      AND PRIORITY < :2
                      AND MAP_ID != :3
                    ORDER BY PRIORITY DESC, MAP_ID DESC
                    """,
                    [to_table, priority, map_id],
                )
            rows = cur.fetchall()

        for prior_map_id, status in rows:
            prior_status = str(self._to_text(status) or "").strip().upper()
            if prior_status != "PASS":
                return {
                    "status": prior_status or "PENDING",
                    "message": f"Same target prior MAP_ID={prior_map_id} status={prior_status or 'NULL'}",
                }
        return {"status": "READY", "message": "Same-target priority dependencies passed"}

    def _save_mig_sql(self, map_id: int, mig_sql: str) -> None:
        # Internal save used by run_migration_job. Public generate_mig_sql returns
        # preview only and does not call this path.
        map_table = self._system_table("NEXT_MIG_INFO")
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                f"""
                UPDATE {map_table}
                SET MIG_SQL = :1,
                    UPD_TS = CURRENT_TIMESTAMP
                WHERE MAP_ID = :2
                """,
                [mig_sql, map_id],
            )
            conn.commit()

    def _save_verify_sql(self, map_id: int, verify_sql: str) -> None:
        # Internal save used by run_migration_job. Public generate_verify_sql is
        # preview-only unless this helper is reached through _internal_run.
        map_table = self._system_table("NEXT_MIG_INFO")
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                f"""
                UPDATE {map_table}
                SET VERIFY_SQL = :1,
                    UPD_TS = CURRENT_TIMESTAMP
                WHERE MAP_ID = :2
                """,
                [verify_sql, map_id],
            )
            conn.commit()

    def _execute_mig_sql_once(self, job: dict[str, Any], retry_count: int) -> dict[str, Any]:
        # This helper intentionally does not update NEXT_MIG_INFO.STATUS. The full
        # run writes final status only after verification or final failure.
        # TRUNC_YN=Y이면 target truncate를 먼저 수행한 뒤 MIG_SQL을 실행한다.
        map_id = int(job["map_id"])
        mig_sql = self._sanitize_migration_sql(str(job.get("mig_sql") or ""))
        if str(job.get("trunc_yn") or "").upper() == "Y":
            self._truncate_target(job)
            self._write_log(map_id, "EXECUTE_SQL", "INFO", "TRUNCATE", "PASS", "Target table truncated", retry_count)
        affected_rows = self._execute_sql_script(mig_sql)
        return {
            "ok": True,
            "map_id": map_id,
            "status": "SUCCESS-MIG",
            "message": "Migration SQL executed",
            "affected_rows": affected_rows,
            "mig_sql": mig_sql,
        }

    def _execute_verify_sql_once(self, job: dict[str, Any]) -> dict[str, Any]:
        # Verification SQL is expected to return numeric difference columns. Any
        # non-zero value marks the verification as FAIL-TEST.
        map_id = int(job["map_id"])
        verify_sql = self._sanitize_verify_sql(str(job.get("verify_sql") or ""))
        verify_ok, verify_message, rows = self._execute_verify_sql_with_rows(verify_sql)
        return {
            "ok": verify_ok,
            "map_id": map_id,
            "status": "PASS" if verify_ok else "FAIL-TEST",
            "message": verify_message,
            "verify_sql": verify_sql,
            "result_rows": rows,
        }

    def _summary_result(self, result: dict[str, Any]) -> dict[str, Any]:
        summary = {
            "ok": bool(result.get("ok")),
            "status": result.get("status"),
        }
        for key in ["message", "error", "generation_source", "affected_rows", "elapsed_seconds", "retry_count"]:
            if key in result:
                summary[key] = result.get(key)
        return summary

    def _write_retry_log(
        self,
        map_id: int,
        step_name: str,
        status: str,
        message: str,
        retry_count: int,
        generate_sql: str | None = None,
    ) -> None:
        self._write_log(
            map_id,
            "ROW_ERROR",
            "WARN",
            "RETRY" if retry_count > 0 else step_name,
            status,
            str(message or "")[:3900],
            retry_count,
            generate_sql,
        )

    def _truncate_target(self, job: dict[str, Any]) -> None:
        target = self._qualify_table(job["to_table"], self.target_schema)
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(f"TRUNCATE TABLE {target}")
            conn.commit()

    def _execute_sql_script(self, sql_script: str) -> int:
        # Sanitizers usually guarantee one statement, but this executor also
        # supports split scripts for internal helpers such as truncate flows.
        statements = self._split_sql_script(sql_script)
        total_rowcount = 0
        with self._connect() as conn:
            cur = conn.cursor()
            for stmt in statements:
                cleaned = stmt.strip().rstrip(";")
                if cleaned:
                    cur.execute(cleaned)
                    if cur.rowcount and cur.rowcount > 0:
                        total_rowcount += cur.rowcount
            conn.commit()
        return total_rowcount

    def _execute_verify_sql_with_rows(self, verify_sql: str) -> tuple[bool, str, list[dict[str, Any]]]:
        # Store rows in the result payload so the Agent can explain mismatches
        # without querying the DB again.
        # 검증 SQL은 숫자 차이값을 반환한다고 가정한다.
        # 반환 row의 모든 value가 0이면 성공, 하나라도 0이 아니면 실패다.
        statements = self._split_sql_script(verify_sql)
        if not statements:
            return False, "verify_sql is empty", []
        last_rows = []
        columns = []
        with self._connect() as conn:
            cur = conn.cursor()
            for stmt in statements:
                cleaned = stmt.strip().rstrip(";")
                if not cleaned:
                    continue
                cur.execute(cleaned)
                if cur.description:
                    columns = [desc[0] for desc in cur.description]
                    last_rows = cur.fetchall()
        if not last_rows:
            return False, "Verification SQL returned no rows", []
        result_rows = [
            {str(columns[i] if i < len(columns) else i): self._to_text(value) for i, value in enumerate(row)}
            for row in last_rows
        ]
        for row in last_rows:
            for value in row:
                if not self._is_zero(value):
                    return False, f"Mismatch found: {row}", result_rows
        return True, "All Verification Passed", result_rows

    def _update_job_status(self, map_id: int, status: str, elapsed_seconds: int, retry_count: int) -> None:
        # Final state writer for run_migration_job and dependency SKIP cases.
        map_table = self._system_table("NEXT_MIG_INFO")
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                f"""
                UPDATE {map_table}
                SET STATUS = :1,
                    ELAPSED_SECONDS = :2,
                    RETRY_COUNT = :3,
                    UPD_TS = CURRENT_TIMESTAMP
                WHERE MAP_ID = :4
                """,
                [status, elapsed_seconds, retry_count, map_id],
            )
            conn.commit()

    def _increment_batch_count(self, map_id: int) -> None:
        map_table = self._system_table("NEXT_MIG_INFO")
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                f"""
                UPDATE {map_table}
                SET BATCH_CNT = NVL(BATCH_CNT, 0) + 1,
                    UPD_TS = CURRENT_TIMESTAMP
                WHERE MAP_ID = :1
                """,
                [map_id],
            )
            conn.commit()

    def _write_log(
        self,
        map_id: int,
        log_type: str,
        log_level: str,
        step_name: str,
        status: str,
        message: str,
        retry_count: int = 0,
        generate_sql: str | None = None,
    ) -> None:
        # Log writes are best-effort. A logging table issue should not hide the
        # actual migration result from the caller.
        # NEXT_MIG_LOG insert 컬럼 순서는 운영 테이블 기준에 맞춰 유지한다.
        log_table = self._system_table("NEXT_MIG_LOG")
        seq = self._system_table("MIGRATION_LOG_SEQ")
        safe_message = str(message or "")[:4000]
        try:
            with self._connect() as conn:
                cur = conn.cursor()
                cur.execute(
                    f"""
                    INSERT INTO {log_table}
                        (CREATED_AT, STATUS, MESSAGE, LOG_ID, MAP_ID, LOG_TYPE,
                         LOG_LEVEL, STEP_NAME, RETRY_COUNT, MIG_KIND, GENERATE_SQL)
                    VALUES
                        (CURRENT_TIMESTAMP, :1, :2, {seq}.NEXTVAL, :3, :4,
                         :5, :6, :7, 'DB_MIG', :8)
                    """,
                    [status, safe_message, map_id, log_type, log_level, step_name, retry_count, generate_sql],
                )
                conn.commit()
        except Exception:
            # Logging must not break the operational command.
            pass

    def _split_sql_script(self, sql_script: str) -> list[str]:
        text = str(sql_script or "")
        statements: list[str] = []
        buffer: list[str] = []
        in_single = False
        in_double = False
        for ch in text:
            if ch == "'" and not in_double:
                in_single = not in_single
            elif ch == '"' and not in_single:
                in_double = not in_double
            if ch == ";" and not in_single and not in_double:
                statement = "".join(buffer).strip()
                if statement:
                    statements.append(statement)
                buffer = []
            else:
                buffer.append(ch)
        tail = "".join(buffer).strip()
        if tail:
            statements.append(tail)
        return statements

    def _is_zero(self, value: Any) -> bool:
        value = self._to_text(value).strip()
        if value == "":
            return False
        try:
            return Decimal(value) == Decimal("0")
        except (InvalidOperation, ValueError):
            return value == "0"

    def _as_bool(self, value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        return str(value).strip().lower() in {"1", "true", "y", "yes", "on"}

    def _require_map_id(self, map_id: Any) -> int:
        if map_id is None or str(map_id).strip() == "":
            raise ValueError("map_id is required")
        return int(map_id)

    def _system_table(self, table_name: str) -> str:
        return self._qualify_table(table_name, self.system_schema)

    def _qualify_table(self, table_name: str, schema: str | None) -> str:
        clean = str(table_name or "").strip()
        clean_schema = str(schema or "").strip().upper()
        if not clean:
            raise ValueError("table_name is empty")
        if "." in clean or not clean_schema:
            return clean
        self._validate_identifier(clean_schema, "schema")
        return f"{clean_schema}.{clean}"

    def _qualify_source_expression(self, fr_table: str) -> str:
        expr = str(fr_table or "").strip()
        schema = str(self.source_schema or "").strip().upper()
        if not schema:
            return expr
        self._validate_identifier(schema, "source_schema")
        table_names = sorted(set(self._extract_table_names(expr)), key=len, reverse=True)
        for table in table_names:
            if "." in table:
                continue
            expr = re.sub(rf"(?<![.\w]){re.escape(table)}(?![.\w])", f"{schema}.{table}", expr)
        return expr

    def _extract_table_names(self, fr_table: str) -> list[str]:
        parts = re.split(r"\b(?:(?:LEFT|RIGHT|FULL|INNER|CROSS)\s+(?:OUTER\s+)?)?JOIN\b", fr_table, flags=re.I)
        tables: list[str] = []
        for part in parts:
            part = re.split(r"\bON\b", part, flags=re.I)[0].strip()
            tokens = part.split()
            if tokens and tokens[0].upper() not in {"SELECT", "WITH", "FROM", "("}:
                tables.append(tokens[0])
        return tables

    def _validate_identifier(self, value: str, label: str) -> None:
        if not re.fullmatch(r"[A-Z][A-Z0-9_$#]*", value):
            raise ValueError(f"Invalid {label}: {value}")

    def _to_text(self, value: Any) -> str:
        if value is None:
            return ""
        if hasattr(value, "read"):
            value = value.read()
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="ignore")
        return str(value)




