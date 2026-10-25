"""
URL configuration for config project.
"""
from django.contrib import admin
from django.urls import include, path

from apps.budgets import views as budget_views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("service-worker.js", budget_views.service_worker, name="service_worker"),
    path("", include("apps.budgets.urls")),
    path("accounts/", include("apps.accounts.urls")),
]
