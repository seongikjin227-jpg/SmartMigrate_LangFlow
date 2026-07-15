from __future__ import annotations

import os
import json
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
load_dotenv(ROOT_DIR / ".env", override=True)

from app.utils import agent_control
from server.agents.migration.orchestrator import MigrationOrchestrator
from server.agents.sql_conversion.agent import SqlConversionAgent
from server.agents.sql_formatting.agent import SqlFormattingAgent
from server.agents.sql_tuning.agent import SqlTuningAgent
from server.repositories.migration.repository import (
    get_pending_jobs as get_pending_migration_jobs,
    increment_batch_count as increment_migration_batch_count,
)
from server.repositories.sql.result_repository import (
    get_formatting_jobs,
    get_pending_jobs as get_pending_sql_conversion_jobs,
    get_sql_job_by_row_id,
    get_tuning_jobs,
    increment_batch_count as increment_sql_batch_count,
)

app = FastAPI(
    title="Migration Agent Langflow API",
    version="1.0.0",
    description="HTTP wrapper for running the migration-agent pipeline from Langflow API Request nodes.",
)


class RunMigrationRequest(BaseModel):
    map_id: int | None = None


class RunSqlRequest(BaseModel):
    row_id: str | None = None


class AgentCommandRequest(BaseModel):
    command: str


def _expected_api_key() -> str:
    return (os.getenv("LANGFLOW_API_KEY") or "").strip()


def _check_api_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
) -> None:
    expected = _expected_api_key()
    if not expected:
        return

    bearer = ""
    if authorization and authorization.lower().startswith("bearer "):
        bearer = authorization[7:].strip()

    if x_api_key == expected or bearer == expected:
        return
    raise HTTPException(status_code=401, detail="Invalid or missing API key.")


Auth = Depends(_check_api_key)


def _safe_text(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _migration_job_payload(job: Any) -> dict[str, Any]:
    return {
        "map_id": job.map_id,
        "map_type": _safe_text(job.map_type),
        "fr_table": _safe_text(job.fr_table),
        "to_table": _safe_text(job.to_table),
        "priority": job.priority,
        "prior_map_id": getattr(job, "prior_map_id", None),
        "retry_count": getattr(job, "retry_count", 0) or 0,
        "status": _safe_text(job.status),
        "batch_cnt": getattr(job, "batch_cnt", 0) or 0,
    }


def _sql_job_payload(job: Any) -> dict[str, Any]:
    return {
        "row_id": _safe_text(job.row_id),
        "tag_kind": _safe_text(job.tag_kind),
        "space_nm": _safe_text(job.space_nm),
        "sql_id": _safe_text(job.sql_id),
        "status": _safe_text(job.status),
        "tuned_test": _safe_text(job.tuned_test),
        "priority": getattr(job, "priority", None),
    }


def _select_job(jobs: list[Any], attr_name: str, requested_id: Any | None) -> Any | None:
    if not jobs:
        return None
    if requested_id is None:
        return jobs[0]
    requested = str(requested_id)
    for job in jobs:
        if str(getattr(job, attr_name, "")) == requested:
            return job
    return None


def _run_with_timing(stage: str, job: Any, runner) -> dict[str, Any]:
    started = time.perf_counter()
    status = runner(job)
    return {
        "stage": stage,
        "status": status,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }


@app.get("/health", dependencies=[Auth])
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "migration-agent-langflow-api",
        "project_path": str(ROOT_DIR),
        "auth_enabled": bool(_expected_api_key()),
    }


@app.get("/agent/status", dependencies=[Auth])
def get_agent_status() -> dict[str, Any]:
    return agent_control.get_status()


@app.post("/agent/start", dependencies=[Auth])
def start_agent() -> dict[str, Any]:
    return {"message": agent_control.start(), "status": agent_control.get_status()}


@app.post("/agent/stop", dependencies=[Auth])
def stop_agent() -> dict[str, Any]:
    return {"message": agent_control.stop(), "status": agent_control.get_status()}


@app.post("/agent/pause", dependencies=[Auth])
def pause_agent() -> dict[str, Any]:
    return {"message": agent_control.pause(), "status": agent_control.get_status()}


@app.post("/agent/resume", dependencies=[Auth])
def resume_agent() -> dict[str, Any]:
    return {"message": agent_control.resume(), "status": agent_control.get_status()}


