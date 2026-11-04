import csv
import json
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .forms import (
    AccountLoginForm,
    ProfileDisplayNameForm,
    RegistrationForm,
    UserPreferencesForm,
)
from .models import UserPreference
from .services import (
    MAX_LOGIN_ATTEMPTS,
    MAX_REGISTER_ATTEMPTS,
    clear_auth_attempts,
    get_client_identifier,
    is_rate_limited,
    record_auth_attempt,
    switch_workspace_scope,
    update_user_preferences,
    update_user_profile,
)


def get_cached_user_preference(request):
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return None
    if not hasattr(request, "_cached_user_preference"):
        request._cached_user_preference = (
            UserPreference.objects.select_related(
                "personal_base_currency",
                "display_currency",
                "active_shared_space",
                "active_shared_space__base_currency",
            )
            .filter(user=request.user)
            .first()
        )
    return request._cached_user_preference


def user_preferences_context(request):
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {
            "user_base_currency": None,
            "user_preference": None,
            "active_workspace_scope": "personal",
            "active_shared_space": None,
            "user_shared_spaces": [],
            "effective_base_currency": None,
            "display_currency": None,
            "effective_display_currency": None,
            "active_currencies": [],
        }

    from apps.budgets.models import Currency
    from apps.budgets.selectors import get_active_workspace_context

    ws = get_active_workspace_context(request.user)
    request._cached_user_preference = ws["preference"]
    active_currencies = list(Currency.objects.filter(is_active=True).order_by("code"))
    effective_display_currency = ws["effective_display_currency"]
    if not ws.get("display_currency"):
        get_disp_code = (request.GET.get("display_currency") or "").strip().upper()
        if get_disp_code:
            override_curr = next((c for c in active_currencies if c.code == get_disp_code), None)
            if override_curr:
                effective_display_currency = override_curr

    return {
        "user_base_currency": ws["personal_base_currency"],
        "user_preference": ws["preference"],
        "active_workspace_scope": ws["active_scope"],
        "active_shared_space": ws["active_shared_space"],
        "user_shared_spaces": ws["shared_spaces"],
        "effective_base_currency": ws["effective_base_currency"],
        "display_currency": ws["display_currency"],
        "effective_display_currency": effective_display_currency,
        "active_currencies": active_currencies,
    }


class RateLimitedLoginView(auth_views.LoginView):
    template_name = "accounts/login.html"
    authentication_form = AccountLoginForm
    extra_context = {"page_title": "Sign In"}

    def post(self, request, *args, **kwargs):
        username = request.POST.get("username", "")
        identifier = get_client_identifier(request, username)
        if is_rate_limited("login", identifier, MAX_LOGIN_ATTEMPTS):
            form = self.get_form()
            form.errors.clear()
            form.add_error(
                None,
                "Too many sign-in attempts. Please wait 15 minutes before trying again.",
            )
            return self.render_to_response(self.get_context_data(form=form), status=429)
        return super().post(request, *args, **kwargs)

    def form_invalid(self, form):
        username = self.request.POST.get("username", "")
        identifier = get_client_identifier(self.request, username)
        record_auth_attempt("login", identifier)
        return super().form_invalid(form)

    def form_valid(self, form):
        username = self.request.POST.get("username", "")
        identifier = get_client_identifier(self.request, username)
        clear_auth_attempts("login", identifier)
        return super().form_valid(form)


