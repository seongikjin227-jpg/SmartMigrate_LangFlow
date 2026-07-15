from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv

try:
    from lfx.custom import Component
    from lfx.io import DataInput, IntInput, Output, StrInput
    from lfx.schema import Data
except ImportError:
    from langflow.custom import Component
    from langflow.io import DataInput, IntInput, Output, StrInput
    from langflow.schema import Data


DEFAULT_PROJECT_PATH = r"C:\Users\11824\Desktop\0609_final-main"


def _as_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Data):
        return dict(value.data or {})
    if isinstance(value, dict):
        return value
    data = getattr(value, "data", None)
    return dict(data or {}) if isinstance(data, dict) else {}


def _job_value(job: Any, *names: str) -> Any:
    for name in names:
        if isinstance(job, dict) and name in job:
            return job.get(name)
        if hasattr(job, name):
            return getattr(job, name)
    return None


def _sql_label(job: Any, fallback: str = "") -> str:
    space_nm = _job_value(job, "space_nm", "SPACE_NM")
    sql_id = _job_value(job, "sql_id", "SQL_ID")
    if space_nm and sql_id:
        return f"{space_nm}.{sql_id}"
    return str(sql_id or space_nm or fallback)


class MigrationPipelineBase(Component):
    project_input = StrInput(
        name="project_path",
        display_name="Project Path",
        value=DEFAULT_PROJECT_PATH,
        required=False,
        info="Absolute path to the copied migration-agent project root.",
    )
    incoming_input = DataInput(
        name="incoming",
        display_name="Incoming",
        required=False,
        info="Optional previous pipeline node output.",
    )

    def _context(self) -> dict[str, Any]:
        incoming = _as_dict(getattr(self, "incoming", None))
        project_path = str(
            getattr(self, "project_path", "")
            or incoming.get("project_path")
            or DEFAULT_PROJECT_PATH
        )
        root = Path(project_path).expanduser().resolve()
        if not root.exists():
            raise FileNotFoundError(f"Project path does not exist: {root}")
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        env_path = root / ".env"
        if env_path.exists():
            load_dotenv(env_path, override=True)
        return {"project_path": str(root), "history": list(incoming.get("history") or [])}

    def _output(self, action: str, result: dict[str, Any]) -> Data:
        ctx = self._context()
        entry = {"node": self.name, "action": action, "result": result}
        history = [*ctx["history"], entry]
        payload = {
            "project_path": ctx["project_path"],
            "last_action": action,
            "last_result": result,
            "history": history,
        }
        self.status = json.dumps(result, ensure_ascii=False, default=str)
        return Data(data=payload)


class LoadMigrationProject(MigrationPipelineBase):
    display_name = "01 Load Migration Project"
    description = "Loads .env and makes the copied migration project importable."
    icon = "folder-open"
    name = "LoadMigrationProject"
    inputs = [MigrationPipelineBase.project_input]
    outputs = [Output(name="project", display_name="Project", method="load_project")]

    def load_project(self) -> Data:
        ctx = self._context()
        root = Path(ctx["project_path"])
        result = {
            "project_path": str(root),
            "env_exists": (root / ".env").exists(),
            "main_exists": (root / "main.py").exists(),
        }
        if not result["main_exists"]:
            raise FileNotFoundError(f"main.py was not found in project path: {root}")
        return self._output("load_project", result)


class PollMigrationJobs(MigrationPipelineBase):
    display_name = "02 Poll DB Migration Jobs"
    description = "Queries pending DB migration jobs."
    icon = "database"
    name = "PollMigrationJobs"
    inputs = [MigrationPipelineBase.project_input, MigrationPipelineBase.incoming_input, IntInput(name="limit", display_name="Limit", value=1)]
    outputs = [Output(name="jobs", display_name="Jobs", method="poll_jobs")]

    def poll_jobs(self) -> Data:
        self._context()
        from server.repositories.migration.repository import get_pending_jobs

        jobs = get_pending_jobs()[: int(self.limit or 1)]
        result = {
            "count": len(jobs),
            "jobs": [
                {
                    "map_id": job.map_id,
                    "map_type": job.map_type,
                    "fr_table": job.fr_table,
                    "to_table": job.to_table,
                    "priority": job.priority,
                    "status": job.status,
                }
                for job in jobs
            ],
        }
        return self._output("poll_migration_jobs", result)


