import asyncio
import json
import os
import sys
import unittest
from functools import partial
from pathlib import Path
from unittest.mock import patch

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import main  # noqa: E402
from local_ai import (  # noqa: E402
    DEFAULT_MODEL,
    MAX_ANALYSIS_FINDINGS,
    MAX_PROMPT_BYTES,
    MAX_REASON_BYTES,
    MAX_RESPONSE_BYTES,
    MAX_SUMMARY_BYTES,
    MODEL_ENV_VAR,
    OLLAMA_CHAT_URL,
    PRIORITY_LEVELS,
    RESPONSE_SCHEMA,
    LocalAIError,
    analyze_findings,
    resolve_model,
)
from static_scan import FINDING_FIELDS, Finding  # noqa: E402


def finding(
    index: int = 1,
    *,
    severity: str = "high",
    rule_id: str = "PY-EVAL-EXEC",
    evidence: str = "value = eval(user_input)",
) -> Finding:
    return Finding(
        rule_id=rule_id,
        severity=severity,
        file_path=f"src/file{index}.py",
        line=index,
        evidence=evidence,
        explanation="检测到动态执行字符串的调用模式。",
        recommendation="避免使用 eval，改用显式解析。",
    )


class FakeTransport:
    def __init__(
        self,
        responder=None,
        *,
        response_body: bytes | None = None,
        status: int = 200,
        error: Exception | None = None,
    ) -> None:
        self.responder = responder
        self.response_body = response_body
        self.status = status
        self.error = error
        self.calls: list[dict[str, object]] = []

    def __call__(self, url: str, body: bytes, timeout: float) -> tuple[int, bytes]:
        self.calls.append({"url": url, "body": body, "timeout": timeout})
        if self.error is not None:
            raise self.error
        if self.responder is not None:
            return self.responder(body)
        if self.response_body is None:
            raise AssertionError("测试假传输缺少响应")
        return self.status, self.response_body


def prompt_findings(body: bytes) -> list[dict[str, object]]:
    request_payload = json.loads(body.decode("utf-8"))
    user_payload = json.loads(request_payload["messages"][1]["content"])
    return user_payload["findings"]


def chat_response(payload: object) -> bytes:
    content = json.dumps(payload, ensure_ascii=False)
    return chat_content_response(content)


def chat_content_response(content: str) -> bytes:
    wrapper = {
        "model": "fake-model",
        "message": {"role": "assistant", "content": content},
        "done": True,
    }
    return json.dumps(wrapper, ensure_ascii=False).encode("utf-8")


def valid_responder(
    *,
    summary: str = "按现有证据优先处理高风险 Finding。",
    priority: str = "high",
    reason: str = "规则严重度高，且证据显示直接执行动态内容。",
):
    def respond(body: bytes) -> tuple[int, bytes]:
        priorities = [
            {
                "finding_index": item["finding_index"],
                "priority": priority,
                "reason": reason,
            }
            for item in prompt_findings(body)
        ]
        return 200, chat_response({"summary": summary, "priorities": priorities})

    return respond


def asgi_post(
    path: str,
    payload: dict | None = None,
    *,
    body: bytes | None = None,
    content_length: int | None = None,
) -> tuple[int, dict]:
    if body is None:
        body = b"" if payload is None else json.dumps(
            payload, ensure_ascii=False
        ).encode("utf-8")
    length = len(body) if content_length is None else content_length
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"testserver"),
            (b"content-type", b"application/json"),
            (b"content-length", str(length).encode("ascii")),
        ],
        "server": ("testserver", 80),
        "client": ("testclient", 12345),
        "state": {},
    }
    messages: list[dict] = []
    request_sent = False

    async def receive() -> dict:
        nonlocal request_sent
        if request_sent:
            return {"type": "http.disconnect"}
        request_sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict) -> None:
        messages.append(message)

    asyncio.run(main.app(scope, receive, send))

    start = next(
        message for message in messages if message["type"] == "http.response.start"
    )
    response_body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    return start["status"], json.loads(response_body.decode("utf-8"))


