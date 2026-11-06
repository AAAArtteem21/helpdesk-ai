from concurrent.futures import ThreadPoolExecutor

from django.core.management import call_command
from django.db import connection
from django.test import Client, TransactionTestCase
from django.urls import reverse

from . import actions
from .models import Article, Ticket

S = Ticket.Status


def mk(i=0, status=S.DRAFT_READY, **kw):
    return Ticket.objects.create(
        event_id=f"e{i}", ticket_id=f"T-{i}", customer_email="u@example.com",
        message="Не можу експортувати звіт у PDF", reply_url="http://h.test/r",
        status=status, draft="Чернетка [KB-1]", **kw)


def in_threads(fn, n):
    def run(i):
        try:
            return fn(i)
        finally:
            connection.close()
    with ThreadPoolExecutor(n) as ex:
        return list(ex.map(run, range(n)))


class ActionTests(TransactionTestCase):
    def test_approve_saves_edited_text_and_resets_attempts(self):
        t = mk(attempts=3)
        self.assertTrue(actions.approve(t.pk, "Виправлений текст"))
        t.refresh_from_db()
        self.assertEqual((t.status, t.draft, t.attempts), (S.APPROVED, "Виправлений текст", 0))

    def test_second_approve_does_not_overwrite(self):
        t = mk()
        self.assertTrue(actions.approve(t.pk, "перший"))
        self.assertFalse(actions.approve(t.pk, "другий"))
        t.refresh_from_db()
        self.assertEqual(t.draft, "перший")

    def test_only_draft_ready_can_be_approved_or_rejected(self):
        for i, st in enumerate([S.RECEIVED, S.DRAFTING, S.SENT, S.FAILED, S.REJECTED, S.APPROVED]):
            t = mk(i, status=st)
            self.assertFalse(actions.approve(t.pk, "x"), st)
            self.assertFalse(actions.reject(t.pk), st)

    def test_reject_then_approve_fails(self):
        t = mk()
        self.assertTrue(actions.reject(t.pk))
        self.assertFalse(actions.approve(t.pk, "x"))
        t.refresh_from_db()
        self.assertEqual(t.status, S.REJECTED)

    def test_concurrent_approve_exactly_one_wins(self):
        t = mk()
        results = in_threads(lambda i: actions.approve(t.pk, f"text {i}"), 8)
        self.assertEqual(results.count(True), 1)
        t.refresh_from_db()
        self.assertEqual(t.status, S.APPROVED)
        self.assertEqual(t.draft, f"text {results.index(True)}")   # текст победителя

    def test_concurrent_approve_and_reject_exactly_one_wins(self):
        t = mk()
        results = in_threads(
            lambda i: actions.approve(t.pk, "ok") if i % 2 else actions.reject(t.pk), 8)
        self.assertEqual(results.count(True), 1)


class ViewTests(TransactionTestCase):
    def setUp(self):
        call_command("seed_kb", verbosity=0)
        self.c = Client()

    def test_list_and_filter(self):
        mk(1, status=S.DRAFT_READY)
        mk(2, status=S.SENT)
        r = self.c.get(reverse("ticket-list"))
        self.assertContains(r, "T-1")
        self.assertContains(r, "T-2")
        r = self.c.get(reverse("ticket-list"), {"status": "sent"})
        self.assertNotContains(r, "T-1")
        self.assertContains(r, "T-2")

    def test_detail_shows_sources_and_form(self):
        a = Article.objects.get(slug="export-pdf")
        t = mk(sources=[a.pk])
        r = self.c.get(reverse("ticket-detail", args=[t.pk]))
        self.assertContains(r, a.title)
        self.assertContains(r, "<textarea")

    def test_detail_escapes_customer_html(self):
        t = mk()
        Ticket.objects.filter(pk=t.pk).update(message="<script>alert(1)</script>")
        r = self.c.get(reverse("ticket-detail", args=[t.pk]))
        self.assertNotContains(r, "<script>alert(1)</script>")

    def test_approve_via_post(self):
        t = mk()
        r = self.c.post(reverse("ticket-approve", args=[t.pk]), {"draft": " Новий текст "})
        self.assertEqual(r.status_code, 302)
        t.refresh_from_db()
        self.assertEqual((t.status, t.draft), (S.APPROVED, "Новий текст"))

    def test_empty_draft_is_not_approved(self):
        t = mk()
        self.c.post(reverse("ticket-approve", args=[t.pk]), {"draft": "   "})
        t.refresh_from_db()
        self.assertEqual(t.status, S.DRAFT_READY)

    def test_get_is_not_allowed_on_actions(self):
        t = mk()
        self.assertEqual(self.c.get(reverse("ticket-approve", args=[t.pk])).status_code, 405)
        self.assertEqual(self.c.get(reverse("ticket-reject", args=[t.pk])).status_code, 405)

    def test_reject_via_post(self):
        t = mk()
        self.c.post(reverse("ticket-reject", args=[t.pk]))
        t.refresh_from_db()
        self.assertEqual(t.status, S.REJECTED)

    def test_api_stats_filters_by_prefix(self):
        mk(1, status=S.DRAFT_READY)
        mk(2, status=S.SENT)
        with self.settings(ENABLE_SIM_API=True):
            data = self.c.get("/api/stats", {"prefix": "T-1"}).json()
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["counts"], {"draft_ready": 1})
        self.assertEqual(len(data["draft_ready"]), 1)
