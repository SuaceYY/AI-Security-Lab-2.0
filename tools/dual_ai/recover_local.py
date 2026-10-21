"""Explicit one-time recovery of a completed, unpublished development phase.

A pinned local Git stash preserves tracked and untracked edits while the tool
branch updates. No development prompt is replayed; only the recorded assistant
result is read, fixed validation reruns, and GitHub review continues.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from tools.dual_ai import web_batch_runtime as rt
from tools.dual_ai.web_batch import HumanRequired, check_scope, parse_result, plan_digest, run_batch, validate_plan
from tools.dual_ai.web_context import VisibleWebSessions
from tools.dual_ai.visible_tui import message_role, message_text, verify_response_model


def stash_paths(batch, stash):
    if not re.fullmatch(r"[0-9a-f]{40}", stash):
        raise HumanRequired("恢复需要完整且固定的 stash 提交 SHA。")
    parents = rt.command(["rev-list", "--parents", "-n", "1", stash]).split()
    if len(parents) not in (3, 4) or parents[1] != batch.head:
        raise HumanRequired("stash 不是原批次提交上的工作区备份。")
    if rt.command(["diff", "--name-only", batch.head, parents[2], "--"]):
        raise HumanRequired("备份含暂存改动，需要人工核对暂存归属。")
    paths = rt.command(["diff", "--name-only", batch.head, stash, "--"]).splitlines()
    if len(parents) == 4:
        paths += rt.command(["ls-tree", "-r", "--name-only", parents[3]]).splitlines()
    paths = sorted(set(paths))
    if not paths:
        raise HumanRequired("备份没有开发改动。")
    check_scope(paths, batch.plan)
    return paths


class SavedDevelopmentBatch(rt.WebBatch):
    def __init__(self, batch_id, plan, stash):
        super().__init__(batch_id, plan)
        self.stash = stash

    def __enter__(self):
        self.replay_refused = True
        if not self.batch_lock.acquire():
            raise HumanRequired("已有批次运行。")
        if not self.worker_lock.acquire():
            self.batch_lock.release()
            raise HumanRequired("旧 Worker 仍在运行。")
        try:
            return self.prepare_saved_development()
        except BaseException:
            # Keep the stash and every restored edit. Switch back only if clean.
            try:
                if (self.original_branch and self.head
                        and rt.command(["branch", "--show-current"]) == self.branch
                        and rt.command(["rev-parse", "HEAD"]) == self.head
                        and not rt.collect_changed_paths()):
                    rt.command(["switch", self.original_branch])
            finally:
                self.worker_lock.release()
                self.batch_lock.release()
            raise

    def prepare_saved_development(self):
        if not self.path.is_file():
            raise HumanRequired("缺少原批次状态，禁止另建批次。")
        previous = json.loads(self.path.read_text(encoding="utf-8"))
        number = previous.get("round")
        if (previous.get("batch_id") != self.batch_id
                or previous.get("plan_sha256") != plan_digest(self.plan)
                or previous.get("phase") != "needs_human"
                or previous.get("action") != "development"
                or type(number) is not int or not 1 <= number <= 3
                or previous.get("rounds_completed") != number - 1
                or previous.get("local_recovery_attempts", 0) >= 1
                or previous.get("branch") != self.branch
                or "单条预算" not in str(previous.get("question", ""))):
            raise HumanRequired("原状态不符合已完成开发但报告未发布的恢复点。")
        head, base = previous.get("head_sha"), previous.get("base_sha")
        if (not isinstance(head, str) or not re.fullmatch(r"[0-9a-f]{40}", head)
                or not isinstance(base, str) or not re.fullmatch(r"[0-9a-f]{40}", base)
                or type(previous.get("issue")) is not int or type(previous.get("pr")) is not int):
            raise HumanRequired("原提交/Issue/PR 记录不完整。")
        rt.ensure_clean_worktree()
        self.original_branch = rt.command(["branch", "--show-current"])
        if not self.original_branch or self.original_branch in {"main", self.branch}:
            raise HumanRequired("请先将工作区备份后在更新过的工具分支运行。")
        self.head, self.base_sha = head, base
        self.issue, self.pr = previous["issue"], previous["pr"]
        self.origin = rt.read_origin_url()
        if not rt.origin_matches_repo(self.origin):
            raise HumanRequired("恢复仓库 origin 不匹配。")
        pages = self.api("issues/" + str(self.issue) + "/comments?per_page=100", "--paginate", "--slurp")
        if not isinstance(pages, list) or not all(isinstance(page, list) for page in pages):
            raise HumanRequired("无法完整核对交接历史。")
        comments = [comment for page in pages for comment in page]
        prefix = "<!-- web-batch=" + self.batch_id + " round=" + str(number) + " phase="
        forbidden = ("review_started -->", "development -->", "round_complete -->")
        if any(any(prefix + phase in c.get("body", "") for phase in forbidden)
               for c in comments if isinstance(c, dict)):
            raise HumanRequired("本轮可能已发布或投递 GLM，禁止重复执行。")
        pr = self.api("pulls/" + str(self.pr))
        if pr.get("state") != "open" or pr.get("head", {}).get("sha") != head:
            raise HumanRequired("PR 已变化，需要主审核对。")
        if rt.command(["rev-parse", self.branch]) != head:
            raise HumanRequired("本地批次分支已变化。")
        rt.command(["fetch", "origin", self.branch])
        if rt.command(["rev-parse", "FETCH_HEAD"]) != head:
            raise HumanRequired("远端批次分支已变化。")
        paths = stash_paths(self, self.stash)
        # Select an already installed backend environment before restoring edits.
        self.backend_environment = rt.backend_python(rt.ROOT)
        self.sessions = VisibleWebSessions(rt.ROOT, self.context_checkpoint)
        self.sessions.check()
        client, binding = self.sessions.client("executor")
        if previous.get("sessions", {}).get("executor") != binding["session_id"]:
            raise HumanRequired("DeepSeek 绑定会话已变化。")
        events = [event for event in previous.get("context_events", [])
                  if event.get("round") == number and event.get("phase") == "model_usage"
                  and event.get("data", {}).get("role") == "executor"]
        if len(events) != 1:
            raise HumanRequired("无法唯一定位已完成的 DeepSeek 开发消息。")
        message_id = events[0]["data"].get("message_id")
        if not isinstance(message_id, str) or not re.fullmatch(r"msg_[A-Za-z0-9_-]+", message_id):
            raise HumanRequired("开发消息 ID 无效。")
        sid = binding["session_id"]
        response = client._request("GET", "/session/" + sid + "/message/" + message_id)
        info = response.get("info", {}) if isinstance(response, dict) else {}
        if (message_role(response) != "assistant" or info.get("id") != message_id
                or info.get("sessionID") != sid or info.get("error")):
            raise HumanRequired("记录的开发结果不是绑定会话的完整响应。")
        verify_response_model(response, binding["model"])
        text = message_text(response).strip()
        result = parse_result(text)
        if result["status"] != "done":
            raise HumanRequired("记录的开发消息未明确完成。")
        rt.command(["switch", self.branch])
        self.config = rt.read_local_git_config()
        rt.command(["stash", "apply", self.stash])
        if sorted(rt.collect_changed_paths()) != paths:
            raise HumanRequired("恢复文件集合与备份不一致，保留现场。")
        self.guard()
        self.state = previous
        self.state["local_recovery_attempts"] = previous.get("local_recovery_attempts", 0) + 1
        self.state["local_recovery_stash"] = self.stash
        self.replay_refused = False
        self.local_checkpoint("recapturing_development", {
            "question": "", "backend_environment": self.backend_environment,
            "reused_message_id": message_id, "status": "resuming",
        })
        # No model POST here: validate the restored work and reuse its exact report.
        report = self.capture(number, "development", text, result)
        report_ref = self.publish(number, "development", report)
        self.local_checkpoint("published", {"resume_report_ref": report_ref})
        print("已恢复第" + str(number) + "轮现场并分段发布证据；从GLM评审继续，保留原计数。",
              flush=True)
        return self


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--stash", required=True)
    parser.add_argument("--plan", default="tools/dual_ai/web_batch_plan.json")
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{4,63}", args.batch_id):
        parser.error("invalid batch ID")
    if not args.write:
        parser.error("--write is required for this explicit recovery")
    plan = validate_plan(json.loads(Path(args.plan).read_text(encoding="utf-8-sig")))
    batch = SavedDevelopmentBatch(args.batch_id, plan, args.stash)
    try:
        with batch:
            result = run_batch(batch, plan, first_round=batch.state["round"],
                               development_report_ref=batch.state["resume_report_ref"])
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
        print("三轮批次已停止，等待主审评估；Git备份仍保留。", flush=True)
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        batch.pause(str(exc) or "操作员中断")
        print("已停止并保留备份，需要人工处理：" + (str(exc) or "操作员中断"), flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
