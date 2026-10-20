"""Safe, bounded handoff from GLM feedback to the existing DeepSeek worker.

The worker owns code execution and PR creation. This module only publishes
an explicit, issue-scoped repair request after GLM's changes_requested verdict.
No repair is queued for needs_evidence. Never re-open historic failed Issues.
"""
from __future__ import annotations

import argparse
import json
import re

from tools.dual_ai.coordinator import gh, load_state, save_state, linked_to_issue
from tools.dual_ai.github_evidence import gh_json
from tools.dual_ai.live_runner import ROOT

REPO = "SuaceYY/AI-Security-Lab-2.0"
HISTORIC_FAILED = {2, 6, 9}
MAX_ROUNDS = 3


def latest_review(state: dict, pr_number: int) -> dict | None:
    reviews = [r for r in state.get("reviewed", []) if r.get("pr") == pr_number]
    return reviews[-1] if reviews else None


def prepare_repair(state: dict, pr_number: int) -> dict:
    record = latest_review(state, pr_number)
    if not record or record.get("status") != "changes_requested":
        raise ValueError("Only changes_requested can request DeepSeek repair")
    if not state.get("created"):
        raise ValueError("No coordinator-owned Issue")
    issue = state["created"][-1]
    if issue in HISTORIC_FAILED:
        raise ValueError("Historical failed Issue cannot be retried")
    attempts = state.setdefault("repair_rounds", {}).get(str(pr_number), 0)
    if attempts >= MAX_ROUNDS:
        raise ValueError("Maximum repair rounds reached")
    sha = record.get("sha")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Invalid reviewed head SHA")
    return {"issue": issue, "pr": pr_number, "sha": sha, "round": attempts + 1}


def dispatch(pr_number: int, *, write: bool = False) -> None:
    state = load_state()
    repair = prepare_repair(state, pr_number)
    pr = gh_json(["pr", "view", str(pr_number), "--json",
                  "headRefOid,body,state"], root=ROOT)
    if pr.get("state") != "OPEN" or pr.get("headRefOid") != repair["sha"]:
        raise ValueError("PR changed since GLM review; cannot queue stale repair")
    issue = repair["issue"]
    if not re.search(rf"(?i)(?:closes|fixes|resolves)\s+#{issue}\b",
                     str(pr.get("body") or "")):
        raise ValueError("PR not linked to coordinator-owned Issue")
    # The marker prevents duplicate dispatch even after a local crash.
    marker = f"<!-- dual-ai-repair pr={pr_number} sha={repair['sha']} -->"
    comments = gh_json(["api", f"repos/{REPO}/issues/{issue}/comments",
                        "--paginate"], root=ROOT)
    if not isinstance(comments, list):
        raise ValueError("Invalid Issue comments")
    if any(marker in str(c.get("body") or "") for c in comments):
        print(f"[GitHub] Repair request already exists for PR #{pr_number}; skip.")
        return
    message = (
        marker + "\n### DeepSeek repair handoff\n\n"
        f"GLM requested changes to PR #{pr_number} at `{repair['sha']}`.\n"
        "Review the existing GLM feedback on the PR. Do not alter protected paths.\n"
        "Keep the same Issue/PR; run the worker's fixed validation and attach evidence.\n"
        "No automatic merge. Human approval required.\n"
    )
    if not write:
        print(f"[Coordinator] Preview repair request for Issue #{issue}:\n{message}")
        return
    # Important: comments alone do not cause the existing worker to re-run.
    # Explicitly do NOT add ai-ready or alter Worker state until it supports repair.
    gh("issue", "comment", str(issue), "--body", message)
    state.setdefault("repair_rounds", {})[str(pr_number)] = repair["round"]
    state["phase"] = "repair_requested_manual_worker_handoff"
    save_state(state)
    print("[GitHub] Repair request posted; worker re-claim NOT automated.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    try:
        dispatch(args.pr, write=args.write)
    except (RuntimeError, ValueError) as exc:
        parser.exit(1, f"[Coordinator] BLOCKED: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
