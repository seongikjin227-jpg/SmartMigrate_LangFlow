import json
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

PROJECT_PATH = r"C:\Users\11824\Desktop\0609_final-main"
MAP_ID = None  # 예: 1001. None이면 첫 pending job 실행.

root = Path(PROJECT_PATH).expanduser().resolve()
if str(root) not in sys.path:
    sys.path.insert(0, str(root))
load_dotenv(root / ".env", override=True)

from server.agents.migration.orchestrator import MigrationOrchestrator
from server.repositories.migration.repository import get_pending_jobs, increment_batch_count

jobs = get_pending_jobs()
if MAP_ID is not None:
    jobs = [job for job in jobs if int(job.map_id) == int(MAP_ID)]

if not jobs:
    result = {"status": "SKIP", "reason": "no_matching_pending_migration_job"}
else:
    job = jobs[0]
    started = time.perf_counter()
    increment_batch_count(job.map_id)
    final_status = MigrationOrchestrator().process_job(job)
    result = {
        "stage": "migration",
        "map_id": job.map_id,
        "status": final_status,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }

print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
