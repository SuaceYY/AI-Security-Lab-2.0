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
        # Do not let HTTP_PROXY/HTTPS_PROXY redirect loopback traffic elsewhere.
        self.opener = build_opener(ProxyHandler({}))

    def _request(self, method: str, path: str, body: dict | None = None):
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        password = os.environ.get("OPENCODE_SERVER_PASSWORD")
        if password:
            username = os.environ.get("OPENCODE_SERVER_USERNAME", "opencode")
            encoded = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
            headers["Authorization"] = f"Basic {encoded}"
        url = self.base_url + path + "?" + urlencode({"directory": self.project_dir})
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read()
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            # Never retry POSTs: a lost response does not prove the TUI rejected it.
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
        """Insert and submit exactly once; do not retry after any ambiguous failure."""
        self.append_prompt(text)
        self.submit_prompt()

    def clear_prompt(self) -> None:
        cleared = self._request("POST", "/tui/clear-prompt")
        if cleared is not True:
            raise TUITransportError("TUI did not confirm prompt clearing")
