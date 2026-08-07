"""NEXT_SQL_INFO ?? ??/?? ?? repository."""

import os

from server.core.logger import logger
from server.services.sql.domain_models import SqlInfoJob
from server.services.sql.db_runtime import get_connection, get_result_table, split_table_owner_and_name
from server.services.sql.statuses import (
    CONVERSION_FAIL_STATUSES,
    CONVERSION_SUCCESS_STATUSES,
    FAIL_TEST,
    LEGACY_FAIL,
    TUNING_FAIL_STATUSES,
    TUNING_SUCCESS_STATUSES,
    sql_in,
)

_COLUMN_LENGTH_CACHE: dict[str, dict[str, int]] = {}
_AVAILABLE_COLUMNS_CACHE: dict[str, set[str]] = {}
_PENDING_JOB_STATUSES = ("URGENT", "READY", "PENDING", LEGACY_FAIL, *CONVERSION_FAIL_STATUSES)
_SQL_LENGTH_SHORT_MAX = 5000
_DEFAULT_JOB_MAX_BATCH_COUNT = 30


_CONVERSION_STATUS_COLUMN = "STATUS_CONVERSION"
_TUNING_STATUS_COLUMN = "STATUS_TUNING"


def _status_select_expr(column: str | None, alias: str) -> str:
    return f"{column} AS {alias}"


def _sql_info_column(available_columns: set[str], preferred: str, fallback: str | None = None) -> str:
    if preferred in available_columns:
        return preferred
    if fallback and fallback in available_columns:
        return fallback
    return preferred


def _optional_alias_expr(
    available_columns: set[str],
    preferred: str,
    alias: str,
    data_type: str = "VARCHAR2(4000)",
    fallback: str | None = None,
) -> str:
    column = _sql_info_column(available_columns, preferred, fallback)
    if column in available_columns:
        return f"{column} AS {alias}"
    return f"CAST(NULL AS {data_type}) AS {alias}"


def _to_text(value, default: str = "") -> str:
    if value is None:
        return default
    if hasattr(value, "read"):
        value = value.read()
    if value is None:
        return default
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    return str(value)


def _to_optional_text(value) -> str | None:
    if value is None:
        return None
    return _to_text(value)


def _cache_key_for_table(table: str) -> tuple[str, str, str]:
    owner, normalized_table = split_table_owner_and_name(table)
    return owner or "", normalized_table, f"{owner or ''}.{normalized_table}"


def _get_job_max_batch_count() -> int:
    raw_value = os.getenv("JOB_MAX_BATCH_COUNT", str(_DEFAULT_JOB_MAX_BATCH_COUNT))
    try:
        return int(raw_value)
    except (TypeError, ValueError):
        logger.warning(
            "[Repo] Invalid JOB_MAX_BATCH_COUNT=%r; using default %s",
            raw_value,
            _DEFAULT_JOB_MAX_BATCH_COUNT,
        )
        return _DEFAULT_JOB_MAX_BATCH_COUNT


def _get_batch_limit_clause(available_columns: set[str]) -> str:
    max_batch_count = _get_job_max_batch_count()
    if "BATCH_CNT" not in available_columns or max_batch_count <= 0:
        return ""
    return f"AND NVL(BATCH_CNT, 0) < {max_batch_count}"


def _get_column_data_lengths(table: str) -> dict[str, int]:
    owner, normalized_table, cache_key = _cache_key_for_table(table)
    if cache_key in _COLUMN_LENGTH_CACHE:
        return _COLUMN_LENGTH_CACHE[cache_key]

    if owner:
        query = """
            SELECT COLUMN_NAME, DATA_TYPE, DATA_LENGTH
            FROM ALL_TAB_COLUMNS
            WHERE OWNER = :1
              AND TABLE_NAME = :2
        """
        params = [owner, normalized_table]
    else:
        query = """
            SELECT COLUMN_NAME, DATA_TYPE, DATA_LENGTH
            FROM USER_TAB_COLUMNS
            WHERE TABLE_NAME = :1
        """
        params = [normalized_table]
    lengths: dict[str, int] = {}
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        for col_name, data_type, data_length in cursor.fetchall():
            col = _to_text(col_name).upper()
            dtype = _to_text(data_type).upper()
            if "CLOB" in dtype:
                continue
            try:
                lengths[col] = int(data_length)
            except Exception:
                continue

    _COLUMN_LENGTH_CACHE[cache_key] = lengths
    return lengths


