from collections import defaultdict
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from django.core.paginator import Paginator
from django.db.models import F, Q, Sum
from django.utils import timezone

from apps.accounts.models import UserPreference

from .exchange_rates import convert_decimal_amount, get_latest_rate
from .models import (
    Category,
    CategoryBudget,
    Currency,
    ExchangeRate,
    PersonalBalanceSharePermission,
    PersonalTransactionShare,
    SavingsContribution,
    SavingsGoal,
    SharedSpace,
    SharedSpaceInvitation,
    SharedSpaceMembership,
    Transaction,
    Transfer,
    Wallet,
    format_currency_amount,
)
from .services import sanitize_spreadsheet_cell


def get_user_active_memberships(user):
    if not user or not user.is_authenticated:
        return SharedSpaceMembership.objects.none()
    return (
        SharedSpaceMembership.objects.select_related(
            "shared_space", "shared_space__base_currency", "shared_space__created_by"
        )
        .filter(user=user, is_active=True)
        .order_by("joined_at")
    )


def get_user_shared_spaces(user):
    memberships = get_user_active_memberships(user)
    return [m.shared_space for m in memberships]


def get_active_workspace_context(user) -> dict:
    if not user or not user.is_authenticated:
        return {
            "preference": None,
            "active_scope": "personal",
            "active_shared_space": None,
            "shared_spaces": [],
            "memberships": [],
            "personal_base_currency": None,
            "effective_base_currency": None,
            "display_currency": None,
            "effective_display_currency": None,
        }

    pref = (
        UserPreference.objects.select_related(
            "personal_base_currency",
            "display_currency",
            "active_shared_space",
            "active_shared_space__base_currency",
        )
        .filter(user=user)
        .first()
    )

    memberships = list(get_user_active_memberships(user))
    shared_spaces = [m.shared_space for m in memberships]
    active_space_ids = {s.id for s in shared_spaces}

    active_scope = (pref.active_scope if pref else None) or "personal"
    active_space = pref.active_shared_space if pref else None

    if active_scope == "shared":
        if active_space is None or active_space.id not in active_space_ids:
            if shared_spaces:
                active_space = shared_spaces[0]
                if pref:
                    pref.active_shared_space = active_space
                    pref.save(update_fields=["active_shared_space", "updated_at"])
            else:
                active_scope = "personal"
                active_space = None
                if pref:
                    pref.active_scope = "personal"
                    pref.active_shared_space = None
                    pref.save(update_fields=["active_scope", "active_shared_space", "updated_at"])
    elif active_space is None and shared_spaces:
        active_space = shared_spaces[0]

    personal_base_currency = (
        pref.personal_base_currency if pref and pref.personal_base_currency_id else None
    )
    effective_base_currency = (
        active_space.base_currency
        if active_scope == "shared" and active_space is not None
        else personal_base_currency
    )
    display_currency = (
        pref.display_currency
        if pref and pref.display_currency_id and pref.display_currency.is_active
        else None
    )
    effective_display_currency = (
        display_currency or effective_base_currency or personal_base_currency
    )

    return {
        "preference": pref,
        "active_scope": active_scope,
        "active_shared_space": active_space,
        "shared_spaces": shared_spaces,
        "memberships": memberships,
        "personal_base_currency": personal_base_currency,
        "effective_base_currency": effective_base_currency,
        "display_currency": display_currency,
        "effective_display_currency": effective_display_currency,
    }


def resolve_user_effective_display_currency(user, *, shared_space=None) -> Currency | None:
    """
    Read-only resolution of a user's effective display currency matching the
    application's fallback policy:
    1. Active user display_currency preference
    2. Active user personal_base_currency preference
    3. Active shared_space base_currency (if provided)
    Does NOT create or modify any UserPreference records or financial data.
    Respects is_active. Returns None if unconfigured or inactive.
    """
    if not user or not getattr(user, "is_authenticated", False):
        return None

    pref = (
        UserPreference.objects.select_related(
            "personal_base_currency",
            "display_currency",
        )
        .filter(user=user)
        .first()
    )

    if (
        pref
        and pref.display_currency_id
        and pref.display_currency
        and pref.display_currency.is_active
    ):
        return pref.display_currency

    if (
        pref
        and pref.personal_base_currency_id
        and pref.personal_base_currency
        and pref.personal_base_currency.is_active
    ):
        return pref.personal_base_currency

    if (
        shared_space
        and shared_space.base_currency_id
        and shared_space.base_currency
        and shared_space.base_currency.is_active
    ):
        return shared_space.base_currency

    return None


def get_accessible_wallets_qs(user, *, scope: str | None = None, include_archived: bool = False):
    if not user or not user.is_authenticated:
        return Wallet.objects.none()

    space_ids = list(
        SharedSpaceMembership.objects.filter(user=user, is_active=True).values_list(
            "shared_space_id", flat=True
        )
    )
    personal_q = Q(ownership_scope=Wallet.OwnershipScope.PERSONAL, owner=user)
    shared_q = Q(ownership_scope=Wallet.OwnershipScope.SHARED, shared_space_id__in=space_ids)

    if scope == "personal":
        qs = Wallet.objects.filter(personal_q)
    elif scope == "shared":
        qs = Wallet.objects.filter(shared_q)
    else:
        qs = Wallet.objects.filter(personal_q | shared_q)

    if not include_archived:
        qs = qs.filter(is_archived=False)

    return qs.select_related("currency", "shared_space", "owner").order_by(
        "is_archived", "ownership_scope", "name", "created_at"
    )


def enrich_wallets_with_balances(wallets) -> list[Wallet]:
    wallet_list = list(wallets)
    if not wallet_list:
        return []

    wallet_ids = [w.id for w in wallet_list]
    agg_rows = (
        Transaction.objects.filter(wallet_id__in=wallet_ids, is_deleted=False)
        .values("wallet_id", "transaction_type")
        .annotate(total=Sum("amount"))
    )

    inflows_map = defaultdict(lambda: Decimal("0.000"))
    outflows_map = defaultdict(lambda: Decimal("0.000"))
    activity_ids = set()

    for row in agg_rows:
        wid = row["wallet_id"]
        activity_ids.add(wid)
        tx_type = row["transaction_type"]
        amt = row["total"] or Decimal("0.000")
        if tx_type in (Transaction.Type.INCOME, Transaction.Type.TRANSFER_IN):
            inflows_map[wid] += amt
        elif tx_type in (Transaction.Type.EXPENSE, Transaction.Type.TRANSFER_OUT):
            outflows_map[wid] += amt

    for w in wallet_list:
        w._prefetched_current_balance = (
            w.opening_balance + inflows_map[w.id] - outflows_map[w.id]
        )
        w.has_activity_flag = w.id in activity_ids
        w.net_change = inflows_map[w.id] - outflows_map[w.id]
        w.formatted_net_change = format_currency_amount(w.net_change, w.currency)

    return wallet_list


def get_accessible_categories_qs(
    user,
    *,
    scope: str | None = None,
    kind: str | None = None,
    include_archived: bool = False,
):
    if not user or not user.is_authenticated:
        return Category.objects.none()

    space_ids = list(
        SharedSpaceMembership.objects.filter(user=user, is_active=True).values_list(
            "shared_space_id", flat=True
        )
    )
    personal_q = Q(scope=Category.Scope.PERSONAL, owner=user)
    shared_q = Q(scope=Category.Scope.SHARED, shared_space_id__in=space_ids)

    if scope == "personal":
        qs = Category.objects.filter(personal_q)
    elif scope == "shared":
        qs = Category.objects.filter(shared_q)
    else:
        qs = Category.objects.filter(personal_q | shared_q)

    if kind in (Category.Kind.EXPENSE, Category.Kind.INCOME):
        qs = qs.filter(kind__in=[kind, Category.Kind.BOTH])

    if not include_archived:
        qs = qs.filter(is_archived=False)

    return qs.select_related("shared_space", "owner").order_by("is_archived", "scope", "name")


def get_accessible_transactions_qs(user, *, include_deleted: bool = False):
    if not user or not user.is_authenticated:
        return Transaction.objects.none()

    space_ids = list(
        SharedSpaceMembership.objects.filter(user=user, is_active=True).values_list(
            "shared_space_id", flat=True
        )
    )
    personal_q = Q(scope=Transaction.Scope.PERSONAL, owner=user)
    shared_q = Q(scope=Transaction.Scope.SHARED, shared_space_id__in=space_ids)

    qs = Transaction.objects.filter(personal_q | shared_q)
    if not include_deleted:
        qs = qs.filter(is_deleted=False)

    return qs.select_related(
        "wallet",
        "wallet__currency",
        "currency",
        "base_currency",
        "category",
        "owner",
        "paid_by",
        "shared_space",
        "transfer",
    ).order_by("-transaction_date", "-created_at")