class ModelConfigTests(unittest.TestCase):
    def test_reads_model_from_environment(self):
        with patch.dict(os.environ, {MODEL_ENV_VAR: "llama3.2:3b"}, clear=False):
            self.assertEqual(resolve_model(), "llama3.2:3b")

    def test_blank_environment_uses_clear_default(self):
        with patch.dict(os.environ, {MODEL_ENV_VAR: "   "}, clear=False):
            self.assertEqual(resolve_model(), DEFAULT_MODEL)

    def test_invalid_environment_model_fails_without_transport(self):
        transport = FakeTransport(responder=valid_responder())
        with patch.dict(os.environ, {MODEL_ENV_VAR: "bad model\n"}, clear=False):
            with self.assertRaises(LocalAIError) as caught:
                analyze_findings([finding()], transport=transport)

        self.assertEqual(caught.exception.code, "invalid_model")
        self.assertEqual(caught.exception.status_code, 500)
        self.assertEqual(transport.calls, [])


class RequestContractTests(unittest.TestCase):
    def test_builds_fixed_ollama_chat_request_with_whitelisted_fields(self):
        data = finding(1).to_dict()
        data["content"] = "SECRET_SOURCE_SENTINEL"
        data["source"] = "SECRET_EXTRA_FIELD"
        transport = FakeTransport(responder=valid_responder())

        with patch.dict(os.environ, {MODEL_ENV_VAR: ""}, clear=False):
            result = analyze_findings([data], transport=transport)

        self.assertEqual(len(transport.calls), 1)
        call = transport.calls[0]
        self.assertEqual(call["url"], OLLAMA_CHAT_URL)
        self.assertEqual(call["timeout"], 10.0)

        request_body = call["body"]
        self.assertIsInstance(request_body, bytes)
        request_text = request_body.decode("utf-8")
        self.assertNotIn("SECRET_SOURCE_SENTINEL", request_text)
        self.assertNotIn("SECRET_EXTRA_FIELD", request_text)

        request_payload = json.loads(request_text)
        self.assertEqual(request_payload["model"], DEFAULT_MODEL)
        self.assertFalse(request_payload["stream"])
        self.assertFalse(request_payload["think"])
        self.assertEqual(request_payload["format"], RESPONSE_SCHEMA)
        self.assertEqual(request_payload["options"], {"temperature": 0})
        self.assertEqual(request_payload["messages"][0]["role"], "system")
        self.assertIn("只输出 JSON", request_payload["messages"][0]["content"])

        user_payload = json.loads(request_payload["messages"][1]["content"])
        self.assertEqual(user_payload["priority_levels"], list(PRIORITY_LEVELS))
        self.assertEqual(len(user_payload["findings"]), 1)
        entry = user_payload["findings"][0]
        self.assertEqual(
            set(entry),
            set(FINDING_FIELDS) | {"finding_index"},
        )
        self.assertEqual(entry["finding_index"], 0)
        self.assertEqual(entry["file_path"], "src/file1.py")
        self.assertEqual(result.model, DEFAULT_MODEL)
        self.assertEqual(result.to_dict()["status"], "done")

    def test_stably_sorts_by_severity_and_analyzes_at_most_limit(self):
        findings = []
        for index in range(60):
            if index < 25:
                severity = "low"
            elif index < 45:
                severity = "medium"
            elif index < 55:
                severity = "high"
            else:
                severity = "info"
            findings.append(finding(index + 1, severity=severity))

        transport = FakeTransport(responder=valid_responder())
        result = analyze_findings(findings, transport=transport)

        expected_indices = list(range(45, 55)) + list(range(25, 45)) + list(range(0, 20))
        actual_indices = [
            item["finding_index"] for item in prompt_findings(transport.calls[0]["body"])
        ]
        self.assertEqual(actual_indices, expected_indices)
        self.assertEqual(len(actual_indices), MAX_ANALYSIS_FINDINGS)
        self.assertEqual(result.total_count, 60)
        self.assertEqual(result.analyzed_count, MAX_ANALYSIS_FINDINGS)
        self.assertEqual(len(result.priorities), MAX_ANALYSIS_FINDINGS)
        self.assertEqual(
            [item.finding_index for item in result.priorities],
            expected_indices,
        )

    def test_rejects_oversized_prompt_before_transport(self):
        transport = FakeTransport(responder=valid_responder())
        oversized = finding(1, evidence="a" * MAX_PROMPT_BYTES)

        with self.assertRaises(LocalAIError) as caught:
            analyze_findings([oversized], transport=transport)

        self.assertEqual(caught.exception.code, "analysis_request_too_large")
        self.assertEqual(caught.exception.status_code, 413)
        self.assertEqual(transport.calls, [])

    def test_missing_finding_field_is_rejected_before_transport(self):
        transport = FakeTransport(responder=valid_responder())
        payload = finding(1).to_dict()
        del payload["file_path"]

        with self.assertRaises(LocalAIError) as caught:
            analyze_findings([payload], transport=transport)

        self.assertEqual(caught.exception.code, "invalid_finding")
        self.assertEqual(transport.calls, [])

    def test_zero_findings_returns_deterministic_result_without_model_call(self):
        transport = FakeTransport(error=AssertionError("不应调用传输"))

        with patch.dict(os.environ, {MODEL_ENV_VAR: ""}, clear=False):
            result = analyze_findings([], transport=transport)

        self.assertEqual(result.to_dict(), {
            "status": "done",
            "model": DEFAULT_MODEL,
            "total_count": 0,
            "analyzed_count": 0,
            "summary": "没有可分析的 Finding。",
            "priorities": [],
        })
        self.assertEqual(transport.calls, [])

    def test_injected_transport_means_no_network_connection_is_opened(self):
        transport = FakeTransport(responder=valid_responder())

        with patch(
            "local_ai.http.client.HTTPConnection",
            side_effect=AssertionError("不应访问真实网络"),
        ):
            result = analyze_findings([finding()], transport=transport)

        self.assertEqual(result.status, "done")
        self.assertEqual(len(transport.calls), 1)


