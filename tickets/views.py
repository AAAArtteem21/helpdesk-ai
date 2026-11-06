import json

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.html import escape, linebreaks
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.conf import settings
from django.http import Http404

from . import actions
from .models import Article, Ticket

S = Ticket.Status
PAGE_SIZE = 50

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


def ticket_list(request):
    status = request.GET.get("status", "")
    qs = Ticket.objects.all()
    if status in S.values:
        qs = qs.filter(status=status)
    else:
        status = ""

    counts = dict(Ticket.objects.order_by().values_list("status").annotate(n=Count("id")))
    tabs = [("", "Усі", sum(counts.values()))] + [(v, v, counts.get(v, 0)) for v in S.values]
    page = Paginator(qs, PAGE_SIZE).get_page(request.GET.get("page"))
    return render(request, "tickets/list.html",
                  {"page": page, "tabs": tabs, "status": status})

def ticket_detail(request, pk):
    ticket = get_object_or_404(Ticket, pk=pk)
    by_id = {a.pk: a for a in Article.objects.filter(pk__in=ticket.sources)}
    sources = [by_id[i] for i in ticket.sources if i in by_id]
    return render(request, "tickets/detail.html", {
        "t": ticket, "sources": sources,
        "editable": ticket.status == S.DRAFT_READY,
        "pending": ticket.status in (S.RECEIVED, S.DRAFTING, S.APPROVED, S.SENDING),
        "max_len": actions.MAX_DRAFT_LEN,
    })

@require_POST
def ticket_approve(request, pk):
    text = request.POST.get("draft", "").strip()
    if not text or len(text) > actions.MAX_DRAFT_LEN:
        messages.error(request, "Текст відповіді задовгий чи порожній.")
    elif actions.approve(pk, text):
        messages.success(request, "Підтверджено. Відповідь піде клієнту.")
    else:
        messages.warning(request, "Звернення вже оброблено: статус змінився.")
    return redirect("ticket-detail", pk=pk)

@require_POST
def ticket_reject(request, pk):
    if actions.reject(pk):
        messages.success(request, "Чернетку відхилено.")
    else:
        messages.warning(request, "Звернення вже оброблено: статус змінився.")
    return redirect("ticket-detail", pk=pk)

def api_stats(request):
    if not settings.ENABLE_SIM_API:
        raise Http404
    prefix = request.GET.get("prefix", "")
    qs = Ticket.objects.filter(ticket_id__startswith=prefix)
    counts = dict(qs.order_by().values_list("status").annotate(n=Count("id")))
    ready = list(qs.filter(status=S.DRAFT_READY).order_by("created_at")
                 .values("id", "ticket_id", "draft")[:100])
    return JsonResponse({"counts": counts, "total": sum(counts.values()),
                         "draft_ready": ready})
