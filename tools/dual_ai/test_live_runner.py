"""Unit tests for actual-event parsing without invoking AI providers."""
import io
import unittest

from tools.dual_ai.live_runner import extract_session_id, redact, summarize_event


class EventParsingTests(unittest.TestCase):
    def test_session_id_top_level(self):
        self.assertEqual(extract_session_id({"type": "text", "sessionID": "ses_abc123"}), "ses_abc123")

    def test_session_id_part(self):
        self.assertEqual(extract_session_id({"part": {"sessionID": "ses_xyz456"}}), "ses_xyz456")

    def test_no_session(self):
        self.assertIsNone(extract_session_id({"type": "step_start"}))

    def test_text_event(self):
        self.assertEqual(summarize_event({"type": "text", "part": {"text": "hello"}}), "hello")

    def test_tool_event(self):
        message = summarize_event({"type": "tool_use", "part": {"tool": "bash", "state": {"status": "completed", "title": "pytest"}}})
        self.assertIn("bash", message)
        self.assertIn("pytest", message)

    def test_redact_known_token(self):
        secret = "ghp_" + "a" * 30
        self.assertNotIn(secret, redact("token=" + secret))
        self.assertIn("[REDACTED]", redact("token=" + secret))


if __name__ == "__main__":
    unittest.main()
