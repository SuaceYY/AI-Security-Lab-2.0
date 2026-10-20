"""Live OpenCode event stream with explicit role/session separation.

Manual invocation only. Does not publish GitHub issues or auto-approve tools.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from tools.dual_ai.opencode_commands import build_command, load_config
from tools.dual_ai.session_registry import register_session

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "data" / "dual_ai" / "sessions.json"
TOKEN = re.compile(r"(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,})")


def redact(value: str) -> str:
    return TOKEN.sub("[REDACTED]", value)


def summarize_event(event: dict) -> str:
    kind = str(event.get("type", "event"))
    part = event.get("part")
    if not isinstance(part, dict):
        part = {}
    if kind == "text":
        return str(part.get("text", ""))
    if kind == "tool_use":
        tool = part.get("tool", "tool")
        state = part.get("state", {})
        if not isinstance(state, dict):
            state = {}
        status = state.get("status", "")
        title = state.get("title", "")
        return f"{tool}: {status} {title}".strip()
    if kind in ("step_start", "step_finish"):
        return kind
    if kind == "error":
        return json.dumps(event.get("error", event), ensure_ascii=False)
    return kind


def extract_session_id(event: dict) -> str | None:
    for candidate in (event.get("sessionID"), event.get("sessionId"), event.get("session_id")):
        if isinstance(candidate, str) and candidate.startswith("ses_"):
            return candidate
    part = event.get("part")
    if isinstance(part, dict):
        candidate = part.get("sessionID")
        if isinstance(candidate, str) and candidate.startswith("ses_"):
            return candidate
    return None


def stream_role(
    role: str,
    prompt: str,
    *,
    root: Path = ROOT,
    registry: Path = REGISTRY,
    timeout: int = 300,
    output=None,
    config: dict | None = None,
) -> int:
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    output = output or sys.stdout
    config = config or load_config()
    cmd = build_command(role, prompt, root, registry, config)
    model = config["roles"][role]["model"]
    tag = "GLM" if role == "planner" else "DeepSeek"
    print(f"[{tag}] starting {model}", file=output, flush=True)
    proc = subprocess.Popen(
        cmd, cwd=str(root), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
        bufsize=1, creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0,
        start_new_session=os.name != "nt",
    )
    messages: queue.Queue[tuple[str, str | None]] = queue.Queue()

    def reader(stream, source: str):
        try:
            for line in stream:
                messages.put((source, line))
        finally:
            messages.put((source, None))

    threads = [
        threading.Thread(target=reader, args=(proc.stdout, "stdout"), daemon=True),
        threading.Thread(target=reader, args=(proc.stderr, "stderr"), daemon=True),
    ]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + timeout
    closed = set()
    observed_session = None
    timed_out = False
    try:
        while len(closed) < 2 or proc.poll() is None:
            if time.monotonic() >= deadline:
                timed_out = True
                break
            try:
                source, line = messages.get(timeout=min(0.2, max(0.01, deadline - time.monotonic())))
            except queue.Empty:
                continue
            if line is None:
                closed.add(source)
                continue
            if source == "stderr":
                print(f"[{tag}][stderr] {redact(line.rstrip())}", file=output, flush=True)
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                print(f"[{tag}][raw] {redact(line.rstrip())}", file=output, flush=True)
                continue
            if not isinstance(event, dict):
                continue
            session_id = extract_session_id(event)
            if session_id:
                if observed_session and observed_session != session_id:
                    raise RuntimeError(f"{tag} emitted conflicting session IDs")
                observed_session = session_id
            stamp = datetime.now().strftime("%H:%M:%S")
            message = redact(summarize_event(event))
            print(f"[{stamp}][{tag}][{event.get('type', 'event')}] {message}", file=output, flush=True)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
    if timed_out:
        print(f"[{tag}] TIMEOUT after {timeout}s", file=output, flush=True)
        return 124
    if proc.returncode != 0:
        print(f"[{tag}] FAILED exit={proc.returncode}", file=output, flush=True)
        return proc.returncode or 1
    if not observed_session:
        print(f"[{tag}] FAILED: no verified session ID in event stream", file=output, flush=True)
        return 1
    register_session(registry, role, observed_session, model)
    print(f"[{tag}] SUCCESS session={observed_session}", file=output, flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=["planner", "executor"])
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()
    return stream_role(args.role, args.prompt, timeout=args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
