"""Coordinator bounded-state and task validation tests (no external AI calls)."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.dual_ai import coordinator


class CoordinatorTests(unittest.TestCase):
        def test_valid_task(self):
        import json

        task = {
            "title": "Document coordinator safety gates",
            "body": (
                "新增 docs/coordinator-guide.md，说明协调器的安全机制。"
                "允许修改路径：仅 docs/coordinator-guide.md。"
                "禁止修改 tools/issue_claimer 和 .github/ 目录。"
                "禁止自动合并，PR 等待人工审核。"
            ),
            "key": "coordinator-docs-v1",
        }

        line = "GLM_TASK_JSON: " + json.dumps(task, ensure_ascii=False)
        self.assertEqual(
            coordinator.task_from_output(line)["key"],
            "coordinator-docs-v1",
        )
    def test_rejects_duplicate_task(self):
        line = 'GLM_TASK_JSON: {"title":"Document coordinator safety gates","body":"' + ('docs/ 文档验收要求。' * 20) + '","key":"coordinator-docs-v1"}'
        with self.assertRaises(ValueError):
            coordinator.task_from_output(line + "\n" + line)

    def test_rejects_extra_fields(self):
        line = 'GLM_TASK_JSON: {"title":"Document coordinator safety gates","body":"' + ('docs/ 文档验收要求。' * 20) + '","key":"coordinator-docs-v1","command":"rm -rf ."}'
        with self.assertRaises(ValueError):
            coordinator.task_from_output(line)

    @patch.object(coordinator, "gh_json", return_value=[
        {"number": 25, "body": "Automated implementation for issue #42.", "mergedAt": "2026-10-20T00:00:00Z"}
    ])
    @patch.object(coordinator, "review_new_pr")
    @patch.object(coordinator, "create_next_issue")
    @patch.object(coordinator, "load_state", return_value={"created": [42]})
    def test_human_merge_unlocks_next_issue(self, load, create, review, github):
        coordinator.tick(write=False)
        create.assert_called_once()
        review.assert_not_called()

    def test_worker_pr_link(self):
        self.assertTrue(coordinator.linked_to_issue(
            "Automated implementation for issue #42.\\n\\n- Branch: ai/issue-42", 42))
        self.assertTrue(coordinator.linked_to_issue("Closes #42", 42))
        self.assertFalse(coordinator.linked_to_issue(
            "Automated implementation for issue #420.", 42))
        self.assertFalse(coordinator.linked_to_issue("Unrelated PR", 42))

    def test_state_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            coordinator.save_state({"created": [42], "phase": "waiting_for_worker"}, path)
            self.assertEqual(coordinator.load_state(path)["created"], [42])

    @patch.object(coordinator, "gh_json", return_value=[])
    @patch.object(coordinator, "review_new_pr")
    @patch.object(coordinator, "create_next_issue")
    @patch.object(coordinator, "load_state")
    def test_does_not_create_again(self, load, create, review, github):
        load.return_value = {"created": [42]}
        coordinator.tick(write=False)
        create.assert_not_called()
        review.assert_called_once()

    @patch.object(coordinator, "review_new_pr")
    @patch.object(coordinator, "create_next_issue")
    @patch.object(coordinator, "load_state")
    def test_creates_only_when_empty(self, load, create, review):
        load.return_value = {"created": []}
        coordinator.tick(write=False)
        create.assert_called_once()
        review.assert_not_called()


if __name__ == "__main__":
    unittest.main()
