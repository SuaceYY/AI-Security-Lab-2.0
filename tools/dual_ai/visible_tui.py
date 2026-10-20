"""Run a role prompt inside its already attached, visible OpenCode TUI session."""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

from tools.dual_ai.tui_transport import TUIClient, TUITransportError

ROOT = Path(__file__).resolve().parents[2]
SESSION_FILE = ROOT / "data" / "dual_ai" / "active_visible_sessions.json"
ROLES = {"planner", "executor"}
SESSION_RE = re.compile(r"ses_[A-Za-z0-9_-]+\Z")


def load_bindings(path: Path = SESSION_FILE) -> dict[str, dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TUITransportError(f"visible TUI binding file unavailable: {path}") from exc
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise TUITransportError("unsupported visible TUI binding file")
    roles = data.get("roles")
    if not isinstance(roles, dict):
        raise TUITransportError("visible TUI roles are missing")
    bindings = {}
    for role in ROLES:
        binding = roles.get(role)
        if not isinstance(binding, dict):
            raise TUITransportError(f"visible TUI binding missing role: {role}")
        url, session_id = binding.get("url"), binding.get("session_id")
        if not isinstance(url, str) or not isinstance(session_id, str) or not SESSION_RE.fullmatch(session_id):
            raise TUITransportError(f"invalid visible TUI binding for role: {role}")
        bindings[role] = {"url": url, "session_id": session_id, "model": binding.get("model")}
    if bindings["planner"]["url"] == bindings["executor"]["url"]:
        raise TUITransportError("planner and executor must use separate OpenCode servers")
    if bindings["planner"]["session_id"] == bindings["executor"]["session_id"]:
        raise TUITransportError("planner and executor must use separate OpenCode sessions")
    return bindings


def load_binding(role: str, path: Path = SESSION_FILE) -> dict:
    if role not in ROLES:
        raise ValueError("role must be planner or executor")
    return load_bindings(path)[role]


def message_text(message: dict) -> str:
    parts = message.get("parts")
    if not isinstance(parts, list):
        return ""
    return "".join(
        part.get("text", "")
        for part in parts
        if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
    )


def message_id(message: dict) -> str | None:
    info = message.get("info")
    value = info.get("id") if isinstance(info, dict) else None
    return value if isinstance(value, str) else None


def message_role(message: dict) -> str | None:
    info = message.get("info")
    value = info.get("role") if isinstance(info, dict) else None
    return value if isinstance(value, str) else None


def run_prompt(
    role: str,
    prompt: str,
    *,
    root: Path = ROOT,
    binding_file: Path = SESSION_FILE,
    timeout: int = 300,
    output=None,
    client_factory=TUIClient,
) -> tuple[str, str]:
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must not be empty")
    output = output or sys.stdout
    binding = load_binding(role, binding_file)
    client = client_factory(binding["url"], root, timeout=5)
    client.inspect()
    session_id = binding["session_id"]
    client.get_session(session_id)
    if client.get_session_status(session_id) != "idle":
        raise TUITransportError("bound visible TUI session is not idle; refusing to queue a prompt")
    before = client.get_messages(session_id)
    prior_ids = {message_id(item) for item in before if message_id(item)}

    # Exactly one submission. If the response is ambiguous, stop; never resend.
    client.send_prompt(prompt)
    tag = "GLM" if role == "planner" else "DeepSeek"
    print(f"[{tag}] prompt submitted to the bound visible TUI session; watch the native window.", file=output, flush=True)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        messages = client.get_messages(session_id)
        new_messages = [item for item in messages if message_id(item) not in prior_ids]
        user = next((item for item in new_messages if message_role(item) == "user" and message_text(item) == prompt), None)
        status = client.get_session_status(session_id)
        if user is not None:
            user_id = message_id(user)
            after_user = []
            found_user = False
            for item in new_messages:
                if message_id(item) == user_id:
                    found_user = True
                    continue
                if found_user and message_role(item) == "assistant":
                    after_user.append(item)
            if status == "idle" and after_user:
                text = "\n".join(message_text(item) for item in after_user if message_text(item)).strip()
                if not text:
                    raise TUITransportError("visible session finished without assistant text")
                print(text, file=output, flush=True)
                return text, session_id
        time.sleep(min(0.5, max(0.05, deadline - time.monotonic())))

    raise TUITransportError(
        f"timed out waiting for visible {role} session {session_id}; prompt was not resent"
    )

