"""GitHub handoff and staging boundaries without subprocesses or model calls."""
import json
import base64
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

from tools.dual_ai.web_batch import HumanRequired
from tools.dual_ai.web_batch_runtime import WebBatch, FENCE
from tools.issue_claimer import claimer as worker

PLAN = json.loads((Path(__file__).parent / "web_batch_plan.json").read_text(encoding="utf-8"))
RUNTIME = "tools.dual_ai.web_batch_runtime."


class RuntimeTests(unittest.TestCase):
    def batch(self, root):
        batch = WebBatch("web-test-001", PLAN)
        batch.path = root / "state.json"
        batch.head = "abc"
        batch.base_sha = "base"
        batch.issue = 1
        return batch

    def test_large_github_body_uses_json_file_and_removes_it(self):
        with tempfile.TemporaryDirectory() as folder:
            batch = self.batch(Path(folder))
            seen = []
            def github(args, **kwargs):
                filename = args[args.index("--input") + 1]
                seen.append(filename)
                payload = json.loads(Path(filename).read_text(encoding="utf-8"))
                self.assertEqual(payload, {"body": "中" * 40000, "draft": True})
                return {"id": 2}
            with patch(RUNTIME + "gh_json", side_effect=github):
                batch.api("issues/1/comments", "-X", "POST", "-f", "body=" + "中" * 40000, "-F", "draft=true")
            self.assertFalse(Path(seen[0]).exists())

    def test_review_is_read_back_from_github_not_local_object(self):
        with tempfile.TemporaryDirectory() as folder:
            batch = self.batch(Path(folder))
            report = {"model_result": {"status": "changes_requested", "instructions": ["repair"]}}
            batch.api = Mock(return_value={"body": FENCE + "json\n" + json.dumps(report) + "\n" + FENCE})
            self.assertEqual(batch.fetch(99), report)
            batch.api.assert_called_once_with("issues/comments/99")

    def test_duplicate_batch_refuses_without_erasing_history(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            batch = self.batch(root)
            batch.path.write_text("old checkpoint", encoding="utf-8")
            with self.assertRaises(HumanRequired):
                batch.__enter__()
            batch.pause("do not overwrite")
            self.assertEqual(batch.path.read_text(encoding="utf-8"), "old checkpoint")

    def test_failed_verification_unstages_own_changes_and_never_commits(self):
        with tempfile.TemporaryDirectory() as folder:
            batch = self.batch(Path(folder))
            batch.guard = Mock(return_value=["backend/scanner.py"])
            batch.fingerprint = Mock(return_value="unchanged")
            batch.verify = Mock(return_value={"ok": False, "steps": [], "reason": "failing test"})
            batch.snapshot = Mock(return_value={"files": {}})
            commands = []
            def command(args):
                commands.append(args)
                return ""
            with patch(RUNTIME + "command", side_effect=command), \
                 patch(RUNTIME + "stage_and_validate_changes") as stage, \
                 patch(RUNTIME + "commit_changes") as commit, \
                 patch(RUNTIME + "push_branch") as push:
                result = batch.capture(1, "development", "report", {"status": "done"})
            self.assertFalse(result["verification"]["ok"])
            stage.assert_called_once_with(["backend/scanner.py"])
            self.assertIn(["restore", "--staged", "--", "backend/scanner.py"], commands)
            commit.assert_not_called()
            push.assert_not_called()
            batch.snapshot.assert_called_once_with(False)

    def test_successful_capture_creates_draft_pr_through_json_api(self):
        with tempfile.TemporaryDirectory() as folder:
            batch = self.batch(Path(folder))
            batch.guard = Mock(return_value=["backend/scanner.py"])
            batch.fingerprint = Mock(return_value="unchanged")
            batch.verify = Mock(return_value={"ok": True, "steps": []})
            batch.snapshot = Mock(return_value={"files": {}})
            batch.api = Mock(return_value={"number": 7})
            with patch(RUNTIME + "command", return_value=""), \
                 patch(RUNTIME + "stage_and_validate_changes"), \
                 patch(RUNTIME + "commit_changes", return_value="newsha"), \
                 patch(RUNTIME + "push_branch"):
                result = batch.capture(1, "development", "report", {"status": "done"})
            self.assertEqual(result["pr"], 7)
            args = batch.api.call_args.args
            self.assertEqual(args[0], "pulls")
            self.assertIn("draft=true", args)
            self.assertIn("base=main", args)

    def test_changed_worktree_during_tests_never_commits(self):
        with tempfile.TemporaryDirectory() as folder:
            batch = self.batch(Path(folder))
            batch.guard = Mock(return_value=[])
            batch.fingerprint = Mock(side_effect=["before", "after"])
            batch.verify = Mock(return_value={"ok": True})
            with patch(RUNTIME + "commit_changes") as commit, self.assertRaises(HumanRequired):
                batch.capture(1, "development", "report", {})
            commit.assert_not_called()

    def test_real_git_commit_push_and_next_guard_preserve_staging_and_tracking(self):
        # Real local repositories only; GitHub, validation and models are mocked.
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "project"
            remote = Path(folder) / "remote.git"
            root.mkdir()
            def git(*args):
                completed = subprocess.run(["git", *args], cwd=root, capture_output=True,
                                           text=True, encoding="utf-8", check=True)
                return completed.stdout.strip()
            git("init", "-b", "main")
            git("config", "user.name", "Offline Test")
            git("config", "user.email", "test@example.invalid")
            for path in ("frontend/src/App.tsx", "frontend/src/fileFilter.ts", "backend/main.py"):
                file = root / path
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text("# baseline\n", encoding="utf-8")
            (root / ".gitignore").write_text("/data/\n", encoding="utf-8")
            git("add", ".")
            git("commit", "-m", "base")
            git("init", "--bare", str(remote))
            git("remote", "add", "origin", str(remote))
            git("push", "origin", "main")
            base = git("rev-parse", "HEAD")
            git("switch", "-c", "ai/web-web-test-001", "origin/main")
            batch = self.batch(root / "data")
            batch.base_sha = base
            batch.head = base
            def api(path, *args):
                if path == "pulls":
                    return {"number": 7}
                relative = path.split("contents/", 1)[1].split("?ref=", 1)[0]
                return {"sha": "test-blob", "content": base64.b64encode((root / relative).read_bytes()).decode()}
            batch.api = api
            batch.verify = Mock(return_value={"ok": True, "steps": []})
            with patch(RUNTIME + "ROOT", root), \
                 patch.object(worker, "PROJECT_ROOT", root), \
                 patch.object(worker, "DATA_DIR", root / "data"):
                batch.origin = worker.read_origin_url()
                batch.config = worker.read_local_git_config()
                (root / "backend/scanner.py").write_text("def scan(): return []\n", encoding="utf-8")
                result = batch.capture(1, "development", "offline test", {"status": "done"})
                self.assertEqual(result["pr"], 7)
                self.assertNotEqual(batch.head, base)
                self.assertEqual(git("diff", "--cached", "--name-only"), "")
                self.assertEqual(batch.guard(), [])
                self.assertEqual(git("rev-parse", "origin/ai/web-web-test-001"), batch.head)


if __name__ == "__main__":
    unittest.main()
