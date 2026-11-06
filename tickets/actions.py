from django.utils import timezone

from .models import Ticket

S = Ticket.Status
MAX_DRAFT_LEN = 10_000

def approve(pk: int, text: str) -> bool:
    """draft_ready -> aproved with text operator, if ticket not in draft_ready -> false"""
    now = timezone.now()
    return Ticket.objects.filter(pk=pk, status=S.DRAFT_READY).update(
        status=S.APPROVED, draft=text,
        attempts=0, next_attempt_at=now,
        locked_until=None, last_error="", updated_at=now,
    ) == 1

def reject(pk: int) -> bool:
    return Ticket.objects.filter(pk=pk, status=S.DRAFT_READY).update(
        status=S.REJECTED, updated_at=timezone.now(),
    ) == 1
