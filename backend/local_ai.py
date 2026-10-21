"""本地 Ollama Finding 优先级分析服务层。

本模块只接收既有 Finding 的受限字段，固定请求本机回环地址上的 Ollama，
不读取用户文件、不执行模型建议，也不访问其他网络位置。传输函数可注入，
测试无需启动模型服务。
"""

from __future__ import annotations

import http.client
import json
import os
import re
import socket
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping
from urllib.parse import urlsplit

try:
    from .static_scan import FINDING_FIELDS, SEVERITIES, Finding
except ImportError:
    from static_scan import (  # type: ignore[no-redef]
        FINDING_FIELDS,
        SEVERITIES,
        Finding,
    )

OLLAMA_CHAT_URL = "http://127.0.0.1:11434/api/chat"
MODEL_ENV_VAR = "AI_SECURITY_LAB_OLLAMA_MODEL"
DEFAULT_MODEL = "qwen2.5:7b-instruct"

MAX_ANALYSIS_FINDINGS = 50
MAX_PROMPT_BYTES = 48 * 1024
MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 256 * 1024
MAX_SUMMARY_BYTES = 4 * 1024
MAX_REASON_BYTES = 1024
DEFAULT_TIMEOUT_SECONDS = 10.0

PRIORITY_LEVELS = ("critical", "high", "medium", "low")
SEVERITY_RANK = {severity: index for index, severity in enumerate(SEVERITIES)}

_MODEL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_JSON_SEPARATORS = (",", ":")

SYSTEM_PROMPT = (
    "你是本地静态扫描结果的优先级分析器。你只会收到 Finding 的受限字段，"
    "没有源码，也不能读取文件或执行代码。请结合规则、严重度、路径、行号、"
    "证据、说明和建议，对每个 Finding 给出修复优先级。只输出 JSON 对象，"
    "且只能包含 summary 和 priorities 两个字段。summary 使用中文说明总体判断。"
    "priorities 必须为输入的每个 finding_index 恰好提供一项，不得重复、遗漏"
    "或引用未提供的索引；每项只能包含 finding_index、priority、reason。"
    "priority 只能为 critical、high、medium、low。reason 使用中文说明该优先级依据。"
    "不得输出 Markdown、额外 Finding、源码或任何工具调用建议。"
)

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "priorities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "finding_index": {"type": "integer", "minimum": 0},
                    "priority": {
                        "type": "string",
                        "enum": list(PRIORITY_LEVELS),
                    },
                    "reason": {"type": "string"},
                },
                "required": ["finding_index", "priority", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "priorities"],
    "additionalProperties": False,
}


class LocalAIError(RuntimeError):
    """表示本地 AI 调用失败，并携带稳定错误码与建议 HTTP 状态。"""

    def __init__(self, code: str, message: str, *, status_code: int = 502) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class FindingPriority:
    finding_index: int
    priority: str
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "finding_index": self.finding_index,
            "priority": self.priority,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class AnalysisResult:
    status: str
    model: str
    total_count: int
    analyzed_count: int
    summary: str
    priorities: tuple[FindingPriority, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "model": self.model,
            "total_count": self.total_count,
            "analyzed_count": self.analyzed_count,
            "summary": self.summary,
            "priorities": [item.to_dict() for item in self.priorities],
        }


Transport = Callable[[str, bytes, float], tuple[int, bytes]]


def _invalid_response() -> LocalAIError:
    return LocalAIError(
        "model_invalid_response",
        "本地模型返回结构不合法。",
    )


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> object:
    raise ValueError("non-standard JSON constant")


