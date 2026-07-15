import json
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_PATH = r"C:\Users\11824\Desktop\0609_final-main"

root = Path(PROJECT_PATH).expanduser().resolve()
if not root.exists():
    raise FileNotFoundError(f"PROJECT_PATH does not exist: {root}")

if str(root) not in sys.path:
    sys.path.insert(0, str(root))

env_path = root / ".env"
if env_path.exists():
    load_dotenv(env_path, override=True)

checks = {
    "project_path": str(root),
    "env_exists": env_path.exists(),
    "main_exists": (root / "main.py").exists(),
}

from server.repositories.migration.repository import get_pending_jobs as get_pending_migration_jobs
from server.repositories.sql.result_repository import get_pending_jobs as get_pending_sql_jobs

checks["imports_ok"] = True
checks["migration_probe_count"] = len(get_pending_migration_jobs()[:1])
checks["sql_conversion_probe_count"] = len(get_pending_sql_jobs()[:1])

result = checks
print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
