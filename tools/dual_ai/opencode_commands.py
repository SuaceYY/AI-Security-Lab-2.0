"""Safe OpenCode invocation builder for separate GLM and DeepSeek roles.

No --auto, no --continue, and no cross-role session resume.
"""
from __future__ import annotations

import json
from pathlib import Path

from tools.dual_ai.session_registry import get_session

CONFIG_PATH = Path(__file__).with_name("model_config.json")
VALID_ROLES = {"planner", "executor"}


def load_config(path: Path = CONFIG_PATH) -> dict:
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    roles = config["roles"]
    for role in VALID_ROLES:
        model = roles[role]["model"]
        if not isinstance(model, str) or "/" not in model:
            raise ValueError(f"invalid {role} model")
    if roles["planner"]["model"] == roles["executor"]["model"]:
        raise ValueError("planner and executor must use distinct models")
    return config


def build_command(
    role: str,
    prompt: str,
    project_dir: Path,
    registry_path: Path,
    config: dict,
    *,
    title: str | None = None,
) -> list[str]:
    if role not in VALID_ROLES:
        raise ValueError("unknown AI role")
    if not prompt.strip():
        raise ValueError("empty prompt")
    model = config["roles"][role]["model"]
    if not isinstance(model, str) or "/" not in model:
        raise ValueError("invalid model ID")
    session_id = get_session(registry_path, role, model)
    command = [
        "opencode", "run", "--model", model,
        "--dir", str(Path(project_dir).resolve()),
        "--format", "json",
    ]
    if session_id:
        command.extend(["--session", session_id])
    else:
        command.extend(["--title", title or f"AI-Security-Lab {role}"])
    command.append(prompt)
    return command