def _get_available_columns(table: str) -> set[str]:
    owner, normalized_table = split_table_owner_and_name(table)
    cache_key = f"{owner or ''}.{normalized_table}"
    if cache_key in _AVAILABLE_COLUMNS_CACHE:
        return _AVAILABLE_COLUMNS_CACHE[cache_key]

    if owner:
        query = """
            SELECT COLUMN_NAME
            FROM ALL_TAB_COLUMNS
            WHERE OWNER = :1
              AND TABLE_NAME = :2
        """
        params = [owner, normalized_table]
    else:
        query = """
            SELECT COLUMN_NAME
            FROM USER_TAB_COLUMNS
            WHERE TABLE_NAME = :1
        """
        params = [normalized_table]
    columns: set[str] = set()
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        for (col_name,) in cursor.fetchall():
            columns.add(_to_text(col_name).upper())

    _AVAILABLE_COLUMNS_CACHE[cache_key] = columns
    return columns


def _can_select_column(table: str, column_name: str) -> bool:
    query = f"SELECT {column_name} FROM {table} WHERE 1 = 0"
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query)
        return True
    except Exception:
        return False


def _row_to_sql_info_job(row) -> SqlInfoJob:
    priority_value = None
    if len(row) > 24 and row[24] is not None:
        try:
            priority_value = int(row[24])
        except Exception:
            priority_value = None
    retry_count_value = None
    if len(row) > 25 and row[25] is not None:
        try:
            retry_count_value = int(row[25])
        except Exception:
            retry_count_value = None

    return SqlInfoJob(
        row_id=row[0],
        tag_kind=_to_text(row[1]),
        space_nm=_to_text(row[2]),
        sql_id=_to_text(row[3]),
        fr_sql_text=_to_text(row[4]),
        target_table=_to_optional_text(row[5]),
        edit_fr_sql=_to_optional_text(row[6]),
        to_sql_text=_to_optional_text(row[7]),
        tuned_sql=_to_optional_text(row[8]),
        tuned_test=_to_optional_text(row[9]),
        bind_sql=_to_optional_text(row[10]),
        bind_set=_to_optional_text(row[11]),
        test_sql=_to_optional_text(row[12]),
        status=_to_optional_text(row[13]),
        log_text=_to_optional_text(row[14]),
        upd_ts=row[15],
        fr_bindtuned_sql=_to_optional_text(row[16]) if len(row) > 16 else None,
        user_edited=_to_optional_text(row[17]) if len(row) > 17 else None,
        sql_length=_to_optional_text(row[20]) if len(row) > 20 else None,
        map_type=_to_optional_text(row[21]) if len(row) > 21 else None,
        formatted_sql=_to_optional_text(row[22]) if len(row) > 22 else None,
        tuned_result=_to_optional_text(row[23]) if len(row) > 23 else None,
        priority=priority_value,
        retry_count=retry_count_value,
    )


