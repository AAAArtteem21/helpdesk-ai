from django.db import models
import uuid
from django.utils import timezone


class Ticket(models.Model):
    class Status(models.TextChoices):
        RECEIVED = "received"
        DRAFTING = "drafting"
        DRAFT_READY = "draft_ready"
        APPROVED = "approved"
        REJECTED = "rejected"
        SENDING = "sending"
        SENT = "sent"
        FAILED = "failed"

    event_id = models.CharField(max_length=128, unique=True)
    ticket = models.CharField(max_length=64, db_index=True)
    customer_email = models.EmailField()
    message = models.TextField()
    reply_url = models.URLField(max_length=500)

    status = models.CharField(max_length=16, choices=Status.choices,
                              default=Status.RECEIVED)
    draft = models.TextField(blank=True)
    sources = models.JSONField(default=list)
    reply_key = models.UUIDField(default=uuid.uuid4, editable=False)

    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now())
    locked_until = models.DateTimeField(null=True, blank=True)
    last_error = models.DateTimeField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "next_attempt_at"])]

    def __str__(self):
        return f"{self.ticket.id} [{self.status}]"
