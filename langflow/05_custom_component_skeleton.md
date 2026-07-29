# Custom Component Skeleton

아래 코드는 Langflow 웹 UI의 Custom Python Component에 붙여 넣기 위한 초기 골격이다.
먼저 `status`, `list_pending`, `reset`, `save_user_sql`만 검증하고, 이후 `run_migration_job`을 채운다.

```python
from lfx.custom.custom_component.component import Component
from lfx.io import MessageTextInput, SecretStrInput, StrInput, Output
from lfx.schema.data import Data
import json


class MigrationCommandTool(Component):
    display_name = "Migration Command Tool"
    description = "Controls SmartMigration DB migration jobs using JSON commands."
    name = "MigrationCommandTool"
    icon = "Database"

    inputs = [
        MessageTextInput(
            name="command_json",
            display_name="Command JSON",
            required=True,
            tool_mode=True,
            info="JSON command. Example: {\"action\":\"status\",\"map_id\":101}",
        ),
        StrInput(
            name="db_dsn",
            display_name="Oracle DSN",
            required=True,
            advanced=True,
            info="Example: localhost:1521/xe",
        ),
        StrInput(
            name="db_user",
            display_name="DB User",
            required=True,
            advanced=True,
        ),
        SecretStrInput(
            name="db_password",
            display_name="DB Password",
            required=True,
            advanced=True,
        ),
    ]

    outputs = [
        Output(display_name="Result", name="result", method="run_command"),
    ]

    def run_command(self) -> Data:
        try:
            command = json.loads(self.command_json)
            action = (command.get("action") or "").strip()
            map_id = command.get("map_id")

            if action == "status":
                result = self._status(map_id)
            elif action == "list_pending":
                result = self._list_pending(command.get("limit", 10))
            elif action == "reset":
                result = self._reset(map_id)
            elif action == "save_user_sql":
                result = self._save_user_sql(map_id, command)
            elif action == "run_migration_job":
                result = self._run_migration_job(map_id, command)
            elif action == "analyze_failure":
                result = self._analyze_failure(map_id)
            else:
                result = {"ok": False, "error": f"Unsupported action: {action}"}

            self.status = result
            return Data(data=result)
        except Exception as exc:
            result = {"ok": False, "error": str(exc)}
            self.status = result
            return Data(data=result)

    def _connect(self):
        import oracledb
        return oracledb.connect(user=self.db_user, password=self.db_password, dsn=self.db_dsn)

    def _status(self, map_id):
        if map_id is None:
            return {"ok": False, "error": "map_id is required"}

        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT MAP_ID, MAP_TYPE, FR_TABLE, TO_TABLE, USE_YN, TRUNC_YN,
                       PRIORITY, STATUS, USER_EDITED, PRIOR_MAP_ID,
                       BATCH_CNT, ELAPSED_SECONDS, RETRY_COUNT,
                       TO_CHAR(CREATED_AT, 'YYYY-MM-DD HH24:MI:SS') AS CREATED_AT,
                       TO_CHAR(UPD_TS, 'YYYY-MM-DD HH24:MI:SS') AS UPD_TS
                FROM NEXT_MIG_INFO
                WHERE MAP_ID = :1
                """,
                [map_id],
            )
            row = cur.fetchone()

        if not row:
            return {"ok": False, "map_id": map_id, "error": "job not found"}

        return {
            "ok": True,
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
            "batch_cnt": row[10],
            "elapsed_seconds": row[11],
            "retry_count": row[12],
            "created_at": self._to_text(row[13]),
            "upd_ts": self._to_text(row[14]),
        }

    def _list_pending(self, limit):
        safe_limit = max(1, min(int(limit or 10), 50))
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                f"""
                SELECT * FROM (
                    SELECT MAP_ID, MAP_TYPE, FR_TABLE, TO_TABLE, PRIORITY, STATUS, RETRY_COUNT
                    FROM NEXT_MIG_INFO
                    WHERE UPPER(TRIM(NVL(USE_YN, 'N'))) = 'Y'
                      AND STATUS IS NULL
                    ORDER BY PRIORITY ASC, MAP_ID ASC
                ) WHERE ROWNUM <= {safe_limit}
                """
            )
            rows = cur.fetchall()

        return {
            "ok": True,
            "jobs": [
                {
                    "map_id": r[0],
                    "map_type": self._to_text(r[1]),
                    "fr_table": self._to_text(r[2]),
                    "to_table": self._to_text(r[3]),
                    "priority": r[4],
                    "status": self._to_text(r[5]),
                    "retry_count": r[6],
                }
                for r in rows
            ],
        }

    def _reset(self, map_id):
        if map_id is None:
            return {"ok": False, "error": "map_id is required"}

        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE NEXT_MIG_INFO
                SET STATUS = NULL,
                    RETRY_COUNT = 0,
                    BATCH_CNT = 0,
                    MIG_SQL = NULL,
                    VERIFY_SQL = NULL,
                    USER_EDITED = 'N',
                    UPD_TS = CURRENT_TIMESTAMP
                WHERE MAP_ID = :1
                """,
                [map_id],
            )
            rowcount = cur.rowcount
            conn.commit()

        return {"ok": rowcount > 0, "map_id": map_id, "updated_rows": rowcount}

    def _save_user_sql(self, map_id, command):
        if map_id is None:
            return {"ok": False, "error": "map_id is required"}
        mig_sql = command.get("mig_sql") or ""
        verify_sql = command.get("verify_sql") or ""
        if not mig_sql.strip():
            return {"ok": False, "map_id": map_id, "error": "mig_sql is required"}

        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE NEXT_MIG_INFO
                SET MIG_SQL = :1,
                    VERIFY_SQL = :2,
                    USER_EDITED = 'Y',
                    STATUS = NULL,
                    UPD_TS = CURRENT_TIMESTAMP
                WHERE MAP_ID = :3
                """,
                [mig_sql, verify_sql, map_id],
            )
            rowcount = cur.rowcount
            conn.commit()

        return {"ok": rowcount > 0, "map_id": map_id, "updated_rows": rowcount}

    def _analyze_failure(self, map_id):
        if map_id is None:
            return {"ok": False, "error": "map_id is required"}

        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT LOG_ID, LOG_TYPE, LOG_LEVEL, STEP_NAME, STATUS, MESSAGE,
                       TO_CHAR(COALESCE(UPD_TS, CREATED_AT), 'YYYY-MM-DD HH24:MI:SS') AS LOG_TIME
                FROM NEXT_MIG_LOG
                WHERE MAP_ID = :1
                ORDER BY LOG_ID DESC
                FETCH FIRST 5 ROWS ONLY
                """,
                [map_id],
            )
            rows = cur.fetchall()

        return {
            "ok": True,
            "map_id": map_id,
            "recent_logs": [
                {
                    "log_id": r[0],
                    "log_type": self._to_text(r[1]),
                    "log_level": self._to_text(r[2]),
                    "step_name": self._to_text(r[3]),
                    "status": self._to_text(r[4]),
                    "message": self._to_text(r[5]),
                    "log_time": self._to_text(r[6]),
                }
                for r in rows
            ],
        }

    def _run_migration_job(self, map_id, command):
        return {
            "ok": False,
            "map_id": map_id,
            "status": "NOT_IMPLEMENTED",
            "message": "Implement after status/reset/save_user_sql are verified.",
        }

    def _to_text(self, value):
        if value is None:
            return ""
        if hasattr(value, "read"):
            value = value.read()
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="ignore")
        return str(value)
```

## 첫 테스트 command

```json
{"action":"status","map_id":101}
```

## Agent 연결 전 직접 테스트할 command

```json
{"action":"list_pending","limit":5}
```

```json
{"action":"reset","map_id":101}
```

```json
{"action":"analyze_failure","map_id":101}
```