def get_pending_jobs() -> list[SqlInfoJob]:
    table = get_result_table()
    available_columns = _get_available_columns(table)
    fr_sql_column = _sql_info_column(available_columns, "FR_SQL", "FR_SQL_TEXT")
    to_sql_column = _sql_info_column(available_columns, "TO_SQL", "TO_SQL_TEXT")
    fr_sql_select = f"{fr_sql_column} AS FR_SQL_TEXT"
    to_sql_select = f"{to_sql_column} AS TO_SQL_TEXT"
    # Pending lookup must stay scalar-only. CLOB columns are loaded later by ROWID.
    user_edited_column = "USER_EDITED" if "USER_EDITED" in available_columns else "CAST(NULL AS VARCHAR2(1)) AS USER_EDITED"
    removed_correct_placeholders = "CAST(NULL AS VARCHAR2(1)) AS UNUSED_SQL_PLACEHOLDER_1, CAST(NULL AS VARCHAR2(1)) AS UNUSED_SQL_PLACEHOLDER_2"
    conversion_status_column = _CONVERSION_STATUS_COLUMN
    tuning_status_column = _TUNING_STATUS_COLUMN
    conversion_status_select = _status_select_expr(conversion_status_column, "STATUS_CONVERSION")
    tuning_status_select = _status_select_expr(tuning_status_column, "STATUS_TUNING")
    tuned_sql_column = _optional_alias_expr(available_columns, "TUNED_TO_SQL", "TUNED_SQL", fallback="TUNED_SQL")
    fr_bindtuned_sql_column = _optional_alias_expr(available_columns, "TUNED_FR_SQL", "FR_BINDTUNED_SQL", fallback="FR_BINDTUNED_SQL")
    sql_length_column = "SQL_LENGTH" if "SQL_LENGTH" in available_columns else "CAST(NULL AS VARCHAR2(4000)) AS SQL_LENGTH"
    map_type_column = "MAP_TYPE" if "MAP_TYPE" in available_columns else "CAST(NULL AS VARCHAR2(4000)) AS MAP_TYPE"
    formatted_sql_column = "FORMATTED_SQL" if "FORMATTED_SQL" in available_columns else "CAST(NULL AS VARCHAR2(4000)) AS FORMATTED_SQL"
    tuned_result_column = "TUNED_RESULT" if "TUNED_RESULT" in available_columns else "CAST(NULL AS VARCHAR2(4000)) AS TUNED_RESULT"
    priority_column = "PRIORITY" if "PRIORITY" in available_columns else "CAST(NULL AS NUMBER) AS PRIORITY"
    retry_count_column = "RETRY_COUNT" if "RETRY_COUNT" in available_columns else "CAST(NULL AS NUMBER) AS RETRY_COUNT"
    priority_order_clause = (
        "PRIORITY ASC NULLS LAST,"
        if "PRIORITY" in available_columns
        else f"""
          CASE
            WHEN UPPER(TRIM({conversion_status_column})) = 'URGENT' THEN 1
            WHEN UPPER(TRIM({conversion_status_column})) = 'READY' THEN 2
            WHEN UPPER(TRIM({conversion_status_column})) IN ({sql_in((LEGACY_FAIL, *CONVERSION_FAIL_STATUSES))}) THEN 3
            WHEN UPPER(TRIM({conversion_status_column})) = 'PENDING' THEN 4
            WHEN {conversion_status_column} IS NULL THEN 5
            ELSE 9
          END,
        """
    )
    batch_limit_clause = _get_batch_limit_clause(available_columns)
    pending_status_sql = sql_in(_PENDING_JOB_STATUSES)
    query = f"""
        SELECT ROWIDTOCHAR(ROWID) AS RID
        FROM {table}
        WHERE (UPPER(TRIM({conversion_status_column})) IN ({pending_status_sql}) OR {conversion_status_column} IS NULL)
          {batch_limit_clause}
        ORDER BY
          {priority_order_clause}
          UPD_TS NULLS FIRST,
          TO_CHAR(SPACE_NM),
          TO_CHAR(SQL_ID)
    """

    jobs: list[SqlInfoJob] = []
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query)
            for row in cursor.fetchall():
                job = get_sql_job_by_row_id(row[0])
                if job:
                    jobs.append(job)
    except Exception as e:
        logger.error(f"[Repo] SqlPipeline 대기 작업 조회 중 오류: {e}", exc_info=True)
    return jobs


