from __future__ import annotations

import copy
import io
import json
import os
import shutil
import subprocess
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

    def test_failed_task_is_not_claimed_again(self) -> None:
        for status, stage in (
            ("failed", "failed"),
            ("timed_out", "timed_out"),
            ("failed", "recovery_required"),
        ):
            with self.subTest(status=status, stage=stage):
                state = {
                    "tasks": {"2": {"status": status, "stage": stage}},
                    "first_run_completed": True,
                }
                issue = {
                    "number": 2,
                    "state": "OPEN",
                    "labels": [{"name": "ai-ready"}],
                }
                with mock.patch.object(
                    m, "remote_claim_exists", return_value=(False, "")
                ):
                    eligible, reason = m.evaluate_issue(state, issue)
                self.assertFalse(eligible)
                self.assertIn("already recorded", reason)

    def test_select_next_issue_scans_ready_issues_after_first_run(self) -> None:
        state = {
            "tasks": {"2": {"status": "succeeded", "stage": "succeeded"}},
            "first_run_completed": True,
        }
        issue_two = {
            "number": 2,
            "state": "OPEN",
            "labels": [{"name": "ai-ready"}],
        }
        issue_three = {
            "number": 3,
            "state": "OPEN",
            "labels": [{"name": "ai-ready"}],
        }
        with (
            mock.patch.object(m, "load_state", return_value=state),
            mock.patch.object(m, "fetch_issue", side_effect=AssertionError),
            mock.patch.object(
                m,
                "list_ready_issues",
                return_value=[issue_two, issue_three],
            ),
            mock.patch.object(m, "remote_claim_exists", return_value=(False, "")),
        ):
            issue = m.select_next_issue()
        self.assertIsNotNone(issue)
        self.assertEqual(issue["number"], 3)

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

    def test_default_lock_uses_patched_path_not_import_time_default(self) -> None:
        production_lock = m.PROJECT_ROOT / "data" / "issue_claimer" / "claimer.lock"
        lock = m.InstanceLock()
        self.assertEqual(lock.path, m.LOCK_FILE)
        self.assertNotEqual(lock.path, production_lock)
        self.assertTrue(lock.acquire())
        try:
            self.assertTrue(m.LOCK_FILE.exists())
            self.assertTrue(m.instance_is_running())
        finally:
            lock.release()
        self.assertFalse(m.instance_is_running())

    def test_default_lock_is_independent_when_another_worker_holds_a_lock(self) -> None:
        with tempfile.TemporaryDirectory() as other_dir:
            worker_lock = m.InstanceLock(Path(other_dir) / "claimer.lock")
            self.assertTrue(worker_lock.acquire())
            try:
                probe = m.InstanceLock()
                self.assertEqual(probe.path, m.LOCK_FILE)
                self.assertTrue(probe.acquire())
                probe.release()
            finally:
                worker_lock.release()


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

    def test_dry_run_uses_worker_selection_after_first_run(self) -> None:
        state = {"tasks": {}, "first_run_completed": True}
        issue = {
            "number": 3,
            "title": "new issue",
            "state": "OPEN",
            "labels": [{"name": "ai-ready"}],
            "url": "https://example.invalid/3",
        }
        output = io.StringIO()
        with (
            mock.patch.object(m, "preflight", return_value={}),
            mock.patch.object(m, "load_state", return_value=state),
            mock.patch.object(m, "fetch_issue", side_effect=AssertionError),
            mock.patch.object(m, "list_ready_issues", return_value=[issue]),
            mock.patch.object(m, "remote_claim_exists", return_value=(False, "")),
            redirect_stdout(output),
        ):
            self.assertEqual(m.dry_run(), 0)

        self.assertIn("number: 3", output.getvalue())
        self.assertIn("claimable: yes", output.getvalue())
        self.assertFalse(m.STATE_FILE.exists())
        self.assertFalse(m.STATUS_FILE.exists())

    def test_dry_run_reports_reason_when_no_issue_is_eligible(self) -> None:
        state = {"tasks": {}, "first_run_completed": True}
        output = io.StringIO()
        with (
            mock.patch.object(m, "preflight", return_value={}),
            mock.patch.object(m, "load_state", return_value=state),
            mock.patch.object(m, "list_ready_issues", return_value=[]),
            redirect_stdout(output),
        ):
            self.assertEqual(m.dry_run(), 2)

        self.assertIn("number: (none)", output.getvalue())
        self.assertIn("no open issues", output.getvalue())
        self.assertIn("claimable: no", output.getvalue())


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
            mock.patch.object(m.shutil, "which", return_value="/fixed/tool"),
            mock.patch.object(m, "run_command", return_value=failure),
        ):
            result = m.run_verification(["frontend/src/App.tsx"], 5)
        self.assertFalse(result.ok)
        self.assertIn("tests failed", result.detail)


