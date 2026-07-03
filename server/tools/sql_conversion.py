import time

from langchain_core.tools import tool

from server.services.sql.statuses import is_conversion_pass
from server.tools.sql_chain import run_tuning_continuation
from server.tools.context import (
    callbacks,
    claim_job_execution,
    record_agent_run,
    refresh_jobs_after_tool,
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

    started = time.perf_counter()
    try:
        if not claim_job_execution():
            return "SKIP: another job already ran in this supervisor cycle."
        callbacks["sql_inc"](row_key)
        final_status = callbacks["sql_proc"](job)
        record_agent_run("SQL_MIGRATION", time.perf_counter() - started, final_status)
        chain_results = []
        if is_conversion_pass(final_status):
            chain_results = run_tuning_continuation(row_key, logger=logger)
        if logger:
            logger.info(f"[SqlConversionTool] row_id={row_key} completed (status={final_status})")
        suffix = f" | {' | '.join(chain_results)}" if chain_results else ""
        return f"SqlConversion row_id={row_key} completed status={final_status}{suffix}"
    except Exception as exc:
        record_agent_run("SQL_MIGRATION", time.perf_counter() - started, "FAIL")
        if logger:
            logger.error(f"[SqlConversionTool] row_id={row_key} error: {exc}")
        return f"ERROR: row_id={row_key} failed: {exc}"
    finally:
        refresh_jobs_after_tool()