def get_sql_job_by_row_id(row_id: str) -> SqlInfoJob | None:
    table = get_result_table()
    available_columns = _get_available_columns(table)
    fr_sql_column = _sql_info_column(available_columns, "FR_SQL", "FR_SQL_TEXT")
    to_sql_column = _sql_info_column(available_columns, "TO_SQL", "TO_SQL_TEXT")
    fr_sql_select = f"{fr_sql_column} AS FR_SQL_TEXT"
    to_sql_select = f"{to_sql_column} AS TO_SQL_TEXT"
    user_edited_column = "USER_EDITED" if "USER_EDITED" in available_columns else "CAST(NULL AS VARCHAR2(1)) AS USER_EDITED"
    removed_correct_placeholders = "CAST(NULL AS VARCHAR2(1)) AS UNUSED_SQL_PLACEHOLDER_1, CAST(NULL AS VARCHAR2(1)) AS UNUSED_SQL_PLACEHOLDER_2"
    conversion_status_column = _CONVERSION_STATUS_COLUMN
    tuning_status_column = _TUNING_STATUS_COLUMN
    conversion_status_select = _status_select_expr(conversion_status_column, "STATUS_CONVERSION")
    tuning_status_select = _status_select_expr(tuning_status_column, "STATUS_TUNING")
    tuned_sql_column = _optional_alias_expr(available_columns, "TUNED_TO_SQL", "TUNED_SQL", fallback="TUNED_SQL")
    fr_bindtuned_sql_column = _optional_alias_expr(available_columns, "TUNED_FR_SQL", "FR_BINDTUNED_SQL", fallback="FR_BINDTUNED_SQL")
    sql_length_column = "SQL_LENGTH" if "SQL_LENGTH" in available_columns else "CAST(NULL AS VARCHAR2(4000)) AS SQL_LENGTH"
    map_type_column = "MAP_TYPE" if "MAP_TYPE" in available_columns else "CAST(NULL AS VARCHAR2(4000)) AS MAP_TYPE"
    formatted_sql_column = "FORMATTED_SQL" if "FORMATTED_SQL" in available_columns else "CAST(NULL AS VARCHAR2(4000)) AS FORMATTED_SQL"
    tuned_result_column = "TUNED_RESULT" if "TUNED_RESULT" in available_columns else "CAST(NULL AS VARCHAR2(4000)) AS TUNED_RESULT"
    priority_column = "PRIORITY" if "PRIORITY" in available_columns else "CAST(NULL AS NUMBER) AS PRIORITY"
    retry_count_column = "RETRY_COUNT" if "RETRY_COUNT" in available_columns else "CAST(NULL AS NUMBER) AS RETRY_COUNT"
    query = f"""
        SELECT ROWIDTOCHAR(ROWID) AS RID,
               TAG_KIND, SPACE_NM, SQL_ID, {fr_sql_select}, TARGET_TABLE, EDIT_FR_SQL,
               {to_sql_select}, {tuned_sql_column}, {tuning_status_select}, BIND_SQL, BIND_SET, TEST_SQL, {conversion_status_select}, LOG,
               UPD_TS, {fr_bindtuned_sql_column}, {user_edited_column}, {removed_correct_placeholders}, {sql_length_column}, {map_type_column}, {formatted_sql_column}, {tuned_result_column}, {priority_column}, {retry_count_column}
        FROM {table}
        WHERE ROWID = CHARTOROWID(:1)
    """
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, [row_id])
            row = cursor.fetchone()
            return _row_to_sql_info_job(row) if row else None
    except Exception as e:
        logger.error(f"[Repo] SQL job lookup by ROWID failed: {e}", exc_info=True)
        return None


def get_tuning_jobs() -> list:
    """Return unfinished tuning jobs under the retry limit."""
    table = get_result_table()
    available_columns = _get_available_columns(table)
    fr_sql_column = _sql_info_column(available_columns, "FR_SQL", "FR_SQL_TEXT")
    to_sql_column = _sql_info_column(available_columns, "TO_SQL", "TO_SQL_TEXT")
    fr_sql_select = f"{fr_sql_column} AS FR_SQL_TEXT"
    to_sql_select = f"{to_sql_column} AS TO_SQL_TEXT"
    conversion_status_column = _CONVERSION_STATUS_COLUMN
    tuning_status_column = _TUNING_STATUS_COLUMN
    conversion_status_select = _status_select_expr(conversion_status_column, "STATUS_CONVERSION")
    tuning_status_select = _status_select_expr(tuning_status_column, "STATUS_TUNING")

    user_edited_column = "USER_EDITED" if "USER_EDITED" in available_columns else "CAST(NULL AS VARCHAR2(1)) AS USER_EDITED"
    removed_correct_placeholders = "CAST(NULL AS VARCHAR2(1)) AS UNUSED_SQL_PLACEHOLDER_1, CAST(NULL AS VARCHAR2(1)) AS UNUSED_SQL_PLACEHOLDER_2"
    tuned_sql_column = _optional_alias_expr(available_columns, "TUNED_TO_SQL", "TUNED_SQL", fallback="TUNED_SQL")
    fr_bindtuned_sql_column = _optional_alias_expr(available_columns, "TUNED_FR_SQL", "FR_BINDTUNED_SQL", fallback="FR_BINDTUNED_SQL")
    sql_length_column = "SQL_LENGTH" if "SQL_LENGTH" in available_columns else "CAST(NULL AS VARCHAR2(4000)) AS SQL_LENGTH"
    map_type_column = "MAP_TYPE" if "MAP_TYPE" in available_columns else "CAST(NULL AS VARCHAR2(4000)) AS MAP_TYPE"
    formatted_sql_column = "FORMATTED_SQL" if "FORMATTED_SQL" in available_columns else "CAST(NULL AS VARCHAR2(4000)) AS FORMATTED_SQL"
    tuned_result_column = "TUNED_RESULT" if "TUNED_RESULT" in available_columns else "CAST(NULL AS VARCHAR2(4000)) AS TUNED_RESULT"
    priority_column = "PRIORITY" if "PRIORITY" in available_columns else "CAST(NULL AS NUMBER) AS PRIORITY"
    retry_count_column = "RETRY_COUNT" if "RETRY_COUNT" in available_columns else "CAST(NULL AS NUMBER) AS RETRY_COUNT"
    batch_limit_clause = _get_batch_limit_clause(available_columns)

    query = f"""
        SELECT ROWIDTOCHAR(ROWID) AS RID
        FROM {table}
        WHERE UPPER(TRIM({tuning_status_column})) IN ({sql_in(('URGENT', 'READY', LEGACY_FAIL, *TUNING_FAIL_STATUSES))})
          AND UPPER(TRIM({conversion_status_column})) IN ({sql_in(CONVERSION_SUCCESS_STATUSES)})
          {batch_limit_clause}
        ORDER BY
          CASE
            WHEN UPPER(TRIM({tuning_status_column})) = 'URGENT' THEN 1
            WHEN UPPER(TRIM({tuning_status_column})) = 'READY' THEN 2
            WHEN UPPER(TRIM({tuning_status_column})) IN ({sql_in((LEGACY_FAIL, *TUNING_FAIL_STATUSES))}) THEN 3
            ELSE 9
          END,
          UPD_TS NULLS FIRST,
          TO_CHAR(SPACE_NM),
          TO_CHAR(SQL_ID)
    """

    jobs = []
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query)
            for row in cursor.fetchall():
                job = get_sql_job_by_row_id(row[0])
                if job:
                    jobs.append(job)
    except Exception as e:
        logger.error(f"[Repo] SqlTuning pending job lookup failed: {e}", exc_info=True)
    return jobs


