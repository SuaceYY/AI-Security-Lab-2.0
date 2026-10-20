"""Fail-closed GLM review verdict and optional GitHub feedback.

A successful OpenCode process is NOT an approved PR. The review must include
one explicit structured verdict, and missing/ambiguous verdicts block progress.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

from tools.dual_ai.github_evidence import fetch_pr_evidence, reviewer_prompt
from tools.dual_ai.live_runner import ROOT, stream_role

MARKER = "GLM_REVIEW_VERDICT:"
ALLOWED = {"needs_evidence", "changes_requested", "ready_for_human"}
VERDICT_RE = re.compile(r"GLM_REVIEW_VERDICT:\s*(\{[^\n]*\})")


class Tee:
    def __init__(self, primary):
        self.primary = primary
        self.chunks: list[str] = []

    def write(self, chunk):
        self.primary.write(chunk)
        self.chunks.append(chunk)

    def flush(self):
        self.primary.flush()

    def getvalue(self):
        return "".join(self.chunks)


def parse_verdict(output: str) -> dict:
    matches = VERDICT_RE.findall(output)
    if len(matches) != 1:
        raise ValueError("expected exactly one GLM_REVIEW_VERDICT line")
    value = json.loads(matches[0])
    if not isinstance(value, dict):
        raise ValueError("verdict must be an object")
    if set(value) != {"status", "reason"}:
        raise ValueError("verdict must contain only status and reason")
    if value["status"] not in ALLOWED:
        raise ValueError("invalid review status")
    if not isinstance(value["reason"], str) or not (10 <= len(value["reason"]) <= 1500):
        raise ValueError("invalid reason length")
    return value


def format_comment(pr_number: int, sha: str, verdict: dict) -> str:
    return (
        f"<!-- dual-ai-glm-review sha={sha} -->\n"
        f"### GLM independent review — PR #{pr_number}\n\n"
        f"**Verdict:** `{verdict['status']}`\n\n"
        f"**Reason:** {verdict['reason']}\n\n"
        "AI review is advisory. Human approval and merge are required.\n"
    )


def review(pr_number: int, *, post: bool = False) -> int:
    import sys
    evidence = fetch_pr_evidence(pr_number)
    pr = evidence["pr"]
    if pr.get("state") != "OPEN":
        print("PR is not open; refusing review feedback.", file=sys.stderr)
        return 1
    sha = pr.get("headRefOid")
    if not isinstance(sha, str) or not sha:
        print("Missing head SHA.", file=sys.stderr)
        return 1
    prompt = reviewer_prompt(evidence) + (
        "\n\n最后独立输出且只输出一行结构化裁决（其余分析正常输出）：\n"
        'GLM_REVIEW_VERDICT: {"status":"needs_evidence","reason":"至少十字的中文理由"}\n'
        "status 只能是 needs_evidence、changes_requested、ready_for_human。"
        "只要缺少固定验证或 CI 证据，就必须选 needs_evidence；"
        "ready_for_human 仅表示可提交人工审查，绝不表示自动合并。"
        "不得直接操作 GitHub。"
    )
    tee = Tee(sys.stdout)
    rc = stream_role("planner", prompt, output=tee)
    if rc:
        return rc
    try:
        verdict = parse_verdict(tee.getvalue())
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"FAIL-CLOSED: {exc}; no GitHub feedback posted.", file=sys.stderr)
        return 1
    print(f"[GLM] structured verdict: {verdict['status']}", flush=True)
    if not post:
        print("[GitHub] Preview only; add --post to write review comment.", flush=True)
        print(format_comment(pr_number, sha, verdict), flush=True)
        return 0
    # Re-fetch to avoid writing an obsolete verdict after a new commit.
    current = fetch_pr_evidence(pr_number)["pr"]
    if current.get("headRefOid") != sha or current.get("state") != "OPEN":
        print("PR changed during review; feedback cancelled.", file=sys.stderr)
        return 1
    body = format_comment(pr_number, sha, verdict)
    proc = subprocess.run(
        ["gh", "pr", "comment", str(pr_number), "--body", body],
        cwd=ROOT, text=True, capture_output=True, timeout=30, check=False,
    )
    if proc.returncode:
        print("GitHub comment failed; inspect gh authentication.", file=sys.stderr)
        return 1
    print("[GitHub] GLM verdict posted; no merge or Issue creation.", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--post", action="store_true", help="Explicitly post comment to GitHub")
    args = parser.parse_args()
    return review(args.pr, post=args.post)


if __name__ == "__main__":
    raise SystemExit(main())
