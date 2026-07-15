from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

try:
    from lfx.custom import Component
    from lfx.io import DropdownInput, MessageTextInput, Output, StrInput
    from lfx.schema import Data
except ImportError:
    from langflow.custom import Component
    from langflow.io import DropdownInput, MessageTextInput, Output, StrInput
    from langflow.schema import Data


class MigrationAgentController(Component):
    display_name = "Migration Agent Controller"
    description = "Start, stop, pause, resume, inspect, or send a command to the local migration agent."
    icon = "workflow"
    name = "MigrationAgentController"

    inputs = [
        StrInput(
            name="project_path",
            display_name="Project Path",
            value=r"C:\Users\11824\Desktop\0609_final-main",
            required=True,
            info="Absolute path to the migration-agent project root.",
        ),
        DropdownInput(
            name="action",
            display_name="Action",
            options=["status", "start", "stop", "pause", "resume", "command"],
            value="status",
            required=True,
        ),
        MessageTextInput(
            name="command",
            display_name="Command",
            value="",
            required=False,
            tool_mode=True,
            info="Used only when Action is command. The command is consumed on the next supervisor cycle.",
        ),
    ]

    outputs = [
        Output(
            name="result",
            display_name="Result",
            method="run_action",
        )
    ]

    def _load_project(self) -> Path:
        root = Path(str(self.project_path)).expanduser().resolve()
        if not root.exists():
            raise FileNotFoundError(f"Project path does not exist: {root}")
        main_py = root / "main.py"
        env_path = root / ".env"
        if not main_py.exists():
            raise FileNotFoundError(f"main.py was not found in project path: {root}")
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        if env_path.exists():
            load_dotenv(env_path, override=True)
        return root

    @staticmethod
    def _normalize_status(status: dict[str, Any]) -> dict[str, Any]:
        return {
            "running": bool(status.get("running")),
            "paused": bool(status.get("paused")),
            "pid": status.get("pid"),
            "label": str(status.get("label") or ""),
            "active_job": status.get("active_job"),
        }

    def _send_command(self, root: Path) -> dict[str, Any]:
        command = str(self.command or "").strip()
        if not command:
            raise ValueError("Action is command, but Command is empty.")
        runtime_dir = root / "runtime"
        runtime_dir.mkdir(exist_ok=True)
        command_file = runtime_dir / "chat_command.json"
        command_file.write_text(
            json.dumps({"command": command}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return {
            "message": "Command queued for the next supervisor cycle.",
            "command_file": str(command_file),
            "command": command,
        }

    def run_action(self) -> Data:
        root = self._load_project()

        from app.utils import agent_control

        action = str(self.action or "status").strip().lower()
        if action == "status":
            result: Any = self._normalize_status(agent_control.get_status())
        elif action == "start":
            result = {"message": agent_control.start(), "status": self._normalize_status(agent_control.get_status())}
        elif action == "stop":
            result = {"message": agent_control.stop(), "status": self._normalize_status(agent_control.get_status())}
        elif action == "pause":
            result = {"message": agent_control.pause(), "status": self._normalize_status(agent_control.get_status())}
        elif action == "resume":
            result = {"message": agent_control.resume(), "status": self._normalize_status(agent_control.get_status())}
        elif action == "command":
            result = self._send_command(root)
        else:
            raise ValueError(f"Unsupported action: {action}")

        self.status = json.dumps(result, ensure_ascii=False)
        return Data(data={"action": action, "project_path": str(root), "result": result})
