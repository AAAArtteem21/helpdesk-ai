from django.shortcuts import render
import json 
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.shortcuts import get_object_or_404
from django.utils.html import escape, linebreaks
from .models import Ticket, Article

REQUIRED = ("event_id", "ticket_id", "customer_email", "message", "reply_url")

def health(request):
    return HttpResponse("OK")

def kb_article(request, slug):
    a = get_object_or_404(Article, slug=slug)
    return HttpResponse(f"<h1>{escape(a.title)}</h1>{linebreaks(a.body)}")


@csrf_exempt
@require_POST
def helpdesk_webhook(request):
    try:
        payload = json.loads(request.body)
    except ValueError:
        return JsonResponse({"error": "invalid json"}, status=400)

    if not isinstance(payload, dict):
        return JsonResponse({"error": "invalid payload"}, status=400)

    bad = [k for k in REQUIRED if not isinstance(payload.get(k), str) or not payload[k]]
    if bad:
        return JsonResponse({"error": "invalid fields", "fields": bad}, status=400)

    _, created = Ticket.objects.get_or_create(
        event_id=payload["event_id"],
        defaults={k: payload[k] for k in REQUIRED if k != "event_id"},
    )

    return JsonResponse({"status": "accepted" if created else "duplicate"})
