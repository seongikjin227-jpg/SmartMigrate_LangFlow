from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote_plus

from lfx.custom.custom_component.component import Component
from lfx.io import BoolInput, IntInput, MessageTextInput, SecretStrInput, StrInput, Output
from lfx.schema.data import Data


class _BatchStopRequested(Exception):
    """Raised inside the batch worker when a stop request should interrupt the current job."""


class BatchAgentCommandTool(Component):
    display_name = "Batch Agent Command Tool"
    description = "Starts and controls a background SmartMigration batch loop inside the Langflow container."
    name = "BatchAgentCommandTool"
    icon = "Timer"

    _thread: threading.Thread | None = None
    _stop_event = threading.Event()
    _state_lock = threading.Lock()
    _db_cache: dict[str, Any] = {}

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
            info='Batch command. Supported actions: {"action":"start"}, {"action":"stop"}, {"action":"status"}. Plain text batch start phrases map to start.',
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
        IntInput(name="sql_conversion_max_attempts", display_name="SQL Conversion Max Attempts", value=3, required=False),
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
        IntInput(
            name="status_log_alive_grace_seconds",
            display_name="Status Log Alive Grace Seconds",
            value=1800,
            required=False,
            info="Control heartbeat grace seconds. RUNNING/STOP_REQUESTED is treated as alive only when NEXT_BATCH_CONTROL.HEARTBEAT_AT is newer than this value.",
        ),
        BoolInput(
            name="auto_install_packages",
            display_name="Auto Install Missing Packages",
            value=False,
            required=False,
            info="If true, installs missing runtime packages with pip before DB connection.",
        ),
        BoolInput(
            name="background_thread_daemon",
            display_name="Background Thread Daemon",
            value=False,
            required=False,
            info="If false, the background thread is non-daemon so normal Python process shutdown waits for it. This can improve survival but may delay container shutdown.",
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
                result = self._status(config)
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

            run_id = datetime.now().strftime("%Y%m%d%H%M%S%f")
            control_result = self._try_start_control(config, run_id)
            if not control_result.get("acquired"):
                existing_run_id = control_result.get("run_id")
                self._write_batch_log_safe(config, existing_run_id, 0, "ALREADY_RUNNING", message=str(control_result.get("message") or "Batch agent is already running."))
                return {"ok": True, "status": control_result.get("status") or "already_running", "running": True, **control_result}

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
                daemon=bool(config["background_thread_daemon"]),
                name=f"smartmigration-batch-{run_id}",
            )
            cls._thread.start()

        self._write_batch_log_safe(config, run_id, 0, "START", message="Batch agent started.")
        return {"ok": True, "status": "started", "running": True, "mode": "background_thread", "daemon": bool(config["background_thread_daemon"])}

    def _stop(self, config: dict[str, Any]) -> dict[str, Any]:
        cls = self.__class__
        state = self._status(config)
        cls._stop_event.set()
        control_result = self._request_stop_control(config)
        run_id = control_result.get("run_id") or state.get("run_id") or self._find_latest_active_run_id(config)
        loop_no = int(state.get("loop_no") or 0)
        with cls._state_lock:
            cls._state["running"] = False
            cls._state["updated_at"] = datetime.now().isoformat(timespec="seconds")
            cls._state["last_event"] = "STOP_REQUESTED"
        self._write_batch_log_safe(config, run_id, loop_no, "STOP_REQUESTED", message="Stop requested.")
        return {"ok": True, "status": "stop_requested", "running": False, "run_id": run_id, "control": control_result}

    def _status(self, config: dict[str, Any] | None = None) -> dict[str, Any]:
        cls = self.__class__
        memory_alive = bool(cls._thread and cls._thread.is_alive() and not cls._stop_event.is_set())
        with cls._state_lock:
            state = dict(cls._state)
        state_alive = bool(state.get("running") and not cls._stop_event.is_set())
        control_status = self._get_control_status(config) if config else {}
        control_running = (
            str(control_status.get("status") or "").upper() in {"RUNNING", "STOP_REQUESTED"}
            and bool(control_status.get("heartbeat_alive"))
        )
        state["running"] = bool(memory_alive or state_alive or control_running)
        if control_running:
            state["running"] = True
        state["memory_thread_alive"] = memory_alive
        state["memory_state_running"] = state_alive
        state["status_source"] = "memory" if (memory_alive or state_alive) else ("batch_control" if control_running else "none")
        state["control"] = control_status
        if control_status:
            state["run_id"] = control_status.get("run_id") or state.get("run_id")
            state["loop_no"] = control_status.get("loop_no") or state.get("loop_no")
            state["last_event"] = control_status.get("last_event") or state.get("last_event")
            state["last_agent"] = control_status.get("last_agent") or state.get("last_agent")
            state["last_job_id"] = control_status.get("last_job_id") or state.get("last_job_id")
            state["last_job_status"] = control_status.get("last_job_status") or state.get("last_job_status")
            state["last_error"] = control_status.get("last_error") or state.get("last_error")
        return {"ok": True, **state}

    def _ensure_control_row(self, config: dict[str, Any]) -> None:
        table = self._qualify_table("NEXT_BATCH_CONTROL", config["system_schema"])
        try:
            with self._connect(config) as conn:
                cur = conn.cursor()
                cur.execute(
                    f"""
                    INSERT INTO {table} (
                        CONTROL_NAME, STATUS, STOP_REQUESTED_YN, LOOP_NO,
                        UPDATED_AT, MESSAGE
                    ) VALUES (
                        'BATCH_AGENT', 'STOPPED', 'N', 0,
                        CURRENT_TIMESTAMP, 'Initialized by batch agent action.'
                    )
                    """
                )
                conn.commit()
        except Exception as exc:
            if "ORA-00001" not in str(exc):
                raise

    def _try_start_control(self, config: dict[str, Any], run_id: str) -> dict[str, Any]:
        self._ensure_control_row(config)
        table = self._qualify_table("NEXT_BATCH_CONTROL", config["system_schema"])
        grace_seconds = max(1, int(config.get("status_log_alive_grace_seconds") or 1800))
        with self._connect(config) as conn:
            cur = conn.cursor()
            cur.execute(
                f"""
                SELECT STATUS, RUN_ID, STOP_REQUESTED_YN, LOOP_NO,
                       CASE
                         WHEN HEARTBEAT_AT >= LOCALTIMESTAMP - NUMTODSINTERVAL(:1, 'SECOND')
                         THEN 1 ELSE 0
                       END AS HEARTBEAT_ALIVE
                FROM {table}
                WHERE CONTROL_NAME = 'BATCH_AGENT'
                FOR UPDATE
                """,
                [grace_seconds],
            )
            row = cur.fetchone()
            if not row:
                raise ValueError("NEXT_BATCH_CONTROL row BATCH_AGENT was not initialized")

            status = self._to_text(row[0]).strip().upper()
            existing_run_id = self._to_text(row[1])
            heartbeat_alive = bool(row[4])
            if status in {"RUNNING", "STOP_REQUESTED"} and heartbeat_alive:
                conn.rollback()
                return {
                    "acquired": False,
                    "status": "already_running" if status == "RUNNING" else "stop_requested",
                    "control_status": status,
                    "run_id": existing_run_id,
                    "heartbeat_alive": heartbeat_alive,
                    "message": f"Batch control is {status} for RUN_ID={existing_run_id}.",
                }

            cur.execute(
                f"""
                UPDATE {table}
                SET STATUS = 'RUNNING',
                    RUN_ID = :1,
                    STOP_REQUESTED_YN = 'N',
                    LOOP_NO = 0,
                    STARTED_AT = CURRENT_TIMESTAMP,
                    STOP_REQUESTED_AT = NULL,
                    STOPPED_AT = NULL,
                    HEARTBEAT_AT = CURRENT_TIMESTAMP,
                    UPDATED_AT = CURRENT_TIMESTAMP,
                    LAST_EVENT = 'START',
                    LAST_AGENT = NULL,
                    LAST_JOB_ID = NULL,
                    LAST_JOB_STATUS = NULL,
                    LAST_ERROR = NULL,
                    MESSAGE = :2
                WHERE CONTROL_NAME = 'BATCH_AGENT'
                """,
                [run_id, f"Batch agent started. RUN_ID={run_id}"],
            )
            conn.commit()
        return {"acquired": True, "status": "started", "run_id": run_id, "heartbeat_alive": True}

    def _request_stop_control(self, config: dict[str, Any]) -> dict[str, Any]:
        self._ensure_control_row(config)
        table = self._qualify_table("NEXT_BATCH_CONTROL", config["system_schema"])
        with self._connect(config) as conn:
            cur = conn.cursor()
            cur.execute(
                f"""
                SELECT STATUS, RUN_ID, LOOP_NO
                FROM {table}
                WHERE CONTROL_NAME = 'BATCH_AGENT'
                FOR UPDATE
                """
            )
            row = cur.fetchone()
            if not row:
                raise ValueError("NEXT_BATCH_CONTROL row BATCH_AGENT was not initialized")
            status = self._to_text(row[0]).strip().upper()
            run_id = self._to_text(row[1])
            loop_no = int(row[2] or 0)
            cur.execute(
                f"""
                UPDATE {table}
                SET STATUS = CASE WHEN STATUS = 'RUNNING' THEN 'STOP_REQUESTED' ELSE STATUS END,
                    STOP_REQUESTED_YN = 'Y',
                    STOP_REQUESTED_AT = CURRENT_TIMESTAMP,
                    UPDATED_AT = CURRENT_TIMESTAMP,
                    LAST_EVENT = 'STOP_REQUESTED',
                    MESSAGE = 'Stop requested by batch agent action.'
                WHERE CONTROL_NAME = 'BATCH_AGENT'
                """
            )
            conn.commit()
        return {"ok": True, "previous_status": status, "status": "STOP_REQUESTED", "run_id": run_id, "loop_no": loop_no}

    def _get_control_status(self, config: dict[str, Any]) -> dict[str, Any]:
        self._ensure_control_row(config)
        table = self._qualify_table("NEXT_BATCH_CONTROL", config["system_schema"])
        grace_seconds = max(1, int(config.get("status_log_alive_grace_seconds") or 1800))
        sql = f"""
            SELECT STATUS, RUN_ID, STOP_REQUESTED_YN, LOOP_NO,
                   LAST_EVENT, LAST_AGENT, LAST_JOB_ID, LAST_JOB_STATUS, LAST_ERROR, MESSAGE,
                   TO_CHAR(STARTED_AT, 'YYYY-MM-DD HH24:MI:SS') AS STARTED_AT_TEXT,
                   TO_CHAR(HEARTBEAT_AT, 'YYYY-MM-DD HH24:MI:SS') AS HEARTBEAT_AT_TEXT,
                   TO_CHAR(STOP_REQUESTED_AT, 'YYYY-MM-DD HH24:MI:SS') AS STOP_REQUESTED_AT_TEXT,
                   TO_CHAR(STOPPED_AT, 'YYYY-MM-DD HH24:MI:SS') AS STOPPED_AT_TEXT,
                   TO_CHAR(UPDATED_AT, 'YYYY-MM-DD HH24:MI:SS') AS UPDATED_AT_TEXT,
                   CASE
                     WHEN HEARTBEAT_AT >= LOCALTIMESTAMP - NUMTODSINTERVAL(:1, 'SECOND')
                     THEN 1 ELSE 0
                   END AS HEARTBEAT_ALIVE
            FROM {table}
            WHERE CONTROL_NAME = 'BATCH_AGENT'
        """
        try:
            rows = self._query(config, sql, [grace_seconds])
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        if not rows:
            return {"ok": False, "error": "NEXT_BATCH_CONTROL row BATCH_AGENT not found"}
        row = rows[0]
        return {
            "ok": True,
            "status": self._to_text(row[0]),
            "run_id": self._to_text(row[1]),
            "stop_requested": self._to_text(row[2]).strip().upper() == "Y",
            "loop_no": row[3],
            "last_event": self._to_text(row[4]),
            "last_agent": self._to_text(row[5]),
            "last_job_id": self._to_text(row[6]),
            "last_job_status": self._to_text(row[7]),
            "last_error": self._to_text(row[8]),
            "message": self._to_text(row[9]),
            "started_at": self._to_text(row[10]),
            "heartbeat_at": self._to_text(row[11]),
            "stop_requested_at": self._to_text(row[12]),
            "stopped_at": self._to_text(row[13]),
            "updated_at": self._to_text(row[14]),
            "heartbeat_alive": bool(row[15]),
            "grace_seconds": grace_seconds,
        }

    def _update_control_heartbeat(
        self,
        config: dict[str, Any],
        run_id: str,
        loop_no: int,
        last_event: str,
        agent_name: Any = None,
        job_id: Any = None,
        job_status: Any = None,
        last_error: Any = None,
        message: Any = None,
    ) -> None:
        table = self._qualify_table("NEXT_BATCH_CONTROL", config["system_schema"])
        try:
            with self._connect(config) as conn:
                cur = conn.cursor()
                cur.execute(
                    f"""
                    UPDATE {table}
                    SET LOOP_NO = :1,
                        HEARTBEAT_AT = CURRENT_TIMESTAMP,
                        UPDATED_AT = CURRENT_TIMESTAMP,
                        LAST_EVENT = :2,
                        LAST_AGENT = :3,
                        LAST_JOB_ID = :4,
                        LAST_JOB_STATUS = :5,
                        LAST_ERROR = :6,
                        MESSAGE = :7
                    WHERE CONTROL_NAME = 'BATCH_AGENT'
                      AND RUN_ID = :8
                    """,
                    [
                        int(loop_no or 0),
                        str(last_event or "")[:50],
                        str(agent_name or "")[:50] if agent_name else None,
                        str(job_id or "")[:200] if job_id else None,
                        str(job_status or "")[:50] if job_status else None,
                        str(last_error or "")[:3900] if last_error else None,
                        str(message or "")[:1000] if message else None,
                        str(run_id or ""),
                    ],
                )
                conn.commit()
        except Exception:
            pass

    def _finish_control(self, config: dict[str, Any], run_id: str, message: str) -> None:
        table = self._qualify_table("NEXT_BATCH_CONTROL", config["system_schema"])
        try:
            with self._connect(config) as conn:
                cur = conn.cursor()
                cur.execute(
                    f"""
                    UPDATE {table}
                    SET STATUS = 'STOPPED',
                        STOP_REQUESTED_YN = 'N',
                        STOPPED_AT = CURRENT_TIMESTAMP,
                        HEARTBEAT_AT = CURRENT_TIMESTAMP,
                        UPDATED_AT = CURRENT_TIMESTAMP,
                        LAST_EVENT = 'STOPPED',
                        MESSAGE = :1
                    WHERE CONTROL_NAME = 'BATCH_AGENT'
                      AND RUN_ID = :2
                    """,
                    [message, str(run_id or "")],
                )
                conn.commit()
        except Exception:
            pass

    def _find_latest_active_run_id(self, config: dict[str, Any]) -> str | None:
        control_status = self._get_control_status(config)
        return self._to_text(control_status.get("run_id")) or None

    def _is_db_stop_requested(self, config: dict[str, Any], run_id: str) -> bool:
        if not run_id:
            return False
        table = self._qualify_table("NEXT_BATCH_CONTROL", config["system_schema"])
        sql = f"""
            SELECT COUNT(*)
            FROM {table}
            WHERE CONTROL_NAME = 'BATCH_AGENT'
              AND RUN_ID = :1
              AND (
                    STOP_REQUESTED_YN = 'Y'
                 OR STATUS = 'STOP_REQUESTED'
              )
        """
        try:
            rows = self._query(config, sql, [run_id])
        except Exception:
            return False
        if not rows:
            return False
        return int(rows[0][0] or 0) > 0

    def _raise_if_batch_stop_requested(self) -> None:
        config = getattr(self, "_batch_runtime_config", None)
        run_id = str((config or {}).get("batch_run_id") or "").strip()
        if self.__class__._stop_event.is_set():
            raise _BatchStopRequested("Batch stop requested.")
        if config and run_id and self._is_db_stop_requested(config, run_id):
            raise _BatchStopRequested("Batch stop requested.")

    @classmethod
    def _worker_loop(cls, config: dict[str, Any], run_id: str) -> None:
        helper = object.__new__(cls)
        config = {**config, "batch_run_id": run_id}
        helper._batch_runtime_config = config
        try:
            while not cls._stop_event.is_set() and not helper._is_db_stop_requested(config, run_id):
                with cls._state_lock:
                    cls._state["loop_no"] = int(cls._state.get("loop_no") or 0) + 1
                    loop_no = int(cls._state["loop_no"])
                    cls._state["updated_at"] = datetime.now().isoformat(timespec="seconds")
                    cls._state["last_event"] = "LOOP_START"

                started = time.perf_counter()
                helper._update_control_heartbeat(config, run_id, loop_no, "LOOP_START", message="Batch loop started.")
                helper._write_batch_log_safe(config, run_id, loop_no, "LOOP_START", message="Batch loop started.")
                if helper._is_db_stop_requested(config, run_id):
                    break

                try:
                    result = helper._execute_one_job(config)
                    elapsed = round(time.perf_counter() - started, 3)
                    event_type = "JOB_SUCCESS" if result.get("job_executed") else "NO_JOB"
                    if str(result.get("status") or "").strip().upper() == "STOPPED":
                        event_type = "JOB_STOPPED"
                    elif result.get("job_executed") and not result.get("ok"):
                        event_type = "JOB_FAIL"

                    with cls._state_lock:
                        cls._state["last_event"] = event_type
                        cls._state["last_agent"] = result.get("agent")
                        cls._state["last_job_id"] = result.get("job_id")
                        cls._state["last_job_status"] = result.get("status")
                        cls._state["last_error"] = result.get("error")
                        cls._state["updated_at"] = datetime.now().isoformat(timespec="seconds")

                    sleep_seconds = 0 if result.get("job_executed") else int(config["no_job_sleep_seconds"])
                    helper._update_control_heartbeat(
                        config,
                        run_id,
                        loop_no,
                        event_type,
                        agent_name=result.get("agent"),
                        job_id=result.get("job_id"),
                        job_status=result.get("status"),
                        last_error=result.get("error"),
                        message=result.get("message"),
                    )
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
                    if event_type == "JOB_STOPPED":
                        break
                    if sleep_seconds > 0:
                        helper._interruptible_sleep(sleep_seconds, config, run_id)

                except _BatchStopRequested as exc:
                    elapsed = round(time.perf_counter() - started, 3)
                    with cls._state_lock:
                        cls._state["last_event"] = "JOB_STOPPED"
                        cls._state["last_error"] = str(exc)
                        cls._state["updated_at"] = datetime.now().isoformat(timespec="seconds")
                    helper._update_control_heartbeat(config, run_id, loop_no, "JOB_STOPPED", last_error=str(exc), message=str(exc))
                    helper._write_batch_log_safe(
                        config,
                        run_id,
                        loop_no,
                        "JOB_STOPPED",
                        message=str(exc),
                        elapsed_seconds=elapsed,
                    )
                    break

                except Exception as exc:
                    elapsed = round(time.perf_counter() - started, 3)
                    error_message = f"{exc}\n{traceback.format_exc()}"
                    with cls._state_lock:
                        cls._state["last_event"] = "LOOP_ERROR"
                        cls._state["last_error"] = str(exc)
                        cls._state["updated_at"] = datetime.now().isoformat(timespec="seconds")
                    helper._update_control_heartbeat(config, run_id, loop_no, "LOOP_ERROR", last_error=str(exc), message="Unexpected batch loop error.")
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
                    helper._interruptible_sleep(int(config["error_sleep_seconds"]), config, run_id)
        finally:
            with cls._state_lock:
                cls._state["running"] = False
                cls._state["last_event"] = "STOPPED"
                cls._state["updated_at"] = datetime.now().isoformat(timespec="seconds")
                if cls._thread is threading.current_thread():
                    cls._thread = None
            helper._finish_control(config, run_id, "Batch agent stopped.")
            helper._write_batch_log_safe(config, run_id, int(cls._state.get("loop_no") or 0), "STOPPED", message="Batch agent stopped.")

    def _execute_one_job(self, config: dict[str, Any]) -> dict[str, Any]:
        self._batch_runtime_config = config
        self._raise_if_batch_stop_requested()
        migration_job = self._poll_next_migration_job(config)
        if migration_job:
            self._raise_if_batch_stop_requested()
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
            self._raise_if_batch_stop_requested()
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
        self._apply_config(config)
        self._batch_runtime_config = config
        return self._mig__run_migration_job(
            map_id,
            {
                "action": "run_migration_job",
                "map_id": map_id,
                "max_attempts": config["migration_max_attempts"],
            },
        )
    def _run_sql_conversion_job(self, config: dict[str, Any], space_nm: str, sql_id: str) -> dict[str, Any]:
        self._apply_config(config)
        self._batch_runtime_config = config
        return self._sql_run_sql_conversion_job(
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

    def _apply_config(self, config: dict[str, Any]) -> None:
        for key, value in config.items():
            setattr(self, key, value)
        self.default_max_attempts = config["migration_max_attempts"]
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
            "sql_conversion_max_attempts": max(1, int(self.sql_conversion_max_attempts or 3)),
            "no_job_sleep_seconds": max(1, int(self.no_job_sleep_seconds or 600)),
            "error_sleep_seconds": max(1, int(self.error_sleep_seconds or 60)),
            "status_log_alive_grace_seconds": max(1, int(self.status_log_alive_grace_seconds or 1800)),
            "auto_install_packages": self._as_bool(self.auto_install_packages),
            "background_thread_daemon": self._as_bool(self.background_thread_daemon),
        }

    def _parse_command(self) -> dict[str, Any]:
        raw = str(self.command_json or "").strip()
        if not raw:
            return {}
        if raw in {"백그라운드 실행", "백그라운드 에이전트 실행", "배치 에이전트 시작", "배치 시작"}:
            return {"action": "start"}
        if raw in {"배치 멈춰", "배치 중지", "백그라운드 중지"}:
            return {"action": "stop"}
        if raw in {"배치 상태", "백그라운드 상태"}:
            return {"action": "status"}
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
        table = self._qualify_table("NEXT_BATCH_LOG", config["system_schema"])
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

    def _interruptible_sleep(self, seconds: int, config: dict[str, Any] | None = None, run_id: str | None = None) -> None:
        deadline = time.time() + max(0, int(seconds))
        while time.time() < deadline:
            if self.__class__._stop_event.is_set():
                break
            if config and run_id and self._is_db_stop_requested(config, run_id):
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

    def _mig_run_command(self) -> Data:
        try:
            command = self._mig__parse_command()
            action = (command.get("action") or "").strip().lower()
            map_id = command.get("map_id")

            if action == "test_connection":
                result = self._mig__test_connection()
            elif action == "status":
                result = self._mig__status(map_id)
            elif action == "list_pending":
                result = self._mig__list_pending(command.get("limit", 10))
            elif action == "get_table_ddl":
                result = self._mig__get_table_ddl(command.get("table_name"), command.get("schema"))
            elif action == "generate_mig_sql":
                result = self._mig__generate_mig_sql(map_id, command)
            elif action == "generate_verify_sql":
                result = self._mig__generate_verify_sql(map_id, command)
            elif action == "preview_mig_prompt":
                result = self._mig__preview_sql_prompt(map_id, command, prompt_kind="mig")
            elif action == "preview_verify_prompt":
                result = self._mig__preview_sql_prompt(map_id, command, prompt_kind="verify")
            elif action == "reset":
                result = self._mig__reset(map_id, command)
            elif action == "save_user_sql":
                result = self._mig__save_user_sql(map_id, command)
            elif action == "analyze_failure":
                result = self._mig__analyze_failure(map_id)
            elif action == "run_migration_job":
                result = self._mig__run_migration_job(map_id, command)
            else:
                result = {"ok": False, "error": f"Unsupported action: {action}"}

            self.status = result
            return Data(data=result)
        except Exception as exc: 
            result = {"ok": False, "error": str(exc)}
            self.status = result
            return Data(data=result)

    # action="test_connection": DB와 LLM 연결 상태를 확인한다.
    def _mig__test_connection(self) -> dict[str, Any]:
        try:
            rows = self._mig__normalize_query_rows(self._mig__get_db().run("SELECT 1 AS OK FROM DUAL", include_columns=True))
            db_result = {"ok": True, "message": "DB connection OK", "result": rows}
        except Exception as exc:
            db_result = {"ok": False, "message": "DB connection failed", "error": str(exc)}

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
                data = self._mig__post_json(url, payload, {"Authorization": f"Bearer {api_key}"})
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

    # action="status": 작업 상태를 조회한다.
    def _mig__status(self, map_id: Any) -> dict[str, Any]:
        if map_id is None or str(map_id).strip() == "":
            raise ValueError("map_id is required")
        map_id = int(map_id)
        job = self._mig__load_job(map_id)
        if not job:
            return {"ok": False, "map_id": map_id, "error": "job not found"}
        details = self._mig__load_details(map_id)
        return {"ok": True, "job": job, "details": details}

    # action="list_pending": 실행 가능한 대기 작업을 조회한다.
    def _mig__list_pending(self, limit: Any) -> dict[str, Any]:
        safe_limit = max(1, min(int(limit or 10), 50))
        map_table = self._mig__qualify_table("NEXT_MIG_INFO", self.system_schema)
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
        with self._mig__connect() as conn:
            cur = conn.cursor()
            cur.execute(sql)
            rows = cur.fetchall()
        jobs = [
            {
                "map_id": r[0],
                "map_type": self._mig__to_text(r[1]),
                "fr_table": self._mig__to_text(r[2]),
                "to_table": self._mig__to_text(r[3]),
                "use_yn": self._mig__to_text(r[4]),
                "trunc_yn": self._mig__to_text(r[5]),
                "priority": r[6],
                "status": self._mig__to_text(r[7]),
                "user_edited": self._mig__to_text(r[8]),
                "prior_map_id": r[9],
                "retry_count": r[10],
                "upd_ts": self._mig__to_text(r[11]),
            }
            for r in rows
        ]
        return {"ok": True, "count": len(jobs), "jobs": jobs}

    # action="get_table_ddl": Oracle 테이블 컬럼 메타데이터를 조회한다.
    def _mig__get_table_ddl(self, table_name: Any, schema: Any = None) -> dict[str, Any]:
        clean_table = str(table_name or "").strip().upper()
        clean_schema = str(schema or "").strip().upper()
        if not clean_table:
            raise ValueError("table_name is required")
        if "." in clean_table and not clean_schema:
            clean_schema, clean_table = clean_table.split(".", 1)
        if not re.fullmatch(r"[A-Z][A-Z0-9_$#]*", clean_table):
            raise ValueError(f"Invalid table_name: {clean_table}")
        if clean_schema:
            if not re.fullmatch(r"[A-Z][A-Z0-9_$#]*", clean_schema):
                raise ValueError(f"Invalid schema: {clean_schema}")
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
        rows = self._mig__normalize_query_rows(self._mig__get_db().run(query, include_columns=True))

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
                "column_name": self._mig__to_text(column_value(row, "COLUMN_NAME")),
                "data_type": self._mig__to_text(column_value(row, "DATA_TYPE")),
                "data_length": column_value(row, "DATA_LENGTH"),
                "data_precision": column_value(row, "DATA_PRECISION"),
                "data_scale": column_value(row, "DATA_SCALE"),
                "nullable": self._mig__to_text(column_value(row, "NULLABLE")),
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

    # action="generate_mig_sql": MIG_SQL을 생성한다.
    def _mig__generate_mig_sql(self, map_id: Any, command: dict[str, Any]) -> dict[str, Any]:
        if map_id is None or str(map_id).strip() == "":
            raise ValueError("map_id is required")
        map_id = int(map_id)
        job = self._mig__load_job(map_id)
        if not job:
            return {"ok": False, "map_id": map_id, "error": "job not found"}

        user_edited = str(job.get("user_edited") or "").strip().upper() == "Y"
        existing_mig_sql = str(job.get("mig_sql") or "").strip()
        if user_edited:
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
        dep = self._mig__check_dependencies(job)
        if not dep["ok"]:
            return {"ok": False, "map_id": map_id, "status": dep["status"], "message": dep["message"]}
        details = self._mig__load_details(map_id)
        if not details:
            return {"ok": False, "map_id": map_id, "error": "No mapping details found"}

        generation_source = "llm"
        llm_error = ""
        try:
            mig_sql_prompt = str(self.mig_sql_prompt or "").strip()
            if not mig_sql_prompt:
                raise ValueError("MIG SQL Prompt input is required for SQL generation")
            prompt = self._mig__render_sql_prompt(
                template=mig_sql_prompt,
                job=job,
                details=details,
                command=command,
            )
            mig_sql = self._mig__sanitize_migration_sql(
                self._mig__extract_sql(self._mig__call_llm(prompt), expected="insert", key="migration_sql")
            )
        except Exception as exc:
            llm_error = str(exc)
            return {"ok": False, "map_id": map_id, "error": llm_error, "generation_source": generation_source}
        return {
            "ok": True,
            "map_id": map_id,
            "status": "MIG_SQL_GENERATED",
            "generation_source": generation_source,
            "llm_error": llm_error,
            "mig_sql": mig_sql,
        }

    # action="generate_verify_sql": VERIFY_SQL을 생성한다.
    def _mig__generate_verify_sql(self, map_id: Any, command: dict[str, Any]) -> dict[str, Any]:
        if map_id is None or str(map_id).strip() == "":
            raise ValueError("map_id is required")
        map_id = int(map_id)
        job = self._mig__load_job(map_id)
        if not job:
            return {"ok": False, "map_id": map_id, "error": "job not found"}

        user_edited = str(job.get("user_edited") or "").strip().upper() == "Y"
        existing_mig_sql = str(job.get("mig_sql") or "").strip()
        existing_verify_sql = str(job.get("verify_sql") or "").strip()
        if user_edited:
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

        dep = self._mig__check_dependencies(job)
        if not dep["ok"]:
            return {"ok": False, "map_id": map_id, "status": dep["status"], "message": dep["message"]}

        details = self._mig__load_details(map_id)
        generation_source = "llm"
        llm_error = ""

        try:
            verify_sql_prompt = str(self.verify_sql_prompt or "").strip()
            if not verify_sql_prompt:
                raise ValueError("VERIFY SQL Prompt input is required for SQL generation")
            prompt = self._mig__render_sql_prompt(
                template=verify_sql_prompt,
                job=job,
                details=details,
                command=command,
            )
            verify_sql = self._mig__sanitize_verify_sql(
                self._mig__extract_sql(self._mig__call_llm(prompt), expected="select", key="verification_sql")
            )
        except Exception as exc:
            llm_error = str(exc)
            return {"ok": False, "map_id": map_id, "error": llm_error, "generation_source": generation_source}

        return {
            "ok": True,
            "map_id": map_id,
            "status": "VERIFY_SQL_GENERATED",
            "generation_source": generation_source,
            "llm_error": llm_error,
            "verify_sql": verify_sql,
        }

    # action="preview_*_prompt": LLM 호출 없이 최종 프롬프트를 확인한다.
    def _mig__preview_sql_prompt(self, map_id: Any, command: dict[str, Any], prompt_kind: str) -> dict[str, Any]:
        if map_id is None or str(map_id).strip() == "":
            raise ValueError("map_id is required")
        map_id = int(map_id)
        job = self._mig__load_job(map_id)
        if not job:
            return {"ok": False, "map_id": map_id, "error": "job not found"}
        details = self._mig__load_details(map_id)
        if prompt_kind == "mig":
            mig_sql_prompt = str(self.mig_sql_prompt or "").strip()
            if not mig_sql_prompt:
                raise ValueError("MIG SQL Prompt input is required for SQL generation")
            prompt = self._mig__render_sql_prompt(
                template=mig_sql_prompt,
                job=job,
                details=details,
                command=command,
            )
            action = "preview_mig_prompt"
        elif prompt_kind == "verify":
            verify_sql_prompt = str(self.verify_sql_prompt or "").strip()
            if not verify_sql_prompt:
                raise ValueError("VERIFY SQL Prompt input is required for SQL generation")
            prompt = self._mig__render_sql_prompt(
                template=verify_sql_prompt,
                job=job,
                details=details,
                command=command,
            )
            action = "preview_verify_prompt"
        else:
            return {"ok": False, "map_id": map_id, "error": f"Unsupported prompt_kind: {prompt_kind}"}

        source_context = self._mig__build_source_context(job)
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

    # action="reset": 재실행을 위해 상태값을 초기화한다.
    def _mig__reset(self, map_id: Any, command: dict[str, Any]) -> dict[str, Any]:
        if map_id is None or str(map_id).strip() == "":
            raise ValueError("map_id is required")
        map_id = int(map_id)
        if not self._mig__as_bool(command.get("confirm", False)):
            return {
                "ok": False,
                "map_id": map_id,
                "status": "CONFIRM_REQUIRED",
                "error": "reset requires confirm=true because it changes DB state.",
            }
        map_table = self._mig__qualify_table("NEXT_MIG_INFO", self.system_schema)
        sql = f"""
            UPDATE {map_table}
            SET STATUS = NULL,
                RETRY_COUNT = 0,
                BATCH_CNT = 0,
                UPD_TS = CURRENT_TIMESTAMP
            WHERE MAP_ID = :1
        """
        with self._mig__connect() as conn:
            cur = conn.cursor()
            cur.execute(sql, [map_id])
            rowcount = cur.rowcount
            conn.commit()
        self._mig__write_log(map_id, "RESET", "INFO", "RESET", "PASS", "Job reset. SQL values preserved.")
        return {"ok": rowcount > 0, "map_id": map_id, "updated_rows": rowcount}

    # action="save_user_sql": 사용자가 수정한 SQL을 저장한다.
    def _mig__save_user_sql(self, map_id: Any, command: dict[str, Any]) -> dict[str, Any]:
        if map_id is None or str(map_id).strip() == "":
            raise ValueError("map_id is required")
        map_id = int(map_id)
        if not self._mig__as_bool(command.get("confirm", False)):
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

        map_table = self._mig__qualify_table("NEXT_MIG_INFO", self.system_schema)
        with self._mig__connect() as conn:
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
        self._mig__write_log(map_id, "SAVE_USER_SQL", "INFO", "USER_SQL", "PASS", "User SQL saved", generate_sql=str(mig_sql))
        return {"ok": rowcount > 0, "map_id": map_id, "updated_rows": rowcount}

    # action="analyze_failure": 최근 실패 로그와 저장 SQL을 조회한다.
    def _mig__analyze_failure(self, map_id: Any) -> dict[str, Any]:
        if map_id is None or str(map_id).strip() == "":
            raise ValueError("map_id is required")
        map_id = int(map_id)
        log_table = self._mig__qualify_table("NEXT_MIG_LOG", self.system_schema)
        map_table = self._mig__qualify_table("NEXT_MIG_INFO", self.system_schema)
        with self._mig__connect() as conn:
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
                "log_type": self._mig__to_text(r[1]),
                "log_level": self._mig__to_text(r[2]),
                "step_name": self._mig__to_text(r[3]),
                "status": self._mig__to_text(r[4]),
                "message": self._mig__to_text(r[5]),
                "generate_sql": self._mig__to_text(r[6]),
                "log_time": self._mig__to_text(r[7]),
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
                "status": self._mig__to_text(job_row[0]),
                "mig_sql": self._mig__to_text(job_row[1]),
                "verify_sql": self._mig__to_text(job_row[2]),
                "retry_count": job_row[3],
                "elapsed_seconds": job_row[4],
                "upd_ts": self._mig__to_text(job_row[5]),
            },
            "latest_failure_log": latest_failure_log,
            "recent_logs": recent_logs,
        }

    # action="run_migration_job": SQL 생성, 실행, 검증까지 전체 마이그레이션 사이클을 수행한다.
    def _mig__run_migration_job(self, map_id: Any, command: dict[str, Any]) -> dict[str, Any]:

        if map_id is None or str(map_id).strip() == "":
            raise ValueError("map_id is required")
        map_id = int(map_id)

        started = time.perf_counter()
        max_attempts = max(1, int(command.get("max_attempts") or self.default_max_attempts or 1))

        job = self._mig__load_job(map_id)
        if not job:
            return {"ok": False, "map_id": map_id, "error": "job not found"}

        if str(job.get("use_yn") or "").upper() != "Y":
            return {"ok": False, "map_id": map_id, "status": "SKIP", "error": "USE_YN is not Y"}

        current_status = str(job.get("status") or "").strip().upper()
        if current_status == "PASS":
            return {"ok": True, "map_id": map_id, "status": "PASS", "message": "Job already passed"}
        if current_status:
            return {
                "ok": False,
                "map_id": map_id,
                "status": current_status,
                "error": "Full migration is allowed only when STATUS is NULL.",
            }

        dep = self._mig__check_dependencies(job)
        if not dep["ok"]:
            final_status = str(dep.get("status") or "WAITING")
            self._mig__write_log(map_id, "DEPENDENCY", "WARN", "DEP_CHECK", final_status, dep["message"])
            return {"ok": True, "map_id": map_id, "status": final_status, "message": dep["message"]}

        steps: list[dict[str, Any]] = []

        last_mig_sql = str(job.get("mig_sql") or "")
        last_verify_sql = str(job.get("verify_sql") or "")
        last_retry_count = 0

        try:
            self._raise_if_batch_stop_requested()
            job = self._mig__load_job(map_id) or job
            user_edited = str(job.get("user_edited") or "").upper() == "Y"

            last_failure: dict[str, Any] = {}
            mig_executed = False
            verify_sql_executed = False

            for attempt in range(1, max_attempts + 1):
                self._raise_if_batch_stop_requested()
                retry_count = attempt - 1
                last_retry_count = retry_count

                job = self._mig__load_job(map_id) or job
                user_edited = str(job.get("user_edited") or "").upper() == "Y"

                if not mig_executed:
                    self._raise_if_batch_stop_requested()
                    if user_edited:
                        mig_sql = str(job.get("mig_sql") or "").strip()
                        if not mig_sql:
                            raise ValueError("USER_EDITED=Y but MIG_SQL is empty")
                        last_mig_sql = mig_sql
                        steps.append({"step": "generate_mig_sql", "attempt": attempt, "status": "SKIPPED_USER_EDITED"})
                    else:
                        mig_command = {
                            "retry_count": retry_count,
                            "last_error": last_failure.get("error", ""),
                            "last_sql": last_mig_sql,
                        }
                        mig_result = self._mig__generate_mig_sql(map_id, mig_command)
                        self._raise_if_batch_stop_requested()
                        steps.append({"step": "generate_mig_sql", "attempt": attempt, **self._mig__summary_result(mig_result)})
                        if not mig_result.get("ok"):
                            last_failure = {"status": "FAIL-INSERT", "error": mig_result.get("error") or "MIG_SQL generation failed"}
                            self._mig__write_log(map_id, "ROW_ERROR", "WARN", "RETRY" if retry_count > 0 else "GENERATE_MIG_SQL", "FAIL-INSERT", str(last_failure["error"])[:3900], retry_count)
                            if attempt < max_attempts:
                                continue
                            break

                        last_mig_sql = str(mig_result.get("mig_sql") or "")
                        self._mig__write_log(
                            map_id,
                            "GENERATE_SQL",
                            "INFO",
                            "GENERATE_MIG_SQL",
                            "PASS",
                            "MIG_SQL generated",
                            retry_count,
                            last_mig_sql,
                        )

                    try:
                        self._raise_if_batch_stop_requested()
                        job = {**job, "mig_sql": last_mig_sql}
                        mig_sql = self._mig__sanitize_migration_sql(str(job.get("mig_sql") or ""))
                        if str(job.get("trunc_yn") or "").upper() == "Y":
                            self._mig__truncate_target(job)
                            self._mig__write_log(map_id, "EXECUTE_SQL", "INFO", "TRUNCATE", "PASS", "Target table truncated", retry_count)
                        affected_rows = self._mig__execute_sql_script(mig_sql)
                        if affected_rows <= 0:
                            raise ValueError("Migration SQL affected 0 rows")
                        mig_exec_result = {
                            "ok": True,
                            "map_id": map_id,
                            "status": "SUCCESS-MIG",
                            "message": "Migration SQL executed",
                            "affected_rows": affected_rows,
                            "mig_sql": mig_sql,
                        }
                        steps.append({"step": "execute_mig_sql", "attempt": attempt, **self._mig__summary_result(mig_exec_result)})
                        mig_executed = True
                        self._raise_if_batch_stop_requested()
                    except _BatchStopRequested:
                        raise
                    except Exception as exc:
                        last_failure = {"status": "FAIL-INSERT", "error": str(exc)}
                        steps.append({"step": "execute_mig_sql", "attempt": attempt, "ok": False, **last_failure})
                        self._mig__write_log(map_id, "ROW_ERROR", "WARN", "RETRY" if retry_count > 0 else "SQL_EXEC", "FAIL-INSERT", str(exc)[:3900], retry_count, str(job.get("mig_sql") or ""))
                        if attempt < max_attempts:
                            continue
                        break

                if not verify_sql_executed:
                    self._raise_if_batch_stop_requested()
                    job = self._mig__load_job(map_id) or job
                    user_edited = str(job.get("user_edited") or "").upper() == "Y"
                    verify_sql = str(job.get("verify_sql") or "").strip()

                    if user_edited and verify_sql:
                        last_verify_sql = verify_sql
                        steps.append({"step": "generate_verify_sql", "attempt": attempt, "status": "SKIPPED_USER_EDITED"})
                    else:
                        verify_command = {
                            "retry_count": retry_count,
                            "last_error": last_failure.get("error", ""),
                            "last_sql": last_verify_sql,
                        }
                        verify_result = self._mig__generate_verify_sql(map_id, verify_command)
                        self._raise_if_batch_stop_requested()
                        steps.append({"step": "generate_verify_sql", "attempt": attempt, **self._mig__summary_result(verify_result)})

                        if not verify_result.get("ok"):
                            last_failure = {"status": "FAIL-TEST", "error": verify_result.get("error") or "VERIFY_SQL generation failed"}
                            self._mig__write_log(map_id, "ROW_ERROR", "WARN", "RETRY" if retry_count > 0 else "GENERATE_VERIFY_SQL", "FAIL-TEST", str(last_failure["error"])[:3900], retry_count)
                            if attempt < max_attempts:
                                continue
                            break

                        last_verify_sql = str(verify_result.get("verify_sql") or "")
                        self._mig__write_log(
                            map_id,
                            "GENERATE_SQL",
                            "INFO",
                            "GENERATE_VERIFY_SQL",
                            "PASS",
                            "VERIFY_SQL generated",
                            retry_count,
                            last_verify_sql,
                        )

                    try:
                        self._raise_if_batch_stop_requested()
                        job = {**job, "verify_sql": last_verify_sql}
                        verify_sql = self._mig__sanitize_verify_sql(str(job.get("verify_sql") or ""))
                        verify_ok, verify_message, rows = self._mig__execute_verify_sql_with_rows(verify_sql)
                        verify_exec_result = {
                            "ok": verify_ok,
                            "map_id": map_id,
                            "status": "PASS" if verify_ok else "FAIL-TEST",
                            "message": verify_message,
                            "verify_sql": verify_sql,
                            "result_rows": rows,
                        }
                        steps.append({"step": "execute_verify_sql", "attempt": attempt, **self._mig__summary_result(verify_exec_result)})

                        if verify_exec_result.get("ok"):
                            verify_sql_executed = True
                            elapsed = int(time.perf_counter() - started)
                            self._mig__save_final_sql(map_id, last_mig_sql, last_verify_sql)
                            self._mig__update_job_status(map_id, "PASS", elapsed, retry_count)
                            self._mig__write_log(map_id, "VERIFY_SQL", "INFO", "VERIFY", "PASS", "Migration Success", retry_count, verify_exec_result.get("verify_sql"))
                            return {
                                "ok": True,
                                "map_id": map_id,
                                "status": "PASS",
                                "message": "Migration completed",
                                "elapsed_seconds": elapsed,
                                "retry_count": retry_count,
                                "steps": steps,
                            }

                        self._raise_if_batch_stop_requested()
                        last_failure = {"status": "FAIL-TEST", "error": verify_exec_result.get("message") or "Verification failed"}
                        self._mig__write_log(map_id, "ROW_ERROR", "WARN", "RETRY" if retry_count > 0 else "VERIFY", "FAIL-TEST", str(last_failure["error"])[:3900], retry_count, verify_exec_result.get("verify_sql"))
                        if attempt < max_attempts:
                            continue
                        break
                    except _BatchStopRequested:
                        raise
                    except Exception as exc:
                        last_failure = {"status": "FAIL-TEST", "error": str(exc)}
                        steps.append({"step": "execute_verify_sql", "attempt": attempt, "ok": False, **last_failure})
                        self._mig__write_log(map_id, "ROW_ERROR", "WARN", "RETRY" if retry_count > 0 else "VERIFY", "FAIL-TEST", str(exc)[:3900], retry_count, str(job.get("verify_sql") or ""))
                        if attempt < max_attempts:
                            continue
                        break

            final_status = str(last_failure.get("status") or "FAIL")
            elapsed = int(time.perf_counter() - started)

            self._mig__save_final_sql(map_id, last_mig_sql, last_verify_sql)
            self._mig__update_job_status(map_id, final_status, elapsed, last_retry_count)
            self._mig__write_log(
                map_id,
                "JOB_FAIL",
                "ERROR",
                "FINAL",
                final_status,
                str(last_failure.get("error") or "Max attempts reached")[:3900],
                last_retry_count,
                last_verify_sql if final_status == "FAIL-TEST" else last_mig_sql,
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
        except _BatchStopRequested as exc:
            elapsed = int(time.perf_counter() - started)
            self._mig__save_final_sql(map_id, last_mig_sql, last_verify_sql)
            self._mig__update_job_status(map_id, "STOPPED", elapsed, last_retry_count)
            self._mig__write_log(map_id, "JOB_STOPPED", "WARN", "STOP_REQUESTED", "STOPPED", str(exc)[:3900], last_retry_count, last_verify_sql or last_mig_sql)
            return {
                "ok": False,
                "map_id": map_id,
                "status": "STOPPED",
                "error": str(exc),
                "elapsed_seconds": elapsed,
                "retry_count": last_retry_count,
                "steps": steps,
            }
        except Exception as exc:
            elapsed = int(time.perf_counter() - started)
            self._mig__save_final_sql(map_id, last_mig_sql, last_verify_sql)
            self._mig__update_job_status(map_id, "FAIL", elapsed, int(job.get("retry_count") or 0))
            self._mig__write_log(map_id, "ROW_ERROR", "ERROR", "RUN_FULL", "FAIL", str(exc)[:3900])
            return {
                "ok": False,
                "map_id": map_id,
                "status": "FAIL",
                "error": str(exc),
                "elapsed_seconds": elapsed,
                "steps": steps,
            }

    # ======================================================================
    # 공통 코드
    # ======================================================================
    # command_json을 dict로 파싱한다.
    def _mig__parse_command(self) -> dict[str, Any]:
        raw = self.command_json
        if isinstance(raw, dict):
            return raw
        text = str(raw or "").strip()
        if not text:
            raise ValueError("command_json is required")
        return json.loads(text)

    # DB 입력값으로 Oracle SQLAlchemy connection string을 만든다.
    def _mig__connection_string(self) -> str:
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

    # 같은 DB 접속 정보는 SQLDatabase 인스턴스를 캐시해 재사용한다.
    def _mig__get_db(self):
        self._mig__ensure_runtime_dependencies()
        from langchain_community.utilities import SQLDatabase
        cache_key = "|".join(
            [
                str(self.db_host or "").strip(),
                str(self.db_port or 1521),
                str(self.db_service_name or "").strip(),
                str(self.db_username or "").strip(),
            ]
        )
        if cache_key not in self._db_cache:
            self._db_cache[cache_key] = SQLDatabase.from_uri(self._mig__connection_string())
        self.db = self._db_cache[cache_key]
        return self.db

    # DB 연결에 필요한 런타임 패키지를 확인한다.
    def _mig__ensure_runtime_dependencies(self) -> None:
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
        if not self._mig__as_bool(getattr(self, "auto_install_packages", False)):
            raise ModuleNotFoundError(
                "Missing packages: "
                + ", ".join(missing_packages)
                + ". Enable Auto Install Missing Packages or install them in the Langflow runtime."
            )
        for package in missing_packages:
            self._mig__pip_install(package)

    def _mig__pip_install(self, package: str) -> None:
        subprocess.check_call([sys.executable, "-m", "pip", "install", package])

    def _mig__post_json(self, url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        req_headers = {"Content-Type": "application/json", **headers}
        request = urllib.request.Request(url, data=body, headers=req_headers, method="POST")
        timeout_seconds = max(1, int(self.llm_timeout_seconds or 900))
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                    raw = response.read().decode("utf-8", errors="ignore")
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="ignore")[:1000]
                last_error = RuntimeError(f"HTTP {exc.code}: {detail}")
                if exc.code not in {429, 502, 503, 504} or attempt >= 3:
                    raise last_error from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                last_error = RuntimeError(f"LLM request failed: {exc}")
                if attempt >= 3:
                    raise last_error from exc
            time.sleep(min(8, 2 ** (attempt - 1)))
        raise last_error or RuntimeError("LLM request failed")

    def _mig__normalize_query_rows(self, raw: Any) -> list[dict[str, Any]]:
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
            return self._mig__normalize_query_rows(parsed)
        return [{"value": raw}]

    @contextmanager
    def _mig__connect(self):
        db = self._mig__get_db()
        engine = getattr(db, "_engine", None) or getattr(db, "engine", None)
        if engine is None:
            raise ValueError("SQLDatabase engine is not available")
        conn = engine.raw_connection()
        try:
            yield conn
        finally:
            conn.close()

    def _mig__render_sql_prompt(
        self,
        template: str,
        job: dict[str, Any],
        details: list[dict[str, Any]],
        command: dict[str, Any],
    ) -> str:
        source_context = self._mig__build_source_context(job)
        to_table = self._mig__qualify_table(job.get("to_table", ""), self.target_schema)
        from_table = source_context["from_table"]
        mapping_info = self._mig__format_mapping_info(details)
        ddl_info_block = self._mig__build_ddl_info_block(from_table, to_table)
        last_error = str(command.get("last_error") or "").strip()
        last_sql = str(command.get("last_sql") or "").strip()
        retry_context = self._mig__build_retry_context(last_error, last_sql, command.get("retry_count"))
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

    def _mig__build_retry_context(self, last_error: str, last_sql: str, retry_count: Any = None) -> str:
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

    def _mig__format_mapping_info(self, details: list[dict[str, Any]]) -> str:
        lines = []
        for detail in details:
            fr_col = str(detail.get("fr_col") or "").strip()
            to_col = str(detail.get("to_col") or "").strip()
            if to_col:
                lines.append(f"  - {fr_col} -> {to_col}")
            else:
                lines.append(f"  - {fr_col} -> <skip target column; source expression may be used only as part of another mapped expression>")
        return "\n".join(lines) if lines else "  - No mapping details"

    def _mig__build_ddl_info_block(self, from_table: str, to_table: str) -> str:
        blocks = ["[DDL information]"]
        for label, table_name in [("Source", from_table), ("Target", to_table)]:
            try:
                columns = self._mig__table_columns_for_prompt(table_name)
            except Exception as exc:
                columns = f"Unable to load columns: {exc}"
            blocks.append(f"- {label} {table_name}:\n{columns}")
        return "\n".join(blocks)

    def _mig__build_source_context(self, job: dict[str, Any]) -> dict[str, str]:
        map_type = str(job.get("map_type") or "").strip().upper()
        raw_source = str(job.get("fr_table") or "").strip()
        qualified_source = raw_source
        source_schema = str(self.source_schema or "").strip().upper()
        if source_schema:
            if not re.fullmatch(r"[A-Z][A-Z0-9_$#]*", source_schema):
                raise ValueError(f"Invalid source_schema: {source_schema}")
            join_parts = re.split(r"\b(?:(?:LEFT|RIGHT|FULL|INNER|CROSS)\s+(?:OUTER\s+)?)?JOIN\b", raw_source, flags=re.I)
            source_tables: list[str] = []
            for part in join_parts:
                before_on = re.split(r"\bON\b", part, flags=re.I)[0].strip()
                tokens = before_on.split()
                if tokens and tokens[0].upper() not in {"SELECT", "WITH", "FROM", "("}:
                    source_tables.append(tokens[0])
            for table in sorted(set(source_tables), key=len, reverse=True):
                if "." in table:
                    continue
                qualified_source = re.sub(rf"(?<![.\w]){re.escape(table)}(?![.\w])", f"{source_schema}.{table}", qualified_source)
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

    def _mig__table_columns_for_prompt(self, table_name: str) -> str:
        clean = str(table_name or "").strip()
        if not clean or any(token in clean.upper() for token in [" JOIN ", " SELECT ", " WITH "]):
            return "Complex source expression. Use mapping rules as the source of truth."
        schema = None
        table = clean
        if "." in clean:
            schema, table = clean.split(".", 1)
        meta = self._mig__get_table_ddl(table, schema)
        columns = meta.get("columns", [])
        if not columns:
            return "No columns found."
        return "\n".join(
            f"  - {col.get('column_name')} {col.get('data_type')}"
            + (f"({col.get('data_precision')},{col.get('data_scale')})" if col.get("data_precision") else f"({col.get('data_length')})")
            + f" nullable={col.get('nullable')}"
            for col in columns[:200]
        )

    def _mig__call_llm(self, prompt: str) -> str:
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
        data = self._mig__post_json(url, payload, {"Authorization": f"Bearer {api_key}"})
        return str(data["choices"][0]["message"].get("content", ""))

    def _mig__extract_sql(self, value: Any, expected: str, key: str | None = None) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("LLM returned empty SQL")
        fence = re.search(r"```(?:sql)?\s*(.*?)```", text, flags=re.I | re.S)
        if fence:
            text = fence.group(1).strip()
        if key:
            parsed = self._mig__parse_llm_json(text)
            text = str(parsed.get(key) or "").strip()
        text = text.rstrip(";").strip()
        first_word = text.split(None, 1)[0].upper() if text.split(None, 1) else ""
        allowed = {"insert": {"INSERT"}, "select": {"SELECT", "WITH"}}
        if first_word not in allowed.get(expected, set()):
            raise ValueError(f"Expected {expected.upper()} SQL but got: {first_word or text[:40]}")
        return text

    def _mig__parse_llm_json(self, text: str) -> dict[str, Any]:
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

    def _mig__sanitize_migration_sql(self, sql: str) -> str:
        cleaned = str(sql or "").strip().rstrip(";").strip()
        if not cleaned:
            raise ValueError("MIG_SQL is empty")
        upper = cleaned.upper()
        forbidden = ["TRUNCATE", "COMMIT", "ROLLBACK", "DELETE", "UPDATE", "MERGE", "DROP", "ALTER"]
        for token in forbidden:
            if re.search(rf"\b{token}\b", upper):
                raise ValueError(f"MIG_SQL must not contain {token}")
        statements = self._mig__split_sql_script(cleaned)
        if len(statements) != 1:
            raise ValueError("MIG_SQL must contain exactly one INSERT statement")
        statement = statements[0].strip().rstrip(";").strip()
        if not statement.upper().startswith("INSERT"):
            raise ValueError("MIG_SQL must start with INSERT")
        return statement

    def _mig__sanitize_verify_sql(self, sql: str) -> str:
        cleaned = str(sql or "").strip().rstrip(";").strip()
        if not cleaned:
            raise ValueError("VERIFY_SQL is empty")
        upper = cleaned.upper()
        forbidden = ["TRUNCATE", "COMMIT", "ROLLBACK", "INSERT", "DELETE", "UPDATE", "MERGE", "DROP", "ALTER"]
        for token in forbidden:
            if re.search(rf"\b{token}\b", upper):
                raise ValueError(f"VERIFY_SQL must not contain {token}")
        statements = self._mig__split_sql_script(cleaned)
        if len(statements) != 1:
            raise ValueError("VERIFY_SQL must contain exactly one SELECT statement")
        statement = statements[0].strip().rstrip(";").strip()
        first_word = statement.split(None, 1)[0].upper() if statement.split(None, 1) else ""
        if first_word not in {"SELECT", "WITH"}:
            raise ValueError("VERIFY_SQL must start with SELECT or WITH")
        return statement

    # NEXT_MIG_INFO에서 map_id에 해당하는 작업 row를 조회한다.
    def _mig__load_job(self, map_id: int) -> dict[str, Any] | None:
        map_table = self._mig__qualify_table("NEXT_MIG_INFO", self.system_schema)
        with self._mig__connect() as conn:
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
            "map_type": self._mig__to_text(row[1]),
            "fr_table": self._mig__to_text(row[2]),
            "to_table": self._mig__to_text(row[3]),
            "use_yn": self._mig__to_text(row[4]),
            "trunc_yn": self._mig__to_text(row[5]),
            "priority": row[6],
            "status": self._mig__to_text(row[7]),
            "user_edited": self._mig__to_text(row[8]),
            "prior_map_id": row[9],
            "condition": self._mig__to_text(row[10]),
            "mig_sql": self._mig__to_text(row[11]),
            "verify_sql": self._mig__to_text(row[12]),
            "batch_cnt": row[13],
            "elapsed_seconds": row[14],
            "retry_count": row[15],
            "created_at": self._mig__to_text(row[16]),
            "upd_ts": self._mig__to_text(row[17]),
        }

    # NEXT_MIG_INFO_DTL에서 컬럼 매핑 목록을 조회한다.
    def _mig__load_details(self, map_id: int) -> list[dict[str, Any]]:
        detail_table = self._mig__qualify_table("NEXT_MIG_INFO_DTL", self.system_schema)
        with self._mig__connect() as conn:
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
            {"map_dtl": r[0], "map_id": r[1], "fr_col": self._mig__to_text(r[2]), "to_col": self._mig__to_text(r[3])}
            for r in rows
        ]

    def _mig__check_dependencies(self, job: dict[str, Any]) -> dict[str, Any]:
        prior_map_id = job.get("prior_map_id")
        try:
            prior_map_id_int = int(prior_map_id) if prior_map_id is not None and str(prior_map_id).strip() else 0
        except (TypeError, ValueError):
            return {"ok": False, "status": "WAITING", "message": f"Invalid PRIOR_MAP_ID={prior_map_id}"}

        if prior_map_id_int > 0:
            prior = self._mig__load_job(prior_map_id_int)
            if not prior:
                return {"ok": False, "status": "WAITING", "message": f"Prior MAP_ID={prior_map_id} not found"}
            prior_status = str(prior.get("status") or "").upper()
            if prior_status != "PASS":
                return {
                    "ok": False,
                    "status": "WAITING",
                    "message": f"Prior MAP_ID={prior_map_id} status={prior_status or 'NULL'}",
                }

        target_dep = self._mig__check_same_target_priority_dependencies(job)
        if not target_dep["ok"]:
            return target_dep

        return {"ok": True, "message": "Dependencies passed"}

    def _mig__check_same_target_priority_dependencies(self, job: dict[str, Any]) -> dict[str, Any]:
        to_table = str(job.get("to_table") or "").strip()
        priority = job.get("priority")
        map_id = int(job.get("map_id") or 0)
        if not to_table or priority is None:
            return {"ok": True, "message": "No same-target priority dependency"}

        map_table = self._mig__qualify_table("NEXT_MIG_INFO", self.system_schema)
        with self._mig__connect() as conn:
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
            prior_status = str(self._mig__to_text(status) or "").strip().upper()
            if prior_status != "PASS":
                return {
                    "ok": False,
                    "status": "WAITING",
                    "message": f"Same target prior MAP_ID={prior_map_id} status={prior_status or 'NULL'}",
                }
        return {"ok": True, "message": "Same-target priority dependencies passed"}

    def _mig__save_final_sql(self, map_id: int, mig_sql: str, verify_sql: str) -> None:
        assignments = []
        params: list[Any] = []
        clean_mig_sql = str(mig_sql or "").strip()
        clean_verify_sql = str(verify_sql or "").strip()
        if clean_mig_sql:
            params.append(clean_mig_sql)
            assignments.append(f"MIG_SQL = :{len(params)}")
        if clean_verify_sql:
            params.append(clean_verify_sql)
            assignments.append(f"VERIFY_SQL = :{len(params)}")
        if not assignments:
            return

        params.append(map_id)
        map_table = self._mig__qualify_table("NEXT_MIG_INFO", self.system_schema)
        with self._mig__connect() as conn:
            cur = conn.cursor()
            cur.execute(
                f"""
                UPDATE {map_table}
                SET {", ".join(assignments)},
                    UPD_TS = CURRENT_TIMESTAMP
                WHERE MAP_ID = :{len(params)}
                """,
                params,
            )
            conn.commit()

    def _mig__summary_result(self, result: dict[str, Any]) -> dict[str, Any]:
        summary = {
            "ok": bool(result.get("ok")),
            "status": result.get("status"),
        }
        for key in ["message", "error", "generation_source", "affected_rows", "elapsed_seconds", "retry_count"]:
            if key in result:
                summary[key] = result.get(key)
        return summary

    def _mig__truncate_target(self, job: dict[str, Any]) -> None:
        target = self._mig__qualify_table(job["to_table"], self.target_schema)
        with self._mig__connect() as conn:
            cur = conn.cursor()
            cur.execute(f"TRUNCATE TABLE {target}")
            conn.commit()

    # MIG_SQL script를 statement 단위로 실행하고 처리 row 수를 합산한다.
    def _mig__execute_sql_script(self, sql_script: str) -> int:
        statements = self._mig__split_sql_script(sql_script)
        total_rowcount = 0
        with self._mig__connect() as conn:
            cur = conn.cursor()
            for stmt in statements:
                cleaned = stmt.strip().rstrip(";")
                if cleaned:
                    cur.execute(cleaned)
                    if cur.rowcount and cur.rowcount > 0:
                        total_rowcount += cur.rowcount
            conn.commit()
        return total_rowcount

    # VERIFY_SQL 결과의 모든 값이 0인지 확인한다.
    def _mig__execute_verify_sql_with_rows(self, verify_sql: str) -> tuple[bool, str, list[dict[str, Any]]]:
        statements = self._mig__split_sql_script(verify_sql)
        if not statements:
            return False, "verify_sql is empty", []
        last_rows = []
        columns = []
        with self._mig__connect() as conn:
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
            {str(columns[i] if i < len(columns) else i): self._mig__to_text(value) for i, value in enumerate(row)}
            for row in last_rows
        ]
        for row in last_rows:
            for value in row:
                text_value = self._mig__to_text(value).strip()
                if text_value == "":
                    return False, f"Mismatch found: {row}", result_rows
                try:
                    is_zero = Decimal(text_value) == Decimal("0")
                except (InvalidOperation, ValueError):
                    is_zero = text_value == "0"
                if not is_zero:
                    return False, f"Mismatch found: {row}", result_rows
        return True, "All Verification Passed", result_rows

    # 최종 상태, 소요시간, retry count, batch count를 NEXT_MIG_INFO에 저장한다.
    def _mig__update_job_status(self, map_id: int, status: str, elapsed_seconds: int, retry_count: int) -> None:
        map_table = self._mig__qualify_table("NEXT_MIG_INFO", self.system_schema)
        with self._mig__connect() as conn:
            cur = conn.cursor()
            cur.execute(
                f"""
                UPDATE {map_table}
                SET STATUS = :1,
                    ELAPSED_SECONDS = :2,
                    RETRY_COUNT = :3,
                    BATCH_CNT = NVL(BATCH_CNT, 0) + 1,
                    UPD_TS = CURRENT_TIMESTAMP
                WHERE MAP_ID = :4
                """,
                [status, elapsed_seconds, retry_count, map_id],
            )
            conn.commit()

    # 마이그레이션 단계 로그를 NEXT_MIG_LOG에 저장한다.
    def _mig__write_log(
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
        log_table = self._mig__qualify_table("NEXT_MIG_LOG", self.system_schema)
        seq = self._mig__qualify_table("MIGRATION_LOG_SEQ", self.system_schema)
        safe_message = str(message or "")[:4000]
        try:
            with self._mig__connect() as conn:
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
            pass

    def _mig__split_sql_script(self, sql_script: str) -> list[str]:
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

    def _mig__as_bool(self, value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        return str(value).strip().lower() in {"1", "true", "y", "yes", "on"}

    def _mig__qualify_table(self, table_name: str, schema: str | None) -> str:
        clean = str(table_name or "").strip()
        clean_schema = str(schema or "").strip().upper()
        if not clean:
            raise ValueError("table_name is empty")
        if "." in clean or not clean_schema:
            return clean
        if not re.fullmatch(r"[A-Z][A-Z0-9_$#]*", clean_schema):
            raise ValueError(f"Invalid schema: {clean_schema}")
        return f"{clean_schema}.{clean}"

    def _mig__to_text(self, value: Any) -> str:
        if value is None:
            return ""
        if hasattr(value, "read"):
            value = value.read()
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="ignore")
        return str(value)

    def _sql_run_command(self) -> Data:
        try:
            command = self._sql__parse_command()
            action = str(command.get("action") or "").strip().lower()
            space_nm = command.get("space_nm")
            sql_id = command.get("sql_id")
            last_error = command.get("last_error")
            to_sql = command.get("to_sql")
            bind_sql = command.get("bind_sql")
            bind_set = command.get("bind_set")

            if action == "test_connection":
                result = self._sql__test_connection()
            elif action == "status":
                result = self._sql__status(space_nm, sql_id)
            elif action == "list_pending":
                result = self._sql__list_pending(command.get("limit", 20))
            elif action == "generate_to_sql":
                result = self._sql__generate_to_sql(space_nm, sql_id, last_error)
            elif action == "generate_bind_sql":
                result = self._sql__generate_bind_sql(space_nm, sql_id, to_sql, last_error)
            elif action == "generate_test_sql":
                result = self._sql__generate_test_sql(space_nm, sql_id, to_sql, bind_sql, bind_set, last_error)
            elif action == "preview_to_sql_prompt":
                result = self._sql__preview_to_sql_prompt(space_nm, sql_id, last_error)
            elif action == "preview_bind_sql_prompt":
                result = self._sql__preview_bind_sql_prompt(space_nm, sql_id, to_sql, last_error)
            elif action == "preview_test_sql_prompt":
                result = self._sql__preview_test_sql_prompt(space_nm, sql_id, to_sql, bind_sql, bind_set, last_error)
            elif action == "run_sql_conversion_job":
                result = self._sql_run_sql_conversion_job(sql_id, space_nm, command)
            else:
                result = {"ok": False, "error": f"Unsupported action: {action}"}

            self.status = result
            return Data(data=result)

        except Exception as exc:
            result = {"ok": False, "error": str(exc)}
            self.status = result
            return Data(data=result)


    # action="test_connection": DB와 LLM 연결 상태를 확인한다.
    def _sql__test_connection(self) -> dict[str, Any]:
        try:
            result = self._sql__get_db().run("SELECT 1 AS OK FROM DUAL", include_columns=True)
            db_result = {"ok": True, "message": "DB connection OK", "result": result}
        except Exception as exc:
            db_result = {"ok": False, "message": "DB connection failed", "error": str(exc)}

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
                data = self._sql__post_json(url, payload, {"Authorization": f"Bearer {api_key}"})
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


    # action="status": 작업 상태를 조회한다.
    def _sql__status(self, space_nm: Any, sql_id: Any) -> dict[str, Any]:
        job = self._sql__load_job(space_nm, sql_id)
        return {"ok": bool(job), "job": job, "error": "" if job else "job not found"}


    # action="list_pending": 실행 가능한 대기 작업을 조회한다.
    def _sql__list_pending(self, limit: Any) -> dict[str, Any]:
        safe_limit = max(1, min(int(limit or 20), 100))
        table = self._sql__qualify_table("NEXT_SQL_INFO", self.system_schema)
        sql = f"""
                SELECT *
                FROM (
                    SELECT TAG_KIND, SPACE_NM, SQL_ID, STATUS_CONVERSION,
                           DBMS_LOB.SUBSTR(FR_SQL, 1000, 1) AS FR_SQL_PREVIEW,
                           DBMS_LOB.GETLENGTH(FR_SQL) AS FR_SQL_LEN,
                           DBMS_LOB.GETLENGTH(EDIT_FR_SQL) AS EDIT_FR_SQL_LEN,
                           PRIORITY, UPD_TS
                    FROM {table}
                    WHERE STATUS_CONVERSION IS NULL
                    ORDER BY PRIORITY ASC NULLS LAST, UPD_TS NULLS FIRST, SPACE_NM, SQL_ID
                )
                WHERE ROWNUM <= {safe_limit}
                """
        with self._sql__connect() as conn:
                    cur = conn.cursor()
                    cur.execute(sql)
                    rows = cur.fetchall()
        jobs = [
            {
                "tag_kind": self._sql__to_text(r[0]),
                "space_nm": self._sql__to_text(r[1]),
                "sql_id": self._sql__to_text(r[2]),
                "status_conversion": self._sql__to_text(r[3]),
                "fr_sql_preview": self._sql__to_text(r[4]),
                "fr_sql_len": r[5],
                "edit_fr_sql_len": r[6],
                "priority": r[7],
                "upd_ts": self._sql__to_text(r[8]),
            }
            for r in rows
        ]
        return {"ok": True, "count": len(jobs), "jobs": jobs}

    # action="get_table_ddl": Oracle 테이블 컬럼 메타데이터를 조회한다.

    # action="generate_to_sql": TO_SQL을 생성한다.
    def _sql__generate_to_sql(self, space_nm: Any, sql_id: Any, last_error: Any = None) -> dict[str, Any]:
        if not str(space_nm or "").strip() or not str(sql_id or "").strip():
            raise ValueError("space_nm and sql_id are required")
        job = self._sql__load_job(space_nm, sql_id)
        if not job:
            return {"ok": False, "error": "job not found"}

        user_edited = str(job.get("user_edited") or "").strip().upper() == "Y"
        existing_to_sql = str(job.get("to_sql") or "").strip()
        if user_edited:
            if existing_to_sql:
                return {
                    "ok": True,
                    "space_nm": space_nm,
                    "sql_id": sql_id,
                    "status": "TO_SQL_SKIPPED_USER_EDITED",
                    "message": "USER_EDITED=Y. Existing TO_SQL was preserved.",
                    "db_updated": False,
                    "to_sql": existing_to_sql,
                }
            return {"ok": False, "space_nm": space_nm, "sql_id": sql_id, "error": "USER_EDITED=Y but TO_SQL is empty"}

        # EDIT_FR_SQL이 있으면 원본 FR_SQL보다 우선 사용한다.
        edit_fr_sql = str(job.get("edit_fr_sql") or "").strip()
        fr_sql = str(job.get("fr_sql") or "").strip()
        source_sql = edit_fr_sql if edit_fr_sql else fr_sql
        if not source_sql:
            return {"ok": False, "space_nm": space_nm, "sql_id": sql_id, "error": "source SQL is empty"}

        mapping_schema_text, map_ids, fr_tables, rag_rule_count = self._sql__build_mapping_schema_text(job)
        prompt = self._sql__render_to_sql_prompt(
            from_sql=source_sql,
            mapping_schema_text=mapping_schema_text,
            source_schema=str(self.source_schema or "").strip() or "UNKNOWN",
            target_schema=str(self.target_schema or "").strip() or "UNKNOWN",
            last_error=str(last_error or "None"),
        )
        to_sql = self._sql__sanitize_to_sql(self._sql__call_llm(prompt))

        return {
            "ok": True,
            "space_nm": space_nm,
            "sql_id": sql_id,
            "status": "TO_SQL_GENERATED",
            "db_updated": False,
            "fr_tables": fr_tables,
            "map_ids": map_ids,
            "rag_rule_count": rag_rule_count,
            "to_sql": to_sql,
        }

    # action="generate_bind_sql": BIND_SQL을 생성한다.
    def _sql__generate_bind_sql(self, space_nm: Any, sql_id: Any, to_sql: Any = None, last_error: Any = None) -> dict[str, Any]:
        job = self._sql__load_job(space_nm, sql_id)
        if not job:
            return {"ok": False, "error": "job not found"}

        user_edited = str(job.get("user_edited") or "").strip().upper() == "Y"
        existing_bind_sql = str(job.get("bind_sql") or "").strip()
        if user_edited and existing_bind_sql:
            return {
                "ok": True,
                "space_nm": space_nm,
                "sql_id": sql_id,
                "status": "BIND_SQL_SKIPPED_USER_EDITED",
                "message": "USER_EDITED=Y. Existing BIND_SQL was preserved.",
                "db_updated": False,
                "bind_sql": existing_bind_sql,
            }

        final_to_sql = str(to_sql or job.get("to_sql") or "").strip()
        if not final_to_sql:
            return {"ok": False, "space_nm": space_nm, "sql_id": sql_id, "error": "TO_SQL is empty. Pass to_sql or save TO_SQL before generating BIND_SQL."}

        # EDIT_FR_SQL이 있으면 원본 FR_SQL보다 우선 사용한다.
        edit_fr_sql = str(job.get("edit_fr_sql") or "").strip()
        fr_sql = str(job.get("fr_sql") or "").strip()
        source_sql = edit_fr_sql if edit_fr_sql else fr_sql
        if not source_sql:
            return {"ok": False, "space_nm": space_nm, "sql_id": sql_id, "status": "FAIL-BIND", "error": "source SQL is empty"}

        mapping_schema_text, map_ids, fr_tables, rag_rule_count = self._sql__build_mapping_schema_text(job)
        try:
            prompt = self._sql__build_bind_sql_prompt(job, final_to_sql, mapping_schema_text, last_error)
            bind_sql = self._sql__sanitize_to_sql(self._sql__call_llm(prompt))
        except Exception as exc:
            return {"ok": False, "space_nm": space_nm, "sql_id": sql_id, "status": "FAIL-BIND", "error": str(exc), "db_updated": False}

        return {
            "ok": True,
            "space_nm": space_nm,
            "sql_id": sql_id,
            "status": "SUCCESS-BIND",
            "db_updated": False,
            "fr_tables": fr_tables,
            "map_ids": map_ids,
            "rag_rule_count": rag_rule_count,
            "bind_sql": bind_sql,
        }

    # action="generate_test_sql": TEST_SQL을 생성한다.
    def _sql__generate_test_sql(self, space_nm: Any, sql_id: Any, to_sql: Any = None, bind_sql: Any = None, bind_set: Any = None, last_error: Any = None) -> dict[str, Any]:
        job = self._sql__load_job(space_nm, sql_id)
        if not job:
            return {"ok": False, "error": "job not found"}

        user_edited = str(job.get("user_edited") or "").strip().upper() == "Y"
        existing_test_sql = str(job.get("test_sql") or "").strip()
        if user_edited and existing_test_sql:
            return {
                "ok": True,
                "space_nm": space_nm,
                "sql_id": sql_id,
                "status": "TEST_SQL_SKIPPED_USER_EDITED",
                "message": "USER_EDITED=Y. Existing TEST_SQL was preserved.",
                "db_updated": False,
                "test_sql": existing_test_sql,
            }

        final_to_sql = str(to_sql or job.get("to_sql") or "").strip()
        final_bind_sql = str(bind_sql or job.get("bind_sql") or "").strip()
        final_bind_set = str(bind_set or job.get("bind_set") or "").strip()
        if not final_to_sql:
            return {"ok": False, "space_nm": space_nm, "sql_id": sql_id, "error": "TO_SQL is empty. Pass to_sql or save TO_SQL before generating TEST_SQL."}
        if not final_bind_set:
            return {"ok": False, "space_nm": space_nm, "sql_id": sql_id, "error": "BIND_SET is empty. Pass bind_set or run BIND_SQL before generating TEST_SQL."}

        # EDIT_FR_SQL이 있으면 원본 FR_SQL보다 우선 사용한다.
        edit_fr_sql = str(job.get("edit_fr_sql") or "").strip()
        fr_sql = str(job.get("fr_sql") or "").strip()
        source_sql = edit_fr_sql if edit_fr_sql else fr_sql
        if not source_sql:
            return {"ok": False, "space_nm": space_nm, "sql_id": sql_id, "status": "FAIL-TEST", "error": "source SQL is empty"}

        mapping_schema_text, map_ids, fr_tables, rag_rule_count = self._sql__build_mapping_schema_text(job)
        try:
            prompt = self._sql__build_test_sql_prompt(job, final_to_sql, final_bind_sql, final_bind_set, mapping_schema_text, last_error)
            test_sql = self._sql__sanitize_to_sql(self._sql__call_llm(prompt))
        except Exception as exc:
            return {"ok": False, "space_nm": space_nm, "sql_id": sql_id, "status": "FAIL-TEST", "error": str(exc), "db_updated": False}

        return {
            "ok": True,
            "space_nm": space_nm,
            "sql_id": sql_id,
            "status": "TEST_SQL_GENERATED",
            "db_updated": False,
            "fr_tables": fr_tables,
            "map_ids": map_ids,
            "rag_rule_count": rag_rule_count,
            "test_sql": test_sql,
        }

    # action="preview_*_prompt": LLM 호출 없이 최종 프롬프트를 확인한다.
    def _sql__preview_to_sql_prompt(self, space_nm: Any, sql_id: Any, last_error: Any = None) -> dict[str, Any]:

        job = self._sql__load_job(space_nm, sql_id)
        if not job:
            return {"ok": False, "error": "job not found"}


        # EDIT_FR_SQL이 있으면 원본 FR_SQL보다 우선 사용한다.
        edit_fr_sql = str(job.get("edit_fr_sql") or "").strip()
        fr_sql = str(job.get("fr_sql") or "").strip()
        source_sql = edit_fr_sql if edit_fr_sql else fr_sql
        if not source_sql:
            return {"ok": False, "space_nm": space_nm, "sql_id": sql_id, "error": "source SQL is empty"}


        mapping_schema_text, map_ids, fr_tables, rag_rule_count = self._sql__build_mapping_schema_text(job)
        prompt = self._sql__render_to_sql_prompt(
            from_sql=source_sql,
            mapping_schema_text=mapping_schema_text,
            source_schema=str(self.source_schema or "").strip() or "UNKNOWN",
            target_schema=str(self.target_schema or "").strip() or "UNKNOWN",
            last_error=str(last_error or "None"),
        )
        return {
            "ok": True,
            "action": "preview_to_sql_prompt",
            "space_nm": space_nm,
            "sql_id": sql_id,
            "prompt_kind": "to_sql",
            "prompt_length": len(prompt),
            "prompt": prompt,
            "db_updated": False,
            "llm_called": False,
            "fr_tables": fr_tables,
            "map_ids": map_ids,
            "rag_rule_count": rag_rule_count,
        }

    # action="preview_*_prompt": LLM 호출 없이 최종 프롬프트를 확인한다.
    def _sql__preview_bind_sql_prompt(self, space_nm: Any, sql_id: Any, to_sql: Any = None, last_error: Any = None) -> dict[str, Any]:
        job = self._sql__load_job(space_nm, sql_id)
        if not job:
            return {"ok": False, "error": "job not found"}

        final_to_sql = str(to_sql or job.get("to_sql") or "").strip()
        if not final_to_sql:
            return {"ok": False, "space_nm": space_nm, "sql_id": sql_id, "error": "TO_SQL is empty. Pass to_sql or save TO_SQL before previewing BIND prompt."}

        mapping_schema_text, map_ids, fr_tables, rag_rule_count = self._sql__build_mapping_schema_text(job)
        prompt = self._sql__build_bind_sql_prompt(job, final_to_sql, mapping_schema_text, last_error)
        return {"ok": True, "action": "preview_bind_sql_prompt", "space_nm": space_nm, "sql_id": sql_id, "prompt_kind": "bind", "prompt_length": len(prompt), "prompt": prompt, "db_updated": False, "llm_called": False, "fr_tables": fr_tables, "map_ids": map_ids, "rag_rule_count": rag_rule_count}

    # action="preview_*_prompt": LLM 호출 없이 최종 프롬프트를 확인한다.
    def _sql__preview_test_sql_prompt(self, space_nm: Any, sql_id: Any, to_sql: Any = None, bind_sql: Any = None, bind_set: Any = None, last_error: Any = None) -> dict[str, Any]:
        job = self._sql__load_job(space_nm, sql_id)
        if not job:
            return {"ok": False, "error": "job not found"}

        final_to_sql = str(to_sql or job.get("to_sql") or "").strip()
        final_bind_sql = str(bind_sql or job.get("bind_sql") or "").strip()
        final_bind_set = str(bind_set or job.get("bind_set") or "").strip()
        if not final_to_sql:
            return {"ok": False, "space_nm": space_nm, "sql_id": sql_id, "error": "TO_SQL is empty. Pass to_sql or save TO_SQL before previewing TEST prompt."}
        if not final_bind_set:
            return {"ok": False, "space_nm": space_nm, "sql_id": sql_id, "error": "BIND_SET is empty. Pass bind_set or run BIND_SQL before previewing TEST prompt."}

        mapping_schema_text, map_ids, fr_tables, rag_rule_count = self._sql__build_mapping_schema_text(job)
        prompt = self._sql__build_test_sql_prompt(job, final_to_sql, final_bind_sql, final_bind_set, mapping_schema_text, last_error)
        return {"ok": True, "action": "preview_test_sql_prompt", "space_nm": space_nm, "sql_id": sql_id, "prompt_kind": "test", "prompt_length": len(prompt), "prompt": prompt, "db_updated": False, "llm_called": False, "fr_tables": fr_tables, "map_ids": map_ids, "rag_rule_count": rag_rule_count}


    # action="run_sql_conversion_job": TO_SQL, BIND_SQL, TEST_SQL 생성과 검증을 수행한다.
    def _sql_run_sql_conversion_job(self, sql_id: str, space_nm: str, command: dict[str, Any]) -> dict[str, Any]:

        if (sql_id is None or str(sql_id).strip() == "") or (space_nm is None or str(space_nm).strip() == ""):
            return {"ok": False, "error": "sql_id and space_nm are required for run_sql_conversion_job"}
        sql_id = str(sql_id or "").strip()
        space_nm = str(space_nm or "").strip()

        started = time.perf_counter()
        max_attempts = max(1, int(command.get("max_attempts") or 3))

        job = self._sql__load_job(space_nm, sql_id)
        if not job:
            return {"ok": False, "space_nm": space_nm, "sql_id": sql_id, "error": "job not found"}

        current_status = str(job.get("status_conversion") or "").strip().upper()
        if current_status:
            return {"ok": False, "space_nm": space_nm, "sql_id": sql_id, "status": current_status, "error": "run_sql_conversion_job is allowed only when STATUS_CONVERSION is NULL."}

        steps: list[dict[str, Any]] = []
        last_to_sql = str(job.get("to_sql") or "")
        last_bind_sql = str(job.get("bind_sql") or "")
        last_bind_set = str(job.get("bind_set") or "")
        last_test_sql = str(job.get("test_sql") or "")
        last_retry_count = 0

        try:
            self._raise_if_batch_stop_requested()
            mapping_schema_text, map_ids, fr_tables, rag_rule_count = self._sql__build_mapping_schema_text(job)
            last_failure: dict[str, Any] = {}
            to_sql_executed = False
            bind_sql_executed = False
            test_sql_executed = False
            for attempt in range(1, max_attempts + 1):
                self._raise_if_batch_stop_requested()
                retry_count = attempt - 1
                last_retry_count = retry_count
                job = self._sql__load_job(space_nm, sql_id) or job
                user_edited = str(job.get("user_edited") or "").strip().upper() == "Y"
                tag_kind = str(job.get("tag_kind") or "").strip().upper()

                if not to_sql_executed:
                    self._raise_if_batch_stop_requested()
                    if user_edited:
                        to_sql = str(job.get("to_sql") or "").strip()
                        if not to_sql:
                            raise ValueError("USER_EDITED=Y but TO_SQL is empty")
                        last_to_sql = to_sql
                        steps.append({"step": "generate_to_sql", "attempt": attempt, "status": "SUCCESS-TOBE", "message": "USER_EDITED=Y. Existing TO_SQL was used."})
                    else:
                        try:
                            to_sql_result = self._sql__generate_to_sql(space_nm, sql_id, last_error=last_failure.get("error", ""))
                            self._raise_if_batch_stop_requested()
                        except _BatchStopRequested:
                            raise
                        except Exception as exc:
                            to_sql_result = {"ok": False, "space_nm": space_nm, "sql_id": sql_id, "status": "FAIL-TOBE", "error": str(exc), "db_updated": False}
                        if to_sql_result.get("ok"):
                            to_sql_result["status"] = "SUCCESS-TOBE"
                        steps.append({"step": "generate_to_sql", "attempt": attempt, **self._sql__summary_result(to_sql_result)})
                        if not to_sql_result.get("ok"):
                            last_failure = {"status": "FAIL-TOBE", "error": to_sql_result.get("error") or "TO_SQL generation failed"}
                            self._sql__write_log(sql_id, space_nm, "TO_SQL", "FAIL", "GENERATE_TO_SQL", str(last_failure["error"])[:3900], retry_count, last_to_sql, int(time.perf_counter() - started), "TO_SQL_PROMPT")
                            if attempt < max_attempts:
                                continue
                            break
                        last_to_sql = str(to_sql_result.get("to_sql") or "").strip()
                    to_sql_executed = True
                    self._sql__write_log(sql_id, space_nm, "TO_SQL", "PASS", "GENERATE_TO_SQL", "TO_SQL generated", retry_count, last_to_sql, int(time.perf_counter() - started), "TO_SQL_PROMPT")

                if tag_kind != "SELECT":
                    self._raise_if_batch_stop_requested()
                    elapsed = int(time.perf_counter() - started)
                    self._sql__save_final_sql(sql_id, space_nm, last_to_sql, last_bind_sql, last_bind_set, last_test_sql)
                    self._sql__update_job_status(sql_id, space_nm, "PASS-CONVERSION", elapsed, retry_count, status_tuning="READY")
                    self._sql__write_log(sql_id, space_nm, "TO_SQL", "PASS", "FINAL", "SQL Conversion completed without BIND/TEST because TAG_KIND is not SELECT", retry_count, last_to_sql, elapsed)
                    return {"ok": True, "space_nm": space_nm, "sql_id": sql_id, "status": "PASS-CONVERSION", "status_tuning": "READY", "elapsed_seconds": elapsed, "retry_count": retry_count, "steps": steps, "to_sql": last_to_sql, "map_ids": map_ids, "fr_tables": fr_tables, "rag_rule_count": rag_rule_count}

                if not bind_sql_executed:
                    try:
                        self._raise_if_batch_stop_requested()
                        bind_result = self._sql__generate_bind_sql(space_nm, sql_id, last_to_sql, last_failure.get("error", ""))
                        self._raise_if_batch_stop_requested()
                        steps.append({"step": "generate_bind_sql", "attempt": attempt, **self._sql__summary_result(bind_result)})
                        if not bind_result.get("ok"):
                            last_failure = {"status": "FAIL-BIND", "error": bind_result.get("error") or "BIND_SQL generation failed"}
                            self._sql__write_log(sql_id, space_nm, "BIND_SQL", "FAIL", "GENERATE_BIND_SQL", str(last_failure["error"])[:3900], retry_count, last_bind_sql, int(time.perf_counter() - started), "BIND_SQL_PROMPT")
                            if attempt < max_attempts:
                                continue
                            break
                        last_bind_sql = str(bind_result.get("bind_sql") or "").strip()
                        self._sql__write_log(sql_id, space_nm, "BIND_SQL", "PASS", "GENERATE_BIND_SQL", "BIND_SQL generated", retry_count, last_bind_sql, int(time.perf_counter() - started), "BIND_SQL_PROMPT")
                        existing_bind_set = str(job.get("bind_set") or "").strip()
                        if bind_result.get("status") == "BIND_SQL_SKIPPED_USER_EDITED" and existing_bind_set:
                            last_bind_set = existing_bind_set
                            bind_sql_executed = True
                            steps.append({"step": "execute_bind_sql", "attempt": attempt, "ok": True, "status": "BIND_SET_SKIPPED_USER_EDITED", "message": "USER_EDITED=Y. Existing BIND_SET was used."})
                        else:
                            clean_bind_sql = self._sql__prepare_runtime_sql(last_bind_sql, "EXECUTE_BIND_SQL")
                            if not clean_bind_sql:
                                raise ValueError("BIND_SQL is empty")
                            with self._sql__connect() as conn:
                                cur = conn.cursor()
                                cur.execute(clean_bind_sql)
                                columns = [desc[0] for desc in cur.description] if cur.description else []
                                rows = cur.fetchmany(20)
                            result_rows = [{str(columns[i] if i < len(columns) else i): self._sql__json_value(value) for i, value in enumerate(row)} for row in rows]
                            last_bind_set = json.dumps(result_rows, ensure_ascii=False)
                            bind_exec_result = {"ok": True, "status": "SUCCESS-BIND", "row_count": len(result_rows), "bind_set": last_bind_set}
                            steps.append({"step": "execute_bind_sql", "attempt": attempt, **self._sql__summary_result(bind_exec_result)})
                            bind_sql_executed = True
                            self._sql__write_log(sql_id, space_nm, "BIND_SET", "PASS", "EXECUTE_BIND_SQL", "BIND_SQL executed", retry_count, last_bind_set, int(time.perf_counter() - started))
                            self._raise_if_batch_stop_requested()
                    except _BatchStopRequested:
                        raise
                    except Exception as exc:
                        last_failure = {"status": "FAIL-BIND", "error": str(exc)}
                        steps.append({"step": "execute_bind_sql", "attempt": attempt, "ok": False, **last_failure})
                        self._sql__write_log(sql_id, space_nm, "BIND_SQL", "FAIL", "EXECUTE_BIND_SQL", str(exc)[:3900], retry_count, last_bind_sql, int(time.perf_counter() - started))
                        if attempt < max_attempts:
                            continue
                        break

                if not test_sql_executed:
                    try:
                        self._raise_if_batch_stop_requested()
                        test_result = self._sql__generate_test_sql(space_nm, sql_id, last_to_sql, last_bind_sql, last_bind_set, last_failure.get("error", ""))
                        self._raise_if_batch_stop_requested()
                        steps.append({"step": "generate_test_sql", "attempt": attempt, **self._sql__summary_result(test_result)})
                        if not test_result.get("ok"):
                            last_failure = {"status": "FAIL-TEST", "error": test_result.get("error") or "TEST_SQL generation failed"}
                            self._sql__write_log(sql_id, space_nm, "TEST_SQL", "FAIL", "GENERATE_TEST_SQL", str(last_failure["error"])[:3900], retry_count, last_test_sql, int(time.perf_counter() - started), "TEST_SQL_PROMPT")
                            if attempt < max_attempts:
                                continue
                            break
                        last_test_sql = str(test_result.get("test_sql") or "").strip()
                        self._sql__write_log(sql_id, space_nm, "TEST_SQL", "PASS", "GENERATE_TEST_SQL", "TEST_SQL generated", retry_count, last_test_sql, int(time.perf_counter() - started), "TEST_SQL_PROMPT")

                        clean_test_sql = self._sql__prepare_runtime_sql(last_test_sql, "EXECUTE_TEST_SQL")
                        if not clean_test_sql:
                            raise ValueError("TEST_SQL is empty")
                        with self._sql__connect() as conn:
                            cur = conn.cursor()
                            cur.execute(clean_test_sql)
                            columns = [desc[0] for desc in cur.description] if cur.description else []
                            rows = cur.fetchall()
                        result_rows = [{str(columns[i] if i < len(columns) else i): self._sql__json_value(value) for i, value in enumerate(row)} for row in rows]
                        self._raise_if_batch_stop_requested()
                        if not result_rows:
                            test_exec_result = {"ok": False, "status": "FAIL-TEST", "message": "TEST_SQL returned no rows", "result_rows": result_rows}
                        else:
                            sample_keys = {str(key).lower() for key in result_rows[0].keys()}
                            if not {"case_no", "from_count", "to_count"}.issubset(sample_keys):
                                test_exec_result = {"ok": False, "status": "FAIL-TEST", "message": f"TEST_SQL must return CASE_NO, FROM_COUNT, TO_COUNT. Actual columns: {sorted(sample_keys)}", "result_rows": result_rows}
                            else:
                                test_exec_result = {"ok": True, "status": "PASS-CONVERSION", "message": "All test counts matched", "result_rows": result_rows}
                                for row in result_rows:
                                    from_count = self._sql__get_row_value(row, "FROM_COUNT")
                                    to_count = self._sql__get_row_value(row, "TO_COUNT")
                                    if str(from_count).strip() != str(to_count).strip():
                                        test_exec_result = {"ok": False, "status": "FAIL-TEST", "message": f"Count mismatch: {row}", "result_rows": result_rows}
                                        break
                        steps.append({"step": "execute_test_sql", "attempt": attempt, **self._sql__summary_result(test_exec_result)})
                        if test_exec_result.get("ok"):
                            test_sql_executed = True
                            elapsed = int(time.perf_counter() - started)
                            self._sql__save_final_sql(sql_id, space_nm, last_to_sql, last_bind_sql, last_bind_set, last_test_sql)
                            self._sql__update_job_status(sql_id, space_nm, "PASS-CONVERSION", elapsed, retry_count, status_tuning="READY")
                            self._sql__write_log(sql_id, space_nm, "TEST_SQL", "PASS", "EXECUTE_TEST_SQL", "SQL Conversion test passed", retry_count, last_test_sql, elapsed)
                            return {"ok": True, "space_nm": space_nm, "sql_id": sql_id, "status": "PASS-CONVERSION", "status_tuning": "READY", "elapsed_seconds": elapsed, "retry_count": retry_count, "steps": steps, "to_sql": last_to_sql, "bind_sql": last_bind_sql, "bind_set": last_bind_set, "test_sql": last_test_sql, "test_rows": test_exec_result.get("result_rows"), "map_ids": map_ids, "fr_tables": fr_tables, "rag_rule_count": rag_rule_count}
                        last_failure = {"status": "FAIL-TEST", "error": test_exec_result.get("message") or "TEST_SQL validation failed"}
                        self._sql__write_log(sql_id, space_nm, "TEST_SQL", "FAIL", "EXECUTE_TEST_SQL", str(last_failure["error"])[:3900], retry_count, last_test_sql, int(time.perf_counter() - started))
                        if attempt < max_attempts:
                            continue
                        break
                    except _BatchStopRequested:
                        raise
                    except Exception as exc:
                        last_failure = {"status": "FAIL-TEST", "error": str(exc)}
                        steps.append({"step": "execute_test_sql", "attempt": attempt, "ok": False, **last_failure})
                        self._sql__write_log(sql_id, space_nm, "TEST_SQL", "FAIL", "EXECUTE_TEST_SQL", str(exc)[:3900], retry_count, last_test_sql, int(time.perf_counter() - started))
                        if attempt < max_attempts:
                            continue
                        break

            final_status = str(last_failure.get("status") or self._sql__fallback_conversion_failure_status(last_to_sql, last_bind_sql, last_bind_set, last_test_sql))
            elapsed = int(time.perf_counter() - started)
            self._sql__save_final_sql(sql_id, space_nm, last_to_sql, last_bind_sql, last_bind_set, last_test_sql)
            self._sql__update_job_status(sql_id, space_nm, final_status, elapsed, last_retry_count)
            self._sql__write_log(sql_id, space_nm, "ERROR", "FAIL", "FINAL", str(last_failure.get("error") or "Max attempts reached")[:3900], last_retry_count, last_test_sql or last_bind_sql or last_to_sql, elapsed)
            return {"ok": False, "space_nm": space_nm, "sql_id": sql_id, "status": final_status, "error": last_failure.get("error") or "Max attempts reached", "elapsed_seconds": elapsed, "retry_count": last_retry_count, "steps": steps}
        except _BatchStopRequested as exc:
            elapsed = int(time.perf_counter() - started)
            self._sql__save_final_sql(sql_id, space_nm, last_to_sql, last_bind_sql, last_bind_set, last_test_sql)
            self._sql__update_job_status(sql_id, space_nm, "STOPPED", elapsed, last_retry_count)
            self._sql__write_log(sql_id, space_nm, "ERROR", "WARN", "STOP_REQUESTED", str(exc)[:3900], last_retry_count, last_test_sql or last_bind_sql or last_to_sql, elapsed)
            return {"ok": False, "space_nm": space_nm, "sql_id": sql_id, "status": "STOPPED", "error": str(exc), "elapsed_seconds": elapsed, "retry_count": last_retry_count, "steps": steps}
        except Exception as exc:
            elapsed = int(time.perf_counter() - started)
            final_status = self._sql__fallback_conversion_failure_status(last_to_sql, last_bind_sql, last_bind_set, last_test_sql)
            self._sql__save_final_sql(sql_id, space_nm, last_to_sql, last_bind_sql, last_bind_set, last_test_sql)
            self._sql__update_job_status(sql_id, space_nm, final_status, elapsed, last_retry_count)
            self._sql__write_log(sql_id, space_nm, "ERROR", "FAIL", "RUN_FULL", str(exc)[:3900], last_retry_count, last_test_sql or last_bind_sql or last_to_sql, elapsed)
            return {"ok": False, "space_nm": space_nm, "sql_id": sql_id, "status": final_status, "error": str(exc), "elapsed_seconds": elapsed, "retry_count": last_retry_count, "steps": steps}

    # ======================================================================
    # 공통 코드
    # ======================================================================
    def _sql__parse_command(self) -> dict[str, Any]:


        raw = self.command_json
        if isinstance(raw, dict):
            return raw
        text = str(raw or "").strip()
        if not text:
            raise ValueError("command_json is required")
        return json.loads(text)

    # DB 입력값으로 Oracle SQLAlchemy connection string을 만든다.
    def _sql__connection_string(self) -> str:
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

    # 같은 DB 접속 정보는 SQLDatabase 인스턴스를 캐시해 재사용한다.
    def _sql__get_db(self):
        self._sql__ensure_runtime_dependencies()
        from langchain_community.utilities import SQLDatabase

        cache_key = "|".join(
            [
                str(self.db_host or "").strip(),
                str(self.db_port or 1521),
                str(self.db_service_name or "").strip(),
                str(self.db_username or "").strip(),
            ]
        )
        if cache_key not in self._db_cache:
            self._db_cache[cache_key] = SQLDatabase.from_uri(self._sql__connection_string())
        self.db = self._db_cache[cache_key]
        return self.db

    # DB 연결에 필요한 런타임 패키지를 확인한다.
    def _sql__ensure_runtime_dependencies(self) -> None:
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
        if not self._sql__as_bool(getattr(self, "auto_install_packages", False)):
            raise ModuleNotFoundError(
                "Missing packages: "
                + ", ".join(missing_packages)
                + ". Enable Auto Install Missing Packages or install them in the Langflow runtime."
            )
        for package in missing_packages:
            self._sql__pip_install(package)

    def _sql__pip_install(self, package: str) -> None:
        subprocess.check_call([sys.executable, "-m", "pip", "install", package])

    def _sql__post_json(self, url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        req_headers = {"Content-Type": "application/json", **headers}
        request = urllib.request.Request(url, data=body, headers=req_headers, method="POST")
        timeout_seconds = max(1, int(self.llm_timeout_seconds or 900))
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                    raw = response.read().decode("utf-8", errors="ignore")
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="ignore")[:1000]
                last_error = RuntimeError(f"HTTP {exc.code}: {detail}")
                if exc.code not in {429, 502, 503, 504} or attempt >= 3:
                    raise last_error from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                last_error = RuntimeError(f"LLM request failed: {exc}")
                if attempt >= 3:
                    raise last_error from exc
            time.sleep(min(8, 2 ** (attempt - 1)))
        raise last_error or RuntimeError("LLM request failed")

    @contextmanager
    def _sql__connect(self):
        db = self._sql__get_db()
        with db._engine.connect() as conn:
            raw = conn.connection
            yield raw

    # NEXT_SQL_INFO에서 space_nm/sql_id에 해당하는 작업 row를 조회한다.
    def _sql__load_job(self, space_nm: Any, sql_id: Any) -> dict[str, Any] | None:
        table = self._sql__qualify_table("NEXT_SQL_INFO", self.system_schema)
        space_nm = str(space_nm or "").strip()
        sql_id = str(sql_id or "").strip()
        if not space_nm or not sql_id:
            raise ValueError("space_nm and sql_id are required")

        with self._sql__connect() as conn:
            cur = conn.cursor()
            cur.execute(
                f"""
                SELECT TAG_KIND, SPACE_NM, SQL_ID, FR_SQL, EDIT_FR_SQL,
                       TARGET_TABLE, TO_SQL, STATUS_CONVERSION, LOG,
                       TUNED_FR_SQL, TUNED_TO_SQL, SQL_LENGTH, MAP_TYPE,
                       PRIORITY, BATCH_CNT, UPD_TS, USER_EDITED,
                       BIND_SQL, BIND_SET, TEST_SQL, STATUS_TUNING, RETRY_COUNT
                FROM {table}
                WHERE SPACE_NM = :1
                  AND SQL_ID = :2
                """,
                [space_nm, sql_id],
            )
            row = cur.fetchone()
        if not row:
            return None
        return {
            "tag_kind": self._sql__to_text(row[0]),
            "space_nm": self._sql__to_text(row[1]),
            "sql_id": self._sql__to_text(row[2]),
            "fr_sql": self._sql__to_text(row[3]),
            "edit_fr_sql": self._sql__to_text(row[4]),
            "target_table": self._sql__to_text(row[5]),
            "to_sql": self._sql__to_text(row[6]),
            "status_conversion": self._sql__to_text(row[7]),
            "log": self._sql__to_text(row[8]),
            "tuned_fr_sql": self._sql__to_text(row[9]),
            "tuned_to_sql": self._sql__to_text(row[10]),
            "sql_length": self._sql__to_text(row[11]),
            "map_type": self._sql__to_text(row[12]),
            "priority": row[13],
            "batch_cnt": row[14],
            "upd_ts": self._sql__to_text(row[15]),
            "user_edited": self._sql__to_text(row[16]),
            "bind_sql": self._sql__to_text(row[17]),
            "bind_set": self._sql__to_text(row[18]),
            "test_sql": self._sql__to_text(row[19]),
            "status_tuning": self._sql__to_text(row[20]),
            "retry_count": row[21],
        }


    # TARGET_TABLE의 FR_TABLE 기준으로 mapping/RAG 정보를 구성한다.
    def _sql__build_mapping_schema_text(self, job: dict[str, Any]) -> tuple[str, list[int], list[str], int]:

        fr_tables = self._sql__extract_target_fr_tables(job.get("target_table"))
        if not fr_tables:
            sections = [
                "[TARGET_TABLE_FR_TABLE_HINTS]",
                "  - No FR_TABLE hints found.",
                "\n[MIGRATION_MAP_IDS]",
                "  - No MAP_ID found because TARGET_TABLE is empty.",
                "\n[MIGRATION_MAPPING_RULES]",
                "  - No mapping rules found because TARGET_TABLE is empty.",
                "\n[UNMAPPED_FR_TABLES]",
                "  - None.",
                "\n[SQL_CONVERSION_RAG_GUIDANCE]",
                "  - No FR_TABLE hints for SQL_CONVERSION RAG lookup.",
            ]
            return "\n".join(sections), [], [], 0

        normalized_fr_tables = {self._sql__normalize_table_name(name) for name in fr_tables if self._sql__normalize_table_name(name)}

        sections = ["[TARGET_TABLE_FR_TABLE_HINTS]"]
        for table_name in fr_tables:
            sections.append(f"  - {table_name}")

        sections.append("\n[MIGRATION_MAP_IDS]")
        map_ids: list[int] = []
        table = self._sql__qualify_table("NEXT_MIG_INFO", self.system_schema)
        detail = self._sql__qualify_table("NEXT_MIG_INFO_DTL", self.system_schema)
        with self._sql__connect() as conn:
            cur = conn.cursor()
            cur.execute(
                f"""
                SELECT M.MAP_ID, M.MAP_TYPE, M.FR_TABLE, D.FR_COL, M.TO_TABLE, D.TO_COL, M.CONDITION
                FROM {table} M
                LEFT JOIN {detail} D ON M.MAP_ID = D.MAP_ID
                ORDER BY M.PRIORITY ASC, M.MAP_ID ASC, D.MAP_DTL ASC
                """
            )
            rows = cur.fetchall()

        matched_rows = []
        matched_fr_tables: set[str] = set()
        for row in rows:
            map_id, map_type, fr_table, fr_col, to_table, to_col, condition = row
            fr_table_text = self._sql__to_text(fr_table)
            normalized_fr_table = self._sql__normalize_table_name(fr_table_text)
            if normalized_fr_table not in normalized_fr_tables:
                continue
            matched_rows.append((map_id, map_type, fr_table, fr_col, to_table, to_col, condition))
            matched_fr_tables.add(normalized_fr_table)
            if map_id is not None and int(map_id) not in map_ids:
                map_ids.append(int(map_id))

        if map_ids:
            for map_id in map_ids:
                sections.append(f"  - {map_id}")
        else:
            sections.append("  - No MAP_ID found for FR_TABLE hints.")

        unmatched_fr_tables = [
            table_name for table_name in fr_tables if self._sql__normalize_table_name(table_name) not in matched_fr_tables
        ]
        sections.append("\n[UNMAPPED_FR_TABLES]")
        if unmatched_fr_tables:
            for table_name in unmatched_fr_tables:
                sections.append(f"  - {table_name}: no mapping rule found. Keep the original table/column names.")
        else:
            sections.append("  - None.")

        sections.append("\n[MIGRATION_MAPPING_RULES]")
        if not matched_rows:
            sections.append("  - No mapping rules found.")
        else:
            for row in matched_rows[:1000]:
                map_id, map_type, fr_table, fr_col, to_table, to_col, condition = row
                map_type, fr_table, fr_col, to_table, to_col, condition = [
                    self._sql__to_text(v) for v in (map_type, fr_table, fr_col, to_table, to_col, condition)
                ]
                sections.append(
                    f"  - map_id={map_id}; map_type={map_type}; from={fr_table}.{fr_col or '*'}; to={to_table}.{to_col or '*'}; condition={condition}"
                )
        sections.append("\n[SQL_CONVERSION_RAG_GUIDANCE]")
        rag_lines = self._sql__load_conversion_rag_rules(fr_tables)
        sections.extend(rag_lines)
        rag_rule_count = len([line for line in rag_lines if line.strip().startswith("- {")])
        return "\n".join(sections), map_ids, fr_tables, rag_rule_count

    def _sql__load_conversion_rag_rules(self, fr_tables: list[str]) -> list[str]:
        table = self._sql__qualify_table("NEXT_MIG_RAG_INFO", self.system_schema)
        if not fr_tables:
            return ["  - No FR_TABLE hints for SQL_CONVERSION RAG lookup."]
        lines = []
        try:
            with self._sql__connect() as conn:
                cur = conn.cursor()
                for fr_table in fr_tables:
                    source_table = str(fr_table or "").strip().upper()
                    cur.execute(
                        f"""
                        SELECT RULE_TYPE, SOURCE_TABLES, GUIDANCE_TEXT, SOURCE_SQL, TARGET_SQL
                        FROM {table}
                        WHERE CATEGORY = 'SQL_CONVERSION'
                          AND UPPER(TRIM(NVL(USE_YN, 'Y'))) = 'Y'
                          AND UPPER(TRIM(SOURCE_TABLES)) = :1
                        ORDER BY CASE WHEN RULE_TYPE = 'GENERAL' THEN 1 ELSE 2 END, RAG_ID
                        FETCH FIRST 3 ROWS ONLY
                        """,
                        [source_table],
                    )
                    for rule_type, source_tables, guidance, source_sql, target_sql in cur.fetchall():
                        lines.append(
                            "  - "
                            + json.dumps(
                                {
                                    "rule_type": self._sql__to_text(rule_type),
                                    "source_tables": self._sql__to_text(source_tables),
                                    "guidance": self._sql__to_text(guidance),
                                    "source_sql": self._sql__to_text(source_sql)[:1000],
                                    "target_sql": self._sql__to_text(target_sql)[:1000],
                                },
                                ensure_ascii=False,
                            )
                        )
        except Exception:
            return ["  - No SQL_CONVERSION RAG rules loaded."]
        return lines or ["  - No SQL_CONVERSION RAG rules found for FR_TABLE hints."]

    # to_sql_prompt placeholder를 실제 값으로 치환한다.
    def _sql__render_to_sql_prompt(
        self,
        from_sql: str,
        mapping_schema_text: str,
        source_schema: str,
        target_schema: str,
        last_error: str,
    ) -> str:
        template = str(self.to_sql_prompt or "").strip()
        if not template:
            raise ValueError("TO SQL Prompt input is required for SQL generation")
        values = {
            "from_sql": from_sql,
            "mapping_schema_text": mapping_schema_text,
            "source_schema": source_schema,
            "target_schema": target_schema,
            "last_error": last_error,
        }
        for key, value in values.items():
            template = template.replace("{" + key + "}", str(value))
        return template

    # bind_sql_prompt placeholder를 실제 값으로 치환한다.
    def _sql__render_bind_sql_prompt(
        self,
        from_sql: str,
        to_sql: str,
        mapping_schema_text: str,
        source_schema: str,
        target_schema: str,
        last_error: str,
    ) -> str:
        template = str(self.bind_sql_prompt or "").strip()
        if not template:
            raise ValueError("BIND SQL Prompt input is required for BIND_SQL generation")
        values = {"from_sql": from_sql, "to_sql": to_sql, "mapping_schema_text": mapping_schema_text, "source_schema": source_schema, "target_schema": target_schema, "last_error": last_error}
        for key, value in values.items():
            template = template.replace("{" + key + "}", str(value))
        return template

    # test_sql_prompt placeholder를 실제 값으로 치환한다.
    def _sql__render_test_sql_prompt(
        self,
        from_sql: str,
        to_sql: str,
        bind_sql: str,
        bind_set: str,
        mapping_schema_text: str,
        source_schema: str,
        target_schema: str,
        last_error: str,
    ) -> str:
        template = str(self.test_sql_prompt or "").strip()
        if not template:
            raise ValueError("TEST SQL Prompt input is required for TEST_SQL generation")
        values = {"from_sql": from_sql, "to_sql": to_sql, "bind_sql": bind_sql, "bind_set": bind_set, "mapping_schema_text": mapping_schema_text, "source_schema": source_schema, "target_schema": target_schema, "last_error": last_error}
        for key, value in values.items():
            template = template.replace("{" + key + "}", str(value))
        return template

    def _sql__call_llm(self, prompt: str) -> str:
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
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0,
        }
        data = self._sql__post_json(url, payload, {"Authorization": f"Bearer {api_key}"})
        return str(data["choices"][0]["message"].get("content", ""))

    def _sql__sanitize_to_sql(self, value: str) -> str:
        text = str(value or "").strip()
        if text.startswith("```"):
            fence = re.search(r"```(?:sql)?\s*(.*?)```", text, flags=re.I | re.S)
            if fence:
                text = fence.group(1).strip()
        text = text.rstrip(";").strip()
        if not text:
            raise ValueError("LLM returned empty SQL")
        return text

    def _sql__build_bind_sql_prompt(self, job: dict[str, Any], to_sql: str, mapping_schema_text: str, last_error: Any = None) -> str:
        # EDIT_FR_SQL이 있으면 원본 FR_SQL보다 우선 사용한다.
        edit_fr_sql = str(job.get("edit_fr_sql") or "").strip()
        fr_sql = str(job.get("fr_sql") or "").strip()
        source_sql = edit_fr_sql if edit_fr_sql else fr_sql
        if not source_sql:
            raise ValueError("source SQL is empty")

        fr_tables = self._sql__extract_target_fr_tables(job.get("target_table"))
        source_schema = str(self.source_schema or "").strip().upper()
        if source_schema:
            for table_name in fr_tables:
                clean_table = str(table_name or "").strip().strip('"')
                if not clean_table or "." in clean_table:
                    continue
                source_sql = re.sub(rf"(?<![A-Z0-9_$#.]){re.escape(clean_table)}(?![A-Z0-9_$#])", f"{source_schema}.{clean_table}", source_sql, flags=re.I)

        return self._sql__render_bind_sql_prompt(
            from_sql=source_sql,
            to_sql=to_sql,
            mapping_schema_text=mapping_schema_text,
            source_schema=source_schema or "UNKNOWN",
            target_schema=str(self.target_schema or "").strip() or "UNKNOWN",
            last_error=str(last_error or "None"),
        )

    def _sql__build_test_sql_prompt(self, job: dict[str, Any], to_sql: str, bind_sql: str, bind_set: str, mapping_schema_text: str, last_error: Any = None) -> str:
        # EDIT_FR_SQL이 있으면 원본 FR_SQL보다 우선 사용한다.
        edit_fr_sql = str(job.get("edit_fr_sql") or "").strip()
        fr_sql = str(job.get("fr_sql") or "").strip()
        source_sql = edit_fr_sql if edit_fr_sql else fr_sql
        if not source_sql:
            raise ValueError("source SQL is empty")

        return self._sql__render_test_sql_prompt(
            from_sql=source_sql,
            to_sql=to_sql,
            bind_sql=bind_sql,
            bind_set=bind_set,
            mapping_schema_text=mapping_schema_text,
            source_schema=str(self.source_schema or "").strip() or "UNKNOWN",
            target_schema=str(self.target_schema or "").strip() or "UNKNOWN",
            last_error=str(last_error or "None"),
        )

    def _sql__save_final_sql(self, sql_id: str, space_nm: str, to_sql: str, bind_sql: str, bind_set: str, test_sql: str) -> None:
        assignments = []
        params: list[Any] = []
        for column, value in (("TO_SQL", to_sql), ("BIND_SQL", bind_sql), ("BIND_SET", bind_set), ("TEST_SQL", test_sql)):
            clean_value = str(value or "").strip()
            if clean_value:
                params.append(clean_value)
                assignments.append(f"{column} = :{len(params)}")
        if not assignments:
            return
        params.extend([space_nm, sql_id])
        table = self._sql__qualify_table("NEXT_SQL_INFO", self.system_schema)
        with self._sql__connect() as conn:
            cur = conn.cursor()
            cur.execute(
                f"""
                UPDATE {table}
                SET {", ".join(assignments)},
                    UPD_TS = CURRENT_TIMESTAMP
                WHERE SPACE_NM = :{len(params) - 1}
                  AND SQL_ID = :{len(params)}
                """,
                params,
            )
            conn.commit()

    # SQL Conversion 최종 상태와 batch count를 NEXT_SQL_INFO에 저장한다.
    def _sql__update_job_status(self, sql_id: str, space_nm: str, status_conversion: str, elapsed_seconds: int, retry_count: int, status_tuning: str | None = None) -> None:
        assignments = ["STATUS_CONVERSION = :1", "RETRY_COUNT = :2", "BATCH_CNT = NVL(BATCH_CNT, 0) + 1", "LOG = :3", "UPD_TS = CURRENT_TIMESTAMP"]
        params: list[Any] = [status_conversion, retry_count, f"STATUS_CONVERSION={status_conversion}; elapsed={elapsed_seconds}s; retry={retry_count}"]
        if status_tuning:
            params.append(status_tuning)
            assignments.append(f"STATUS_TUNING = :{len(params)}")
        params.extend([space_nm, sql_id])
        table = self._sql__qualify_table("NEXT_SQL_INFO", self.system_schema)
        with self._sql__connect() as conn:
            cur = conn.cursor()
            cur.execute(
                f"""
                UPDATE {table}
                SET {", ".join(assignments)}
                WHERE SPACE_NM = :{len(params) - 1}
                  AND SQL_ID = :{len(params)}
                """,
                params,
            )
            conn.commit()

    # SQL Conversion 단계 로그를 NEXT_SQL_LOG에 저장한다.
    def _sql__write_log(self, sql_id: str, space_nm: str, sql_kind: str, status: str, stage_name: str, message: str, retry_count: int = 0, sql_content: str | None = None, elapsed_seconds: int | None = None, prompt_name: str | None = None) -> None:
        table = self._sql__qualify_table("NEXT_SQL_LOG", self.system_schema)
        try:
            with self._sql__connect() as conn:
                cur = conn.cursor()
                cur.execute(
                    f"""
                    INSERT INTO {table} (
                        CREATED_AT, SPACE_NM, SQL_ID, SQL_KIND, SQL_CONTENT,
                        STATUS, PROMPT_NAME, MODEL_NAME, ELAPSED_SECONDS,
                        ATTEMPT_NO, STAGE_NAME, ERROR_MESSAGE
                    ) VALUES (
                        CURRENT_TIMESTAMP, :1, :2, :3, :4,
                        :5, :6, :7, :8,
                        :9, :10, :11
                    )
                    """,
                    [
                        str(space_nm or "")[:200],
                        str(sql_id or "")[:200],
                        str(sql_kind or "")[:30],
                        sql_content,
                        str(status or "")[:20],
                        str(prompt_name or "")[:120] if prompt_name else None,
                        str(self.llm_model or "")[:120] if self.llm_model else None,
                        elapsed_seconds,
                        retry_count,
                        str(stage_name or "")[:100],
                        str(message or "")[:3900],
                    ],
                )
                conn.commit()
        except Exception:
            pass

    def _sql__prepare_runtime_sql(self, sql_text: str, stage: str) -> str:
        clean_sql = self._sql__sanitize_to_sql(sql_text)
        lowered = clean_sql.lower()
        for token in ("<if", "<choose", "<when", "<otherwise", "<where", "<trim", "#{", "${"):
            if token in lowered:
                raise ValueError(f"{stage} generated non-executable SQL containing '{token}'")
        limit_match = re.search(r"\s+LIMIT\s+(\d+)\s*$", clean_sql, flags=re.I)
        if limit_match:
            limit = int(limit_match.group(1))
            inner = re.sub(r"\s+LIMIT\s+\d+\s*$", "", clean_sql, flags=re.I).strip()
            clean_sql = f"SELECT * FROM ({inner}) WHERE ROWNUM <= {limit}"
        fetch_match = re.search(r"\s+FETCH\s+FIRST\s+(\d+)\s+ROWS\s+ONLY\s*$", clean_sql, flags=re.I)
        if fetch_match:
            limit = int(fetch_match.group(1))
            inner = re.sub(r"\s+FETCH\s+FIRST\s+\d+\s+ROWS\s+ONLY\s*$", "", clean_sql, flags=re.I).strip()
            clean_sql = f"SELECT * FROM ({inner}) WHERE ROWNUM <= {limit}"
        return clean_sql

    def _sql__get_row_value(self, row: dict[str, Any], key: str) -> Any:
        if key in row:
            return row[key]
        lowered = key.lower()
        for existing_key, value in row.items():
            if str(existing_key).lower() == lowered:
                return value
        return None

    def _sql__json_value(self, value: Any) -> Any:
        if value is None:
            return None
        if hasattr(value, "read"):
            value = value.read()
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="ignore")
        if isinstance(value, (str, int, float, bool)):
            return value
        return str(value)

    def _sql__summary_result(self, result: dict[str, Any]) -> dict[str, Any]:
        summary = {"ok": bool(result.get("ok")), "status": result.get("status")}
        for key in ["message", "error", "row_count", "elapsed_seconds", "retry_count"]:
            if key in result:
                summary[key] = result.get(key)
        return summary

    def _sql__fallback_conversion_failure_status(self, to_sql: str, bind_sql: str, bind_set: str, test_sql: str) -> str:
        if not self._sql__to_text(to_sql).strip():
            return "FAIL-TOBE"
        if not self._sql__to_text(bind_sql).strip() or not self._sql__to_text(bind_set).strip():
            return "FAIL-BIND"
        return "FAIL-TEST"

    def _sql__extract_target_fr_tables(self, value: Any) -> list[str]:
        text = self._sql__to_text(value).strip()
        if not text:
            return []
        parsed = json.loads(text)
        if not isinstance(parsed, list):
            raise ValueError("TARGET_TABLE must be a JSON array like [\"table_a\", \"table_b\"]")
        names: list[str] = []
        for table_name in parsed:
            clean_table = str(table_name or "").strip()
            if clean_table and clean_table not in names:
                names.append(clean_table)
        return names[:50]

    def _sql__normalize_table_name(self, value: Any) -> str:
        text = self._sql__to_text(value).strip().strip('"').upper()
        if "." in text:
            text = text.split(".")[-1]
        return text

    def _sql__qualify_table(self, table_name: str, schema: str | None) -> str:
        clean = str(table_name or "").strip()
        clean_schema = str(schema or "").strip().upper()
        if not clean:
            raise ValueError("table_name is empty")
        if "." in clean or not clean_schema:
            return clean
        if not re.fullmatch(r"[A-Z][A-Z0-9_$#]*", clean_schema):
            raise ValueError(f"Invalid schema: {clean_schema}")
        return f"{clean_schema}.{clean}"

    def _sql__to_text(self, value: Any) -> str:
        if value is None:
            return ""
        if hasattr(value, "read"):
            value = value.read()
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="ignore")
        return str(value)

    def _sql__as_bool(self, value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        return str(value).strip().lower() in {"1", "true", "t", "y", "yes", "on"}


