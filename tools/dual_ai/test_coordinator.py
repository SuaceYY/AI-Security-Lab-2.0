"""Coordinator bounded-state and task validation tests (no external AI calls)."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.dual_ai import coordinator


class CoordinatorTests(unittest.TestCase):
    def test_valid_task(self):
        line = 'GLM_TASK_JSON: {"title":"Document coordinator safety gates","body":"' + ('文档验收要求。' * 20) + '","key":"coordinator-docs-v1"}'
        self.assertEqual(coordinator.task_from_output(line)["key"], "coordinator-docs-v1")

    def test_rejects_duplicate_task(self):
        line = 'GLM_TASK_JSON: {"title":"Document coordinator safety gates","body":"' + ('文档验收要求。' * 20) + '","key":"coordinator-docs-v1"}'
        with self.assertRaises(ValueError):
            coordinator.task_from_output(line + "\n" + line)

    def test_rejects_extra_fields(self):
        line = 'GLM_TASK_JSON: {"title":"Document coordinator safety gates","body":"' + ('文档验收要求。' * 20) + '","key":"coordinator-docs-v1","command":"rm -rf ."}'
        with self.assertRaises(ValueError):
            coordinator.task_from_output(line)

    def test_state_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            coordinator.save_state({"created": [42], "phase": "waiting_for_worker"}, path)
            self.assertEqual(coordinator.load_state(path)["created"], [42])

    @patch.object(coordinator, "review_new_pr")
    @patch.object(coordinator, "create_next_issue")
    @patch.object(coordinator, "load_state")
    def test_does_not_create_again(self, load, create, review):
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