def get_formatting_jobs() -> list[SqlInfoJob]:
    """Return tuned rows explicitly requested for FORMATTED_SQL regeneration."""
    table = get_result_table()
    available_columns = _get_available_columns(table)
    fr_sql_column = _sql_info_column(available_columns, "FR_SQL", "FR_SQL_TEXT")
    to_sql_column = _sql_info_column(available_columns, "TO_SQL", "TO_SQL_TEXT")
    fr_sql_select = f"{fr_sql_column} AS FR_SQL_TEXT"
    to_sql_select = f"{to_sql_column} AS TO_SQL_TEXT"
    tuning_status_column = _TUNING_STATUS_COLUMN
    if (
        "FORMATTED_SQL" not in available_columns
        or "FORMATTING_RETRY_YN" not in available_columns
    ):
        return []
    conversion_status_column = _CONVERSION_STATUS_COLUMN
    conversion_status_select = _status_select_expr(conversion_status_column, "STATUS_CONVERSION")
    tuning_status_select = _status_select_expr(tuning_status_column, "STATUS_TUNING")

    user_edited_column = "USER_EDITED" if "USER_EDITED" in available_columns else "CAST(NULL AS VARCHAR2(1)) AS USER_EDITED"
    removed_correct_placeholders = "CAST(NULL AS VARCHAR2(1)) AS UNUSED_SQL_PLACEHOLDER_1, CAST(NULL AS VARCHAR2(1)) AS UNUSED_SQL_PLACEHOLDER_2"
    tuned_sql_column = _optional_alias_expr(available_columns, "TUNED_TO_SQL", "TUNED_SQL", fallback="TUNED_SQL")
    fr_bindtuned_sql_column = _optional_alias_expr(available_columns, "TUNED_FR_SQL", "FR_BINDTUNED_SQL", fallback="FR_BINDTUNED_SQL")
    sql_length_column = "SQL_LENGTH" if "SQL_LENGTH" in available_columns else "CAST(NULL AS VARCHAR2(4000)) AS SQL_LENGTH"
    map_type_column = "MAP_TYPE" if "MAP_TYPE" in available_columns else "CAST(NULL AS VARCHAR2(4000)) AS MAP_TYPE"
    tuned_result_column = "TUNED_RESULT" if "TUNED_RESULT" in available_columns else "CAST(NULL AS VARCHAR2(4000)) AS TUNED_RESULT"
    priority_column = "PRIORITY" if "PRIORITY" in available_columns else "CAST(NULL AS NUMBER) AS PRIORITY"
    retry_count_column = "RETRY_COUNT" if "RETRY_COUNT" in available_columns else "CAST(NULL AS NUMBER) AS RETRY_COUNT"
    batch_limit_clause = _get_batch_limit_clause(available_columns)

    query = f"""
        SELECT ROWIDTOCHAR(ROWID) AS RID
        FROM {table}
        WHERE UPPER(TRIM({tuning_status_column})) IN ({sql_in(TUNING_SUCCESS_STATUSES)})
          AND UPPER(TRIM(FORMATTING_RETRY_YN)) = 'Y'
          {batch_limit_clause}
        ORDER BY
          UPD_TS NULLS FIRST,
          TO_CHAR(SPACE_NM),
          TO_CHAR(SQL_ID)
    """

    jobs: list[SqlInfoJob] = []
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query)
            for row in cursor.fetchall():
                job = get_sql_job_by_row_id(row[0])
                if job:
                    jobs.append(job)
    except Exception as e:
        logger.error(f"[Repo] SqlFormatting pending job lookup failed: {e}", exc_info=True)
    return jobs