@app.post("/agent/command", dependencies=[Auth])
def queue_agent_command(payload: AgentCommandRequest) -> dict[str, Any]:
    command = payload.command.strip()
    if not command:
        raise HTTPException(status_code=400, detail="command is required.")
    runtime_dir = ROOT_DIR / "runtime"
    runtime_dir.mkdir(exist_ok=True)
    command_file = runtime_dir / "chat_command.json"
    command_file.write_text(
        json.dumps({"command": command}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {"queued": True, "command_file": str(command_file), "command": command}


@app.get("/jobs/migration/pending", dependencies=[Auth])
def list_pending_migration_jobs(limit: int = 1) -> dict[str, Any]:
    jobs = get_pending_migration_jobs()[: max(1, limit)]
    return {"count": len(jobs), "jobs": [_migration_job_payload(job) for job in jobs]}


@app.post("/jobs/migration/run", dependencies=[Auth])
def run_migration_job(payload: RunMigrationRequest | None = None) -> dict[str, Any]:
    jobs = get_pending_migration_jobs()
    job = _select_job(jobs, "map_id", payload.map_id if payload else None)
    if job is None:
        return {"status": "SKIP", "reason": "no_matching_pending_migration_job"}

    increment_migration_batch_count(job.map_id)
    result = _run_with_timing("migration", job, MigrationOrchestrator().process_job)
    result["job"] = _migration_job_payload(job)
    return result


@app.get("/jobs/sql-conversion/pending", dependencies=[Auth])
def list_pending_sql_conversion_jobs(limit: int = 1) -> dict[str, Any]:
    jobs = get_pending_sql_conversion_jobs()[: max(1, limit)]
    return {"count": len(jobs), "jobs": [_sql_job_payload(job) for job in jobs]}


@app.post("/jobs/sql-conversion/run", dependencies=[Auth])
def run_sql_conversion_job(payload: RunSqlRequest | None = None) -> dict[str, Any]:
    job = get_sql_job_by_row_id(payload.row_id) if payload and payload.row_id else None
    if job is None:
        job = _select_job(get_pending_sql_conversion_jobs(), "row_id", None)
    if job is None:
        return {"status": "SKIP", "reason": "no_matching_pending_sql_conversion_job"}

    increment_sql_batch_count(job.row_id)
    result = _run_with_timing("sql_conversion", job, SqlConversionAgent().process_job)
    result["job"] = _sql_job_payload(job)
    return result


@app.get("/jobs/sql-tuning/pending", dependencies=[Auth])
def list_pending_sql_tuning_jobs(limit: int = 1) -> dict[str, Any]:
    jobs = get_tuning_jobs()[: max(1, limit)]
    return {"count": len(jobs), "jobs": [_sql_job_payload(job) for job in jobs]}


@app.post("/jobs/sql-tuning/run", dependencies=[Auth])
def run_sql_tuning_job(payload: RunSqlRequest | None = None) -> dict[str, Any]:
    jobs = get_tuning_jobs()
    job = _select_job(jobs, "row_id", payload.row_id if payload else None)
    if job is None:
        return {"status": "SKIP", "reason": "no_matching_pending_sql_tuning_job"}

    increment_sql_batch_count(job.row_id)
    result = _run_with_timing("sql_tuning", job, SqlTuningAgent().process_job)
    result["job"] = _sql_job_payload(job)
    return result


@app.get("/jobs/sql-formatting/pending", dependencies=[Auth])
def list_pending_sql_formatting_jobs(limit: int = 1) -> dict[str, Any]:
    jobs = get_formatting_jobs()[: max(1, limit)]
    return {"count": len(jobs), "jobs": [_sql_job_payload(job) for job in jobs]}


@app.post("/jobs/sql-formatting/run", dependencies=[Auth])
def run_sql_formatting_job(payload: RunSqlRequest | None = None) -> dict[str, Any]:
    jobs = get_formatting_jobs()
    job = _select_job(jobs, "row_id", payload.row_id if payload else None)
    if job is None:
        return {"status": "SKIP", "reason": "no_matching_pending_sql_formatting_job"}

    increment_sql_batch_count(job.row_id)
    result = _run_with_timing("sql_formatting", job, SqlFormattingAgent().process_job)
    result["job"] = _sql_job_payload(job)
    return result
