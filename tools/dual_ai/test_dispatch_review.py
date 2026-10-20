"""Review dispatcher tests: draft filtering, dedupe, and no false checkpoint."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.dual_ai.dispatch_review import dispatch_once, pending_prs, load_cursor


class DispatcherTests(unittest.TestCase):
    @patch("tools.dual_ai.dispatch_review.gh_json")
    def test_filters_drafts_and_reviewed_heads(self, gh):
        gh.return_value = [
            {"number": 10, "isDraft": True, "headRefOid": "a"},
            {"number": 11, "isDraft": False, "headRefOid": "b"},
            {"number": 12, "isDraft": False, "headRefOid": "c"},
        ]
        result = pending_prs(cursor={"11": "b"})
        self.assertEqual([p["number"] for p in result], [12])

    @patch("tools.dual_ai.dispatch_review.stream_role", return_value=1)
    @patch("tools.dual_ai.dispatch_review.fetch_pr_evidence")
    @patch("tools.dual_ai.dispatch_review.pending_prs")
    def test_failed_review_does_not_checkpoint(self, pending, fetch, stream):
        pending.return_value = [{"number": 12, "headRefOid": "abc"}]
        fetch.return_value = {"pr": {"headRefOid": "abc"}, "diff": "diff"}
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "cursor.json"
            self.assertEqual(dispatch_once(state=state), 1)
            self.assertFalse(state.exists())

    @patch("tools.dual_ai.dispatch_review.stream_role", return_value=0)
    @patch("tools.dual_ai.dispatch_review.fetch_pr_evidence")
    @patch("tools.dual_ai.dispatch_review.pending_prs")
    def test_success_records_exact_head(self, pending, fetch, stream):
        pending.return_value = [{"number": 12, "headRefOid": "abc"}]
        fetch.return_value = {"pr": {"headRefOid": "abc"}, "diff": "diff"}
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "cursor.json"
            self.assertEqual(dispatch_once(state=state), 0)
            self.assertEqual(load_cursor(state), {"12": "abc"})

    @patch("tools.dual_ai.dispatch_review.stream_role")
    @patch("tools.dual_ai.dispatch_review.fetch_pr_evidence")
    @patch("tools.dual_ai.dispatch_review.pending_prs")
    def test_changed_head_does_not_run(self, pending, fetch, stream):
        pending.return_value = [{"number": 12, "headRefOid": "old"}]
        fetch.return_value = {"pr": {"headRefOid": "new"}, "diff": "diff"}
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(dispatch_once(state=Path(tmp) / "cursor.json"), 1)
            stream.assert_not_called()


if __name__ == "__main__":
    unittest.main()
