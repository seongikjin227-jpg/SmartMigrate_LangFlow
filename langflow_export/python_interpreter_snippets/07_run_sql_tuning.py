import json
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

PROJECT_PATH = r"C:\Users\11824\Desktop\0609_final-main"
ROW_ID = None  # 예: "AAAZ9xAAEAAABrXAAA". None이면 첫 pending tuning job 실행.

root = Path(PROJECT_PATH).expanduser().resolve()
if str(root) not in sys.path:
    sys.path.insert(0, str(root))
load_dotenv(root / ".env", override=True)

from server.agents.sql_tuning.agent import SqlTuningAgent
from server.repositories.sql.result_repository import get_tuning_jobs, increment_batch_count

jobs = get_tuning_jobs()
if ROW_ID:
    jobs = [job for job in jobs if str(job.row_id) == str(ROW_ID)]

if not jobs:
    result = {"status": "SKIP", "reason": "no_matching_pending_sql_tuning_job"}
else:
    job = jobs[0]
    started = time.perf_counter()
    increment_batch_count(job.row_id)
    final_status = SqlTuningAgent().process_job(job)
    result = {
        "stage": "sql_tuning",
        "row_id": job.row_id,
        "space_nm": job.space_nm,
        "sql_id": job.sql_id,
        "status": final_status,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }

print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
