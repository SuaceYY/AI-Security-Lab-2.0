from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import claimer as m


def ok(stdout: str = "") -> m.CommandResult:
    return m.CommandResult(0, stdout, "")


class TempDataTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.patcher = mock.patch.multiple(
            m,
            DATA_DIR=root,
            STATE_FILE=root / "state.json",
            STATUS_FILE=root / "status.json",
            HANDOFF_FILE=root / "handoff.md",
            LOCK_FILE=root / "claimer.lock",
            PID_FILE=root / "claimer.pid",
            STOP_FILE=root / "stop.flag",
            LOG_FILE=root / "claimer.log",
        )
        self.patcher.start()

    def tearDown(self) -> None:
        self.patcher.stop()
        self.temp_dir.cleanup()


class IssueSelectionTests(TempDataTest):
    def test_label_filter_and_duplicate_detection(self) -> None:
        state = {"tasks": {}, "first_run_completed": True}
        issue = {"number": 3, "state": "OPEN", "labels": [{"name": "ai-ready"}]}
        with mock.patch.object(m, "remote_claim_exists", return_value=(False, "")):
            self.assertEqual(m.evaluate_issue(state, issue), (True, "eligible"))

        issue["labels"] = []
        eligible, reason = m.evaluate_issue(state, issue)
        self.assertFalse(eligible)
        self.assertIn("ai-ready", reason)

    def test_first_run_is_restricted_to_issue_two(self) -> None:
        state = {"tasks": {}, "first_run_completed": False}
        issue = {"number": 3, "state": "OPEN", "labels": [{"name": "ai-ready"}]}
        eligible, reason = m.evaluate_issue(state, issue)
        self.assertFalse(eligible)
        self.assertIn("issue #2", reason)

    def test_duplicate_task_is_rejected(self) -> None:
        state = {"tasks": {"2": {"status": "succeeded"}}, "first_run_completed": True}
        issue = {"number": 2, "state": "OPEN", "labels": [{"name": "ai-ready"}]}
        eligible, reason = m.evaluate_issue(state, issue)
        self.assertFalse(eligible)
        self.assertIn("already recorded", reason)

    def test_fetch_issue_uses_gh_json(self) -> None:
        payload = json.dumps(
            {
                "number": 2,
                "title": "test",
                "state": "OPEN",
                "labels": [],
                "url": "https://example.invalid/2",
                "body": "body",
            }
        )
        with mock.patch.object(m, "gh", return_value=ok(payload)) as gh:
            issue = m.fetch_issue(2)
        self.assertEqual(issue["number"], 2)
        self.assertIn("--repo", gh.call_args.args[0])

    def test_state_roundtrip(self) -> None:
        state = m.default_state()
        state["tasks"]["2"] = {"status": "running", "stage": "claimed"}
        m.save_state(state)
        loaded = m.load_state()
        self.assertEqual(loaded["tasks"]["2"]["stage"], "claimed")
        self.assertFalse(m.STATE_FILE.with_name("state.json.tmp").exists())


class LockTests(TempDataTest):
    def test_lock_is_exclusive_and_releasable(self) -> None:
        first = m.InstanceLock(m.LOCK_FILE)
        second = m.InstanceLock(m.LOCK_FILE)
        self.assertTrue(first.acquire())
        self.assertFalse(second.acquire())
        first.release()
        self.assertTrue(second.acquire())
        second.release()


