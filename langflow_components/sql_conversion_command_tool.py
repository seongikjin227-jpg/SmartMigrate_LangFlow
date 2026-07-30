from __future__ import annotations

import json
import re
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
    description = "Generates TO_SQL_TEXT for SmartMigration SQL conversion jobs."
    name = "SqlConversionCommandTool"
    icon = "FileCode"

    _db_cache: dict[str, Any] = {}

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
            info="Prompt template for generate_to_sql_text. Use placeholders: {from_sql}, {mapping_schema_text}, {target_schema}, {correct_sql_hint_json}, {last_error}.",
        ),
        StrInput(
            name="system_schema",
            display_name="System Schema",
            required=False,
            info="Schema containing NEXT_SQL_INFO/NEXT_MIG_INFO/NEXT_MIG_INFO_DTL/NEXT_MIG_RAG_INFO. Leave blank for current user.",
        ),
        StrInput(
            name="target_schema",
            display_name="Target Schema",
            required=False,
            info="Target schema to apply to physical TO-BE tables.",
        ),
        IntInput(name="list_limit", display_name="Default List Limit", value=10, required=False),
    ]

    outputs = [
        Output(display_name="Result", name="result", method="run_command"),
    ]

    # ==================== 액션 코드 ====================
    def run_command(self) -> Data:
        try:
            # Langflow에서 들어온 command_json을 dict로 변환한다.
            # action은 어떤 작업을 실행할지 결정하고, space_nm/sql_id는 SQL Conversion 작업 대상을 찾는 key로 사용한다.
            command = self._parse_command()
            action = str(command.get("action") or "").strip().lower()
            # action 값에 따라 실제 처리 함수를 호출한다.
            # 각 함수는 result dict를 반환하고, 마지막에 Langflow Data로 감싸서 돌려준다.
            if action == "test_connection":
                result = self._test_connection()
            elif action == "status":
                result = self._status(command)
            elif action == "list_pending":
                result = self._list_pending(command.get("limit", self.list_limit))
            elif action == "generate_to_sql_text":
                result = self._generate_to_sql_text(command)
            else:
                result = {"ok": False, "error": f"Unsupported action: {action}"}
            self.status = result
            return Data(data=result)
        except Exception as exc:
            # 예외가 나도 Langflow Agent가 읽을 수 있게 ok=False 형태의 JSON으로 반환한다.
            result = {"ok": False, "error": str(exc)}
            self.status = result
            return Data(data=result)

    # ======================================================================
    # 기본 실행/연결 코드
    # ======================================================================
    def _parse_command(self) -> dict[str, Any]:
        # command_json 문자열을 dict로 바꾼다.
        # 여기서 action/space_nm/sql_id/save/confirm 같은 실행 파라미터가 처음 해석된다.
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

    def _cache_key(self) -> str:
        # 같은 DB 접속 정보는 _db_cache에서 재사용하기 위해 문자열 key로 만든다.
        return "|".join(
            [
                str(self.db_host or "").strip(),
                str(self.db_port or 1521),
                str(self.db_service_name or "").strip(),
                str(self.db_username or "").strip(),
            ]
        )

    def _get_db(self):
        # DB 연결에 필요한 패키지를 확인하고, 같은 접속 정보면 cached SQLDatabase를 재사용한다.
        self._ensure_runtime_dependencies()
        from langchain_community.utilities import SQLDatabase

        key = self._cache_key()
        db = self._db_cache.get(key)
        if db is None:
            db = SQLDatabase.from_uri(self._connection_string())
            self._db_cache[key] = db
        return db

    def _ensure_runtime_dependencies(self) -> None:
        # SQL Conversion 실행에 필요한 패키지가 Langflow 런타임에 설치되어 있는지 import로만 확인한다.
        import langchain_community.utilities
        import oracledb
        import sqlalchemy

    @contextmanager
    def _connect(self):
        # SELECT/UPDATE처럼 cursor가 필요한 작업은 SQLDatabase 내부 engine connection을 사용한다.
        db = self._get_db()
        with db._engine.connect() as conn:
            raw = conn.connection
            yield raw

    # ======================================================================
    # 액션 함수 상세 코드
    # ======================================================================
    # action="test_connection": DB와 LLM 연결을 확인한다.
    def _test_connection(self) -> dict[str, Any]:
        # DB 연결 결과와 LLM 연결 결과를 각각 확인한 뒤 둘 다 성공해야 ok=True로 반환한다.
        db_result = self._test_db_connection()
        llm_result = self._test_llm_connection()
        return {"ok": bool(db_result.get("ok") and llm_result.get("ok")), "db": db_result, "llm": llm_result}

    def _test_db_connection(self) -> dict[str, Any]:
        # DB는 실제 SELECT 1 쿼리로 연결 가능 여부를 확인한다.
        try:
            rows = self._get_db().run("SELECT 1 AS OK FROM DUAL", include_columns=True)
            return {"ok": True, "message": "DB connection OK", "rows": self._normalize_query_rows(rows)}
        except Exception as exc:
            return {"ok": False, "message": str(exc)}

    def _test_llm_connection(self) -> dict[str, Any]:
        # LLM은 현재 base_url/api_key/model 입력값으로 짧은 chat/completions 호출을 보낸다.
        try:
            text = self._call_llm("Return exactly: OK")
            return {"ok": bool(text.strip()), "provider": "openai-compatible", "model": self.llm_model, "response_preview": text[:200]}
        except Exception as exc:
            return {"ok": False, "message": str(exc), "provider": "openai-compatible", "model": self.llm_model}

    # action="status": space_nm/sql_id 기준 SQL Conversion 작업 1건을 조회한다.
    def _status(self, command: dict[str, Any]) -> dict[str, Any]:
        # command에서 space_nm/sql_id를 꺼내 NEXT_SQL_INFO 단건을 조회한다.
        job = self._load_job(command)
        return {"ok": bool(job), "job": job, "error": "" if job else "job not found"}

    # action="list_pending": SQL Conversion 작업 후보를 조회한다.
    def _list_pending(self, limit: Any) -> dict[str, Any]:
        # limit은 너무 크지 않게 1~100 사이로 제한한다.
        safe_limit = max(1, min(int(limit or self.list_limit or 10), 100))
        table = self._system_table("NEXT_SQL_INFO")
        with self._connect() as conn:
            cur = conn.cursor()
            # STATUS_CONVERSION이 NULL 또는 READY인 작업만 agent가 처리 가능한 후보로 본다.
            cur.execute(
                f"""
                SELECT *
                FROM (
                    SELECT TAG_KIND, SPACE_NM, SQL_ID, STATUS_CONVERSION,
                           DBMS_LOB.SUBSTR(FR_SQL_TEXT, 1000, 1) AS FR_SQL_PREVIEW,
                           DBMS_LOB.GETLENGTH(FR_SQL_TEXT) AS FR_SQL_LEN,
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

    # action="generate_to_sql_text": FROM SQL을 TO-BE SQL로 변환한다.
    def _generate_to_sql_text(self, command: dict[str, Any]) -> dict[str, Any]:
        # command의 space_nm/sql_id로 변환 대상 job을 조회한다.
        job = self._load_job(command)
        if not job:
            return {"ok": False, "error": "job not found"}
        # EDIT_FR_SQL이 있으면 사용자가 보정한 SQL을 우선하고, 없으면 원본 FR_SQL_TEXT를 사용한다.
        source_sql = str(job.get("edit_fr_sql") or job.get("fr_sql_text") or "").strip()
        if not source_sql:
            return {"ok": False, "space_nm": job.get("space_nm"), "sql_id": job.get("sql_id"), "error": "source SQL is empty"}

        # source_sql과 migration mapping/RAG 정보를 prompt에 넣어서 LLM에 변환을 요청한다.
        prompt = self._render_to_sql_prompt(
            from_sql=source_sql,
            mapping_schema_text=self._build_mapping_schema_text(source_sql),
            target_schema=str(self.target_schema or "").strip() or "UNKNOWN",
            correct_sql_hint_json="[]",
            last_error=str(command.get("last_error") or "None"),
        )
        # LLM 응답에서 실제 SQL 텍스트만 정리한다. 이 시점에는 아직 DB 저장 전이다.
        to_sql_text = self._sanitize_to_sql_text(self._call_llm(prompt))
        save = self._as_bool(command.get("save", False))
        if save:
            # save=true이면 NEXT_SQL_INFO.TO_SQL_TEXT를 업데이트하므로 confirm=true가 반드시 필요하다.
            if not self._as_bool(command.get("confirm", False)):
                return {
                    "ok": False,
                    "space_nm": job.get("space_nm"),
                    "sql_id": job.get("sql_id"),
                    "status": "CONFIRM_REQUIRED",
                    "error": "generate_to_sql_text with save=true requires confirm=true because it updates NEXT_SQL_INFO.",
                    "to_sql_text": to_sql_text,
                }
            # 최종 변환 SQL을 DB에 저장한다. save=false이면 사용자에게 preview만 반환한다.
            self._save_to_sql_text(str(job["space_nm"]), str(job["sql_id"]), to_sql_text)

        return {
            "ok": True,
            "space_nm": job.get("space_nm"),
            "sql_id": job.get("sql_id"),
            "status": "TO_SQL_TEXT_GENERATED",
            "saved": save,
            "to_sql_text": to_sql_text,
        }

    # ======================================================================
    # 공통 코드
    # ======================================================================
    def _load_job(self, command: dict[str, Any]) -> dict[str, Any] | None:
        # SQL Conversion의 작업 key는 space_nm + sql_id다. 둘 중 하나라도 없으면 조회할 수 없다.
        table = self._system_table("NEXT_SQL_INFO")
        sql_id = str(command.get("sql_id") or "").strip()
        space_nm = str(command.get("space_nm") or "").strip()
        if not space_nm or not sql_id:
            raise ValueError("space_nm and sql_id are required")

        # NEXT_SQL_INFO에서 원본 SQL, 수정 SQL, 기존 TO_SQL_TEXT, 상태값을 한 번에 가져온다.
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                f"""
                SELECT TAG_KIND, SPACE_NM, SQL_ID, FR_SQL_TEXT, EDIT_FR_SQL,
                       TARGET_TABLE, TO_SQL_TEXT, STATUS_CONVERSION, LOG,
                       FR_BINDTUNED_SQL, TOBE_CORRECT_SQL, SQL_LENGTH, MAP_TYPE,
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
            "fr_sql_text": self._to_text(row[3]),
            "edit_fr_sql": self._to_text(row[4]),
            "target_table": self._to_text(row[5]),
            "to_sql_text": self._to_text(row[6]),
            "status_conversion": self._to_text(row[7]),
            "log": self._to_text(row[8]),
            "fr_bindtuned_sql": self._to_text(row[9]),
            "tobe_correct_sql": self._to_text(row[10]),
            "sql_length": self._to_text(row[11]),
            "map_type": self._to_text(row[12]),
            "priority": row[13],
            "batch_cnt": row[14],
            "upd_ts": self._to_text(row[15]),
        }

    def _build_mapping_schema_text(self, source_sql: str) -> str:
        # 변환 prompt에 넣을 mapping/RAG/context 블록을 만든다.
        # NEXT_MIG_INFO 계열 mapping rule과 NEXT_MIG_RAG_INFO의 SQL_CONVERSION rule을 같이 포함한다.
        sections = ["[MIGRATION_MAPPING_RULES]"]
        table = self._system_table("NEXT_MIG_INFO")
        detail = self._system_table("NEXT_MIG_INFO_DTL")
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                f"""
                SELECT M.MAP_TYPE, M.FR_TABLE, D.FR_COL, M.TO_TABLE, D.TO_COL, M.CONDITION
                FROM {table} M
                LEFT JOIN {detail} D ON M.MAP_ID = D.MAP_ID
                WHERE UPPER(TRIM(NVL(M.USE_YN, 'N'))) = 'Y'
                ORDER BY M.PRIORITY ASC, M.MAP_ID ASC, D.MAP_DTL ASC
                """
            )
            rows = cur.fetchall()
        if not rows:
            sections.append("  - No mapping rules found.")
        else:
            # mapping rule이 너무 길어지지 않도록 앞쪽 1000건까지만 prompt에 넣는다.
            for row in rows[:1000]:
                map_type, fr_table, fr_col, to_table, to_col, condition = [self._to_text(v) for v in row]
                sections.append(
                    f"  - map_type={map_type}; from={fr_table}.{fr_col or '*'}; to={to_table}.{to_col or '*'}; condition={condition}"
                )
        sections.append("\n[SQL_CONVERSION_GENERAL_RAG_GUIDANCE]")
        sections.extend(self._load_conversion_rag_rules())
        sections.append("\n[CURRENT_SOURCE_SQL_TABLE_HINTS]")
        # 현재 변환 대상 SQL에 등장하는 FROM/JOIN 테이블명을 hint로 추가한다.
        for table_name in self._extract_table_names(source_sql):
            sections.append(f"  - {table_name}")
        return "\n".join(sections)

    def _load_conversion_rag_rules(self) -> list[str]:
        # SQL Conversion 전용 RAG rule을 NEXT_MIG_RAG_INFO에서 가져온다.
        # RAG 테이블이 없거나 조회 실패해도 변환 자체는 계속 진행되도록 기본 문구를 반환한다.
        table = self._system_table("NEXT_MIG_RAG_INFO")
        try:
            with self._connect() as conn:
                cur = conn.cursor()
                cur.execute(
                    f"""
                    SELECT RULE_TYPE, SOURCE_TABLES, GUIDANCE_TEXT, SOURCE_SQL, TARGET_SQL
                    FROM {table}
                    WHERE CATEGORY = 'SQL_CONVERSION'
                      AND UPPER(TRIM(NVL(USE_YN, 'Y'))) = 'Y'
                    ORDER BY CASE WHEN RULE_TYPE = 'GENERAL' THEN 1 ELSE 2 END, RAG_ID
                    FETCH FIRST 30 ROWS ONLY
                    """
                )
                rows = cur.fetchall()
        except Exception:
            return ["  - No SQL_CONVERSION RAG rules loaded."]
        if not rows:
            return ["  - No SQL_CONVERSION RAG rules found."]
        lines = []
        # 각 RAG rule은 JSON 문자열로 넣어서 prompt에서 구조를 유지한다.
        for rule_type, source_tables, guidance, source_sql, target_sql in rows:
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
        return lines

    def _render_to_sql_prompt(
        self,
        from_sql: str,
        mapping_schema_text: str,
        target_schema: str,
        correct_sql_hint_json: str,
        last_error: str,
    ) -> str:
        # Langflow에 입력한 to_sql_prompt의 placeholder를 실제 값으로 치환한다.
        template = str(self.to_sql_prompt or "").strip()
        if not template:
            raise ValueError("TO SQL Prompt input is required")
        values = {
            "from_sql": from_sql,
            "mapping_schema_text": mapping_schema_text,
            "target_schema": target_schema,
            "correct_sql_hint_json": correct_sql_hint_json,
            "last_error": last_error,
        }
        for key, value in values.items():
            template = template.replace("{" + key + "}", str(value))
        return template

    def _call_llm(self, prompt: str) -> str:
        # SQL 변환도 OpenAI-compatible chat/completions 한 경로만 사용한다.
        # 반환값은 아직 정리되기 전의 LLM raw content다.
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

    def _save_to_sql_text(self, space_nm: str, sql_id: str, to_sql_text: str) -> None:
        # generate_to_sql_text에서 save=true, confirm=true인 경우에만 호출된다.
        # 바인드 변수(:1, :2, :3)를 써서 긴 SQL 문자열을 안전하게 업데이트한다.
        table = self._system_table("NEXT_SQL_INFO")
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                f"""
                UPDATE {table}
                SET TO_SQL_TEXT = :1,
                    LOG = 'TO_SQL_TEXT generated by Langflow SQL Conversion Command Tool',
                    UPD_TS = CURRENT_TIMESTAMP
                WHERE SPACE_NM = :2
                  AND SQL_ID = :3
                """,
                [to_sql_text, space_nm, sql_id],
            )
            conn.commit()

    def _sanitize_to_sql_text(self, value: str) -> str:
        # LLM이 markdown sql fence를 붙여도 안쪽 SQL만 꺼낸다.
        # TO_SQL_TEXT는 SELECT/INSERT 제한을 걸지 않고 비어 있는지만 확인한다.
        text = str(value or "").strip()
        if text.startswith("```"):
            fence = re.search(r"```(?:sql)?\s*(.*?)```", text, flags=re.I | re.S)
            if fence:
                text = fence.group(1).strip()
        text = text.rstrip(";").strip()
        if not text:
            raise ValueError("LLM returned empty TO_SQL_TEXT")
        return text

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
        return [{"value": str(raw)}]

    def _extract_table_names(self, sql: str) -> list[str]:
        # 변환 대상 SQL의 FROM/JOIN 뒤에 나오는 테이블명을 뽑아서 prompt hint로 사용한다.
        names = []
        for match in re.finditer(r"\b(?:FROM|JOIN)\s+([A-Z0-9_$#.\"]+)", str(sql or ""), flags=re.I):
            name = match.group(1).strip().strip('"')
            if name.upper() not in {"SELECT", "DUAL"} and name not in names:
                names.append(name)
        return names[:50]

    def _system_table(self, table_name: str) -> str:
        # system_schema 입력값이 있으면 시스템 테이블명에 schema prefix를 붙인다.
        schema = str(self.system_schema or "").strip().upper()
        clean_table = str(table_name or "").strip().upper()
        if not schema or "." in clean_table:
            return clean_table
        if not re.fullmatch(r"[A-Z][A-Z0-9_$#]*", schema):
            raise ValueError(f"Invalid System Schema: {schema}")
        return f"{schema}.{clean_table}"

    def _to_text(self, value: Any) -> str:
        # Oracle CLOB/bytes/None 값을 화면에 반환 가능한 문자열로 통일한다.
        if value is None:
            return ""
        if hasattr(value, "read"):
            value = value.read()
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="ignore")
        return str(value)

    def _as_bool(self, value: Any) -> bool:
        # command_json에서 넘어온 true/false 계열 문자열을 bool로 해석한다.
        if isinstance(value, bool):
            return value
        return str(value or "").strip().lower() in {"1", "true", "t", "y", "yes"}
