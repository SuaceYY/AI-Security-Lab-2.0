"""Exactly three visible DeepSeek -> GitHub -> GLM -> DeepSeek cycles.

Every completed cycle includes the repair/report phase. A completed batch is
terminal and requires a new operator-approved plan and batch ID to start again.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROUNDS = 3
MARKER = "WEB_BATCH_RESULT:"
VALID_STATUSES = {"done", "pass", "changes_requested", "needs_evidence", "needs_human"}


class HumanRequired(RuntimeError):
    """An explicit operator decision or recovery is required."""


def parse_result(text: str) -> dict:
    markers = list(re.finditer(r"(?m)^[ \t]*WEB_BATCH_RESULT:[ \t]*", text))
    if len(markers) != 1:
        raise HumanRequired("模型未返回唯一的 WEB_BATCH_RESULT，需人工核对会话。")
    try:
        obj, end = json.JSONDecoder().raw_decode(text[markers[0].end():].lstrip())
    except (ValueError, TypeError) as exc:
        raise HumanRequired("模型的结构化结果不是有效 JSON。") from exc
    tail = text[markers[0].end():].lstrip()[end:]
    if tail.strip() or not isinstance(obj, dict):
        raise HumanRequired("模型结构化结果后出现额外内容。")
    if set(obj) != {"status", "summary", "instructions", "question"}:
        raise HumanRequired("模型结果字段不完整或包含未批准字段。")
    if not isinstance(obj["status"], str) or obj["status"] not in VALID_STATUSES:
        raise HumanRequired("模型返回了未知状态。")
    if not isinstance(obj["summary"], str) or not 1 <= len(obj["summary"]) <= 3000:
        raise HumanRequired("模型结果缺少有界总结。")
    if (not isinstance(obj["instructions"], list) or len(obj["instructions"]) > 12
            or any(not isinstance(x, str) or not 1 <= len(x) <= 1200 for x in obj["instructions"])):
        raise HumanRequired("模型建议格式不合法。")
    if not isinstance(obj["question"], str) or len(obj["question"]) > 1500:
        raise HumanRequired("人工问题格式不合法。")
    if obj["status"] == "needs_human":
        raise HumanRequired(obj["question"] or obj["summary"])
    return obj


def validate_plan(plan: dict) -> dict:
    required = {"schema_version", "revision", "title", "roadmap", "goal",
                "allowed_roots", "allowed_suffixes", "round_focus"}
    if not isinstance(plan, dict) or set(plan) != required or plan["schema_version"] != 1:
        raise ValueError("invalid web batch plan schema")
    if not isinstance(plan["revision"], int) or plan["revision"] < 1:
        raise ValueError("invalid plan revision")
    for key in ("title", "roadmap", "goal"):
        if not isinstance(plan[key], str) or not 1 <= len(plan[key]) <= 12000:
            raise ValueError("invalid plan text: " + key)
    if plan["allowed_roots"] != ["frontend/src/", "frontend/tests/", "backend/"]:
        raise ValueError("only the approved web source/test roots are allowed")
    suffixes = plan["allowed_suffixes"]
    if (not isinstance(suffixes, list) or not suffixes
            or any(not isinstance(x, str) or x not in {".py", ".ts", ".tsx", ".js", ".jsx", ".css", ".json"} for x in suffixes)):
        raise ValueError("invalid allowed source suffixes")
    focus = plan["round_focus"]
    if (not isinstance(focus, list) or len(focus) != ROUNDS
            or any(not isinstance(x, str) or not 1 <= len(x) <= 5000 for x in focus)):
        raise ValueError("the approved plan must contain exactly three round focuses")
    return plan


def plan_digest(plan: dict) -> str:
    return hashlib.sha256(json.dumps(plan, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def check_scope(paths: list[str], plan: dict) -> None:
    for path in paths:
        if not isinstance(path, str):
            raise HumanRequired("文件路径不是字符串。")
        parts = Path(path).parts
        if ("\\" in path or path.startswith("/")
                or ".." in parts or not any(path.startswith(root) for root in plan["allowed_roots"])
                or Path(path).suffix not in plan["allowed_suffixes"]
                or any(x in {".git", ".env", "__pycache__", "node_modules", ".venv"} for x in parts)
                or Path(path).name in {"package.json", "package-lock.json", "tsconfig.json"}):
            raise HumanRequired("文件超出本批网页开发范围：" + str(path))


def run_batch(bridge, plan: dict) -> dict:
    """The bridge owns real IO; tests can verify ordering without model calls."""
    validate_plan(plan)
    last = None
    for number in range(1, ROUNDS + 1):
        development = bridge.develop(number, plan["round_focus"][number - 1], last)
        report_ref = bridge.publish(number, "development", development)
        published_report = bridge.fetch(report_ref)
        review = bridge.review(number, published_report)
        review_ref = bridge.publish(number, "review", review)
        published_review = bridge.fetch(review_ref)
        repair = bridge.repair(number, published_review)
        repair_ref = bridge.publish(number, "repair", repair)
        bridge.round_complete(number, repair_ref)
        last = bridge.fetch(repair_ref)
    return bridge.finish(ROUNDS, last)


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", default="tools/dual_ai/web_batch_plan.json")
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--check", action="store_true", help="read-only TUI compatibility check; no AI call")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{4,63}", args.batch_id):
        parser.error("batch-id must be 5..64 lowercase letters/digits/hyphens")
    from tools.dual_ai.web_batch_runtime import WebBatch
    if args.status:
        print(WebBatch.status(args.batch_id))
        return 0
    if args.check:
        try:
            from tools.dual_ai.web_context import VisibleWebSessions
            from tools.dual_ai.web_batch_runtime import ROOT
            checked = VisibleWebSessions(ROOT, lambda *args: None).check()
            print(json.dumps(checked, ensure_ascii=False, indent=2), flush=True)
            return 0
        except Exception as exc:
            print("会话兼容性检查未通过：" + str(exc), flush=True)
            return 2
    if not args.write:
        parser.error("use --write to start one explicitly authorized three-cycle batch")
    plan = validate_plan(json.loads(Path(args.plan).read_text(encoding="utf-8-sig")))
    bridge = WebBatch(args.batch_id, plan)
    try:
        with bridge:
            result = run_batch(bridge, plan)
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
        print("三轮批次已停止。请联系主审助手核对 GitHub 进度、证据与可行度，再取得下一批启动指令。", flush=True)
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        bridge.pause(str(exc) or "操作员中断")
        print("已停止，需要人工处理：" + (str(exc) or "操作员中断"), flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
