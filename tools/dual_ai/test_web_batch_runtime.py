"""GitHub handoff and staging boundaries without subprocesses or model calls."""
import json
import hashlib
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
            root = Path(folder).resolve() / "project"
            remote = Path(folder).resolve() / "remote.git"
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

    def prepare_resume(self, root):
        batch = self.batch(root)
        batch.resume_report_ref = 99
        head = "a" * 40
        previous = dict(batch.state, phase="needs_human", last_phase="development",
                        last_report_id=99, round=1, rounds_completed=0,
                        branch=batch.branch, head_sha=head, base_sha="b" * 40,
                        issue=33, pr=34)
        batch.path.write_text(json.dumps(previous), encoding="utf-8")
        source = {"files": {}}
        report = {"batch_id": batch.batch_id, "round": 1, "phase": "development", "pr": 34,
                  "head_sha": head, "verification": {"ok": True}, "source": source,
                  "source_hash": hashlib.sha256(json.dumps(source, sort_keys=True,
                                                          ensure_ascii=False).encode()).hexdigest()}
        batch.fetch = Mock(return_value=report)
        batch.guard = Mock(return_value=[])
        batch.post = Mock(return_value=100)
        batch.api = Mock(side_effect=lambda path, *args:
                         [] if "/comments?" in path else {"state": "open", "head": {"sha": head}})
        return batch, head

    def test_resume_preserves_counter_and_sends_no_model_request_in_preflight(self):
        with tempfile.TemporaryDirectory() as folder:
            batch, head = self.prepare_resume(Path(folder))
            def command(args):
                return "feature/web-three-cycle-batch" if args[0] == "branch" else head if args[0] == "rev-parse" else ""
            with patch(RUNTIME + "ensure_clean_worktree"), \
                 patch(RUNTIME + "command", side_effect=command), \
                 patch(RUNTIME + "read_origin_url", return_value="https://github.com/SuaceYY/AI-Security-Lab-2.0"), \
                 patch(RUNTIME + "read_local_git_config", return_value="config"), \
                 patch(RUNTIME + "VisibleWebSessions") as sessions:
                self.assertIs(batch.enter_resume(), batch)
            self.assertEqual(batch.state["rounds_completed"], 0)
            self.assertEqual(batch.state["resume_attempts"], 1)
            sessions.return_value.check.assert_called_once()
            sessions.return_value.call.assert_not_called()
            self.assertEqual(batch.state["head_sha"], head)

    def test_resume_after_review_dispatch_is_refused_without_overwriting_history(self):
        with tempfile.TemporaryDirectory() as folder:
            batch, head = self.prepare_resume(Path(folder))
            original = batch.path.read_text(encoding="utf-8")
            marker = "<!-- web-batch=" + batch.batch_id + " round=1 phase=review_started -->"
            batch.api = Mock(return_value=[{"body": marker}])
            with patch(RUNTIME + "ensure_clean_worktree"), \
                 patch(RUNTIME + "command", return_value="feature/web-three-cycle-batch"), \
                 patch(RUNTIME + "read_origin_url", return_value="https://github.com/SuaceYY/AI-Security-Lab-2.0"):
                with self.assertRaises(HumanRequired):
                    batch.enter_resume()
            batch.pause("refused")
            self.assertEqual(batch.path.read_text(encoding="utf-8"), original)
            batch.fetch.assert_not_called()

    def test_resume_with_changed_report_or_plan_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            batch, head = self.prepare_resume(Path(folder))
            batch.resume_report_ref = 98
            with self.assertRaises(HumanRequired):
                batch.enter_resume()
            batch.api.assert_not_called()


if __name__ == "__main__":
    unittest.main()