def convert_transaction_to_target(
    tx: Transaction,
    target_currency: Currency | None,
    *,
    user=None,
    scope: str | None = None,
    shared_space: SharedSpace | None = None,
):
    """
    Returns (converted_decimal_or_none, status_str) for reporting / aggregation.
    - If target_currency is None: returns (None, "no_target")
    - If tx.currency == target_currency: returns (tx.amount, "identity")
    - If tx already booked into target_currency with a non-null converted_amount:
      uses the stored historical snapshot (never overwritten by later rate refreshes).
    - Otherwise looks up an ExchangeRate for tx.transaction_date (if historical, requires
      date match so today's rate is never silently applied to historical dates),
      strictly scoped to the transaction's user/scope/shared-space context.
    """
    if target_currency is None:
        return None, "no_target"
    if tx.currency_id == target_currency.id:
        return tx.amount, "identity"
    if (
        tx.base_currency_id == target_currency.id
        and tx.converted_amount is not None
        and tx.conversion_status != Transaction.ConversionStatus.PENDING
    ):
        return tx.converted_amount, tx.conversion_status

    effective_scope = scope or tx.scope
    effective_user = user or tx.owner
    effective_space = (
        shared_space
        if shared_space is not None
        else (tx.shared_space if effective_scope == Transaction.Scope.SHARED else None)
    )

    today = timezone.localdate()
    is_historical = tx.transaction_date < today
    lookup = get_latest_rate(
        tx.currency,
        target_currency,
        for_date=tx.transaction_date if is_historical else None,
        require_date_match=is_historical,
        user=effective_user,
        scope=effective_scope,
        shared_space=effective_space,
    )
    if lookup.rate is not None:
        return convert_decimal_amount(tx.amount, lookup.rate, target_currency), "converted"
    return None, "pending"


def enrich_transactions_with_display_currency(
    transactions_list,
    display_currency: Currency | None,
    *,
    user=None,
    scope: str | None = None,
    shared_space: SharedSpace | None = None,
    rates_cache: dict | None = None,
):
    """
    Enriches each transaction in transactions_list with dynamic display-currency
    presentation attributes without modifying or persisting database records.
    Derives the rate authorization scope and shared space from each transaction
    row, using user as the viewer. Caches rates by (viewer_id, source_id, target_id,
    scope, shared_space_id) so equivalent row contexts reuse lookups while
    preventing cache bleed across personal and distinct shared spaces.
    Bypasses lookups for same-currency rows.
    """
    if not transactions_list:
        return transactions_list

    if display_currency is None:
        for tx in transactions_list:
            tx.display_currency = None
            tx.display_amount = None
            tx.formatted_display_amount = None
            tx.display_status = "no_target"
            tx.display_status_label = "No display currency configured"
            tx.display_is_stale = False
            tx.display_rate = None
            tx.display_rate_source = None
            tx.display_rate_timestamp = None
        return transactions_list

    if rates_cache is None:
        rates_cache = {}

    user_id = getattr(user, "pk", None) or getattr(user, "id", None)

    for tx in transactions_list:
        tx.display_currency = display_currency
        if tx.currency_id == display_currency.id:
            tx.display_amount = tx.amount
            tx.formatted_display_amount = tx.formatted_amount
            tx.display_status = "identity"
            tx.display_status_label = "Same currency (1:1)"
            tx.display_is_stale = False
            tx.display_rate = Decimal("1.00000000")
            tx.display_rate_source = "identity"
            tx.display_rate_timestamp = None
            continue

        tx_scope = getattr(tx, "scope", None) or scope or "personal"
        if tx_scope == "shared":
            tx_shared_space = getattr(tx, "shared_space", None) or (
                shared_space if scope == "shared" else None
            )
            tx_shared_space_id = (
                getattr(tx, "shared_space_id", None)
                or (tx_shared_space.id if tx_shared_space else None)
            )
        else:
            tx_scope = "personal"
            tx_shared_space = None
            tx_shared_space_id = None

        cache_key = (
            user_id,
            tx.currency_id,
            display_currency.id,
            tx_scope,
            tx_shared_space_id,
        )

        if cache_key not in rates_cache:
            rates_cache[cache_key] = get_latest_rate(
                tx.currency,
                display_currency,
                user=user,
                scope=tx_scope,
                shared_space=tx_shared_space,
            )

        lookup = rates_cache[cache_key]
        tx.display_is_stale = lookup.is_stale
        tx.display_rate = lookup.rate
        tx.display_rate_timestamp = lookup.rate_timestamp
        tx.display_status = lookup.status
        tx.display_status_label = lookup.status_label

        if lookup.is_manual:
            tx.display_rate_source = "Manual rate"
        elif lookup.provider:
            tx.display_rate_source = lookup.provider
        else:
            tx.display_rate_source = None

        if lookup.rate is not None:
            tx.display_amount = convert_decimal_amount(tx.amount, lookup.rate, display_currency)
            tx.formatted_display_amount = format_currency_amount(tx.display_amount, display_currency)
        else:
            tx.display_amount = None
            tx.formatted_display_amount = None

    return transactions_list


def get_filtered_transactions(user, params: dict, *, per_page: int = 25) -> dict:
    qs = get_accessible_transactions_qs(user, include_deleted=False)

    search = (params.get("q") or "").strip()
    scope = (params.get("scope") or "").strip()
    tx_type = (params.get("type") or "").strip()
    wallet_id = (params.get("wallet") or "").strip()
    category_id = (params.get("category") or "").strip()
    currency_code = (params.get("currency") or "").strip().upper()
    display_currency_code = (params.get("display_currency") or "").strip().upper()
    start_date_raw = (params.get("start_date") or "").strip()
    end_date_raw = (params.get("end_date") or "").strip()

    if search:
        qs = qs.filter(
            Q(description__icontains=search)
            | Q(notes__icontains=search)
            | Q(category__name__icontains=search)
            | Q(wallet__name__icontains=search)
        )

    if scope in ("personal", "shared"):
        qs = qs.filter(scope=scope)

    if tx_type in (
        Transaction.Type.INCOME,
        Transaction.Type.EXPENSE,
        Transaction.Type.TRANSFER_OUT,
        Transaction.Type.TRANSFER_IN,
    ):
        qs = qs.filter(transaction_type=tx_type)
    elif tx_type == "transfer":
        qs = qs.filter(
            transaction_type__in=[Transaction.Type.TRANSFER_OUT, Transaction.Type.TRANSFER_IN]
        )

    if wallet_id:
        qs = qs.filter(wallet_id=wallet_id)

    if category_id:
        if category_id == "uncategorized":
            qs = qs.filter(category__isnull=True)
        else:
            qs = qs.filter(category_id=category_id)

    if currency_code:
        qs = qs.filter(currency__code=currency_code)

    start_date = None
    end_date = None
    if start_date_raw:
        try:
            start_date = date.fromisoformat(start_date_raw)
            qs = qs.filter(transaction_date__gte=start_date)
        except ValueError:
            start_date = None

    if end_date_raw:
        try:
            end_date = date.fromisoformat(end_date_raw)
            qs = qs.filter(transaction_date__lte=end_date)
        except ValueError:
            end_date = None

    paginator = Paginator(qs, per_page)
    page_number = params.get("page") or 1
    page_obj = paginator.get_page(page_number)

    return {
        "queryset": qs,
        "page_obj": page_obj,
        "total_count": paginator.count,
        "filters": {
            "q": search,
            "scope": scope,
            "type": tx_type,
            "wallet": wallet_id,
            "category": category_id,
            "currency": currency_code,
            "display_currency": display_currency_code,
            "start_date": start_date_raw if start_date else "",
            "end_date": end_date_raw if end_date else "",
        },
    }


def get_accessible_transfers(user, *, scope: str | None = None, limit: int = 20):
    if not user or not user.is_authenticated:
        return Transfer.objects.none()

    space_ids = list(
        SharedSpaceMembership.objects.filter(user=user, is_active=True).values_list(
            "shared_space_id", flat=True
        )
    )
    personal_q = Q(scope=Transfer.Scope.PERSONAL, created_by=user)
    shared_q = Q(scope=Transfer.Scope.SHARED, shared_space_id__in=space_ids)

    if scope == "personal":
        qs = Transfer.objects.filter(personal_q)
    elif scope == "shared":
        qs = Transfer.objects.filter(shared_q)
    else:
        qs = Transfer.objects.filter(personal_q | shared_q)

    return (
        qs.filter(is_deleted=False)
        .select_related(
            "source_wallet",
            "destination_wallet",
            "source_currency",
            "destination_currency",
            "created_by",
            "shared_space",
        )
        .order_by("-transfer_date", "-created_at")[:limit]
    )