def _load_json(raw: bytes | str) -> object:
    try:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        return json.loads(
            text,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise _invalid_response() from exc


def _utf8_size(value: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise _invalid_response() from exc


def resolve_model(model: str | None = None) -> str:
    """解析并校验模型名；未设置或留空的环境变量使用清晰默认值。"""
    candidate = model if model is not None else os.environ.get(MODEL_ENV_VAR, "")
    if not isinstance(candidate, str):
        raise LocalAIError(
            "invalid_model",
            "本地模型名称必须是字符串。",
            status_code=500,
        )

    candidate = candidate.strip()
    if not candidate and model is None:
        candidate = DEFAULT_MODEL
    if not _MODEL_NAME_PATTERN.fullmatch(candidate):
        raise LocalAIError(
            "invalid_model",
            "本地模型名称不合法。",
            status_code=500,
        )
    return candidate


def _normalize_finding(item: Finding | Mapping[str, object], index: int) -> dict[str, object]:
    if isinstance(item, Finding):
        data = item.to_dict()
    elif isinstance(item, Mapping):
        missing = [field for field in FINDING_FIELDS if field not in item]
        if missing:
            raise LocalAIError(
                "invalid_finding",
                f"第 {index + 1} 条 Finding 缺少必要字段。",
                status_code=400,
            )
        data = {field: item[field] for field in FINDING_FIELDS}
    else:
        raise LocalAIError(
            "invalid_finding",
            f"第 {index + 1} 条 Finding 类型不合法。",
            status_code=400,
        )

    for field in (
        "rule_id",
        "severity",
        "file_path",
        "evidence",
        "explanation",
        "recommendation",
    ):
        value = data[field]
        if not isinstance(value, str):
            raise LocalAIError(
                "invalid_finding",
                f"第 {index + 1} 条 Finding 的 {field} 必须是字符串。",
                status_code=400,
            )
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise LocalAIError(
                "invalid_finding",
                f"第 {index + 1} 条 Finding 的 {field} 不是合法 UTF-8。",
                status_code=400,
            ) from exc

    if not str(data["rule_id"]).strip() or not str(data["file_path"]).strip():
        raise LocalAIError(
            "invalid_finding",
            f"第 {index + 1} 条 Finding 的规则或路径为空。",
            status_code=400,
        )
    if data["severity"] not in SEVERITIES:
        raise LocalAIError(
            "invalid_finding",
            f"第 {index + 1} 条 Finding 的严重度不合法。",
            status_code=400,
        )

    line = data["line"]
    if isinstance(line, bool) or not isinstance(line, int) or line < 1:
        raise LocalAIError(
            "invalid_finding",
            f"第 {index + 1} 条 Finding 的行号不合法。",
            status_code=400,
        )

    return data


def _select_findings(
    findings: list[dict[str, object]],
) -> list[tuple[int, dict[str, object]]]:
    indexed = list(enumerate(findings))
    indexed.sort(key=lambda item: (SEVERITY_RANK[str(item[1]["severity"])], item[0]))
    return indexed[:MAX_ANALYSIS_FINDINGS]


def _build_request_body(
    model: str,
    selected: list[tuple[int, dict[str, object]]],
) -> bytes:
    user_payload = {
        "priority_levels": list(PRIORITY_LEVELS),
        "findings": [
            {"finding_index": index, **data}
            for index, data in selected
        ],
    }
    user_prompt = json.dumps(
        user_payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=_JSON_SEPARATORS,
    )
    prompt_bytes = _utf8_size(SYSTEM_PROMPT) + _utf8_size(user_prompt)
    if prompt_bytes > MAX_PROMPT_BYTES:
        raise LocalAIError(
            "analysis_request_too_large",
            f"分析提示词超过上限 {MAX_PROMPT_BYTES} 字节。",
            status_code=413,
        )

    request_payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "format": RESPONSE_SCHEMA,
        "stream": False,
        "think": False,
        "options": {"temperature": 0},
    }
    body = json.dumps(
        request_payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=_JSON_SEPARATORS,
    ).encode("utf-8")
    if len(body) > MAX_REQUEST_BYTES:
        raise LocalAIError(
            "analysis_request_too_large",
            f"分析请求超过上限 {MAX_REQUEST_BYTES} 字节。",
            status_code=413,
        )
    return body


def _extract_model_content(body: bytes) -> str:
    wrapper = _load_json(body)
    if not isinstance(wrapper, dict):
        raise _invalid_response()
    message = wrapper.get("message")
    if not isinstance(message, dict):
        raise _invalid_response()
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise _invalid_response()
    return content


def _parse_analysis_content(
    content: str,
    allowed_indices: frozenset[int],
) -> tuple[str, tuple[FindingPriority, ...]]:
    payload = _load_json(content)
    if not isinstance(payload, dict) or set(payload) != {"summary", "priorities"}:
        raise _invalid_response()

    summary = payload["summary"]
    if not isinstance(summary, str) or not summary.strip():
        raise _invalid_response()
    if _utf8_size(summary) > MAX_SUMMARY_BYTES:
        raise LocalAIError(
            "model_invalid_response",
            f"本地模型摘要超过上限 {MAX_SUMMARY_BYTES} 字节。",
        )

    raw_priorities = payload["priorities"]
    if not isinstance(raw_priorities, list) or len(raw_priorities) != len(allowed_indices):
        raise _invalid_response()

    priorities: list[FindingPriority] = []
    seen: set[int] = set()
    for raw in raw_priorities:
        if not isinstance(raw, dict) or set(raw) != {
            "finding_index",
            "priority",
            "reason",
        }:
            raise _invalid_response()

        finding_index = raw["finding_index"]
        if (
            isinstance(finding_index, bool)
            or not isinstance(finding_index, int)
            or finding_index not in allowed_indices
        ):
            raise LocalAIError(
                "model_invalid_response",
                "本地模型引用了不存在或未分析的 Finding。",
            )
        if finding_index in seen:
            raise LocalAIError(
                "model_invalid_response",
                "本地模型重复引用了同一个 Finding。",
            )

        priority = raw["priority"]
        if priority not in PRIORITY_LEVELS:
            raise _invalid_response()

        reason = raw["reason"]
        if not isinstance(reason, str) or not reason.strip():
            raise _invalid_response()
        if _utf8_size(reason) > MAX_REASON_BYTES:
            raise LocalAIError(
                "model_invalid_response",
                f"本地模型优先级理由超过上限 {MAX_REASON_BYTES} 字节。",
            )

        seen.add(finding_index)
        priorities.append(
            FindingPriority(
                finding_index=finding_index,
                priority=str(priority),
                reason=reason,
            )
        )

    if seen != allowed_indices:
        raise _invalid_response()

    return summary, tuple(priorities)


def _default_transport(url: str, body: bytes, timeout: float) -> tuple[int, bytes]:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.port != 11434
        or parsed.path != "/api/chat"
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise LocalAIError(
            "model_endpoint_invalid",
            "本地模型地址必须固定为回环地址。",
            status_code=500,
        )

    connection = http.client.HTTPConnection("127.0.0.1", 11434, timeout=timeout)
    try:
        connection.request(
            "POST",
            "/api/chat",
            body=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        response = connection.getresponse()
        response_body = response.read(MAX_RESPONSE_BYTES + 1)
        return response.status, response_body
    except (TimeoutError, socket.timeout) as exc:
        raise LocalAIError(
            "model_timeout",
            "本地模型请求超时。",
            status_code=504,
        ) from exc
    except OSError as exc:
        raise LocalAIError(
            "model_unavailable",
            "无法连接本地模型服务，请确认 Ollama 已在本机启动。",
            status_code=503,
        ) from exc
    except http.client.HTTPException as exc:
        raise LocalAIError(
            "model_unavailable",
            "本地模型连接异常。",
            status_code=503,
        ) from exc
    finally:
        connection.close()


def analyze_findings(
    findings: Iterable[Finding | Mapping[str, object]],
    *,
    transport: Transport | None = None,
    model: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> AnalysisResult:
    """分析最多 50 条 Finding，返回严格校验后的摘要与优先级。"""
    resolved_model = resolve_model(model)
    normalized = [
        _normalize_finding(item, index)
        for index, item in enumerate(findings)
    ]
    total_count = len(normalized)
    if total_count == 0:
        return AnalysisResult(
            status="done",
            model=resolved_model,
            total_count=0,
            analyzed_count=0,
            summary="没有可分析的 Finding。",
            priorities=(),
        )

    selected = _select_findings(normalized)
    request_body = _build_request_body(resolved_model, selected)
    active_transport = transport or _default_transport

    try:
        status, response_body = active_transport(
            OLLAMA_CHAT_URL,
            request_body,
            timeout,
        )
    except LocalAIError:
        raise
    except (TimeoutError, socket.timeout) as exc:
        raise LocalAIError(
            "model_timeout",
            "本地模型请求超时。",
            status_code=504,
        ) from exc
    except OSError as exc:
        raise LocalAIError(
            "model_unavailable",
            "无法连接本地模型服务，请确认 Ollama 已在本机启动。",
            status_code=503,
        ) from exc

    if not isinstance(status, int) or isinstance(status, bool):
        raise _invalid_response()
    if not 200 <= status < 300:
        raise LocalAIError(
            "model_http_error",
            f"本地模型返回非成功状态 {status}。",
        )

    try:
        response_bytes = bytes(response_body)
    except TypeError as exc:
        raise _invalid_response() from exc
    if len(response_bytes) > MAX_RESPONSE_BYTES:
        raise LocalAIError(
            "model_response_too_large",
            f"本地模型响应超过上限 {MAX_RESPONSE_BYTES} 字节。",
        )

    content = _extract_model_content(response_bytes)
    allowed_indices = frozenset(index for index, _data in selected)
    summary, priorities = _parse_analysis_content(content, allowed_indices)

    return AnalysisResult(
        status="done",
        model=resolved_model,
        total_count=total_count,
        analyzed_count=len(selected),
        summary=summary,
        priorities=priorities,
    )
