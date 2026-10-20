"""One-shot, zero-model probe of two native OpenCode TUI sessions on Windows.

This is transport verification, NOT the GitHub workflow or a model execution test.
Never calls run, prompt_async, submit-prompt, shell, or the existing Worker.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import time
from urllib.error import URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import ProxyHandler, build_opener, Request
import uuid

ROOT = Path(__file__).resolve().parents[2]
ROLES = ("planner", "executor")
SESSION = re.compile(r"ses_[A-Za-z0-9_-]+\Z")


def endpoint(port: int) -> str:
    if not 1024 <= port <= 65535:
        raise ValueError("port must be between 1024 and 65535")
    return f"http://127.0.0.1:{port}"


def ps_command(argv: list[str]) -> list[str]:
    # No shell interpolation of paths/model IDs. PowerShell single quotes escape
    # apostrophes by doubling; encoded commands avoid nested command-line quoting.
    quoted = " ".join("'" + arg.replace("'", "''") + "'" for arg in argv)
    source = f"& {quoted}; exit $LASTEXITCODE"
    encoded = base64.b64encode(source.encode("utf-16-le")).decode("ascii")
    return ["powershell.exe", "-NoLogo", "-NoProfile", "-EncodedCommand", encoded]


class Client:
    def __init__(self, url: str, root: Path):
        parsed = urlsplit(url)
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
                or not parsed.port or parsed.path or parsed.query or parsed.fragment
                or parsed.username or parsed.password):
            raise ValueError("only a plain loopback OpenCode endpoint is allowed")
        self.url, self.root = url, str(root.resolve())
        self.opener = build_opener(ProxyHandler({}))

    def request(self, method: str, path: str, body=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        password = os.environ.get("OPENCODE_SERVER_PASSWORD")
        if password:
            username = os.environ.get("OPENCODE_SERVER_USERNAME", "opencode")
            token = base64.b64encode(f"{username}:{password}".encode()).decode()
            headers["Authorization"] = f"Basic {token}"
        req = Request(self.url + path + "?" + urlencode({"directory": self.root}),
                      data=data, headers=headers, method=method)
        with self.opener.open(req, timeout=5) as response:
            return json.load(response)

    def inspect(self):
        health = self.request("GET", "/global/health")
        if health.get("healthy") is not True:
            raise RuntimeError("OpenCode did not report healthy")
        path = self.request("GET", "/path")
        if os.path.normcase(os.path.realpath(path.get("directory", ""))) != os.path.normcase(self.root):
            raise RuntimeError("OpenCode directory does not match this repository")
        return {"version": health.get("version"), "directory": path["directory"]}

    def create_session(self, role: str) -> str:
        if role not in ROLES:
            raise ValueError("invalid role")
        result = self.request("POST", "/session", {"title": f"Visible transport probe: {role}"})
        session_id = result.get("id", "")
        if not SESSION.fullmatch(session_id):
            raise RuntimeError("invalid session ID from OpenCode")
        return session_id

    def mark(self, session_id: str, role: str, model: str, nonce: str):
        if not SESSION.fullmatch(session_id) or role not in ROLES:
            raise ValueError("invalid session binding")
        provider, model_id = model.split("/", 1)
        marker = f"[连接验证/{role}/{nonce}] 仅验证真实会话传输；未调用模型，未执行开发任务。"
        # The documented context-only API: no model inference or tools.
        result = self.request("POST", f"/session/{session_id}/message", {
            "noReply": True, "model": {"providerID": provider, "modelID": model_id},
            "parts": [{"type": "text", "text": marker}],
        })
        info = result.get("info", {})
        if info.get("role") != "user" or info.get("sessionID") != session_id:
            raise RuntimeError("unexpected noReply response; stop without resending")
        messages = self.request("GET", f"/session/{session_id}/message")
        found = [m for m in messages if m.get("info", {}).get("id") == info.get("id")]
        if len(found) != 1 or not any(p.get("text") == marker for p in found[0].get("parts", [])):
            raise RuntimeError("context marker readback failed; stop without resending")
        if any(m.get("info", {}).get("role") == "assistant" for m in messages):
            raise RuntimeError("unexpected assistant activity in zero-model probe")
        return {"message_id": info["id"], "marker": marker, "readback_ok": True}


def available(port: int):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", port))


def stop_owned(process: subprocess.Popen):
    if process.poll() is None:
        # Only kill the process tree we created, never discover/kill other workers.
        subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)


def launch(ports: tuple[int, int], lifetime: int):
    if os.name != "nt":
        raise RuntimeError("--launch requires Windows; remote Windows is not accessible here")
    if ports[0] == ports[1]:
        raise ValueError("two distinct ports required")
    config = json.loads((Path(__file__).with_name("model_config.json")).read_text(encoding="utf-8"))
    models = {r: config["roles"][r]["model"] for r in ROLES}
    if models["planner"] == models["executor"] or any(not isinstance(m, str) or "/" not in m for m in models.values()):
        raise ValueError("two distinct provider/model IDs required")
    executable = shutil.which("opencode")
    if not executable:
        raise RuntimeError("opencode is not on PATH")
    for port in ports:
        endpoint(port)
        available(port)  # Refuse occupied ports; do not attach to an unknown server.
    clients = {r: Client(endpoint(p), ROOT) for r, p in zip(ROLES, ports)}
    owned = []
    nonce = uuid.uuid4().hex[:12]
    state_path = ROOT / "data" / "dual_ai" / f"visible-probe-{nonce}.json"
    active_path = ROOT / "data" / "dual_ai" / "active_visible_sessions.json"
    if active_path.exists():
        raise RuntimeError(f"active visible TUI binding already exists: {active_path}")
    active_written = False
    state = {"probe_id": nonce, "model_inference_requested": False,
             "visual_verification": "pending", "workflow_verified": False, "roles": {}}
    try:
        for role, port in zip(ROLES, ports):
            server = subprocess.Popen(ps_command([executable, "serve", "--hostname", "127.0.0.1", "--port", str(port)]),
                                      cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                      creationflags=subprocess.CREATE_NO_WINDOW)
            owned.append(server)
            deadline = time.monotonic() + 30
            while True:
                if server.poll() is not None:
                    raise RuntimeError(f"{role} server exited during startup")
                try:
                    meta = clients[role].inspect()
                    break
                except (URLError, TimeoutError):
                    if time.monotonic() >= deadline:
                        raise RuntimeError(f"{role} server startup timed out")
                    time.sleep(0.25)  # Bounded health checks only; no model retries.
            session_id = clients[role].create_session(role)
            if any(s["session_id"] == session_id for s in state["roles"].values()):
                raise RuntimeError("roles unexpectedly share a session")
            state["roles"][role] = {**meta, "url": clients[role].url,
                                      "session_id": session_id, "model": models[role]}
            window = subprocess.Popen(ps_command([executable, "attach", clients[role].url,
                                                  "--dir", str(ROOT), "--session", session_id]),
                                      cwd=ROOT, creationflags=subprocess.CREATE_NEW_CONSOLE)
            owned.append(window)
        for role in ROLES:
            state["roles"][role].update(clients[role].mark(
                state["roles"][role]["session_id"], role, models[role], nonce))
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        active = {
            "schema_version": 1,
            "probe_id": nonce,
            "root": str(ROOT),
            "roles": {
                role: {key: state["roles"][role][key] for key in ("url", "session_id", "model")}
                for role in ROLES
            },
        }
        active_tmp = active_path.with_suffix(".tmp")
        active_tmp.write_text(json.dumps(active, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(active_tmp, active_path)
        active_written = True
        print(json.dumps(state, ensure_ascii=False, indent=2), flush=True)
        print(f"Visible TUI bindings written to {active_path}", flush=True)
        print("接口回读通过。请核对两个原生 TUI 中各自的连接标记。尚未验证模型执行或自动交接。", flush=True)
        print(f"保持窗口最多 {lifetime} 秒；Ctrl+C 关闭本次启动的进程。记录：{state_path}", flush=True)
        deadline = time.monotonic() + lifetime
        while time.monotonic() < deadline:
            if any(p.poll() is not None for p in owned):
                raise RuntimeError("a probe server/TUI closed; verification stopped")
            time.sleep(0.5)
    finally:
        cleanup_failed = False
        for process in reversed(owned):
            try:
                stop_owned(process)
            except (OSError, subprocess.SubprocessError):
                cleanup_failed = True
                print(f"无法关闭本次进程 PID {process.pid}；请关闭对应验证窗口。")
        if active_written:
            try:
                current = json.loads(active_path.read_text(encoding="utf-8"))
                if current.get("probe_id") == nonce:
                    active_path.unlink()
            except (OSError, json.JSONDecodeError):
                print(f"Could not clean up visible TUI binding file: {active_path}")
        if cleanup_failed:
            raise RuntimeError("probe process cleanup incomplete")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--launch", action="store_true", help="Open two NEW native TUI probe windows; zero model calls")
    parser.add_argument("--ports", type=int, nargs=2, default=(41961, 41962))
    parser.add_argument("--lifetime", type=int, default=28800)
    args = parser.parse_args()
    if not 30 <= args.lifetime <= 28800:
        parser.error("lifetime must be 30..28800 seconds")
    try:
        if args.launch:
            launch(tuple(args.ports), args.lifetime)
        else:
            for role, port in zip(ROLES, args.ports):
                print(json.dumps({"role": role, **Client(endpoint(port), ROOT).inspect()}, ensure_ascii=False))
        return 0
    except KeyboardInterrupt:
        print("连接验证已停止；现有 Worker/会话记录未修改。")
        return 130
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError) as exc:
        print(f"BLOCKED: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
