import time

from server.repositories.sql.result_repository import get_sql_job_by_row_id
from server.services.sql.statuses import is_tuning_pass
from server.tools.context import callbacks, record_agent_run, set_active_job
from server.tools.poll import _agent_flags


def run_tuning_continuation(row_id: str, logger=None) -> list[str]:
    """Run SQL tuning, and formatting on tuning pass, for the same SQL row."""
    _, _, run_tuning, _ = _agent_flags()
    if not run_tuning:
        return []

    tune_proc = callbacks.get("tune_proc")
    sql_inc = callbacks.get("sql_inc")
    if not tune_proc or not sql_inc:
        return ["tuning skipped: callback not available"]

    row_key = str(row_id)
    job = get_sql_job_by_row_id(row_key)
    if job is None:
        return [f"tuning skipped: row_id={row_key} not found"]

    started = time.perf_counter()
    try:
        set_active_job("SQL Tuning", row_key, "TUNING")
        sql_inc(row_key)
        final_status = tune_proc(job)
        record_agent_run("SQL_TUNING", time.perf_counter() - started, final_status)
        if logger:
            logger.info(f"[SqlChain] row_id={row_key} tuning completed (status={final_status})")
    except Exception as exc:
        record_agent_run("SQL_TUNING", time.perf_counter() - started, "FAIL")
        if logger:
            logger.error(f"[SqlChain] row_id={row_key} tuning error: {exc}")
        return [f"tuning failed: {exc}"]

    results = [f"tuning={final_status}"]
    if is_tuning_pass(final_status):
        results.extend(run_formatting_continuation(row_key, logger=logger))
    return results


def run_formatting_continuation(row_id: str, logger=None) -> list[str]:
    """Run SQL formatting for the same SQL row when enabled."""
    _, _, _, run_formatting = _agent_flags()
    if not run_formatting:
        return []

    format_proc = callbacks.get("format_proc")
    sql_inc = callbacks.get("sql_inc")
    if not format_proc or not sql_inc:
        return ["formatting skipped: callback not available"]

    row_key = str(row_id)
    job = get_sql_job_by_row_id(row_key)
    if job is None:
        return [f"formatting skipped: row_id={row_key} not found"]

    started = time.perf_counter()
    try:
        set_active_job("SQL Formatting", row_key, "FORMATTING")
        sql_inc(row_key)
        final_status = format_proc(job)
        record_agent_run("SQL_FORMATTING", time.perf_counter() - started, final_status)
        if logger:
            logger.info(f"[SqlChain] row_id={row_key} formatting completed (status={final_status})")
        return [f"formatting={final_status}"]
    except Exception as exc:
        record_agent_run("SQL_FORMATTING", time.perf_counter() - started, "FAIL")
        if logger:
            logger.error(f"[SqlChain] row_id={row_key} formatting error: {exc}")
        return [f"formatting failed: {exc}"]
