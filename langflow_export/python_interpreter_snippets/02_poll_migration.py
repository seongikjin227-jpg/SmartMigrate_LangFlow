import json
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_PATH = r"C:\Users\11824\Desktop\0609_final-main"
LIMIT = 1

root = Path(PROJECT_PATH).expanduser().resolve()
if str(root) not in sys.path:
    sys.path.insert(0, str(root))
load_dotenv(root / ".env", override=True)

from server.repositories.migration.repository import get_pending_jobs

jobs = get_pending_jobs()[:LIMIT]
result = {
    "count": len(jobs),
    "jobs": [
        {
            "map_id": job.map_id,
            "map_type": job.map_type,
            "fr_table": job.fr_table,
            "to_table": job.to_table,
            "priority": job.priority,
            "prior_map_id": getattr(job, "prior_map_id", None),
            "retry_count": getattr(job, "retry_count", 0) or 0,
            "status": job.status,
            "batch_cnt": getattr(job, "batch_cnt", 0) or 0,
        }
        for job in jobs
    ],
}
print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
