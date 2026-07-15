import json
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_PATH = r"C:\Users\11824\Desktop\0609_final-main"

root = Path(PROJECT_PATH).expanduser().resolve()
if str(root) not in sys.path:
    sys.path.insert(0, str(root))
load_dotenv(root / ".env", override=True)

from app.utils.agent_control import get_status

result = get_status()
print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
