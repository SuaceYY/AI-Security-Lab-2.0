"""Offline real Git backup/restore and refusal tests. No model calls."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tools.dual_ai import recover_local as recovery
from tools.dual_ai import web_batch_runtime as rt
from tools.dual_ai.web_batch import HumanRequired
from tools.issue_claimer import claimer as worker

PLAN = json.loads((Path(__file__).parent / "web_batch_plan.json").read_text(encoding="utf-8"))


class SavedWorktreeTests(unittest.TestCase):
    def test_real_stash_retains_modified_and_untracked_code(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            def git(*args):
                return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True,
                                      encoding="utf-8").stdout.strip()
            git("init", "-b", "main")
            git("config", "user.name", "Offline Test")
            git("config", "user.email", "test@example.invalid")
            (root / "backend").mkdir()
            (root / "backend/main.py").write_text("# original\n", encoding="utf-8")
            git("add", ".")
            git("commit", "-m", "base")
            head = git("rev-parse", "HEAD")
            git("switch", "-c", "ai/web-web-test-001")
            (root / "backend/main.py").write_text("# modified\n", encoding="utf-8")
            (root / "backend/scan_service.py").write_text("# new\n", encoding="utf-8")
            git("stash", "push", "-u", "-m", "retain code")
            stash = git("rev-parse", "refs/stash")
            batch = Mock(head=head, plan=PLAN)
            with patch.object(worker, "PROJECT_ROOT", root), patch.object(worker, "DATA_DIR", root / "data"):
                paths = recovery.stash_paths(batch, stash)
            self.assertEqual(paths, ["backend/main.py", "backend/scan_service.py"])
            git("stash", "apply", stash)
            self.assertEqual((root / "backend/main.py").read_text(encoding="utf-8"), "# modified\n")
            self.assertEqual((root / "backend/scan_service.py").read_text(encoding="utf-8"), "# new\n")
            self.assertEqual(git("rev-parse", "refs/stash"), stash)

    def test_wrong_stash_base_is_refused_before_apply(self):
        batch = Mock(head="a" * 40, plan=PLAN)
        with patch.object(rt, "command", return_value="s " + "b" * 40 + " index") as command:
            with self.assertRaises(HumanRequired):
                recovery.stash_paths(batch, "c" * 40)
        self.assertEqual(command.call_count, 1)

    def test_recapture_reuses_recorded_result_and_keeps_completed_round(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            batch = recovery.SavedDevelopmentBatch("web-test-001", PLAN, "c" * 40)
            batch.path = root / "state.json"
            head = "a" * 40
            state = dict(batch.state, phase="needs_human", action="development", round=2,
                         rounds_completed=1, head_sha=head, base_sha="b" * 40,
                         branch=batch.branch, issue=33, pr=34, question="单条预算",
                         sessions={"executor": "ses_ds"},
                         context_events=[{"round": 2, "phase": "model_usage",
                                          "data": {"role": "executor", "message_id": "msg_saved"}}])
            batch.path.write_text(json.dumps(state), encoding="utf-8")
            result = {"status": "done", "summary": "existing development", "instructions": [], "question": ""}
            text = "WEB_BATCH_RESULT: " + json.dumps(result)
            response = {"info": {"role": "assistant", "id": "msg_saved", "sessionID": "ses_ds",
                                 "providerID": "provider", "modelID": "deepseek"},
                        "parts": [{"type": "text", "text": text}]}
            client = Mock()
            client._request.return_value = response
            batch.api = Mock(side_effect=lambda path, *args:
                             [[]] if "/comments?" in path else {"state": "open", "head": {"sha": head}})
            batch.guard = Mock()
            report = {"phase": "development", "verification": {"ok": True}}
            batch.capture = Mock(return_value=report)
            batch.publish = Mock(return_value=123)
            paths = ["backend/main.py", "backend/scan_service.py"]
            def command(args):
                return "feature/web-three-cycle-batch" if args[0] == "branch" else head if args[0] == "rev-parse" else ""
            with patch.object(rt, "ensure_clean_worktree"), \
                 patch.object(rt, "command", side_effect=command), \
                 patch.object(rt, "read_origin_url", return_value="https://github.com/SuaceYY/AI-Security-Lab-2.0"), \
                 patch.object(rt, "read_local_git_config", return_value="config"), \
                 patch.object(rt, "collect_changed_paths", return_value=paths), \
                 patch.object(rt, "backend_python", return_value={"executable": "existing-env"}), \
                 patch.object(recovery, "stash_paths", return_value=paths), \
                 patch.object(recovery, "VisibleWebSessions") as sessions:
                sessions.return_value.client.return_value = (
                    client, {"session_id": "ses_ds", "model": "provider/deepseek"})
                self.assertIs(batch.prepare_saved_development(), batch)
            sessions.return_value.call.assert_not_called()
            client._request.assert_called_once_with("GET", "/session/ses_ds/message/msg_saved")
            batch.capture.assert_called_once_with(2, "development", text, result)
            batch.publish.assert_called_once_with(2, "development", report)
            self.assertEqual(batch.state["rounds_completed"], 1)
            self.assertEqual(batch.state["resume_report_ref"], 123)
            self.assertEqual(batch.state["local_recovery_stash"], "c" * 40)


if __name__ == "__main__":
    unittest.main()
