import json
from datetime import timedelta

import httpx
from asgiref.sync import sync_to_async
from django.conf import settings
from django.test import TransactionTestCase
from django.utils import timezone

from . import send_jobs as sj
from .models import Ticket
from .sender import deliver

S = Ticket.Status


def mk(i=0, status=S.APPROVED, **kw):
    return Ticket.objects.create(
        event_id=f"e{i}", ticket_id=f"T-{i}", customer_email="u@example.com",
        message="питання", reply_url="http://h.test/r", status=status,
        draft="Готова відповідь", **kw)


def client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class SendClaimTests(TransactionTestCase):
    def test_claims_approved_only(self):
        mk(1, status=S.DRAFT_READY)
        mk(2, status=S.APPROVED)
        [t] = sj.claim(10)
        self.assertEqual((t.event_id, t.status, t.attempts), ("e2", S.SENDING, 1))

    def test_future_retry_not_claimed(self):
        mk(next_attempt_at=timezone.now() + timedelta(hours=1))
        self.assertEqual(sj.claim(5), [])

    def test_crashed_sender_ticket_is_reclaimed_with_same_key(self):
        t = mk(status=S.SENDING, attempts=1, locked_until=timezone.now() - timedelta(seconds=1))
        [again] = sj.claim(5)
        self.assertEqual((again.attempts, again.reply_key), (2, t.reply_key))

    def test_live_lease_not_reclaimed(self):
        mk(status=S.SENDING, attempts=1, locked_until=timezone.now() + timedelta(minutes=5))
        self.assertEqual(sj.claim(5), [])

    def test_exhausted_lost_ticket_fails_as_unknown(self):
        t = mk(status=S.SENDING, attempts=settings.SEND_MAX_ATTEMPTS,
               locked_until=timezone.now() - timedelta(seconds=1))
        self.assertEqual(sj.claim(5), [])
        t.refresh_from_db()
        self.assertEqual(t.status, S.FAILED)
        self.assertIn("unknown", t.last_error)


class DeliverTests(TransactionTestCase):
    async def claim_one(self, **kw):
        await sync_to_async(mk)(**kw)
        [t] = await sync_to_async(sj.claim)(1)
        return t

    async def test_success_sends_key_and_body(self):
        seen = []

        def handler(req):
            seen.append(req)
            return httpx.Response(201, json={})

        t = await self.claim_one()
        async with client(handler) as http:
            await deliver(t, http)
        self.assertEqual(seen[0].headers["idempotency-key"], str(t.reply_key))
        self.assertEqual(json.loads(seen[0].content)["body"], "Готова відповідь")
        self.assertEqual(str(seen[0].url), "http://h.test/r")
        t = await Ticket.objects.aget(pk=t.pk)
        self.assertEqual((t.status, t.locked_until), (S.SENT, None))

    async def test_retry_after_5xx_reuses_same_key(self):
        keys, answers = [], iter([httpx.Response(503), httpx.Response(201, json={})])

        def handler(req):
            keys.append(req.headers["idempotency-key"])
            return next(answers)

        t = await self.claim_one()
        async with client(handler) as http:
            await deliver(t, http)
            t1 = await Ticket.objects.aget(pk=t.pk)
            self.assertEqual(t1.status, S.APPROVED)
            self.assertGreater(t1.next_attempt_at, timezone.now())
            await Ticket.objects.filter(pk=t.pk).aupdate(next_attempt_at=timezone.now())
            [t2] = await sync_to_async(sj.claim)(1)
            await deliver(t2, http)
        self.assertEqual(len(set(keys)), 1)
        self.assertEqual(len(keys), 2)
        t = await Ticket.objects.aget(pk=t.pk)
        self.assertEqual((t.status, t.attempts), (S.SENT, 2))

    async def test_timeout_means_retry_not_failure(self):
        def handler(req):
            raise httpx.ReadTimeout("slow")

        t = await self.claim_one()
        async with client(handler) as http:
            await deliver(t, http)
        t = await Ticket.objects.aget(pk=t.pk)
        self.assertEqual(t.status, S.APPROVED)
        self.assertIn("unknown", t.last_error)

    async def test_429_honors_retry_after(self):
        t = await self.claim_one()
        async with client(lambda r: httpx.Response(429, headers={"retry-after": "30"})) as http:
            await deliver(t, http)
        t = await Ticket.objects.aget(pk=t.pk)
        self.assertEqual(t.status, S.APPROVED)
        self.assertGreater(t.next_attempt_at, timezone.now() + timedelta(seconds=25))

    async def test_4xx_is_permanent(self):
        t = await self.claim_one()
        async with client(lambda r: httpx.Response(404, text="no such ticket")) as http:
            await deliver(t, http)
        t = await Ticket.objects.aget(pk=t.pk)
        self.assertEqual(t.status, S.FAILED)
        self.assertIn("404", t.last_error)

    async def test_gives_up_after_max_attempts(self):
        t = await self.claim_one(attempts=settings.SEND_MAX_ATTEMPTS - 1)
        async with client(lambda r: httpx.Response(503)) as http:
            await deliver(t, http)
        t = await Ticket.objects.aget(pk=t.pk)
        self.assertEqual(t.status, S.FAILED)
        self.assertIn("unknown", t.last_error)

    async def test_stale_sender_cannot_overwrite(self):
        old = await self.claim_one()
        await Ticket.objects.filter(pk=old.pk).aupdate(
            locked_until=timezone.now() - timedelta(seconds=1))
        [new] = await sync_to_async(sj.claim)(1)
        self.assertEqual(new.attempts, 2)
        async with client(lambda r: httpx.Response(201, json={})) as http:
            await deliver(old, http)
        t = await Ticket.objects.aget(pk=old.pk)
        self.assertEqual(t.status, S.SENDING)