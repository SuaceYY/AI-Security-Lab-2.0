import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import main  # noqa: E402
import static_scan  # noqa: E402
from scan_service import (  # noqa: E402
    MAX_FINDINGS,
    MAX_SCAN_RESPONSE_BYTES,
    ScanSummary,
    ScanValidationError,
    _json_size,
    _serialized_response_size,
    run_scan,
)
from static_scan import FindingLimitExceeded, scan_text  # noqa: E402

FINDING_LINE = "eval(user_input)\n"


def scan_file(path: str, content: str) -> dict:
    return {"file_path": path, "content": content}


def source_with_findings(count: int) -> str:
    return FINDING_LINE * count


def internal_response_size(payload: dict) -> int:
    return _serialized_response_size(
        payload["task_id"],
        ScanSummary(**payload["summary"]),
        [_json_size(item) for item in payload["files"]],
        [_json_size(item) for item in payload["findings"]],
    )


def post_scan_response(files: list[dict]) -> tuple[int, dict, bytes]:
    body = json.dumps({"files": files}).encode("utf-8")
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/scans",
        "raw_path": b"/scans",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"testserver"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
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
    return (
        start["status"],
        json.loads(response_body.decode("utf-8")),
        response_body,
    )


def post_scan(files: list[dict]) -> tuple[int, dict, int]:
    status, payload, response_body = post_scan_response(files)
    return status, payload, len(response_body)


