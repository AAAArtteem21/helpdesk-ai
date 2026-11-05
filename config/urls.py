
from django.contrib import admin
from django.urls import path
from tickets import views


urlpatterns = [
    path("admin/", admin.site.urls),
    path("health", views.health),
    path("webhooks/helpdesk", views.helpdesk_webhook),
]
