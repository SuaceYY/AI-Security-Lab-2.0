"""Regression tests for Issue Claimer input and session safeguards."""
import importlib.util
from pathlib import Path
from unittest import TestCase, mock

MODULE_PATH = Path(__file__).with_name("claimer.py")
spec = importlib.util.spec_from_file_location("issue_claimer_under_test", MODULE_PATH)
claimer = importlib.util.module_from_spec(spec)
import sys
sys.modules[spec.name] = claimer
spec.loader.exec_module(claimer)


class ClaimerRegressionTests(TestCase):
    def test_rejects_empty_issue_body(self):
        with self.assertRaises(claimer.TaskError):
            claimer.build_prompt({"number": 9, "title": "test", "body": ""}, 1, "")

    def test_prompt_includes_issue_body_and_chinese(self):
        result = claimer.build_prompt({"number": 9, "title": "test", "body": "仅新增 docs/test.md"}, 1, "")
        self.assertIn("仅新增 docs/test.md", result)
        self.assertIn("Simplified Chinese", result)

    def test_exact_session_matching(self):
        import json
        directory = str(claimer.PROJECT_ROOT)
        output = json.dumps([
            {"id": "ses_old111", "title": "AI issue #9 attempt 1", "directory": directory, "created": 1000},
            {"id": "ses_new222", "title": "AI issue #9 attempt 1", "directory": directory, "created": 2000},
            {"id": "ses_other333", "title": "AI issue #6 attempt 1", "directory": directory, "created": 2000},
        ])
        with mock.patch.object(claimer, "run_command", return_value=claimer.CommandResult(0, output, "")):
            self.assertEqual(claimer.find_opencode_session(9, 1, 1500), "ses_new222")

    def test_ambiguous_session_fails_closed(self):
        import json
        directory = str(claimer.PROJECT_ROOT)
        output = json.dumps([
            {"id": "ses_aaa111", "title": "AI issue #9 attempt 1", "directory": directory, "created": 2000},
            {"id": "ses_bbb222", "title": "AI issue #9 attempt 1", "directory": directory, "created": 2001},
        ])
        with mock.patch.object(claimer, "run_command", return_value=claimer.CommandResult(0, output, "")):
            self.assertIsNone(claimer.find_opencode_session(9, 1, 1500))

    def test_wrong_directory_is_rejected(self):
        import json
        output = json.dumps([{"id": "ses_aaa111", "title": "AI issue #9 attempt 1", "directory": "C:/wrong-project", "created": 2000}])
        with mock.patch.object(claimer, "run_command", return_value=claimer.CommandResult(0, output, "")):
            self.assertIsNone(claimer.find_opencode_session(9, 1, 1500))

    def test_invalid_session_rejected_before_execution(self):
        with self.assertRaises(claimer.TaskError):
            claimer.run_opencode({"number": 9, "body": "test"}, 1, "", 10, session_id="--continue")
