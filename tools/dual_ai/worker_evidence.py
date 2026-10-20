"""Read-only local Worker verification evidence for coordinator diagnostics.

Does not invent a test result or upload private logs to GitHub.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from tools.dual_ai.live_runner import ROOT

WORKER_DATA = ROOT / "data" / "issue_claimer"
ALLOWED = {"succeeded", "failed", "timed_out", "abandoned"}


def evidence(issue: int, *, data_dir: Path = WORKER_DATA) -> dict:
    if issue < 1:
        raise ValueError("invalid Issue number")
    path = data_dir / "state.json"
    if not path.is_file():
        return {"issue": issue, "status": "missing_worker_state", "verified": False}
    state = json.loads(path.read_text(encoding="utf-8"))
    tasks = state.get("tasks", {})
    if not isinstance(tasks, dict):
        raise ValueError("invalid Worker state")
    task = tasks.get(str(issue))
    if not isinstance(task, dict):
        return {"issue": issue, "status": "not_found", "verified": False}
    # Only the Worker's own durable state can report its result. We do not
    # infer success from PR existence, exit code alone, or GLM prose.
    status = task.get("status")
    result = {
        "issue": issue,
        "status": str(status or "unknown"),
        "verified": status == "succeeded",
        "pr_url": task.get("pr_url") if isinstance(task.get("pr_url"), str) else None,
        "commit_sha": task.get("commit_sha") if isinstance(task.get("commit_sha"), str) else None,
        "stage": task.get("stage") if isinstance(task.get("stage"), str) else None,
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issue", type=int, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(evidence(args.issue), ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        parser.exit(1, f"Evidence unavailable: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