def update_tuning_error(row_id: str, error_msg: str, tuned_sql: str | None = None) -> None:
    """Record a tuning error and mark the row as retryable FAIL."""
    table = get_result_table()
    available_columns = _get_available_columns(table)
    tuning_status_column = _TUNING_STATUS_COLUMN
    tuned_sql_column = _sql_info_column(available_columns, "TUNED_TO_SQL", "TUNED_SQL")
    payload = _fit_payload_to_column_limits(
        table=table,
        values={
            tuned_sql_column: tuned_sql if tuned_sql_column in available_columns else None,
        },
    )
    tuned_test_clause = f"{tuning_status_column} = '{FAIL_TEST}',"
    tuned_sql_clause = f"{tuned_sql_column} = :tuned_sql," if payload[tuned_sql_column] else ""
    query = f"""
        UPDATE {table}
        SET {tuned_test_clause}
            {tuned_sql_clause}
            LOG = SUBSTR('[TUNING_ERROR] ' || :err, 1, 4000),
            UPD_TS = SYSDATE
        WHERE ROWID = CHARTOROWID(:rid)
    """
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            params = {"err": error_msg, "rid": row_id}
            if tuned_sql_clause:
                params["tuned_sql"] = payload[tuned_sql_column]
            cursor.execute(query, params)
            conn.commit()
    except Exception as e:
        logger.error(f"[Repo] Tuning error update failed: {e}")


def update_job_skip(row_id: str, reason: str) -> None:
    table = get_result_table()
    available_columns = _get_available_columns(table)
    conversion_status_column = _CONVERSION_STATUS_COLUMN
    payload = _fit_payload_to_column_limits(
        table=table,
        values={
            conversion_status_column: "SKIP",
            "LOG": f"SKIP reason={reason}",
        },
    )
    query = f"""
        UPDATE {table}
        SET {conversion_status_column} = :1,
            LOG = :2,
            UPD_TS = CURRENT_TIMESTAMP
        WHERE ROWID = CHARTOROWID(:3)
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(query, [payload[conversion_status_column], payload["LOG"], row_id])
        conn.commit()


def classify_sql_length(fr_sql_text: str | None, edit_fr_sql: str | None) -> str:
    fr_length = len(_to_text(fr_sql_text))
    edit_text = _to_text(edit_fr_sql).strip()
    edit_length = len(edit_text) if edit_text else 0
    if fr_length <= _SQL_LENGTH_SHORT_MAX and edit_length <= _SQL_LENGTH_SHORT_MAX:
        return "SHORT"
    return "LONG"


def update_job_classification(row_id: str, sql_length: str, map_type: str | None) -> None:
    table = get_result_table()
    available_columns = _get_available_columns(table)
    values: dict[str, str | None] = {}
    if "SQL_LENGTH" in available_columns:
        values["SQL_LENGTH"] = sql_length
    if "MAP_TYPE" in available_columns:
        values["MAP_TYPE"] = map_type
    if not values:
        return

    payload = _fit_payload_to_column_limits(table=table, values=values)
    set_clauses: list[str] = []
    params: list[str | None] = []
    for column, value in payload.items():
        params.append(value)
        set_clauses.append(f"{column} = :{len(params)}")
    set_clauses.append("UPD_TS = CURRENT_TIMESTAMP")
    params.append(row_id)

    query = f"""
        UPDATE {table}
        SET {", ".join(set_clauses)}
        WHERE ROWID = CHARTOROWID(:{len(params)})
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        conn.commit()


