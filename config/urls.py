from django.contrib import admin
from django.urls import path

from tickets import views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("health", views.health),
    path("webhooks/helpdesk", views.helpdesk_webhook),
    path("kb/<slug:slug>", views.kb_article, name="kb-article"),
    path("", views.ticket_list, name="ticket-list"),
    path("tickets/<int:pk>", views.ticket_detail, name="ticket-detail"),
    path("tickets/<int:pk>/approve", views.ticket_approve, name="ticket-approve"),
    path("tickets/<int:pk>/reject", views.ticket_reject, name="ticket-reject"),
]