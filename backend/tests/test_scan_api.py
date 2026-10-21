import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import main  # noqa: E402
from scan_service import MAX_FILES, MAX_FILE_BYTES  # noqa: E402
from static_scan import FINDING_FIELDS  # noqa: E402


def request(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    body = b"" if payload is None else json.dumps(payload).encode("utf-8")
    parsed = urlsplit(path)
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": parsed.path,
        "raw_path": parsed.path.encode("utf-8"),
        "query_string": parsed.query.encode("utf-8"),
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

    start = next(message for message in messages if message["type"] == "http.response.start")
    response_body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    return start["status"], json.loads(response_body.decode("utf-8"))


def scan(files: list[dict]) -> tuple[int, dict]:
    return request("POST", "/scans", {"files": files})


def scan_file(path: str, content: str) -> dict:
    return {"file_path": path, "content": content}


class HealthTests(unittest.TestCase):
    def test_health_returns_ok(self):
        status, payload = request("GET", "/health")

        self.assertEqual(status, 200)
        self.assertEqual(payload, {"status": "ok"})

    def test_home_route_is_preserved(self):
        status, payload = request("GET", "/")

        self.assertEqual(status, 200)
        self.assertEqual(payload, {"message": "AI-Security-Lab 2.0 API is running"})


class ScanEndpointTests(unittest.TestCase):
    def test_normal_scan_returns_done_status_and_unified_findings(self):
        status, payload = scan(
            [
                scan_file(".\\src\\app.py", "value = eval(user_input)\n"),
                scan_file("src/view.js", "element.innerHTML = userInput\n"),
                scan_file("docs/notes.md", "eval(user_input)\n"),
            ]
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "done")
        self.assertTrue(payload["task_id"])
        self.assertEqual(
            payload["summary"],
            {
                "file_count": 3,
                "scanned_count": 2,
                "skipped_count": 1,
                "finding_count": 2,
            },
        )
        self.assertEqual(
            [item["status"] for item in payload["files"]],
            ["scanned", "scanned", "skipped"],
        )
        self.assertEqual(payload["files"][2]["reason"], "不支持的文件类型")
        self.assertEqual(
            [finding["rule_id"] for finding in payload["findings"]],
            ["PY-EVAL-EXEC", "JS-INNER-HTML"],
        )
        self.assertEqual(payload["findings"][0]["file_path"], "src/app.py")
        self.assertEqual(payload["findings"][0]["line"], 1)
        self.assertEqual(tuple(payload["findings"][0].keys()), FINDING_FIELDS)

    def test_safe_code_returns_empty_findings(self):
        status, payload = scan(
            [scan_file("src/safe.py", "def greet(name):\n    print(name)\n")]
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "done")
        self.assertEqual(payload["summary"]["scanned_count"], 1)
        self.assertEqual(payload["summary"]["finding_count"], 0)
        self.assertEqual(payload["findings"], [])

    def test_request_body_over_limit_is_rejected_before_scan(self):
        request_payload = {
            "files": [
                scan_file(
                    "src/large.py",
                    "a" * (main.MAX_SCAN_REQUEST_BODY_BYTES + 1),
                )
            ]
        }

        with patch.object(main, "run_scan") as run_scan:
            status, payload = request("POST", "/scans", request_payload)

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
        run_scan.assert_not_called()

    def test_empty_file_list_is_rejected(self):
        status, payload = scan([])

        self.assertEqual(status, 400)
        self.assertEqual(payload["detail"]["status"], "failed")
        self.assertEqual(payload["detail"]["code"], "empty_file_list")

    def test_malformed_request_uses_uniform_error_shape(self):
        status, payload = request(
            "POST",
            "/scans",
            {"files": [{"file_path": 123, "content": ""}]},
        )

        self.assertEqual(status, 422)
        self.assertEqual(payload["detail"]["status"], "failed")
        self.assertEqual(payload["detail"]["code"], "invalid_request")
        self.assertTrue(payload["detail"]["errors"])

    def test_too_many_files_is_rejected(self):
        files = [
            scan_file(f"src/file{index}.py", "")
            for index in range(MAX_FILES + 1)
        ]
        status, payload = scan(files)

        self.assertEqual(status, 413)
        self.assertEqual(payload["detail"]["status"], "failed")
        self.assertEqual(payload["detail"]["code"], "too_many_files")

    def test_file_count_at_limit_is_accepted(self):
        files = [
            scan_file(f"src/file{index}.py", "")
            for index in range(MAX_FILES)
        ]
        status, payload = scan(files)

        self.assertEqual(status, 200)
        self.assertEqual(payload["summary"]["file_count"], MAX_FILES)

    def test_single_file_over_limit_is_rejected(self):
        status, payload = scan(
            [scan_file("src/large.py", "a" * (MAX_FILE_BYTES + 1))]
        )

        self.assertEqual(status, 413)
        self.assertEqual(payload["detail"]["code"], "file_too_large")

    def test_single_file_at_limit_is_accepted(self):
        status, payload = scan(
            [scan_file("src/large.py", "a" * MAX_FILE_BYTES)]
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload["summary"]["scanned_count"], 1)

    def test_total_size_over_limit_is_rejected(self):
        files = [
            scan_file(f"src/part{index}.py", "a" * MAX_FILE_BYTES)
            for index in range(6)
        ]
        status, payload = scan(files)

        self.assertEqual(status, 413)
        self.assertEqual(payload["detail"]["code"], "total_size_too_large")

    def test_total_size_at_limit_is_accepted(self):
        files = [
            scan_file(f"src/part{index}.py", "a" * MAX_FILE_BYTES)
            for index in range(5)
        ]
        status, payload = scan(files)

        self.assertEqual(status, 200)
        self.assertEqual(payload["summary"]["scanned_count"], 5)

    def test_absolute_and_parent_paths_are_rejected(self):
        for path in ("C:\\Users\\example\\app.py", "/etc/passwd", "../outside.py"):
            with self.subTest(path=path):
                status, payload = scan([scan_file(path, "print('x')\n")])

                self.assertEqual(status, 400)
                self.assertEqual(payload["detail"]["code"], "invalid_file_path")

    def test_sensitive_files_and_directories_are_rejected(self):
        for path in (".env", "config/production.env", ".ssh/id_rsa", "src/server.pem"):
            with self.subTest(path=path):
                status, payload = scan([scan_file(path, "secret")])

                self.assertEqual(status, 400)
                self.assertEqual(payload["detail"]["status"], "failed")
                self.assertEqual(payload["detail"]["code"], "sensitive_file")

    def test_duplicate_normalized_paths_are_rejected(self):
        status, payload = scan(
            [
                scan_file("src/app.py", "print('x')\n"),
                scan_file(".\\src\\app.py", "print('y')\n"),
            ]
        )

        self.assertEqual(status, 400)
        self.assertEqual(payload["detail"]["code"], "duplicate_file_path")

    def test_unsupported_files_are_explicitly_skipped_and_counted(self):
        status, payload = scan(
            [scan_file("docs/notes.md", "eval(user_input)\n")]
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "done")
        self.assertEqual(payload["summary"]["scanned_count"], 0)
        self.assertEqual(payload["summary"]["skipped_count"], 1)
        self.assertEqual(payload["summary"]["finding_count"], 0)
        self.assertEqual(payload["files"][0]["status"], "skipped")
        self.assertEqual(payload["files"][0]["reason"], "不支持的文件类型")
        self.assertEqual(payload["findings"], [])

    def test_excluded_directories_are_skipped_with_reason(self):
        status, payload = scan(
            [scan_file("node_modules/pkg/index.js", "eval(userInput)\n")]
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload["summary"]["skipped_count"], 1)
        self.assertIn("排除目录", payload["files"][0]["reason"])
        self.assertEqual(payload["findings"], [])


if __name__ == "__main__":
    unittest.main()
