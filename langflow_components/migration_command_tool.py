from __future__ import annotations

import ast
import json
import re
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from urllib.parse import quote_plus
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import IntInput, MessageTextInput, SecretStrInput, StrInput, Output
from lfx.schema.data import Data


class MigrationCommandTool(Component):
    display_name = "Migration Command Tool"
    description = "Controls SmartMigration DB migration jobs through Oracle metadata tables."
    name = "MigrationCommandTool"
    icon = "Database"

    _db_cache: dict[str, Any] = {}

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
    ]

    # ==================== 출력 정의 : Action들의 결과를 담은 result JSON 반환합니다. ====================
    outputs = [
        Output(display_name="Result", name="result", method="run_command"),
    ]

    # ==================== 액션 코드 ====================
    def run_command(self) -> Data:
        try:
            # Langflow에서 들어온 command_json을 먼저 dict로 바꾼다.
            # action은 어떤 함수를 실행할지 결정하고, map_id는 대부분의 action에서 작업 대상을 찾는 key로 사용한다.
            command = self._parse_command()
            action = (command.get("action") or "").strip().lower()
            map_id = command.get("map_id")

            # action 값에 따라 실제 처리 함수를 호출한다.
            # 각 함수는 result dict를 반환하고, 마지막에 Langflow Data로 감싸서 돌려준다.
            if action == "test_connection":
                result = self._test_connection()
            elif action == "status":
                result = self._status(map_id)
            elif action == "list_pending":
                result = self._list_pending(command.get("limit", 10))
            elif action == "get_table_ddl":
                result = self._get_table_ddl(command.get("table_name"), command.get("schema"))
            elif action == "generate_mig_sql":
                result = self._generate_mig_sql(map_id, command)
            elif action == "generate_verify_sql":
                result = self._generate_verify_sql(map_id, command)
            elif action == "preview_mig_prompt":
                result = self._preview_sql_prompt(map_id, command, prompt_kind="mig")
            elif action == "preview_verify_prompt":
                result = self._preview_sql_prompt(map_id, command, prompt_kind="verify")
            elif action == "reset":
                result = self._reset(map_id, command)
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
        # 예외가 나도 Langflow Agent가 읽을 수 있게 ok=False 형태의 JSON으로 반환한다.
        except Exception as exc:
            result = {"ok": False, "error": str(exc)}
            self.status = result
            return Data(data=result)

    # action="test_connection": DB와 LLM 연결을 확인한다.
    def _test_connection(self) -> dict[str, Any]:
        # DB는 실제 SELECT 1 쿼리로 연결 가능 여부를 확인한다.
        try:
            rows = self._normalize_query_rows(self._get_db().run("SELECT 1 AS OK FROM DUAL", include_columns=True))
            db_result = {"ok": True, "message": "DB connection OK", "result": rows}
        except Exception as exc:
            db_result = {"ok": False, "message": "DB connection failed", "error": str(exc)}

        # LLM은 API key/model/base_url 입력값을 사용해서 아주 짧은 chat/completions 호출을 보낸다.
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

    # action="status": map_id 기준 master/detail 상태를 조회한다.
    def _status(self, map_id: Any) -> dict[str, Any]:
        # map_id로 NEXT_MIG_INFO master 1건과 NEXT_MIG_INFO_DTL detail 목록을 같이 조회한다.
        map_id = self._require_map_id(map_id)
        job = self._load_job(map_id)
        if not job:
            return {"ok": False, "map_id": map_id, "error": "job not found"}
        details = self._load_details(map_id)
        return {"ok": True, "job": job, "details": details}

    # action="list_pending": 실행 가능한 migration 후보를 조회한다.
    def _list_pending(self, limit: Any) -> dict[str, Any]:
        # limit은 너무 크지 않게 1~50 사이로 제한한다.
        safe_limit = max(1, min(int(limit or 10), 50))
        map_table = self._system_table("NEXT_MIG_INFO")
        # STATUS IS NULL이고 USE_YN=Y인 건만 agent가 처리 가능한 후보로 본다.
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

    # action="get_table_ddl": Oracle 컬럼 메타데이터를 조회한다.
    def _get_table_ddl(self, table_name: Any, schema: Any = None) -> dict[str, Any]:
        # table_name은 필수이고, SCHEMA.TABLE 형태로 들어오면 schema/table을 분리해서 처리한다.
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

        # get_table_ddl 전용 row 값 추출 함수임!
        # SQLDatabase.run() 결과 key 대소문자가 환경마다 달라질 수 있어서 여기서만 보정한다.
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

    # action="generate_mig_sql": MIG_SQL을 생성한다.
    def _generate_mig_sql(self, map_id: Any, command: dict[str, Any]) -> dict[str, Any]:
        # map_id 필수임! 요청이 없거나 잘못된 경우 에러를 반환한다.
        map_id = self._require_map_id(map_id)
        job = self._load_job(map_id)
        if not job:
            return {"ok": False, "map_id": map_id, "error": "job not found"}

        # force_regenerate는 USER_EDITED=Y인 경우에도 MIG_SQL을 강제로 재생성할지 여부를 결정한다.
        # 사용자가 직접 저장한 MIG_SQL이 이미 있으면 기본적으로 그대로 유지한다.
        force_regenerate = self._as_bool(command.get("force_regenerate", False))
        user_edited = str(job.get("user_edited") or "").strip().upper() == "Y"
        existing_mig_sql = str(job.get("mig_sql") or "").strip()
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

        # dependency check를 수행한다. READY가 아니면 SQL을 만들지 않고 SKIP 또는 WAITING을 반환한다.
        dep = self._check_dependencies(job)
        if dep["status"] != "READY":
            final_status = "SKIP" if dep["status"] in {"FAIL", "SKIP"} else "WAITING"
            return {"ok": False, "map_id": map_id, "status": final_status, "message": dep["message"]}

        # mapping details는 NEXT_MIG_INFO_DTL에서 가져온 컬럼 매핑 정보다.
        # 이 정보가 prompt의 mapping_info로 들어가므로 없으면 MIG_SQL을 만들 수 없다.
        details = self._load_details(map_id)
        if not details:
            return {"ok": False, "map_id": map_id, "error": "No mapping details found"}

        generation_source = "llm"
        llm_error = ""

        # prompt를 렌더링해서 LLM을 호출하고, 반환값에서 migration_sql만 꺼낸 뒤 INSERT SQL인지 검증한다.
        try:
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

        return {
            "ok": True,
            "map_id": map_id,
            "status": "MIG_SQL_GENERATED",
            "generation_source": generation_source,
            "llm_error": llm_error,
            "mig_sql": mig_sql,
        }

    # action="generate_verify_sql": VERIFY_SQL을 생성한다.
    def _generate_verify_sql(self, map_id: Any, command: dict[str, Any]) -> dict[str, Any]:
        # map_id로 master job을 먼저 조회한다. VERIFY_SQL도 MIG_SQL과 같은 job 정보를 기반으로 만든다.
        map_id = self._require_map_id(map_id)
        job = self._load_job(map_id)
        if not job:
            return {"ok": False, "map_id": map_id, "error": "job not found"}

        # USER_EDITED=Y이면 사용자가 저장한 SQL을 우선한다.
        # VERIFY_SQL만 있어도 안 되고, 실행 대상인 MIG_SQL도 같이 있어야 한다.
        force_regenerate = self._as_bool(command.get("force_regenerate", False))
        user_edited = str(job.get("user_edited") or "").strip().upper() == "Y"
        existing_mig_sql = str(job.get("mig_sql") or "").strip()
        existing_verify_sql = str(job.get("verify_sql") or "").strip()
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

        # 선행 작업이 아직 PASS가 아니면 검증 SQL도 만들지 않는다.
        dep = self._check_dependencies(job)
        if dep["status"] != "READY":
            final_status = "SKIP" if dep["status"] in {"FAIL", "SKIP"} else "WAITING"
            return {"ok": False, "map_id": map_id, "status": final_status, "message": dep["message"]}

        # details는 prompt의 mapping_info로 들어간다. MIG_SQL과 VERIFY_SQL이 같은 매핑 기준을 보게 하기 위함이다.
        details = self._load_details(map_id)
        generation_source = "llm"
        llm_error = ""

        # LLM 응답에서 verification_sql을 꺼내고, SELECT/WITH 검증 SQL인지 확인한다.
        try:
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

        return {
            "ok": True,
            "map_id": map_id,
            "status": "VERIFY_SQL_GENERATED",
            "generation_source": generation_source,
            "llm_error": llm_error,
            "verify_sql": verify_sql,
        }

    # action="preview_mig_prompt" / "preview_verify_prompt": 치환된 prompt를 반환한다.
    def _preview_sql_prompt(self, map_id: Any, command: dict[str, Any], prompt_kind: str) -> dict[str, Any]:
        # 실제 LLM 호출 없이 prompt에 어떤 값이 들어가는지만 확인할 때 사용한다.
        map_id = self._require_map_id(map_id)
        job = self._load_job(map_id)
        if not job:
            return {"ok": False, "map_id": map_id, "error": "job not found"}
        details = self._load_details(map_id)
        # prompt_kind에 따라 MIG SQL prompt 또는 VERIFY SQL prompt를 선택한다.
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
        # db_updated=False, llm_called=False로 명확히 내려서 preview가 읽기 전용임을 agent가 알 수 있게 한다.
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

    # action="reset": 재실행을 위해 상태 값을 초기화한다.
    def _reset(self, map_id: Any, command: dict[str, Any]) -> dict[str, Any]:
        # reset은 DB 상태를 바꾸는 action이라 confirm=true 없으면 막는다.
        map_id = self._require_map_id(map_id)
        if not self._as_bool(command.get("confirm", False)):
            return {
                "ok": False,
                "map_id": map_id,
                "status": "CONFIRM_REQUIRED",
                "error": "reset requires confirm=true because it changes DB state.",
            }
        map_table = self._system_table("NEXT_MIG_INFO")
        # MIG_SQL/VERIFY_SQL은 유지하고 STATUS, RETRY_COUNT, BATCH_CNT만 초기화한다.
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

    # action="save_user_sql": 사용자가 수정한 SQL을 저장한다.
    def _save_user_sql(self, map_id: Any, command: dict[str, Any]) -> dict[str, Any]:
        # 사용자가 직접 수정한 SQL을 DB에 저장하는 유일한 action이다.
        # 저장하면 USER_EDITED=Y로 바뀌어서 자동 생성보다 사용자 SQL이 우선된다.
        map_id = self._require_map_id(map_id)
        if not self._as_bool(command.get("confirm", False)):
            return {
                "ok": False,
                "map_id": map_id,
                "status": "CONFIRM_REQUIRED",
                "error": "save_user_sql requires confirm=true because it changes DB state and sets USER_EDITED=Y.",
            }
        # command_json에서 mig_sql/verify_sql을 받아온다. MIG_SQL은 실행 SQL이라 필수다.
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

    # action="analyze_failure": 최신 실패 로그와 저장 SQL을 조회한다.
    def _analyze_failure(self, map_id: Any) -> dict[str, Any]:
        # 실패 원인 분석용으로 NEXT_MIG_INFO의 현재 SQL/상태와 NEXT_MIG_LOG 최신 10건을 같이 반환한다.
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

        # 로그 row를 agent가 읽기 쉬운 dict 목록으로 바꾼다.
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
        # 최신 로그 중 ERROR/FAIL/ROW_ERROR/JOB_FAIL에 해당하는 첫 번째 로그를 대표 실패 로그로 잡는다.
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

    # action="run_migration_job": SQL 생성, 실행, 검증까지 전체 사이클을 수행한다.
    def _run_migration_job(self, map_id: Any, command: dict[str, Any]) -> dict[str, Any]:
        # run_migration_job은 map_id 하나를 받아서 MIG_SQL 생성 -> 실행 -> VERIFY_SQL 생성 -> 검증까지 처리한다.
        map_id = self._require_map_id(map_id)
        started = time.perf_counter()
        # max_attempts는 command_json 값이 우선이고, 없으면 Langflow input의 default_max_attempts를 사용한다.
        max_attempts = max(1, int(command.get("max_attempts") or self.default_max_attempts or 1))

        # 최초 job 조회. 여기서 USE_YN/STATUS/의존성 같은 실행 가능 조건을 먼저 확인한다.
        job = self._load_job(map_id)
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

        # PRIOR_MAP_ID나 같은 TO_TABLE의 낮은 PRIORITY 작업이 아직 끝나지 않았는지 확인한다.
        dep = self._check_dependencies(job)
        if dep["status"] != "READY":
            final_status = "SKIP" if dep["status"] in {"FAIL", "SKIP"} else "WAITING"
            if final_status == "SKIP":
                self._update_job_status(map_id, "SKIP", 0, int(job.get("retry_count") or 0))
            self._write_log(map_id, "DEPENDENCY", "WARN", "DEP_CHECK", final_status, dep["message"])
            return {"ok": final_status == "WAITING", "map_id": map_id, "status": final_status, "message": dep["message"]}

        # steps는 agent에게 반환할 실행 이력이다. 각 attempt에서 어떤 단계가 성공/실패했는지 쌓는다.
        steps: list[dict[str, Any]] = []
        # last_mig_sql/last_verify_sql은 재시도 시 prompt의 last_sql로 넘기고, 최종 PASS/FAIL 시 DB에 저장할 SQL로도 사용한다.
        last_mig_sql = str(job.get("mig_sql") or "")
        last_verify_sql = str(job.get("verify_sql") or "")

        try:
            # 실행 직전에 job을 다시 읽어서 사용자가 직전에 저장한 SQL이나 상태 변경을 최대한 반영한다.
            job = self._load_job(map_id) or job
            user_edited = str(job.get("user_edited") or "").upper() == "Y"

            # 실제 full migration 시도 횟수로 BATCH_CNT를 1 증가시킨다.
            self._increment_batch_count(map_id)
            # generate_mig_sql/generate_verify_sql에 공통으로 넘길 생성 옵션이다.
            generation_command = {
                "force_regenerate": command.get("force_regenerate", False),
            }
            # last_failure는 직전 실패 상태/에러를 담는다. 다음 LLM 재생성 prompt의 last_error로 들어간다.
            last_failure: dict[str, Any] = {}
            # MIG_SQL 실행은 한 번 성공하면 같은 job 안에서 다시 INSERT하지 않는다. 이후 재시도는 VERIFY 쪽만 반복한다.
            mig_executed = False
            last_retry_count = 0

            for attempt in range(1, max_attempts + 1):
                # retry_count는 DB 로그에 남길 재시도 번호다. 첫 시도는 0, 두 번째 시도는 1이다.
                retry_count = attempt - 1
                last_retry_count = retry_count
                # attempt마다 job을 다시 읽는다. 사용자 SQL 저장 등 외부 변경을 반영하기 위해서다.
                job = self._load_job(map_id) or job
                user_edited = str(job.get("user_edited") or "").upper() == "Y"

                if not mig_executed:
                    if user_edited:
                        # USER_EDITED=Y이면 LLM을 호출하지 않고 DB에 저장된 MIG_SQL을 그대로 실행한다.
                        mig_sql = str(job.get("mig_sql") or "").strip()
                        if not mig_sql:
                            raise ValueError("USER_EDITED=Y but MIG_SQL is empty")
                        last_mig_sql = mig_sql
                        steps.append({"step": "generate_mig_sql", "attempt": attempt, "status": "SKIPPED_USER_EDITED"})
                    else:
                        mig_command = {
                            **generation_command,
                            "retry_count": retry_count,
                            "last_error": last_failure.get("error", ""),
                            "last_sql": last_mig_sql,
                        }
                        # _generate_mig_sql은 dict를 반환한다. 여기서는 ok와 mig_sql 값만 꺼내서 다음 실행 단계로 넘긴다.
                        mig_result = self._generate_mig_sql(map_id, mig_command)
                        steps.append({"step": "generate_mig_sql", "attempt": attempt, **self._summary_result(mig_result)})
                        # generate_mig_sql에서 ok=False를 받은 경우, last_failure에 기록하고 다음 attempt에서 재시도한다.
                        if not mig_result.get("ok"):
                            last_failure = {"status": "FAIL-INSERT", "error": mig_result.get("error") or "MIG_SQL generation failed"}
                            self._write_retry_log(map_id, "GENERATE_MIG_SQL", "FAIL-INSERT", str(last_failure["error"]), retry_count)
                            if attempt < max_attempts:
                                continue
                            break
                        # 생성된 mig_sql은 DB에 바로 저장하지 않고 메모리 변수에만 담는다.
                        # 최종 PASS/FAIL이 확정되면 _save_final_sql에서 NEXT_MIG_INFO에 저장한다.
                        last_mig_sql = str(mig_result.get("mig_sql") or "")
                        self._write_log(
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
                        # _execute_mig_sql_once는 job dict의 mig_sql을 읽어서 실행하므로, 방금 만든 last_mig_sql을 job에 주입한다.
                        job = {**job, "mig_sql": last_mig_sql}
                        mig_exec_result = self._execute_mig_sql_once(job, retry_count)
                        steps.append({"step": "execute_mig_sql", "attempt": attempt, **self._summary_result(mig_exec_result)})
                        # INSERT가 성공하면 이후 attempt에서는 MIG_SQL을 다시 실행하지 않기 위해 True로 둔다.
                        mig_executed = True
                    except Exception as exc:
                        # INSERT 실패는 FAIL-INSERT로 보고, 에러와 실패 SQL을 로그에 남긴 뒤 다음 attempt에서 MIG_SQL부터 다시 생성한다.
                        last_failure = {"status": "FAIL-INSERT", "error": str(exc)}
                        steps.append({"step": "execute_mig_sql", "attempt": attempt, "ok": False, **last_failure})
                        self._write_retry_log(map_id, "SQL_EXEC", "FAIL-INSERT", str(exc), retry_count, last_mig_sql)
                        if attempt < max_attempts:
                            continue
                            break

                # VERIFY_SQL 생성 전에도 job을 다시 읽는다. 사용자가 verify_sql을 저장했을 수 있기 때문이다.
                job = self._load_job(map_id) or job
                user_edited = str(job.get("user_edited") or "").upper() == "Y"
                verify_sql = str(job.get("verify_sql") or "").strip()
                if user_edited and verify_sql:
                    # USER_EDITED=Y이고 VERIFY_SQL도 있으면 LLM을 호출하지 않고 저장된 VERIFY_SQL을 사용한다.
                    last_verify_sql = verify_sql
                    steps.append({"step": "generate_verify_sql", "attempt": attempt, "status": "SKIPPED_USER_EDITED"})
                else:
                    verify_command = {
                        **generation_command,
                        "retry_count": retry_count,
                        "last_error": last_failure.get("error", ""),
                        "last_sql": last_verify_sql,
                    }
                    # VERIFY_SQL 생성도 dict로 결과를 받고, ok와 verify_sql만 다음 검증 단계에서 사용한다.
                    verify_result = self._generate_verify_sql(map_id, verify_command)
                    steps.append({"step": "generate_verify_sql", "attempt": attempt, **self._summary_result(verify_result)})
                    # VERIFY_SQL 생성 실패는 FAIL-TEST로 보고 다음 attempt에서 다시 생성한다.
                    if not verify_result.get("ok"):
                        last_failure = {"status": "FAIL-TEST", "error": verify_result.get("error") or "VERIFY_SQL generation failed"}
                        self._write_retry_log(map_id, "GENERATE_VERIFY_SQL", "FAIL-TEST", str(last_failure["error"]), retry_count)
                        if attempt < max_attempts:
                            continue
                        break
                    # 생성된 VERIFY_SQL도 최종 상태 확정 전까지는 메모리 변수에만 보관한다.
                    last_verify_sql = str(verify_result.get("verify_sql") or "")
                    self._write_log(
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
                    # _execute_verify_sql_once는 job dict의 verify_sql을 읽어서 실행하므로 last_verify_sql을 job에 주입한다.
                    job = {**job, "verify_sql": last_verify_sql}
                    verify_exec_result = self._execute_verify_sql_once(job)
                    steps.append({"step": "execute_verify_sql", "attempt": attempt, **self._summary_result(verify_exec_result)})
                    if verify_exec_result.get("ok"):
                        # 검증까지 PASS면 마지막으로 사용한 MIG_SQL/VERIFY_SQL을 DB에 저장하고 STATUS=PASS로 마감한다.
                        elapsed = int(time.perf_counter() - started)
                        self._save_final_sql(map_id, last_mig_sql, last_verify_sql)
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

                    # 검증 SQL은 실행됐지만 결과 row에 0이 아닌 값이 있으면 FAIL-TEST로 재시도한다.
                    last_failure = {"status": "FAIL-TEST", "error": verify_exec_result.get("message") or "Verification failed"}
                    self._write_retry_log(map_id, "VERIFY", "FAIL-TEST", str(last_failure["error"]), retry_count, verify_exec_result.get("verify_sql"))
                    if attempt < max_attempts:
                        continue
                    break
                except Exception as exc:
                    # VERIFY_SQL 실행 자체가 실패한 경우도 FAIL-TEST로 보고, 마지막 verify_sql을 로그에 남긴다.
                    last_failure = {"status": "FAIL-TEST", "error": str(exc)}
                    steps.append({"step": "execute_verify_sql", "attempt": attempt, "ok": False, **last_failure})
                    self._write_retry_log(map_id, "VERIFY", "FAIL-TEST", str(exc), retry_count, last_verify_sql)
                    if attempt < max_attempts:
                        continue
                    break

            # 여기까지 왔다는 것은 max_attempts 안에 PASS하지 못했다는 뜻이다.
            # 최종 실패여도 마지막으로 사용한 SQL은 저장해서 사용자가 분석/수정할 수 있게 한다.
            final_status = str(last_failure.get("status") or "FAIL")
            elapsed = int(time.perf_counter() - started)
            self._save_final_sql(map_id, last_mig_sql, last_verify_sql)
            self._update_job_status(map_id, final_status, elapsed, last_retry_count)
            self._write_log(
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
        except Exception as exc:
            # 예상하지 못한 예외도 작업 상태를 FAIL로 남기고, 지금까지 확보한 SQL은 저장한다.
            elapsed = int(time.perf_counter() - started)
            self._save_final_sql(map_id, last_mig_sql, last_verify_sql)
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

    # ======================================================================
    # 공통 코드
    # ======================================================================
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
        # Langflow input으로 받은 DB 접속 정보를 SQLAlchemy Oracle URI로 조립한다.
        # username/password는 URL에 들어가므로 quote_plus로 안전하게 인코딩한다.
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
        # 같은 DB 접속 정보는 _db_cache에서 재사용한다. action이 여러 번 호출돼도 매번 engine을 새로 만들지 않기 위함이다.
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
        self.db = self._db_cache[cache_key]
        return self.db

    def _ensure_runtime_dependencies(self) -> None:
        # DB 연결에 필요한 패키지가 Langflow 런타임에 설치되어 있는지 import로만 확인한다.
        import langchain_community
        import sqlalchemy
        import oracledb

    def _post_json(self, url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        # LLM gateway로 HTTP POST를 보내고 JSON 응답을 dict로 반환한다.
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
        # SQLDatabase.run() 결과가 list/tuple/string 등으로 달라질 수 있어서 agent가 읽기 쉬운 list[dict]로 맞춘다.
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

    @contextmanager
    def _connect(self):
        # UPDATE/INSERT처럼 commit이 필요한 작업은 SQLDatabase 내부 engine의 raw connection을 사용한다.
        db = self._get_db()
        engine = getattr(db, "_engine", None) or getattr(db, "engine", None)
        if engine is None:
            raise ValueError("SQLDatabase engine is not available")
        conn = engine.raw_connection()
        try:
            yield conn
        finally:
            conn.close()

    def _render_sql_prompt(
        self,
        template: str,
        job: dict[str, Any],
        details: list[dict[str, Any]],
        command: dict[str, Any],
    ) -> str:
        source_context = self._build_source_context(job)
        # Langflow prompt input에 있는 {placeholder}들을 아래 값으로 치환한다.
        # job은 NEXT_MIG_INFO에서, details는 NEXT_MIG_INFO_DTL에서, command는 현재 action 요청에서 온 값이다.
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

    # 재시도 할 때 이전 에러와 SQL을 포함한 컨텍스트를 생성한다.
    def _build_retry_context(self, last_error: str, last_sql: str, retry_count: Any = None) -> str:
        # last_error/last_sql은 run_migration_job의 last_failure, last_mig_sql, last_verify_sql에서 넘어온다.
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
        # Langflow 화면에서 입력한 prompt template이 비어 있으면 SQL 생성을 진행하지 않는다.
        value = str(getattr(self, attr_name, "") or "").strip()
        if not value:
            raise ValueError(f"{display_name} input is required for SQL generation")
        return value

    def _format_mapping_info(self, details: list[dict[str, Any]]) -> str:
        # NEXT_MIG_INFO_DTL의 FR_COL -> TO_COL 매핑을 prompt에 넣을 텍스트로 바꾼다.
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
        # source/target 컬럼 정보를 prompt에 넣기 위해 DDL information 블록을 만든다.
        blocks = ["[DDL information]"]
        for label, table_name in [("Source", from_table), ("Target", to_table)]:
            try:
                columns = self._table_columns_for_prompt(table_name)
            except Exception as exc:
                columns = f"Unable to load columns: {exc}"
            blocks.append(f"- {label} {table_name}:\n{columns}")
        return "\n".join(blocks)

    def _build_source_context(self, job: dict[str, Any]) -> dict[str, str]:
        # FR_TABLE이 일반 테이블/조인인지 COMPLEX 쿼리인지 구분해서 prompt에 넣을 source 정보를 만든다.
        map_type = str(job.get("map_type") or "").strip().upper()
        raw_source = str(job.get("fr_table") or "").strip()
        qualified_source = self._qualify_source_expression(raw_source)
        if map_type == "COMPLEX":
            # COMPLEX는 FR_TABLE 자체를 inline view로 감싸고 alias SRC를 붙여서 LLM이 그대로 사용하게 한다.
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
        # prompt에 넣을 컬럼 목록을 조회한다. 복잡한 source query는 매핑 정보만 기준으로 삼도록 안내한다.
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
        # 반환값은 아직 SQL 검증 전의 LLM raw content다.
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
        # LLM 응답에서 SQL만 꺼낸다. JSON 응답이면 key 값으로, markdown fence면 fence 안쪽으로 추출한다.
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
        # LLM이 JSON fence를 붙이거나 앞뒤 설명을 붙여도 JSON object 부분만 최대한 찾아서 파싱한다.
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
        # MIG_SQL은 INSERT 한 문장만 허용한다. 실행 전에 위험한 DML/DDL 키워드를 걸러낸다.
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
        # VERIFY_SQL은 SELECT/WITH 한 문장만 허용한다. 검증 쿼리가 데이터를 바꾸면 안 되기 때문이다.
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

    def _load_job(self, map_id: int) -> dict[str, Any] | None:
        # NEXT_MIG_INFO 단건 조회 결과를 Python dict로 변환한다.
        # action 함수들은 이 dict의 mig_sql/status/user_edited 같은 값을 기준으로 분기한다.
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
        # NEXT_MIG_INFO_DTL의 FR_COL -> TO_COL 목록을 MAP_DTL 순서로 가져온다.
        # 이 결과가 prompt의 mapping_info로 들어간다.
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
        # 반환 status가 READY가 아니면 SQL 생성/실행 단계로 넘어가지 않는다.
        # 먼저 PRIOR_MAP_ID를 확인하고, 그 다음 같은 target table의 priority 의존성을 확인한다.
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
        # 현재 job의 to_table/priority/map_id를 기준으로 선행 작업을 조회한다.
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

    def _save_final_sql(self, map_id: int, mig_sql: str, verify_sql: str) -> None:
        # run_migration_job의 최종 PASS/FAIL 시점에 마지막으로 사용한 SQL을 NEXT_MIG_INFO에 저장한다.
        # 비어 있는 SQL은 update 대상에서 제외해서 기존 값을 불필요하게 덮어쓰지 않는다.
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
        map_table = self._system_table("NEXT_MIG_INFO")
        with self._connect() as conn:
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

    def _execute_mig_sql_once(self, job: dict[str, Any], retry_count: int) -> dict[str, Any]:
        # TRUNC_YN=Y이면 target truncate를 먼저 수행한 뒤 MIG_SQL을 실행한다.
        # job["mig_sql"]은 DB에서 온 값일 수도 있고, run_migration_job에서 방금 주입한 last_mig_sql일 수도 있다.
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
        # job["verify_sql"]을 실행해서 결과값이 모두 0인지 확인한다.
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
        # steps에 너무 큰 SQL 본문을 넣지 않기 위해 핵심 상태값만 요약한다.
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
        # 재시도 중 발생한 실패는 ROW_ERROR/WARN 형태로 NEXT_MIG_LOG에 남긴다.
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
        # target_schema 입력값이 있으면 TO_TABLE에 schema prefix를 붙여 truncate한다.
        target = self._qualify_table(job["to_table"], self.target_schema)
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(f"TRUNCATE TABLE {target}")
            conn.commit()

    def _execute_sql_script(self, sql_script: str) -> int:
        # 세미콜론 기준으로 나눈 SQL을 순서대로 실행하고, 전체 affected row 수를 합산한다.
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
        # 검증 SQL은 숫자 차이값을 반환한다고 가정한다.
        # 반환 row의 모든 value가 0이면 성공, 하나라도 0이 아니면 실패다.
        # result_rows는 실패했을 때 agent가 어떤 값이 달랐는지 설명하는 데 사용한다.
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
        # 최종 상태와 실행 시간, retry_count를 NEXT_MIG_INFO에 저장한다.
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
        # run_migration_job이 시작될 때마다 BATCH_CNT를 1 올린다.
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
        # NEXT_MIG_LOG insert 컬럼 순서는 운영 테이블 기준에 맞춰 유지한다.
        # generate_sql에는 LLM이 만든 SQL 또는 실패 당시 SQL을 넣어서 나중에 추적할 수 있게 한다.
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
            pass

    def _split_sql_script(self, sql_script: str) -> list[str]:
        # 문자열 안의 세미콜론은 무시하고, 실제 SQL 문장 구분 세미콜론만 기준으로 나눈다.
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
        # VERIFY_SQL 결과값이 숫자 0인지 판단한다. 문자열/Decimal 형태 모두 처리한다.
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
        # map_id는 대부분의 action에서 필수다. 비어 있으면 바로 에러를 낸다.
        if map_id is None or str(map_id).strip() == "":
            raise ValueError("map_id is required")
        return int(map_id)

    def _system_table(self, table_name: str) -> str:
        # system_schema 입력값이 있으면 시스템 테이블명에 schema prefix를 붙인다.
        return self._qualify_table(table_name, self.system_schema)

    def _qualify_table(self, table_name: str, schema: str | None) -> str:
        # 이미 SCHEMA.TABLE 형태거나 schema가 비어 있으면 원래 table_name을 그대로 사용한다.
        clean = str(table_name or "").strip()
        clean_schema = str(schema or "").strip().upper()
        if not clean:
            raise ValueError("table_name is empty")
        if "." in clean or not clean_schema:
            return clean
        self._validate_identifier(clean_schema, "schema")
        return f"{clean_schema}.{clean}"

    def _qualify_source_expression(self, fr_table: str) -> str:
        # source_schema가 입력된 경우 FR_TABLE 안의 물리 테이블명에 schema prefix를 붙인다.
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
        # 일반 table/join 형태의 FR_TABLE에서 schema prefix를 붙일 대상 테이블명을 뽑는다.
        parts = re.split(r"\b(?:(?:LEFT|RIGHT|FULL|INNER|CROSS)\s+(?:OUTER\s+)?)?JOIN\b", fr_table, flags=re.I)
        tables: list[str] = []
        for part in parts:
            part = re.split(r"\bON\b", part, flags=re.I)[0].strip()
            tokens = part.split()
            if tokens and tokens[0].upper() not in {"SELECT", "WITH", "FROM", "("}:
                tables.append(tokens[0])
        return tables

    def _validate_identifier(self, value: str, label: str) -> None:
        # schema/table 이름에 허용하지 않는 문자가 들어오면 SQL에 직접 붙이기 전에 막는다.
        if not re.fullmatch(r"[A-Z][A-Z0-9_$#]*", value):
            raise ValueError(f"Invalid {label}: {value}")

    def _to_text(self, value: Any) -> str:
        # Oracle CLOB/bytes/None 값을 화면에 반환 가능한 문자열로 통일한다.
        if value is None:
            return ""
        if hasattr(value, "read"):
            value = value.read()
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="ignore")
        return str(value)




