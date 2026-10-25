from django.urls import path

from . import views

app_name = "budgets"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("transactions/", views.transactions, name="transactions"),
    path("transactions/export/", views.export_transactions, name="export_transactions"),
    path("budgets/", views.budgets, name="budgets"),
    path("shared-space/", views.shared_space, name="shared_space"),
    path("shared-activity/", views.shared_activity, name="shared_activity"),
    path("sharing-privacy/", views.sharing_privacy, name="sharing_privacy"),
    path("wallets/", views.wallets, name="wallets"),
    path("savings-goals/", views.savings_goals, name="savings_goals"),
    path("reports/", views.reports, name="reports"),
    path("currencies/", views.currencies, name="currencies"),
    path("offline/", views.offline_page, name="offline"),
    path("api/sync/state/", views.sync_state_api, name="sync_state_api"),
    path("api/sync/mutations/", views.sync_mutations_api, name="sync_mutations_api"),
]