class VerificationMatrixTests(TempDataTest):
    def test_frontend_plan_uses_fixed_commands(self) -> None:
        calls: list[tuple[list[str], Path]] = []

        def fake_run(
            command: list[str],
            cwd: Path,
            timeout: int,
            env: dict[str, str] | None = None,
        ) -> m.CommandResult:
            calls.append((list(command), cwd))
            return ok()

        with (
            mock.patch.object(m.shutil, "which", side_effect=lambda name: f"/fixed/{name}"),
            mock.patch.object(m, "run_command", side_effect=fake_run),
        ):
            result = m.run_verification(["frontend/src/App.tsx"], 60)

        self.assertTrue(result.ok, result.detail)
        expected = [
            list(m.VERIFICATION_COMMANDS[0]),
            ["/fixed/npm", "ci"],
            ["/fixed/node", "--test", "tests/fileFilter.test.ts"],
            ["/fixed/npm", "run", "build"],
            ["/fixed/npm", "run", "lint"],
        ]
        self.assertEqual([command for command, _ in calls], expected)
        frontend = m.PROJECT_ROOT / "frontend"
        self.assertTrue(all(cwd == frontend for _, cwd in calls[1:]))

    def test_unknown_path_fails_closed(self) -> None:
        with mock.patch.object(m, "run_command") as runner:
            result = m.run_verification(["README.md"], 5)
        self.assertFalse(result.ok)
        self.assertIn("no fixed verification plan", result.detail)
        runner.assert_not_called()

    def test_actions_workflow_is_statically_validated(self) -> None:
        valid = """name: frontend-ci
on:
  pull_request:
    branches: [main]
permissions:
  contents: read
jobs:
  test:
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: frontend
    steps:
      - uses: actions/setup-node@v4
        with:
          node-version: 24
      - run: npm ci
      - run: node --test tests/fileFilter.test.ts
      - run: npm run build
      - run: npm run lint
"""
        with tempfile.TemporaryDirectory() as root_name:
            root = Path(root_name)
            workflows = root / ".github" / "workflows"
            workflows.mkdir(parents=True)
            workflow = workflows / "frontend-ci.yml"
            workflow.write_text(valid, encoding="utf-8")

            with (
                mock.patch.object(m, "PROJECT_ROOT", root),
                mock.patch.object(m, "run_command", return_value=ok()),
            ):
                result = m.run_verification(
                    [".github/workflows/frontend-ci.yml"],
                    5,
                )

            self.assertTrue(result.ok, result.detail)

            workflow.write_text(
                valid.replace("contents: read", "contents: write"),
                encoding="utf-8",
            )
            with (
                mock.patch.object(m, "PROJECT_ROOT", root),
                mock.patch.object(m, "run_command", return_value=ok()),
            ):
                invalid = m.run_verification(
                    [".github/workflows/frontend-ci.yml"],
                    5,
                )
            self.assertFalse(invalid.ok)
            self.assertIn("contents: read", invalid.detail)

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

    def test_staged_secret_patterns_are_rejected(self) -> None:
        diffs = (
            f"diff --git a/config.txt b/config.txt\n+token=ghp_{'a' * 36}\n",
            "diff --git a/key.pem b/key.pem\n+-----BEGIN "
            "RSA PRIVATE KEY-----\n",
        )
        for diff in diffs:
            with self.subTest(diff=diff.splitlines()[0]):
                with mock.patch.object(m, "git", return_value=ok(diff)):
                    with self.assertRaises(m.TaskError):
                        m.check_staged_secrets()

    def test_staged_byte_limit_boundary(self) -> None:
        for size, allowed in (
            (m.MAX_STAGED_BYTES, True),
            (m.MAX_STAGED_BYTES + 1, False),
        ):
            with self.subTest(size=size):
                root = Path(tempfile.mkdtemp(dir=m.PROJECT_ROOT))
                self.addCleanup(
                    lambda root=root: shutil.rmtree(root, ignore_errors=True)
                )
                candidate = root / "limit.bin"
                with candidate.open("wb") as handle:
                    handle.truncate(size)
                relative = candidate.relative_to(m.PROJECT_ROOT).as_posix()
                if allowed:
                    m.validate_task_paths([relative])
                else:
                    with self.assertRaises(m.TaskError):
                        m.validate_task_paths([relative])

    def test_symlink_file_is_rejected_before_resolution(self) -> None:
        root = Path(tempfile.mkdtemp(dir=m.PROJECT_ROOT))
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        target = root / "target.txt"
        target.write_text("target", encoding="utf-8")
        link = root / "link.txt"
        try:
            os.symlink(target, link)
        except OSError as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")

        relative = link.relative_to(m.PROJECT_ROOT).as_posix()
        with self.assertRaises(m.TaskError):
            m.validate_task_paths([relative])

    def test_symlink_component_is_rejected_before_resolution(self) -> None:
        root = Path(tempfile.mkdtemp(dir=m.PROJECT_ROOT))
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        target = root / "target"
        target.mkdir()
        (target / "file.txt").write_text("target", encoding="utf-8")
        link = root / "linked"
        try:
            os.symlink(target, link, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")

        relative = (link / "file.txt").relative_to(m.PROJECT_ROOT).as_posix()
        with self.assertRaises(m.TaskError):
            m.validate_task_paths([relative])

    def test_original_path_symlink_metadata_is_rejected(self) -> None:
        real_lstat = m.os.lstat

        def fake_lstat(path: Path) -> object:
            if Path(path).name == "App.tsx":
                return mock.Mock(st_mode=m.stat.S_IFLNK, st_file_attributes=0)
            return real_lstat(path)

        with mock.patch.object(m.os, "lstat", side_effect=fake_lstat):
            with self.assertRaises(m.TaskError):
                m.validate_task_paths(["frontend/src/App.tsx"])

    def test_link_metadata_detects_symlink_and_reparse_point(self) -> None:
        symlink = mock.Mock(st_mode=m.stat.S_IFLNK, st_file_attributes=0)
        reparse = mock.Mock(
            st_mode=m.stat.S_IFREG,
            st_file_attributes=getattr(
                m.stat,
                "FILE_ATTRIBUTE_REPARSE_POINT",
                0x400,
            ),
        )
        regular = mock.Mock(st_mode=m.stat.S_IFREG, st_file_attributes=0)
        for info, expected in ((symlink, True), (reparse, True), (regular, False)):
            with self.subTest(expected=expected):
                with mock.patch.object(m.os, "lstat", return_value=info):
                    self.assertEqual(
                        m.is_link_or_reparse(Path("candidate")),
                        expected,
                    )

    def test_reparse_point_component_is_rejected(self) -> None:
        def fake_reparse(path: Path) -> bool:
            return Path(path).name == "frontend"

        with mock.patch.object(m, "is_link_or_reparse", side_effect=fake_reparse):
            with self.assertRaises(m.TaskError):
                m.validate_task_paths(["frontend/src/App.tsx"])

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

    def test_head_mismatch_is_rejected(self) -> None:
        def fake_git(args: list[str], timeout: int = 120) -> m.CommandResult:
            if args == ["branch", "--show-current"]:
                return ok("ai/issue-2\n")
            if args == ["rev-parse", "HEAD"]:
                return ok("other-sha\n")
            raise AssertionError(f"unexpected git call: {args}")

        with mock.patch.object(m, "git", side_effect=fake_git):
            with self.assertRaises(m.TaskError) as raised:
                m.ensure_task_repository_state(
                    "ai/issue-2",
                    "base-sha",
                    "https://github.com/SuaceYY/AI-Security-Lab-2.0.git",
                    "user.name=Test\n",
                )
        self.assertIn("history", str(raised.exception))

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

        task = state["tasks"]["2"]
        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["stage"], "failed")
        self.assertIn("RuntimeError", task["failure_reason"])
        self.assertIn("opencode run", task["recovery"])
        self.assertIn("completed_at", task)
        checkpoint.assert_called()

    def test_interrupt_marks_task_failed_with_recovery_details(self) -> None:
        state = {"tasks": {}, "first_run_completed": False, "last_poll_at": None}
        issue = {"number": 2, "title": "test", "state": "OPEN", "labels": []}
        with (
            mock.patch.object(m, "load_state", return_value=state),
            mock.patch.object(m, "save_state"),
            mock.patch.object(m, "write_checkpoint") as checkpoint,
            mock.patch.object(m, "git", side_effect=KeyboardInterrupt()),
        ):
            with self.assertRaises(KeyboardInterrupt):
                m.process_issue(issue)

        task = state["tasks"]["2"]
        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["stage"], "interrupted")
        self.assertIn("KeyboardInterrupt", task["failure_reason"])
        self.assertIn("opencode run", task["recovery"])
        self.assertIn("completed_at", task)
        checkpoint.assert_called()


