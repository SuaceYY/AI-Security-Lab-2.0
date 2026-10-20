"""Separate, persistent GLM/DeepSeek OpenCode session IDs.

This registry never reads or mutates the existing Issue Claimer state.
It does not create sessions: session IDs must come from successful real OpenCode calls.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

ROLES = ("planner", "executor")


def register_session(path: Path, role: str, session_id: str, model_id: str) -> dict:
    if role not in ROLES:
        raise ValueError("invalid role")
    if not session_id.startswith("ses_") or len(session_id) < 8:
        raise ValueError("invalid OpenCode session ID")
    if not model_id.strip():
        raise ValueError("model ID required")
    path = Path(path)
    state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"sessions": {}}
    sessions = state.setdefault("sessions", {})
    for other_role, existing in sessions.items():
        if other_role != role and existing.get("session_id") == session_id:
            raise ValueError("GLM and DeepSeek cannot share an OpenCode session")
    current = sessions.get(role)
    if current and current.get("model_id") != model_id:
        raise ValueError("model changed: explicit session reset required")
    sessions[role] = {"session_id": session_id, "model_id": model_id}
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    try:
        temp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    return state


def get_session(path: Path, role: str, model_id: str) -> str | None:
    if role not in ROLES:
        raise ValueError("invalid role")
    path = Path(path)
    if not path.exists():
        return None
    state = json.loads(path.read_text(encoding="utf-8"))
    entry = state.get("sessions", {}).get(role)
    if not entry:
        return None
    if entry["model_id"] != model_id:
        raise ValueError("stored session belongs to a different model")
    return entry["session_id"]
