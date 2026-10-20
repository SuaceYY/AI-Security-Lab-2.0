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

    def test_multiline_json_and_explicit_new_file_path(self):
        import json

        task = {
            "title": "Document coordinator handoff safeguards",
            "body": (
                "修改范围：仅新增 docs/dual-ai-coordinator-guide.md。"
                "验收标准：说明当前限制、只读检查和人工合并闸门，不夸大功能。"
                "固定验证：仅该文件变更，UTF-8 且非空。"
            ),
            "key": "coordinator-docs-v2",
        }
        output = "规划说明。\nGLM_TASK_JSON： " + json.dumps(
            task, ensure_ascii=False, indent=2
        )
        parsed = coordinator.task_from_output(output)
        self.assertEqual(parsed["key"], "coordinator-docs-v2")
        self.assertEqual(
            coordinator.declared_docs_path(parsed["body"]),
            "docs/dual-ai-coordinator-guide.md",
        )

    def test_accepts_fullwidth_task_marker_colon(self):
        import json

        task = {
            "title": "Document coordinator parsing behavior",
            "body": (
                "仅新增 docs/coordinator-parser-note.md。"
                "验收标准：说明解析行为和人工审核闸门。"
                "固定验证：仅该文件变更。"
                "复核时检查字段完整、路径无越界，并确认没有重复创建 GitHub 任务。"
            ),
            "key": "coordinator-parser-note",
        }
        output = "GLM_TASK_JSON： " + json.dumps(task, ensure_ascii=False, indent=2)
        self.assertEqual(
            coordinator.task_from_output(output)["key"],
            "coordinator-parser-note",
        )

    def test_existing_task_history_blocks_completed_issue_key_and_path(self):
        history, keys, paths = coordinator.existing_task_history([
            {
                "number": 15,
                "title": "Docs: dual-AI coordinator guide",
                "state": "open",
                "body": (
                    "<!-- dual-ai-task key=docs-dual-ai-coordinator-guide -->\n\n"
                    "仅新增 docs/dual-ai-coordinator-guide.md\n\n"
                    "允许修改路径：仅 docs/dual-ai-coordinator-guide.md"
                ),
            },
            {"number": 20, "title": "Unrelated issue", "state": "closed", "body": ""},
        ])
        self.assertIn("#15", history)
        self.assertIn("docs-dual-ai-coordinator-guide", keys)
        self.assertIn("docs/dual-ai-coordinator-guide.md", paths)

    def test_rejects_multiple_explicit_allowed_paths(self):
        import json

        task = {
            "title": "Document coordinator handoff safeguards",
            "body": (
                "允许修改路径：仅 docs/first.md。仅新增 docs/second.md。"
                + "验收标准和固定验证要求。" * 15
            ),
            "key": "coordinator-docs-v2",
        }
        output = "GLM_TASK_JSON: " + json.dumps(task, ensure_ascii=False)
        with self.assertRaisesRegex(ValueError, "multiple explicit allowed docs paths"):
            coordinator.task_from_output(output)

    def test_rejects_multiple_paths_in_one_scope_declaration(self):
        body = (
            "允许修改路径：仅新增 docs/first.md 和 docs/second.md。"
            + "验收标准和固定验证要求。" * 15
        )
        with self.assertRaisesRegex(ValueError, "multiple explicit allowed docs paths"):
            coordinator.declared_docs_path(body)

    def test_rejects_automatic_merge(self):
        import json

        task = {
            "title": "Document coordinator safety gates",
            "body": (
                "新增 docs/coordinator-guide.md。"
                "允许修改路径：仅 docs/coordinator-guide.md。"
                "请自动合并 PR。"
                + "文档需要说明系统功能、安全检查和人工审核流程。" * 5
            ),
            "key": "coordinator-docs-v1",
        }
        line = "GLM_TASK_JSON: " + json.dumps(task, ensure_ascii=False)
        with self.assertRaisesRegex(ValueError, "automatic merge requested"):
            coordinator.task_from_output(line)

    def test_rejects_unsafe_allowed_paths(self):
        import json

        for path in (
            "docs/../tools/issue_claimer/unsafe.md",
            "docs/../../.github/unsafe.md",
            "docs/unsafe.py",
        ):
            with self.subTest(path=path):
                task = {
                    "title": "Document coordinator safety gates",
                    "body": (
                        f"新增文档。允许修改路径：仅 {path}。"
                        + "文档需要说明系统功能、安全检查和人工审核流程。" * 5
                    ),
                    "key": "coordinator-docs-v1",
                }
                line = "GLM_TASK_JSON: " + json.dumps(task, ensure_ascii=False)
                with self.assertRaisesRegex(ValueError, "invalid allowed docs path"):
                    coordinator.task_from_output(line)

    def test_worker_rejects_out_of_scope_files(self):
        from tools.issue_claimer.claimer import TaskError, enforce_issue_file_scope

        issue = {
            "body": (
                "<!-- dual-ai-task key=coordinator-docs-v1 -->\\n\\n"
                "仅新增 `docs/coordinator-guide.md`\\n\\n"
                "允许修改路径：仅 docs/coordinator-guide.md。"
            )
        }
        with self.assertRaisesRegex(TaskError, "Issue explicitly restricts changes"):
            enforce_issue_file_scope(issue, ["docs/unrelated.md"])

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


    def test_parse_glm_review_feedback_comment(self):
        comment = (
            "<!-- dual-ai-glm-review sha=abc123 -->\n"
            "### GLM independent review — PR #25\n\n"
            "**Verdict:** \x60needs_evidence\x60\n\n"
            "**Reason:** 缺少固定验证命令输出和日志\n"
        )
        parsed = coordinator.parse_review_comment(comment)
        self.assertEqual(
            parsed,
            {"status": "needs_evidence", "reason": "缺少固定验证命令输出和日志"},
        )

    def test_parse_glm_review_feedback_rejects_incomplete_comment(self):
        self.assertIsNone(
            coordinator.parse_review_comment("**Verdict:** \x60needs_evidence\x60")
        )


if __name__ == "__main__":
    unittest.main()