class ManualRecoveryTests(TempDataTest):
    def recovery_state(self) -> dict[str, object]:
        return {
            "version": 1,
            "first_run_completed": True,
            "tasks": {
                "2": {
                    "issue_number": 2,
                    "title": "broken task",
                    "status": "failed",
                    "stage": "recovery_required",
                    "failure_reason": "previous worker stopped",
                    "tests": "not_run",
                    "recovery": "opencode run --continue",
                    "recovery_count": 1,
                    "next_step": "inspect worktree",
                }
            },
            "recovery_history": [],
        }

    def test_recovery_requires_confirmation_and_writes_nothing(self) -> None:
        state = self.recovery_state()
        output = io.StringIO()
        with (
            mock.patch.object(m, "configure_logging", return_value=mock.Mock()),
            mock.patch.object(m, "load_state", return_value=state),
            mock.patch.object(m, "save_state") as save,
            redirect_stdout(output),
        ):
            result = m.manual_recover(2, "retry", confirm=False, note="approved")

        self.assertEqual(result, 2)
        self.assertIn("no changes written", output.getvalue())
        save.assert_not_called()
        self.assertEqual(
            state["tasks"]["2"]["stage"],
            "recovery_required",
        )
        self.assertEqual(state["recovery_history"], [])

    def test_retry_is_explicit_audited_and_claimable(self) -> None:
        state = self.recovery_state()
        with (
            mock.patch.object(m, "configure_logging", return_value=mock.Mock()),
            mock.patch.object(m, "load_state", return_value=state),
            mock.patch.object(m, "save_state") as save,
            mock.patch.object(m, "write_checkpoint") as checkpoint,
        ):
            result = m.manual_recover(2, "retry", confirm=True, note="approved")

        task = state["tasks"]["2"]
        self.assertEqual(result, 0)
        self.assertEqual(task["status"], m.RETRY_AUTHORIZED_STATUS)
        self.assertEqual(task["stage"], m.MANUAL_RETRY_STAGE)
        self.assertEqual(task["failure_reason"], "previous worker stopped")
        self.assertEqual(task["manual_recovery"]["action"], "retry")
        self.assertEqual(task["manual_recovery"]["note"], "approved")
        self.assertEqual(task["recovery_history"][0]["action"], "retry")
        self.assertEqual(len(state["recovery_history"]), 1)
        archived = state["recovery_history"][0]
        self.assertEqual(archived["previous_stage"], "recovery_required")
        self.assertEqual(archived["task"]["stage"], "recovery_required")
        save.assert_called_once()
        checkpoint.assert_called_once()

        issue = {"number": 2, "state": "OPEN", "labels": [{"name": "ai-ready"}]}
        with mock.patch.object(m, "remote_claim_exists", return_value=(False, "")):
            eligible, reason = m.evaluate_issue(state, issue)
        self.assertTrue(eligible, reason)

    def test_repeated_recovery_does_not_duplicate_or_consume_authorization(self) -> None:
        state = self.recovery_state()
        with (
            mock.patch.object(m, "configure_logging", return_value=mock.Mock()),
            mock.patch.object(m, "load_state", return_value=state),
            mock.patch.object(m, "save_state") as save,
            mock.patch.object(m, "write_checkpoint"),
        ):
            first = m.manual_recover(2, "retry", confirm=True)
            second = m.manual_recover(2, "retry", confirm=True)

        self.assertEqual(first, 0)
        self.assertEqual(second, 2)
        task = state["tasks"]["2"]
        self.assertEqual(task["status"], m.RETRY_AUTHORIZED_STATUS)
        self.assertEqual(task["stage"], m.MANUAL_RETRY_STAGE)
        self.assertEqual(len(state["recovery_history"]), 1)
        self.assertEqual(len(task["recovery_history"]), 1)
        save.assert_called_once()

    def test_close_keeps_failure_record_and_remains_unclaimable(self) -> None:
        state = self.recovery_state()
        with (
            mock.patch.object(m, "configure_logging", return_value=mock.Mock()),
            mock.patch.object(m, "load_state", return_value=state),
            mock.patch.object(m, "save_state") as save,
            mock.patch.object(m, "write_checkpoint"),
        ):
            result = m.manual_recover(2, "close", confirm=True)

        task = state["tasks"]["2"]
        self.assertEqual(result, 0)
        self.assertEqual(task["status"], m.ABANDONED_STATUS)
        self.assertEqual(task["stage"], m.MANUAL_CLOSED_STAGE)
        self.assertEqual(task["failure_reason"], "previous worker stopped")
        self.assertIn("not be re-claimed", task["next_step"])
        self.assertEqual(state["recovery_history"][0]["action"], "close")
        save.assert_called_once()

        issue = {"number": 2, "state": "OPEN", "labels": [{"name": "ai-ready"}]}
        eligible, reason = m.evaluate_issue(state, issue)
        self.assertFalse(eligible)
        self.assertIn("already recorded", reason)

    def test_only_recovery_required_tasks_can_be_recovered(self) -> None:
        state = self.recovery_state()
        state["tasks"]["2"]["stage"] = "failed"
        with (
            mock.patch.object(m, "configure_logging", return_value=mock.Mock()),
            mock.patch.object(m, "load_state", return_value=state),
            mock.patch.object(m, "save_state") as save,
        ):
            result = m.manual_recover(2, "retry", confirm=True)

        self.assertEqual(result, 2)
        save.assert_not_called()
        self.assertEqual(state["recovery_history"], [])

    def test_recovery_is_blocked_while_a_worker_holds_the_lock(self) -> None:
        state = self.recovery_state()
        with (
            mock.patch.object(m, "configure_logging", return_value=mock.Mock()),
            mock.patch.object(m, "InstanceLock") as lock_class,
            mock.patch.object(m, "load_state", return_value=state),
            mock.patch.object(m, "save_state") as save,
        ):
            lock_class.return_value.acquire.return_value = False
            result = m.manual_recover(2, "retry", confirm=True)

        self.assertEqual(result, 1)
        save.assert_not_called()
        self.assertEqual(state["tasks"]["2"]["stage"], "recovery_required")

    def test_authorized_retry_is_archived_when_worker_claims_it(self) -> None:
        state = {
            "tasks": {
                "2": {
                    "issue_number": 2,
                    "status": m.RETRY_AUTHORIZED_STATUS,
                    "stage": m.MANUAL_RETRY_STAGE,
                    "failure_reason": "old failure",
                }
            },
            "first_run_completed": True,
            "recovery_history": [],
        }
        issue = {"number": 2, "title": "test", "state": "OPEN", "labels": []}
        with (
            mock.patch.object(m, "load_state", return_value=state),
            mock.patch.object(m, "save_state"),
            mock.patch.object(m, "write_checkpoint"),
            mock.patch.object(m, "preflight_retry"),
            mock.patch.object(m, "git", side_effect=RuntimeError("boom")),
        ):
            with self.assertRaises(m.StopWorker):
                m.process_issue(issue)

        self.assertEqual(len(state["recovery_history"]), 1)
        archived = state["recovery_history"][0]
        self.assertEqual(archived["action"], "retry_started")
        self.assertEqual(archived["previous_stage"], m.MANUAL_RETRY_STAGE)
        self.assertEqual(archived["previous_failure_reason"], "old failure")
        self.assertEqual(archived["task"]["stage"], m.MANUAL_RETRY_STAGE)
        self.assertEqual(state["tasks"]["2"]["status"], "failed")


