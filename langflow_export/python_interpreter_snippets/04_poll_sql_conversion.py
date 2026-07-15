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

from server.repositories.sql.result_repository import get_pending_jobs

jobs = get_pending_jobs()[:LIMIT]
result = {
    "count": len(jobs),
    "jobs": [
        {
            "row_id": job.row_id,
            "space_nm": job.space_nm,
            "sql_id": job.sql_id,
            "status": job.status,
            "tag_kind": job.tag_kind,
            "priority": getattr(job, "priority", None),
        }
        for job in jobs
    ],
}
print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