class RunMigrationJob(MigrationPipelineBase):
    display_name = "03 Run DB Migration Job"
    description = "Runs the first pending DB migration job."
    icon = "play"
    name = "RunMigrationJob"
    inputs = [MigrationPipelineBase.project_input, MigrationPipelineBase.incoming_input]
    outputs = [Output(name="result", display_name="Result", method="run_job")]

    def run_job(self) -> Data:
        self._context()
        from server.agents.migration.orchestrator import MigrationOrchestrator
        from server.repositories.migration.repository import get_pending_jobs, increment_batch_count

        jobs = get_pending_jobs()
        if not jobs:
            return self._output("run_migration_job", {"status": "SKIP", "reason": "no_pending_job"})
        job = jobs[0]
        started = time.perf_counter()
        increment_batch_count(job.map_id)
        final_status = MigrationOrchestrator().process_job(job)
        return self._output(
            "run_migration_job",
            {
                "map_id": job.map_id,
                "status": final_status,
                "elapsed_seconds": round(time.perf_counter() - started, 3),
            },
        )


class PollSqlConversionJobs(MigrationPipelineBase):
    display_name = "04 Poll SQL Conversion Jobs"
    description = "Queries pending SQL conversion jobs."
    icon = "search"
    name = "PollSqlConversionJobs"
    inputs = [MigrationPipelineBase.project_input, MigrationPipelineBase.incoming_input, IntInput(name="limit", display_name="Limit", value=1)]
    outputs = [Output(name="jobs", display_name="Jobs", method="poll_jobs")]

    def poll_jobs(self) -> Data:
        self._context()
        from server.repositories.sql.result_repository import get_pending_jobs

        jobs = get_pending_jobs()[: int(self.limit or 1)]
        result = {
            "count": len(jobs),
            "jobs": [
                {
                    "row_id": job.row_id,
                    "space_nm": job.space_nm,
                    "sql_id": job.sql_id,
                    "status": job.status,
                    "priority": getattr(job, "priority", None),
                }
                for job in jobs
            ],
        }
        return self._output("poll_sql_conversion_jobs", result)


class RunSqlConversionJob(MigrationPipelineBase):
    display_name = "05 Run SQL Conversion Job"
    description = "Runs the first pending SQL conversion job."
    icon = "shuffle"
    name = "RunSqlConversionJob"
    inputs = [MigrationPipelineBase.project_input, MigrationPipelineBase.incoming_input]
    outputs = [Output(name="result", display_name="Result", method="run_job")]

    def run_job(self) -> Data:
        self._context()
        from server.agents.sql_conversion.agent import SqlConversionAgent
        from server.repositories.sql.result_repository import get_pending_jobs, increment_batch_count

        jobs = get_pending_jobs()
        if not jobs:
            return self._output("run_sql_conversion_job", {"status": "SKIP", "reason": "no_pending_job"})
        job = jobs[0]
        started = time.perf_counter()
        increment_batch_count(job.row_id)
        final_status = SqlConversionAgent().process_job(job)
        return self._output(
            "run_sql_conversion_job",
            {
                "row_id": job.row_id,
                "job": _sql_label(job, job.row_id),
                "status": final_status,
                "elapsed_seconds": round(time.perf_counter() - started, 3),
            },
        )


class PollTuningJobs(MigrationPipelineBase):
    display_name = "06 Poll SQL Tuning Jobs"
    description = "Queries pending SQL tuning jobs."
    icon = "gauge"
    name = "PollTuningJobs"
    inputs = [MigrationPipelineBase.project_input, MigrationPipelineBase.incoming_input, IntInput(name="limit", display_name="Limit", value=1)]
    outputs = [Output(name="jobs", display_name="Jobs", method="poll_jobs")]

    def poll_jobs(self) -> Data:
        self._context()
        from server.repositories.sql.result_repository import get_tuning_jobs

        jobs = get_tuning_jobs()[: int(self.limit or 1)]
        result = {
            "count": len(jobs),
            "jobs": [
                {
                    "row_id": job.row_id,
                    "space_nm": job.space_nm,
                    "sql_id": job.sql_id,
                    "tuned_test": job.tuned_test,
                }
                for job in jobs
            ],
        }
        return self._output("poll_tuning_jobs", result)


