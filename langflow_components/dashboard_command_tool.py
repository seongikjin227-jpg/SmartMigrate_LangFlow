from __future__ import annotations

import json
import re
import subprocess
import sys
from contextlib import contextmanager
from typing import Any
from urllib.parse import quote_plus

from lfx.custom.custom_component.component import Component
from lfx.io import BoolInput, IntInput, MessageTextInput, SecretStrInput, StrInput, Output
from lfx.schema.data import Data


class DashboardCommandTool(Component):
    display_name = "Dashboard Command Tool"
    description = "Summarizes SmartMigration agent job queues and recommends the next agent action."
    name = "DashboardCommandTool"
    icon = "LayoutDashboard"

    _db_cache: dict[str, Any] = {}

    inputs = [
        MessageTextInput(
            name="command_json",
            display_name="Command JSON",
            required=True,
            tool_mode=True,
            info='JSON command. Example: {"action":"summary"}',
        ),
        StrInput(name="db_host", display_name="DB Host", required=True),
        IntInput(name="db_port", display_name="DB Port", value=1521, required=True),
        StrInput(name="db_service_name", display_name="Service Name", required=True),
        StrInput(name="db_username", display_name="Username", required=True),
        SecretStrInput(name="db_password", display_name="Password", required=True),
        StrInput(
            name="system_schema",
            display_name="System Schema",
            required=False,
            info="Schema containing NEXT_MIG_INFO/NEXT_SQL_INFO. Leave blank for current user.",
        ),
        IntInput(name="list_limit", display_name="Default List Limit", value=5, required=False),
        BoolInput(
            name="auto_install_packages",
            display_name="Auto Install Missing Packages",
            value=False,
            required=False,
            info="If true, installs missing runtime packages with pip before DB connection.",
        ),
    ]

    outputs = [
        Output(display_name="Result", name="result", method="run_command"),
    ]

    def run_command(self) -> Data:
        try:
            command = self._parse_command()
            action = str(command.get("action") or "summary").strip().lower()

            if action == "summary":
                result = self._summary(command)
            else:
                result = {"ok": False, "error": f"Unsupported action: {action}"}

            self.status = result
            return Data(data=result)
        except Exception as exc:
            result = {"ok": False, "error": str(exc)}
            self.status = result
            return Data(data=result)

    # action="summary"
    def _summary(self, command: dict[str, Any]) -> dict[str, Any]:
        limit = max(1, min(int(command.get("limit") or self.list_limit or 5), 20))
        agents = {
            "db_migration": self._migration_summary(limit),
            "sql_conversion": self._sql_conversion_summary(limit),
            "sql_tuning": self._sql_tuning_summary(limit),
            "sql_formatting": self._sql_formatting_summary(limit),
        }
        recommendations = self._recommend_next_actions(agents)
        return {
            "ok": True,
            "action": "summary",
            "recommendations": recommendations,
            "agents": agents,
        }

    def _migration_summary(self, limit: int) -> dict[str, Any]:
        table = self._system_table("NEXT_MIG_INFO")
        status_counts = self._status_counts(table, "STATUS", "UPPER(TRIM(NVL(USE_YN, 'N'))) = 'Y'")
        target_count = self._count(
            table,
            "UPPER(TRIM(NVL(USE_YN, 'N'))) = 'Y' AND STATUS IS NULL",
        )
        next_jobs = self._query_rows(
            f"""
            SELECT *
            FROM (
                SELECT MAP_ID, MAP_TYPE, FR_TABLE, TO_TABLE, PRIORITY, STATUS, BATCH_CNT, RETRY_COUNT, UPD_TS
                FROM {table}
                WHERE UPPER(TRIM(NVL(USE_YN, 'N'))) = 'Y'
                  AND STATUS IS NULL
                ORDER BY PRIORITY ASC, MAP_ID ASC
            )
            WHERE ROWNUM <= :1
            """,
            [limit],
            ["map_id", "map_type", "fr_table", "to_table", "priority", "status", "batch_cnt", "retry_count", "upd_ts"],
        )
        return {
            "agent": "DB_MIGRATION",
            "table": table,
            "target_count": target_count,
            "target_condition": "USE_YN='Y' AND STATUS IS NULL",
            "status_counts": status_counts,
            "next_jobs": next_jobs,
        }

    def _sql_conversion_summary(self, limit: int) -> dict[str, Any]:
        table = self._system_table("NEXT_SQL_INFO")
        status_counts = self._status_counts(table, "STATUS_CONVERSION")
        target_count = self._count(
            table,
            "STATUS_CONVERSION IS NULL OR UPPER(TRIM(STATUS_CONVERSION)) = 'READY'",
        )
        next_jobs = self._query_rows(
            f"""
            SELECT *
            FROM (
                SELECT TAG_KIND, SPACE_NM, SQL_ID, STATUS_CONVERSION, PRIORITY, BATCH_CNT, UPD_TS
                FROM {table}
                WHERE STATUS_CONVERSION IS NULL
                   OR UPPER(TRIM(STATUS_CONVERSION)) = 'READY'
                ORDER BY PRIORITY ASC NULLS LAST, UPD_TS NULLS FIRST, SPACE_NM, SQL_ID
            )
            WHERE ROWNUM <= :1
            """,
            [limit],
            ["tag_kind", "space_nm", "sql_id", "status_conversion", "priority", "batch_cnt", "upd_ts"],
        )
        return {
            "agent": "SQL_CONVERSION",
            "table": table,
            "target_count": target_count,
            "target_condition": "STATUS_CONVERSION IS NULL OR STATUS_CONVERSION='READY'",
            "status_counts": status_counts,
            "next_jobs": next_jobs,
        }

    def _sql_tuning_summary(self, limit: int) -> dict[str, Any]:
        table = self._system_table("NEXT_SQL_INFO")
        columns = self._available_columns("NEXT_SQL_INFO")
        if "STATUS_TUNING" not in columns:
            return self._unavailable("SQL_TUNING", table, "STATUS_TUNING column not found")

        status_counts = self._status_counts(table, "STATUS_TUNING")
        target_count = self._count(
            table,
            "UPPER(TRIM(STATUS_TUNING)) IN ('READY', 'URGENT', 'FAIL', 'FAIL-TUNED', 'FAIL-BIND', 'FAIL-TEST') "
            "AND TO_SQL_TEXT IS NOT NULL "
            "AND UPPER(TRIM(STATUS_CONVERSION)) IN ('PASS-CONVERSION', 'PASS')",
        )
        next_jobs = self._query_rows(
            f"""
            SELECT *
            FROM (
                SELECT TAG_KIND, SPACE_NM, SQL_ID, STATUS_CONVERSION, STATUS_TUNING, PRIORITY, BATCH_CNT, UPD_TS
                FROM {table}
                WHERE UPPER(TRIM(STATUS_TUNING)) IN ('READY', 'URGENT', 'FAIL', 'FAIL-TUNED', 'FAIL-BIND', 'FAIL-TEST')
                  AND TO_SQL_TEXT IS NOT NULL
                  AND UPPER(TRIM(STATUS_CONVERSION)) IN ('PASS-CONVERSION', 'PASS')
                ORDER BY PRIORITY ASC NULLS LAST, UPD_TS NULLS FIRST, SPACE_NM, SQL_ID
            )
            WHERE ROWNUM <= :1
            """,
            [limit],
            ["tag_kind", "space_nm", "sql_id", "status_conversion", "status_tuning", "priority", "batch_cnt", "upd_ts"],
        )
        return {
            "agent": "SQL_TUNING",
            "table": table,
            "target_count": target_count,
            "target_condition": "STATUS_TUNING in retryable states, TO_SQL_TEXT exists, conversion passed",
            "status_counts": status_counts,
            "next_jobs": next_jobs,
        }

    def _sql_formatting_summary(self, limit: int) -> dict[str, Any]:
        table = self._system_table("NEXT_SQL_INFO")
        columns = self._available_columns("NEXT_SQL_INFO")
        if "FORMATTING_RETRY_YN" not in columns or "FORMATTED_SQL" not in columns:
            return self._unavailable("SQL_FORMATTING", table, "FORMATTING_RETRY_YN or FORMATTED_SQL column not found")

        target_count = self._count(
            table,
            "UPPER(TRIM(FORMATTING_RETRY_YN)) = 'Y'",
        )
        next_jobs = self._query_rows(
            f"""
            SELECT *
            FROM (
                SELECT TAG_KIND, SPACE_NM, SQL_ID, STATUS_CONVERSION, STATUS_TUNING,
                       FORMATTING_RETRY_YN, PRIORITY, BATCH_CNT, UPD_TS
                FROM {table}
                WHERE UPPER(TRIM(FORMATTING_RETRY_YN)) = 'Y'
                ORDER BY PRIORITY ASC NULLS LAST, UPD_TS NULLS FIRST, SPACE_NM, SQL_ID
            )
            WHERE ROWNUM <= :1
            """,
            [limit],
            [
                "tag_kind",
                "space_nm",
                "sql_id",
                "status_conversion",
                "status_tuning",
                "formatting_retry_yn",
                "priority",
                "batch_cnt",
                "upd_ts",
            ],
        )
        return {
            "agent": "SQL_FORMATTING",
            "table": table,
            "target_count": target_count,
            "target_condition": "FORMATTING_RETRY_YN='Y'",
            "status_counts": {"FORMATTING_RETRY_YN=Y": target_count},
            "next_jobs": next_jobs,
        }

    def _recommend_next_actions(self, agents: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        priority = ["db_migration", "sql_conversion", "sql_tuning", "sql_formatting"]
        recommendations = []
        for key in priority:
            summary = agents.get(key, {})
            count = int(summary.get("target_count") or 0)
            if count <= 0:
                continue
            recommendations.append(
                {
                    "agent": summary.get("agent"),
                    "target_count": count,
                    "reason": f"{summary.get('agent')} 작업 대상이 {count}건 있습니다.",
                    "first_job": (summary.get("next_jobs") or [None])[0],
                }
            )
        return recommendations

    def _parse_command(self) -> dict[str, Any]:
        raw = self.command_json
        if isinstance(raw, dict):
            return raw
        text = str(raw or "").strip()
        if not text:
            return {"action": "summary"}
        return json.loads(text)

    def _connection_string(self) -> str:
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
        self._ensure_runtime_dependencies()
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
            self._db_cache[cache_key] = SQLDatabase.from_uri(self._connection_string())
        return self._db_cache[cache_key]

    def _ensure_runtime_dependencies(self) -> None:
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
        if not bool(self.auto_install_packages):
            raise ModuleNotFoundError(
                "Missing packages: "
                + ", ".join(missing_packages)
                + ". Enable Auto Install Missing Packages or install them in the Langflow runtime."
            )
        for package in missing_packages:
            self._pip_install(package)

    def _pip_install(self, package: str) -> None:
        subprocess.check_call([sys.executable, "-m", "pip", "install", package])

    @contextmanager
    def _connect(self):
        db = self._get_db()
        engine = getattr(db, "_engine", None) or getattr(db, "engine", None)
        if engine is None:
            raise ValueError("SQLDatabase engine is not available")
        conn = engine.raw_connection()
        try:
            yield conn
        finally:
            conn.close()

    def _count(self, table: str, where_clause: str) -> int:
        query = f"SELECT COUNT(*) FROM {table} WHERE {where_clause}"
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(query)
            row = cur.fetchone()
        return int(row[0] or 0) if row else 0

    def _status_counts(self, table: str, column: str, where_clause: str = "1 = 1") -> dict[str, int]:
        query = f"""
            SELECT NVL(TO_CHAR({column}), 'NULL') AS STATUS_VALUE, COUNT(*)
            FROM {table}
            WHERE {where_clause}
            GROUP BY NVL(TO_CHAR({column}), 'NULL')
            ORDER BY STATUS_VALUE
        """
        rows = self._query_rows(query, [], ["status", "count"])
        return {str(row["status"]): int(row["count"] or 0) for row in rows}

    def _query_rows(self, query: str, params: list[Any], columns: list[str]) -> list[dict[str, Any]]:
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(query, params)
            rows = cur.fetchall()
        return [
            {columns[i]: self._to_text(value) if not isinstance(value, (int, float)) else value for i, value in enumerate(row)}
            for row in rows
        ]

    def _available_columns(self, table_name: str) -> set[str]:
        schema = str(self.system_schema or "").strip().upper()
        clean_table = str(table_name or "").strip().upper()
        if schema:
            query = """
                SELECT COLUMN_NAME
                FROM ALL_TAB_COLUMNS
                WHERE OWNER = :1
                  AND TABLE_NAME = :2
            """
            params = [schema, clean_table]
        else:
            query = """
                SELECT COLUMN_NAME
                FROM USER_TAB_COLUMNS
                WHERE TABLE_NAME = :1
            """
            params = [clean_table]
        rows = self._query_rows(query, params, ["column_name"])
        return {str(row["column_name"]).upper() for row in rows}

    def _unavailable(self, agent: str, table: str, reason: str) -> dict[str, Any]:
        return {
            "agent": agent,
            "table": table,
            "available": False,
            "target_count": 0,
            "reason": reason,
            "status_counts": {},
            "next_jobs": [],
        }

    def _system_table(self, table_name: str) -> str:
        clean = str(table_name or "").strip().upper()
        schema = str(self.system_schema or "").strip().upper()
        if not schema:
            return clean
        self._validate_identifier(schema, "system_schema")
        self._validate_identifier(clean, "table_name")
        return f"{schema}.{clean}"

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