class DryRunTests(TempDataTest):
    def test_dry_run_does_not_write_state_or_status(self) -> None:
        issue = {
            "number": 2,
            "title": "test issue",
            "state": "OPEN",
            "labels": [{"name": "ai-ready"}],
            "url": "https://example.invalid/2",
        }
        with (
            mock.patch.object(m, "preflight", return_value={}),
            mock.patch.object(m, "fetch_issue", return_value=issue),
            mock.patch.object(m, "evaluate_issue", return_value=(True, "eligible")),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(m.dry_run(), 0)
        self.assertFalse(m.STATE_FILE.exists())
        self.assertFalse(m.STATUS_FILE.exists())
        self.assertFalse(m.HANDOFF_FILE.exists())


class CommandSafetyTests(TempDataTest):
    def test_opencode_command_never_enables_auto_approval(self) -> None:
        issue = {"number": 2, "title": "test", "body": "requirements"}
        with mock.patch.object(m, "run_command", return_value=ok()) as runner:
            m.run_opencode(issue, 1, "", 30)
        args = runner.call_args.args[0]
        self.assertEqual(args[0], "opencode")
        self.assertIn("--pure", args)
        self.assertIn("--dir", args)
        self.assertNotIn("--auto", args)

    def test_timeout_kills_subprocess(self) -> None:
        result = m.run_command(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            timeout=1,
        )
        self.assertTrue(result.timed_out)

    def test_verification_fails_closed_without_commands(self) -> None:
        with mock.patch.object(m, "VERIFICATION_COMMANDS", ()):
            result = m.run_verification([], 5)
        self.assertFalse(result.ok)

    def test_verification_failure_is_reported(self) -> None:
        failure = m.CommandResult(1, "", "tests failed")
        with (
            mock.patch.object(m, "VERIFICATION_COMMANDS", (("safe",),)),
            mock.patch.object(m, "run_command", return_value=failure),
        ):
            result = m.run_verification([], 5)
        self.assertFalse(result.ok)
        self.assertIn("tests failed", result.detail)

    def test_context_exhaustion_is_detected(self) -> None:
        result = m.CommandResult(1, "", "context window exceeded")
        self.assertTrue(m.context_exhausted(result))


class StagingTests(TempDataTest):
    def test_changed_path_parser_handles_rename_and_untracked(self) -> None:
        output = " M frontend/src/App.tsx\u0000?? new.txt\u0000R  old.txt\u0000new.txt\u0000"
        with mock.patch.object(m, "git", return_value=ok(output)):
            paths = m.collect_changed_paths()
        self.assertEqual(paths, ["frontend/src/App.tsx", "new.txt", "old.txt"])

    def test_runtime_protected_and_sensitive_paths_are_rejected(self) -> None:
        for path in (
            "data/state.json",
            "logs/run.log",
            "tools/issue_claimer/claimer.py",
            "../outside.txt",
        ):
            with self.subTest(path=path), self.assertRaises(m.TaskError):
                m.check_sensitive_paths([path])

    def test_staging_uses_explicit_paths_not_add_all(self) -> None:
        calls: list[list[str]] = []

        def fake_git(args: list[str], timeout: int = 120) -> m.CommandResult:
            calls.append(list(args))
            if args[:2] == ["diff", "--cached"] and "--name-only" in args:
                return ok("frontend/src/App.tsx\u0000")
            if args[:2] == ["diff", "--cached"]:
                return ok()
            return ok()

        with (
            mock.patch.object(m, "git", side_effect=fake_git),
            mock.patch.object(m, "check_staged_secrets"),
        ):
            m.stage_and_validate_changes(["frontend/src/App.tsx"])

        self.assertIn(["add", "--", "frontend/src/App.tsx"], calls)
        self.assertNotIn(["add", "--all"], calls)

    def test_verification_failure_blocks_commit_and_push(self) -> None:
        state = {"tasks": {}, "first_run_completed": False, "last_poll_at": None}
        issue = {"number": 2, "title": "test", "state": "OPEN", "labels": []}

        def fake_git(args: list[str], timeout: int = 120) -> m.CommandResult:
            if args == ["branch", "--show-current"]:
                return ok("ai/issue-2\n")
            if args == ["rev-parse", "HEAD"]:
                return ok("base-sha\n")
            if args == ["remote", "get-url", "origin"]:
                return ok("https://github.com/SuaceYY/AI-Security-Lab-2.0.git\n")
            if args == ["config", "--local", "--list"]:
                return ok("user.name=Test\n")
            if args[:2] == ["diff", "--cached"]:
                return ok()
            return ok()

        with (
            mock.patch.object(m, "load_state", return_value=state),
            mock.patch.object(m, "ensure_clean_worktree"),
            mock.patch.object(m, "remote_claim_exists", return_value=(False, "")),
            mock.patch.object(m, "git", side_effect=fake_git),
            mock.patch.object(m, "run_opencode", return_value=ok()),
            mock.patch.object(m, "collect_changed_paths", return_value=["frontend/src/App.tsx"]),
            mock.patch.object(m, "validate_task_paths"),
            mock.patch.object(
                m,
                "run_verification",
                return_value=m.VerificationResult(False, "fixed verification failed"),
            ),
            mock.patch.object(m, "commit_changes") as commit,
            mock.patch.object(m, "push_branch") as push,
        ):
            with self.assertRaises(m.StopWorker):
                m.process_issue(issue)

        commit.assert_not_called()
        push.assert_not_called()
        self.assertEqual(state["tasks"]["2"]["status"], "failed")


class RepositoryStateTests(TempDataTest):
    def test_origin_validation_requires_exact_github_repository(self) -> None:
        valid = [
            "https://github.com/SuaceYY/AI-Security-Lab-2.0",
            "https://github.com/SuaceYY/AI-Security-Lab-2.0.git",
            "git@github.com:SuaceYY/AI-Security-Lab-2.0.git",
            "ssh://git@github.com/SuaceYY/AI-Security-Lab-2.0.git",
        ]
        invalid = [
            "https://evil.example/SuaceYY/AI-Security-Lab-2.0.git",
            "https://github.com.evil.example/SuaceYY/AI-Security-Lab-2.0.git",
            "https://github.com/attacker/AI-Security-Lab-2.0.git",
            "git@evil.example:SuaceYY/AI-Security-Lab-2.0.git",
            "https://github.com/SuaceYY/AI-Security-Lab-2.0/extra",
        ]
        for url in valid:
            with self.subTest(url=url):
                self.assertTrue(m.origin_matches_repo(url))
        for url in invalid:
            with self.subTest(url=url):
                self.assertFalse(m.origin_matches_repo(url))

    def test_origin_change_is_rejected(self) -> None:
        def fake_git(args: list[str], timeout: int = 120) -> m.CommandResult:
            if args == ["branch", "--show-current"]:
                return ok("ai/issue-2\n")
            if args == ["rev-parse", "HEAD"]:
                return ok("base-sha\n")
            if args == ["remote", "get-url", "origin"]:
                return ok("https://example.invalid/attacker.git\n")
            if args == ["config", "--local", "--list"]:
                return ok("user.name=Test\n")
            return ok()

        with mock.patch.object(m, "git", side_effect=fake_git):
            with self.assertRaises(m.TaskError):
                m.ensure_task_repository_state(
                    "ai/issue-2",
                    "base-sha",
                    "https://github.com/SuaceYY/AI-Security-Lab-2.0.git",
                    "user.name=Test\n",
                )

    def test_git_config_change_is_rejected(self) -> None:
        def fake_git(args: list[str], timeout: int = 120) -> m.CommandResult:
            if args == ["branch", "--show-current"]:
                return ok("ai/issue-2\n")
            if args == ["rev-parse", "HEAD"]:
                return ok("base-sha\n")
            if args == ["remote", "get-url", "origin"]:
                return ok("https://github.com/SuaceYY/AI-Security-Lab-2.0.git\n")
            if args == ["config", "--local", "--list"]:
                return ok("user.name=Changed\n")
            return ok()

        with mock.patch.object(m, "git", side_effect=fake_git):
            with self.assertRaises(m.TaskError):
                m.ensure_task_repository_state(
                    "ai/issue-2",
                    "base-sha",
                    "https://github.com/SuaceYY/AI-Security-Lab-2.0.git",
                    "user.name=Test\n",
                )

    def test_unexpected_error_marks_task_failed(self) -> None:
        state = {"tasks": {}, "first_run_completed": False, "last_poll_at": None}
        issue = {"number": 2, "title": "test", "state": "OPEN", "labels": []}
        with (
            mock.patch.object(m, "load_state", return_value=state),
            mock.patch.object(m, "save_state"),
            mock.patch.object(m, "write_checkpoint") as checkpoint,
            mock.patch.object(m, "git", side_effect=RuntimeError("boom")),
        ):
            with self.assertRaises(m.StopWorker):
                m.process_issue(issue)

        self.assertEqual(state["tasks"]["2"]["status"], "failed")
        self.assertIn("RuntimeError", state["tasks"]["2"]["failure_reason"])
        checkpoint.assert_called()


class CheckpointTests(TempDataTest):
    def test_checkpoint_is_atomic_redacted_and_local(self) -> None:
        fake_token = "ghp_" + "a" * 36
        task = {
            "issue_number": 2,
            "title": f"token {fake_token}",
            "branch": "ai/issue-2",
            "commit_sha": None,
            "pr_url": None,
            "failure_reason": None,
        }
        m.write_checkpoint(task, "verified", "stage changes", "passed")

        status = json.loads(m.STATUS_FILE.read_text(encoding="utf-8"))
        handoff = m.HANDOFF_FILE.read_text(encoding="utf-8")
        self.assertEqual(status["stage"], "verified")
        self.assertEqual(status["tests"], "passed")
        self.assertNotIn(fake_token, handoff)
        self.assertIn("[REDACTED]", handoff)
        self.assertFalse(m.STATUS_FILE.with_name("status.json.tmp").exists())
        self.assertFalse(m.HANDOFF_FILE.with_name("handoff.md.tmp").exists())

    def test_recovery_marks_stale_running_task_failed(self) -> None:
        state = {
            "tasks": {"2": {"status": "running", "stage": "opencode_attempt_1"}},
            "first_run_completed": False,
        }
        with (
            mock.patch.object(m, "load_state", return_value=state),
            mock.patch.object(m, "save_state") as save,
            mock.patch.object(m, "write_checkpoint") as checkpoint,
        ):
            self.assertTrue(m.recover_stale_tasks())

        self.assertEqual(state["tasks"]["2"]["status"], "failed")
        self.assertEqual(state["tasks"]["2"]["stage"], "recovery_required")
        save.assert_called_once()
        checkpoint.assert_called_once()


if __name__ == "__main__":
    unittest.main()
