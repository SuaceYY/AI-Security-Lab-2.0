"""Tests for structured GLM review and fail-closed handling."""
import unittest
from tools.dual_ai.review_feedback import parse_verdict, format_comment


class ReviewVerdictTests(unittest.TestCase):
    def test_valid_needs_evidence(self):
        text = '[20:00][GLM][text] GLM_REVIEW_VERDICT: {"status":"needs_evidence","reason":"缺少固定验证的原始日志与证据，需要人工确认。"}'
        verdict = parse_verdict(text)
        self.assertEqual(verdict["status"], "needs_evidence")
        self.assertIn("human approval", format_comment(12, "abc", verdict).lower())

    def test_missing_verdict_fails(self):
        with self.assertRaises(ValueError):
            parse_verdict("[GLM] SUCCESS session=ses_abc")

    def test_duplicate_verdict_fails(self):
        line = 'GLM_REVIEW_VERDICT: {"status":"ready_for_human","reason":"已经完成全部检查，可以进行人工审核。"}'
        with self.assertRaises(ValueError):
            parse_verdict(line + "\n" + line)

    def test_unknown_status_fails(self):
        with self.assertRaises(ValueError):
            parse_verdict('GLM_REVIEW_VERDICT: {"status":"approved","reason":"已经完成全部检查，可以进行人工审核。"}')

    def test_short_reason_fails(self):
        with self.assertRaises(ValueError):
            parse_verdict('GLM_REVIEW_VERDICT: {"status":"changes_requested","reason":"bad"}')


if __name__ == "__main__":
    unittest.main()
