import json
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

PROJECT_PATH = r"C:\Users\11824\Desktop\0609_final-main"
ROW_ID = None  # 예: "AAAZ9xAAEAAABrXAAA". None이면 첫 pending job 실행.

root = Path(PROJECT_PATH).expanduser().resolve()
if str(root) not in sys.path:
    sys.path.insert(0, str(root))
load_dotenv(root / ".env", override=True)

from server.agents.sql_conversion.agent import SqlConversionAgent
from server.repositories.sql.result_repository import (
    get_pending_jobs,
    get_sql_job_by_row_id,
    increment_batch_count,
)

job = get_sql_job_by_row_id(ROW_ID) if ROW_ID else None
if job is None:
    jobs = get_pending_jobs()
    job = jobs[0] if jobs else None

if job is None:
    result = {"status": "SKIP", "reason": "no_matching_pending_sql_conversion_job"}
else:
    started = time.perf_counter()
    increment_batch_count(job.row_id)
    final_status = SqlConversionAgent().process_job(job)
    result = {
        "stage": "sql_conversion",
        "row_id": job.row_id,
        "space_nm": job.space_nm,
        "sql_id": job.sql_id,
        "status": final_status,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }

print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