class RunTuningJob(MigrationPipelineBase):
    display_name = "07 Run SQL Tuning Job"
    description = "Runs the first pending SQL tuning job."
    icon = "wand-sparkles"
    name = "RunTuningJob"
    inputs = [MigrationPipelineBase.project_input, MigrationPipelineBase.incoming_input]
    outputs = [Output(name="result", display_name="Result", method="run_job")]

    def run_job(self) -> Data:
        self._context()
        from server.agents.sql_tuning.agent import SqlTuningAgent
        from server.repositories.sql.result_repository import get_tuning_jobs, increment_batch_count

        jobs = get_tuning_jobs()
        if not jobs:
            return self._output("run_tuning_job", {"status": "SKIP", "reason": "no_pending_job"})
        job = jobs[0]
        started = time.perf_counter()
        increment_batch_count(job.row_id)
        final_status = SqlTuningAgent().process_job(job)
        return self._output(
            "run_tuning_job",
            {
                "row_id": job.row_id,
                "job": _sql_label(job, job.row_id),
                "status": final_status,
                "elapsed_seconds": round(time.perf_counter() - started, 3),
            },
        )


class PollFormattingJobs(MigrationPipelineBase):
    display_name = "08 Poll SQL Formatting Jobs"
    description = "Queries pending SQL formatting jobs."
    icon = "align-left"
    name = "PollFormattingJobs"
    inputs = [MigrationPipelineBase.project_input, MigrationPipelineBase.incoming_input, IntInput(name="limit", display_name="Limit", value=1)]
    outputs = [Output(name="jobs", display_name="Jobs", method="poll_jobs")]

    def poll_jobs(self) -> Data:
        self._context()
        from server.repositories.sql.result_repository import get_formatting_jobs

        jobs = get_formatting_jobs()[: int(self.limit or 1)]
        result = {
            "count": len(jobs),
            "jobs": [
                {
                    "row_id": job.row_id,
                    "space_nm": job.space_nm,
                    "sql_id": job.sql_id,
                }
                for job in jobs
            ],
        }
        return self._output("poll_formatting_jobs", result)


class RunFormattingJob(MigrationPipelineBase):
    display_name = "09 Run SQL Formatting Job"
    description = "Runs the first pending SQL formatting job."
    icon = "text"
    name = "RunFormattingJob"
    inputs = [MigrationPipelineBase.project_input, MigrationPipelineBase.incoming_input]
    outputs = [Output(name="result", display_name="Result", method="run_job")]

    def run_job(self) -> Data:
        self._context()
        from server.agents.sql_formatting.agent import SqlFormattingAgent
        from server.repositories.sql.result_repository import get_formatting_jobs, increment_batch_count

        jobs = get_formatting_jobs()
        if not jobs:
            return self._output("run_formatting_job", {"status": "SKIP", "reason": "no_pending_job"})
        job = jobs[0]
        started = time.perf_counter()
        increment_batch_count(job.row_id)
        final_status = SqlFormattingAgent().process_job(job)
        return self._output(
            "run_formatting_job",
            {
                "row_id": job.row_id,
                "job": _sql_label(job, job.row_id),
                "status": final_status,
                "elapsed_seconds": round(time.perf_counter() - started, 3),
            },
        )


class PipelineSummary(MigrationPipelineBase):
    display_name = "10 Pipeline Summary"
    description = "Returns a compact summary of the visual Langflow pipeline run."
    icon = "list-checks"
    name = "PipelineSummary"
    inputs = [MigrationPipelineBase.project_input, MigrationPipelineBase.incoming_input]
    outputs = [Output(name="summary", display_name="Summary", method="summarize")]

    def summarize(self) -> Data:
        incoming = _as_dict(getattr(self, "incoming", None))
        history = list(incoming.get("history") or [])
        result = {
            "steps": len(history),
            "last_action": incoming.get("last_action"),
            "last_result": incoming.get("last_result"),
            "actions": [item.get("action") for item in history],
        }
        return self._output("pipeline_summary", result)