class RealGitRecoveryTests(TempDataTest):
    def setUp(self) -> None:
        super().setUp()
        self.repo_temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.repo_temp.name).resolve()
        self._run_git("init", "-b", "main")
        self._run_git("config", "user.name", "Test")
        self._run_git("config", "user.email", "test@example.invalid")
        (self.repo / "base.txt").write_text("base\n", encoding="utf-8")
        self._run_git("add", "base.txt")
        self._run_git("commit", "-m", "base")
        self.project_patcher = mock.patch.object(m, "PROJECT_ROOT", self.repo)
        self.project_patcher.start()

    def tearDown(self) -> None:
        self.project_patcher.stop()
        self.repo_temp.cleanup()
        super().tearDown()

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=str(self.repo),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

    def _run_git(self, *args: str) -> subprocess.CompletedProcess[str]:
        result = self._git(*args)
        if result.returncode != 0:
            raise AssertionError(
                f"git {' '.join(args)} failed: {result.stderr or result.stdout}"
            )
        return result

    def retry_state(self) -> dict[str, object]:
        prior = {
            "issue_number": 2,
            "action": "retry",
            "previous_failure_reason": "old failure",
        }
        return {
            "version": 1,
            "first_run_completed": True,
            "tasks": {
                "2": {
                    "issue_number": 2,
                    "title": "old task",
                    "status": m.RETRY_AUTHORIZED_STATUS,
                    "stage": m.MANUAL_RETRY_STAGE,
                    "branch": "ai/issue-2",
                    "failure_reason": "old failure",
                    "tests": "not_run",
                    "recovery": "opencode run --continue",
                    "recovery_count": 1,
                    "recovery_history": [copy.deepcopy(prior)],
                    "next_step": "explicit retry authorized",
                }
            },
            "recovery_history": [copy.deepcopy(prior)],
        }

    def issue(self) -> dict[str, object]:
        return {
            "number": 2,
            "title": "test",
            "state": "OPEN",
            "labels": [{"name": "ai-ready"}],
        }

    def test_dirty_worktree_keeps_retry_authorization(self) -> None:
        state = self.retry_state()
        history = copy.deepcopy(state["recovery_history"])
        (self.repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")

        with (
            mock.patch.object(m, "load_state", return_value=state),
            mock.patch.object(m, "save_state") as save,
            mock.patch.object(m, "write_checkpoint") as checkpoint,
            mock.patch.object(m, "remote_claim_exists") as remote,
        ):
            with self.assertRaises(m.StopWorker):
                m.process_issue(self.issue())

        task = state["tasks"]["2"]
        self.assertEqual(task["status"], m.RETRY_AUTHORIZED_STATUS)
        self.assertEqual(task["stage"], m.MANUAL_RETRY_STAGE)
        self.assertEqual(task["failure_reason"], "old failure")
        self.assertEqual(state["recovery_history"], history)
        self.assertIn("working tree", task["preflight_failure"])
        self.assertIn("retry authorization retained", task["next_step"])
        self.assertEqual(task["preflight_history"][-1]["result"], "failed")
        remote.assert_not_called()
        save.assert_called_once()
        checkpoint.assert_called_once()

    def test_existing_local_branch_keeps_retry_authorization(self) -> None:
        state = self.retry_state()
        history = copy.deepcopy(state["recovery_history"])
        self._run_git("switch", "-c", "ai/issue-2")
        self._run_git("switch", "main")

        with (
            mock.patch.object(m, "load_state", return_value=state),
            mock.patch.object(m, "save_state"),
            mock.patch.object(m, "write_checkpoint"),
            mock.patch.object(m, "remote_claim_exists") as remote,
        ):
            with self.assertRaises(m.StopWorker):
                m.process_issue(self.issue())

        task = state["tasks"]["2"]
        self.assertEqual(task["status"], m.RETRY_AUTHORIZED_STATUS)
        self.assertEqual(state["recovery_history"], history)
        self.assertIn("local task branch already exists", task["preflight_failure"])
        self.assertIn("retry authorization retained", task["next_step"])
        remote.assert_not_called()

    def test_clean_authorized_retry_preflight_passes(self) -> None:
        with mock.patch.object(m, "remote_claim_exists", return_value=(False, "")):
            m.preflight_retry(2, "ai/issue-2")
        self.assertFalse(m.local_branch_exists("ai/issue-2"))

    def test_remote_branch_keeps_retry_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as bare_name:
            bare = Path(bare_name)
            self._run_git("init", "--bare", str(bare))
            self._run_git("remote", "add", "origin", str(bare))
            self._run_git("push", "origin", "main")
            self._run_git("switch", "-c", "ai/issue-2")
            self._run_git("push", "origin", "ai/issue-2")
            self._run_git("switch", "main")
            self._run_git("branch", "-D", "ai/issue-2")

            with self.assertRaises(m.TaskError) as raised:
                m.preflight_retry(2, "ai/issue-2")
        self.assertIn("remote branch already exists", str(raised.exception))

    def test_existing_pull_request_keeps_retry_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as bare_name:
            bare = Path(bare_name)
            self._run_git("init", "--bare", str(bare))
            self._run_git("remote", "add", "origin", str(bare))
            self._run_git("push", "origin", "main")
            prs = json.dumps(
                [{"number": 9, "url": "https://example.invalid/pr/9", "state": "OPEN"}]
            )

            with (
                mock.patch.object(m, "gh", return_value=ok(prs)),
                self.assertRaises(m.TaskError) as raised,
            ):
                m.preflight_retry(2, "ai/issue-2")
        self.assertIn("pull request already exists", str(raised.exception))

    def test_clean_retry_archives_prior_failure_before_new_attempt(self) -> None:
        state = self.retry_state()
        prior_history = copy.deepcopy(state["tasks"]["2"]["recovery_history"])

        with (
            mock.patch.object(m, "load_state", return_value=state),
            mock.patch.object(m, "save_state"),
            mock.patch.object(m, "write_checkpoint"),
            mock.patch.object(m, "remote_claim_exists", return_value=(False, "")),
        ):
            with self.assertRaises(m.StopWorker):
                m.process_issue(self.issue())

        archived = [
            event
            for event in state["recovery_history"]
            if isinstance(event, dict) and event.get("action") == "retry_started"
        ]
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0]["previous_failure_reason"], "old failure")
        self.assertEqual(archived[0]["task"]["status"], m.RETRY_AUTHORIZED_STATUS)
        self.assertEqual(archived[0]["task"]["recovery_history"], prior_history)
        self.assertEqual(state["tasks"]["2"]["status"], "failed")


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
            "tasks": {
                "2": {
                    "status": "running",
                    "stage": "opencode_attempt_1",
                    "tests": "not_run",
                    "recovery_count": 0,
                }
            },
            "first_run_completed": False,
        }
        with (
            mock.patch.object(m, "load_state", return_value=state),
            mock.patch.object(m, "save_state") as save,
            mock.patch.object(m, "write_checkpoint") as checkpoint,
        ):
            self.assertTrue(m.recover_stale_tasks())

        task = state["tasks"]["2"]
        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["stage"], "recovery_required")
        self.assertEqual(task["previous_stage"], "opencode_attempt_1")
        self.assertIn("previous worker stopped", task["failure_reason"])
        self.assertIn("opencode run", task["recovery"])
        self.assertIn("recovered_at", task)
        self.assertEqual(task["recovery_count"], 1)
        self.assertTrue(state["first_run_completed"])
        save.assert_called_once()
        checkpoint.assert_called_once()
        self.assertEqual(checkpoint.call_args.args[4], task["recovery"])

    def test_recovery_is_not_repeated_and_task_stays_unclaimable(self) -> None:
        state = {
            "tasks": {
                "2": {
                    "status": "running",
                    "stage": "opencode_attempt_1",
                    "recovery_count": 0,
                }
            },
            "first_run_completed": False,
        }
        with (
            mock.patch.object(m, "load_state", return_value=state),
            mock.patch.object(m, "save_state") as save,
            mock.patch.object(m, "write_checkpoint"),
        ):
            self.assertTrue(m.recover_stale_tasks())
            self.assertFalse(m.recover_stale_tasks())

        task = state["tasks"]["2"]
        self.assertEqual(task["recovery_count"], 1)
        self.assertEqual(task["stage"], "recovery_required")
        save.assert_called_once()

        issue = {"number": 2, "state": "OPEN", "labels": [{"name": "ai-ready"}]}
        eligible, reason = m.evaluate_issue(state, issue)
        self.assertFalse(eligible)
        self.assertIn("already recorded", reason)


if __name__ == "__main__":
    unittest.main()
