from __future__ import annotations

import json
import re
import time
from decimal import Decimal, InvalidOperation
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import BoolInput, IntInput, MessageTextInput, SecretStrInput, StrInput, Output
from lfx.schema.data import Data


class MigrationCommandTool(Component):
    display_name = "Migration Command Tool"
    description = "Controls SmartMigration DB migration jobs through Oracle metadata tables."
    name = "MigrationCommandTool"
    icon = "Database"

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
            name="allow_generated_sql_execution",
            display_name="Allow Generated SQL Execution",
            value=True,
            required=False,
            info="If false, run_migration_job only saves generated SQL and does not execute it unless USER_EDITED=Y.",
        ),
    ]

    outputs = [
        Output(display_name="Result", name="result", method="run_command"),
    ]

    def run_command(self) -> Data:
        try:
            command = self._parse_command()
            action = (command.get("action") or "").strip().lower()
            map_id = command.get("map_id")

            if action == "status":
                result = self._status(map_id)
            elif action == "list_pending":
                result = self._list_pending(command.get("limit", 10))
            elif action == "reset":
                result = self._reset(map_id, preserve_user_sql=bool(command.get("preserve_user_sql", False)))
            elif action == "save_user_sql":
                result = self._save_user_sql(map_id, command)
            elif action == "analyze_failure":
                result = self._analyze_failure(map_id)
            elif action == "run_migration_job":
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
        raw = self.command_json
        if isinstance(raw, dict):
            return raw
        text = str(raw or "").strip()
        if not text:
            raise ValueError("command_json is required")
        return json.loads(text)

    def _connect(self):
        import oracledb

        host = str(self.db_host or "").strip()
        port = int(self.db_port or 1521)
        service_name = str(self.db_service_name or "").strip()
        username = str(self.db_username or "").strip()
        if not host:
            raise ValueError("DB Host is required")
        if not service_name:
            raise ValueError("Service Name is required")
        if not username:
            raise ValueError("Username is required")

        dsn = f"{host}:{port}/{service_name}"
        return oracledb.connect(user=username, password=self.db_password, dsn=dsn)

    def _status(self, map_id: Any) -> dict[str, Any]:
        map_id = self._require_map_id(map_id)
        job = self._load_job(map_id)
        if not job:
            return {"ok": False, "map_id": map_id, "error": "job not found"}
        details = self._load_details(map_id)
        return {"ok": True, "job": job, "details": details}

    def _list_pending(self, limit: Any) -> dict[str, Any]:
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

    def _reset(self, map_id: Any, preserve_user_sql: bool = False) -> dict[str, Any]:
        map_id = self._require_map_id(map_id)
        map_table = self._system_table("NEXT_MIG_INFO")
        if preserve_user_sql:
            sql = f"""
                UPDATE {map_table}
                SET STATUS = NULL,
                    RETRY_COUNT = 0,
                    BATCH_CNT = 0,
                    UPD_TS = CURRENT_TIMESTAMP
                WHERE MAP_ID = :1
            """
        else:
            sql = f"""
                UPDATE {map_table}
                SET STATUS = NULL,
                    RETRY_COUNT = 0,
                    BATCH_CNT = 0,
                    MIG_SQL = NULL,
                    VERIFY_SQL = NULL,
                    USER_EDITED = 'N',
                    UPD_TS = CURRENT_TIMESTAMP
                WHERE MAP_ID = :1
            """
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(sql, [map_id])
            rowcount = cur.rowcount
            conn.commit()
        self._write_log(map_id, "RESET", "INFO", "RESET", "PASS", f"Job reset. preserve_user_sql={preserve_user_sql}")
        return {"ok": rowcount > 0, "map_id": map_id, "updated_rows": rowcount}

    def _save_user_sql(self, map_id: Any, command: dict[str, Any]) -> dict[str, Any]:
        map_id = self._require_map_id(map_id)
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

    def _analyze_failure(self, map_id: Any) -> dict[str, Any]:
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
                       TO_CHAR(COALESCE(UPD_TS, CREATED_AT), 'YYYY-MM-DD HH24:MI:SS') AS LOG_TIME
                FROM {log_table}
                WHERE MAP_ID = :1
                ORDER BY LOG_ID DESC
                FETCH FIRST 10 ROWS ONLY
                """,
                [map_id],
            )
            rows = cur.fetchall()

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
            "recent_logs": [
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
            ],
        }

    def _run_migration_job(self, map_id: Any, command: dict[str, Any]) -> dict[str, Any]:
        map_id = self._require_map_id(map_id)
        started = time.perf_counter()
        max_attempts = max(1, int(command.get("max_attempts") or self.default_max_attempts or 1))
        force_rerun = bool(command.get("force_rerun", False))

        job = self._load_job(map_id)
        if not job:
            return {"ok": False, "map_id": map_id, "error": "job not found"}

        if str(job.get("use_yn") or "").upper() != "Y":
            return {"ok": False, "map_id": map_id, "status": "SKIP", "error": "USE_YN is not Y"}

        if job.get("status") == "PASS" and not force_rerun:
            return {"ok": True, "map_id": map_id, "status": "PASS", "message": "Job already passed"}

        dep = self._check_dependencies(job)
        if dep["status"] != "READY":
            final_status = "SKIP" if dep["status"] == "FAIL" else "WAITING"
            if final_status == "SKIP":
                self._update_job_status(map_id, "SKIP", 0, int(job.get("retry_count") or 0))
            self._write_log(map_id, "DEPENDENCY", "WARN", "DEP_CHECK", final_status, dep["message"])
            return {"ok": final_status == "WAITING", "map_id": map_id, "status": final_status, "message": dep["message"]}

        details = self._load_details(map_id)
        if not details:
            self._update_job_status(map_id, "FAIL", 0, 0)
            return {"ok": False, "map_id": map_id, "status": "FAIL", "error": "No mapping details found"}

        self._increment_batch_count(map_id)
        last_error = ""
        retry_count = 0
        current_mig_sql = ""
        current_verify_sql = ""

        for attempt in range(1, max_attempts + 1):
            retry_count = attempt - 1
            try:
                job = self._load_job(map_id) or job
                user_edited = str(job.get("user_edited") or "").upper() == "Y"
                if user_edited:
                    current_mig_sql = str(job.get("mig_sql") or "").strip()
                    current_verify_sql = str(job.get("verify_sql") or "").strip()
                    if not current_mig_sql:
                        raise ValueError("USER_EDITED=Y but MIG_SQL is empty")
                    if not current_verify_sql:
                        current_verify_sql = self._build_verify_sql(job)
                        self._save_generated_sql(map_id, current_mig_sql, current_verify_sql)
                else:
                    current_mig_sql = self._build_migration_sql(job, details)
                    current_verify_sql = self._build_verify_sql(job)
                    self._save_generated_sql(map_id, current_mig_sql, current_verify_sql)

                self._write_log(
                    map_id,
                    "GENERATE_SQL",
                    "INFO",
                    "GENERATE",
                    "PASS",
                    f"Migration SQL prepared. attempt={attempt}",
                    retry_count=retry_count,
                    generate_sql=current_mig_sql,
                )

                if not user_edited and not bool(self.allow_generated_sql_execution):
                    return {
                        "ok": True,
                        "map_id": map_id,
                        "status": "SQL_GENERATED",
                        "message": "Generated SQL saved. Execution skipped because allow_generated_sql_execution=false.",
                        "mig_sql": current_mig_sql,
                        "verify_sql": current_verify_sql,
                    }

                if str(job.get("trunc_yn") or "").upper() == "Y":
                    self._truncate_target(job)

                self._execute_sql_script(current_mig_sql)
                self._write_log(map_id, "EXECUTE_SQL", "INFO", "SQL_EXEC", "PASS", "Migration SQL executed", retry_count)

                verify_ok, verify_message = self._execute_verify_sql(current_verify_sql)
                if not verify_ok:
                    raise RuntimeError(f"Verification failed: {verify_message}")

                elapsed = int(time.perf_counter() - started)
                self._update_job_status(map_id, "PASS", elapsed, retry_count)
                self._write_log(map_id, "VERIFY_SQL", "INFO", "VERIFY", "PASS", verify_message, retry_count, current_verify_sql)
                return {
                    "ok": True,
                    "map_id": map_id,
                    "status": "PASS",
                    "message": "Migration completed",
                    "elapsed_seconds": elapsed,
                    "retry_count": retry_count,
                    "mig_sql": current_mig_sql,
                    "verify_sql": current_verify_sql,
                }
            except Exception as exc:
                last_error = str(exc)
                failure_status = self._classify_failure(last_error)
                self._write_log(
                    map_id,
                    "ROW_ERROR",
                    "WARN",
                    "RETRY" if attempt < max_attempts else "FINAL",
                    failure_status,
                    last_error[:3900],
                    retry_count,
                    current_verify_sql if failure_status == "FAIL-TEST" else current_mig_sql,
                )
                if attempt >= max_attempts:
                    elapsed = int(time.perf_counter() - started)
                    self._update_job_status(map_id, failure_status, elapsed, retry_count)
                    return {
                        "ok": False,
                        "map_id": map_id,
                        "status": failure_status,
                        "error": last_error,
                        "elapsed_seconds": elapsed,
                        "retry_count": retry_count,
                        "mig_sql": current_mig_sql,
                        "verify_sql": current_verify_sql,
                    }

        return {"ok": False, "map_id": map_id, "status": "FAIL", "error": last_error}

    def _load_job(self, map_id: int) -> dict[str, Any] | None:
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
        prior_map_id = job.get("prior_map_id")
        if prior_map_id is None or int(prior_map_id) <= 0:
            return {"status": "READY", "message": "No prior dependency"}
        prior = self._load_job(int(prior_map_id))
        if not prior:
            return {"status": "PENDING", "message": f"Prior MAP_ID={prior_map_id} not found"}
        prior_status = str(prior.get("status") or "").upper()
        if prior_status != "PASS":
            return {"status": prior_status or "PENDING", "message": f"Prior MAP_ID={prior_map_id} status={prior_status or 'NULL'}"}
        return {"status": "READY", "message": "Prior dependency passed"}

    def _build_migration_sql(self, job: dict[str, Any], details: list[dict[str, Any]]) -> str:
        to_table = self._qualify_table(job["to_table"], self.target_schema)
        fr_expr = self._qualify_source_expression(job["fr_table"])
        condition = str(job.get("condition") or "").strip()
        target_cols = ", ".join(self._quote_identifier(d["to_col"]) for d in details)
        source_cols = ", ".join(d["fr_col"] for d in details)
        where_clause = f"\nWHERE {condition}" if condition else ""
        return f"INSERT INTO {to_table} ({target_cols})\nSELECT {source_cols}\nFROM {fr_expr}{where_clause}"

    def _build_verify_sql(self, job: dict[str, Any]) -> str:
        to_table = self._qualify_table(job["to_table"], self.target_schema)
        fr_expr = self._qualify_source_expression(job["fr_table"])
        condition = str(job.get("condition") or "").strip()
        source_where = f" WHERE {condition}" if condition else ""
        return (
            "SELECT ABS((SELECT COUNT(*) FROM "
            + fr_expr
            + source_where
            + ") - (SELECT COUNT(*) FROM "
            + to_table
            + ")) AS DIFF FROM DUAL"
        )

    def _save_generated_sql(self, map_id: int, mig_sql: str, verify_sql: str) -> None:
        map_table = self._system_table("NEXT_MIG_INFO")
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                f"""
                UPDATE {map_table}
                SET MIG_SQL = :1,
                    VERIFY_SQL = :2,
                    UPD_TS = CURRENT_TIMESTAMP
                WHERE MAP_ID = :3
                """,
                [mig_sql, verify_sql, map_id],
            )
            conn.commit()

    def _truncate_target(self, job: dict[str, Any]) -> None:
        target = self._qualify_table(job["to_table"], self.target_schema)
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(f"TRUNCATE TABLE {target}")
            conn.commit()

    def _execute_sql_script(self, sql_script: str) -> None:
        statements = self._split_sql_script(sql_script)
        with self._connect() as conn:
            cur = conn.cursor()
            for stmt in statements:
                cleaned = stmt.strip().rstrip(";")
                if cleaned:
                    cur.execute(cleaned)
            conn.commit()

    def _execute_verify_sql(self, verify_sql: str) -> tuple[bool, str]:
        statements = self._split_sql_script(verify_sql)
        if not statements:
            return False, "verify_sql is empty"
        last_rows = []
        with self._connect() as conn:
            cur = conn.cursor()
            for stmt in statements:
                cleaned = stmt.strip().rstrip(";")
                if not cleaned:
                    continue
                cur.execute(cleaned)
                if cur.description:
                    last_rows = cur.fetchall()
        if not last_rows:
            return False, "Verification SQL returned no rows"
        for row in last_rows:
            for value in row:
                if not self._is_zero(value):
                    return False, f"Mismatch found: {row}"
        return True, "All Verification Passed"

    def _update_job_status(self, map_id: int, status: str, elapsed_seconds: int, retry_count: int) -> None:
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
        log_table = self._system_table("NEXT_MIG_LOG")
        seq = self._system_table("MIGRATION_LOG_SEQ")
        safe_message = str(message or "")[:4000]
        try:
            with self._connect() as conn:
                cur = conn.cursor()
                cur.execute(
                    f"""
                    INSERT INTO {log_table}
                        (LOG_ID, MAP_ID, MIG_KIND, LOG_TYPE, LOG_LEVEL, STEP_NAME, STATUS,
                         MESSAGE, GENERATE_SQL, RETRY_COUNT, CREATED_AT, UPD_TS)
                    VALUES
                        ({seq}.NEXTVAL, :1, 'DB_MIG', :2, :3, :4, :5,
                         :6, :7, :8, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """,
                    [map_id, log_type, log_level, step_name, status, safe_message, generate_sql, retry_count],
                )
                conn.commit()
        except Exception:
            # Logging must not break the operational command.
            pass

    def _classify_failure(self, error: str) -> str:
        text = str(error or "").upper()
        if "TRUNCATE" in text:
            return "FAIL-TRUNCATE"
        if "VERIFY" in text or "MISMATCH" in text:
            return "FAIL-TEST"
        return "FAIL-INSERT"

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

    def _quote_identifier(self, identifier: str) -> str:
        clean = str(identifier or "").strip()
        if not clean:
            raise ValueError("empty identifier")
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_$#]*", clean):
            return clean
        if "." in clean:
            return clean
        return '"' + clean.replace('"', '""') + '"'

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


