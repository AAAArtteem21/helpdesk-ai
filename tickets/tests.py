import json
from concurrent.futures import ThreadPoolExecutor

from django.db import connection
from django.test import Client, TransactionTestCase

from .models import Ticket

PAYLOAD = {
    "event_id": "evt_1", "ticket_id": "T-1", "customer_email": "u@example.com",
    "message": "Не можу експортувати звіт у PDF",
    "reply_url": "http://helpdesk.test/tickets/T-1/replies",
}


def post():
    try:
        return Client().post("/webhooks/helpdesk", json.dumps(PAYLOAD),
                             content_type="application/json")
    finally:
        connection.close()


class WebhookTests(TransactionTestCase):
    def test_health(self):
        r = self.client.get("/health")
        self.assertEqual((r.status_code, r.content), (200, b"OK"))

    def test_duplicate_event_creates_one_ticket(self):
        post(); post()
        self.assertEqual(Ticket.objects.count(), 1)

    def test_concurrent_duplicates_create_one_ticket(self):
        with ThreadPoolExecutor(8) as ex:
            responses = list(ex.map(lambda _: post(), range(8)))
        self.assertTrue(all(r.status_code == 200 for r in responses))
        self.assertEqual(Ticket.objects.count(), 1)

    def test_invalid_payload(self):
        r = self.client.post("/webhooks/helpdesk", "{}", content_type="application/json")
        self.assertEqual(r.status_code, 400)
