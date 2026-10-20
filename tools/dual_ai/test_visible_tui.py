"""Tests for exact visible-session binding and one-shot prompt completion."""
import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tools.dual_ai import visible_tui
from tools.dual_ai.tui_transport import TUITransportError


class VisibleTUITests(unittest.TestCase):
    def binding_text(self, executor_session="ses_executor") -> str:
        return json.dumps({
            "schema_version": 1,
            "roles": {
                "planner": {"url": "http://127.0.0.1:4311", "session_id": "ses_planner", "model": "provider/glm"},
                "executor": {"url": "http://127.0.0.1:4312", "session_id": executor_session, "model": "provider/deepseek"},
            },
        })

    def test_requires_valid_distinct_visible_bindings(self):
        path = Path("binding.json")
        with patch.object(Path, "read_text", return_value=self.binding_text()):
            self.assertEqual(visible_tui.load_binding("planner", path)["session_id"], "ses_planner")
        with patch.object(Path, "read_text", return_value=self.binding_text("ses_planner")):
            with self.assertRaisesRegex(TUITransportError, "separate OpenCode sessions"):
                visible_tui.load_bindings(path)

    def test_prompt_is_submitted_once_and_read_from_bound_session(self):
        root = Path.cwd()
        binding = Path("binding.json")
        with patch.object(Path, "read_text", return_value=self.binding_text()):
            assistant = {"info": {"id": "a1", "role": "assistant", "sessionID": "ses_planner", "providerID": "provider", "modelID": "glm"}, "parts": [{"type": "text", "text": "done"}]}
            client = Mock()
            client.inspect.return_value = {"directory": str(root)}
            client.get_session.return_value = {"id": "ses_planner"}
            client.get_session_status.return_value = "idle"
            client.send_session_message.return_value = assistant
            factory = Mock(return_value=client)
            text, session = visible_tui.run_prompt("planner", "task", root=root,
                                                   binding_file=binding, output=Mock(),
                                                   client_factory=factory)
            self.assertEqual((text, session), ("done", "ses_planner"))
            client.send_session_message.assert_called_once_with(
                "ses_planner", "task", "provider/glm", timeout=305
            )

    def test_non_idle_session_is_not_sent(self):
        root = Path.cwd()
        binding = Path("binding.json")
        with patch.object(Path, "read_text", return_value=self.binding_text()):
            client = Mock()
            client.inspect.return_value = {"directory": str(root)}
            client.get_session.return_value = {"id": "ses_planner"}
            client.get_session_status.return_value = "busy"
            with self.assertRaises(TUITransportError):
                visible_tui.run_prompt("planner", "task", root=root, binding_file=binding,
                                       client_factory=Mock(return_value=client))
            client.send_session_message.assert_not_called()


if __name__ == "__main__":
    unittest.main()