def register(request):
    if request.method == "POST":
        identifier = get_client_identifier(request)
        if is_rate_limited("register", identifier, MAX_REGISTER_ATTEMPTS):
            form = RegistrationForm(request.POST)
            form.errors.clear()
            form.add_error(
                None,
                "Too many registration attempts from this address. Please try again later.",
            )
            return render(
                request,
                "accounts/register.html",
                {"form": form, "page_title": "Create Account"},
                status=429,
            )

        form = RegistrationForm(request.POST)
        if form.is_valid():
            form.save()
            clear_auth_attempts("register", identifier)
            messages.success(
                request,
                "Your account has been created. Please sign in with your new credentials.",
            )
            return redirect("accounts:login")
        else:
            record_auth_attempt("register", identifier)
    else:
        form = RegistrationForm()

    return render(
        request,
        "accounts/register.html",
        {
            "form": form,
            "page_title": "Create Account",
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def settings(request):
    from apps.budgets.forms import PersonalBaseCurrencyForm
    from apps.budgets.models import Currency
    from apps.budgets.views import get_currencies_context, currencies as currencies_view

    pref = (
        UserPreference.objects.select_related("personal_base_currency")
        .filter(user=request.user)
        .first()
    )
    request._cached_user_preference = pref

    display_name = request.user.get_full_name().strip() or request.user.username
    email = request.user.email or ""

    profile_form = ProfileDisplayNameForm(initial={"display_name": display_name})
    preferences_form = UserPreferencesForm(instance=pref)
    curr_ctx = get_currencies_context(request)
    currency_form = curr_ctx["form"]
    has_active_currencies = curr_ctx["has_active_currencies"]
    active_tab = (request.GET.get("tab") or "profile").strip().lower()

    if request.method == "POST":
        action = request.POST.get("action", "").strip()
        if not action and "personal_base_currency" in request.POST:
            action = "save_base_currency"

        if action in ("save_base_currency", "convert_currency", "record_manual_rate", "refresh_rates"):
            return currencies_view(request)

        elif action == "save_profile":
            profile_form = ProfileDisplayNameForm(request.POST)
            if profile_form.is_valid():
                update_user_profile(
                    user=request.user,
                    display_name=profile_form.cleaned_data["display_name"],
                )
                messages.success(request, "Profile display name has been updated.")
                return redirect("accounts:settings")
        elif action == "save_preferences":
            preferences_form = UserPreferencesForm(request.POST, instance=pref)
            if preferences_form.is_valid():
                updated_pref = update_user_preferences(
                    user=request.user,
                    timezone_name=preferences_form.cleaned_data["timezone"],
                    week_starts_on=preferences_form.cleaned_data["week_starts_on"],
                )
                request._cached_user_preference = updated_pref
                messages.success(request, "Display and calendar preferences have been saved.")
                return redirect("accounts:settings")

    return render(
        request,
        "accounts/settings.html",
        {
            "page_title": "Settings",
            "active_tab": active_tab,
            "profile_display_name": display_name,
            "profile_email": email,
            "profile_form": profile_form,
            "preferences_form": preferences_form,
            "currency_form": currency_form,
            "has_active_currencies": has_active_currencies,
            **curr_ctx,
        },
    )


def _safe_redirect(request):
    next_url = request.POST.get("next") or ""
    if next_url and url_has_allowed_host_and_scheme(
        url=next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return redirect(next_url)
    return redirect("budgets:dashboard")


def _safe_currency_redirect(request, next_url, new_currency_code=None):
    if not next_url or not url_has_allowed_host_and_scheme(
        url=next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return redirect("budgets:dashboard")

    parsed = urlparse(next_url)
    query_params = parse_qs(parsed.query, keep_blank_values=True)
    if "display_currency" in query_params:
        del query_params["display_currency"]
        new_query = urlencode(query_params, doseq=True)
        reconciled_url = urlunparse((
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            new_query,
            parsed.fragment,
        ))
        return redirect(reconciled_url)

    return redirect(next_url)


@login_required
@require_POST
def switch_scope(request):
    from apps.budgets.models import SharedSpace

    choice = (
        request.POST.get("view_choice")
        or request.POST.get("scope")
        or "personal"
    ).strip()
    space_id = (request.POST.get("shared_space_id") or "").strip()

    if ":" in choice:
        scope, parsed_space_id = choice.split(":", 1)
        scope = scope.strip()
        space_id = parsed_space_id.strip()
    else:
        scope = choice

    shared_space = None
    if scope == "shared":
        if space_id:
            try:
                import uuid
                space_uuid = uuid.UUID(str(space_id))
                shared_space = SharedSpace.objects.filter(pk=space_uuid).first()
                if shared_space is None:
                    messages.error(request, "Selected shared space was not found.")
                    return _safe_redirect(request)
            except (ValueError, TypeError, AttributeError):
                messages.error(request, "Invalid shared space ID.")
                return _safe_redirect(request)

    try:
        updated_pref = switch_workspace_scope(
            user=request.user,
            scope=scope,
            shared_space=shared_space,
        )
        request._cached_user_preference = updated_pref
        if updated_pref.active_scope == "shared" and updated_pref.active_shared_space:
            messages.success(
                request,
                f'Switched workspace to Shared Space "{updated_pref.active_shared_space.name}".',
            )
        else:
            messages.success(request, "Switched workspace to Personal scope.")
    except (ValidationError, PermissionDenied) as exc:
        msg = "; ".join(exc.messages) if hasattr(exc, "messages") else str(exc)
        messages.error(request, msg)

    return _safe_redirect(request)


@login_required
@require_POST
def set_display_currency(request):
    from apps.budgets.models import Currency

    currency_val = (
        request.POST.get("currency_code")
        or request.POST.get("display_currency")
        or request.POST.get("currency")
        or ""
    ).strip()
    next_url = request.POST.get("next") or ""

    if not currency_val:
        messages.error(request, "Select a valid currency.")
        return _safe_currency_redirect(request, next_url)

    currency = None
    if currency_val.isdigit():
        currency = Currency.objects.filter(pk=int(currency_val), is_active=True).first()
    if not currency:
        currency = Currency.objects.filter(code=currency_val.upper(), is_active=True).first()

    if not currency:
        messages.error(request, f'Currency "{currency_val}" is invalid or inactive.')
        return _safe_currency_redirect(request, next_url)

    pref, _ = UserPreference.objects.select_for_update().get_or_create(user=request.user)
    pref.display_currency = currency
    pref.save(update_fields=["display_currency", "updated_at"])
    request._cached_user_preference = pref

    messages.success(
        request,
        f"Display currency updated to {currency.code} ({currency.name}).",
    )
    return _safe_currency_redirect(request, next_url, new_currency_code=currency.code)


@login_required
@require_GET
def export_account_data(request):
    from apps.budgets.selectors import (
        build_transactions_export_rows,
        enrich_wallets_with_balances,
        get_accessible_categories_qs,
        get_accessible_wallets_qs,
        get_savings_goals_overview,
    )
    from apps.budgets.services import sanitize_spreadsheet_cell

    fmt = (request.GET.get("format") or "json").strip().lower()
    tx_params = request.GET.copy()
    if "month" not in tx_params:
        tx_params["month"] = "all"
    tx_rows = build_transactions_export_rows(request.user, tx_params)

    if fmt == "csv":
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="budget_tracker_export.csv"'
        fieldnames = [
            "id",
            "date",
            "type",
            "scope",
            "shared_space",
            "wallet",
            "category",
            "description",
            "notes",
            "amount",
            "currency",
            "base_currency",
            "converted_amount",
            "conversion_rate",
            "conversion_status",
            "paid_by",
        ]
        writer = csv.DictWriter(response, fieldnames=fieldnames)
        writer.writeheader()
        for row in tx_rows:
            writer.writerow({k: sanitize_spreadsheet_cell(row.get(k, "")) for k in fieldnames})
        return response

    wallets = enrich_wallets_with_balances(
        get_accessible_wallets_qs(request.user, include_archived=True)
    )
    categories = get_accessible_categories_qs(request.user, include_archived=True)
    goals = get_savings_goals_overview(request.user, include_archived=True)

    payload = {
        "user": {
            "username": request.user.username,
            "display_name": request.user.get_full_name().strip() or request.user.username,
        },
        "wallets": [
            {
                "id": str(w.id),
                "name": w.name,
                "type": w.wallet_type,
                "scope": w.ownership_scope,
                "currency": w.currency.code,
                "opening_balance": w.plain_opening_balance,
                "opening_balance_date": w.opening_balance_date.isoformat(),
                "current_balance": w.plain_current_balance,
                "is_archived": w.is_archived,
            }
            for w in wallets
        ],
        "categories": [
            {
                "id": str(c.id),
                "name": c.name,
                "kind": c.kind,
                "scope": c.scope,
                "is_archived": c.is_archived,
            }
            for c in categories
        ],
        "savings_goals": [
            {
                "id": str(r["goal"].id),
                "name": r["goal"].name,
                "scope": r["goal"].scope,
                "currency": r["goal"].currency.code,
                "target_amount": r["plain_target"],
                "saved_amount": r["plain_saved"],
                "remaining_amount": r["plain_remaining"],
                "target_date": r["goal"].target_date.isoformat() if r["goal"].target_date else None,
            }
            for r in goals["rows"]
        ],
        "transactions": tx_rows,
    }
    response = HttpResponse(
        json.dumps(payload, indent=2),
        content_type="application/json; charset=utf-8",
    )
    response["Content-Disposition"] = 'attachment; filename="budget_tracker_export.json"'
    return response
