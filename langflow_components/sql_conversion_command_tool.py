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


    # action="list_pending": STATUS_CONVERSION이 READY 또는 NULL인 작업 대상을 조회한다.
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
                    WHERE (STATUS_CONVERSION IS NULL OR UPPER(TRIM(STATUS_CONVERSION)) = 'READY')
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

    # 수정 중
    # action="run_sql_conversion_job": TO_SQL 생성, 실행, verify SQL 생성, 검증 실행까지 전체 사이클을 수행한다.
    def run_sql_conversion_job(self, sql_id: str, space_nm: str, command: dict[str, Any]) -> dict[str, Any]:

        # =====_run_sql_conversion_job은 사용자가 채팅으로 호출할 수도 있기 때문에 사용자가 요청한 job이 실행 가능한지 검증한다.=====
        if (sql_id is None or str(sql_id).strip() == "") or (space_nm is None or str(space_nm).strip() == ""):
            return {"ok": False, "error": "sql_id and space_nm are required for run_sql_conversion_job"}    
        sql_id = str(sql_id or "").strip()
        space_nm = str(space_nm or "").strip()

        # started는 최종 PASS/FAIL 상태 저장 시 elapsed_seconds 계산에 사용한다.
        started = time.perf_counter()
        max_attempts = max(1, int(command.get("max_attempts") or 1))

        job = self._load_job(space_nm, sql_id)
        if not job:
            return {"ok": False, "error": f"job not found for sql_id={sql_id}, space_nm={space_nm}"}    

        # SQL Conversion 에서는 Use_yn은 사용하지 않음, 오직 Status 컬럼 만으로 상태를 관리함
        if str(job.get("status_conversion") or "").strip().upper() not in ("READY", ""):
            return {
                "ok": False,
                "space_nm": job.get("space_nm"),
                "sql_id": job.get("sql_id"),
                "status": job.get("status_conversion"),
                "error": f"job is not in READY,null status for sql_id={sql_id}, space_nm={space_nm}",
            }

        # check_dependency는 SQL Conversion에서는 사용하지 않음

        # steps에는 SQL 생성/실행/검증 각 단계의 요약 결과를 순서대로 누적한다.
        steps: list[dict[str, Any]] = []

        # Keep the latest TO_SQL value for final PASS/FAIL persistence.
        last_to_sql = str(job.get("to_sql") or "")
        last_tuned_fr_sql = str(job.get("tuned_fr_sql") or "")
        last_bind_sql = str(job.get("bind_sql") or "")
        last_test_sql = str(job.get("test_sql") or "")

        try:
            # 실행 직전에 작업을 다시 읽어 사용자 수정 SQL이나 최신 상태를 반영한다.
            job = self._load_job(space_nm, sql_id) or job
            user_edited = str(job.get("user_edited") or "").upper() == "Y"

            # force_regenerate 옵션은 TO_SQL을 LLM으로 재생성할지 여부를 결정한다.
            generation_command = {
                "force_regenerate": command.get("force_regenerate", False),
            }

                        # 이전 실패 정보와 마지막 SQL을 TO_SQL 생성 함수에 넘겨 retry prompt에 반영한다.
            last_failure: dict[str, Any] = {}
            conversion_executed = False
            last_retry_count = 0

            # attempt는 1부터 시작하고, retry_count는 DB/로그 기준으로 0부터 시작한다.
            for attempt in range(1, max_attempts + 1):
                retry_count = attempt - 1
                last_retry_count = retry_count

                job = self._load_job(space_nm, sql_id) or job
                user_edited = str(job.get("user_edited") or "").upper() == "Y"

                # TO_SQL은 따로 실행할 필요가 없으므로 LLM 생성만 성공하면 이 단계는 통과한다.
                if not conversion_executed:
                    # USER_EDITED=Y이면 force_regenerate가 아닌 경우 DB에 저장된 TO_SQL을 그대로 사용한다.
                    if user_edited and not generation_command.get("force_regenerate", False):
                        to_sql = str(job.get("to_sql") or "").strip()
                        if not to_sql:
                            raise ValueError("USER_EDITED=Y but TO_SQL is empty")
                        last_to_sql = to_sql
                        steps.append({"step": "generate_to_sql", "attempt": attempt, "status": "SKIPPED_USER_EDITED"})
                    else:
                        # 이전 실패 정보와 마지막 SQL을 TO_SQL 생성 함수에 넘겨 retry prompt에 반영한다.
                        conversion_command = {
                            **generation_command,
                            "retry_count": retry_count,
                            "last_error": last_failure.get("error", ""),
                            "last_sql": last_to_sql,
                        }
                        conversion_result = self._generate_to_sql(
                            space_nm,
                            sql_id,
                            last_error=conversion_command.get("last_error"),
                            last_sql=conversion_command.get("last_sql"),
                            retry_count=conversion_command.get("retry_count"),
                            force_regenerate=bool(conversion_command.get("force_regenerate", False)),
                        )
                        steps.append({"step": "generate_to_sql", "attempt": attempt, **conversion_result})
                        if not conversion_result.get("ok"):
                            last_failure = {"status": "FAIL-CONVERT", "error": conversion_result.get("error") or "TO_SQL generation failed"}
                            if attempt < max_attempts:
                                continue
                            break
                        last_to_sql = str(conversion_result.get("to_sql") or "")

                    conversion_executed = True
                    elapsed = int(time.perf_counter() - started)
                    return {
                        "ok": True,
                        "space_nm": space_nm,
                        "sql_id": sql_id,
                        "status": "TO_SQL_GENERATED",
                        "elapsed_seconds": elapsed,
                        "retry_count": last_retry_count,
                        "steps": steps,
                        "to_sql": last_to_sql,
                    }

            elapsed = int(time.perf_counter() - started)
            return {
                "ok": False,
                "space_nm": space_nm,
                "sql_id": sql_id,
                "status": last_failure.get("status") or "FAIL-CONVERT",
                "error": last_failure.get("error") or "TO_SQL generation failed",
                "elapsed_seconds": elapsed,
                "retry_count": last_retry_count,
                "steps": steps,
            }
        except Exception as exc:
            return {"ok": False, "space_nm": space_nm, "sql_id": sql_id, "error": str(exc)}

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
                       PRIORITY, BATCH_CNT, UPD_TS
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

    # LLM 응답에서 markdown 코드 블록과 마지막 세미콜론을 제거한다.
    def _sanitize_to_sql(self, value: str) -> str:
        text = str(value or "").strip()
        if text.startswith("```"):
            if fence:
                text = fence.group(1).strip()
        text = text.rstrip(";").strip()
        if not text:
            raise ValueError("LLM returned empty TO_SQL")
        return text

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

