"""GitHub-backed duplicate review tests."""
import unittest
from unittest.mock import patch

from tools.dual_ai.review_feedback import existing_review


class DuplicateReviewTests(unittest.TestCase):
    @patch("tools.dual_ai.github_evidence.gh_json")
    def test_same_sha_skipped(self, gh):
        gh.return_value = [{"body": "<!-- dual-ai-glm-review sha=abc -->\nneeds_evidence"}]
        self.assertTrue(existing_review(12, "abc"))

    @patch("tools.dual_ai.github_evidence.gh_json")
    def test_new_sha_requires_review(self, gh):
        gh.return_value = [{"body": "<!-- dual-ai-glm-review sha=abc -->"}]
        self.assertFalse(existing_review(12, "def"))

    @patch("tools.dual_ai.github_evidence.gh_json")
    def test_unrelated_comments_not_counted(self, gh):
        gh.return_value = [{"body": "ordinary comment"}, {"body": None}]
        self.assertFalse(existing_review(12, "abc"))

    @patch("tools.dual_ai.github_evidence.gh_json")
    def test_invalid_response_fails_closed(self, gh):
        gh.return_value = {"error": "unavailable"}
        with self.assertRaises(ValueError):
            existing_review(12, "abc")


if __name__ == "__main__":
    unittest.main()
