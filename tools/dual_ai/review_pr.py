"""Read-only GLM review of an actual GitHub pull request.

This is a deliberate gated milestone: the review is visible in real time,
but is not yet allowed to post, merge, or create follow-up issues.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from tools.dual_ai.github_evidence import fetch_pr_evidence, reviewer_prompt
from tools.dual_ai.live_runner import stream_role


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()
    evidence = fetch_pr_evidence(args.pr)
    pr = evidence["pr"]
    print(f"GitHub PR #{pr['number']} @ {pr.get('headRefOid', 'unknown')}", flush=True)
    print("GLM will review actual GitHub evidence; no GitHub writes will occur.", flush=True)
    return stream_role("planner", reviewer_prompt(evidence), timeout=args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
