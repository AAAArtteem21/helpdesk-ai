from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import httpx
from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.management import call_command
from django.db import connection
from django.test import TransactionTestCase
from django.utils import timezone

from . import jobs
from .llm import Draft, LLMClient, LLMPermanentError, LLMRateLimited, LLMTemporaryError
from .models import Article, Ticket
from .worker import process

S = Ticket.Status


def mk(i=0, **kw):
    return Ticket.objects.create(
        event_id=f"e{i}", ticket_id=f"T-{i}", customer_email="u@example.com",
        message="Не можу експортувати звіт у PDF", reply_url="http://h.test/r", **kw)


class FakeClient:
    def __init__(self, result):
        self.result = result

    async def generate_draft(self, message, articles):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class ClaimTests(TransactionTestCase):
    def test_claim_sets_lease_and_attempt(self):
        mk()
        [t] = jobs.claim(5)
        t.refresh_from_db()
        self.assertEqual((t.status, t.attempts), (S.DRAFTING, 1))
        self.assertGreater(t.locked_until, timezone.now())

    def test_future_retry_is_not_claimed(self):
        mk(next_attempt_at=timezone.now() + timedelta(hours=1))
        self.assertEqual(jobs.claim(5), [])

    def test_new_tickets_go_before_retries(self):
        mk(1, attempts=2, next_attempt_at=timezone.now() - timedelta(hours=1))
        mk(2)
        [t] = jobs.claim(1)
        self.assertEqual(t.event_id, "e2")

    def test_expired_lease_is_reclaimed(self):
        mk(status=S.DRAFTING, attempts=1, locked_until=timezone.now() - timedelta(seconds=1))
        [t] = jobs.claim(5)
        self.assertEqual(t.attempts, 2)

    def test_live_lease_is_not_reclaimed(self):
        mk(status=S.DRAFTING, attempts=1, locked_until=timezone.now() + timedelta(minutes=5))
        self.assertEqual(jobs.claim(5), [])

    def test_exhausted_expired_ticket_fails(self):
        t = mk(status=S.DRAFTING, attempts=settings.WORKER_MAX_ATTEMPTS,
               locked_until=timezone.now() - timedelta(seconds=1))
        self.assertEqual(jobs.claim(5), [])
        t.refresh_from_db()
        self.assertEqual(t.status, S.FAILED)

    def test_parallel_claims_never_overlap(self):
        for i in range(20):
            mk(i)

        def grab(_):
            try:
                return [t.pk for t in jobs.claim(10)]
            finally:
                connection.close()

        with ThreadPoolExecutor(4) as ex:
            got = [pk for part in ex.map(grab, range(4)) for pk in part]
        got += [t.pk for t in jobs.claim(20)]          # добираем остаток
        self.assertEqual(len(got), len(set(got)))       # ни один тикет не взят дважды
        self.assertEqual(len(got), 20)


class ProcessTests(TransactionTestCase):
    def setUp(self):
        call_command("seed_kb", verbosity=0)

    async def claim_one(self, **kw):
        await sync_to_async(mk)(**kw)
        [t] = await sync_to_async(jobs.claim)(1)
        return t

    async def test_success_saves_draft_and_sources(self):
        t = await self.claim_one()
        await process(t, FakeClient(Draft("Див. [KB-1]", [1])))
        t = await Ticket.objects.aget(pk=t.pk)
        self.assertEqual((t.status, t.draft, t.sources), (S.DRAFT_READY, "Див. [KB-1]", [1]))
        self.assertIsNone(t.locked_until)

    async def test_rate_limit_schedules_retry(self):
        t = await self.claim_one()
        await process(t, FakeClient(LLMRateLimited("429", retry_after=30)))
        t = await Ticket.objects.aget(pk=t.pk)
        self.assertEqual(t.status, S.RECEIVED)
        self.assertGreater(t.next_attempt_at, timezone.now() + timedelta(seconds=25))
        self.assertIn("429", t.last_error)

    async def test_permanent_error_fails(self):
        t = await self.claim_one()
        await process(t, FakeClient(LLMPermanentError("400")))
        t = await Ticket.objects.aget(pk=t.pk)
        self.assertEqual(t.status, S.FAILED)

    async def test_gives_up_after_max_attempts(self):
        t = await self.claim_one(attempts=settings.WORKER_MAX_ATTEMPTS - 1)
        await process(t, FakeClient(LLMTemporaryError("503")))
        t = await Ticket.objects.aget(pk=t.pk)
        self.assertEqual(t.status, S.FAILED)

    async def test_stale_worker_cannot_overwrite(self):
        old = await self.claim_one()
        await Ticket.objects.filter(pk=old.pk).aupdate(
            locked_until=timezone.now() - timedelta(seconds=1))
        [new] = await sync_to_async(jobs.claim)(1)       # другой воркер перехватил
        self.assertEqual(new.attempts, 2)
        await process(old, FakeClient(Draft("запізніла відповідь", [])))
        t = await Ticket.objects.aget(pk=old.pk)
        self.assertEqual((t.status, t.draft), (S.DRAFTING, ""))

    async def test_keyless_end_to_end(self):
        t = await self.claim_one()
        client = LLMClient(api_key="", keyless_delay=(0, 0),
                           transport=httpx.MockTransport(lambda r: httpx.Response(401)))
        await process(t, client)
        await client.aclose()
        t = await Ticket.objects.aget(pk=t.pk)
        self.assertEqual(t.status, S.DRAFT_READY)
        self.assertIn("[KB-", t.draft)
        ids = set(await sync_to_async(lambda: set(Article.objects.values_list("pk", flat=True)))())
        self.assertTrue(t.sources and set(t.sources) <= ids)