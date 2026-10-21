"""Nested JSON, Windows paths and ANSI logs must survive CLI display filtering."""
import hashlib
import json
import unittest

from tools.dual_ai.web_batch import HumanRequired
from tools.dual_ai.web_handoff import (
    encode_payload, payload_frame, decode_payload, FENCE, text_pages, bounded_comment,
)


def fenced(data):
    return FENCE + "json\n" + json.dumps(data, ensure_ascii=False) + "\n" + FENCE


class HandoffTests(unittest.TestCase):
    def envelope(self, data):
        serialized = json.dumps(data, ensure_ascii=False, indent=2)
        chunks, digest = encode_payload(serialized)
        ids = list(range(100, 100 + len(chunks)))
        parts = {ref: fenced({"index": index, "count": len(chunks), "sha256": digest, "data": chunk})
                 for index, (ref, chunk) in enumerate(zip(ids, chunks))}
        visible = FENCE + "json\n" + serialized + "\n" + FENCE
        return payload_frame(ids, digest) + "\n" + visible, parts

    def test_nested_ansi_and_windows_paths_survive_cli_caret_replacement(self):
        data = {"output": "\x1b[36mvite\x1b[39m", "command": "C:\\Program Files\\nodejs\\npm.CMD",
                "source": {"code": 'nul = "\\x00"\n', "unicode": "中文"}}
        body, parts = self.envelope(data)
        # gh/go-gh replaces escaped control codes in JSON string content.
        # Its outer JSON remains valid, while this nested pretty JSON can break.
        damaged = body.replace("\\u001b", "\\^[")
        with self.assertRaises(ValueError):
            json.loads(damaged[damaged.index(FENCE + "json\n") + 8:-4])
        self.assertEqual(decode_payload(damaged, parts.__getitem__), data)

    def test_multiple_chunks_reassemble_and_verify(self):
        data = {"text": "中" * 15000}
        body, parts = self.envelope(data)
        self.assertEqual(len(parts), 4)
        self.assertEqual(decode_payload(body, parts.__getitem__), data)

    def test_tampered_part_is_rejected(self):
        body, parts = self.envelope({"text": "original"})
        part = json.loads(parts[100][8:-4])
        part["data"] = part["data"][:-4] + "AAAA"
        parts[100] = fenced(part)
        with self.assertRaises(HumanRequired):
            decode_payload(body, parts.__getitem__)

    def test_wrong_order_is_rejected(self):
        body, parts = self.envelope({"text": "中" * 15000})
        parts[100], parts[101] = parts[101], parts[100]
        with self.assertRaises(HumanRequired):
            decode_payload(body, parts.__getitem__)

    def test_old_valid_report_still_reads(self):
        self.assertEqual(decode_payload(fenced({"old": True}), lambda _: None), {"old": True})

    def test_damaged_old_report_is_not_guessed_or_silently_accepted(self):
        body = fenced({"output": "\x1b[32m"}).replace("\\u001b", "\\^[")
        with self.assertRaises(HumanRequired):
            decode_payload(body, lambda _: None)

    def test_oversize_payload_stops_before_post(self):
        with self.assertRaises(HumanRequired):
            encode_payload(json.dumps({"text": "x" * 200000}))

    def test_utf8_pages_are_lossless_and_under_comment_budget(self):
        text = "中文😀\\u001b\\x00\n" * 5000
        pages = text_pages(text)
        self.assertEqual("".join(pages), text)
        self.assertGreater(len(pages), 1)
        for page in pages:
            self.assertLessEqual(len(page.encode("utf-8")), 18000)
            bounded_comment("header\n" + page)
        with self.assertRaises(HumanRequired):
            bounded_comment("中" * 9000)


if __name__ == "__main__":
    unittest.main()
