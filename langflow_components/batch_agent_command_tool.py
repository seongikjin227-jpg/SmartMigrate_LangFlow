from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import BoolInput, IntInput, MessageTextInput, SecretStrInput, StrInput, Output
from lfx.schema.data import Data

try:
    from migration_command_tool import MigrationCommandTool
    from sql_conversion_command_tool import SqlConversionCommandTool
except ImportError:
    from langflow_components.migration_command_tool import MigrationCommandTool
    from langflow_components.sql_conversion_command_tool import SqlConversionCommandTool


class BatchAgentCommandTool(Component):
    display_name = "Batch Agent Command Tool"
    description = "Starts and controls a background SmartMigration batch loop inside the Langflow container."
    name = "BatchAgentCommandTool"
    icon = "Timer"

    _thread: threading.Thread | None = None
    _stop_event = threading.Event()
    _state_lock = threading.Lock()
    _state: dict[str, Any] = {
        "running": False,
        "run_id": None,
        "loop_no": 0,
        "started_at": None,
        "updated_at": None,
        "last_event": None,
        "last_agent": None,
        "last_job_id": None,
        "last_job_status": None,
        "last_error": None,
    }

    inputs = [
        MessageTextInput(
            name="command_json",
            display_name="Command JSON",
            required=True,
            tool_mode=True,
            info='Batch command. Supported actions: {"action":"start"}, {"action":"stop"}, {"action":"status"}',
        ),
        StrInput(name="db_host", display_name="DB Host", required=True),
        IntInput(name="db_port", display_name="DB Port", value=1521, required=True),
        StrInput(name="db_service_name", display_name="Service Name", required=True),
        StrInput(name="db_username", display_name="Username", required=True),
        SecretStrInput(name="db_password", display_name="Password", required=True),
        StrInput(
            name="llm_base_url",
            display_name="LLM Base URL",
            required=False,
            info="OpenAI-compatible LLM gateway base URL.",
        ),
        SecretStrInput(name="llm_api_key", display_name="LLM API Key", required=False),
        StrInput(name="llm_model", display_name="LLM Model", value="claude-haiku-4-5-20251001", required=False),
        IntInput(name="llm_max_tokens", display_name="LLM Max Tokens", value=4096, required=False),
        IntInput(name="llm_timeout_seconds", display_name="LLM Timeout Seconds", value=900, required=False),
        MessageTextInput(name="mig_sql_prompt", display_name="MIG SQL Prompt", required=False),
        MessageTextInput(name="verify_sql_prompt", display_name="VERIFY SQL Prompt", required=False),
        MessageTextInput(name="to_sql_prompt", display_name="TO SQL Prompt", required=True),
        MessageTextInput(name="bind_sql_prompt", display_name="BIND SQL Prompt", required=False),
        MessageTextInput(name="test_sql_prompt", display_name="TEST SQL Prompt", required=False),
        StrInput(
            name="system_schema",
            display_name="System Schema",
            required=False,
            info="Schema containing SmartMigration metadata/log tables. Leave blank for current user.",
        ),
        StrInput(name="source_schema", display_name="Source Schema", required=False),
        StrInput(name="target_schema", display_name="Target Schema", required=False),
        IntInput(name="migration_max_attempts", display_name="Migration Max Attempts", value=3, required=False),
        IntInput(name="sql_conversion_max_attempts", display_name="SQL Conversion Max Attempts", value=1, required=False),
        IntInput(
            name="no_job_sleep_seconds",
            display_name="No Job Sleep Seconds",
            value=600,
            required=False,
            info="Sleep interval after a loop finds no executable job. Default: 600 seconds.",
        ),
        IntInput(
            name="error_sleep_seconds",
            display_name="Error Sleep Seconds",
            value=60,
            required=False,
            info="Sleep interval after an unexpected loop error. Default: 60 seconds.",
        ),
        BoolInput(
            name="auto_install_packages",
            display_name="Auto Install Missing Packages",
            value=False,
            required=False,
            info="If true, installs missing runtime packages with pip before DB connection.",
        ),
        BoolInput(
            name="auto_create_log_table",
            display_name="Auto Create Batch Log Table",
            value=True,
            required=False,
            info="Create AG_BATCH_AGENT_LOG if it does not exist.",
        ),
    ]

    outputs = [
        Output(display_name="Result", name="result", method="run_command"),
    ]

    def run_command(self) -> Data:
        try:
            command = self._parse_command()
            action = str(command.get("action") or "").strip().lower()
            config = self._snapshot_config()

            if action == "start":
                result = self._start(config)
            elif action == "stop":
                result = self._stop(config)
            elif action == "status":
                result = self._status()
            else:
                result = {"ok": False, "error": f"Unsupported action: {action}"}

            self.status = result
            return Data(data=result)
        except Exception as exc:
            result = {"ok": False, "error": str(exc)}
            self.status = result
            return Data(data=result)

    def _start(self, config: dict[str, Any]) -> dict[str, Any]:
        cls = self.__class__
        with cls._state_lock:
            if cls._thread and cls._thread.is_alive() and not cls._stop_event.is_set():
                state = dict(cls._state)
                self._write_batch_log_safe(config, state.get("run_id"), int(state.get("loop_no") or 0), "ALREADY_RUNNING", message="Batch agent is already running.")
                return {"ok": True, "status": "already_running", "running": True}

            if config["auto_create_log_table"]:
                self._ensure_log_table(config)

            run_id = datetime.now().strftime("%Y%m%d%H%M%S")
            cls._stop_event.clear()
            cls._state.update(
                {
                    "running": True,
                    "run_id": run_id,
                    "loop_no": 0,
                    "started_at": datetime.now().isoformat(timespec="seconds"),
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                    "last_event": "START",
                    "last_agent": None,
                    "last_job_id": None,
                    "last_job_status": None,
                    "last_error": None,
                }
            )

            cls._thread = threading.Thread(
                target=cls._worker_loop,
                args=(config, run_id),
                daemon=True,
                name=f"smartmigration-batch-{run_id}",
            )
            cls._thread.start()

        self._write_batch_log_safe(config, run_id, 0, "START", message="Batch agent started.")
        return {"ok": True, "status": "started", "running": True}

    def _stop(self, config: dict[str, Any]) -> dict[str, Any]:
        cls = self.__class__
        state = self._status()
        cls._stop_event.set()
        with cls._state_lock:
            cls._state["running"] = False
            cls._state["updated_at"] = datetime.now().isoformat(timespec="seconds")
            cls._state["last_event"] = "STOP_REQUESTED"
        self._write_batch_log_safe(config, state.get("run_id"), int(state.get("loop_no") or 0), "STOP_REQUESTED", message="Stop requested.")
        return {"ok": True, "status": "stop_requested", "running": False}

    def _status(self) -> dict[str, Any]:
        cls = self.__class__
        alive = bool(cls._thread and cls._thread.is_alive() and not cls._stop_event.is_set())
        with cls._state_lock:
            state = dict(cls._state)
        state["running"] = alive
        return {"ok": True, **state}

    @classmethod
    def _worker_loop(cls, config: dict[str, Any], run_id: str) -> None:
        helper = object.__new__(cls)
        try:
            while not cls._stop_event.is_set():
                with cls._state_lock:
                    cls._state["loop_no"] = int(cls._state.get("loop_no") or 0) + 1
                    loop_no = int(cls._state["loop_no"])
                    cls._state["updated_at"] = datetime.now().isoformat(timespec="seconds")
                    cls._state["last_event"] = "LOOP_START"

                started = time.perf_counter()
                helper._write_batch_log_safe(config, run_id, loop_no, "LOOP_START", message="Batch loop started.")

                try:
                    result = helper._execute_one_job(config)
                    elapsed = round(time.perf_counter() - started, 3)
                    event_type = "JOB_SUCCESS" if result.get("job_executed") else "NO_JOB"
                    if result.get("job_executed") and not result.get("ok"):
                        event_type = "JOB_FAIL"

                    with cls._state_lock:
                        cls._state["last_event"] = event_type
                        cls._state["last_agent"] = result.get("agent")
                        cls._state["last_job_id"] = result.get("job_id")
                        cls._state["last_job_status"] = result.get("status")
                        cls._state["last_error"] = result.get("error")
                        cls._state["updated_at"] = datetime.now().isoformat(timespec="seconds")

                    sleep_seconds = 0 if result.get("job_executed") else int(config["no_job_sleep_seconds"])
                    helper._write_batch_log_safe(
                        config,
                        run_id,
                        loop_no,
                        event_type,
                        agent_name=result.get("agent"),
                        job_id=result.get("job_id"),
                        job_status=result.get("status"),
                        message=result.get("message"),
                        error_message=result.get("error"),
                        sleep_seconds=sleep_seconds,
                        elapsed_seconds=elapsed,
                    )
                    if sleep_seconds > 0:
                        helper._interruptible_sleep(sleep_seconds)

                except Exception as exc:
                    elapsed = round(time.perf_counter() - started, 3)
                    error_message = f"{exc}\n{traceback.format_exc()}"
                    with cls._state_lock:
                        cls._state["last_event"] = "LOOP_ERROR"
                        cls._state["last_error"] = str(exc)
                        cls._state["updated_at"] = datetime.now().isoformat(timespec="seconds")
                    helper._write_batch_log_safe(
                        config,
                        run_id,
                        loop_no,
                        "LOOP_ERROR",
                        message="Unexpected batch loop error.",
                        error_message=error_message,
                        sleep_seconds=int(config["error_sleep_seconds"]),
                        elapsed_seconds=elapsed,
                    )
                    helper._interruptible_sleep(int(config["error_sleep_seconds"]))
        finally:
            with cls._state_lock:
                cls._state["running"] = False
                cls._state["last_event"] = "STOPPED"
                cls._state["updated_at"] = datetime.now().isoformat(timespec="seconds")
            helper._write_batch_log_safe(config, run_id, int(cls._state.get("loop_no") or 0), "STOPPED", message="Batch agent stopped.")

    def _execute_one_job(self, config: dict[str, Any]) -> dict[str, Any]:
        migration_job = self._poll_next_migration_job(config)
        if migration_job:
            map_id = migration_job["map_id"]
            result = self._run_migration_job(config, map_id)
            return {
                "job_executed": True,
                "ok": bool(result.get("ok")),
                "agent": "DB_MIGRATION",
                "job_id": str(map_id),
                "status": result.get("status"),
                "message": result.get("message") or "Migration job finished.",
                "error": result.get("error"),
            }

        sql_job = self._poll_next_sql_conversion_job(config)
        if sql_job:
            space_nm = sql_job["space_nm"]
            sql_id = sql_job["sql_id"]
            result = self._run_sql_conversion_job(config, space_nm, sql_id)
            return {
                "job_executed": True,
                "ok": bool(result.get("ok")),
                "agent": "SQL_CONVERSION",
                "job_id": f"{space_nm}/{sql_id}",
                "status": result.get("status"),
                "message": result.get("message") or "SQL conversion job finished.",
                "error": result.get("error"),
            }

        return {
            "job_executed": False,
            "ok": True,
            "agent": None,
            "job_id": None,
            "status": "NO_JOB",
            "message": "No pending migration or SQL conversion job found.",
            "error": None,
        }

    def _run_migration_job(self, config: dict[str, Any], map_id: int) -> dict[str, Any]:
        tool = self._build_migration_tool(config)
        return tool._run_migration_job(
            map_id,
            {
                "action": "run_migration_job",
                "map_id": map_id,
                "max_attempts": config["migration_max_attempts"],
            },
        )

    def _run_sql_conversion_job(self, config: dict[str, Any], space_nm: str, sql_id: str) -> dict[str, Any]:
        tool = self._build_sql_conversion_tool(config)
        return tool.run_sql_conversion_job(
            sql_id,
            space_nm,
            {
                "action": "run_sql_conversion_job",
                "space_nm": space_nm,
                "sql_id": sql_id,
                "max_attempts": config["sql_conversion_max_attempts"],
            },
        )

    def _poll_next_migration_job(self, config: dict[str, Any]) -> dict[str, Any] | None:
        table = self._qualify_table("NEXT_MIG_INFO", config["system_schema"])
        sql = f"""
            SELECT MAP_ID, PRIORITY
            FROM (
                SELECT MAP_ID, PRIORITY
                FROM {table}
                WHERE UPPER(TRIM(NVL(USE_YN, 'N'))) = 'Y'
                  AND STATUS IS NULL
                ORDER BY PRIORITY ASC, MAP_ID ASC
            )
            WHERE ROWNUM <= 1
        """
        rows = self._query(config, sql)
        if not rows:
            return None
        return {"map_id": rows[0][0], "priority": rows[0][1]}

    def _poll_next_sql_conversion_job(self, config: dict[str, Any]) -> dict[str, Any] | None:
        table = self._qualify_table("NEXT_SQL_INFO", config["system_schema"])
        sql = f"""
            SELECT SPACE_NM, SQL_ID, PRIORITY
            FROM (
                SELECT SPACE_NM, SQL_ID, PRIORITY
                FROM {table}
                WHERE STATUS_CONVERSION IS NULL
                ORDER BY PRIORITY ASC NULLS LAST, UPD_TS NULLS FIRST, SPACE_NM, SQL_ID
            )
            WHERE ROWNUM <= 1
        """
        rows = self._query(config, sql)
        if not rows:
            return None
        return {"space_nm": self._to_text(rows[0][0]), "sql_id": self._to_text(rows[0][1]), "priority": rows[0][2]}

    def _build_migration_tool(self, config: dict[str, Any]) -> MigrationCommandTool:
        tool = object.__new__(MigrationCommandTool)
        for key, value in config.items():
            setattr(tool, key, value)
        tool.default_max_attempts = config["migration_max_attempts"]
        return tool

    def _build_sql_conversion_tool(self, config: dict[str, Any]) -> SqlConversionCommandTool:
        tool = object.__new__(SqlConversionCommandTool)
        for key, value in config.items():
            setattr(tool, key, value)
        return tool

    def _snapshot_config(self) -> dict[str, Any]:
        return {
            "db_host": str(self.db_host or "").strip(),
            "db_port": int(self.db_port or 1521),
            "db_service_name": str(self.db_service_name or "").strip(),
            "db_username": str(self.db_username or "").strip(),
            "db_password": self._secret_to_str(self.db_password),
            "llm_base_url": str(self.llm_base_url or "").strip(),
            "llm_api_key": self._secret_to_str(self.llm_api_key),
            "llm_model": str(self.llm_model or "").strip(),
            "llm_max_tokens": int(self.llm_max_tokens or 4096),
            "llm_timeout_seconds": int(self.llm_timeout_seconds or 900),
            "mig_sql_prompt": str(self.mig_sql_prompt or ""),
            "verify_sql_prompt": str(self.verify_sql_prompt or ""),
            "to_sql_prompt": str(self.to_sql_prompt or ""),
            "bind_sql_prompt": str(self.bind_sql_prompt or ""),
            "test_sql_prompt": str(self.test_sql_prompt or ""),
            "system_schema": str(self.system_schema or "").strip(),
            "source_schema": str(self.source_schema or "").strip(),
            "target_schema": str(self.target_schema or "").strip(),
            "migration_max_attempts": max(1, int(self.migration_max_attempts or 3)),
            "sql_conversion_max_attempts": max(1, int(self.sql_conversion_max_attempts or 1)),
            "no_job_sleep_seconds": max(1, int(self.no_job_sleep_seconds or 600)),
            "error_sleep_seconds": max(1, int(self.error_sleep_seconds or 60)),
            "auto_install_packages": self._as_bool(self.auto_install_packages),
            "auto_create_log_table": self._as_bool(self.auto_create_log_table),
        }

    def _parse_command(self) -> dict[str, Any]:
        raw = str(self.command_json or "").strip()
        if not raw:
            return {}
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("command_json must be a JSON object")
        return parsed

    def _connect(self, config: dict[str, Any]):
        self._ensure_runtime_dependencies(config)
        import oracledb

        dsn = f"{config['db_host']}:{config['db_port']}/{config['db_service_name']}"
        return oracledb.connect(user=config["db_username"], password=config["db_password"], dsn=dsn)

    def _ensure_runtime_dependencies(self, config: dict[str, Any]) -> None:
        missing_packages: list[str] = []
        try:
            import langchain_community
        except ModuleNotFoundError:
            missing_packages.append("langchain-community")
        try:
            import sqlalchemy
        except ModuleNotFoundError:
            missing_packages.append("SQLAlchemy")
        try:
            import oracledb
        except ModuleNotFoundError:
            missing_packages.append("oracledb")

        if not missing_packages:
            return
        if not self._as_bool(config.get("auto_install_packages")):
            raise ModuleNotFoundError(
                "Missing packages: "
                + ", ".join(missing_packages)
                + ". Enable Auto Install Missing Packages or install them in the Langflow runtime."
            )
        for package in missing_packages:
            self._pip_install(package)

    def _pip_install(self, package: str) -> None:
        subprocess.check_call([sys.executable, "-m", "pip", "install", package])

    def _query(self, config: dict[str, Any], sql: str, params: list[Any] | None = None) -> list[tuple]:
        with self._connect(config) as conn:
            cur = conn.cursor()
            cur.execute(sql, params or [])
            return cur.fetchall()

    def _ensure_log_table(self, config: dict[str, Any]) -> None:
        table = self._qualify_table("AG_BATCH_AGENT_LOG", config["system_schema"])
        ddl = f"""
            CREATE TABLE {table} (
                LOG_ID NUMBER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                RUN_ID VARCHAR2(64) NOT NULL,
                LOOP_NO NUMBER NOT NULL,
                EVENT_TYPE VARCHAR2(30) NOT NULL,
                AGENT_NAME VARCHAR2(50),
                JOB_ID VARCHAR2(200),
                JOB_STATUS VARCHAR2(50),
                MESSAGE VARCHAR2(1000),
                ERROR_MESSAGE CLOB,
                SLEEP_SECONDS NUMBER,
                STARTED_AT TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FINISHED_AT TIMESTAMP,
                ELAPSED_SECONDS NUMBER
            )
        """
        try:
            with self._connect(config) as conn:
                cur = conn.cursor()
                cur.execute(ddl)
                conn.commit()
        except Exception as exc:
            if "ORA-00955" not in str(exc):
                raise

    def _write_batch_log_safe(
        self,
        config: dict[str, Any],
        run_id: Any,
        loop_no: int,
        event_type: str,
        agent_name: Any = None,
        job_id: Any = None,
        job_status: Any = None,
        message: Any = None,
        error_message: Any = None,
        sleep_seconds: int | None = None,
        elapsed_seconds: float | None = None,
    ) -> None:
        try:
            self._write_batch_log(
                config,
                run_id=run_id,
                loop_no=loop_no,
                event_type=event_type,
                agent_name=agent_name,
                job_id=job_id,
                job_status=job_status,
                message=message,
                error_message=error_message,
                sleep_seconds=sleep_seconds,
                elapsed_seconds=elapsed_seconds,
            )
        except Exception:
            pass

    def _write_batch_log(
        self,
        config: dict[str, Any],
        run_id: Any,
        loop_no: int,
        event_type: str,
        agent_name: Any = None,
        job_id: Any = None,
        job_status: Any = None,
        message: Any = None,
        error_message: Any = None,
        sleep_seconds: int | None = None,
        elapsed_seconds: float | None = None,
    ) -> None:
        table = self._qualify_table("AG_BATCH_AGENT_LOG", config["system_schema"])
        with self._connect(config) as conn:
            cur = conn.cursor()
            cur.execute(
                f"""
                INSERT INTO {table} (
                    RUN_ID, LOOP_NO, EVENT_TYPE, AGENT_NAME, JOB_ID, JOB_STATUS,
                    MESSAGE, ERROR_MESSAGE, SLEEP_SECONDS, STARTED_AT, FINISHED_AT, ELAPSED_SECONDS
                ) VALUES (
                    :1, :2, :3, :4, :5, :6,
                    :7, :8, :9, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, :10
                )
                """,
                [
                    str(run_id or "")[:64],
                    int(loop_no or 0),
                    str(event_type or "")[:30],
                    str(agent_name or "")[:50] if agent_name else None,
                    str(job_id or "")[:200] if job_id else None,
                    str(job_status or "")[:50] if job_status else None,
                    str(message or "")[:1000] if message else None,
                    str(error_message or "") if error_message else None,
                    sleep_seconds,
                    elapsed_seconds,
                ],
            )
            conn.commit()

    def _interruptible_sleep(self, seconds: int) -> None:
        deadline = time.time() + max(0, int(seconds))
        while time.time() < deadline:
            if self.__class__._stop_event.is_set():
                break
            time.sleep(min(1.0, max(0.0, deadline - time.time())))

    def _qualify_table(self, table_name: str, schema: str | None) -> str:
        clean = str(table_name or "").strip().upper()
        clean_schema = str(schema or "").strip().upper()
        if not clean:
            raise ValueError("table_name is empty")
        if "." in clean or not clean_schema:
            return clean
        if not re.fullmatch(r"[A-Z][A-Z0-9_$#]*", clean_schema):
            raise ValueError(f"Invalid schema: {clean_schema}")
        return f"{clean_schema}.{clean}"

    def _secret_to_str(self, value: Any) -> str:
        if value is None:
            return ""
        if hasattr(value, "get_secret_value"):
            return str(value.get_secret_value())
        return str(value)

    def _to_text(self, value: Any) -> str:
        if value is None:
            return ""
        if hasattr(value, "read"):
            value = value.read()
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="ignore")
        return str(value)

    def _as_bool(self, value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        return str(value).strip().lower() in {"1", "true", "t", "y", "yes", "on"}
