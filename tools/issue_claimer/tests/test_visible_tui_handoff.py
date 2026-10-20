"""Visible TUI worker routing and historical issue exclusions."""
import unittest
from unittest.mock import patch

from tools.dual_ai.tui_transport import TUITransportError
from tools.issue_claimer import claimer


class VisibleTUIHandoffTests(unittest.TestCase):
    def test_historical_issues_are_permanently_excluded(self):
        for number in (2, 6, 9, 10, 15):
            with self.subTest(issue=number):
                eligible, reason = claimer.evaluate_issue(
                    {"tasks": {}},
                    {"number": number, "state": "OPEN", "labels": ["ai-ready"]},
                )
                self.assertFalse(eligible)
                self.assertIn("permanently excluded", reason)

    def test_first_new_issue_is_not_blocked_by_excluded_issue_two(self):
        issue = {
            "number": 18,
            "state": "OPEN",
            "labels": ["ai-ready"],
            "title": "Visible task",
            "body": "bounded task",
        }
        with (
            patch.object(claimer, "list_ready_issues", return_value=[issue]),
            patch.object(claimer, "fetch_issue", return_value=issue),
            patch.object(claimer, "remote_claim_exists", return_value=(False, "none")),
        ):
            selected, _ = claimer.find_next_issue({"first_run_completed": False, "tasks": {}})
        self.assertEqual(selected["number"], 18)

    def test_worker_uses_exact_deepseek_visible_session(self):
        issue = {"number": 18, "title": "Visible task", "body": "bounded task"}
        with (
            patch.object(claimer, "load_binding", return_value={"session_id": "ses_executor"}),
            patch.object(claimer, "run_prompt", return_value=("done", "ses_executor")) as run,
        ):
            result = claimer.run_opencode(issue, 1, "", 120)
        self.assertEqual(result.stdout, "done")
        self.assertEqual(run.call_args.args[0], "executor")
        self.assertIn("Issue #18", run.call_args.args[1])

    def test_ambiguous_visible_transport_failure_stops_without_retry(self):
        issue = {"number": 18, "title": "Visible task", "body": "bounded task"}
        with (
            patch.object(claimer, "load_binding", return_value={"session_id": "ses_executor"}),
            patch.object(claimer, "run_prompt", side_effect=TUITransportError("ambiguous POST")) as run,
        ):
            with self.assertRaises(claimer.StopWorker):
                claimer.run_opencode(issue, 1, "", 120)
        run.assert_called_once()


if __name__ == "__main__":
    unittest.main()