def reset_tuning_state(row_id: str) -> None:
    table = get_result_table()
    available_columns = _get_available_columns(table)
    set_clauses = ["UPD_TS = CURRENT_TIMESTAMP"]
    tuned_sql_column = _sql_info_column(available_columns, "TUNED_TO_SQL", "TUNED_SQL")
    if tuned_sql_column in available_columns:
        set_clauses.append(f"{tuned_sql_column} = NULL")
    tuning_status_column = _TUNING_STATUS_COLUMN
    set_clauses.append(f"{tuning_status_column} = NULL")
    if "TUNED_RESULT" in available_columns:
        set_clauses.append("TUNED_RESULT = NULL")
    if "BLOCK_RAG_CONTENT" in available_columns:
        set_clauses.append("BLOCK_RAG_CONTENT = NULL")

    if len(set_clauses) == 1:
        return

    query = f"""
        UPDATE {table}
        SET {", ".join(set_clauses)}
        WHERE ROWID = CHARTOROWID(:1)
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(query, [row_id])
        conn.commit()

def increment_batch_count(row_id: str) -> None:
    table = get_result_table()
    lengths = _get_column_data_lengths(table)
    if "BATCH_CNT" in lengths:
        query = f"""
            UPDATE {table}
            SET BATCH_CNT = NVL(BATCH_CNT, 0) + 1,
                UPD_TS = CURRENT_TIMESTAMP
            WHERE ROWID = CHARTOROWID(:1)
        """
    else:
        query = f"""
            UPDATE {table}
            SET UPD_TS = CURRENT_TIMESTAMP
            WHERE ROWID = CHARTOROWID(:1)
        """
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(query, [row_id])
        conn.commit()


def update_block_rag_content(row_id: str, block_rag_content: str) -> None:
    table = get_result_table()
    available_columns = _get_available_columns(table)
    if "BLOCK_RAG_CONTENT" not in available_columns:
        return

    payload = _fit_payload_to_column_limits(
        table=table,
        values={"BLOCK_RAG_CONTENT": block_rag_content},
    )
    query = f"""
        UPDATE {table}
        SET BLOCK_RAG_CONTENT = :1,
            UPD_TS = CURRENT_TIMESTAMP
        WHERE ROWID = CHARTOROWID(:2)
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(query, [payload["BLOCK_RAG_CONTENT"], row_id])
        conn.commit()


def update_formatted_sql(row_id: str, formatted_sql: str) -> None:
    table = get_result_table()
    available_columns = _get_available_columns(table)
    if "FORMATTED_SQL" not in available_columns:
        logger.warning("[Repo] FORMATTED_SQL column is not available; formatted SQL was not saved.")
        return

    payload = _fit_payload_to_column_limits(
        table=table,
        values={
            "FORMATTED_SQL": formatted_sql,
            "FORMATTING_RETRY_YN": "N" if "FORMATTING_RETRY_YN" in available_columns else None,
        },
    )
    set_clauses = ["FORMATTED_SQL = :1"]
    params = [payload["FORMATTED_SQL"]]
    next_index = 2
    if "FORMATTING_RETRY_YN" in available_columns:
        set_clauses.append(f"FORMATTING_RETRY_YN = :{next_index}")
        params.append(payload["FORMATTING_RETRY_YN"])
        next_index += 1
    set_clauses.append("UPD_TS = CURRENT_TIMESTAMP")
    set_clause = ",\n            ".join(set_clauses)
    query = f"""
        UPDATE {table}
        SET {set_clause}
        WHERE ROWID = CHARTOROWID(:{next_index})
    """
    params.append(row_id)
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        conn.commit()



