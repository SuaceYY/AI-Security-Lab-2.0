"""Session registry isolation regression tests."""
import json
import tempfile
import unittest
from pathlib import Path

from tools.dual_ai.session_registry import get_session, register_session


class SessionRegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "sessions.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_independent_sessions(self):
        register_session(self.path, "planner", "ses_glm_1234", "opencode/glm-5.3-flash-free")
        register_session(self.path, "executor", "ses_deepseek_1234", "opencode/deepseek-v4.1-flash-free")
        self.assertEqual(get_session(self.path, "planner", "opencode/glm-5.3-flash-free"), "ses_glm_1234")
        self.assertEqual(get_session(self.path, "executor", "opencode/deepseek-v4.1-flash-free"), "ses_deepseek_1234")

    def test_reject_shared_session(self):
        register_session(self.path, "planner", "ses_shared_1234", "glm")
        with self.assertRaises(ValueError):
            register_session(self.path, "executor", "ses_shared_1234", "deepseek")

    def test_reject_wrong_model_on_resume(self):
        register_session(self.path, "planner", "ses_glm_1234", "glm")
        with self.assertRaises(ValueError):
            get_session(self.path, "planner", "deepseek")

    def test_reject_model_change(self):
        register_session(self.path, "planner", "ses_glm_1234", "glm")
        with self.assertRaises(ValueError):
            register_session(self.path, "planner", "ses_other_1234", "other")
        self.assertEqual(get_session(self.path, "planner", "glm"), "ses_glm_1234")

    def test_no_state_file_until_registered(self):
        self.assertIsNone(get_session(self.path, "planner", "glm"))
        self.assertFalse(self.path.exists())

    def test_reject_unknown_role(self):
        with self.assertRaises(ValueError):
            register_session(self.path, "worker", "ses_valid_1234", "glm")


if __name__ == "__main__":
    unittest.main()
