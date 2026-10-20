"""One-shot GitHub PR review dispatcher for the GLM session.

This is a safe, explicit first orchestration step, not an autonomous loop.
It reads GitHub state, ignores drafts, and reviews each PR head SHA once.
No GitHub write, no auto merge, and no changes to the DeepSeek worker.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from tools.dual_ai.github_evidence import gh_json, fetch_pr_evidence, reviewer_prompt
from tools.dual_ai.live_runner import ROOT, stream_role

STATE = ROOT / "data" / "dual_ai" / "review_cursor.json"


def pending_prs(*, root: Path = ROOT, cursor: dict | None = None) -> list[dict]:
    cursor = cursor or {}
    prs = gh_json(["pr", "list", "--state", "open", "--limit", "30",
                   "--json", "number,title,isDraft,headRefOid,url"], root=root)
    if not isinstance(prs, list):
        raise ValueError("invalid PR list")
    return [p for p in prs
            if isinstance(p, dict)
            and isinstance(p.get("number"), int)
            and not p.get("isDraft", False)
            and isinstance(p.get("headRefOid"), str)
            and p["headRefOid"]
            and cursor.get(str(p["number"])) != p["headRefOid"]]


def load_cursor(path: Path = STATE) -> dict:
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("invalid cursor file")
    return value


def save_cursor(cursor: dict, path: Path = STATE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(cursor, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def dispatch_once(*, root: Path = ROOT, state: Path = STATE,
                  timeout: int = 300, dry_run: bool = False) -> int:
    cursor = load_cursor(state)
    candidates = pending_prs(root=root, cursor=cursor)
    if not candidates:
        print("[GLM] No new open, non-draft PR head to review.", flush=True)
        return 0
    candidate = candidates[0]
    number = candidate["number"]
    sha = candidate["headRefOid"]
    print(f"[GitHub] PR #{number} head={sha} review pending", flush=True)
    if dry_run:
        return 0
    evidence = fetch_pr_evidence(number, root=root)
    if evidence["pr"].get("headRefOid") != sha:
        print("[GitHub] PR head changed during fetch; skip.", flush=True)
        return 1
    result = stream_role("planner", reviewer_prompt(evidence), root=root, timeout=timeout)
    if result != 0:
        print("[GLM] Review failed; cursor unchanged.", flush=True)
        return result
    # This checkpoint means "GLM review run completed", not "PR approved".
    cursor[str(number)] = sha
    save_cursor(cursor, state)
    print(f"[GitHub] Recorded review attempt for PR #{number} @ {sha}. "
          "No GitHub comment was posted.", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()
    return dispatch_once(dry_run=args.dry_run, timeout=args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
