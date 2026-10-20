"""Transport contract tests. Fake HTTP is NOT evidence of a live OpenCode TUI."""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import Mock

from tools.dual_ai.visible_probe import Client, endpoint, ps_command


class ProbeTests(unittest.TestCase):
    def test_remote_endpoints_rejected(self):
        for url in ("http://example.com:4096", "http://127.0.0.1:4096/redirect",
                    "https://127.0.0.1:4096", "http://user:secret@127.0.0.1:4096",
                    "http://127.0.0.1:4096?remote=1"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                Client(url, Path.cwd())

    def test_port_range(self):
        for port in (0, 80, 65536):
            with self.assertRaises(ValueError):
                endpoint(port)

    def test_powershell_arguments_are_literal(self):
        command = ps_command(["C:\\O'Brien\\opencode.cmd", "a$(Get-Secret)`x"])
        source = base64.b64decode(command[-1]).decode("utf-16-le")
        self.assertEqual(source, "& 'C:\\O''Brien\\opencode.cmd' 'a$(Get-Secret)`x'; exit $LASTEXITCODE")

    def test_directory_mismatch_stops(self):
        client = Client(endpoint(41961), Path.cwd())
        client.request = Mock(side_effect=[{"healthy": True, "version": "test"},
                                           {"directory": str(Path.cwd() / "wrong")}])
        with self.assertRaises(RuntimeError):
            client.inspect()

    def test_post_failure_is_never_retried(self):
        client = Client(endpoint(41961), Path.cwd())
        client.request = Mock(side_effect=TimeoutError("ambiguous delivery"))
        with self.assertRaises(TimeoutError):
            client.mark("ses_planner", "planner", "opencode/glm", "nonce")
        self.assertEqual(client.request.call_count, 1)

    def test_wrong_response_session_stops(self):
        client = Client(endpoint(41961), Path.cwd())
        client.request = Mock(return_value={"info": {"role": "user", "sessionID": "ses_other"}})
        with self.assertRaises(RuntimeError):
            client.mark("ses_planner", "planner", "opencode/glm", "nonce")
        self.assertEqual(client.request.call_count, 1)

    def test_assistant_activity_stops(self):
        client = Client(endpoint(41961), Path.cwd())
        marker = "[连接验证/planner/n] 仅验证真实会话传输；未调用模型，未执行开发任务。"
        msg = {"info": {"role": "user", "sessionID": "ses_planner", "id": "m1"},
               "parts": [{"type": "text", "text": marker}]}
        client.request = Mock(side_effect=[msg, [msg, {"info": {"role": "assistant"}}]])
        with self.assertRaises(RuntimeError):
            client.mark("ses_planner", "planner", "opencode/glm", "n")

    def test_invalid_session_not_sent(self):
        client = Client(endpoint(41961), Path.cwd())
        client.request = Mock()
        with self.assertRaises(ValueError):
            client.mark("ses_x/../../shell", "planner", "opencode/glm", "n")
        client.request.assert_not_called()

    def test_real_http_context_only_roundtrip(self):
        calls, messages = [], {}
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                path = self.path.split("?")[0]
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                calls.append(("POST", path, body))
                sid = path.split("/")[2]
                msg = {"info": {"id": "msg_" + sid, "sessionID": sid, "role": "user"},
                       "parts": body["parts"]}
                messages[sid] = [msg]
                self.reply(msg)

            def do_GET(self):
                path = self.path.split("?")[0]
                calls.append(("GET", path, None))
                self.reply(messages[path.split("/")[2]])

            def reply(self, obj):
                data = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                client = Client(endpoint(server.server_port), Path.cwd())
                for role in ("planner", "executor"):
                    result = client.mark("ses_" + role, role, "opencode/" + role, "probe")
                    self.assertTrue(result["readback_ok"])
            finally:
                server.shutdown()
                thread.join(timeout=2)
        self.assertEqual(len(calls), 4)
        for method, path, body in calls:
            self.assertRegex(path, r"^/session/ses_(planner|executor)/message$")
            if method == "POST":
                self.assertIs(body["noReply"], True)
        self.assertNotEqual(messages["ses_planner"], messages["ses_executor"])


if __name__ == "__main__":
    unittest.main()
