"""Read-only OpenCode model/session preflight for the future GLM/DeepSeek loop.

Does not start AI, create GitHub issues, or change the existing claimer state.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def run(args: list[str], timeout: int = 30) -> tuple[int, str, str]:
    try:
        result = subprocess.run(
            args, cwd=ROOT, text=True, encoding="utf-8", errors="replace",
            capture_output=True, timeout=timeout, check=False,
        )
        return result.returncode, result.stdout, result.stderr
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)


def check_models(glm: str, deepseek: str) -> dict:
    if not shutil.which("opencode"):
        return {"ok": False, "error": "opencode not found in PATH"}
    rc, out, err = run(["opencode", "models"], timeout=45)
    if rc:
        return {"ok": False, "error": (err or out).strip()[:500]}
    available = {line.strip().split()[0] for line in out.splitlines() if line.strip()}
    return {
        "ok": glm in available and deepseek in available and glm != deepseek,
        "glm": {"model": glm, "available": glm in available},
        "deepseek": {"model": deepseek, "available": deepseek in available},
        "distinct_models": glm != deepseek,
        "available_models": sorted(available),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--glm", help="Exact model ID from 'opencode models'")
    parser.add_argument("--deepseek", help="Exact model ID from 'opencode models'")
    parser.add_argument("--list-models", action="store_true")
    args = parser.parse_args()

    if args.list_models:
        rc, out, err = run(["opencode", "models"], timeout=45)
        if rc:
            print(err or out, file=sys.stderr)
            return 1
        print(out, end="")
        return 0

    if not args.glm or not args.deepseek:
        parser.error("Provide both --glm and --deepseek, or use --list-models")
    report = check_models(args.glm, args.deepseek)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
