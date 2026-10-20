"""Tests for the read-only dual-AI model preflight."""
import unittest
from unittest.mock import patch
from tools.dual_ai import preflight


class ModelPreflightTests(unittest.TestCase):
    @patch.object(preflight.shutil, "which", return_value="opencode")
    @patch.object(preflight, "run")
    def test_distinct_models_available(self, run, which):
        run.return_value = (0, "provider/glm-model\nprovider/deepseek-model\n", "")
        result = preflight.check_models("provider/glm-model", "provider/deepseek-model")
        self.assertTrue(result["ok"])

    @patch.object(preflight.shutil, "which", return_value="opencode")
    @patch.object(preflight, "run")
    def test_missing_model_fails_closed(self, run, which):
        run.return_value = (0, "provider/glm-model\n", "")
        result = preflight.check_models("provider/glm-model", "provider/deepseek-model")
        self.assertFalse(result["ok"])
        self.assertFalse(result["deepseek"]["available"])

    @patch.object(preflight.shutil, "which", return_value="opencode")
    @patch.object(preflight, "run")
    def test_same_model_fails(self, run, which):
        run.return_value = (0, "provider/glm-model\n", "")
        self.assertFalse(preflight.check_models("provider/glm-model", "provider/glm-model")["ok"])

    @patch.object(preflight.shutil, "which", return_value=None)
    def test_missing_opencode_fails(self, which):
        self.assertFalse(preflight.check_models("a", "b")["ok"])

    @patch.object(preflight.shutil, "which", return_value="opencode")
    @patch.object(preflight, "run")
    def test_command_failure_fails(self, run, which):
        run.return_value = (1, "", "failed")
        self.assertFalse(preflight.check_models("a", "b")["ok"])


if __name__ == "__main__":
    unittest.main()