def get_budget_overview_for_period(
    user,
    *,
    period: str | None = None,
    scope: str | None = None,
    currency_code: str | None = None,
    shared_space: SharedSpace | None = None,
) -> dict:
    today = timezone.localdate()
    effective_period = period if period and len(period) == 7 else today.strftime("%Y-%m")
    try:
        year_int, month_int = [int(p) for p in effective_period.split("-")]
        period_start = date(year_int, month_int, 1)
        if month_int == 12:
            period_end = date(year_int + 1, 1, 1) - timezone.timedelta(days=1)
        else:
            period_end = date(year_int, month_int + 1, 1) - timezone.timedelta(days=1)
    except (ValueError, TypeError):
        effective_period = today.strftime("%Y-%m")
        period_start = date(today.year, today.month, 1)
        if today.month == 12:
            period_end = date(today.year + 1, 1, 1) - timezone.timedelta(days=1)
        else:
            period_end = date(today.year, today.month + 1, 1) - timezone.timedelta(days=1)

    space_ids = list(
        SharedSpaceMembership.objects.filter(user=user, is_active=True).values_list(
            "shared_space_id", flat=True
        )
    )
    personal_q = Q(scope=CategoryBudget.Scope.PERSONAL, owner=user)
    if shared_space:
        shared_q = Q(scope=CategoryBudget.Scope.SHARED, shared_space=shared_space)
    else:
        shared_q = Q(scope=CategoryBudget.Scope.SHARED, shared_space_id__in=space_ids)

    if scope == "personal":
        budget_qs = CategoryBudget.objects.filter(personal_q)
    elif scope == "shared":
        budget_qs = CategoryBudget.objects.filter(shared_q)
    else:
        budget_qs = CategoryBudget.objects.filter(personal_q | shared_q)

    budget_qs = (
        budget_qs.filter(is_archived=False, period=effective_period)
        .select_related("category", "currency", "shared_space", "owner")
        .order_by("scope", "category__name")
    )
    if currency_code:
        budget_qs = budget_qs.filter(currency__code=currency_code.upper())

    budgets = list(budget_qs)

    tx_qs = get_accessible_transactions_qs(user).filter(
        transaction_type=Transaction.Type.EXPENSE,
        transaction_date__gte=period_start,
        transaction_date__lte=period_end,
    )
    if scope == "personal":
        tx_qs = tx_qs.filter(scope=Transaction.Scope.PERSONAL, owner=user)
    elif scope == "shared":
        if shared_space:
            tx_qs = tx_qs.filter(scope=Transaction.Scope.SHARED, shared_space=shared_space)
        else:
            tx_qs = tx_qs.filter(scope=Transaction.Scope.SHARED)
    period_expenses = list(tx_qs)

    rows = []
    currencies_seen = set()
    total_budgeted = Decimal("0.000")
    total_spent = Decimal("0.000")
    any_incomplete_conversion = False

    for b in budgets:
        currencies_seen.add(b.currency)
        matching_txs = [
            tx
            for tx in period_expenses
            if tx.category_id == b.category_id
            and tx.scope == b.scope
            and (b.scope == "personal" or tx.shared_space_id == b.shared_space_id)
        ]

        spent = Decimal("0.000")
        unconverted_count = 0
        for tx in matching_txs:
            conv_amt, status = convert_transaction_to_target(
                tx,
                b.currency,
                user=user,
                scope=b.scope,
                shared_space=b.shared_space if b.scope == CategoryBudget.Scope.SHARED else None,
            )
            if conv_amt is not None:
                spent += conv_amt
            else:
                unconverted_count += 1

        incomplete = unconverted_count > 0
        if incomplete:
            any_incomplete_conversion = True

        remaining = b.limit_amount - spent
        progress_pct = Decimal("0")
        if b.limit_amount > Decimal("0"):
            progress_pct = ((spent / b.limit_amount) * Decimal("100")).quantize(
                Decimal("0.1"), rounding=ROUND_HALF_UP
            )

        if incomplete:
            status_label = "Conversion incomplete"
            status_variant = "warning"
        elif spent > b.limit_amount:
            status_label = "Over budget"
            status_variant = "danger"
        elif progress_pct >= Decimal("80.0"):
            status_label = "Near limit"
            status_variant = "warning"
        else:
            status_label = "On track"
            status_variant = "success"

        rows.append(
            {
                "budget": b,
                "category": b.category,
                "scope": b.scope,
                "scope_display": b.get_scope_display(),
                "currency": b.currency,
                "limit_amount": b.limit_amount,
                "formatted_limit": format_currency_amount(b.limit_amount, b.currency),
                "plain_limit": format_currency_amount(b.limit_amount, b.currency, grouping=False),
                "spent_amount": spent,
                "formatted_spent": format_currency_amount(spent, b.currency),
                "plain_spent": format_currency_amount(spent, b.currency, grouping=False),
                "remaining_amount": remaining,
                "formatted_remaining": format_currency_amount(remaining, b.currency),
                "plain_remaining": format_currency_amount(remaining, b.currency, grouping=False),
                "progress_pct": min(Decimal("100"), max(Decimal("0"), progress_pct)),
                "raw_progress_pct": progress_pct,
                "status_label": status_label,
                "status_variant": status_variant,
                "incomplete_conversion": incomplete,
                "unconverted_count": unconverted_count,
            }
        )
        total_budgeted += b.limit_amount
        total_spent += spent

    single_currency = list(currencies_seen)[0] if len(currencies_seen) == 1 else None
    summary_available = single_currency is not None and not any_incomplete_conversion
    total_remaining = total_budgeted - total_spent if summary_available else None

    return {
        "period": effective_period,
        "period_start": period_start,
        "period_end": period_end,
        "scope": scope or "",
        "currency_code": currency_code or "",
        "rows": rows,
        "single_currency": single_currency,
        "any_incomplete_conversion": any_incomplete_conversion,
        "summary_available": summary_available,
        "formatted_total_budgeted": (
            format_currency_amount(total_budgeted, single_currency)
            if single_currency
            else "—"
        ),
        "formatted_total_spent": (
            format_currency_amount(total_spent, single_currency)
            if summary_available
            else "—"
        ),
        "formatted_total_remaining": (
            format_currency_amount(total_remaining, single_currency)
            if summary_available and total_remaining is not None
            else "—"
        ),
    }


def get_savings_goals_overview(
    user,
    *,
    scope: str | None = None,
    include_archived: bool = False,
    shared_space: SharedSpace | None = None,
) -> dict:
    space_ids = list(
        SharedSpaceMembership.objects.filter(user=user, is_active=True).values_list(
            "shared_space_id", flat=True
        )
    )
    personal_q = Q(scope=SavingsGoal.Scope.PERSONAL, owner=user)
    if shared_space:
        shared_q = Q(scope=SavingsGoal.Scope.SHARED, shared_space=shared_space)
    else:
        shared_q = Q(scope=SavingsGoal.Scope.SHARED, shared_space_id__in=space_ids)

    if scope == "personal":
        qs = SavingsGoal.objects.filter(personal_q)
    elif scope == "shared":
        qs = SavingsGoal.objects.filter(shared_q)
    else:
        qs = SavingsGoal.objects.filter(personal_q | shared_q)

    if not include_archived:
        qs = qs.filter(is_archived=False)

    goals = list(
        qs.select_related("currency", "shared_space", "owner")
        .prefetch_related("contributions", "contributions__created_by")
        .order_by("is_archived", "scope", "name", "created_at")
    )

    goal_rows = []
    for g in goals:
        saved = g.saved_amount
        remaining = g.remaining_amount
        pct = Decimal("0")
        if g.target_amount > Decimal("0"):
            pct = ((saved / g.target_amount) * Decimal("100")).quantize(
                Decimal("0.1"), rounding=ROUND_HALF_UP
            )
        goal_rows.append(
            {
                "goal": g,
                "saved_amount": saved,
                "remaining_amount": remaining,
                "formatted_target": g.formatted_target_amount,
                "plain_target": g.plain_target_amount,
                "formatted_saved": format_currency_amount(saved, g.currency),
                "plain_saved": (
                    format_currency_amount(saved, g.currency, grouping=False)
                    if saved is not None and g.currency_id
                    else ""
                ),
                "formatted_remaining": format_currency_amount(remaining, g.currency),
                "plain_remaining": (
                    format_currency_amount(remaining, g.currency, grouping=False)
                    if remaining is not None and g.currency_id
                    else ""
                ),
                "progress_pct": min(Decimal("100"), max(Decimal("0"), pct)),
                "raw_progress_pct": pct,
                "is_completed": saved >= g.target_amount,
                "recent_contributions": list(g.contributions.all()[:5]),
            }
        )

    return {
        "rows": goal_rows,
        "scope": scope or "",
        "total_goals": len(goal_rows),
    }


def compute_income_expense_summary(
    transactions,
    target_currency: Currency | None,
    *,
    user=None,
) -> dict:
    """
    Aggregates income, expenses, and net income (`income - expenses`),
    strictly excluding transfer legs (`transfer_out`, `transfer_in`).
    Never fabricates converted totals if target_currency is missing or any transaction lacks a rate.
    """
    income_total = Decimal("0.000")
    expense_total = Decimal("0.000")
    unconverted_count = 0
    tx_count = 0

    for tx in transactions:
        if tx.transaction_type not in (Transaction.Type.INCOME, Transaction.Type.EXPENSE):
            continue
        tx_count += 1
        if target_currency is None:
            unconverted_count += 1
            continue
        conv_amt, _ = convert_transaction_to_target(
            tx,
            target_currency,
            user=user or tx.owner,
            scope=tx.scope,
            shared_space=tx.shared_space if tx.scope == Transaction.Scope.SHARED else None,
        )
        if conv_amt is None:
            unconverted_count += 1
        else:
            if tx.transaction_type == Transaction.Type.INCOME:
                income_total += conv_amt
            elif tx.transaction_type == Transaction.Type.EXPENSE:
                expense_total += conv_amt

    complete = target_currency is not None and unconverted_count == 0
    net_income = income_total - expense_total if complete else None

    return {
        "target_currency": target_currency,
        "tx_count": tx_count,
        "unconverted_count": unconverted_count,
        "is_complete": complete,
        "income_total": income_total if complete else None,
        "expense_total": expense_total if complete else None,
        "net_income": net_income,
        "formatted_income": (
            format_currency_amount(income_total, target_currency) if complete else "—"
        ),
        "formatted_expense": (
            format_currency_amount(expense_total, target_currency) if complete else "—"
        ),
        "formatted_net_income": (
            format_currency_amount(net_income, target_currency)
            if complete and net_income is not None
            else "—"
        ),
    }


