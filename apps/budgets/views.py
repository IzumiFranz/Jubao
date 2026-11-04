import csv
from datetime import date, datetime, time as dt_time
from decimal import Decimal
import json
from pathlib import Path
import re
from types import SimpleNamespace
import uuid

from django.conf import settings as django_settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Count, Q, Sum
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template import loader as template_loader
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import cache_control
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.accounts.views import get_cached_user_preference

from .exchange_rates import (
    convert_decimal_amount,
    get_accessible_exchange_rates_qs,
    get_configured_provider_info,
    get_latest_rate,
    record_manual_exchange_rate,
    refresh_exchange_rates,
)
from .forms import (
    AcceptInvitationForm,
    CategoryBudgetForm,
    CategoryForm,
    CurrencyConverterForm,
    ManualExchangeRateForm,
    PersonalBaseCurrencyForm,
    SavingsContributionForm,
    SavingsGoalForm,
    SharedSpaceForm,
    SharedSpaceInvitationForm,
    TransactionEntryForm,
    TransferEntryForm,
    WalletForm,
)
from .models import (
    Category,
    CategoryBudget,
    Currency,
    ExchangeRate,
    PersonalBalanceSharePermission,
    PersonalTransactionShare,
    SavingsGoal,
    SharedSpace,
    SharedSpaceInvitation,
    SharedSpaceMembership,
    Transaction,
    Transfer,
    Wallet,
    format_currency_amount,
)
from .selectors import (
    build_transactions_export_rows,
    enrich_transactions_with_display_currency,
    enrich_wallets_with_balances,
    get_accessible_categories_qs,
    get_accessible_transactions_qs,
    get_accessible_transfers,
    get_accessible_wallets_qs,
    get_active_shares_for_user_transactions,
    get_active_workspace_context,
    get_budget_overview_for_period,
    get_dashboard_context,
    get_eligible_partner_spaces_for_user,
    get_filtered_shared_activity,
    get_filtered_transactions,
    get_personal_balance_disclosure_for_space,
    get_reports_context,
    get_savings_goals_overview,
    get_shared_personal_activity_for_space,
    get_sync_snapshot_for_user,
    get_user_active_memberships,
    resolve_user_effective_display_currency,
)
from .services import (
    INVITATION_ERROR_MESSAGE,
    INVITATION_LOCKOUT_MESSAGE,
    accept_shared_space_invitation,
    check_invitation_rate_limit,
    clear_invitation_rate_limit,
    generate_invitation_qr_svg,
    normalize_invitation_code,
    record_invitation_failure,
    archive_category,
    archive_category_budget,
    archive_savings_goal,
    create_category,
    create_savings_goal,
    create_shared_space,
    create_shared_space_invitation,
    create_transaction_entry,
    create_transfer_entry,
    create_wallet_record,
    delete_transaction_entry,
    delete_transfer_entry,
    leave_shared_space,
    process_sync_batch,
    record_savings_contribution,
    revoke_personal_balance_share_permission,
    revoke_personal_transaction_share,
    revoke_shared_space_invitation,
    sanitize_spreadsheet_cell,
    save_category_budget,
    save_personal_balance_share_permission,
    share_personal_transaction,
    update_category,
    update_personal_transaction_share,
    update_savings_goal,
    update_shared_space,
    update_transaction_entry,
    update_transfer_entry,
    update_wallet_record,
)


def _format_error_message(exc) -> str:
    if hasattr(exc, "message_dict"):
        parts = []
        for field, msgs in exc.message_dict.items():
            joined = "; ".join(msgs)
            if field == "__all__":
                parts.append(joined)
            else:
                parts.append(f"{field}: {joined}")
        return " | ".join(parts)
    if hasattr(exc, "messages"):
        return "; ".join(exc.messages)
    return str(exc)


