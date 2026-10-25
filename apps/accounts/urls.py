from django.contrib.auth import views as auth_views
from django.urls import path, reverse_lazy

from . import views
from .forms import StyledPasswordChangeForm

app_name = "accounts"

urlpatterns = [
    path(
        "login/",
        views.RateLimitedLoginView.as_view(),
        name="login",
    ),
    path(
        "logout/",
        auth_views.LogoutView.as_view(),
        name="logout",
    ),
    path("register/", views.register, name="register"),
    path("settings/", views.settings, name="settings"),
    path("switch-scope/", views.switch_scope, name="switch_scope"),
    path("set-display-currency/", views.set_display_currency, name="set_display_currency"),
    path("export/", views.export_account_data, name="export_data"),
    path(
        "password-change/",
        auth_views.PasswordChangeView.as_view(
            template_name="accounts/password_change.html",
            form_class=StyledPasswordChangeForm,
            success_url=reverse_lazy("accounts:password_change_done"),
            extra_context={"page_title": "Change Password"},
        ),
        name="password_change",
    ),
    path(
        "password-change/done/",
        auth_views.PasswordChangeDoneView.as_view(
            template_name="accounts/password_change_done.html",
            extra_context={"page_title": "Password Changed"},
        ),
        name="password_change_done",
    ),
]
