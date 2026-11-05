import asyncio
import time
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase

import httpx

from .llm import (LLMClient, LLMPermanentError, LLMRateLimited, LLMTemporaryError,
                  SlidingWindowLimiter, extract_sources)

ARTICLES = [
    SimpleNamespace(pk=1, title="Експорт звіту у PDF", body="...", public_url="http://x/kb/export-pdf"),
    SimpleNamespace(pk=2, title="Тарифи", body="...", public_url="http://x/kb/plans"),
]


def make_client(handler, **kw):
    kw.setdefault("keyless_delay", (0, 0))
    return LLMClient(transport=httpx.MockTransport(handler), **kw)


def ok(text):
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})


class LimiterTests(IsolatedAsyncioTestCase):
    async def test_limiter_blocks_over_limit(self):
        lim = SlidingWindowLimiter(limit=2, window=0.5)
        t0 = time.monotonic()
        for _ in range(3):
            await lim.acquire()
        self.assertGreaterEqual(time.monotonic() - t0, 0.45)

    async def test_penalize_pauses_everyone(self):
        lim = SlidingWindowLimiter(limit=100, window=60)
        lim.penalize(0.3)
        t0 = time.monotonic()
        await lim.acquire()
        self.assertGreaterEqual(time.monotonic() - t0, 0.25)


class ClientTests(IsolatedAsyncioTestCase):
    async def test_real_mode_parses_sources(self):
        c = make_client(lambda r: ok("Див. [KB-1] і чужий [KB-99]"), api_key="k")
        d = await c.generate_draft("питання", ARTICLES)
        self.assertEqual(d.sources, [1])
        await c.aclose()

    async def test_keyless_still_sends_request_and_returns_static_draft(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(401)

        c = make_client(handler, api_key="")
        d = await c.generate_draft("питання", ARTICLES)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("authorization", calls[0].headers)
        self.assertEqual(d.sources, [1, 2])
        await c.aclose()

    async def test_429_raises_and_penalizes(self):
        c = make_client(lambda r: httpx.Response(429, headers={"retry-after": "2"}), api_key="k")
        with self.assertRaises(LLMRateLimited) as cm:
            await c.generate_draft("q", ARTICLES)
        self.assertEqual(cm.exception.retry_after, 2.0)
        await c.aclose()

    async def test_5xx_is_temporary_4xx_is_permanent(self):
        c = make_client(lambda r: httpx.Response(503), api_key="k")
        with self.assertRaises(LLMTemporaryError):
            await c.generate_draft("q", ARTICLES)
        c2 = make_client(lambda r: httpx.Response(400), api_key="k")
        with self.assertRaises(LLMPermanentError):
            await c2.generate_draft("q", ARTICLES)

    async def test_concurrency_is_capped(self):
        running = peak = 0

        async def handler(request):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.05)
            running -= 1
            return ok("ok")

        c = make_client(handler, api_key="k", concurrency=3, rpm=1000)
        await asyncio.gather(*(c.generate_draft("q", ARTICLES) for _ in range(12)))
        self.assertEqual(peak, 3)
        await c.aclose()

    def test_extract_sources_dedup(self):
        self.assertEqual(extract_sources("[KB-2] [KB-2] [KB-1]", ARTICLES), [2, 1])