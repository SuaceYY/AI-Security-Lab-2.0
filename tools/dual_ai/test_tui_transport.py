"""Protocol tests for loopback OpenCode TUI transport; no model is invoked."""
import io
import json
from pathlib import Path
import unittest
from unittest.mock import Mock
from urllib.parse import urlsplit

from tools.dual_ai.tui_transport import TUIClient, TUIRequestTimeout, TUITransportError


class TUITransportTests(unittest.TestCase):
    def test_rejects_non_loopback_or_ambiguous_urls(self):
        for url in (
            "http://localhost:4311",
            "http://example.com:4311",
            "https://127.0.0.1:4311",
            "http://127.0.0.1:4311/path",
            "http://user:secret@127.0.0.1:4311",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                TUIClient(url, Path.cwd())

    def test_sends_prompt_to_tui_in_order(self):
        calls = []
        project = str(Path.cwd().resolve())
        responses = [
            {"healthy": True, "version": "test"},
            {"directory": project},
            True,
            True,
        ]

        class FakeOpener:
            def open(self, request, timeout):
                raw = request.data
                calls.append((request.get_method(), urlsplit(request.full_url),
                              json.loads(raw) if raw else None, timeout))
                return io.BytesIO(json.dumps(responses[len(calls) - 1]).encode("utf-8"))

        client = TUIClient("http://127.0.0.1:4311", Path(project))
        client.opener = FakeOpener()
        client.inspect()
        client.send_prompt("visible task")
        self.assertEqual([call[0] for call in calls], ["GET", "GET", "POST", "POST"])
        self.assertEqual(calls[2][1].path, "/tui/append-prompt")
        self.assertEqual(calls[2][2], {"text": "visible task"})
        self.assertEqual(calls[3][1].path, "/tui/submit-prompt")
        self.assertIsNone(calls[3][2])
        self.assertTrue(all("directory=" in call[1].query for call in calls))

    def test_rejected_append_does_not_submit(self):
        client = TUIClient("http://127.0.0.1:4311", Path.cwd())
        client._request = Mock(return_value=False)
        with self.assertRaises(TUITransportError):
            client.send_prompt("task")
        self.assertEqual(client._request.call_count, 1)

    def test_ambiguous_post_failure_is_not_retried(self):
        client = TUIClient("http://127.0.0.1:4311", Path.cwd())
        opener = Mock()
        opener.open.side_effect = TimeoutError("ambiguous delivery")
        client.opener = opener
        with self.assertRaises(TUIRequestTimeout):
            client.append_prompt("task")
        self.assertEqual(opener.open.call_count, 1)

    def test_url_error_wrapping_timeout_keeps_timeout_type(self):
        from urllib.error import URLError

        client = TUIClient("http://127.0.0.1:4311", Path.cwd())
        opener = Mock()
        opener.open.side_effect = URLError(TimeoutError("server response timed out"))
        client.opener = opener
        with self.assertRaises(TUIRequestTimeout):
            client.send_session_message("ses_executor", "task", "provider/model")
        opener.open.assert_called_once()

    def test_empty_prompt_is_rejected_before_http(self):
        client = TUIClient("http://127.0.0.1:4311", Path.cwd())
        client._request = Mock()
        with self.assertRaises(ValueError):
            client.send_prompt("  ")
        client._request.assert_not_called()

    def test_session_message_pins_session_and_model_once(self):
        client = TUIClient("http://127.0.0.1:4312", Path.cwd())
        client._request = Mock(return_value={"info": {"sessionID": "ses_executor"}})
        result = client.send_session_message(
            "ses_executor", "authorized task", "opencode/deepseek-v4.1-flash-free", timeout=905
        )
        self.assertEqual(result["info"]["sessionID"], "ses_executor")
        args, kwargs = client._request.call_args
        self.assertEqual(args[:2], ("POST", "/session/ses_executor/message"))
        self.assertEqual(args[2]["model"], {
            "providerID": "opencode", "modelID": "deepseek-v4.1-flash-free"
        })
        self.assertEqual(args[2]["parts"], [{"type": "text", "text": "authorized task"}])
        self.assertEqual(kwargs["timeout"], 905)
