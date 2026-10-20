"""Verify safe OpenCode CLI construction and exact per-role resume."""
import tempfile
import unittest
from pathlib import Path

from tools.dual_ai.opencode_commands import build_command, load_config
from tools.dual_ai.session_registry import register_session


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.registry = self.root / "sessions.json"
        self.config = load_config()

    def tearDown(self):
        self.tmp.cleanup()

    def test_new_sessions_have_distinct_models(self):
        glm = build_command("planner", "plan", self.root, self.registry, self.config)
        deepseek = build_command("executor", "execute", self.root, self.registry, self.config)
        self.assertIn("opencode/glm-5.3-flash-free", glm)
        self.assertIn("opencode/deepseek-v4.1-flash-free", deepseek)
        self.assertNotIn("--session", glm)
        self.assertNotIn("--session", deepseek)
        for command in (glm, deepseek):
            self.assertIn("--format", command)
            self.assertIn("json", command)
            self.assertNotIn("--auto", command)
            self.assertNotIn("--continue", command)

    def test_exact_session_resume(self):
        register_session(self.registry, "planner", "ses_glm_1234", self.config["roles"]["planner"]["model"])
        register_session(self.registry, "executor", "ses_deepseek_1234", self.config["roles"]["executor"]["model"])
        glm = build_command("planner", "review", self.root, self.registry, self.config)
        deepseek = build_command("executor", "fix", self.root, self.registry, self.config)
        self.assertEqual(glm[glm.index("--session") + 1], "ses_glm_1234")
        self.assertEqual(deepseek[deepseek.index("--session") + 1], "ses_deepseek_1234")

    def test_invalid_role_or_prompt(self):
        with self.assertRaises(ValueError):
            build_command("unknown", "hello", self.root, self.registry, self.config)
        with self.assertRaises(ValueError):
            build_command("planner", " ", self.root, self.registry, self.config)


if __name__ == "__main__":
    unittest.main()