@login_required
def dashboard(request):
    tab = (request.GET.get("tab") or "").strip().lower()
    if tab == "reports":
        return reports(request)

    raw_period = (request.GET.get("period") or request.GET.get("month") or "").strip()
    ctx = get_dashboard_context(request.user, period=raw_period or None)
    tx_form = TransactionEntryForm(user=request.user)
    accessible_wallets = enrich_wallets_with_balances(
        get_accessible_wallets_qs(request.user, include_archived=False)
    )
    accessible_categories = list(
        get_accessible_categories_qs(request.user, include_archived=False)
    )
    return render(
        request,
        "budgets/dashboard.html",
        {
            "page_title": "Dashboard",
            "active_tab": "overview",
            "transaction_modal_enabled": True,
            "tx_form": tx_form,
            "accessible_wallets": accessible_wallets,
            "accessible_categories": accessible_categories,
            **ctx,
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def transactions(request):
    ws = get_active_workspace_context(request.user)
    tx_form = TransactionEntryForm(
        user=request.user,
        initial={"scope": ws["active_scope"]},
    )
    transfer_form = TransferEntryForm(user=request.user)
    category_form = CategoryForm(initial={"scope": ws["active_scope"]})

    if request.method == "POST":
        action = (request.POST.get("action") or "create_transaction").strip()
        try:
            if action == "create_transaction":
                tx_form = TransactionEntryForm(request.POST, user=request.user)
                if tx_form.is_valid():
                    cd = tx_form.cleaned_data
                    wallet = cd["wallet"]
                    scope = cd.get("scope") or wallet.ownership_scope
                    tx = create_transaction_entry(
                        user=request.user,
                        transaction_type=cd["transaction_type"],
                        amount=cd["amount"],
                        wallet=wallet,
                        transaction_date=cd["transaction_date"],
                        category=cd.get("category"),
                        scope=scope,
                        shared_space=wallet.shared_space if scope == "shared" else None,
                        description=cd.get("description") or "",
                        notes=cd.get("notes") or "",
                        paid_by=cd.get("paid_by") or request.user,
                        manual_rate=cd.get("manual_rate"),
                    )
                    messages.success(
                        request,
                        f'{tx.get_transaction_type_display()} of {tx.formatted_amount} {tx.currency.code} recorded.',
                    )
                    return redirect("budgets:transactions")
                else:
                    messages.error(request, _format_error_message(ValidationError(tx_form.errors)))

            elif action == "update_transaction":
                tx_id = request.POST.get("transaction_id")
                tx = get_object_or_404(Transaction, pk=tx_id)
                edit_form = TransactionEntryForm(
                    request.POST, user=request.user, include_archived=True
                )
                if edit_form.is_valid():
                    cd = edit_form.cleaned_data
                    expected_rev = request.POST.get("expected_revision") or None
                    updated = update_transaction_entry(
                        user=request.user,
                        tx=tx,
                        transaction_type=cd["transaction_type"],
                        amount=cd["amount"],
                        wallet=cd["wallet"],
                        transaction_date=cd["transaction_date"],
                        category=cd.get("category"),
                        description=cd.get("description") or "",
                        notes=cd.get("notes") or "",
                        paid_by=cd.get("paid_by"),
                        manual_rate=cd.get("manual_rate"),
                        expected_revision=expected_rev,
                    )
                    messages.success(
                        request,
                        f"Transaction updated ({updated.formatted_amount} {updated.currency.code}).",
                    )
                    return redirect("budgets:transactions")
                else:
                    messages.error(request, _format_error_message(ValidationError(edit_form.errors)))

            elif action == "delete_transaction":
                tx_id = request.POST.get("transaction_id")
                tx = get_object_or_404(Transaction, pk=tx_id)
                expected_rev = request.POST.get("expected_revision") or None
                delete_transaction_entry(
                    user=request.user,
                    tx=tx,
                    expected_revision=expected_rev,
                )
                messages.success(request, "Transaction has been deleted.")
                return redirect("budgets:transactions")

            elif action == "create_transfer":
                transfer_form = TransferEntryForm(request.POST, user=request.user)
                if transfer_form.is_valid():
                    cd = transfer_form.cleaned_data
                    tr = create_transfer_entry(
                        user=request.user,
                        source_wallet=cd["source_wallet"],
                        destination_wallet=cd["destination_wallet"],
                        amount_sent=cd["amount_sent"],
                        amount_received=cd.get("amount_received"),
                        manual_rate=cd.get("manual_rate"),
                        fee_amount=cd.get("fee_amount"),
                        transfer_date=cd["transfer_date"],
                        notes=cd.get("notes") or "",
                    )
                    messages.success(
                        request,
                        f"Transfer of {tr.formatted_amount_sent} {tr.source_currency.code} to {tr.destination_wallet.name} recorded.",
                    )
                    return redirect("budgets:transactions")
                else:
                    messages.error(request, _format_error_message(ValidationError(transfer_form.errors)))

            elif action == "update_transfer":
                transfer_id = request.POST.get("transfer_id")
                tr = get_object_or_404(Transfer, pk=transfer_id)
                edit_transfer_form = TransferEntryForm(
                    request.POST, user=request.user, include_archived=True
                )
                if edit_transfer_form.is_valid():
                    cd = edit_transfer_form.cleaned_data
                    expected_rev = request.POST.get("expected_revision") or None
                    updated_tr = update_transfer_entry(
                        user=request.user,
                        transfer_obj=tr,
                        source_wallet=cd["source_wallet"],
                        destination_wallet=cd["destination_wallet"],
                        amount_sent=cd["amount_sent"],
                        amount_received=cd.get("amount_received"),
                        manual_rate=cd.get("manual_rate"),
                        fee_amount=cd.get("fee_amount"),
                        transfer_date=cd["transfer_date"],
                        notes=cd.get("notes") or "",
                        expected_revision=expected_rev,
                    )
                    messages.success(
                        request,
                        f"Transfer updated ({updated_tr.formatted_amount_sent} {updated_tr.source_currency.code} to {updated_tr.destination_wallet.name}).",
                    )
                    return redirect("budgets:transactions")
                else:
                    messages.error(
                        request,
                        _format_error_message(ValidationError(edit_transfer_form.errors)),
                    )

            elif action == "delete_transfer":
                transfer_id = request.POST.get("transfer_id")
                tr = get_object_or_404(Transfer, pk=transfer_id)
                expected_rev = request.POST.get("expected_revision") or None
                delete_transfer_entry(
                    user=request.user,
                    transfer_obj=tr,
                    expected_revision=expected_rev,
                )
                messages.success(request, "Transfer and its linked ledger legs have been deleted.")
                return redirect("budgets:transactions")

            elif action == "create_category":
                category_form = CategoryForm(request.POST)
                if category_form.is_valid():
                    cd = category_form.cleaned_data
                    cat = create_category(
                        user=request.user,
                        name=cd["name"],
                        kind=cd["kind"],
                        scope=cd.get("scope") or "personal",
                        shared_space=ws["active_shared_space"] if cd.get("scope") == "shared" else None,
                    )
                    messages.success(request, f'Category "{cat.name}" created.')
                    return redirect("budgets:transactions")
                else:
                    messages.error(request, _format_error_message(ValidationError(category_form.errors)))

            elif action == "update_category":
                cat_id = request.POST.get("category_id")
                cat = get_object_or_404(Category, pk=cat_id)
                name = request.POST.get("name", "")
                kind = request.POST.get("kind", cat.kind)
                update_category(user=request.user, category=cat, name=name, kind=kind)
                messages.success(request, f'Category "{cat.name}" updated.')
                return redirect("budgets:transactions")

            elif action == "archive_category":
                cat_id = request.POST.get("category_id")
                cat = get_object_or_404(Category, pk=cat_id)
                archived_flag = request.POST.get("archived", "true").lower() != "false"
                archive_category(user=request.user, category=cat, archived=archived_flag)
                state_word = "archived" if archived_flag else "restored"
                messages.success(request, f'Category "{cat.name}" {state_word}.')
                return redirect("budgets:transactions")

            elif action == "share_personal_transaction":
                tx_id = request.POST.get("transaction_id")
                space_id = request.POST.get("shared_space_id")
                include_notes = request.POST.get("include_notes") in ("true", "1", "on", True)
                share = share_personal_transaction(
                    user=request.user,
                    transaction_id=tx_id,
                    shared_space_id=space_id,
                    include_notes=include_notes,
                )
                messages.success(
                    request,
                    f'Personal transaction shared with {share.recipient.username} in "{share.shared_space.name}".',
                )
                return redirect("budgets:transactions")

            elif action == "update_personal_transaction_share":
                share_id = request.POST.get("share_id")
                include_notes = request.POST.get("include_notes") in ("true", "1", "on", True)
                share = update_personal_transaction_share(
                    user=request.user,
                    share_id=share_id,
                    include_notes=include_notes,
                )
                status_str = "included" if share.include_notes else "hidden"
                messages.success(request, f"Sharing settings updated. Personal notes are now {status_str}.")
                return redirect("budgets:transactions")

            elif action == "revoke_personal_transaction_share":
                share_id = request.POST.get("share_id")
                tx_id = request.POST.get("transaction_id")
                space_id = request.POST.get("shared_space_id")
                revoke_personal_transaction_share(
                    user=request.user,
                    share_id=share_id,
                    transaction_id=tx_id,
                    shared_space_id=space_id,
                )
                messages.success(request, "Sharing stopped. Your partner can no longer view this personal transaction.")
                return redirect("budgets:transactions")

        except (ValidationError, PermissionDenied) as exc:
            messages.error(request, _format_error_message(exc))

    filtered = get_filtered_transactions(request.user, request.GET)

    default_display_currency = (
        ws.get("effective_display_currency")
        or ws.get("display_currency")
        or ws.get("effective_base_currency")
        or ws.get("personal_base_currency")
        or Currency.objects.filter(code="USD", is_active=True).first()
        or Currency.objects.filter(is_active=True).first()
    )
    default_display_code = default_display_currency.code if default_display_currency else ""

    active_filter_keys = []
    for key in ("q", "start_date", "end_date", "type", "scope", "category", "wallet", "currency"):
        val = (request.GET.get(key) or "").strip()
        if val:
            active_filter_keys.append(key)

    has_active_filters = len(active_filter_keys) > 0
    active_filters_count = len(active_filter_keys)

    display_currency = ws.get("display_currency")
    if not display_currency:
        display_currency_code = (request.GET.get("display_currency") or "").strip().upper()
        if display_currency_code:
            display_currency = Currency.objects.filter(code=display_currency_code, is_active=True).first()
    if not display_currency:
        display_currency = ws.get("effective_base_currency") or ws.get("personal_base_currency")

    if display_currency:
        filtered["filters"]["display_currency"] = display_currency.code

    rates_cache = {}
    enrich_transactions_with_display_currency(
        filtered["page_obj"].object_list,
        display_currency,
        user=request.user,
        rates_cache=rates_cache,
    )

    # Compute summary totals server-side using DB aggregation grouped by currency and transaction type
    total_income_dec = Decimal("0")
    total_expense_dec = Decimal("0")
    unconverted_count = 0

    if display_currency:
        summary_qs = filtered["queryset"].filter(
            transaction_type__in=[Transaction.Type.INCOME, Transaction.Type.EXPENSE]
        )
        grouped_totals = (
            summary_qs.values("currency_id", "scope", "shared_space_id", "transaction_type")
            .annotate(total_amount=Sum("amount"), tx_count=Count("id"))
        )

        currency_ids = {g["currency_id"] for g in grouped_totals}
        currencies_by_id = {c.id: c for c in Currency.objects.filter(id__in=currency_ids)}
        space_ids = {g["shared_space_id"] for g in grouped_totals if g["shared_space_id"]}
        spaces_by_id = {s.id: s for s in SharedSpace.objects.filter(id__in=space_ids)} if space_ids else {}

        user_id = getattr(request.user, "pk", None) or getattr(request.user, "id", None)

        for g in grouped_totals:
            curr_id = g["currency_id"]
            source_curr = currencies_by_id.get(curr_id)
            if not source_curr:
                unconverted_count += g["tx_count"]
                continue

            tx_type = g["transaction_type"]
            group_amount = g["total_amount"] or Decimal("0")
            tx_count = g["tx_count"]

            if curr_id == display_currency.id:
                converted_amt = group_amount
            else:
                g_scope = g["scope"] or "personal"
                g_space = spaces_by_id.get(g["shared_space_id"]) if g_scope == "shared" else None
                g_space_id = g_space.id if g_space else None
                cache_key = (
                    user_id,
                    curr_id,
                    display_currency.id,
                    g_scope,
                    g_space_id,
                )
                if cache_key not in rates_cache:
                    rates_cache[cache_key] = get_latest_rate(
                        source_curr,
                        display_currency,
                        user=request.user,
                        scope=g_scope,
                        shared_space=g_space,
                    )
                lookup = rates_cache[cache_key]
                if lookup.rate is not None:
                    converted_amt = convert_decimal_amount(group_amount, lookup.rate, display_currency)
                else:
                    converted_amt = None

            if converted_amt is not None:
                if tx_type == Transaction.Type.INCOME:
                    total_income_dec += converted_amt
                elif tx_type == Transaction.Type.EXPENSE:
                    total_expense_dec += converted_amt
            else:
                unconverted_count += tx_count

    net_dec = total_income_dec - total_expense_dec
    summary_totals = {
        "income": total_income_dec,
        "expense": total_expense_dec,
        "net": net_dec,
        "formatted_income": format_currency_amount(total_income_dec, display_currency) if display_currency else "0.00",
        "formatted_expense": format_currency_amount(total_expense_dec, display_currency) if display_currency else "0.00",
        "formatted_net": format_currency_amount(abs(net_dec), display_currency) if display_currency else "0.00",
        "unconverted_count": unconverted_count,
    }

    has_page_stale_rates = any(getattr(tx, "display_is_stale", False) for tx in filtered["page_obj"].object_list)
    hide_converted_column = bool(
        display_currency
        and filtered["total_count"] > 0
        and not filtered["queryset"].exclude(currency=display_currency).exists()
    )

    tx_ids = [tx.id for tx in filtered["page_obj"].object_list]
    active_shares_by_tx = get_active_shares_for_user_transactions(request.user, tx_ids)
    for tx in filtered["page_obj"].object_list:
        tx.active_personal_shares = active_shares_by_tx.get(tx.id, [])
    eligible_partner_spaces = get_eligible_partner_spaces_for_user(request.user)

    accessible_wallets = enrich_wallets_with_balances(
        get_accessible_wallets_qs(request.user, include_archived=False)
    )
    all_accessible_wallets = enrich_wallets_with_balances(
        get_accessible_wallets_qs(request.user, include_archived=True)
    )
    accessible_categories = list(
        get_accessible_categories_qs(request.user, include_archived=False)
    )
    all_categories = list(
        get_accessible_categories_qs(request.user, include_archived=True)
    )
    recent_transfers = list(
        get_accessible_transfers(
            request.user,
            limit=15,
            start_date=filtered["filters"].get("effective_start_date"),
            end_date=filtered["filters"].get("effective_end_date"),
        )
    )

    # Compute month bar navigation metadata
    today = timezone.localdate()
    cur_month_str = today.strftime("%Y-%m")
    month_mode = filtered["filters"].get("month_mode", "month")
    selected_month_str = filtered["filters"].get("month") or cur_month_str
    month_start = filtered["filters"].get("month_start")
    month_end = filtered["filters"].get("month_end")

    if month_start:
        # Prev month: 1st of month - 1 day gives previous month
        prev_month_date = date(month_start.year - (1 if month_start.month == 1 else 0), 12 if month_start.month == 1 else month_start.month - 1, 1)
        prev_month_str = prev_month_date.strftime("%Y-%m")
        # Next month
        next_month_date = date(month_start.year + (1 if month_start.month == 12 else 0), 1 if month_start.month == 12 else month_start.month + 1, 1)
        next_month_str = next_month_date.strftime("%Y-%m")
        is_current_month = (selected_month_str == cur_month_str)
        month_input_val = selected_month_str
        selected_month_label = month_start.strftime("%B %Y")
    else:
        prev_month_str = cur_month_str
        next_month_str = cur_month_str
        is_current_month = False
        month_input_val = cur_month_str
        selected_month_label = filtered["filters"].get("period_label", "All time")

    currencies_list = list(Currency.objects.filter(is_active=True).order_by("code"))

    return render(
        request,
        "budgets/transactions.html",
        {
            "page_title": "Transactions",
            "transaction_modal_enabled": True,
            "workspace": ws,
            "page_obj": filtered["page_obj"],
            "transactions_list": filtered["page_obj"].object_list,
            "display_currency": display_currency,
            "effective_display_currency": display_currency,
            "total_count": filtered["total_count"],
            "filters": filtered["filters"],
            "has_active_filters": has_active_filters,
            "active_filters_count": active_filters_count,
            "default_display_currency": default_display_currency,
            "accessible_wallets": accessible_wallets,
            "all_accessible_wallets": all_accessible_wallets,
            "accessible_categories": accessible_categories,
            "all_categories": all_categories,
            "recent_transfers": recent_transfers,
            "currencies": currencies_list,
            "eligible_partner_spaces": eligible_partner_spaces,
            "summary_totals": summary_totals,
            "has_page_stale_rates": has_page_stale_rates,
            "hide_converted_column": hide_converted_column,
            "tx_form": tx_form,
            "transfer_form": transfer_form,
            "category_form": category_form,
            "month_bar": {
                "mode": month_mode,
                "selected_month": selected_month_str,
                "selected_month_label": selected_month_label,
                "current_month": cur_month_str,
                "is_current_month": is_current_month,
                "prev_month": prev_month_str,
                "next_month": next_month_str,
                "month_input_val": month_input_val,
                "period_label": filtered["filters"].get("period_label"),
            },
        },
    )


@login_required
@require_GET
def export_transactions(request):
    fmt = (request.GET.get("format") or "csv").strip().lower()
    params = request.GET.copy()
    if params.get("all_time") in ("1", "true", "yes"):
        params["month"] = "all"
        params.pop("start_date", None)
        params.pop("end_date", None)
    rows = build_transactions_export_rows(request.user, params)

    if fmt == "json":
        response = HttpResponse(
            json.dumps({"transactions": rows}, indent=2),
            content_type="application/json; charset=utf-8",
        )
        response["Content-Disposition"] = 'attachment; filename="transactions_export.json"'
        return response

    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="transactions_export.csv"'
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
    for row in rows:
        writer.writerow({k: sanitize_spreadsheet_cell(row.get(k, "")) for k in fieldnames})
    return response


@login_required
@require_http_methods(["GET", "POST"])
def budgets(request):
    ws = get_active_workspace_context(request.user)
    default_currency = ws["effective_base_currency"]
    budget_form = CategoryBudgetForm(
        user=request.user,
        initial={
            "scope": ws["active_scope"],
            "currency": default_currency,
            "period": timezone.localdate().strftime("%Y-%m"),
        },
    )
    category_form = CategoryForm(
        initial={"scope": ws["active_scope"], "kind": Category.Kind.EXPENSE}
    )

    if request.method == "POST":
        action = (request.POST.get("action") or "save_budget").strip()
        try:
            if action == "save_budget":
                budget_form = CategoryBudgetForm(request.POST, user=request.user)
                if budget_form.is_valid():
                    cd = budget_form.cleaned_data
                    category = cd["category"]
                    scope = cd.get("scope") or category.scope
                    b = save_category_budget(
                        user=request.user,
                        category=category,
                        period=cd["period"],
                        currency=cd["currency"],
                        limit_amount=cd["limit_amount"],
                        scope=scope,
                        shared_space=category.shared_space if scope == "shared" else None,
                    )
                    messages.success(
                        request,
                        f'Budget for "{b.category.name}" ({b.period}) saved as {b.formatted_limit_amount} {b.currency.code}.',
                    )
                    return redirect(f"{request.path}?period={b.period}")
                else:
                    messages.error(request, _format_error_message(ValidationError(budget_form.errors)))

            elif action == "archive_budget":
                budget_id = request.POST.get("budget_id")
                b = get_object_or_404(CategoryBudget, pk=budget_id)
                archive_category_budget(user=request.user, budget=b)
                messages.success(request, f'Budget for "{b.category.name}" removed.')
                return redirect("budgets:budgets")

            elif action == "create_category":
                category_form = CategoryForm(request.POST)
                if category_form.is_valid():
                    cd = category_form.cleaned_data
                    cat = create_category(
                        user=request.user,
                        name=cd["name"],
                        kind=cd["kind"],
                        scope=cd.get("scope") or "personal",
                        shared_space=ws["active_shared_space"] if cd.get("scope") == "shared" else None,
                    )
                    messages.success(request, f'Category "{cat.name}" created.')
                    return redirect("budgets:budgets")
                else:
                    messages.error(request, _format_error_message(ValidationError(category_form.errors)))

        except (ValidationError, PermissionDenied) as exc:
            messages.error(request, _format_error_message(exc))

    period = (request.GET.get("period") or "").strip()
    scope_filter = (request.GET.get("scope") or "").strip()
    currency_filter = (request.GET.get("currency") or "").strip()

    overview = get_budget_overview_for_period(
        request.user,
        period=period or None,
        scope=scope_filter or None,
        currency_code=currency_filter or None,
    )
    currencies_list = list(Currency.objects.filter(is_active=True).order_by("code"))
    categories_list = list(
        get_accessible_categories_qs(request.user, include_archived=False)
    )

    try:
        p_year, p_month = [int(x) for x in overview["period"].split("-")]
        if p_month == 1:
            prev_period = f"{p_year - 1}-12"
        else:
            prev_period = f"{p_year}-{p_month - 1:02d}"
        if p_month == 12:
            next_period = f"{p_year + 1}-01"
        else:
            next_period = f"{p_year}-{p_month + 1:02d}"
    except Exception:
        prev_period = ""
        next_period = ""

    active_categories_count = len(overview["rows"])
    on_track_count = sum(1 for r in overview["rows"] if r["status_label"] == "On track")
    near_limit_count = sum(1 for r in overview["rows"] if r["status_label"] == "Near limit")
    over_budget_count = sum(1 for r in overview["rows"] if r["status_label"] == "Over budget")

    total_spent_pct = None
    if overview.get("summary_available") and overview.get("rows"):
        tot_b = sum((r["limit_amount"] for r in overview["rows"]), Decimal("0"))
        tot_s = sum((r["spent_amount"] for r in overview["rows"]), Decimal("0"))
        if tot_b > Decimal("0"):
            total_spent_pct = ((tot_s / tot_b) * Decimal("100")).quantize(Decimal("0.1"))

    for r in overview["rows"]:
        if r["limit_amount"] > Decimal("0"):
            rem_pct = ((r["remaining_amount"] / r["limit_amount"]) * Decimal("100")).quantize(Decimal("0.1"))
            r["remaining_pct"] = rem_pct
        else:
            r["remaining_pct"] = None

    return render(
        request,
        "budgets/budgets.html",
        {
            "page_title": "Budgets",
            "workspace": ws,
            "overview": overview,
            "budget_form": budget_form,
            "category_form": category_form,
            "currencies": currencies_list,
            "categories": categories_list,
            "prev_period": prev_period,
            "next_period": next_period,
            "active_categories_count": active_categories_count,
            "on_track_count": on_track_count,
            "near_limit_count": near_limit_count,
            "over_budget_count": over_budget_count,
            "total_spent_pct": total_spent_pct,
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
@cache_control(private=True, no_cache=True, no_store=True, must_revalidate=True)
def shared_space(request):
    tab = (request.GET.get("tab") or "").strip().lower()
    if tab == "activity":
        return shared_activity(request)
    elif tab in ("privacy", "sharing", "sharing_privacy"):
        return sharing_privacy(request)

    ws = get_active_workspace_context(request.user)
    memberships = list(get_user_active_memberships(request.user))
    current_space = ws["active_shared_space"] or (
        memberships[0].shared_space if memberships else None
    )

    space_id_raw = (request.GET.get("space") or "").strip()
    if space_id_raw:
        try:
            space_uuid = uuid.UUID(space_id_raw)
            matched = next((m.shared_space for m in memberships if m.shared_space.id == space_uuid), None)
            if matched:
                current_space = matched
        except (ValueError, TypeError):
            pass

    space_form = SharedSpaceForm(
        initial={
            "name": current_space.name if current_space else "",
            "base_currency": current_space.base_currency if current_space else ws["personal_base_currency"],
        }
    )
    invite_form = SharedSpaceInvitationForm()
    accept_form = AcceptInvitationForm()
    category_form = CategoryForm(initial={"scope": "shared"})

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()
        try:
            if action == "create_space":
                space_form = SharedSpaceForm(request.POST)
                if space_form.is_valid():
                    space = create_shared_space(
                        user=request.user,
                        name=space_form.cleaned_data["name"],
                        base_currency=space_form.cleaned_data["base_currency"],
                    )
                    pref = ws["preference"]
                    if pref:
                        pref.active_scope = "shared"
                        pref.active_shared_space = space
                        pref.save(update_fields=["active_scope", "active_shared_space", "updated_at"])
                    messages.success(
                        request,
                        f'Shared space "{space.name}" ({space.base_currency.code}) created.',
                    )
                    return redirect("budgets:shared_space")

            elif action == "update_space" and current_space:
                space_form = SharedSpaceForm(request.POST)
                if space_form.is_valid():
                    updated = update_shared_space(
                        user=request.user,
                        shared_space=current_space,
                        name=space_form.cleaned_data["name"],
                        base_currency=space_form.cleaned_data["base_currency"],
                    )
                    messages.success(
                        request,
                        f'Shared space "{updated.name}" updated ({updated.base_currency.code}).',
                    )
                    return redirect("budgets:shared_space")

            elif action == "create_invitation" and current_space:
                invite_form = SharedSpaceInvitationForm(request.POST)
                if invite_form.is_valid():
                    expiry_val = invite_form.cleaned_data.get("expiry") or "24h"
                    expires_in_days_val = invite_form.cleaned_data.get("expires_in_days")
                    inv = create_shared_space_invitation(
                        user=request.user,
                        shared_space=current_space,
                        partner_hint=invite_form.cleaned_data.get("partner_hint") or "",
                        expiry=expiry_val,
                        expires_in_days=expires_in_days_val if expires_in_days_val is not None else None,
                    )
                    request.session["newly_created_invite_id"] = str(inv.id)
                    messages.success(
                        request,
                        f"Invitation code generated: {inv.formatted_code} ({inv.code})",
                    )
                    return redirect("budgets:shared_space")

            elif action == "revoke_invitation":
                inv_id = request.POST.get("invitation_id")
                inv = get_object_or_404(SharedSpaceInvitation, pk=inv_id)
                revoke_shared_space_invitation(user=request.user, invitation=inv)
                messages.success(request, "Invitation has been revoked.")
                return redirect("budgets:shared_space")

            elif action == "accept_invitation":
                submitted_code = request.POST.get("code") or ""
                membership = accept_shared_space_invitation(
                    user=request.user,
                    code=submitted_code,
                    request=request,
                )
                pref = ws["preference"]
                if pref:
                    pref.active_scope = "shared"
                    pref.active_shared_space = membership.shared_space
                    pref.save(update_fields=["active_scope", "active_shared_space", "updated_at"])
                messages.success(
                    request,
                    f'Joined shared space "{membership.shared_space.name}".',
                )
                return redirect("budgets:shared_space")

            elif action == "leave_space" and current_space:
                leave_shared_space(user=request.user, shared_space=current_space)
                messages.success(
                    request,
                    f'You have left "{current_space.name}". Historical shared entries remain preserved.',
                )
                return redirect("budgets:shared_space")

            elif action == "create_shared_category" and current_space:
                category_form = CategoryForm(request.POST)
                if category_form.is_valid():
                    cat = create_category(
                        user=request.user,
                        name=category_form.cleaned_data["name"],
                        kind=category_form.cleaned_data["kind"],
                        scope="shared",
                        shared_space=current_space,
                    )
                    messages.success(request, f'Shared category "{cat.name}" created.')
                    return redirect("budgets:shared_space")

        except (ValidationError, PermissionDenied) as exc:
            messages.error(request, _format_error_message(exc))

    space_members = []
    space_invitations = []
    shared_wallets = []
    shared_categories = []
    shared_transactions = []
    is_creator = False

    if current_space:
        is_creator = current_space.created_by_id == request.user.id
        space_members = list(
            SharedSpaceMembership.objects.select_related("user")
            .filter(shared_space=current_space)
            .order_by("-is_active", "joined_at")
        )
        space_invitations = list(
            SharedSpaceInvitation.objects.select_related("created_by", "accepted_by")
            .filter(shared_space=current_space)
            .order_by("-created_at")[:10]
        )
        for inv in space_invitations:
            if inv.status == SharedSpaceInvitation.Status.PENDING and not inv.is_expired:
                inv.join_url = request.build_absolute_uri(f"{reverse('budgets:shared_space')}?code={inv.code}")
                inv.qr_svg = generate_invitation_qr_svg(inv.join_url, inv.created_by.username)
        shared_wallets = enrich_wallets_with_balances(
            Wallet.objects.filter(
                ownership_scope=Wallet.OwnershipScope.SHARED,
                shared_space=current_space,
                is_archived=False,
            ).select_related("currency", "owner")
        )
        shared_categories = list(
            Category.objects.filter(
                scope=Category.Scope.SHARED,
                shared_space=current_space,
                is_archived=False,
            ).order_by("name")
        )
        shared_transactions = list(
            Transaction.objects.filter(
                scope=Transaction.Scope.SHARED,
                shared_space=current_space,
                is_deleted=False,
            )
            .select_related("wallet", "currency", "category", "owner", "paid_by")
            .order_by("-transaction_date", "-created_at")[:20]
        )
        sp_page = request.GET.get("sp_page") or 1
        shared_personal_activity = get_shared_personal_activity_for_space(
            request.user, current_space, page=sp_page
        )
        today = timezone.localdate()
        cur_period = today.strftime("%Y-%m")
        period_start = date(today.year, today.month, 1)
        if today.month == 12:
            period_end = date(today.year + 1, 1, 1) - timezone.timedelta(days=1)
        else:
            period_end = date(today.year, today.month + 1, 1) - timezone.timedelta(days=1)

        this_month_shared_txs = [
            tx for tx in shared_transactions
            if period_start <= tx.transaction_date <= period_end
        ]
        this_month_shared_spending = sum(
            (tx.amount for tx in this_month_shared_txs if tx.transaction_type == Transaction.Type.EXPENSE),
            Decimal("0.00")
        )
        this_month_shared_spending_display = format_currency_amount(
            this_month_shared_spending, current_space.base_currency
        )
        shared_budgets_overview = get_budget_overview_for_period(
            request.user,
            period=cur_period,
            scope="shared",
            shared_space=current_space,
        )
        shared_budgets = shared_budgets_overview.get("rows", [])
        cur_period_display = period_start.strftime("%B %Y")
        this_month_shared_txs_count = len(this_month_shared_txs)
    else:
        shared_personal_activity = None
        this_month_shared_spending_display = "—"
        this_month_shared_txs_count = 0
        shared_budgets = []
        cur_period_display = ""

    join_confirmation = None
    join_error = None
    is_locked = False
    lock_msg = ""

    raw_query_code = (request.GET.get("code") or "").strip()
    if raw_query_code:
        is_locked, lock_msg = check_invitation_rate_limit(request)
        if is_locked:
            join_error = lock_msg
            messages.error(request, lock_msg)
        else:
            compact = re.sub(r"[\s\-]", "", raw_query_code)
            lookup_code = compact.upper() if len(compact) == 6 else raw_query_code
            now = timezone.now()
            invitation = (
                SharedSpaceInvitation.objects.select_related("shared_space", "created_by", "shared_space__base_currency")
                .filter(Q(code=lookup_code) | Q(code=compact.upper()) | Q(code=raw_query_code))
                .first()
            )
            if not invitation or invitation.status != SharedSpaceInvitation.Status.PENDING or invitation.expires_at <= now:
                record_invitation_failure(request, raw_query_code)
                join_error = INVITATION_ERROR_MESSAGE
                messages.error(request, INVITATION_ERROR_MESSAGE)
            elif invitation.shared_space.created_by_id == request.user.id or invitation.created_by_id == request.user.id:
                join_error = "You cannot join your own shared space."
                messages.error(request, join_error)
            elif SharedSpaceMembership.objects.filter(shared_space=invitation.shared_space, user=request.user, is_active=True).exists():
                join_error = "You are already an active member of this shared space."
                messages.info(request, join_error)
            elif SharedSpaceMembership.objects.filter(shared_space=invitation.shared_space, is_active=True).count() >= SharedSpace.MAX_ACTIVE_MEMBERS:
                join_error = "This shared space has already reached its two-member limit."
                messages.error(request, join_error)
            else:
                join_confirmation = {
                    "invitation": invitation,
                    "space": invitation.shared_space,
                    "owner": invitation.created_by,
                    "code": raw_query_code,
                    "formatted_code": invitation.formatted_code,
                }

    newly_created_invite_id = request.session.pop("newly_created_invite_id", None)

    return render(
        request,
        "budgets/shared_space.html",
        {
            "page_title": "Shared Space",
            "join_confirmation": join_confirmation,
            "join_error": join_error,
            "is_locked": is_locked,
            "lock_msg": lock_msg,
            "newly_created_invite_id": newly_created_invite_id,
            "active_tab": "overview",
            "workspace": ws,
            "current_space": current_space,
            "selected_space": current_space,
            "is_creator": is_creator,
            "space_members": space_members,
            "active_member_count": sum(1 for m in space_members if m.is_active),
            "space_invitations": space_invitations,
            "shared_wallets": shared_wallets,
            "shared_categories": shared_categories,
            "shared_transactions": shared_transactions,
            "shared_personal_activity": shared_personal_activity,
            "space_form": space_form,
            "invite_form": invite_form,
            "accept_form": accept_form,
            "category_form": category_form,
            "currencies": list(Currency.objects.filter(is_active=True).order_by("code")),
            "this_month_shared_spending_display": this_month_shared_spending_display,
            "this_month_shared_txs_count": this_month_shared_txs_count,
            "shared_budgets": shared_budgets,
            "cur_period_display": cur_period_display,
            "active_spaces": [m.shared_space for m in memberships],
        },
    )


@login_required
@require_GET
@cache_control(private=True, no_cache=True, no_store=True, must_revalidate=True)
def shared_activity(request):
    ws = get_active_workspace_context(request.user)
    memberships = list(get_user_active_memberships(request.user))
    active_spaces = [m.shared_space for m in memberships]

    space_id_raw = (request.GET.get("space") or "").strip()
    selected_space = None
    if space_id_raw:
        try:
            space_uuid = uuid.UUID(space_id_raw)
            selected_space = next((s for s in active_spaces if s.id == space_uuid), None)
        except (ValueError, TypeError):
            pass

    if not selected_space:
        selected_space = ws.get("active_shared_space") or (active_spaces[0] if active_spaces else None)

    partner = None
    partner_balance = None
    viewer_balance = None

    display_currency = ws.get("display_currency")
    if not display_currency:
        display_currency_code = (request.GET.get("display_currency") or "").strip().upper()
        if display_currency_code:
            display_currency = Currency.objects.filter(code=display_currency_code, is_active=True).first()
    if not display_currency:
        display_currency = ws.get("effective_base_currency") or ws.get("personal_base_currency")

    if selected_space:
        partner_membership = (
            SharedSpaceMembership.objects.filter(
                shared_space=selected_space,
                is_active=True,
            )
            .exclude(user=request.user)
            .select_related("user")
            .first()
        )
        partner = partner_membership.user if partner_membership else None

        if partner:
            partner_balance = get_personal_balance_disclosure_for_space(
                viewer=request.user,
                shared_space=selected_space,
                target_user=partner,
                display_currency=display_currency,
            )
            viewer_balance = get_personal_balance_disclosure_for_space(
                viewer=request.user,
                shared_space=selected_space,
                target_user=request.user,
                display_currency=display_currency,
            )

    page_num = request.GET.get("page") or 1
    activity_data = get_filtered_shared_activity(
        user=request.user,
        shared_space=selected_space,
        query_params=request.GET,
        page=page_num,
    )

    return render(
        request,
        "budgets/shared_activity.html",
        {
            "page_title": "Shared Space — Activity",
            "active_tab": "activity",
            "workspace": ws,
            "selected_space": selected_space,
            "current_space": selected_space,
            "active_spaces": active_spaces,
            "partner": partner,
            "partner_balance": partner_balance,
            "viewer_balance": viewer_balance,
            "page_obj": activity_data["page_obj"],
            "activity_items": activity_data["page_obj"].object_list,
            "filters": activity_data["filters"],
            "total_count": activity_data["total_count"],
            "currencies": list(Currency.objects.filter(is_active=True).order_by("code")),
            "display_currency": display_currency,
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
@cache_control(private=True, no_cache=True, no_store=True, must_revalidate=True)
def sharing_privacy(request):
    ws = get_active_workspace_context(request.user)
    memberships = list(get_user_active_memberships(request.user))
    active_spaces = [m.shared_space for m in memberships]

    space_id_raw = (
        request.POST.get("shared_space_id") or request.GET.get("space") or ""
    ).strip()
    selected_space = None
    if space_id_raw:
        try:
            space_uuid = uuid.UUID(space_id_raw)
            selected_space = next((s for s in active_spaces if s.id == space_uuid), None)
        except (ValueError, TypeError):
            pass

    if not selected_space:
        selected_space = ws.get("active_shared_space") or (active_spaces[0] if active_spaces else None)

    partner = None
    if selected_space:
        partner_membership = (
            SharedSpaceMembership.objects.filter(
                shared_space=selected_space,
                is_active=True,
            )
            .exclude(user=request.user)
            .select_related("user")
            .first()
        )
        partner = partner_membership.user if partner_membership else None

    all_personal_wallets = enrich_wallets_with_balances(
        Wallet.objects.filter(
            owner=request.user,
            ownership_scope=Wallet.OwnershipScope.PERSONAL,
        ).select_related("currency").order_by("is_archived", "name")
    )

    perm = None
    if selected_space and partner:
        perm = PersonalBalanceSharePermission.objects.filter(
            owner=request.user,
            recipient=partner,
            shared_space=selected_space,
            is_revoked=False,
        ).first()

    preview_result = None
    is_preview = False
    submitted_show_balance = perm.show_balance if perm else False
    submitted_wallet_ids = [str(w.id) for w in perm.selected_wallets.all()] if perm else []

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()
        try:
            if not selected_space:
                raise ValidationError("Please select a shared space.")
            if not partner:
                raise ValidationError("This shared space has no active partner.")

            if action == "save_balance_permission":
                submitted_show_balance = request.POST.get("show_balance") in ("true", "1", "on", True)
                submitted_wallet_ids = request.POST.getlist("wallet_ids")
                save_personal_balance_share_permission(
                    user=request.user,
                    shared_space_id=selected_space.id,
                    show_balance=submitted_show_balance,
                    wallet_ids=submitted_wallet_ids,
                )
                messages.success(request, f'Sharing & Privacy settings saved for "{selected_space.name}".')
                return redirect(f"{reverse('budgets:sharing_privacy')}?space={selected_space.id}")

            elif action == "stop_sharing_balance":
                revoke_personal_balance_share_permission(
                    user=request.user,
                    shared_space_id=selected_space.id,
                )
                messages.success(request, f'Personal balance sharing stopped for "{selected_space.name}".')
                return redirect(f"{reverse('budgets:sharing_privacy')}?space={selected_space.id}")

            elif action == "preview_balance":
                is_preview = True
                submitted_show_balance = request.POST.get("show_balance") in ("true", "1", "on", True)
                submitted_wallet_ids = request.POST.getlist("wallet_ids")

                chosen_wallets = []
                for wid in submitted_wallet_ids:
                    matched = next((w for w in all_personal_wallets if str(w.id) == str(wid)), None)
                    if matched and not matched.is_archived:
                        chosen_wallets.append(matched)

                preview_perm = SimpleNamespace(
                    show_balance=submitted_show_balance,
                    selected_wallets=chosen_wallets,
                )
                partner_display = resolve_user_effective_display_currency(
                    partner,
                    shared_space=selected_space,
                )

                preview_result = get_personal_balance_disclosure_for_space(
                    viewer=request.user,
                    shared_space=selected_space,
                    target_user=request.user,
                    display_currency=partner_display,
                    preview_permission=preview_perm,
                    rate_policy="preview",
                )

        except (ValidationError, PermissionDenied) as exc:
            messages.error(request, _format_error_message(exc))

    ineligible_selected_wallets = []
    if perm:
        ineligible_selected_wallets = [w for w in perm.selected_wallets.all() if w.is_archived]

    shared_tx_grants = []
    if selected_space:
        shared_tx_grants = list(
            PersonalTransactionShare.objects.filter(
                owner=request.user,
                shared_space=selected_space,
                is_revoked=False,
                transaction__is_deleted=False,
            )
            .select_related("transaction", "transaction__currency", "recipient")
            .order_by("-transaction__transaction_date")
        )

    return render(
        request,
        "budgets/sharing_privacy.html",
        {
            "page_title": "Shared Space — Privacy & sharing",
            "active_tab": "privacy",
            "workspace": ws,
            "selected_space": selected_space,
            "current_space": selected_space,
            "active_spaces": active_spaces,
            "partner": partner,
            "perm": perm,
            "all_personal_wallets": all_personal_wallets,
            "submitted_show_balance": submitted_show_balance,
            "submitted_wallet_ids": submitted_wallet_ids,
            "ineligible_selected_wallets": ineligible_selected_wallets,
            "shared_tx_grants": shared_tx_grants,
            "preview_result": preview_result,
            "is_preview": is_preview,
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def wallets(request):
    ws = get_active_workspace_context(request.user)
    has_active_currencies = Currency.objects.filter(is_active=True).exists()
    form = WalletForm(user=request.user)

    if request.method == "POST":
        action = (request.POST.get("action") or "create_wallet").strip()
        try:
            if action == "create_wallet":
                form = WalletForm(request.POST, user=request.user)
                if form.is_valid() and has_active_currencies:
                    cd = form.cleaned_data
                    scope = cd.get("ownership_scope") or Wallet.OwnershipScope.PERSONAL
                    shared_space_obj = (
                        cd.get("shared_space") or ws["active_shared_space"]
                        if scope == Wallet.OwnershipScope.SHARED
                        else None
                    )
                    wallet = create_wallet_record(
                        user=request.user,
                        name=cd["name"],
                        wallet_type=cd["wallet_type"],
                        currency=cd["currency"],
                        opening_balance=cd["opening_balance"],
                        opening_balance_date=cd["opening_balance_date"],
                        ownership_scope=scope,
                        shared_space=shared_space_obj,
                    )
                    messages.success(request, f'Wallet "{wallet.name}" has been created.')
                    return redirect("budgets:wallets")

            elif action == "update_wallet":
                wallet_id = request.POST.get("wallet_id")
                wallet = get_object_or_404(Wallet, pk=wallet_id)
                name = request.POST.get("name", wallet.name)
                wallet_type = request.POST.get("wallet_type", wallet.wallet_type)
                currency = None
                if request.POST.get("currency"):
                    currency = Currency.objects.filter(pk=request.POST.get("currency")).first()
                opening_balance = None
                if request.POST.get("opening_balance") not in (None, ""):
                    opening_balance_raw = str(request.POST.get("opening_balance")).strip()
                    if "," in opening_balance_raw:
                        if not re.match(r"^[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?$", opening_balance_raw):
                            raise ValidationError("Enter a valid decimal amount.")
                        opening_balance_raw = opening_balance_raw.replace(",", "")
                    try:
                        opening_balance = Decimal(opening_balance_raw)
                    except (InvalidOperation, ValueError):
                        raise ValidationError("Enter a valid decimal amount.")
                opening_balance_date = None
                if request.POST.get("opening_balance_date"):
                    opening_balance_date = datetime.strptime(
                        request.POST.get("opening_balance_date"), "%Y-%m-%d"
                    ).date()

                updated = update_wallet_record(
                    user=request.user,
                    wallet=wallet,
                    name=name,
                    wallet_type=wallet_type,
                    currency=currency,
                    opening_balance=opening_balance,
                    opening_balance_date=opening_balance_date,
                )
                messages.success(request, f'Wallet "{updated.name}" updated.')
                return redirect("budgets:wallets")

            elif action in ("archive_wallet", "unarchive_wallet"):
                wallet_id = request.POST.get("wallet_id")
                wallet = get_object_or_404(Wallet, pk=wallet_id)
                archive_flag = action == "archive_wallet"
                updated = update_wallet_record(
                    user=request.user,
                    wallet=wallet,
                    name=wallet.name,
                    wallet_type=wallet.wallet_type,
                    is_archived=archive_flag,
                )
                state_word = "archived" if archive_flag else "restored"
                messages.success(request, f'Wallet "{updated.name}" {state_word}.')
                return redirect("budgets:wallets")

        except (ValidationError, PermissionDenied, ValueError) as exc:
            messages.error(request, _format_error_message(exc))

    all_wallets = enrich_wallets_with_balances(
        get_accessible_wallets_qs(request.user, include_archived=True)
    )
    active_wallets = [w for w in all_wallets if not w.is_archived]
    archived_wallets = [w for w in all_wallets if w.is_archived]

    return render(
        request,
        "budgets/wallets.html",
        {
            "page_title": "Wallets",
            "workspace": ws,
            "form": form,
            "wallets": active_wallets,
            "archived_wallets": archived_wallets,
            "has_active_currencies": has_active_currencies,
            "wallet_types": Wallet.WalletType.choices,
            "currencies": list(Currency.objects.filter(is_active=True).order_by("code")),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def savings_goals(request):
    ws = get_active_workspace_context(request.user)
    goal_form = SavingsGoalForm(
        initial={
            "scope": ws["active_scope"],
            "currency": ws["effective_base_currency"],
        }
    )
    contribution_form = SavingsContributionForm(user=request.user)

    if request.method == "POST":
        action = (request.POST.get("action") or "create_goal").strip()
        try:
            if action == "create_goal":
                goal_form = SavingsGoalForm(request.POST)
                if goal_form.is_valid():
                    cd = goal_form.cleaned_data
                    scope = cd.get("scope") or "personal"
                    goal = create_savings_goal(
                        user=request.user,
                        name=cd["name"],
                        currency=cd["currency"],
                        target_amount=cd["target_amount"],
                        target_date=cd.get("target_date"),
                        scope=scope,
                        shared_space=ws["active_shared_space"] if scope == "shared" else None,
                    )
                    messages.success(
                        request,
                        f'Savings goal "{goal.name}" ({goal.formatted_target_amount} {goal.currency.code}) created.',
                    )
                    return redirect("budgets:savings_goals")
                else:
                    messages.error(request, _format_error_message(ValidationError(goal_form.errors)))

            elif action == "update_goal":
                goal_id = request.POST.get("goal_id")
                goal = get_object_or_404(SavingsGoal, pk=goal_id)
                edit_goal_form = SavingsGoalForm(request.POST)
                if edit_goal_form.is_valid():
                    cd = edit_goal_form.cleaned_data
                    expected_rev = request.POST.get("expected_revision") or None
                    updated_goal = update_savings_goal(
                        user=request.user,
                        goal=goal,
                        name=cd["name"],
                        target_amount=cd["target_amount"],
                        target_date=cd.get("target_date"),
                        currency=cd["currency"],
                        expected_revision=expected_rev,
                    )
                    messages.success(
                        request,
                        f'Savings goal "{updated_goal.name}" ({updated_goal.formatted_target_amount} {updated_goal.currency.code}) updated.',
                    )
                    return redirect("budgets:savings_goals")
                else:
                    messages.error(
                        request,
                        _format_error_message(ValidationError(edit_goal_form.errors)),
                    )

            elif action == "record_contribution":
                contribution_form = SavingsContributionForm(request.POST, user=request.user)
                if contribution_form.is_valid():
                    cd = contribution_form.cleaned_data
                    c = record_savings_contribution(
                        user=request.user,
                        goal=cd["goal"],
                        amount=cd["amount"],
                        contribution_date=cd["contribution_date"],
                        entry_type=cd["entry_type"],
                        notes=cd.get("notes") or "",
                    )
                    messages.success(
                        request,
                        f'{c.get_entry_type_display()} of {c.formatted_amount} {c.goal.currency.code} recorded for "{c.goal.name}".',
                    )
                    return redirect("budgets:savings_goals")
                else:
                    messages.error(
                        request, _format_error_message(ValidationError(contribution_form.errors))
                    )

            elif action == "archive_goal":
                goal_id = request.POST.get("goal_id")
                goal = get_object_or_404(SavingsGoal, pk=goal_id)
                archive_savings_goal(user=request.user, goal=goal)
                messages.success(request, f'Savings goal "{goal.name}" archived.')
                return redirect("budgets:savings_goals")

        except (ValidationError, PermissionDenied) as exc:
            messages.error(request, _format_error_message(exc))

    scope_filter = (request.GET.get("scope") or "").strip()
    overview = get_savings_goals_overview(
        request.user, scope=scope_filter if scope_filter in ("personal", "shared") else None
    )
    contribution_form = SavingsContributionForm(user=request.user)

    return render(
        request,
        "budgets/savings_goals.html",
        {
            "page_title": "Savings Goals",
            "workspace": ws,
            "overview": overview,
            "goal_form": goal_form,
            "contribution_form": contribution_form,
            "currencies": list(Currency.objects.filter(is_active=True).order_by("code")),
        },
    )


@login_required
def reports(request):
    ctx = get_reports_context(request.user, request.GET)
    currencies_list = list(Currency.objects.filter(is_active=True).order_by("code"))
    return render(
        request,
        "budgets/reports.html",
        {
            "page_title": "Dashboard — Reports",
            "active_tab": "reports",
            "currencies": currencies_list,
            **ctx,
        },
    )


def get_currencies_context(request, post_data=None):
    ws = get_active_workspace_context(request.user)
    preference = get_cached_user_preference(request)
    supported_currencies = list(Currency.objects.filter(is_active=True).order_by("code"))
    has_active_currencies = bool(supported_currencies)

    form = PersonalBaseCurrencyForm(
        post_data if (post_data and post_data.get("action") == "save_base_currency") else None,
        instance=preference,
    )
    converter_form = CurrencyConverterForm(
        post_data if (post_data and post_data.get("action") == "convert_currency") else (request.GET if request.GET.get("convert") else None),
        initial={"scope": ws["active_scope"]},
    )
    manual_rate_form = ManualExchangeRateForm(
        post_data if (post_data and post_data.get("action") == "record_manual_rate") else None,
        initial={"scope": ws["active_scope"]},
    )
    conversion_result = None
    table_source_currency = ws.get("effective_display_currency")

    saved_base_currency = (
        preference.personal_base_currency
        if preference and preference.personal_base_currency_id
        else None
    )
    provider_info = get_configured_provider_info()
    recent_rates = list(get_accessible_exchange_rates_qs(request.user)[:20])

    rate_table_rows = []
    if table_source_currency:
        for target_curr in supported_currencies:
            if target_curr.id == table_source_currency.id:
                continue
            lookup = get_latest_rate(
                table_source_currency,
                target_curr,
                user=request.user,
                scope=ws["active_scope"],
                shared_space=ws["active_shared_space"],
            )
            rate_table_rows.append(
                {
                    "target_currency": target_curr,
                    "rate": lookup.rate,
                    "is_available": lookup.rate is not None,
                    "is_manual": lookup.is_manual,
                    "is_stale": lookup.is_stale,
                    "status": lookup.status,
                    "status_label": lookup.status_label,
                    "provider": lookup.provider,
                    "rate_timestamp": lookup.rate_timestamp,
                    "retrieved_at": lookup.retrieved_at,
                }
            )

    return {
        "workspace": ws,
        "preference": preference,
        "form": form,
        "currency_form": form,
        "converter_form": converter_form,
        "conversion_result": conversion_result,
        "manual_rate_form": manual_rate_form,
        "supported_currencies": supported_currencies,
        "has_active_currencies": has_active_currencies,
        "saved_base_currency": saved_base_currency,
        "table_source_currency": table_source_currency,
        "rate_table_rows": rate_table_rows,
        "provider_info": provider_info,
        "recent_rates": recent_rates,
    }


@login_required
@require_http_methods(["GET", "POST"])
def currencies(request):
    curr_ctx = get_currencies_context(request)
    ws = curr_ctx["workspace"]
    preference = curr_ctx["preference"]
    has_active_currencies = curr_ctx["has_active_currencies"]
    table_source_currency = curr_ctx["table_source_currency"]

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()
        if not action and "personal_base_currency" in request.POST:
            action = "save_base_currency"

        try:
            if action == "save_base_currency":
                form = PersonalBaseCurrencyForm(request.POST, instance=preference)
                if form.is_valid() and has_active_currencies:
                    saved_pref = form.save(commit=False)
                    saved_pref.user = request.user
                    saved_pref.full_clean()
                    saved_pref.save()
                    request._cached_user_preference = saved_pref
                    messages.success(
                        request,
                        f"Personal base currency saved as {saved_pref.personal_base_currency.code} ({saved_pref.personal_base_currency.name}).",
                    )
                    return redirect("budgets:currencies")
                else:
                    curr_ctx["form"] = form
                    curr_ctx["currency_form"] = form

            elif action == "convert_currency":
                converter_form = CurrencyConverterForm(request.POST)
                if converter_form.is_valid():
                    cd = converter_form.cleaned_data
                    amt = cd["amount"]
                    from_curr = cd["from_currency"]
                    to_curr = cd["to_currency"]
                    conv_scope = cd.get("scope") or ws["active_scope"] or "personal"
                    conv_space = ws["active_shared_space"] if conv_scope == "shared" else None
                    lookup = get_latest_rate(
                        from_curr,
                        to_curr,
                        user=request.user,
                        scope=conv_scope,
                        shared_space=conv_space,
                    )
                    if lookup.rate is not None:
                        converted = convert_decimal_amount(amt, lookup.rate, to_curr)
                        curr_ctx["conversion_result"] = {
                            "available": True,
                            "from_currency": from_curr,
                            "to_currency": to_curr,
                            "formatted_input": format_currency_amount(amt, from_curr),
                            "formatted_output": format_currency_amount(converted, to_curr),
                            "rate": lookup.rate,
                            "provider": lookup.provider,
                            "rate_timestamp": lookup.rate_timestamp,
                            "is_stale": lookup.is_stale,
                            "status_label": lookup.status_label,
                            "scope": conv_scope,
                        }
                    else:
                        curr_ctx["conversion_result"] = {
                            "available": False,
                            "from_currency": from_curr,
                            "to_currency": to_curr,
                            "status_label": lookup.status_label,
                            "scope": conv_scope,
                        }
                curr_ctx["converter_form"] = converter_form

            elif action == "record_manual_rate":
                manual_rate_form = ManualExchangeRateForm(request.POST)
                if manual_rate_form.is_valid():
                    cd = manual_rate_form.cleaned_data
                    rate_dt = None
                    if cd.get("rate_date"):
                        rate_dt = timezone.make_aware(
                            datetime.combine(cd["rate_date"], dt_time(12, 0))
                        )
                    rate_scope = cd.get("scope") or "personal"
                    rate_space = ws["active_shared_space"] if rate_scope == "shared" else None
                    rec = record_manual_exchange_rate(
                        from_currency=cd["from_currency"],
                        to_currency=cd["to_currency"],
                        rate=cd["rate"],
                        user=request.user,
                        scope=rate_scope,
                        shared_space=rate_space,
                        rate_timestamp=rate_dt,
                    )
                    messages.success(
                        request,
                        f"Manual exchange rate ({rec.get_scope_display()}) saved: 1 {rec.from_currency.code} = {rec.rate} {rec.to_currency.code}.",
                    )
                    return redirect("budgets:currencies")
                else:
                    curr_ctx["manual_rate_form"] = manual_rate_form
                    messages.error(
                        request, _format_error_message(ValidationError(manual_rate_form.errors))
                    )

            elif action == "refresh_rates":
                base_code = table_source_currency.code if table_source_currency else "PHP"
                result = refresh_exchange_rates(base_currency_code=base_code, force=False)
                if result["status"] == "updated":
                    messages.success(request, result["message"])
                elif result["status"] == "cooldown":
                    messages.info(request, result["message"])
                else:
                    messages.warning(request, result["message"])
                return redirect("budgets:currencies")

        except (ValidationError, PermissionDenied) as exc:
            messages.error(request, _format_error_message(exc))

    from apps.accounts.forms import ProfileDisplayNameForm, UserPreferencesForm
    display_name = request.user.get_full_name().strip() or request.user.username
    profile_form = ProfileDisplayNameForm(initial={"display_name": display_name})
    preferences_form = UserPreferencesForm(instance=preference)

    context = {
        "page_title": "Settings — Currency",
        "active_tab": "currency",
        "profile_display_name": display_name,
        "profile_email": request.user.email or "",
        "profile_form": profile_form,
        "preferences_form": preferences_form,
        **curr_ctx,
    }
    return render(request, "accounts/settings.html", context)


def offline_page(request):
    """
    Renders an account-neutral offline shell without RequestContext so no
    username, memberships, private balances, session controls, or CSRF tokens
    can ever be embedded into the service-worker-cached /offline/ response.
    """
    template = template_loader.get_template("offline.html")
    html = template.render({"page_title": "Offline"})
    return HttpResponse(html, content_type="text/html; charset=utf-8")


_SW_CACHE_MAP = {}


def compute_service_worker_cache_name(base_dir: Path | None = None) -> str:
    """
    Compute the deterministic service-worker CACHE_NAME from the SHA-256 hash
    of the precached files and offline template so any change to a precached
    file automatically rotates the service-worker cache name.

    Results are cached by file mtimes and sizes to avoid recomputing the hash
    on every request. When STATIC_ROOT is configured and populated (collectstatic),
    the served files in STATIC_ROOT are hashed.
    """
    import hashlib
    import re

    if base_dir is None:
        base_dir = Path(django_settings.BASE_DIR)
    else:
        base_dir = Path(base_dir)

    static_root_setting = getattr(django_settings, "STATIC_ROOT", None)
    static_root = Path(static_root_setting) if static_root_setting else None

    # Service worker JS path: prefer STATIC_ROOT if present, else base_dir/static
    sw_path = None
    if static_root and (static_root / "js" / "service-worker.js").exists():
        sw_path = static_root / "js" / "service-worker.js"
    elif (base_dir / "static" / "js" / "service-worker.js").exists():
        sw_path = base_dir / "static" / "js" / "service-worker.js"

    urls = []
    if sw_path and sw_path.exists():
        sw_text = sw_path.read_text(encoding="utf-8")
        urls = re.findall(r'"(/static/[^"?]+)(?:\?[^"]*)?"', sw_text)

    resolved_assets = []
    seen = set()
    for url_path in urls:
        if url_path in seen:
            continue
        seen.add(url_path)
        rel_under_static = url_path[len("/static/"):].lstrip("/") if url_path.startswith("/static/") else url_path.lstrip("/")
        asset_file = None
        if static_root and (static_root / rel_under_static).exists():
            asset_file = static_root / rel_under_static
        elif (base_dir / "static" / rel_under_static).exists():
            asset_file = base_dir / "static" / rel_under_static
        elif (base_dir / url_path.lstrip("/")).exists():
            asset_file = base_dir / url_path.lstrip("/")
        resolved_assets.append((url_path, asset_file))

    offline_tpl = base_dir / "templates" / "offline.html"

    def file_sig(p):
        if p and p.exists():
            try:
                st = p.stat()
                return (str(p), st.st_mtime_ns, st.st_size)
            except OSError:
                return (str(p), 0, 0)
        return (str(p) if p else None, 0, 0)

    sig = (
        file_sig(sw_path),
        file_sig(offline_tpl),
        tuple((u, file_sig(f)) for u, f in resolved_assets),
    )
    cache_key = (str(base_dir), str(static_root) if static_root else "")
    cached = _SW_CACHE_MAP.get(cache_key)
    if cached and cached.get("sig") == sig:
        return cached["cache_name"]

    hasher = hashlib.sha256()
    for url_path, asset_file in resolved_assets:
        hasher.update(url_path.encode("utf-8"))
        if asset_file and asset_file.exists():
            hasher.update(asset_file.read_bytes())

    if offline_tpl and offline_tpl.exists():
        hasher.update(b"/offline/")
        hasher.update(offline_tpl.read_bytes())

    digest = hasher.hexdigest()[:10]
    cache_name = f"budget-tracker-shell-v12-{digest}"
    _SW_CACHE_MAP[cache_key] = {"sig": sig, "cache_name": cache_name}
    return cache_name


@require_GET
def service_worker(request):
    import re

    static_root_setting = getattr(django_settings, "STATIC_ROOT", None)
    static_root = Path(static_root_setting) if static_root_setting else None
    sw_path = None
    if static_root and (static_root / "js" / "service-worker.js").exists():
        sw_path = static_root / "js" / "service-worker.js"
    elif (Path(django_settings.BASE_DIR) / "static" / "js" / "service-worker.js").exists():
        sw_path = Path(django_settings.BASE_DIR) / "static" / "js" / "service-worker.js"

    content = sw_path.read_text(encoding="utf-8") if sw_path and sw_path.exists() else ""
    cache_name = compute_service_worker_cache_name()
    content = re.sub(
        r'const CACHE_NAME = "budget-tracker-shell-[^"]+";',
        f'const CACHE_NAME = "{cache_name}";',
        content,
        count=1,
    )
    response = HttpResponse(content, content_type="application/javascript; charset=utf-8")
    response["Service-Worker-Allowed"] = "/"
    response["Cache-Control"] = "no-cache"
    return response


@login_required
@require_GET
def sync_state_api(request):
    since = request.GET.get("since")
    limit = request.GET.get("limit", 200)
    offset = request.GET.get("offset", 0)
    snapshot = get_sync_snapshot_for_user(
        request.user,
        since=since,
        limit=limit,
        offset=offset,
    )
    return JsonResponse(snapshot)


@login_required
@require_POST
def sync_mutations_api(request):
    try:
        body = json.loads(request.body.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        return JsonResponse(
            {"error": "Invalid JSON payload.", "batch_status": "failed"},
            status=400,
        )

    if not isinstance(body, dict):
        return JsonResponse(
            {"error": "Top-level JSON payload must be an object.", "batch_status": "failed"},
            status=400,
        )

    mutations = body.get("mutations")
    if not isinstance(mutations, list):
        return JsonResponse(
            {"error": '"mutations" must be a JSON list.', "batch_status": "failed"},
            status=400,
        )

    expected_user_id = body.get("expected_user_id")
    is_final_batch = bool(body.get("is_final_batch", True))
    try:
        outcome = process_sync_batch(
            user=request.user,
            mutations=mutations,
            expected_user_id=expected_user_id,
            is_final_batch=is_final_batch,
        )
    except ValidationError as exc:
        return JsonResponse(
            {"error": _format_error_message(exc), "batch_status": "failed"},
            status=400,
        )

    if outcome.get("batch_status") == "user_mismatch":
        return JsonResponse(outcome, status=409)

    return JsonResponse(outcome)
