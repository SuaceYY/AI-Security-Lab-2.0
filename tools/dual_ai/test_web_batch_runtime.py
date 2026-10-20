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
from tools.dual_ai.web_batch_runtime import WebBatch, FENCE, format_compatibility_checkpoint
from tools.issue_claimer import claimer as worker

PLAN = json.loads((Path(__file__).parent / "web_batch_plan.json").read_text(encoding="utf-8"))
# Legacy offline tests intentionally exercise Python code edits. The active batch
# plan can be narrower; never relax its production scope to satisfy a test.
PLAN["allowed_suffixes"] = [".py", ".ts", ".tsx", ".js", ".jsx", ".css", ".json"]
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

    def test_large_unicode_report_posts_only_under_24kb_and_round_trips(self):
        with tempfile.TemporaryDirectory() as folder:
            batch = self.batch(Path(folder))
            stored = {}
            def api(path, *args):
                if args:
                    body = next(value[5:] for value in args if value.startswith("body="))
                    self.assertLessEqual(len(body.encode("utf-8")), 24000)
                    ref = 100 + len(stored)
                    stored[ref] = body
                    return {"id": ref}
                return {"body": stored[int(path.rsplit("/", 1)[1])]}
            batch.api = api
            report = {"batch_id": batch.batch_id, "round": 2, "phase": "development",
                      "model_result": {"summary": "保留所有证据", "status": "done"},
                      "source": {"files": {"backend/large.py": {"content": "中文😀" * 6500}}},
                      "verification": {"ok": False, "steps": [{"output": "log\\u001b" * 3000}]}}
            with patch(RUNTIME + "DATA", Path(folder)):
                ref = batch.post(2, "development", report)
            self.assertEqual(batch.fetch(ref), report)
            self.assertGreater(len(stored), 4)
            self.assertIn("readable_pages", stored[ref])
            self.assertTrue((Path(folder) / batch.batch_id / "report-2-development.json").is_file())

    def formatted_result(self, status="pass"):
        return {"status": status, "summary": "原结论", "instructions": ["原建议"], "question": ""}

    def test_format_recovery_is_once_readonly_and_does_not_replay_task(self):
        with tempfile.TemporaryDirectory() as folder:
            batch = self.batch(Path(folder))
            batch.fingerprint = Mock(return_value="same")
            corrected = "WEB_BATCH_RESULT: " + json.dumps(self.formatted_result())
            batch.sessions = Mock()
            batch.sessions.call.return_value = corrected
            result = batch.result(3, "review", "planner", "原结论为通过，但结果格式写错")
            self.assertEqual(result["status"], "pass")
            self.assertEqual(batch.sessions.call.call_count, 1)
            self.assertEqual(batch.sessions.call.call_args.kwargs, {"format_only": True})
            self.assertTrue((Path(folder) / batch.batch_id / "response-3-review.json").exists())
            batch.sessions.call.reset_mock()
            with self.assertRaises(HumanRequired):
                batch.result(3, "review", "planner", "still invalid")
            batch.sessions.call.assert_not_called()

    def test_invalid_formatter_does_not_loop(self):
        with tempfile.TemporaryDirectory() as folder:
            batch = self.batch(Path(folder))
            batch.fingerprint = Mock(return_value="same")
            batch.sessions = Mock()
            batch.sessions.call.return_value = "invalid again"
            with self.assertRaises(HumanRequired):
                batch.result(2, "repair", "executor", "invalid first")
            self.assertEqual(batch.sessions.call.call_count, 1)

    def test_human_or_conflicting_verdict_never_enters_formatter(self):
        with tempfile.TemporaryDirectory() as folder:
            batch = self.batch(Path(folder))
            batch.sessions = Mock()
            human = self.formatted_result("needs_human")
            human["question"] = "permission needed"
            text = "WEB_BATCH_RESULT: " + json.dumps(human)
            for reply in (text, text + "\nWEB_BATCH_RESULT: " + json.dumps(self.formatted_result())):
                with self.assertRaises(HumanRequired):
                    batch.result(3, "review", "planner", reply)
            batch.sessions.call.assert_not_called()

    def test_formatter_worktree_change_stops(self):
        with tempfile.TemporaryDirectory() as folder:
            batch = self.batch(Path(folder))
            batch.fingerprint = Mock(side_effect=["before", "after"])
            batch.sessions = Mock()
            batch.sessions.call.return_value = "WEB_BATCH_RESULT: " + json.dumps(self.formatted_result())
            with self.assertRaisesRegex(HumanRequired, "仓库发生修改"):
                batch.result(1, "development", "executor", "bad format")

    def review_recovery_fixture(self, root):
        batch = self.batch(root)
        head = "a" * 40
        bindings = {"planner": "ses_glm", "executor": "ses_ds"}
        previous = dict(batch.state, phase="needs_human", action="review", last_phase="development",
                        last_report_id=99, round=3, rounds_completed=2, head_sha=head, base_sha="b" * 40,
                        branch=batch.branch, issue=33, pr=34, sessions=bindings,
                        question="模型未返回唯一的 WEB_BATCH_RESULT，需人工核对会话。",
                        context_events=[{"round": 3, "phase": "model_usage",
                                         "data": {"role": "planner", "message_id": "msg_review"}}])
        batch.path.write_text(json.dumps(previous), encoding="utf-8")
        source = {"files": {}}
        report = {"batch_id": batch.batch_id, "round": 3, "phase": "development", "pr": 34,
                  "head_sha": head, "verification": {"ok": True}, "source": source,
                  "source_hash": hashlib.sha256(json.dumps(source, sort_keys=True,
                                                          ensure_ascii=False).encode()).hexdigest()}
        batch.fetch = Mock(return_value=report)
        marker = "<!-- web-batch=" + batch.batch_id + " round=3 phase=review_started -->"
        batch.api = Mock(side_effect=lambda path, *args:
                         [[{"body": marker}]] if "/comments?" in path else
                         {"state": "open", "head": {"sha": head}})
        batch.guard = Mock()
        batch.publish = Mock(return_value=123)
        text = "**WEB_BATCH_RESULT:** " + json.dumps(self.formatted_result())
        response = {"info": {"role": "assistant", "id": "msg_review", "sessionID": "ses_glm",
                             "providerID": "provider", "modelID": "glm", "cost": 0},
                    "parts": [{"type": "text", "text": text}]}
        session = Mock()
        session.session_ids = bindings
        client = Mock()
        client._request.return_value = response
        session.client.return_value = (client, {"session_id": "ses_glm", "model": "provider/glm"})
        return batch, head, session, client

    def test_completed_review_recovery_uses_exact_get_not_another_review(self):
        with tempfile.TemporaryDirectory() as folder:
            batch, head, session, client = self.review_recovery_fixture(Path(folder))
            def command(args):
                return "feature/web-three-cycle-batch" if args[0] == "branch" else head if args[0] == "rev-parse" else ""
            with patch(RUNTIME + "ensure_clean_worktree"), \
                 patch(RUNTIME + "command", side_effect=command), \
                 patch(RUNTIME + "read_origin_url", return_value="https://github.com/SuaceYY/AI-Security-Lab-2.0"), \
                 patch(RUNTIME + "read_local_git_config", return_value="config"), \
                 patch(RUNTIME + "backend_python", return_value={"executable": "existing-env"}), \
                 patch(RUNTIME + "VisibleWebSessions", return_value=session):
                self.assertIs(batch.enter_resume_review(), batch)
            self.assertEqual(batch.state["rounds_completed"], 2)
            self.assertEqual(batch.state["round"], 3)
            self.assertEqual(batch.state["resume_review_ref"], 123)
            client._request.assert_called_once_with("GET", "/session/ses_glm/message/msg_review")
            session.call.assert_not_called()
            review = batch.publish.call_args.args[2]
            self.assertEqual(review["model_result"]["status"], "pass")
            self.assertEqual(review["reused_message_id"], "msg_review")
            batch.publish.assert_called_once()

    def test_recovery_refuses_prior_repair_dispatch_without_state_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            batch, head, session, client = self.review_recovery_fixture(Path(folder))
            previous = batch.path.read_text(encoding="utf-8")
            prefix = "<!-- web-batch=" + batch.batch_id + " round=3 phase="
            batch.api = Mock(return_value=[[{"body": prefix + "review_started -->"},
                                           {"body": prefix + "repair_started -->"}]])
            with patch(RUNTIME + "ensure_clean_worktree"), \
                 patch(RUNTIME + "command", return_value="feature/web-three-cycle-batch"), \
                 patch(RUNTIME + "read_origin_url", return_value="https://github.com/SuaceYY/AI-Security-Lab-2.0"):
                with self.assertRaises(HumanRequired):
                    batch.enter_resume_review()
            batch.pause("refused")
            self.assertEqual(batch.path.read_text(encoding="utf-8"), previous)
            batch.publish.assert_not_called()
            session.call.assert_not_called()

    def compatibility_state(self, batch):
        text = "原GLM审查回复：格式需要整理"
        previous = json.loads(batch.path.read_text(encoding="utf-8"))
        previous.update(
            review_recovery_attempts=1,
            question="APIError 403 FreeTierError: OpenCode's free tier can only be used from within OpenCode",
            format_recoveries={"3-review": {
                "attempts": 1, "role": "planner", "reason": "原结果格式不合法",
                "original_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            }},
        )
        return previous, text

    def test_compatibility_matches_only_recorded_toolfree_formatter_rejection(self):
        with tempfile.TemporaryDirectory() as folder:
            batch, _, _, _ = self.review_recovery_fixture(Path(folder))
            previous, _ = self.compatibility_state(batch)
            self.assertTrue(format_compatibility_checkpoint(previous))
            for changes in ({"question": "403 forbidden"},
                            {"question": "quota exhausted"},
                            {"review_recovery_attempts": 0},
                            {"format_compatibility_recovery_attempts": 1},
                            {"format_recoveries": {}}):
                changed = dict(previous, **changes)
                self.assertFalse(format_compatibility_checkpoint(changed))
            previous["format_recoveries"]["3-review"]["corrected_response"] = "already completed"
            self.assertFalse(format_compatibility_checkpoint(previous))

    def test_explicit_compatibility_continuation_preserves_counter_and_failed_record(self):
        with tempfile.TemporaryDirectory() as folder:
            batch, head, session, client = self.review_recovery_fixture(Path(folder))
            previous, text = self.compatibility_state(batch)
            batch.path.write_text(json.dumps(previous), encoding="utf-8")
            batch.resume_format_compatibility = True
            client._request.return_value["parts"] = [{"type": "text", "text": text}]
            session.call.return_value = "WEB_BATCH_RESULT: " + json.dumps(self.formatted_result())
            batch.fingerprint = Mock(return_value="same")
            def command(args):
                return "feature/web-three-cycle-batch" if args[0] == "branch" else head if args[0] == "rev-parse" else ""
            with patch(RUNTIME + "ensure_clean_worktree"), \
                 patch(RUNTIME + "command", side_effect=command), \
                 patch(RUNTIME + "read_origin_url", return_value="https://github.com/SuaceYY/AI-Security-Lab-2.0"), \
                 patch(RUNTIME + "read_local_git_config", return_value="config"), \
                 patch(RUNTIME + "backend_python", return_value={"executable": "existing-env"}), \
                 patch(RUNTIME + "VisibleWebSessions", return_value=session):
                self.assertIs(batch.enter_resume_review(), batch)
            self.assertEqual(batch.state["rounds_completed"], 2)
            self.assertEqual(batch.state["review_recovery_attempts"], 1)
            self.assertEqual(batch.state["format_compatibility_recovery_attempts"], 1)
            saved = batch.state["format_recoveries"]["3-review"]
            self.assertEqual(saved["attempts"], 2)
            self.assertEqual(saved["compatibility_attempts"], 1)
            self.assertEqual(saved["previous_attempt"]["attempts"], 1)
            self.assertIn("FreeTierError", saved["previous_403"])
            session.call.assert_called_once()
            self.assertEqual(session.call.call_args.kwargs, {"format_only": True})
            self.assertEqual(batch.state["resume_review_ref"], 123)
            with self.assertRaises(HumanRequired):
                batch.result(3, "review", "planner", text)
            self.assertEqual(session.call.call_count, 1)

    def test_compatibility_refuses_different_original_response_without_call(self):
        with tempfile.TemporaryDirectory() as folder:
            batch, _, session, _ = self.review_recovery_fixture(Path(folder))
            previous, _ = self.compatibility_state(batch)
            batch.state = previous
            batch.resume_format_compatibility = True
            batch.state["format_compatibility_recovery_attempts"] = 1
            batch.sessions = session
            with self.assertRaises(HumanRequired):
                batch.result(3, "review", "planner", "不同原回复")
            session.call.assert_not_called()

    def test_compatibility_does_not_auto_retry_another_provider_rejection(self):
        with tempfile.TemporaryDirectory() as folder:
            batch, _, session, _ = self.review_recovery_fixture(Path(folder))
            previous, text = self.compatibility_state(batch)
            batch.state = previous
            batch.resume_format_compatibility = True
            batch.state["format_compatibility_recovery_attempts"] = 1
            batch.state["format_compatibility_recovery"] = {"previous_error": previous["question"]}
            batch.sessions = session
            batch.fingerprint = Mock(return_value="same")
            session.call.side_effect = HumanRequired("APIError 403 FreeTierError")
            with self.assertRaisesRegex(HumanRequired, "403"):
                batch.result(3, "review", "planner", text)
            with self.assertRaises(HumanRequired):
                batch.result(3, "review", "planner", text)
            self.assertEqual(session.call.call_count, 1)


if __name__ == "__main__":
    unittest.main()