def enrich_budget_overview_for_dashboard(
    budget_overview: dict,
    *,
    target_currency: Currency | None,
    user=None,
    scope: str = "personal",
    shared_space: SharedSpace | None = None,
) -> dict:
    rows = budget_overview.get("rows", [])
    if not rows:
        budget_overview["display_currency"] = target_currency
        budget_overview["summary_available"] = False
        budget_overview["formatted_total_remaining"] = "—"
        budget_overview["summary_status"] = "empty"
        budget_overview["has_mixed_currencies"] = False
        return budget_overview

    currencies_seen = {r["currency"].pk for r in rows}
    budget_overview["has_mixed_currencies"] = len(currencies_seen) > 1

    if target_currency is None:
        for r in rows:
            r["is_converted"] = False
            r["is_same_currency"] = False
            r["display_currency"] = None
            r["display_limit"] = None
            r["display_spent"] = None
            r["display_remaining"] = None
            r["formatted_display_limit"] = r["formatted_limit"]
            r["formatted_display_spent"] = r["formatted_spent"]
            r["formatted_display_remaining"] = r["formatted_remaining"]
            r["conversion_status"] = "missing_target_currency"
            r["rate_is_stale"] = False
        budget_overview["display_currency"] = None
        budget_overview["summary_available"] = False
        budget_overview["formatted_total_remaining"] = "—"
        budget_overview["summary_status"] = "missing_target_currency"
        budget_overview["missing_rate_explanation"] = "Set base currency on Currencies page"
        return budget_overview

    all_converted = True
    any_stale_rate = False
    total_display_limit = Decimal("0.000")
    total_display_spent = Decimal("0.000")

    for r in rows:
        b = r["budget"]
        b_cur = r["currency"]
        limit_amt = r["limit_amount"]
        spent_amt = r["spent_amount"]
        rem_amt = r["remaining_amount"]

        if b_cur.pk == target_currency.pk:
            r["is_converted"] = True
            r["is_same_currency"] = True
            r["display_currency"] = target_currency
            r["display_limit"] = limit_amt
            r["display_spent"] = spent_amt
            r["display_remaining"] = rem_amt
            r["formatted_display_limit"] = format_currency_amount(limit_amt, target_currency)
            r["formatted_display_spent"] = format_currency_amount(spent_amt, target_currency)
            r["formatted_display_remaining"] = format_currency_amount(rem_amt, target_currency)
            r["conversion_status"] = "same_currency"
            r["rate_is_stale"] = False
            r["rate_metadata"] = None
            total_display_limit += limit_amt
            total_display_spent += spent_amt
        else:
            r["is_same_currency"] = False
            lookup = get_latest_rate(
                b_cur,
                target_currency,
                user=user or b.owner,
                scope=b.scope,
                shared_space=b.shared_space if b.scope == CategoryBudget.Scope.SHARED else None,
            )
            if lookup.rate is not None:
                c_limit = convert_decimal_amount(limit_amt, lookup.rate, target_currency)
                c_spent = convert_decimal_amount(spent_amt, lookup.rate, target_currency)
                c_rem = c_limit - c_spent
                r["is_converted"] = True
                r["display_currency"] = target_currency
                r["display_limit"] = c_limit
                r["display_spent"] = c_spent
                r["display_remaining"] = c_rem
                r["formatted_display_limit"] = format_currency_amount(c_limit, target_currency)
                r["formatted_display_spent"] = format_currency_amount(c_spent, target_currency)
                r["formatted_display_remaining"] = format_currency_amount(c_rem, target_currency)
                r["conversion_status"] = "converted"
                r["rate_is_stale"] = lookup.is_stale
                r["rate_age_hours"] = lookup.age_hours
                r["rate_metadata"] = lookup
                if lookup.is_stale:
                    any_stale_rate = True
                total_display_limit += c_limit
                total_display_spent += c_spent
            else:
                all_converted = False
                r["is_converted"] = False
                r["display_currency"] = None
                r["display_limit"] = None
                r["display_spent"] = None
                r["display_remaining"] = None
                r["formatted_display_limit"] = r["formatted_limit"]
                r["formatted_display_spent"] = r["formatted_spent"]
                r["formatted_display_remaining"] = r["formatted_remaining"]
                r["conversion_status"] = "unavailable"
                r["rate_is_stale"] = False
                r["rate_metadata"] = lookup

    budget_overview["display_currency"] = target_currency
    budget_overview["any_stale_rate"] = any_stale_rate

    if not all_converted:
        budget_overview["summary_available"] = False
        budget_overview["formatted_total_remaining"] = "—"
        budget_overview["summary_status"] = "missing_rate"
        budget_overview["missing_rate_explanation"] = "Exchange rate unavailable for display currency"
    elif budget_overview.get("any_incomplete_conversion"):
        budget_overview["summary_available"] = False
        budget_overview["formatted_total_remaining"] = "—"
        budget_overview["summary_status"] = "incomplete_expense_conversion"
        budget_overview["missing_rate_explanation"] = "Conversion incomplete for some expenses"
    else:
        total_display_remaining = total_display_limit - total_display_spent
        budget_overview["summary_available"] = True
        budget_overview["total_display_limit"] = total_display_limit
        budget_overview["total_display_spent"] = total_display_spent
        budget_overview["total_display_remaining"] = total_display_remaining
        budget_overview["formatted_total_remaining"] = format_currency_amount(
            total_display_remaining, target_currency
        )
        budget_overview["formatted_total_limit"] = format_currency_amount(
            total_display_limit, target_currency
        )
        budget_overview["formatted_total_spent"] = format_currency_amount(
            total_display_spent, target_currency
        )
        budget_overview["summary_status"] = "complete"

    return budget_overview


def enrich_goals_overview_for_dashboard(
    goals_overview: dict,
    *,
    target_currency: Currency | None,
    user=None,
    scope: str = "personal",
    shared_space: SharedSpace | None = None,
) -> dict:
    rows = goals_overview.get("rows", [])
    if not rows:
        goals_overview["display_currency"] = target_currency
        return goals_overview

    if target_currency is None:
        for it in rows:
            it["is_converted"] = False
            it["is_same_currency"] = False
            it["display_currency"] = None
            it["display_target"] = None
            it["display_saved"] = None
            it["display_remaining"] = None
            it["formatted_display_target"] = it["formatted_target"]
            it["formatted_display_saved"] = it["formatted_saved"]
            it["formatted_display_remaining"] = it["formatted_remaining"]
            it["rate_is_stale"] = False
        goals_overview["display_currency"] = None
        return goals_overview

    for it in rows:
        g = it["goal"]
        g_cur = g.currency
        target_amt = g.target_amount
        saved_amt = it["saved_amount"]
        rem_amt = it["remaining_amount"]

        if g_cur.pk == target_currency.pk:
            it["is_converted"] = True
            it["is_same_currency"] = True
            it["display_currency"] = target_currency
            it["display_target"] = target_amt
            it["display_saved"] = saved_amt
            it["display_remaining"] = rem_amt
            it["formatted_display_target"] = format_currency_amount(target_amt, target_currency)
            it["formatted_display_saved"] = format_currency_amount(saved_amt, target_currency)
            it["formatted_display_remaining"] = format_currency_amount(rem_amt, target_currency)
            it["rate_is_stale"] = False
            it["rate_metadata"] = None
        else:
            it["is_same_currency"] = False
            lookup = get_latest_rate(
                g_cur,
                target_currency,
                user=user or g.owner,
                scope=g.scope,
                shared_space=g.shared_space if g.scope == SavingsGoal.Scope.SHARED else None,
            )
            if lookup.rate is not None:
                c_target = convert_decimal_amount(target_amt, lookup.rate, target_currency)
                c_saved = convert_decimal_amount(saved_amt, lookup.rate, target_currency)
                c_rem = max(Decimal("0.000"), c_target - c_saved)
                it["is_converted"] = True
                it["display_currency"] = target_currency
                it["display_target"] = c_target
                it["display_saved"] = c_saved
                it["display_remaining"] = c_rem
                it["formatted_display_target"] = format_currency_amount(c_target, target_currency)
                it["formatted_display_saved"] = format_currency_amount(c_saved, target_currency)
                it["formatted_display_remaining"] = format_currency_amount(c_rem, target_currency)
                it["rate_is_stale"] = lookup.is_stale
                it["rate_age_hours"] = lookup.age_hours
                it["rate_metadata"] = lookup
            else:
                it["is_converted"] = False
                it["display_currency"] = None
                it["display_target"] = None
                it["display_saved"] = None
                it["display_remaining"] = None
                it["formatted_display_target"] = it["formatted_target"]
                it["formatted_display_saved"] = it["formatted_saved"]
                it["formatted_display_remaining"] = it["formatted_remaining"]
                it["rate_is_stale"] = False
                it["rate_metadata"] = lookup

    goals_overview["display_currency"] = target_currency
    return goals_overview


