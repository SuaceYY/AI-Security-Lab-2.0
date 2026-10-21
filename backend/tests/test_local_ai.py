import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

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


if __name__ == "__main__":
    unittest.main()
