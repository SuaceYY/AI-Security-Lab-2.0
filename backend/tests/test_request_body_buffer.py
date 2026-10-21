"""Resource and disconnect contracts for the pre-JSON request body guard."""
import asyncio
import sys
import tracemalloc
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from main import RequestBodyLimitMiddleware  # noqa: E402

SCOPE = {"type": "http", "method": "POST", "path": "/scans", "headers": []}


class RequestBodyBufferTests(unittest.TestCase):
    def test_small_fragments_have_bounded_buffer_memory(self):
        async def run():
            remaining = 32768
            consumed = 0

            async def receive():
                nonlocal remaining
                remaining -= 1
                return {"type": "http.request", "body": b"x", "more_body": remaining > 0}

            async def downstream(scope, receive, send):
                nonlocal consumed
                while True:
                    message = await receive()
                    consumed += len(message.get("body", b""))
                    if not message.get("more_body", False):
                        return

            async def send(message):
                self.fail("a valid body must reach the downstream app")

            tracemalloc.start()
            try:
                await RequestBodyLimitMiddleware(downstream)(SCOPE, receive, send)
                peak = tracemalloc.get_traced_memory()[1]
            finally:
                tracemalloc.stop()
            return consumed, peak

        consumed, peak = asyncio.run(run())
        self.assertEqual(consumed, 32768)
        # The previous list of ASGI dictionaries consumed over 6 MiB here.
        self.assertLess(peak, 2 * 1024 * 1024)

    def test_partial_body_disconnect_is_preserved(self):
        async def run():
            pending = iter([
                {"type": "http.request", "body": b"partial", "more_body": True},
                {"type": "http.disconnect"},
            ])
            replayed = []

            async def receive():
                return next(pending)

            async def downstream(scope, receive, send):
                replayed.extend([await receive(), await receive()])

            async def send(message):
                self.fail("the downstream app handles disconnects")

            await RequestBodyLimitMiddleware(downstream)(SCOPE, receive, send)
            return replayed

        self.assertEqual(asyncio.run(run()), [
            {"type": "http.request", "body": b"partial", "more_body": True},
            {"type": "http.disconnect"},
        ])