def get_dashboard_context(user, period: str | None = None) -> dict:
    ws = get_active_workspace_context(user)
    active_scope = ws["active_scope"]
    active_shared_space = ws.get("active_shared_space")
    target_currency = ws["effective_display_currency"]

    all_accessible_wallets = enrich_wallets_with_balances(
        get_accessible_wallets_qs(user, include_archived=False)
    )
    if active_scope == "personal":
        scoped_wallets = [
            w for w in all_accessible_wallets
            if w.ownership_scope == Wallet.OwnershipScope.PERSONAL and w.owner_id == user.id
        ]
    elif active_scope == "shared" and active_shared_space:
        scoped_wallets = [
            w for w in all_accessible_wallets
            if w.ownership_scope == Wallet.OwnershipScope.SHARED and w.shared_space_id == active_shared_space.id
        ]
    else:
        scoped_wallets = []

    wallet_total = Decimal("0.000")
    wallet_unconverted = 0
    for w in scoped_wallets:
        if target_currency is None:
            wallet_unconverted += 1
            continue
        if w.currency_id == target_currency.id:
            wallet_total += w.current_balance
        else:
            lookup = get_latest_rate(
                w.currency,
                target_currency,
                user=user,
                scope=w.ownership_scope,
                shared_space=w.shared_space if w.ownership_scope == Wallet.OwnershipScope.SHARED else None,
            )
            if lookup.rate is not None:
                wallet_total += convert_decimal_amount(
                    w.current_balance, lookup.rate, target_currency
                )
            else:
                wallet_unconverted += 1

    wallet_total_complete = target_currency is not None and wallet_unconverted == 0
    wallet_count = len(scoped_wallets)

    today = timezone.localdate()
    today_period = today.strftime("%Y-%m")

    base_tx_qs = get_accessible_transactions_qs(user)
    if active_scope == "personal":
        scoped_tx_qs = base_tx_qs.filter(
            scope=Transaction.Scope.PERSONAL,
            owner=user,
        )
        scoped_budget_qs = CategoryBudget.objects.filter(
            scope=CategoryBudget.Scope.PERSONAL,
            owner=user,
            is_archived=False,
        )
    elif active_scope == "shared" and active_shared_space:
        scoped_tx_qs = base_tx_qs.filter(
            scope=Transaction.Scope.SHARED,
            shared_space=active_shared_space,
        )
        scoped_budget_qs = CategoryBudget.objects.filter(
            scope=CategoryBudget.Scope.SHARED,
            shared_space=active_shared_space,
            is_archived=False,
        )
    else:
        scoped_tx_qs = Transaction.objects.none()
        scoped_budget_qs = CategoryBudget.objects.none()

    # Collect distinct YYYY-MM periods from accessible scoped transactions and budgets
    all_periods = set()
    for dt in scoped_tx_qs.values_list("transaction_date", flat=True).distinct():
        if dt:
            if hasattr(dt, "strftime"):
                all_periods.add(dt.strftime("%Y-%m"))
            elif isinstance(dt, str) and len(dt) >= 7 and dt[:4].isdigit() and dt[4] == "-" and dt[5:7].isdigit():
                all_periods.add(dt[:7])

    for p in scoped_budget_qs.values_list("period", flat=True).distinct():
        if p and isinstance(p, str) and len(p) == 7 and p[:4].isdigit() and p[4] == "-" and p[5:7].isdigit():
            all_periods.add(p)

    sorted_periods = sorted(all_periods, reverse=True)

    clean_requested_period = (period or "").strip()
    effective_period = None
    if sorted_periods:
        if clean_requested_period in sorted_periods:
            effective_period = clean_requested_period
        elif today_period in sorted_periods:
            effective_period = today_period
        else:
            effective_period = sorted_periods[0]

    period_options = []
    if sorted_periods and effective_period:
        for p in sorted_periods:
            try:
                y, m = int(p[:4]), int(p[5:7])
                opt_label = date(y, m, 1).strftime("%B %Y")
            except (ValueError, TypeError):
                opt_label = p
            period_options.append({
                "value": p,
                "label": opt_label,
                "selected": (p == effective_period),
            })
        try:
            ey, em = int(effective_period[:4]), int(effective_period[5:7])
            period_label = date(ey, em, 1).strftime("%B %Y")
        except (ValueError, TypeError):
            period_label = effective_period
    else:
        period_label = "No recorded periods"

    if effective_period:
        year_int, month_int = int(effective_period[:4]), int(effective_period[5:7])
        period_start = date(year_int, month_int, 1)
        if month_int == 12:
            period_end = date(year_int + 1, 1, 1) - timezone.timedelta(days=1)
        else:
            period_end = date(year_int, month_int + 1, 1) - timezone.timedelta(days=1)

        month_tx_qs = scoped_tx_qs.filter(
            transaction_date__gte=period_start,
            transaction_date__lte=period_end,
        )
        month_txs = list(month_tx_qs)
        month_summary = compute_income_expense_summary(month_txs, target_currency, user=user)
        budget_overview = get_budget_overview_for_period(
            user,
            period=effective_period,
            scope=active_scope,
            shared_space=active_shared_space if active_scope == "shared" else None,
        )
    else:
        month_txs = []
        month_summary = compute_income_expense_summary([], target_currency, user=user)
        budget_overview = get_budget_overview_for_period(
            user,
            period=today_period,
            scope=active_scope,
            shared_space=active_shared_space if active_scope == "shared" else None,
        )

    # Spending this month breakdown by category
    spending_breakdown = []
    category_expenses = defaultdict(lambda: {"amount": Decimal("0.000"), "currency": target_currency, "count": 0})
    total_converted_expense = Decimal("0.000")
    total_expense_count = 0
    converted_expenses_count = 0
    unconverted_expenses_count = 0

    for tx in month_txs:
        if tx.transaction_type != Transaction.Type.EXPENSE:
            continue
        total_expense_count += 1
        cat_name = tx.category.name if tx.category else "Uncategorized"
        if target_currency is not None:
            conv_amt, _ = convert_transaction_to_target(
                tx,
                target_currency,
                user=user or tx.owner,
                scope=tx.scope,
                shared_space=tx.shared_space if tx.scope == Transaction.Scope.SHARED else None,
            )
        else:
            conv_amt = None

        if conv_amt is not None:
            category_expenses[cat_name]["amount"] += conv_amt
            category_expenses[cat_name]["count"] += 1
            total_converted_expense += conv_amt
            converted_expenses_count += 1
        else:
            unconverted_expenses_count += 1

    has_unconverted_expenses = (unconverted_expenses_count > 0)
    all_expenses_unconverted = (total_expense_count > 0 and converted_expenses_count == 0)

    if not all_expenses_unconverted and converted_expenses_count > 0:
        sorted_cats = sorted(category_expenses.items(), key=lambda x: x[1]["amount"], reverse=True)
        for cat_name, data in sorted_cats:
            amt = data["amount"]
            if has_unconverted_expenses:
                pct = None
                pct_num = None
            else:
                if total_converted_expense > Decimal("0"):
                    pct = ((amt / total_converted_expense) * Decimal("100")).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
                    pct_num = float(pct)
                else:
                    pct = Decimal("0.0")
                    pct_num = 0.0
            spending_breakdown.append({
                "category_name": cat_name,
                "amount": amt,
                "currency": target_currency,
                "formatted_amount": format_currency_amount(amt, target_currency),
                "percentage": pct,
                "progress_pct": min(100.0, max(0.0, pct_num)) if pct_num is not None else None,
                "count": data["count"],
            })

    recent_txs = list(scoped_tx_qs[:8])
    enrich_transactions_with_display_currency(
        recent_txs,
        display_currency=target_currency,
        user=user,
        scope=active_scope,
        shared_space=active_shared_space if active_scope == "shared" else None,
    )

    goals_overview = get_savings_goals_overview(
        user,
        scope=active_scope,
        shared_space=active_shared_space if active_scope == "shared" else None,
    )

    enrich_budget_overview_for_dashboard(
        budget_overview,
        target_currency=target_currency,
        user=user,
        scope=active_scope,
        shared_space=active_shared_space if active_scope == "shared" else None,
    )
    enrich_goals_overview_for_dashboard(
        goals_overview,
        target_currency=target_currency,
        user=user,
        scope=active_scope,
        shared_space=active_shared_space if active_scope == "shared" else None,
    )

    return {
        "workspace": ws,
        "wallets": scoped_wallets[:6],
        "wallet_count": wallet_count,
        "wallet_total_complete": wallet_total_complete,
        "formatted_wallet_total": (
            format_currency_amount(wallet_total, target_currency)
            if wallet_total_complete and scoped_wallets
            else "—"
        ),
        "wallet_unconverted": wallet_unconverted,
        "month_summary": month_summary,
        "recent_transactions": recent_txs,
        "budget_overview": budget_overview,
        "goals_overview": goals_overview,
        "current_period": effective_period or "",
        "period_label": period_label,
        "period_options": period_options,
        "spending_breakdown": spending_breakdown,
        "total_period_expense": total_converted_expense,
        "formatted_total_period_expense": (
            format_currency_amount(total_converted_expense, target_currency)
            if target_currency
            else "—"
        ),
        "has_unconverted_expenses": has_unconverted_expenses,
        "spending_unconverted_count": unconverted_expenses_count,
        "all_expenses_unconverted": all_expenses_unconverted,
        "total_expense_count": total_expense_count,
    }


