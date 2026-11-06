import random
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Case, IntegerField, Q, Value, When, F
from django.utils import timezone

from .models import Ticket

S = Ticket.Status


def backoff(attempts: int, retry_after: float | None = None) -> float:
    """sec for next try"""
    if retry_after is not None:
        return retry_after + random.uniform(0, 2)
    return min(300, 2 * 2 ** (attempts - 1)) * random.uniform(0.5, 1.5)


def fail_exhausted(now=None) -> int:
    now = now or timezone.now()
    return Ticket.objects.filter(
        status=S.DRAFTING, locked_until__lt=now,
        attempts__gte=settings.WORKER_MAX_ATTEMPTS,
    ).update(status=S.FAILED, locked_until=None, updated_at=now,
             last_error="worker lost the ticket too many times (lease expired)")


def claim(limit: int) -> list[Ticket]:
    now = timezone.now()
    fail_exhausted(now)
    with transaction.atomic():
        tickets = list(
            Ticket.objects.select_for_update(skip_locked=True)
            .filter(
                Q(status=S.RECEIVED, next_attempt_at__lte=now)
                | Q(status=S.DRAFTING, locked_until__lt=now)
            )
            .annotate(is_retry=Case(When(attempts__gt=0, then=Value(1)),
                                    default=Value(0), output_field=IntegerField()))
            .order_by("is_retry", "next_attempt_at")[:limit]
        )
        lease = now + timedelta(seconds=settings.WORKER_LEASE_SECONDS)
        for t in tickets:
            t.status = S.DRAFTING
            t.attempts += 1
            t.locked_until = lease
            t.save(update_fields=["status", "attempts", "locked_until", "updated_at"])
    return tickets


def _mine(ticket: Ticket):
    return Ticket.objects.filter(pk=ticket.pk, status=S.DRAFTING, attempts=ticket.attempts)


def finish_ok(ticket: Ticket, text: str, sources: list[int]) -> bool:
    return _mine(ticket).update(
        status=S.DRAFT_READY, draft=text, sources=sources,
        locked_until=None, last_error="", updated_at=timezone.now(),
    ) == 1


def finish_failed(ticket: Ticket, error: str) -> bool:
    return _mine(ticket).update(
        status=S.FAILED, locked_until=None, last_error=error[:1000],
        updated_at=timezone.now(),
    ) == 1


def finish_retry(ticket, error, retry_after=None, count_attempt=True) -> bool:
    if count_attempt and ticket.attempts >= settings.WORKER_MAX_ATTEMPTS:
        return finish_failed(ticket, f"gave up after {ticket.attempts} attempts: {error}")
    delay = backoff(ticket.attempts, retry_after)
    now = timezone.now()
    extra = {} if count_attempt else {"attempts": F("attempts") - 1}
    return _mine(ticket).update(
        status=S.RECEIVED, next_attempt_at=now + timedelta(seconds=delay),
        locked_until=None, last_error=error[:1000], updated_at=now, **extra,
    ) == 1
