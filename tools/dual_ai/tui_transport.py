"""Loopback-only control of a visible OpenCode TUI.

This transport submits prompts through the TUI control API so the running TUI
renders the same interaction the operator sees. Calls are never retried because
a timeout after POST has an ambiguous delivery outcome.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import ProxyHandler, Request, build_opener


class TUITransportError(RuntimeError):
    """The visible TUI could not safely accept or confirm a prompt."""


class TUIClient:
    def __init__(self, base_url: str, project_dir: Path, *, timeout: float = 5.0):
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or not parsed.port
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            raise ValueError("only a plain 127.0.0.1 OpenCode TUI endpoint is allowed")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.base_url = base_url
        self.project_dir = str(Path(project_dir).resolve())
        self.timeout = timeout
        self.opener = build_opener(ProxyHandler({}))

    def _request(self, method: str, path: str, body: dict | None = None, *, timeout: float | None = None):
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        password = os.environ.get("OPENCODE_SERVER_PASSWORD")
        if password:
            username = os.environ.get("OPENCODE_SERVER_USERNAME", "opencode")
            encoded = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
            headers["Authorization"] = f"Basic {encoded}"
        separator = "&" if "?" in path else "?"
        url = self.base_url + path + separator + urlencode({"directory": self.project_dir})
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=self.timeout if timeout is None else timeout) as response:
                raw = response.read()
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise TUITransportError(f"OpenCode TUI request failed: {type(exc).__name__}") from exc
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TUITransportError("OpenCode returned invalid JSON") from exc

    def inspect(self) -> dict:
        health = self._request("GET", "/global/health")
        if not isinstance(health, dict) or health.get("healthy") is not True:
            raise TUITransportError("OpenCode did not report a healthy server")
        path = self._request("GET", "/path")
        if not isinstance(path, dict) or not isinstance(path.get("directory"), str):
            raise TUITransportError("OpenCode did not report its project directory")
        actual = os.path.normcase(os.path.realpath(path["directory"]))
        expected = os.path.normcase(self.project_dir)
        if actual != expected:
            raise TUITransportError("OpenCode project directory does not match the repository")
        return {"version": health.get("version"), "directory": path["directory"]}

    def append_prompt(self, text: str) -> None:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("prompt text must not be empty")
        accepted = self._request("POST", "/tui/append-prompt", {"text": text})
        if accepted is not True:
            raise TUITransportError("TUI did not confirm prompt insertion")

    def submit_prompt(self) -> None:
        accepted = self._request("POST", "/tui/submit-prompt")
        if accepted is not True:
            raise TUITransportError("TUI did not confirm prompt submission")

    def send_prompt(self, text: str) -> None:
        self.append_prompt(text)
        self.submit_prompt()

    def clear_prompt(self) -> None:
        cleared = self._request("POST", "/tui/clear-prompt")
        if cleared is not True:
            raise TUITransportError("TUI did not confirm prompt clearing")

    @staticmethod
    def _validate_session_id(session_id: str) -> str:
        import re
        if not isinstance(session_id, str) or not re.fullmatch(r"ses_[A-Za-z0-9_-]+", session_id):
            raise ValueError("invalid OpenCode session ID")
        return session_id

    def get_session(self, session_id: str) -> dict:
        session_id = self._validate_session_id(session_id)
        result = self._request("GET", f"/session/{session_id}")
        if not isinstance(result, dict) or result.get("id") != session_id:
            raise TUITransportError("OpenCode did not return the bound session")
        directory = result.get("directory")
        if not isinstance(directory, str) or os.path.normcase(os.path.realpath(directory)) != os.path.normcase(self.project_dir):
            raise TUITransportError("bound session directory does not match the repository")
        return result

    def get_session_status(self, session_id: str) -> str:
        session_id = self._validate_session_id(session_id)
        result = self._request("GET", "/session/status")
        if not isinstance(result, dict) or session_id not in result:
            raise TUITransportError("OpenCode did not return status for the bound session")
        status = result[session_id]
        if isinstance(status, str):
            kind = status
        elif isinstance(status, dict):
            kind = status.get("type", status.get("status"))
        else:
            kind = None
        if kind not in {"idle", "busy", "retry"}:
            raise TUITransportError("OpenCode returned an unknown session status")
        return kind

    def get_messages(self, session_id: str) -> list[dict]:
        session_id = self._validate_session_id(session_id)
        result = self._request("GET", f"/session/{session_id}/message?limit=100")
        if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
            raise TUITransportError("OpenCode returned an invalid session message list")
        return result

    def send_session_message(self, session_id: str, prompt: str, model: str, *, timeout: float | None = None) -> dict:
        session_id = self._validate_session_id(session_id)
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt text must not be empty")
        if not isinstance(model, str) or model.count("/") != 1:
            raise ValueError("model must be provider/model")
        provider_id, model_id = model.split("/", 1)
        if not provider_id or not model_id or any(ch.isspace() for ch in model):
            raise ValueError("model must be provider/model")
        result = self._request("POST", f"/session/{session_id}/message", {
            "model": {"providerID": provider_id, "modelID": model_id},
            "parts": [{"type": "text", "text": prompt}],
        }, timeout=timeout)
        if not isinstance(result, dict):
            raise TUITransportError("OpenCode did not return a session response")
        info = result.get("info")
        if not isinstance(info, dict) or info.get("sessionID") != session_id:
            raise TUITransportError("OpenCode response does not match the bound session")
        return result