def get_reports_context(user, params: dict) -> dict:
    ws = get_active_workspace_context(user)
    scope = (params.get("scope") or "").strip()
    if scope not in ("personal", "shared", "all"):
        scope = "all"

    start_date_raw = (params.get("start_date") or "").strip()
    end_date_raw = (params.get("end_date") or "").strip()
    currency_code = (params.get("currency") or "").strip().upper()

    target_currency = None
    if currency_code:
        target_currency = Currency.objects.filter(code=currency_code, is_active=True).first()
    if target_currency is None:
        target_currency = ws["effective_display_currency"]

    tx_qs = get_accessible_transactions_qs(user).filter(
        transaction_type__in=[Transaction.Type.INCOME, Transaction.Type.EXPENSE]
    )
    if scope in ("personal", "shared"):
        tx_qs = tx_qs.filter(scope=scope)

    start_date = None
    end_date = None
    if start_date_raw:
        try:
            start_date = date.fromisoformat(start_date_raw)
            tx_qs = tx_qs.filter(transaction_date__gte=start_date)
        except ValueError:
            start_date = None
    if end_date_raw:
        try:
            end_date = date.fromisoformat(end_date_raw)
            tx_qs = tx_qs.filter(transaction_date__lte=end_date)
        except ValueError:
            end_date = None

    transactions = list(tx_qs)
    summary = compute_income_expense_summary(transactions, target_currency, user=user)

    # Category spending breakdown
    cat_buckets = {}
    total_converted_expense = Decimal("0.000")
    cat_unconverted = False

    for tx in transactions:
        if tx.transaction_type != Transaction.Type.EXPENSE:
            continue
        cat_name = tx.category.name if tx.category else "Uncategorized"
        if target_currency is not None:
            conv_amt, _ = convert_transaction_to_target(
                tx,
                target_currency,
                user=user,
                scope=tx.scope,
                shared_space=tx.shared_space if tx.scope == Transaction.Scope.SHARED else None,
            )
        else:
            conv_amt = None

        if conv_amt is not None:
            key = (cat_name, tx.scope, target_currency.code)
            if key not in cat_buckets:
                cat_buckets[key] = {
                    "category_name": cat_name,
                    "scope_display": tx.get_scope_display(),
                    "currency": target_currency,
                    "amount": Decimal("0.000"),
                    "convertible": True,
                }
            cat_buckets[key]["amount"] += conv_amt
            total_converted_expense += conv_amt
        else:
            cat_unconverted = True
            key = (cat_name, tx.scope, tx.currency.code)
            if key not in cat_buckets:
                cat_buckets[key] = {
                    "category_name": cat_name,
                    "scope_display": tx.get_scope_display(),
                    "currency": tx.currency,
                    "amount": Decimal("0.000"),
                    "convertible": False,
                }
            cat_buckets[key]["amount"] += tx.amount

    category_rows = []
    for item in sorted(cat_buckets.values(), key=lambda r: r["amount"], reverse=True):
        share_str = "—"
        if (
            not cat_unconverted
            and item["convertible"]
            and total_converted_expense > Decimal("0")
        ):
            pct = (
                (item["amount"] / total_converted_expense) * Decimal("100")
            ).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
            share_str = f"{pct}%"
        category_rows.append(
            {
                "category_name": item["category_name"],
                "scope_display": item["scope_display"],
                "currency_code": item["currency"].code,
                "formatted_amount": format_currency_amount(item["amount"], item["currency"]),
                "share_display": share_str,
                "convertible": item["convertible"],
            }
        )

    # Monthly trend breakdown
    month_buckets = defaultdict(lambda: {"income": Decimal("0.000"), "expense": Decimal("0.000"), "incomplete": False})
    for tx in transactions:
        ym = tx.transaction_date.strftime("%Y-%m")
        if target_currency is None:
            month_buckets[ym]["incomplete"] = True
            continue
        conv_amt, _ = convert_transaction_to_target(
            tx,
            target_currency,
            user=user,
            scope=tx.scope,
            shared_space=tx.shared_space if tx.scope == Transaction.Scope.SHARED else None,
        )
        if conv_amt is None:
            month_buckets[ym]["incomplete"] = True
        else:
            if tx.transaction_type == Transaction.Type.INCOME:
                month_buckets[ym]["income"] += conv_amt
            elif tx.transaction_type == Transaction.Type.EXPENSE:
                month_buckets[ym]["expense"] += conv_amt

    monthly_rows = []
    for ym in sorted(month_buckets.keys(), reverse=True):
        mdata = month_buckets[ym]
        inc = mdata["income"]
        exp = mdata["expense"]
        net = inc - exp
        incomplete = mdata["incomplete"] or target_currency is None
        monthly_rows.append(
            {
                "period": ym,
                "currency_code": target_currency.code if target_currency else "—",
                "formatted_income": format_currency_amount(inc, target_currency) if not incomplete else "—",
                "formatted_expense": format_currency_amount(exp, target_currency) if not incomplete else "—",
                "formatted_net": format_currency_amount(net, target_currency) if not incomplete else "—",
                "incomplete": incomplete,
            }
        )

    # Personal vs. Shared comparison
    personal_txs = [tx for tx in transactions if tx.scope == Transaction.Scope.PERSONAL]
    shared_txs = [tx for tx in transactions if tx.scope == Transaction.Scope.SHARED]
    personal_summary = compute_income_expense_summary(personal_txs, target_currency, user=user)
    shared_summary = compute_income_expense_summary(shared_txs, target_currency, user=user)

    return {
        "workspace": ws,
        "filters": {
            "scope": scope if scope != "all" else "",
            "start_date": start_date_raw if start_date else "",
            "end_date": end_date_raw if end_date else "",
            "currency": target_currency.code if target_currency else "",
        },
        "target_currency": target_currency,
        "summary": summary,
        "category_rows": category_rows,
        "monthly_rows": monthly_rows,
        "personal_summary": personal_summary,
        "shared_summary": shared_summary,
        "has_shared_space": bool(ws["shared_spaces"]),
        "transaction_count": len(transactions),
    }


def build_transactions_export_rows(user, params: dict) -> list[dict]:
    filtered = get_filtered_transactions(user, params, per_page=100000)
    rows = []
    for tx in filtered["queryset"]:
        rows.append(
            {
                "id": str(tx.id),
                "date": tx.transaction_date.isoformat(),
                "type": tx.get_transaction_type_display(),
                "scope": tx.get_scope_display(),
                "shared_space": sanitize_spreadsheet_cell(
                    tx.shared_space.name if tx.shared_space else ""
                ),
                "wallet": sanitize_spreadsheet_cell(tx.wallet.name),
                "category": sanitize_spreadsheet_cell(
                    tx.category.name if tx.category else "Uncategorized"
                ),
                "description": sanitize_spreadsheet_cell(tx.description),
                "notes": sanitize_spreadsheet_cell(tx.notes),
                "amount": tx.plain_amount,
                "currency": tx.currency.code,
                "base_currency": tx.base_currency.code if tx.base_currency else "",
                "converted_amount": tx.plain_converted_amount if tx.plain_converted_amount is not None else "",
                "conversion_rate": str(tx.conversion_rate) if tx.conversion_rate is not None else "",
                "conversion_status": tx.get_conversion_status_display(),
                "paid_by": sanitize_spreadsheet_cell(
                    tx.paid_by.username if tx.paid_by else tx.owner.username
                ),
            }
        )
    return rows


