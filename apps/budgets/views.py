import csv
from datetime import datetime, time as dt_time
from decimal import Decimal
import json
from pathlib import Path
from types import SimpleNamespace
import uuid

from django.conf import settings as django_settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
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
    accept_shared_space_invitation,
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
    ctx = get_dashboard_context(request.user)
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

    display_currency = ws.get("display_currency")
    if not display_currency:
        display_currency_code = (request.GET.get("display_currency") or "").strip().upper()
        if display_currency_code:
            display_currency = Currency.objects.filter(code=display_currency_code, is_active=True).first()
    if not display_currency:
        display_currency = ws.get("effective_base_currency") or ws.get("personal_base_currency")

    if display_currency:
        filtered["filters"]["display_currency"] = display_currency.code

    enrich_transactions_with_display_currency(
        filtered["page_obj"].object_list,
        display_currency,
        user=request.user,
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
    recent_transfers = list(get_accessible_transfers(request.user, limit=15))
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
            "total_count": filtered["total_count"],
            "filters": filtered["filters"],
            "accessible_wallets": accessible_wallets,
            "all_accessible_wallets": all_accessible_wallets,
            "accessible_categories": accessible_categories,
            "all_categories": all_categories,
            "recent_transfers": recent_transfers,
            "currencies": currencies_list,
            "eligible_partner_spaces": eligible_partner_spaces,
            "tx_form": tx_form,
            "transfer_form": transfer_form,
            "category_form": category_form,
        },
    )


@login_required
@require_GET
def export_transactions(request):
    fmt = (request.GET.get("format") or "csv").strip().lower()
    rows = build_transactions_export_rows(request.user, request.GET)

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
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
@cache_control(private=True, no_cache=True, no_store=True, must_revalidate=True)
def shared_space(request):
    ws = get_active_workspace_context(request.user)
    memberships = list(get_user_active_memberships(request.user))
    current_space = ws["active_shared_space"] or (
        memberships[0].shared_space if memberships else None
    )

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
                    inv = create_shared_space_invitation(
                        user=request.user,
                        shared_space=current_space,
                        partner_hint=invite_form.cleaned_data.get("partner_hint") or "",
                        expires_in_days=invite_form.cleaned_data.get("expires_in_days") or 7,
                    )
                    messages.success(
                        request,
                        f"Invitation code generated: {inv.code}",
                    )
                    return redirect("budgets:shared_space")

            elif action == "revoke_invitation":
                inv_id = request.POST.get("invitation_id")
                inv = get_object_or_404(SharedSpaceInvitation, pk=inv_id)
                revoke_shared_space_invitation(user=request.user, invitation=inv)
                messages.success(request, "Invitation has been revoked.")
                return redirect("budgets:shared_space")

            elif action == "accept_invitation":
                accept_form = AcceptInvitationForm(request.POST)
                if accept_form.is_valid():
                    membership = accept_shared_space_invitation(
                        user=request.user,
                        code=accept_form.cleaned_data["code"],
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
    else:
        shared_personal_activity = None

    return render(
        request,
        "budgets/shared_space.html",
        {
            "page_title": "Shared Space",
            "workspace": ws,
            "current_space": current_space,
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
            "page_title": "Shared Activity",
            "workspace": ws,
            "selected_space": selected_space,
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
            "page_title": "Sharing & Privacy",
            "workspace": ws,
            "selected_space": selected_space,
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
                    opening_balance = Decimal(str(request.POST.get("opening_balance")))
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
            "page_title": "Reports",
            "currencies": currencies_list,
            **ctx,
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def currencies(request):
    ws = get_active_workspace_context(request.user)
    preference = get_cached_user_preference(request)
    supported_currencies = list(Currency.objects.filter(is_active=True).order_by("code"))
    has_active_currencies = bool(supported_currencies)

    form = PersonalBaseCurrencyForm(instance=preference)
    converter_form = CurrencyConverterForm(
        request.GET if request.GET.get("convert") else None,
        initial={"scope": ws["active_scope"]},
    )
    manual_rate_form = ManualExchangeRateForm(initial={"scope": ws["active_scope"]})
    conversion_result = None
    table_source_currency = ws.get("effective_display_currency")

    if request.method == "POST":
        action = (request.POST.get("action") or "save_base_currency").strip()
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
                        conversion_result = {
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
                        conversion_result = {
                            "available": False,
                            "from_currency": from_curr,
                            "to_currency": to_curr,
                            "status_label": lookup.status_label,
                            "scope": conv_scope,
                        }

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

    return render(
        request,
        "budgets/currencies.html",
        {
            "page_title": "Currencies",
            "workspace": ws,
            "form": form,
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
        },
    )


def offline_page(request):
    """
    Renders an account-neutral offline shell without RequestContext so no
    username, memberships, private balances, session controls, or CSRF tokens
    can ever be embedded into the service-worker-cached /offline/ response.
    """
    template = template_loader.get_template("offline.html")
    html = template.render({"page_title": "Offline"})
    return HttpResponse(html, content_type="text/html; charset=utf-8")


@require_GET
def service_worker(request):
    sw_path = Path(django_settings.BASE_DIR) / "static" / "js" / "service-worker.js"
    content = sw_path.read_text(encoding="utf-8") if sw_path.exists() else ""
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
