"""Bound visible session checks; all clients are fake, no actual AI requests."""
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.dual_ai.web_batch import HumanRequired
from tools.dual_ai.web_context import VisibleWebSessions, context_usage

BINDINGS = {
    "planner": {"url": "http://127.0.0.1:4101", "session_id": "ses_glm", "model": "local/glm"},
    "executor": {"url": "http://127.0.0.1:4102", "session_id": "ses_ds", "model": "local/deepseek"},
}


class Client:
    def __init__(self, *args, **kwargs):
        self.calls = []
        self.messages = []
        self.summary = True
        self.response = {"info": {"role": "assistant", "sessionID": "ses_ds",
                                 "providerID": "local", "modelID": "deepseek", "cost": 0},
                         "parts": [{"type": "text", "text": "real response"}]}
        self.pending = []

    def inspect(self):
        pass

    def get_session(self, sid):
        return {"id": sid}

    def get_session_status(self, sid):
        return "idle"

    def get_messages(self, sid):
        return self.messages

    def _request(self, method, path, body=None, timeout=None):
        self.calls.append((method, path, body))
        if path in {"/permission", "/question"}:
            return self.pending
        if path == "/provider":
            return {"all": []}
        if path.endswith("/summarize"):
            return self.summary
        if path.endswith("/message"):
            return self.response
        if path == "/experimental/tool/ids":
            return ["read", "bash", "edit", "custom"]
        if path.endswith("/abort"):
            return True
        raise AssertionError(path)


def message(count, created=1, cache=0):
    return {"info": {"role": "assistant", "time": {"created": created},
                     "tokens": {"input": count, "cache": {"read": cache, "write": 0}}}}


class VisibleContextTests(unittest.TestCase):
    def session(self, client, **kwargs):
        with patch("tools.dual_ai.web_context.load_bindings", return_value=BINDINGS):
            return VisibleWebSessions(Path("."), lambda *args: None,
                                      client_factory=lambda *args, **kw: client, **kwargs)

    def test_usage_is_latest_input_with_cache_not_cumulative(self):
        self.assertEqual(context_usage([message(1000, 1), message(50, 2, 25)]),
                         (75, "reported_input_plus_cache"))

    def test_missing_usage_is_explicit_estimate(self):
        self.assertTrue(context_usage([{"info": {"role": "user"}}])[1].startswith("estimated"))

    def test_compacts_same_bound_session_and_model_before_prompt(self):
        client = Client()
        client.messages = [message(50000)]
        session = self.session(client)
        session.call("executor", "small prompt")
        paths = [p for _, p, _ in client.calls]
        self.assertLess(paths.index("/session/ses_ds/summarize"), paths.index("/session/ses_ds/message"))
        body = next(b for _, p, b in client.calls if p.endswith("/summarize"))
        self.assertEqual(body, {"providerID": "local", "modelID": "deepseek"})

    def test_failed_summary_sends_no_development_prompt(self):
        client = Client()
        client.messages = [message(50000)]
        client.summary = False
        with self.assertRaises(HumanRequired):
            self.session(client).call("executor", "small")
        self.assertFalse(any(p.endswith("/message") for _, p, _ in client.calls))

    def test_pending_permission_is_not_approved(self):
        client = Client()
        client.pending = [{"id": "permission1", "sessionID": "ses_ds"}]
        with self.assertRaises(HumanRequired):
            self.session(client).call("executor", "small")
        self.assertTrue(all(method == "GET" for method, _, _ in client.calls))

    def test_wrong_bound_session_is_rejected(self):
        client = Client()
        client.response["info"]["sessionID"] = "ses_other"
        with self.assertRaises(HumanRequired):
            self.session(client).call("executor", "small")

    def test_model_error_or_nonzero_cost_halts(self):
        for changes in ({"error": {"name": "QuotaError"}}, {"cost": 0.01}):
            client = Client()
            client.response["info"].update(changes)
            with self.subTest(changes=changes), self.assertRaises(HumanRequired):
                self.session(client).call("executor", "small")

    def test_planner_only_gets_read_tools(self):
        client = Client()
        client.response["info"].update(sessionID="ses_glm", modelID="glm")
        self.session(client).call("planner", "review")
        body = next(b for _, p, b in client.calls if p.endswith("/message"))
        self.assertEqual(body["tools"], {"read": True, "bash": False, "edit": False, "custom": False})


if __name__ == "__main__":
    unittest.main()