def get_sync_snapshot_for_user(
    user,
    *,
    since: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> dict:
    from django.utils.dateparse import parse_datetime

    ws = get_active_workspace_context(user)
    wallets = enrich_wallets_with_balances(
        get_accessible_wallets_qs(user, include_archived=True)
    )
    categories = get_accessible_categories_qs(user, include_archived=True)

    try:
        safe_limit = max(1, min(500, int(limit)))
    except (TypeError, ValueError):
        safe_limit = 200

    try:
        safe_offset = max(0, int(offset))
    except (TypeError, ValueError):
        safe_offset = 0

    tx_qs = get_accessible_transactions_qs(user, include_deleted=True)
    if since:
        since_dt = parse_datetime(str(since).strip())
        if since_dt is not None:
            tx_qs = tx_qs.filter(updated_at__gte=since_dt)

    total_tx_count = tx_qs.count()
    tx_page = list(tx_qs[safe_offset : safe_offset + safe_limit])
    has_more = (safe_offset + len(tx_page)) < total_tx_count
    next_offset = (safe_offset + len(tx_page)) if has_more else None

    pref = ws["preference"]
    shared_spaces_data = []
    for sp in ws["shared_spaces"]:
        members = list(
            SharedSpaceMembership.objects.filter(shared_space=sp, is_active=True)
            .select_related("user")
            .order_by("user__username")
        )
        shared_spaces_data.append(
            {
                "id": str(sp.id),
                "name": sp.name,
                "base_currency_code": sp.base_currency.code,
                "members": [
                    {"id": m.user_id, "username": m.user.username} for m in members
                ],
            }
        )

    return {
        "user_id": user.pk,
        "username": user.username,
        "active_scope": ws["active_scope"],
        "active_shared_space_id": (
            str(ws["active_shared_space"].id) if ws["active_shared_space"] else None
        ),
        "shared_spaces": shared_spaces_data,
        "last_sync_at": pref.last_sync_at.isoformat() if pref and pref.last_sync_at else None,
        "server_time": timezone.now().isoformat(),
        "total_transactions_count": total_tx_count,
        "returned_transactions_count": len(tx_page),
        "offset": safe_offset,
        "limit": safe_limit,
        "has_more_transactions": has_more,
        "next_offset": next_offset,
        "is_bounded_snapshot": has_more or safe_offset > 0,
        "wallets": [
            {
                "id": str(w.id),
                "name": w.name,
                "wallet_type": w.wallet_type,
                "ownership_scope": w.ownership_scope,
                "shared_space_id": str(w.shared_space_id) if w.shared_space_id else None,
                "currency_code": w.currency.code,
                "decimal_places": w.currency.decimal_places,
                "opening_balance": w.plain_opening_balance,
                "opening_balance_date": w.opening_balance_date.isoformat(),
                "current_balance": w.plain_current_balance,
                "is_archived": w.is_archived,
                "revision": w.revision,
            }
            for w in wallets
        ],
        "categories": [
            {
                "id": str(c.id),
                "name": c.name,
                "kind": c.kind,
                "scope": c.scope,
                "shared_space_id": str(c.shared_space_id) if c.shared_space_id else None,
                "is_archived": c.is_archived,
                "revision": c.revision,
            }
            for c in categories
        ],
        "transactions": [
            {
                "id": str(tx.id),
                "transaction_type": tx.transaction_type,
                "scope": tx.scope,
                "shared_space_id": str(tx.shared_space_id) if tx.shared_space_id else None,
                "wallet_id": str(tx.wallet_id),
                "wallet_name": tx.wallet.name,
                "category_id": str(tx.category_id) if tx.category_id else None,
                "category_name": tx.category.name if tx.category else "Uncategorized",
                "amount": tx.plain_amount,
                "currency_code": tx.currency.code,
                "decimal_places": tx.currency.decimal_places,
                "transaction_date": tx.transaction_date.isoformat(),
                "description": tx.description,
                "notes": tx.notes,
                "paid_by_id": tx.paid_by_id,
                "paid_by_username": tx.paid_by.username if tx.paid_by else tx.owner.username,
                "conversion_rate": str(tx.conversion_rate) if tx.conversion_rate is not None else None,
                "conversion_status": tx.conversion_status,
                "is_deleted": tx.is_deleted,
                "revision": tx.revision,
                "updated_at": tx.updated_at.isoformat(),
            }
            for tx in tx_page
        ],
    }


class SharedPersonalActivityItem:
    def __init__(
        self,
        *,
        share_id,
        owner_username: str,
        is_viewer_record: bool = False,
        recorded_by_display: str = "",
        transaction_type: str,
        transaction_date,
        formatted_amount: str,
        currency_code: str,
        description: str,
        notes: str,
        include_notes: bool,
        created_at,
    ):
        self.share_id = share_id
        self.owner_username = owner_username
        self.is_viewer_record = is_viewer_record
        self.recorded_by_display = recorded_by_display or ("You" if is_viewer_record else owner_username)
        self.transaction_type = transaction_type
        self.transaction_date = transaction_date
        self.formatted_amount = formatted_amount
        self.currency_code = currency_code
        self.description = description
        self.notes = notes
        self.include_notes = include_notes
        self.created_at = created_at
        self.is_income = transaction_type == Transaction.Type.INCOME


def get_eligible_partner_spaces_for_user(user):
    if not user or not user.is_authenticated:
        return []
    user_space_ids = list(
        SharedSpaceMembership.objects.filter(user=user, is_active=True).values_list(
            "shared_space_id", flat=True
        )
    )
    if not user_space_ids:
        return []
    other_members = (
        SharedSpaceMembership.objects.filter(
            shared_space_id__in=user_space_ids,
            is_active=True,
        )
        .exclude(user=user)
        .select_related("shared_space", "shared_space__base_currency", "user")
    )
    results = []
    seen = set()
    for m in other_members:
        if m.shared_space_id not in seen:
            seen.add(m.shared_space_id)
            results.append({
                "space": m.shared_space,
                "partner": m.user,
            })
    return results


def get_active_shares_for_user_transactions(user, transaction_ids):
    if not user or not user.is_authenticated or not transaction_ids:
        return {}
    shares = (
        PersonalTransactionShare.objects.filter(
            owner=user,
            transaction_id__in=transaction_ids,
            is_revoked=False,
            transaction__is_deleted=False,
        )
        .select_related("shared_space", "recipient")
        .order_by("created_at")
    )
    if not shares:
        return {}

    space_ids = {s.shared_space_id for s in shares}
    active_space_members = set(
        SharedSpaceMembership.objects.filter(
            shared_space_id__in=space_ids,
            is_active=True,
        ).values_list("shared_space_id", "user_id")
    )
    res = defaultdict(list)
    for s in shares:
        owner_active = (s.shared_space_id, s.owner_id) in active_space_members
        recipient_active = (s.shared_space_id, s.recipient_id) in active_space_members
        s.is_active_grant = owner_active and recipient_active
        s.owner_is_active = owner_active
        s.recipient_is_active = recipient_active
        res[s.transaction_id].append(s)
    return dict(res)


def get_shared_personal_activity_for_space(user, shared_space, page: int = 1, per_page: int = 10):
    if not user or not user.is_authenticated or not shared_space:
        return Paginator([], per_page).page(1)

    active_member_ids = set(
        SharedSpaceMembership.objects.filter(
            shared_space=shared_space,
            is_active=True,
        ).values_list("user_id", flat=True)
    )
    # The viewer must be an active member of this shared space
    if user.id not in active_member_ids:
        return Paginator([], per_page).page(1)

    shares_qs = (
        PersonalTransactionShare.objects.filter(
            shared_space=shared_space,
            owner_id__in=active_member_ids,
            recipient_id__in=active_member_ids,
            is_revoked=False,
            transaction__is_deleted=False,
            transaction__scope=Transaction.Scope.PERSONAL,
            transaction__owner_id=F("owner_id"),
            transaction__transfer__isnull=True,
            transaction__is_fee=False,
            transaction__transaction_type__in=[
                Transaction.Type.INCOME,
                Transaction.Type.EXPENSE,
            ],
        )
        .filter(Q(owner=user) | Q(recipient=user))
        .select_related(
            "transaction",
            "transaction__currency",
            "owner",
            "recipient",
        )
        .order_by("-transaction__transaction_date", "-created_at")
        .distinct()
    )

    paginator = Paginator(shares_qs, per_page)
    try:
        page_obj = paginator.page(page)
    except Exception:
        page_obj = paginator.page(1)

    projected_items = []
    for s in page_obj.object_list:
        tx = s.transaction
        is_viewer = s.owner_id == user.id
        projected_items.append(
            SharedPersonalActivityItem(
                share_id=s.id,
                owner_username=s.owner.username,
                is_viewer_record=is_viewer,
                recorded_by_display="You" if is_viewer else s.owner.username,
                transaction_type=tx.transaction_type,
                transaction_date=tx.transaction_date,
                formatted_amount=tx.formatted_amount,
                currency_code=tx.currency.code,
                description=tx.description,
                notes=tx.notes if s.include_notes else "",
                include_notes=s.include_notes,
                created_at=s.created_at,
            )
        )
    page_obj.object_list = projected_items
    return page_obj


def get_personal_balance_disclosure_for_space(
    *,
    viewer,
    shared_space,
    target_user,
    display_currency,
    preview_permission=None,
    rate_policy: str = "standard",
) -> dict:
    if not viewer or not shared_space or not target_user:
        return {
            "status": "ineligible",
            "label": "No shared space available.",
            "owner_username": target_user.username if target_user else "",
            "is_viewer": viewer.id == target_user.id if (viewer and target_user) else False,
            "display_label": "You" if (viewer and target_user and viewer.id == target_user.id) else (target_user.username if target_user else ""),
            "amount": None,
            "formatted_amount": None,
            "currency": display_currency,
            "is_zero": False,
            "is_stale": False,
            "latest_record_update": None,
            "rate_effective_dates": [],
            "rate_sources": [],
        }

    active_members = set(
        SharedSpaceMembership.objects.filter(
            shared_space=shared_space,
            is_active=True,
        ).values_list("user_id", flat=True)
    )
    if viewer.id not in active_members or target_user.id not in active_members:
        return {
            "status": "ineligible",
            "label": "Both members must be active in this shared space.",
            "owner_username": target_user.username,
            "is_viewer": viewer.id == target_user.id,
            "display_label": "You" if viewer.id == target_user.id else target_user.username,
            "amount": None,
            "formatted_amount": None,
            "currency": display_currency,
            "is_zero": False,
            "is_stale": False,
            "latest_record_update": None,
            "rate_effective_dates": [],
            "rate_sources": [],
        }

    is_viewer = viewer.id == target_user.id
    display_label = "You" if is_viewer else target_user.username

    if preview_permission is not None:
        perm = preview_permission
    else:
        partner_id = next((uid for uid in active_members if uid != target_user.id), None)
        if not partner_id:
            return {
                "status": "ineligible",
                "label": "No partner found in this space.",
                "owner_username": target_user.username,
                "is_viewer": is_viewer,
                "display_label": display_label,
                "amount": None,
                "formatted_amount": None,
                "currency": display_currency,
                "is_zero": False,
                "is_stale": False,
                "latest_record_update": None,
                "rate_effective_dates": [],
                "rate_sources": [],
            }
        perm = PersonalBalanceSharePermission.objects.filter(
            owner=target_user,
            recipient_id=partner_id,
            shared_space=shared_space,
            is_revoked=False,
        ).first()

    if not perm or not perm.show_balance:
        return {
            "status": "visibility_off",
            "label": "You have not shared your personal balance." if is_viewer else "This member has not shared a personal balance.",
            "owner_username": target_user.username,
            "is_viewer": is_viewer,
            "display_label": display_label,
            "amount": None,
            "formatted_amount": None,
            "currency": display_currency,
            "is_zero": False,
            "is_stale": False,
            "latest_record_update": None,
            "rate_effective_dates": [],
            "rate_sources": [],
        }

    if hasattr(perm, "selected_wallets") and hasattr(perm.selected_wallets, "all"):
        wallets_list = list(
            perm.selected_wallets.filter(
                owner=target_user,
                ownership_scope=Wallet.OwnershipScope.PERSONAL,
            ).select_related("currency")
        )
    elif isinstance(getattr(perm, "selected_wallets", None), list):
        wallets_list = perm.selected_wallets
    else:
        wallets_list = []

    active_wallets = [w for w in wallets_list if not w.is_archived]
    if not active_wallets:
        return {
            "status": "no_wallets_selected",
            "label": "No wallets currently selected for disclosure." if is_viewer else "No wallets selected.",
            "owner_username": target_user.username,
            "is_viewer": is_viewer,
            "display_label": display_label,
            "amount": None,
            "formatted_amount": None,
            "currency": display_currency,
            "is_zero": False,
            "is_stale": False,
            "latest_record_update": None,
            "rate_effective_dates": [],
            "rate_sources": [],
        }

    if not display_currency or not getattr(display_currency, "is_active", True):
        return {
            "status": "conversion_unavailable",
            "label": "Display currency unavailable.",
            "owner_username": target_user.username,
            "is_viewer": is_viewer,
            "display_label": display_label,
            "amount": None,
            "formatted_amount": None,
            "currency": display_currency,
            "is_zero": False,
            "is_stale": False,
            "latest_record_update": None,
            "rate_effective_dates": [],
            "rate_sources": [],
        }

    enrich_wallets_with_balances(active_wallets)

    total_balance = Decimal("0")
    rate_effective_dates = []
    rate_sources = set()
    latest_record_update = None
    conversion_missing = False
    has_stale_rate = False

    for w in active_wallets:
        last_tx = (
            Transaction.objects.filter(wallet=w, is_deleted=False)
            .order_by("-updated_at")
            .values_list("updated_at", flat=True)
            .first()
        )
        update_ts = last_tx or w.updated_at
        if update_ts and (latest_record_update is None or update_ts > latest_record_update):
            latest_record_update = update_ts

        if w.currency_id == display_currency.id:
            total_balance += w.current_balance
            rate_sources.add("Same currency (1:1)")
        else:
            if rate_policy == "preview":
                # For preview:
                # Use ONLY global provider rates and manual rates explicitly shared with the selected space.
                # Do NOT use either member's private manual rates.
                lookup = get_latest_rate(
                    w.currency,
                    display_currency,
                    user=viewer,
                    scope="shared",
                    shared_space=shared_space,
                )
            else:
                # Actual viewer on Shared Activity:
                # Can use their own authorized personal rate, or shared space manual rate, or global provider rate.
                lookup = get_latest_rate(
                    w.currency,
                    display_currency,
                    user=viewer,
                    scope="personal",
                    shared_space=shared_space,
                )
                if not lookup or lookup.rate is None:
                    lookup_shared = get_latest_rate(
                        w.currency,
                        display_currency,
                        user=viewer,
                        scope="shared",
                        shared_space=shared_space,
                    )
                    if lookup_shared and lookup_shared.rate is not None:
                        lookup = lookup_shared

            if not lookup or lookup.rate is None:
                conversion_missing = True
                break

            converted_val = convert_decimal_amount(w.current_balance, lookup.rate, display_currency)
            total_balance += converted_val
            if lookup.rate_timestamp:
                rate_effective_dates.append(lookup.rate_timestamp.date())
            if lookup.provider:
                rate_sources.add(lookup.provider)
            if lookup.is_stale:
                has_stale_rate = True

    if conversion_missing:
        return {
            "status": "conversion_unavailable",
            "label": "Balance unavailable — conversion required",
            "owner_username": target_user.username,
            "is_viewer": is_viewer,
            "display_label": display_label,
            "amount": None,
            "formatted_amount": None,
            "currency": display_currency,
            "is_zero": False,
            "is_stale": False,
            "latest_record_update": latest_record_update,
            "rate_effective_dates": sorted(set(rate_effective_dates)),
            "rate_sources": sorted(rate_sources),
        }

    quantum = Decimal(10) ** (-display_currency.decimal_places)
    quantized_total = total_balance.quantize(quantum, rounding=ROUND_HALF_UP)
    formatted_amt = format_currency_amount(quantized_total, display_currency)

    return {
        "status": "available",
        "label": None,
        "owner_username": target_user.username,
        "is_viewer": is_viewer,
        "display_label": display_label,
        "amount": quantized_total,
        "formatted_amount": formatted_amt,
        "currency": display_currency,
        "is_zero": quantized_total == Decimal("0"),
        "is_stale": has_stale_rate,
        "latest_record_update": latest_record_update,
        "rate_effective_dates": sorted(set(rate_effective_dates)),
        "rate_sources": sorted(rate_sources),
    }


def get_filtered_shared_activity(
    *,
    user,
    shared_space,
    query_params,
    page: int = 1,
    per_page: int = 15,
) -> dict:
    if not user or not user.is_authenticated or not shared_space:
        empty_page = Paginator([], per_page).page(1)
        return {"page_obj": empty_page, "filters": {}, "total_count": 0}

    active_member_ids = set(
        SharedSpaceMembership.objects.filter(
            shared_space=shared_space,
            is_active=True,
        ).values_list("user_id", flat=True)
    )
    if user.id not in active_member_ids:
        empty_page = Paginator([], per_page).page(1)
        return {"page_obj": empty_page, "filters": {}, "total_count": 0}

    qs = (
        PersonalTransactionShare.objects.filter(
            shared_space=shared_space,
            owner_id__in=active_member_ids,
            recipient_id__in=active_member_ids,
            is_revoked=False,
            transaction__is_deleted=False,
            transaction__scope=Transaction.Scope.PERSONAL,
            transaction__owner_id=F("owner_id"),
            transaction__transfer__isnull=True,
            transaction__is_fee=False,
            transaction__transaction_type__in=[
                Transaction.Type.INCOME,
                Transaction.Type.EXPENSE,
            ],
        )
        .filter(Q(owner=user) | Q(recipient=user))
    )

    filters = {
        "ownership": (query_params.get("ownership") or "all").strip().lower(),
        "type": (query_params.get("type") or "all").strip().lower(),
        "start_date": (query_params.get("start_date") or "").strip(),
        "end_date": (query_params.get("end_date") or "").strip(),
        "currency": (query_params.get("currency") or "").strip().upper(),
        "q": (query_params.get("q") or "").strip(),
    }

    if filters["ownership"] in ("you", "me"):
        qs = qs.filter(owner=user)
    elif filters["ownership"] in ("partner", "other"):
        qs = qs.filter(recipient=user)

    if filters["type"] in ("income", "expense"):
        qs = qs.filter(transaction__transaction_type=filters["type"])

    if filters["start_date"]:
        try:
            sd = date.fromisoformat(filters["start_date"])
            qs = qs.filter(transaction__transaction_date__gte=sd)
        except (ValueError, TypeError):
            pass

    if filters["end_date"]:
        try:
            ed = date.fromisoformat(filters["end_date"])
            qs = qs.filter(transaction__transaction_date__lte=ed)
        except (ValueError, TypeError):
            pass

    if filters["currency"]:
        qs = qs.filter(transaction__currency__code=filters["currency"])

    if filters["q"]:
        qs = qs.filter(transaction__description__icontains=filters["q"])

    qs = (
        qs.select_related(
            "transaction",
            "transaction__currency",
            "owner",
            "recipient",
        )
        .order_by("-transaction__transaction_date", "-created_at")
        .distinct()
    )

    paginator = Paginator(qs, per_page)
    try:
        page_obj = paginator.page(page)
    except Exception:
        page_obj = paginator.page(1)

    projected_items = []
    for s in page_obj.object_list:
        tx = s.transaction
        is_viewer = s.owner_id == user.id
        projected_items.append(
            SharedPersonalActivityItem(
                share_id=s.id,
                owner_username=s.owner.username,
                is_viewer_record=is_viewer,
                recorded_by_display="You" if is_viewer else s.owner.username,
                transaction_type=tx.transaction_type,
                transaction_date=tx.transaction_date,
                formatted_amount=tx.formatted_amount,
                currency_code=tx.currency.code,
                description=tx.description,
                notes=tx.notes if s.include_notes else "",
                include_notes=s.include_notes,
                created_at=s.created_at,
            )
        )
    page_obj.object_list = projected_items
    return {
        "page_obj": page_obj,
        "filters": filters,
        "total_count": paginator.count,
    }


