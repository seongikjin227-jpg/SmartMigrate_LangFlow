import time

from langchain_core.tools import tool

from server.services.sql.statuses import is_conversion_pass
from server.tools.sql_chain import run_tuning_continuation
from server.tools.context import (
    callbacks,
    clear_active_job,
    claim_job_execution,
    record_agent_run,
    refresh_jobs_after_tool,
    set_active_job,
    sql_job_display_id,
    sql_registry,
)


@tool
def run_sql_conversion(row_id: str) -> str:
    """Run one SQL conversion job selected by row_id."""
    row_key = str(row_id)
    job = sql_registry.get(row_key)
    logger = callbacks.get("logger")

    if job is None:
        return f"ERROR: row_id={row_key} was not found in the current registry."

    job_label = sql_job_display_id(job, row_key)
    started = time.perf_counter()
    try:
        if not claim_job_execution():
            return "SKIP: another job already ran in this supervisor cycle."
        set_active_job("SQL Conversion", job_label, "CONVERSION")
        callbacks["sql_inc"](row_key)
        final_status = callbacks["sql_proc"](job)
        record_agent_run("SQL_MIGRATION", time.perf_counter() - started, final_status)
        chain_results = []
        if is_conversion_pass(final_status):
            chain_results = run_tuning_continuation(row_key, logger=logger)
        if logger:
            logger.info(f"[SqlConversionTool] {job_label} completed (status={final_status})")
        suffix = f" | {' | '.join(chain_results)}" if chain_results else ""
        return f"SqlConversion {job_label} completed status={final_status}{suffix}"
    except Exception as exc:
        record_agent_run("SQL_MIGRATION", time.perf_counter() - started, "FAIL")
        if logger:
            logger.error(f"[SqlConversionTool] {job_label} error: {exc}")
        return f"ERROR: {job_label} failed: {exc}"
    finally:
        clear_active_job()
        refresh_jobs_after_tool()