class ResponseValidationTests(unittest.TestCase):
    def analyze_with_content(self, content: str, findings=None):
        transport = FakeTransport(response_body=chat_content_response(content))
        return analyze_findings(findings or [finding()], transport=transport)

    def assert_invalid_response(self, content: str, findings=None):
        with self.assertRaises(LocalAIError) as caught:
            self.analyze_with_content(content, findings)
        self.assertEqual(caught.exception.code, "model_invalid_response")
        return caught.exception

    def test_rejects_non_json_content(self):
        self.assert_invalid_response("not-json")

    def test_rejects_missing_summary(self):
        self.assert_invalid_response(
            json.dumps(
                {
                    "priorities": [
                        {
                            "finding_index": 0,
                            "priority": "high",
                            "reason": "理由",
                        }
                    ]
                },
                ensure_ascii=False,
            )
        )

    def test_rejects_unknown_finding_reference(self):
        self.assert_invalid_response(
            json.dumps(
                {
                    "summary": "摘要",
                    "priorities": [
                        {
                            "finding_index": 1,
                            "priority": "high",
                            "reason": "理由",
                        }
                    ],
                },
                ensure_ascii=False,
            )
        )

    def test_rejects_duplicate_finding_reference(self):
        content = json.dumps(
            {
                "summary": "摘要",
                "priorities": [
                    {"finding_index": 0, "priority": "high", "reason": "理由一"},
                    {"finding_index": 0, "priority": "high", "reason": "理由二"},
                ],
            },
            ensure_ascii=False,
        )
        error = self.assert_invalid_response(content, [finding(1), finding(2)])
        self.assertIn("重复", error.message)

    def test_rejects_missing_priority_entry(self):
        self.assert_invalid_response(
            json.dumps(
                {
                    "summary": "摘要",
                    "priorities": [
                        {
                            "finding_index": 0,
                            "priority": "high",
                            "reason": "理由",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            [finding(1), finding(2)],
        )

    def test_rejects_extra_top_level_finding_injection(self):
        self.assert_invalid_response(
            json.dumps(
                {
                    "summary": "摘要",
                    "priorities": [
                        {
                            "finding_index": 0,
                            "priority": "high",
                            "reason": "理由",
                        }
                    ],
                    "findings": [{"rule_id": "INJECTED"}],
                },
                ensure_ascii=False,
            )
        )

    def test_rejects_extra_priority_field(self):
        self.assert_invalid_response(
            json.dumps(
                {
                    "summary": "摘要",
                    "priorities": [
                        {
                            "finding_index": 0,
                            "priority": "high",
                            "reason": "理由",
                            "file_path": "injected.py",
                        }
                    ],
                },
                ensure_ascii=False,
            )
        )

    def test_rejects_overlong_utf8_summary(self):
        summary = "中" * MAX_SUMMARY_BYTES
        content = json.dumps(
            {
                "summary": summary,
                "priorities": [
                    {"finding_index": 0, "priority": "high", "reason": "理由"}
                ],
            },
            ensure_ascii=False,
        )
        error = self.assert_invalid_response(content)
        self.assertIn("摘要", error.message)

    def test_rejects_overlong_utf8_reason(self):
        reason = "中" * MAX_REASON_BYTES
        content = json.dumps(
            {
                "summary": "摘要",
                "priorities": [
                    {"finding_index": 0, "priority": "high", "reason": reason}
                ],
            },
            ensure_ascii=False,
        )
        error = self.assert_invalid_response(content)
        self.assertIn("理由", error.message)

    def test_rejects_invalid_priority(self):
        self.assert_invalid_response(
            json.dumps(
                {
                    "summary": "摘要",
                    "priorities": [
                        {
                            "finding_index": 0,
                            "priority": "urgent",
                            "reason": "理由",
                        }
                    ],
                },
                ensure_ascii=False,
            )
        )


class TransportErrorTests(unittest.TestCase):
    def test_non_2xx_status_is_reported_without_upstream_body(self):
        transport = FakeTransport(status=500, response_body=b"SECRET_UPSTREAM_BODY")

        with self.assertRaises(LocalAIError) as caught:
            analyze_findings([finding()], transport=transport)

        error = caught.exception
        self.assertEqual(error.code, "model_http_error")
        self.assertEqual(error.status_code, 502)
        self.assertIn("500", error.message)
        self.assertNotIn("SECRET_UPSTREAM_BODY", error.message)

    def test_timeout_is_mapped_to_stable_error(self):
        transport = FakeTransport(error=TimeoutError("slow"))

        with self.assertRaises(LocalAIError) as caught:
            analyze_findings([finding()], transport=transport)

        self.assertEqual(caught.exception.code, "model_timeout")
        self.assertEqual(caught.exception.status_code, 504)

    def test_connection_failure_is_mapped_to_stable_error(self):
        transport = FakeTransport(error=ConnectionRefusedError("offline"))

        with self.assertRaises(LocalAIError) as caught:
            analyze_findings([finding()], transport=transport)

        self.assertEqual(caught.exception.code, "model_unavailable")
        self.assertEqual(caught.exception.status_code, 503)

    def test_oversized_response_is_rejected_before_parsing(self):
        transport = FakeTransport(
            response_body=b"x" * (MAX_RESPONSE_BYTES + 1),
        )

        with self.assertRaises(LocalAIError) as caught:
            analyze_findings([finding()], transport=transport)

        self.assertEqual(caught.exception.code, "model_response_too_large")
        self.assertEqual(caught.exception.status_code, 502)

    def test_invalid_ollama_wrapper_is_rejected(self):
        transport = FakeTransport(response_body=b"{}")

        with self.assertRaises(LocalAIError) as caught:
            analyze_findings([finding()], transport=transport)

        self.assertEqual(caught.exception.code, "model_invalid_response")


class AnalysisEndpointTests(unittest.TestCase):
    def post_with_transport(self, transport, findings=None):
        if findings is None:
            findings = [finding().to_dict()]
        with (
            patch.dict(os.environ, {MODEL_ENV_VAR: ""}, clear=False),
            patch.object(
                main,
                "analyze_findings",
                partial(analyze_findings, transport=transport),
            ),
        ):
            return asgi_post("/analyses", {"findings": findings})

    def test_asgi_success_uses_injected_transport_and_returns_contract(self):
        transport = FakeTransport(responder=valid_responder())
        status, payload = self.post_with_transport(transport)

        self.assertEqual(status, 200)
        self.assertEqual(
            set(payload),
            {
                "status",
                "model",
                "total_count",
                "analyzed_count",
                "summary",
                "priorities",
            },
        )
        self.assertEqual(payload["status"], "done")
        self.assertEqual(payload["model"], DEFAULT_MODEL)
        self.assertEqual(payload["total_count"], 1)
        self.assertEqual(payload["analyzed_count"], 1)
        self.assertEqual(payload["summary"], "按现有证据优先处理高风险 Finding。")
        self.assertEqual(
            payload["priorities"],
            [
                {
                    "finding_index": 0,
                    "priority": "high",
                    "reason": "规则严重度高，且证据显示直接执行动态内容。",
                }
            ],
        )
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(transport.calls[0]["url"], OLLAMA_CHAT_URL)
        prompted = prompt_findings(transport.calls[0]["body"])
        self.assertEqual(prompted[0]["file_path"], "src/file1.py")

    def test_asgi_zero_findings_is_deterministic_without_transport(self):
        transport = FakeTransport(error=AssertionError("不应调用传输"))

        status, payload = self.post_with_transport(transport, [])

        self.assertEqual(status, 200)
        self.assertEqual(payload, {
            "status": "done",
            "model": DEFAULT_MODEL,
            "total_count": 0,
            "analyzed_count": 0,
            "summary": "没有可分析的 Finding。",
            "priorities": [],
        })
        self.assertEqual(transport.calls, [])

    def test_asgi_more_than_50_selects_by_severity_and_reports_counts(self):
        findings = []
        for index in range(60):
            if index < 25:
                severity = "low"
            elif index < 45:
                severity = "medium"
            elif index < 55:
                severity = "high"
            else:
                severity = "info"
            findings.append(finding(index + 1, severity=severity).to_dict())

        expected_indices = (
            list(range(45, 55))
            + list(range(25, 45))
            + list(range(0, 20))
        )
        transport = FakeTransport(responder=valid_responder())
        status, payload = self.post_with_transport(transport, findings)

        self.assertEqual(status, 200)
        self.assertEqual(payload["total_count"], 60)
        self.assertEqual(payload["analyzed_count"], MAX_ANALYSIS_FINDINGS)
        self.assertEqual(
            [item["finding_index"] for item in payload["priorities"]],
            expected_indices,
        )
        prompted = prompt_findings(transport.calls[0]["body"])
        self.assertEqual(
            [item["finding_index"] for item in prompted],
            expected_indices,
        )

    def test_accepts_exact_utf8_byte_boundaries_with_chinese_path(self):
        summary = "中" * ((MAX_SUMMARY_BYTES - 1) // 3) + "a"
        reason = "中" * ((MAX_REASON_BYTES - 1) // 3) + "a"
        self.assertEqual(len(summary.encode("utf-8")), MAX_SUMMARY_BYTES)
        self.assertEqual(len(reason.encode("utf-8")), MAX_REASON_BYTES)

        data = finding(1).to_dict()
        data["file_path"] = "目录/模块/文件.py"
        transport = FakeTransport(
            response_body=chat_response({
                "summary": summary,
                "priorities": [
                    {
                        "finding_index": 0,
                        "priority": "high",
                        "reason": reason,
                    }
                ],
            })
        )

        with patch.dict(os.environ, {MODEL_ENV_VAR: ""}, clear=False):
            result = analyze_findings([data], transport=transport)

        self.assertEqual(result.summary, summary)
        self.assertEqual(result.priorities[0].reason, reason)
        prompted = prompt_findings(transport.calls[0]["body"])
        self.assertEqual(prompted[0]["file_path"], "目录/模块/文件.py")

    def test_asgi_rejects_utf8_path_over_prompt_byte_limit(self):
        data = finding(1).to_dict()
        data["file_path"] = "目录/" + "中" * (MAX_PROMPT_BYTES // 3 + 2)
        transport = FakeTransport(responder=valid_responder())

        status, payload = self.post_with_transport(transport, [data])

        self.assertEqual(status, 413)
        self.assertEqual(payload["detail"]["code"], "analysis_request_too_large")
        self.assertEqual(transport.calls, [])

    def test_asgi_rejects_injected_or_overlong_model_output(self):
        valid_priority = {
            "finding_index": 0,
            "priority": "high",
            "reason": "理由",
        }
        cases = [
            (
                "extra_finding",
                {
                    "summary": "摘要",
                    "priorities": [valid_priority],
                    "findings": [{"rule_id": "INJECTED_FINDING"}],
                },
                "INJECTED_FINDING",
            ),
            (
                "extra_priority_field",
                {
                    "summary": "摘要",
                    "priorities": [
                        {**valid_priority, "file_path": "INJECTED_PRIORITY"}
                    ],
                },
                "INJECTED_PRIORITY",
            ),
            (
                "overlong_summary",
                {
                    "summary": "中" * ((MAX_SUMMARY_BYTES - 1) // 3) + "aa",
                    "priorities": [valid_priority],
                },
                "",
            ),
            (
                "overlong_reason",
                {
                    "summary": "摘要",
                    "priorities": [
                        {
                            **valid_priority,
                            "reason": "中" * ((MAX_REASON_BYTES - 1) // 3) + "aa",
                        }
                    ],
                },
                "",
            ),
        ]

        for name, content, sentinel in cases:
            with self.subTest(name=name):
                transport = FakeTransport(response_body=chat_response(content))
                status, payload = self.post_with_transport(transport)

                self.assertEqual(status, 502)
                self.assertEqual(payload["detail"]["code"], "model_invalid_response")
                self.assertNotIn("priorities", payload)
                if sentinel:
                    self.assertNotIn(
                        sentinel,
                        json.dumps(payload, ensure_ascii=False),
                    )

    def test_asgi_maps_local_ai_failures_to_stable_errors(self):
        cases = [
            ("timeout", FakeTransport(error=TimeoutError("slow")), 504, "model_timeout"),
            (
                "unavailable",
                FakeTransport(error=ConnectionRefusedError("offline")),
                503,
                "model_unavailable",
            ),
            (
                "upstream",
                FakeTransport(status=500, response_body=b"SECRET_UPSTREAM_BODY"),
                502,
                "model_http_error",
            ),
            (
                "response_too_large",
                FakeTransport(response_body=b"x" * (MAX_RESPONSE_BYTES + 1)),
                502,
                "model_response_too_large",
            ),
            (
                "invalid_response",
                FakeTransport(response_body=b"{}"),
                502,
                "model_invalid_response",
            ),
        ]

        for name, transport, expected_status, expected_code in cases:
            with self.subTest(name=name):
                status, payload = self.post_with_transport(transport)

                self.assertEqual(status, expected_status)
                self.assertEqual(payload["detail"]["status"], "failed")
                self.assertEqual(payload["detail"]["code"], expected_code)
                self.assertTrue(payload["detail"]["message"])
                self.assertNotIn(
                    "SECRET_UPSTREAM_BODY",
                    json.dumps(payload, ensure_ascii=False),
                )

    def test_asgi_maps_oversized_prompt_without_calling_transport(self):
        transport = FakeTransport(responder=valid_responder())
        oversized = finding(evidence="a" * MAX_PROMPT_BYTES).to_dict()

        status, payload = self.post_with_transport(transport, [oversized])

        self.assertEqual(status, 413)
        self.assertEqual(payload["detail"]["status"], "failed")
        self.assertEqual(payload["detail"]["code"], "analysis_request_too_large")
        self.assertEqual(transport.calls, [])

    def test_asgi_invalid_model_fails_before_transport(self):
        transport = FakeTransport(responder=valid_responder())

        with (
            patch.dict(
                os.environ,
                {MODEL_ENV_VAR: "bad model\n"},
                clear=False,
            ),
            patch.object(
                main,
                "analyze_findings",
                partial(analyze_findings, transport=transport),
            ),
        ):
            status, payload = asgi_post(
                "/analyses",
                {"findings": [finding().to_dict()]},
            )

        self.assertEqual(status, 500)
        self.assertEqual(payload["detail"]["status"], "failed")
        self.assertEqual(payload["detail"]["code"], "invalid_model")
        self.assertEqual(transport.calls, [])

    def test_analyses_is_protected_by_request_body_limit(self):
        body = b" " * (main.MAX_SCAN_REQUEST_BODY_BYTES + 1)

        with patch.object(main, "analyze_findings") as analyze:
            status, payload = asgi_post("/analyses", body=body)

        self.assertEqual(status, 413)
        self.assertEqual(
            payload,
            {
                "detail": {
                    "status": "failed",
                    "code": "request_too_large",
                    "message": main.REQUEST_TOO_LARGE_MESSAGE,
                }
            },
        )
        analyze.assert_not_called()


if __name__ == "__main__":
    unittest.main()
