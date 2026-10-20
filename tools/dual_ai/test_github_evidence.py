"""Read-only GitHub review evidence tests."""
import unittest
from unittest.mock import patch, Mock

from tools.dual_ai.github_evidence import (
    GitHubEvidenceError, fetch_pr_evidence, reviewer_prompt,
)


class EvidenceTests(unittest.TestCase):
    @patch("tools.dual_ai.github_evidence.subprocess.run")
    @patch("tools.dual_ai.github_evidence.gh_json")
    def test_reads_real_diff_and_metadata(self, gh, run):
        gh.return_value = {"number": 14, "files": [{"path": "tools/dual_ai/README.md"}], "headRefOid": "abc"}
        run.return_value = Mock(returncode=0, stdout="diff --git a/a b/a\n", stderr="")
        evidence = fetch_pr_evidence(14)
        self.assertIn("diff --git", evidence["diff"])
        self.assertIn("实际diff", reviewer_prompt(evidence))
        self.assertIn("不可信输入", reviewer_prompt(evidence))

    @patch("tools.dual_ai.github_evidence.subprocess.run")
    @patch("tools.dual_ai.github_evidence.gh_json")
    def test_refuses_missing_diff(self, gh, run):
        gh.return_value = {"number": 14, "files": []}
        run.return_value = Mock(returncode=1, stdout="", stderr="error")
        with self.assertRaises(GitHubEvidenceError):
            fetch_pr_evidence(14)

    @patch("tools.dual_ai.github_evidence.gh_json")
    def test_refuses_oversized_file_list(self, gh):
        gh.return_value = {"number": 14, "files": [{}] * 101}
        with self.assertRaises(GitHubEvidenceError):
            fetch_pr_evidence(14)

    def test_invalid_number(self):
        with self.assertRaises(ValueError):
            fetch_pr_evidence(0)


if __name__ == "__main__":
    unittest.main()