def update_fr_bindtuned_sql(row_id: str, fr_bindtuned_sql: str) -> None:
    table = get_result_table()
    available_columns = _get_available_columns(table)
    fr_bindtuned_column = _sql_info_column(available_columns, "TUNED_FR_SQL", "FR_BINDTUNED_SQL")
    if fr_bindtuned_column not in available_columns:
        logger.warning("[Repo] TUNED_FR_SQL/FR_BINDTUNED_SQL column is not available; bind pretuning SQL was not saved.")
        return

    payload = _fit_payload_to_column_limits(
        table=table,
        values={fr_bindtuned_column: fr_bindtuned_sql},
    )
    query = f"""
        UPDATE {table}
        SET {fr_bindtuned_column} = :1
        WHERE ROWID = CHARTOROWID(:2)
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(query, [payload[fr_bindtuned_column], row_id])
        conn.commit()


def update_cycle_result(
    row_id: str,
    tobe_sql: str,
    tuned_sql: str | None,
    tuned_result: str | None,
    tuned_test: str | None,
    bind_sql: str,
    bind_set: str | None,
    test_sql: str,
    status: str,
    final_log: str,
    formatted_sql: str | None = None,
    retry_count: int | None = None,
):
    table = get_result_table()
    available_columns = _get_available_columns(table)
    conversion_status_column = _CONVERSION_STATUS_COLUMN
    tuning_status_column = _TUNING_STATUS_COLUMN
    to_sql_column = _sql_info_column(available_columns, "TO_SQL", "TO_SQL_TEXT")
    tuned_sql_column = _sql_info_column(available_columns, "TUNED_TO_SQL", "TUNED_SQL")
    payload = _fit_payload_to_column_limits(
        table=table,
        values={
            to_sql_column: tobe_sql,
            tuned_sql_column: tuned_sql if tuned_sql_column in available_columns else None,
            "TUNED_RESULT": tuned_result if "TUNED_RESULT" in available_columns else None,
            tuning_status_column: tuned_test,
            "FORMATTED_SQL": formatted_sql if "FORMATTED_SQL" in available_columns and formatted_sql is not None else None,
            "BIND_SQL": bind_sql,
            "BIND_SET": bind_set,
            "TEST_SQL": test_sql,
            conversion_status_column: status,
            "LOG": final_log,
        },
    )
    set_clauses = [f"{to_sql_column} = :1"]
    params: list[str | None] = [payload[to_sql_column]]
    if tuned_sql_column in available_columns:
        set_clauses.append(f"{tuned_sql_column} = :2")
        params.append(payload[tuned_sql_column])
        next_index = 3
    else:
        next_index = 2
    if "TUNED_RESULT" in available_columns:
        set_clauses.append(f"TUNED_RESULT = :{next_index}")
        params.append(payload["TUNED_RESULT"])
        next_index += 1
    set_clauses.append(f"{tuning_status_column} = :{next_index}")
    params.append(payload[tuning_status_column])
    next_index += 1
    if "FORMATTED_SQL" in available_columns and formatted_sql is not None:
        set_clauses.append(f"FORMATTED_SQL = :{next_index}")
        params.append(payload["FORMATTED_SQL"])
        next_index += 1
    set_clauses.extend(
        [
            f"BIND_SQL = :{next_index}",
            f"BIND_SET = :{next_index + 1}",
            f"TEST_SQL = :{next_index + 2}",
            f"{conversion_status_column} = :{next_index + 3}",
            f"LOG = :{next_index + 4}",
        ]
    )
    params.extend([payload["BIND_SQL"], payload["BIND_SET"], payload["TEST_SQL"]])
    params.append(payload[conversion_status_column])
    params.append(payload["LOG"])
    next_index += 5
    if "RETRY_COUNT" in available_columns and retry_count is not None:
        set_clauses.append(f"RETRY_COUNT = :{next_index}")
        params.append(int(retry_count))
        next_index += 1
    set_clauses.append("UPD_TS = CURRENT_TIMESTAMP")
    params.append(row_id)
    query = f"""
        UPDATE {table}
        SET {", ".join(set_clauses)}
        WHERE ROWID = CHARTOROWID(:{len(params)})
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        conn.commit()


def get_feedback_corpus_rows(correct_kind: str, limit: int = 2000) -> list[dict[str, str]]:
    return []


def _fit_payload_to_column_limits(
    table: str,
    values: dict[str, str | None],
) -> dict[str, str | None]:
    lengths = _get_column_data_lengths(table)
    fitted: dict[str, str | None] = {}
    for column, value in values.items():
        if value is None:
            fitted[column] = None
            continue
        limit = lengths.get(column.upper())
        text = _to_text(value, default="")
        fitted[column] = _truncate_utf8_by_bytes(text, limit) if limit else text
    return fitted


def _truncate_utf8_by_bytes(text: str, byte_limit: int) -> str:
    if byte_limit <= 0:
        return ""
    encoded = text.encode("utf-8", errors="ignore")
    if len(encoded) <= byte_limit:
        return text
    return encoded[:byte_limit].decode("utf-8", errors="ignore")
