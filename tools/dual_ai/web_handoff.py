"""Lossless GitHub comment payloads beside human-readable reports.

gh's terminal sanitizer can rewrite nested JSON Unicode escapes (e.g. Vite's
ANSI colors) into caret sequences. ASCII base64 parts preserve exact evidence;
the visible JSON remains readable but is never used to drive enveloped calls.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re

from tools.dual_ai.web_batch import HumanRequired

FENCE = chr(96) * 3
PREFIX = "<!-- web-payload-v1: "
MAX_PAYLOAD_BYTES = 192000
COMMENT_BYTES = 24000
CHUNK_CHARS = 16000
MAX_PARTS = 16
LEGACY_CHUNK_CHARS = 42000


def encode_payload(serialized: str):
    raw = serialized.encode("utf-8")
    if len(raw) > MAX_PAYLOAD_BYTES:
        raise HumanRequired("GitHub 交接数据超过预算，请人工核对任务范围。")
    encoded = base64.b64encode(raw).decode("ascii")
    chunks = [encoded[i:i + CHUNK_CHARS] for i in range(0, len(encoded), CHUNK_CHARS)]
    return chunks, hashlib.sha256(raw).hexdigest()


def payload_frame(ids, digest):
    return PREFIX + json.dumps({"parts": ids, "sha256": digest}, separators=(",", ":")) + " -->"


def fenced_json(body):
    start = body.find(FENCE + "json\n")
    if start < 0 or not body.endswith("\n" + FENCE):
        raise HumanRequired("GitHub 报告结构不完整。")
    try:
        return json.loads(body[start + len(FENCE) + 5:-(len(FENCE) + 1)])
    except (ValueError, TypeError) as exc:
        raise HumanRequired("GitHub 可读报告无法解析；需要核对机器交接块，禁止猜测修补转义。") from exc


def decode_payload(body, read_part):
    frames = list(re.finditer(r"(?m)^<!-- web-payload-v1: (.+) -->$", body))
    if not frames:
        return fenced_json(body)  # Compatibility for older, valid plain reports.
    if len(frames) != 1:
        raise HumanRequired("GitHub 报告含多个机器交接清单。")
    try:
        manifest = json.loads(frames[0].group(1))
    except ValueError as exc:
        raise HumanRequired("GitHub 机器交接清单损坏。") from exc
    if not isinstance(manifest, dict) or set(manifest) != {"parts", "sha256"}:
        raise HumanRequired("GitHub 机器交接清单字段不合法。")
    ids, digest = manifest["parts"], manifest["sha256"]
    if (not isinstance(ids, list) or not 1 <= len(ids) <= MAX_PARTS
            or any(type(i) is not int or i <= 0 for i in ids) or len(set(ids)) != len(ids)
            or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
        raise HumanRequired("GitHub 机器交接清单超出预算或缺少校验。")
    chunks = []
    for index, comment_id in enumerate(ids):
        part = fenced_json(read_part(comment_id))
        if (not isinstance(part, dict) or set(part) != {"index", "count", "sha256", "data"}
                or part["index"] != index or part["count"] != len(ids) or part["sha256"] != digest
                or not isinstance(part["data"], str) or len(part["data"]) > LEGACY_CHUNK_CHARS):
            raise HumanRequired("GitHub 机器交接分块不一致。")
        chunks.append(part["data"])
    try:
        raw = base64.b64decode("".join(chunks), validate=True)
        if len(raw) > MAX_PAYLOAD_BYTES or hashlib.sha256(raw).hexdigest() != digest:
            raise HumanRequired("GitHub 机器交接数据校验失败。")
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise HumanRequired("GitHub 机器交接数据无法解码。") from exc
    if not isinstance(payload, dict):
        raise HumanRequired("GitHub 交接结果必须是对象。")
    return payload


def text_pages(text: str, limit=18000) -> list[str]:
    """Bound UTF-8 bytes without dropping or splitting any code point."""
    pages, current, size = [], [], 0
    for character in text:
        width = len(character.encode("utf-8"))
        if size + width > limit:
            pages.append("".join(current))
            current, size = [], 0
        current.append(character)
        size += width
    if current:
        pages.append("".join(current))
    return pages


def bounded_comment(body: str) -> str:
    if len(body.encode("utf-8")) > COMMENT_BYTES:
        raise HumanRequired("单条评论超过24KB发布约束，尚未发送。")
    return body
