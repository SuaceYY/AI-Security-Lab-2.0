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
from scan_service import MAX_FINDINGS, ScanValidationError, run_scan  # noqa: E402
from static_scan import FindingLimitExceeded, scan_text  # noqa: E402

FINDING_LINE = "eval(user_input)\n"


def scan_file(path: str, content: str) -> dict:
    return {"file_path": path, "content": content}


def source_with_findings(count: int) -> str:
    return FINDING_LINE * count


def post_scan(files: list[dict]) -> tuple[int, dict]:
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
    return start["status"], json.loads(response_body.decode("utf-8"))


class FindingLimitTests(unittest.TestCase):
    def test_exactly_1000_findings_across_files_completes(self):
        status, payload = post_scan(
            [
                scan_file("src/first.py", source_with_findings(600)),
                scan_file("src/second.py", source_with_findings(400)),
            ]
        )

        self.assertEqual(status, 200)
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
        status, payload = post_scan(
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
        self.assertEqual(finding_class.call_count, MAX_FINDINGS)


if __name__ == "__main__":
    unittest.main()
