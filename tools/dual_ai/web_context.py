"""Visible bound-session calls with context checkpoints and human-stop handling."""
from __future__ import annotations

import json
import queue
import threading
import time

from tools.dual_ai.tui_transport import TUIClient
from tools.dual_ai.visible_tui import (
    load_bindings, message_text, message_role, verify_response_model,
)
from tools.dual_ai.web_batch import HumanRequired


def context_usage(messages: list[dict]) -> tuple[int, str]:
    assistants = [m for m in messages if isinstance(m.get("info"), dict)
                  and m["info"].get("role") == "assistant"]
    if assistants:
        latest = max(enumerate(assistants),
                     key=lambda x: (x[1]["info"].get("time", {}).get("created", 0), x[0]))[1]
        tokens = latest["info"].get("tokens", {})
        if isinstance(tokens, dict):
            cache = tokens.get("cache", {})
            cache = cache if isinstance(cache, dict) else {}
            values = [tokens.get("input"), cache.get("read", 0), cache.get("write", 0)]
            if all(isinstance(v, (int, float)) and v >= 0 for v in values):
                return int(sum(values)), "reported_input_plus_cache"
    # This fallback is a conservative estimate, never labelled actual usage.
    text = json.dumps(messages, ensure_ascii=False)
    return (len(text) + 1) // 2, "estimated_from_transcript_chars"