class FindingLimitTests(unittest.TestCase):
    def test_exactly_1000_findings_across_files_completes(self):
        status, payload, response_body = post_scan_response(
            [
                scan_file("src/first.py", source_with_findings(600)),
                scan_file("src/second.py", source_with_findings(400)),
            ]
        )

        self.assertEqual(status, 200)
        self.assertLessEqual(len(response_body), MAX_SCAN_RESPONSE_BYTES)
        self.assertEqual(internal_response_size(payload), len(response_body))
        self.assertEqual(payload["status"], "done")
        self.assertEqual(
            payload["summary"],
            {
                "file_count": 2,
                "scanned_count": 2,
                "skipped_count": 0,
                "finding_count": MAX_FINDINGS,
            },
        )
        self.assertEqual(
            [item["finding_count"] for item in payload["files"]],
            [600, 400],
        )
        self.assertEqual(len(payload["findings"]), MAX_FINDINGS)

    def test_1001st_finding_across_files_is_rejected(self):
        status, payload, _ = post_scan(
            [
                scan_file("src/first.py", source_with_findings(600)),
                scan_file("src/second.py", source_with_findings(401)),
            ]
        )

        self.assertEqual(status, 413)
        self.assertNotIn("status", payload)
        self.assertEqual(payload["detail"]["status"], "failed")
        self.assertEqual(payload["detail"]["code"], "too_many_findings")
        self.assertIn(str(MAX_FINDINGS), payload["detail"]["message"])

    def test_run_scan_rejects_at_first_finding_over_limit(self):
        with self.assertRaises(ScanValidationError) as caught:
            run_scan(
                [
                    ("src/first.py", source_with_findings(600)),
                    ("src/second.py", source_with_findings(401)),
                ]
            )

        self.assertEqual(caught.exception.status_code, 413)
        self.assertEqual(caught.exception.code, "too_many_findings")

    def test_scan_text_stops_before_constructing_the_1001st_finding(self):
        with patch.object(
            static_scan, "Finding", wraps=static_scan.Finding
        ) as finding_class:
            with self.assertRaises(FindingLimitExceeded) as caught:
                scan_text(
                    "src/many.py",
                    source_with_findings(5000),
                    max_findings=MAX_FINDINGS,
                )

        self.assertEqual(caught.exception.limit, MAX_FINDINGS)
        self.assertIn(f"最多允许 {MAX_FINDINGS} 条", str(caught.exception))
        self.assertEqual(finding_class.call_count, MAX_FINDINGS)

    def test_long_path_with_few_findings_is_rejected_before_scan(self):
        long_path = "a" * (MAX_SCAN_RESPONSE_BYTES + 1024) + ".py"
        with patch.object(
            static_scan, "Finding", wraps=static_scan.Finding
        ) as finding_class:
            status, payload, _ = post_scan([scan_file(long_path, FINDING_LINE)])

        self.assertEqual(status, 413)
        self.assertNotIn("status", payload)
        self.assertNotIn("findings", payload)
        self.assertEqual(payload["detail"]["status"], "failed")
        self.assertEqual(payload["detail"]["code"], "scan_output_too_large")
        self.assertIn(str(MAX_SCAN_RESPONSE_BYTES), payload["detail"]["message"])
        self.assertEqual(finding_class.call_count, 0)

    def test_repeated_long_path_findings_are_rejected(self):
        long_path = "src/" + "a" * 4096 + ".py"
        status, payload, _ = post_scan(
            [scan_file(long_path, source_with_findings(1000))]
        )

        self.assertEqual(status, 413)
        self.assertNotIn("status", payload)
        self.assertNotIn("findings", payload)
        self.assertEqual(payload["detail"]["status"], "failed")
        self.assertEqual(payload["detail"]["code"], "scan_output_too_large")

    def test_multi_file_zero_finding_skipped_and_utf8_round_trip(self):
        status, payload, response_body = post_scan_response(
            [
                scan_file("src/安全/空.py", 'print("安全")\n'),
                scan_file("src/unsupported.txt", FINDING_LINE),
                scan_file("node_modules/pkg/util.js", FINDING_LINE),
                scan_file("src/危险.js", "eval(用户输入)\n"),
            ]
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "done")
        self.assertEqual(
            payload["summary"],
            {
                "file_count": 4,
                "scanned_count": 2,
                "skipped_count": 2,
                "finding_count": 1,
            },
        )
        self.assertEqual(
            [
                (
                    item["file_path"],
                    item["status"],
                    item["finding_count"],
                    item["reason"],
                )
                for item in payload["files"]
            ],
            [
                ("src/安全/空.py", "scanned", 0, None),
                ("src/unsupported.txt", "skipped", 0, "不支持的文件类型"),
                (
                    "node_modules/pkg/util.js",
                    "skipped",
                    0,
                    "位于排除目录：node_modules",
                ),
                ("src/危险.js", "scanned", 1, None),
            ],
        )
        self.assertEqual(len(payload["findings"]), 1)
        finding = payload["findings"][0]
        self.assertEqual(finding["rule_id"], "JS-EVAL")
        self.assertEqual(finding["file_path"], "src/危险.js")
        self.assertEqual(finding["evidence"], "eval(用户输入)")
        self.assertEqual(internal_response_size(payload), len(response_body))

    def test_exactly_2mib_response_succeeds_and_one_more_byte_is_rejected(self):
        _, _, base_body = post_scan_response([scan_file("src/.py", FINDING_LINE)])
        padding = (MAX_SCAN_RESPONSE_BYTES - len(base_body)) // 2
        self.assertEqual(len(base_body) + 2 * padding, MAX_SCAN_RESPONSE_BYTES)
        path = "src/" + "a" * padding + ".py"

        status, payload, response_body = post_scan_response(
            [scan_file(path, FINDING_LINE)]
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "done")
        self.assertEqual(len(response_body), MAX_SCAN_RESPONSE_BYTES)
        self.assertEqual(internal_response_size(payload), len(response_body))

        status, payload, _ = post_scan_response(
            [scan_file(path, "eval(user_input)#\n")]
        )

        self.assertEqual(status, 413)
        self.assertNotIn("status", payload)
        self.assertNotIn("findings", payload)
        self.assertEqual(payload["detail"]["status"], "failed")
        self.assertEqual(payload["detail"]["code"], "scan_output_too_large")


if __name__ == "__main__":
    unittest.main()
