from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .jobs import backoff
from .models import Ticket

S = Ticket.Status

def fail_exhaused(now=None) -> int:
    now = now or timezone.now()
    return Ticket.objects.filter(
        status=S.SENDING, locked_until__lt=now,
        attempts_gte=settings.SEND_MAX_ATTEMPTS,
    ).update(status=S.FAILED, locked_until=None, updated_at=now,
            last_error="delivery outcome unknown: sender lost the ticket too many times; "
                        "check the helpdesk for reply " )

def claim(limit: int) -> list[Ticket]:
    now = timezone.now()
    fail_exhaused(now)
    with transaction.atomic():
        tickets = list(
            Ticket.objects.select_for_update(skip_locked=True)
            .filter(
                Q(status=S.APPROVED, next_attempt_at__lte=now)
                | Q(status=S.SENDING, locked_until__lt=now)
            )
            .order_by("next_attempt_at")[:limit]
        )
        lease = now + timedelta(seconds=settings.WORKER_LEASE_SECONDS)
        for t in tickets:
            t.status = S.SENDING
            t.attempts += 1
            t.locked_until = lease
            t.save(update_fields=["status", "attempts", "locked_until", "updated_at"])
    return tickets

def _mine(ticket: Ticket):
    return Ticket.objects.filter(pk=ticket.pk, status=S.SENDING, attempts=ticket.attempts)


def finish_sent(ticket: Ticket) -> bool:
    return _mine(ticket).update(
        status=S.SENT, locked_until=None, last_error="", updated_at=timezone.now(),
    ) == 1

def finish_failed(ticket: Ticket, error: str) -> bool:
    return _mine(ticket).update(
        status=S.FAILED, locked_until=None, last_error=error[:1000],
        updated_at=timezone.now(),
    ) == 1

def finish_retry(ticket: Ticket, error: str, retry_after: float | None = None) -> bool:
    if ticket.attempts >= settings.SEND_MAX_ATTEMPTS:
        return finish_failed(
            ticket, f"delivery outcome unknown after {ticket.attempts} attempts: {error}")
    now = timezone.now()
    delay = backoff(ticket.attempts, retry_after)
    return _mine(ticket).update(
        status=S.APPROVED, next_attempt_at=now + timedelta(seconds=delay),
        locked_until=None, last_error=error[:1000], updated_at=now,
    ) == 1
