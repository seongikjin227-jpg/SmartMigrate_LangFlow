import time

from langchain_core.tools import tool

from server.services.sql.statuses import is_tuning_pass
from server.tools.sql_chain import run_formatting_continuation
from server.tools.context import (
    callbacks,
    clear_active_job,
    claim_job_execution,
    record_agent_run,
    refresh_jobs_after_tool,
    set_active_job,
    sql_job_display_id,
    tuning_registry,
)


@tool
def run_sql_tuning(row_ids: list) -> str:
    """Run SQL tuning jobs for the given NEXT_SQL_INFO row IDs."""
    results = []
    logger = callbacks.get("logger")

    for row_id in row_ids:
        job = tuning_registry.get(str(row_id))
        if job is None:
            results.append(f"row_id={row_id} not found")
            continue

        job_label = sql_job_display_id(job, row_id)
        started = time.perf_counter()
        try:
            if not claim_job_execution():
                return "SKIP: another job already ran in this supervisor cycle."
            row_key = str(row_id)
            set_active_job("SQL Tuning", job_label, "TUNING")
            callbacks["sql_inc"](row_key)
            final_status = callbacks["tune_proc"](job)
            record_agent_run("SQL_TUNING", time.perf_counter() - started, final_status)
            results.append(f"{job_label} completed status={final_status}")
            if is_tuning_pass(final_status):
                results.extend(run_formatting_continuation(row_key, logger=logger))
        except Exception as exc:
            record_agent_run("SQL_TUNING", time.perf_counter() - started, "FAIL")
            if logger:
                logger.error(f"[SqlTuningTool] {job_label} error: {exc}")
            results.append(f"{job_label} failed: {exc}")
        finally:
            clear_active_job()
            refresh_jobs_after_tool()
        break

    return "SqlTuning result: " + " | ".join(results)
