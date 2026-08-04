from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from typing import Any
from urllib.parse import quote_plus

from lfx.custom.custom_component.component import Component
from lfx.io import IntInput, MessageTextInput, SecretStrInput, StrInput, Output
from lfx.schema.data import Data


class SqlConversionCommandTool(Component):
    display_name = "SQL Conversion Command Tool"
    description = "Generates TO_SQL for SmartMigration SQL conversion jobs."
    name = "SqlConversionCommandTool"
    icon = "FileCode"

    _db_cache: dict[str, Any] = {}


    # ==================== 입력 정의: DB/LLM 연결 정보와 command JSON, 프롬포트를 입력받는다. ====================
    inputs = [
        MessageTextInput(
            name="command_json",
            display_name="Command JSON",
            required=True,
            tool_mode=True,
            info='JSON command. Example: {"action":"status","space_nm":"SFA","sql_id":"selectUser"}',
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
            info="OpenAI-compatible LLM gateway base URL. Only OpenAI-compatible chat/completions is supported.",
        ),
        SecretStrInput(name="llm_api_key", display_name="LLM API Key", required=False),
        StrInput(name="llm_model", display_name="LLM Model", value="claude-haiku-4-5-20251001", required=False),
        IntInput(name="llm_max_tokens", display_name="LLM Max Tokens", value=4096, required=False),
        IntInput(
            name="llm_timeout_seconds",
            display_name="LLM Timeout Seconds",
            value=900,
            required=False,
            info="HTTP timeout for LLM API calls. Default: 900 seconds.",
        ),
        MessageTextInput(
            name="to_sql_prompt",
            display_name="TO SQL Prompt",
            required=True,
            info="Prompt template for generate_to_sql_text. Use placeholders: {from_sql}, {mapping_schema_text}, {source_schema}, {target_schema}, {correct_sql_hint_json}, {last_error}.",
        ),
        MessageTextInput(
            name="bind_sql_prompt",
            display_name="BIND SQL Prompt",
            required=False,
            info="Prompt template for BIND_SQL generation. Use placeholders: {from_sql}, {to_sql}, {mapping_schema_text}, {source_schema}, {target_schema}, {last_error}.",
        ),
        MessageTextInput(
            name="test_sql_prompt",
            display_name="TEST SQL Prompt",
            required=False,
            info="Prompt template for TEST_SQL generation. Use placeholders: {from_sql}, {to_sql}, {bind_sql}, {bind_set}, {mapping_schema_text}, {source_schema}, {target_schema}, {last_error}.",
        ),
        MessageTextInput(
            name="verify_sql_prompt",
            display_name="VERIFY SQL Prompt",
            required=False,
            info="Prompt template for preview_verify_prompt. Use placeholders: {from_sql}, {to_sql}, {mapping_schema_text}, {source_schema}, {target_schema}, {correct_sql_hint_json}, {last_error}.",
        ),
        StrInput(
            name="system_schema",
            display_name="System Schema",
            required=False,
            info="Schema containing NEXT_SQL_INFO/NEXT_MIG_INFO/NEXT_MIG_INFO_DTL/NEXT_MIG_RAG_INFO. Leave blank for current user.",
        ),
        StrInput(
            name="source_schema",
            display_name="Source Schema",
            required=False,
            info="Optional AS-IS schema hint for matching source tables in FR_SQL/EDIT_FR_SQL.",
        ),
        StrInput(
            name="target_schema",
            display_name="Target Schema",
            required=False,
            info="Target schema to apply to physical TO-BE tables.",
        ),
    ]


    # ==================== 출력 정의: action 처리 결과를 result JSON으로 반환한다. ====================
    outputs = [
        Output(display_name="Result", name="result", method="run_command"),
    ]


    # ==================== 액션 코드 ====================
    # Langflow에서 받은 action 값을 if/elif로 분기한다.
    def run_command(self) -> Data:
        try:
            command = self._parse_command()
            action = str(command.get("action") or "").strip().lower()

            if action == "test_connection":
                result = self._test_connection()
            elif action == "status":
                result = self._status(command.get("space_nm"), command.get("sql_id"))
            elif action == "list_pending":
                result = self._list_pending(command.get("limit", 20))
            elif action == "generate_to_sql_text":
                result = self._generate_to_sql_text(command.get("space_nm"), command.get("sql_id"), command.get("last_error"))
            elif action == "preview_conversion_prompt":
                result = self._preview_conversion_prompt(command.get("space_nm"), command.get("sql_id"), command.get("last_error"))
            elif action == "preview_verify_prompt":
                result = self._preview_verify_prompt(command.get("space_nm"), command.get("sql_id"), command.get("to_sql"), command.get("last_error"))
            elif action == "run_sql_conversion_job":
                result = self.run_sql_conversion_job(command.get("sql_id"), command.get("space_nm"), command)
            else:
                result = {"ok": False, "error": f"Unsupported action: {action}"}

            self.status = result
            return Data(data=result)

        except Exception as exc:
            result = {"ok": False, "error": str(exc)}
            self.status = result
            return Data(data=result)


    # action="test_connection": DB와 LLM 연결 상태를 확인한다.
    def _test_connection(self) -> dict[str, Any]:
        try:
            result = self._get_db().run("SELECT 1 AS OK FROM DUAL", include_columns=True)
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


    # action="status": space_nm/sql_id 기준 SQL Conversion 작업 상태를 조회한다.
    def _status(self, space_nm: Any, sql_id: Any) -> dict[str, Any]:
        job = self._load_job(space_nm, sql_id)
        return {"ok": bool(job), "job": job, "error": "" if job else "job not found"}


    # action="list_pending": list SQL Conversion jobs where STATUS_CONVERSION is NULL.
    def _list_pending(self, limit: Any) -> dict[str, Any]:
        safe_limit = max(1, min(int(limit or 20), 100))
        table = self._qualify_table("NEXT_SQL_INFO", self.system_schema)
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                f"""
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
                WHERE ROWNUM <= :1
                """,
                [safe_limit],
            )
            rows = cur.fetchall()
        jobs = [
            {
                "tag_kind": self._to_text(r[0]),
                "space_nm": self._to_text(r[1]),
                "sql_id": self._to_text(r[2]),
                "status_conversion": self._to_text(r[3]),
                "fr_sql_preview": self._to_text(r[4]),
                "fr_sql_len": r[5],
                "edit_fr_sql_len": r[6],
                "priority": r[7],
                "upd_ts": self._to_text(r[8]),
            }
            for r in rows
        ]
        return {"ok": True, "count": len(jobs), "jobs": jobs}


    # action="generate_to_sql_text": TO_SQL을 생성해서 채팅 응답으로 반환한다.
    def _generate_to_sql_text(self, space_nm: Any, sql_id: Any, last_error: Any = None) -> dict[str, Any]:
        return self._generate_to_sql(space_nm, sql_id, last_error=last_error)

    # TO_SQL 생성 공통 함수: 채팅 요청과 job 실행 내부에서 같이 사용한다.
    def _generate_to_sql(
        self,
        space_nm: Any,
        sql_id: Any,
        last_error: Any = None,
        last_sql: Any = None,
        retry_count: Any = 0,
        force_regenerate: bool = False,
    ) -> dict[str, Any]:
        job = self._load_job(space_nm, sql_id)
        if not job:
            return {"ok": False, "error": "job not found"}

        edit_fr_sql = str(job.get("edit_fr_sql") or "").strip()
        fr_sql = str(job.get("fr_sql") or "").strip()
        source_sql = edit_fr_sql if edit_fr_sql else fr_sql
        if not source_sql:
            return {"ok": False, "space_nm": job.get("space_nm"), "sql_id": job.get("sql_id"), "error": "source SQL is empty"}

        mapping_schema_text, map_ids, fr_tables, rag_rule_count = self._build_mapping_schema_text(job)
        prompt = self._render_to_sql_prompt(
            from_sql=source_sql,
            mapping_schema_text=mapping_schema_text,
            source_schema=str(self.source_schema or "").strip() or "UNKNOWN",
            target_schema=str(self.target_schema or "").strip() or "UNKNOWN",
            correct_sql_hint_json="[]",
            last_error=str(last_error or "None"),
        )
        to_sql = self._sanitize_to_sql(self._call_llm(prompt))

        return {
            "ok": True,
            "space_nm": job.get("space_nm"),
            "sql_id": job.get("sql_id"),
            "status": "TO_SQL_GENERATED",
            "db_updated": False,
            "fr_tables": fr_tables,
            "map_ids": map_ids,
            "rag_rule_count": rag_rule_count,
            "to_sql": to_sql,
        }

    # action="preview_conversion_prompt": LLM 호출 없이 SQL Conversion prompt를 미리 확인한다.
    def _preview_conversion_prompt(self, space_nm: Any, sql_id: Any, last_error: Any = None) -> dict[str, Any]:

        job = self._load_job(space_nm, sql_id)
        if not job:
            return {"ok": False, "error": "job not found"}


        edit_fr_sql = str(job.get("edit_fr_sql") or "").strip()
        fr_sql = str(job.get("fr_sql") or "").strip()
        source_sql = edit_fr_sql if edit_fr_sql else fr_sql
        if not source_sql:
            return {"ok": False, "space_nm": job.get("space_nm"), "sql_id": job.get("sql_id"), "error": "source SQL is empty"}


        mapping_schema_text, map_ids, fr_tables, rag_rule_count = self._build_mapping_schema_text(job)
        prompt = self._render_to_sql_prompt(
            from_sql=source_sql,
            mapping_schema_text=mapping_schema_text,
            source_schema=str(self.source_schema or "").strip() or "UNKNOWN",
            target_schema=str(self.target_schema or "").strip() or "UNKNOWN",
            correct_sql_hint_json="[]",
            last_error=str(last_error or "None"),
        )
        return {
            "ok": True,
            "action": "preview_conversion_prompt",
            "space_nm": job.get("space_nm"),
            "sql_id": job.get("sql_id"),
            "prompt_kind": "conversion",
            "prompt_length": len(prompt),
            "prompt": prompt,
            "db_updated": False,
            "llm_called": False,
            "fr_tables": fr_tables,
            "map_ids": map_ids,
            "rag_rule_count": rag_rule_count,
        }


    # action="preview_verify_prompt": LLM 호출 없이 검증 prompt를 미리 확인한다.
    def _preview_verify_prompt(self, space_nm: Any, sql_id: Any, to_sql: Any = None, last_error: Any = None) -> dict[str, Any]:

        job = self._load_job(space_nm, sql_id)
        if not job:
            return {"ok": False, "error": "job not found"}


        edit_fr_sql = str(job.get("edit_fr_sql") or "").strip()
        fr_sql = str(job.get("fr_sql") or "").strip()
        source_sql = edit_fr_sql if edit_fr_sql else fr_sql
        to_sql = str(to_sql or job.get("to_sql") or "").strip()
        if not source_sql:
            return {"ok": False, "space_nm": job.get("space_nm"), "sql_id": job.get("sql_id"), "error": "source SQL is empty"}
        if not to_sql:
            return {
                "ok": False,
                "space_nm": job.get("space_nm"),
                "sql_id": job.get("sql_id"),
                "error": "to_sql is required for preview_verify_prompt",
            }


        mapping_schema_text, map_ids, fr_tables, rag_rule_count = self._build_mapping_schema_text(job)
        prompt = self._render_verify_prompt(
            from_sql=source_sql,
            to_sql=to_sql,
            mapping_schema_text=mapping_schema_text,
            source_schema=str(self.source_schema or "").strip() or "UNKNOWN",
            target_schema=str(self.target_schema or "").strip() or "UNKNOWN",
            correct_sql_hint_json="[]",
            last_error=str(last_error or "None"),
        )
        return {
            "ok": True,
            "action": "preview_verify_prompt",
            "space_nm": job.get("space_nm"),
            "sql_id": job.get("sql_id"),
            "prompt_kind": "verify",
            "prompt_length": len(prompt),
            "prompt": prompt,
            "db_updated": False,
            "llm_called": False,
            "fr_tables": fr_tables,
            "map_ids": map_ids,
            "rag_rule_count": rag_rule_count,
        }

    # action="run_sql_conversion_job": run the full SQL Conversion flow: TO_SQL, BIND_SQL, TEST_SQL.
    def run_sql_conversion_job(self, sql_id: str, space_nm: str, command: dict[str, Any]) -> dict[str, Any]:

        # sql_id and space_nm are required because this job has no single numeric id.
        if (sql_id is None or str(sql_id).strip() == "") or (space_nm is None or str(space_nm).strip() == ""):
            return {"ok": False, "error": "sql_id and space_nm are required for run_sql_conversion_job"}
        sql_id = str(sql_id or "").strip()
        space_nm = str(space_nm or "").strip()

        # started is used for elapsed_seconds in response and NEXT_SQL_LOG.
        started = time.perf_counter()
        max_attempts = max(1, int(command.get("max_attempts") or 1))

        # Load the job first and block rows that are already processed.
        job = self._load_job(space_nm, sql_id)
        if not job:
            return {"ok": False, "space_nm": space_nm, "sql_id": sql_id, "error": "job not found"}

        # SQL Conversion runs only when STATUS_CONVERSION is NULL.
        current_status = str(job.get("status_conversion") or "").strip().upper()
        if current_status:
            return {"ok": False, "space_nm": job.get("space_nm"), "sql_id": job.get("sql_id"), "status": job.get("status_conversion"), "error": "run_sql_conversion_job is allowed only when STATUS_CONVERSION is NULL."}

        steps: list[dict[str, Any]] = []
        last_to_sql = str(job.get("to_sql") or "")
        last_bind_sql = str(job.get("bind_sql") or "")
        last_bind_set = str(job.get("bind_set") or "")
        last_test_sql = str(job.get("test_sql") or "")
        last_retry_count = 0

        try:
            # mapping_schema_text is shared by TO_SQL and TEST_SQL prompts.
            mapping_schema_text, map_ids, fr_tables, rag_rule_count = self._build_mapping_schema_text(job)
            last_failure: dict[str, Any] = {}
            to_sql_executed = False
            bind_sql_executed = False

            # attempt starts at 1, while retry_count and ATTEMPT_NO start at 0.
            for attempt in range(1, max_attempts + 1):
                retry_count = attempt - 1
                last_retry_count = retry_count
                job = self._load_job(space_nm, sql_id) or job
                user_edited = str(job.get("user_edited") or "").strip().upper() == "Y"
                tag_kind = str(job.get("tag_kind") or "").strip().upper()

                # TO_SQL step: use saved TO_SQL for USER_EDITED=Y, otherwise call LLM.
                if not to_sql_executed:
                    if user_edited:
                        to_sql = str(job.get("to_sql") or "").strip()
                        if not to_sql:
                            raise ValueError("USER_EDITED=Y but TO_SQL is empty")
                        last_to_sql = to_sql
                        steps.append({"step": "generate_to_sql", "attempt": attempt, "status": "SUCCESS-TOBE", "message": "USER_EDITED=Y. Existing TO_SQL was used."})
                    else:
                        to_sql_result = self._generate_to_sql(space_nm, sql_id, last_error=last_failure.get("error", ""))
                        if to_sql_result.get("ok"):
                            to_sql_result["status"] = "SUCCESS-TOBE"
                        steps.append({"step": "generate_to_sql", "attempt": attempt, **self._summary_result(to_sql_result)})
                        if not to_sql_result.get("ok"):
                            last_failure = {"status": "FAIL-TOBE", "error": to_sql_result.get("error") or "TO_SQL generation failed"}
                            self._write_log(sql_id, space_nm, "TO_SQL", "FAIL", "GENERATE_TO_SQL", str(last_failure["error"])[:3900], retry_count, last_to_sql, int(time.perf_counter() - started), "TO_SQL_PROMPT")
                            if attempt < max_attempts:
                                continue
                            break
                        last_to_sql = str(to_sql_result.get("to_sql") or "").strip()
                    to_sql_executed = True
                    self._write_log(sql_id, space_nm, "TO_SQL", "PASS", "GENERATE_TO_SQL", "TO_SQL generated", retry_count, last_to_sql, int(time.perf_counter() - started), "TO_SQL_PROMPT")

                # Non-SELECT SQL does not need BIND/TEST, so TO_SQL success completes conversion.
                if tag_kind != "SELECT":
                    elapsed = int(time.perf_counter() - started)
                    self._save_final_sql(sql_id, space_nm, last_to_sql, last_bind_sql, last_bind_set, last_test_sql)
                    self._update_job_status(sql_id, space_nm, "PASS-CONVERSION", elapsed, retry_count, status_tuning="READY")
                    self._write_log(sql_id, space_nm, "TO_SQL", "PASS", "FINAL", "SQL Conversion completed without BIND/TEST because TAG_KIND is not SELECT", retry_count, last_to_sql, elapsed)
                    return {"ok": True, "space_nm": space_nm, "sql_id": sql_id, "status": "PASS-CONVERSION", "status_tuning": "READY", "elapsed_seconds": elapsed, "retry_count": retry_count, "steps": steps, "to_sql": last_to_sql, "map_ids": map_ids, "fr_tables": fr_tables, "rag_rule_count": rag_rule_count}

                # BIND_SQL step: build a SELECT from FR_SQL and save its result rows as BIND_SET JSON.
                if not bind_sql_executed:
                    try:
                        bind_result = self._generate_bind_sql(job, last_to_sql, mapping_schema_text, last_failure.get("error", ""))
                        steps.append({"step": "generate_bind_sql", "attempt": attempt, **self._summary_result(bind_result)})
                        if not bind_result.get("ok"):
                            last_failure = {"status": "FAIL-BIND", "error": bind_result.get("error") or "BIND_SQL generation failed"}
                            self._write_log(sql_id, space_nm, "BIND_SQL", "FAIL", "GENERATE_BIND_SQL", str(last_failure["error"])[:3900], retry_count, last_bind_sql, int(time.perf_counter() - started), "BIND_SQL_PROMPT")
                            if attempt < max_attempts:
                                continue
                            break
                        last_bind_sql = str(bind_result.get("bind_sql") or "").strip()
                        self._write_log(sql_id, space_nm, "BIND_SQL", "PASS", "GENERATE_BIND_SQL", "BIND_SQL generated", retry_count, last_bind_sql, int(time.perf_counter() - started), "BIND_SQL_PROMPT")
                        bind_exec_result = self._execute_bind_sql(last_bind_sql)
                        steps.append({"step": "execute_bind_sql", "attempt": attempt, **self._summary_result(bind_exec_result)})
                        last_bind_set = str(bind_exec_result.get("bind_set") or "")
                        bind_sql_executed = True
                        self._write_log(sql_id, space_nm, "BIND_SET", "PASS", "EXECUTE_BIND_SQL", "BIND_SQL executed", retry_count, last_bind_set, int(time.perf_counter() - started))
                    except Exception as exc:
                        last_failure = {"status": "FAIL-BIND", "error": str(exc)}
                        steps.append({"step": "execute_bind_sql", "attempt": attempt, "ok": False, **last_failure})
                        self._write_log(sql_id, space_nm, "BIND_SQL", "FAIL", "EXECUTE_BIND_SQL", str(exc)[:3900], retry_count, last_bind_sql, int(time.perf_counter() - started))
                        if attempt < max_attempts:
                            continue
                        break

                # TEST_SQL step: compare FROM_COUNT and TO_COUNT for every CASE_NO.
                try:
                    test_result = self._generate_test_sql(job, last_to_sql, last_bind_sql, last_bind_set, mapping_schema_text, last_failure.get("error", ""))
                    steps.append({"step": "generate_test_sql", "attempt": attempt, **self._summary_result(test_result)})
                    if not test_result.get("ok"):
                        last_failure = {"status": "FAIL-TEST", "error": test_result.get("error") or "TEST_SQL generation failed"}
                        self._write_log(sql_id, space_nm, "TEST_SQL", "FAIL", "GENERATE_TEST_SQL", str(last_failure["error"])[:3900], retry_count, last_test_sql, int(time.perf_counter() - started), "TEST_SQL_PROMPT")
                        if attempt < max_attempts:
                            continue
                        break
                    last_test_sql = str(test_result.get("test_sql") or "").strip()
                    self._write_log(sql_id, space_nm, "TEST_SQL", "PASS", "GENERATE_TEST_SQL", "TEST_SQL generated", retry_count, last_test_sql, int(time.perf_counter() - started), "TEST_SQL_PROMPT")
                    test_exec_result = self._execute_test_sql(last_test_sql)
                    steps.append({"step": "execute_test_sql", "attempt": attempt, **self._summary_result(test_exec_result)})
                    if test_exec_result.get("ok"):
                        elapsed = int(time.perf_counter() - started)
                        self._save_final_sql(sql_id, space_nm, last_to_sql, last_bind_sql, last_bind_set, last_test_sql)
                        self._update_job_status(sql_id, space_nm, "PASS-CONVERSION", elapsed, retry_count, status_tuning="READY")
                        self._write_log(sql_id, space_nm, "TEST_SQL", "PASS", "EXECUTE_TEST_SQL", "SQL Conversion test passed", retry_count, last_test_sql, elapsed)
                        return {"ok": True, "space_nm": space_nm, "sql_id": sql_id, "status": "PASS-CONVERSION", "status_tuning": "READY", "elapsed_seconds": elapsed, "retry_count": retry_count, "steps": steps, "to_sql": last_to_sql, "bind_sql": last_bind_sql, "bind_set": last_bind_set, "test_sql": last_test_sql, "test_rows": test_exec_result.get("result_rows"), "map_ids": map_ids, "fr_tables": fr_tables, "rag_rule_count": rag_rule_count}
                    last_failure = {"status": "FAIL-TEST", "error": test_exec_result.get("message") or "TEST_SQL validation failed"}
                    self._write_log(sql_id, space_nm, "TEST_SQL", "FAIL", "EXECUTE_TEST_SQL", str(last_failure["error"])[:3900], retry_count, last_test_sql, int(time.perf_counter() - started))
                    if attempt < max_attempts:
                        continue
                    break
                except Exception as exc:
                    last_failure = {"status": "FAIL-TEST", "error": str(exc)}
                    steps.append({"step": "execute_test_sql", "attempt": attempt, "ok": False, **last_failure})
                    self._write_log(sql_id, space_nm, "TEST_SQL", "FAIL", "EXECUTE_TEST_SQL", str(exc)[:3900], retry_count, last_test_sql, int(time.perf_counter() - started))
                    if attempt < max_attempts:
                        continue
                    break

            # If all attempts end without PASS, save the last failure status and SQL values.
            final_status = str(last_failure.get("status") or "FAIL-CONVERSION")
            elapsed = int(time.perf_counter() - started)
            self._save_final_sql(sql_id, space_nm, last_to_sql, last_bind_sql, last_bind_set, last_test_sql)
            self._update_job_status(sql_id, space_nm, final_status, elapsed, last_retry_count)
            self._write_log(sql_id, space_nm, "ERROR", "FAIL", "FINAL", str(last_failure.get("error") or "Max attempts reached")[:3900], last_retry_count, last_test_sql or last_bind_sql or last_to_sql, elapsed)
            return {"ok": False, "space_nm": space_nm, "sql_id": sql_id, "status": final_status, "error": last_failure.get("error") or "Max attempts reached", "elapsed_seconds": elapsed, "retry_count": last_retry_count, "steps": steps}
        except Exception as exc:
            # Unexpected exceptions are also saved as final failure with the latest SQL values.
            elapsed = int(time.perf_counter() - started)
            self._save_final_sql(sql_id, space_nm, last_to_sql, last_bind_sql, last_bind_set, last_test_sql)
            self._update_job_status(sql_id, space_nm, "FAIL-CONVERSION", elapsed, last_retry_count)
            self._write_log(sql_id, space_nm, "ERROR", "FAIL", "RUN_FULL", str(exc)[:3900], last_retry_count, last_test_sql or last_bind_sql or last_to_sql, elapsed)
            return {"ok": False, "space_nm": space_nm, "sql_id": sql_id, "status": "FAIL-CONVERSION", "error": str(exc), "elapsed_seconds": elapsed, "retry_count": last_retry_count, "steps": steps}

    # ======================================================================
    # 공통 코드
    # ======================================================================
    # command_json을 dict로 변환하고 action/space_nm/sql_id 같은 실행 값을 해석한다.
    def _parse_command(self) -> dict[str, Any]:


        raw = self.command_json
        if isinstance(raw, dict):
            return raw
        text = str(raw or "").strip()
        if not text:
            raise ValueError("command_json is required")
        return json.loads(text)

    # DB 입력값을 Oracle SQLAlchemy connection string으로 조립한다.
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

    # 같은 DB 접속 정보는 _db_cache에서 재사용한다.
    def _get_db(self):
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

    # OpenAI 호환 LLM API에 JSON 요청을 보내고 응답 dict를 반환한다.
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

    # SQLDatabase 내부 engine에서 DB connection을 꺼내 cursor 작업에 사용한다.
    @contextmanager
    def _connect(self):
        db = self._get_db()
        with db._engine.connect() as conn:
            raw = conn.connection
            yield raw

    # NEXT_SQL_INFO에서 space_nm/sql_id에 해당하는 작업 row를 조회한다.
    def _load_job(self, space_nm: Any, sql_id: Any) -> dict[str, Any] | None:
        table = self._qualify_table("NEXT_SQL_INFO", self.system_schema)
        space_nm = str(space_nm or "").strip()
        sql_id = str(sql_id or "").strip()
        if not space_nm or not sql_id:
            raise ValueError("space_nm and sql_id are required")

        with self._connect() as conn:
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
            "tag_kind": self._to_text(row[0]),
            "space_nm": self._to_text(row[1]),
            "sql_id": self._to_text(row[2]),
            "fr_sql": self._to_text(row[3]),
            "edit_fr_sql": self._to_text(row[4]),
            "target_table": self._to_text(row[5]),
            "to_sql": self._to_text(row[6]),
            "status_conversion": self._to_text(row[7]),
            "log": self._to_text(row[8]),
            "tuned_fr_sql": self._to_text(row[9]),
            "tuned_to_sql": self._to_text(row[10]),
            "sql_length": self._to_text(row[11]),
            "map_type": self._to_text(row[12]),
            "priority": row[13],
            "batch_cnt": row[14],
            "upd_ts": self._to_text(row[15]),
            "user_edited": self._to_text(row[16]),
            "bind_sql": self._to_text(row[17]),
            "bind_set": self._to_text(row[18]),
            "test_sql": self._to_text(row[19]),
            "status_tuning": self._to_text(row[20]),
            "retry_count": row[21],
        }


    # TARGET_TABLE의 FR_TABLE 목록을 기준으로 migration map/rag 정보를 만든다.
    def _build_mapping_schema_text(self, job: dict[str, Any]) -> tuple[str, list[int], list[str], int]:

        fr_tables = self._extract_target_fr_tables(job.get("target_table"))
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

        normalized_fr_tables = {self._normalize_table_name(name) for name in fr_tables if self._normalize_table_name(name)}

        sections = ["[TARGET_TABLE_FR_TABLE_HINTS]"]
        for table_name in fr_tables:
            sections.append(f"  - {table_name}")

        sections.append("\n[MIGRATION_MAP_IDS]")
        map_ids: list[int] = []
        table = self._qualify_table("NEXT_MIG_INFO", self.system_schema)
        detail = self._qualify_table("NEXT_MIG_INFO_DTL", self.system_schema)
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                f"""
                SELECT M.MAP_ID, M.MAP_TYPE, M.FR_TABLE, D.FR_COL, M.TO_TABLE, D.TO_COL, M.CONDITION
                FROM {table} M
                LEFT JOIN {detail} D ON M.MAP_ID = D.MAP_ID
                WHERE UPPER(TRIM(NVL(M.USE_YN, 'N'))) = 'Y'
                ORDER BY M.PRIORITY ASC, M.MAP_ID ASC, D.MAP_DTL ASC
                """
            )
            rows = cur.fetchall()

        matched_rows = []
        matched_fr_tables: set[str] = set()
        for row in rows:
            map_id, map_type, fr_table, fr_col, to_table, to_col, condition = row
            fr_table_text = self._to_text(fr_table)
            normalized_fr_table = self._normalize_table_name(fr_table_text)
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
            table_name for table_name in fr_tables if self._normalize_table_name(table_name) not in matched_fr_tables
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
                    self._to_text(v) for v in (map_type, fr_table, fr_col, to_table, to_col, condition)
                ]
                sections.append(
                    f"  - map_id={map_id}; map_type={map_type}; from={fr_table}.{fr_col or '*'}; to={to_table}.{to_col or '*'}; condition={condition}"
                )

        sections.append("\n[SQL_CONVERSION_RAG_GUIDANCE]")
        rag_lines = self._load_conversion_rag_rules(fr_tables)
        sections.extend(rag_lines)
        rag_rule_count = len([line for line in rag_lines if line.strip().startswith("- {")])
        return "\n".join(sections), map_ids, fr_tables, rag_rule_count

    # NEXT_MIG_RAG_INFO에서 CATEGORY=SQL_CONVERSION이고 SOURCE_TABLES가 FR_TABLE과 같은 rule을 최대 3개씩 가져온다.
    def _load_conversion_rag_rules(self, fr_tables: list[str]) -> list[str]:
        table = self._qualify_table("NEXT_MIG_RAG_INFO", self.system_schema)
        if not fr_tables:
            return ["  - No FR_TABLE hints for SQL_CONVERSION RAG lookup."]
        lines = []
        try:
            with self._connect() as conn:
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
                                    "rule_type": self._to_text(rule_type),
                                    "source_tables": self._to_text(source_tables),
                                    "guidance": self._to_text(guidance),
                                    "source_sql": self._to_text(source_sql)[:1000],
                                    "target_sql": self._to_text(target_sql)[:1000],
                                },
                                ensure_ascii=False,
                            )
                        )
        except Exception:
            return ["  - No SQL_CONVERSION RAG rules loaded."]
        return lines or ["  - No SQL_CONVERSION RAG rules found for FR_TABLE hints."]

    # SQL 변환 프롬포트의 placeholder를 실제 값으로 치환한다.
    def _render_to_sql_prompt(
        self,
        from_sql: str,
        mapping_schema_text: str,
        source_schema: str,
        target_schema: str,
        correct_sql_hint_json: str,
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
            "correct_sql_hint_json": correct_sql_hint_json,
            "last_error": last_error,
        }
        for key, value in values.items():
            template = template.replace("{" + key + "}", str(value))
        return template

    # Render the BIND_SQL prompt by replacing placeholders with runtime values.
    def _render_bind_sql_prompt(
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

    # Render the TEST_SQL prompt by replacing placeholders with runtime values.
    def _render_test_sql_prompt(
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

    # SQL 변환 검증 프롬포트의 placeholder를 실제 값으로 치환한다.
    def _render_verify_prompt(
        self,
        from_sql: str,
        to_sql: str,
        mapping_schema_text: str,
        source_schema: str,
        target_schema: str,
        correct_sql_hint_json: str,
        last_error: str,
    ) -> str:
        template = str(self.verify_sql_prompt or "").strip()
        if not template:
            raise ValueError("VERIFY SQL Prompt input is required for SQL generation")
        values = {
            "from_sql": from_sql,
            "to_sql": to_sql,
            "mapping_schema_text": mapping_schema_text,
            "source_schema": source_schema,
            "target_schema": target_schema,
            "correct_sql_hint_json": correct_sql_hint_json,
            "last_error": last_error,
        }
        for key, value in values.items():
            template = template.replace("{" + key + "}", str(value))
        return template

    # OpenAI 호환 chat/completions 경로로 LLM을 호출한다.
    def _call_llm(self, prompt: str) -> str:
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
        data = self._post_json(url, payload, {"Authorization": f"Bearer {api_key}"})
        return str(data["choices"][0]["message"].get("content", ""))

    # Remove markdown code fences and the trailing semicolon from an LLM SQL response.
    def _sanitize_to_sql(self, value: str) -> str:
        text = str(value or "").strip()
        if text.startswith("```"):
            fence = re.search(r"```(?:sql)?\s*(.*?)```", text, flags=re.I | re.S)
            if fence:
                text = fence.group(1).strip()
        text = text.rstrip(";").strip()
        if not text:
            raise ValueError("LLM returned empty SQL")
        return text

    # Generate BIND_SQL from FR_SQL/EDIT_FR_SQL with source_schema hints applied.
    def _generate_bind_sql(self, job: dict[str, Any], to_sql: str, mapping_schema_text: str, last_error: Any = None) -> dict[str, Any]:
        edit_fr_sql = str(job.get("edit_fr_sql") or "").strip()
        fr_sql = str(job.get("fr_sql") or "").strip()
        source_sql = edit_fr_sql if edit_fr_sql else fr_sql
        if not source_sql:
            return {"ok": False, "status": "FAIL-BIND", "error": "source SQL is empty"}

        # TARGET_TABLE FR_TABLE values are used to decide where source_schema should be attached.
        fr_tables = self._extract_target_fr_tables(job.get("target_table"))
        source_schema = str(self.source_schema or "").strip().upper()
        if source_schema:
            for table_name in fr_tables:
                clean_table = str(table_name or "").strip().strip('"')
                if not clean_table or "." in clean_table:
                    continue
                source_sql = re.sub(rf"(?<![A-Z0-9_$#.]){re.escape(clean_table)}(?![A-Z0-9_$#])", f"{source_schema}.{clean_table}", source_sql, flags=re.I)

        prompt = self._render_bind_sql_prompt(
            from_sql=source_sql,
            to_sql=to_sql,
            mapping_schema_text=mapping_schema_text,
            source_schema=source_schema or "UNKNOWN",
            target_schema=str(self.target_schema or "").strip() or "UNKNOWN",
            last_error=str(last_error or "None"),
        )
        bind_sql = self._sanitize_to_sql(self._call_llm(prompt))
        return {"ok": True, "status": "SUCCESS-BIND", "bind_sql": bind_sql}

    # Generate TEST_SQL from FR_SQL, TO_SQL, mapping rules, and BIND_SET.
    def _generate_test_sql(self, job: dict[str, Any], to_sql: str, bind_sql: str, bind_set: str, mapping_schema_text: str, last_error: Any = None) -> dict[str, Any]:
        edit_fr_sql = str(job.get("edit_fr_sql") or "").strip()
        fr_sql = str(job.get("fr_sql") or "").strip()
        source_sql = edit_fr_sql if edit_fr_sql else fr_sql
        if not source_sql:
            return {"ok": False, "status": "FAIL-TEST", "error": "source SQL is empty"}
        if not to_sql:
            return {"ok": False, "status": "FAIL-TEST", "error": "TO_SQL is empty"}
        if not bind_set:
            return {"ok": False, "status": "FAIL-TEST", "error": "BIND_SET is empty"}

        prompt = self._render_test_sql_prompt(
            from_sql=source_sql,
            to_sql=to_sql,
            bind_sql=bind_sql,
            bind_set=bind_set,
            mapping_schema_text=mapping_schema_text,
            source_schema=str(self.source_schema or "").strip() or "UNKNOWN",
            target_schema=str(self.target_schema or "").strip() or "UNKNOWN",
            last_error=str(last_error or "None"),
        )
        test_sql = self._sanitize_to_sql(self._call_llm(prompt))
        return {"ok": True, "status": "TEST_SQL_GENERATED", "test_sql": test_sql}

    # Execute BIND_SQL and return rows as a JSON string like [{column: value}].
    def _execute_bind_sql(self, bind_sql: str) -> dict[str, Any]:
        clean_sql = self._prepare_runtime_sql(bind_sql, "EXECUTE_BIND_SQL")
        if not clean_sql:
            raise ValueError("BIND_SQL is empty")
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(clean_sql)
            columns = [desc[0] for desc in cur.description] if cur.description else []
            rows = cur.fetchmany(20)
        result_rows = [{str(columns[i] if i < len(columns) else i): self._json_value(value) for i, value in enumerate(row)} for row in rows]
        bind_set = json.dumps(result_rows, ensure_ascii=False)
        return {"ok": True, "status": "SUCCESS-BIND", "row_count": len(result_rows), "bind_set": bind_set}

    # Execute TEST_SQL and verify FROM_COUNT equals TO_COUNT for every CASE_NO.
    def _execute_test_sql(self, test_sql: str) -> dict[str, Any]:
        clean_sql = self._prepare_runtime_sql(test_sql, "EXECUTE_TEST_SQL")
        if not clean_sql:
            raise ValueError("TEST_SQL is empty")
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(clean_sql)
            columns = [desc[0] for desc in cur.description] if cur.description else []
            rows = cur.fetchall()
        result_rows = [{str(columns[i] if i < len(columns) else i): self._json_value(value) for i, value in enumerate(row)} for row in rows]
        if not result_rows:
            return {"ok": False, "status": "FAIL-TEST", "message": "TEST_SQL returned no rows", "result_rows": result_rows}
        sample_keys = {str(key).lower() for key in result_rows[0].keys()}
        if not {"case_no", "from_count", "to_count"}.issubset(sample_keys):
            return {"ok": False, "status": "FAIL-TEST", "message": f"TEST_SQL must return CASE_NO, FROM_COUNT, TO_COUNT. Actual columns: {sorted(sample_keys)}", "result_rows": result_rows}
        for row in result_rows:
            from_count = self._get_row_value(row, "FROM_COUNT")
            to_count = self._get_row_value(row, "TO_COUNT")
            if str(from_count).strip() != str(to_count).strip():
                return {"ok": False, "status": "FAIL-TEST", "message": f"Count mismatch: {row}", "result_rows": result_rows}
        return {"ok": True, "status": "PASS-CONVERSION", "message": "All test counts matched", "result_rows": result_rows}

    # Save generated SQL artifacts to NEXT_SQL_INFO only at final success/failure time.
    def _save_final_sql(self, sql_id: str, space_nm: str, to_sql: str, bind_sql: str, bind_set: str, test_sql: str) -> None:
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
        table = self._qualify_table("NEXT_SQL_INFO", self.system_schema)
        with self._connect() as conn:
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

    # Save final STATUS_CONVERSION/STATUS_TUNING plus retry and batch count to NEXT_SQL_INFO.
    def _update_job_status(self, sql_id: str, space_nm: str, status_conversion: str, elapsed_seconds: int, retry_count: int, status_tuning: str | None = None) -> None:
        assignments = ["STATUS_CONVERSION = :1", "RETRY_COUNT = :2", "BATCH_CNT = NVL(BATCH_CNT, 0) + 1", "LOG = :3", "UPD_TS = CURRENT_TIMESTAMP"]
        params: list[Any] = [status_conversion, retry_count, f"STATUS_CONVERSION={status_conversion}; elapsed={elapsed_seconds}s; retry={retry_count}"]
        if status_tuning:
            params.append(status_tuning)
            assignments.append(f"STATUS_TUNING = :{len(params)}")
        params.extend([space_nm, sql_id])
        table = self._qualify_table("NEXT_SQL_INFO", self.system_schema)
        with self._connect() as conn:
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

    # Append SQL Conversion stage history to NEXT_SQL_LOG.
    def _write_log(self, sql_id: str, space_nm: str, sql_kind: str, status: str, stage_name: str, message: str, retry_count: int = 0, sql_content: str | None = None, elapsed_seconds: int | None = None, prompt_name: str | None = None) -> None:
        table = self._qualify_table("NEXT_SQL_LOG", self.system_schema)
        try:
            with self._connect() as conn:
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

    # Prepare runtime SQL by rejecting MyBatis tags and normalizing LIMIT/FETCH clauses.
    def _prepare_runtime_sql(self, sql_text: str, stage: str) -> str:
        clean_sql = self._sanitize_to_sql(sql_text)
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

    # Read a dict row value using case-insensitive column matching.
    def _get_row_value(self, row: dict[str, Any], key: str) -> Any:
        if key in row:
            return row[key]
        lowered = key.lower()
        for existing_key, value in row.items():
            if str(existing_key).lower() == lowered:
                return value
        return None

    # Convert DB values into JSON-serializable values.
    def _json_value(self, value: Any) -> Any:
        if value is None:
            return None
        if hasattr(value, "read"):
            value = value.read()
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="ignore")
        if isinstance(value, (str, int, float, bool)):
            return value
        return str(value)

    # Keep action step summaries small by excluding large SQL bodies.
    def _summary_result(self, result: dict[str, Any]) -> dict[str, Any]:
        summary = {"ok": bool(result.get("ok")), "status": result.get("status")}
        for key in ["message", "error", "row_count", "elapsed_seconds", "retry_count"]:
            if key in result:
                summary[key] = result.get(key)
        return summary

    # TARGET_TABLE JSON 배열에서 FR_TABLE 목록을 꺼낸다.
    def _extract_target_fr_tables(self, value: Any) -> list[str]:
        text = self._to_text(value).strip()
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

    # schema가 붙은 테이블명은 마지막 테이블명만 남겨 비교 기준으로 사용한다.
    def _normalize_table_name(self, value: Any) -> str:
        text = self._to_text(value).strip().strip('"').upper()
        if "." in text:
            text = text.split(".")[-1]
        return text

    # schema 입력값이 있으면 테이블명 앞에 schema를 붙인다.
    def _qualify_table(self, table_name: str, schema: str | None) -> str:
        clean = str(table_name or "").strip()
        clean_schema = str(schema or "").strip().upper()
        if not clean:
            raise ValueError("table_name is empty")
        if "." in clean or not clean_schema:
            return clean
        if not re.fullmatch(r"[A-Z][A-Z0-9_$#]*", clean_schema):
            raise ValueError(f"Invalid schema: {clean_schema}")
        return f"{clean_schema}.{clean}"

    # CLOB/bytes/None 값을 안전하게 문자열로 변환한다.
    def _to_text(self, value: Any) -> str:
        if value is None:
            return ""
        if hasattr(value, "read"):
            value = value.read()
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="ignore")
        return str(value)

