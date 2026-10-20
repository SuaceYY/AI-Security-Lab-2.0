"""Repair handoff fail-closed tests."""
import unittest
from tools.dual_ai.repair_handoff import prepare_repair


class RepairTests(unittest.TestCase):
    def test_changes_requested(self):
        state = {"created": [25], "reviewed": [
            {"pr": 31, "sha": "a" * 40, "status": "changes_requested"}]}
        self.assertEqual(prepare_repair(state, 31)["round"], 1)

    def test_needs_evidence_not_repair(self):
        state = {"created": [25], "reviewed": [
            {"pr": 31, "sha": "a" * 40, "status": "needs_evidence"}]}
        with self.assertRaises(ValueError):
            prepare_repair(state, 31)

    def test_failed_history_blocked(self):
        state = {"created": [9], "reviewed": [
            {"pr": 31, "sha": "a" * 40, "status": "changes_requested"}]}
        with self.assertRaises(ValueError):
            prepare_repair(state, 31)

    def test_round_limit(self):
        state = {"created": [25], "repair_rounds": {"31": 3}, "reviewed": [
            {"pr": 31, "sha": "a" * 40, "status": "changes_requested"}]}
        with self.assertRaises(ValueError):
            prepare_repair(state, 31)


if __name__ == "__main__":
    unittest.main()