class VisibleWebSessions:
    def __init__(self, root, checkpoint, *, client_factory=TUIClient,
                 soft_context_tokens=60000, timeout=600):
        self.root = root
        self.checkpoint = checkpoint
        self.factory = client_factory
        self.bindings = load_bindings()
        self.soft_context_tokens = soft_context_tokens
        self.timeout = timeout
        self.session_ids = {role: b["session_id"] for role, b in self.bindings.items()}

    def client(self, role):
        b = self.bindings[role]
        client = self.factory(b["url"], self.root, timeout=5)
        client.inspect()
        client.get_session(b["session_id"])
        return client, b

    @staticmethod
    def pending(client, session_id):
        requests = []
        for path in ("/permission", "/question"):
            data = client._request("GET", path)
            if not isinstance(data, list):
                raise HumanRequired("OpenCode 未提供可核对的权限/提问状态，需检查本机版本。")
            requests.extend(x for x in data if isinstance(x, dict) and x.get("sessionID") == session_id)
        return requests

    def check(self):
        """Read-only API compatibility check; no model request or compaction."""
        checked = {}
        for role in ("planner", "executor"):
            client, binding = self.client(role)
            sid = binding["session_id"]
            if not isinstance(binding.get("model"), str) or "/" not in binding["model"]:
                raise HumanRequired(role + " 未绑定有效模型。")
            if client.get_session_status(sid) != "idle" or self.pending(client, sid):
                raise HumanRequired(role + " 会话未空闲或有人工问题。")
            tools = client._request("GET", "/experimental/tool/ids")
            if not isinstance(tools, list) or any(not isinstance(t, str) for t in tools):
                raise HumanRequired("无法核对 OpenCode 工具 API。")
            checked[role] = {"session_id": sid, "model": binding["model"], "ready": True}
        return checked

    def prepare(self, role, prompt):
        client, b = self.client(role)
        sid = b["session_id"]
        if client.get_session_status(sid) != "idle":
            raise HumanRequired(role + " 会话正在执行其他工作，请先核对窗口。")
        requests = self.pending(client, sid)
        if requests:
            raise HumanRequired("OpenCode 等待人工回答，请检查绑定窗口。")
        messages = client.get_messages(sid)
        tokens, source = context_usage(messages)
        limit = self.soft_context_tokens
        providers = client._request("GET", "/provider")
        if isinstance(providers, dict):
            provider_id, model_id = b["model"].split("/", 1)
            for provider in providers.get("all", []):
                if not isinstance(provider, dict) or provider.get("id") != provider_id:
                    continue
                models = provider.get("models", {})
                model = models.get(model_id, {}) if isinstance(models, dict) else {}
                limits = model.get("limit", {}) if isinstance(model, dict) else {}
                actual = limits.get("context") if isinstance(limits, dict) else None
                if isinstance(actual, int) and actual > 0:
                    limit = min(limit, actual)
        projected = tokens + (len(prompt) + 1) // 2 + 8000
        self.checkpoint("context_usage", {
            "role": role, "session_id": sid, "input_tokens": tokens,
            "usage_source": source, "soft_limit": limit, "projected_with_reserve": projected,
        })
        if projected >= int(limit * 0.75) or (len(messages) >= 100 and source.startswith("estimated")):
            self.checkpoint("context_before_compaction", {
                "role": role, "session_id": sid, "reason": "context_threshold",
            })
            provider_id, model_id = b["model"].split("/", 1)
            result = self.request(client, sid, "/session/" + sid + "/summarize",
                                  {"providerID": provider_id, "modelID": model_id}, timeout=180)
            if result is not True:
                raise HumanRequired("OpenCode 未确认上下文摘要，停止本批。")
            deadline = time.monotonic() + 30
            while client.get_session_status(sid) != "idle":
                if time.monotonic() >= deadline:
                    raise HumanRequired("上下文压缩后会话仍未空闲，请核对窗口。")
                time.sleep(0.5)
            self.checkpoint("context_compacted", {"role": role, "session_id": sid})
        if (len(prompt) + 1) // 2 + 8000 >= limit * 0.75:
            raise HumanRequired("单次任务超过上下文安全预算，请人工缩小任务。")
        return client, b

    def call(self, role, prompt):
        client, b = self.prepare(role, prompt)
        sid = b["session_id"]
        provider_id, model_id = b["model"].split("/", 1)
        body = {
            "model": {"providerID": provider_id, "modelID": model_id},
            "agent": "plan" if role == "planner" else "build",
            "parts": [{"type": "text", "text": prompt}],
        }
        if role == "planner":
            tool_ids = client._request("GET", "/experimental/tool/ids")
            if not isinstance(tool_ids, list) or any(not isinstance(t, str) for t in tool_ids):
                raise HumanRequired("无法核对 GLM 只读工具清单。")
            readonly = {"read", "glob", "grep", "webfetch"}
            body["tools"] = {name: name in readonly for name in tool_ids}
        print("[" + ("GLM" if role == "planner" else "DeepSeek") + "] 可见会话：" + sid, flush=True)
        response = self.request(client, sid, "/session/" + sid + "/message", body, timeout=self.timeout)
        if (not isinstance(response, dict) or not isinstance(response.get("info"), dict)
                or response["info"].get("sessionID") != sid or message_role(response) != "assistant"):
            raise HumanRequired("模型响应不是绑定会话的 assistant 结果。")
        verify_response_model(response, b["model"])
        info = response["info"]
        if info.get("error"):
            raise HumanRequired("模型错误或额度不可用：" + json.dumps(info["error"], ensure_ascii=False))
        cost = info.get("cost")
        if isinstance(cost, (int, float)) and cost > 0:
            raise HumanRequired("本批配置为免费模型，但接口报告计费，请核对额度。")
        text = message_text(response).strip()
        if not text:
            raise HumanRequired("模型没有返回可核对的结果。")
        print(text, flush=True)
        self.checkpoint("model_usage", {
            "role": role, "session_id": sid, "message_id": info.get("id"),
            "tokens": info.get("tokens"), "reported_cost": cost,
        })
        return text

    def request(self, client, sid, path, body, *, timeout):
        """Monitor the exact visible request and abort on every uncertain exit."""
        replies = queue.Queue()

        def send():
            try:
                replies.put((True, client._request("POST", path, body, timeout=timeout)))
            except Exception as exc:
                replies.put((False, exc))

        thread = threading.Thread(target=send, daemon=True)
        thread.start()
        deadline = time.monotonic() + timeout
        completed = False
        try:
            while True:
                try:
                    ok, response = replies.get(timeout=1)
                    if not ok:
                        raise HumanRequired("会话请求失败，投递状态可能不明确；不会重投：" + str(response))
                    completed = True
                    break
                except queue.Empty:
                    pending = self.pending(client, sid)
                    if pending:
                        raise HumanRequired("OpenCode 需要人工回答，请检查绑定窗口；请求ID：" +
                                            ", ".join(str(x.get("id", "?")) for x in pending))
                    if time.monotonic() >= deadline:
                        raise HumanRequired("本轮会话超时，请核对窗口后人工恢复。")
        finally:
            if not completed:
                try:
                    client._request("POST", "/session/" + sid + "/abort", timeout=5)
                except Exception:
                    print("中止请求未获确认，请在绑定 TUI 窗口手动停止。", flush=True)
                thread.join(timeout=5)
        return response
