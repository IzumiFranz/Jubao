from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.contrib.sessions.models import Session
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import AuthRateLimitAttempt, UserPreference
from apps.budgets.models import (
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
    SyncMutationReceipt,
    Transaction,
    Transfer,
    Wallet,
)
from apps.budgets.services import (
    accept_shared_space_invitation,
    create_category,
    create_savings_goal,
    create_shared_space,
    create_shared_space_invitation,
    create_transaction_entry,
    create_transfer_entry,
    create_wallet_record,
    record_savings_contribution,
    save_category_budget,
    save_personal_balance_share_permission,
    share_personal_transaction,
    update_wallet_record,
)


USD_BASE_RATES = {
    "USD": Decimal("1.00000000"),
    "EUR": Decimal("0.92450000"),
    "JPY": Decimal("153.40000000"),
    "PHP": Decimal("58.25000000"),
    "TWD": Decimal("32.15000000"),
    "KWD": Decimal("0.30650000"),
}


def _shift_month(anchor: date, months_back: int) -> tuple[int, int]:
    year = anchor.year
    month = anchor.month - months_back
    while month <= 0:
        month += 12
        year -= 1
    return year, month


def _period_str(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def _safe_day(year: int, month: int, day: int, cap_date: date | None = None) -> date:
    if month == 12:
        next_first = date(year + 1, 1, 1)
    else:
        next_first = date(year, month + 1, 1)
    max_day = (next_first - timedelta(days=1)).day
    d = date(year, month, min(day, max_day))
    if cap_date is not None and d > cap_date:
        return cap_date
    return d


class Command(BaseCommand):
    help = "Clean non-catalog data and optionally populate realistic portfolio demo data for Jubao."

    def add_arguments(self, parser):
        parser.add_argument(
            "--clean",
            action="store_true",
            help="Delete existing users, transactions, wallets, budgets, goals, shared spaces, and rates first.",
        )
        parser.add_argument(
            "--clean-only",
            action="store_true",
            help="Only clean the database (keeping the Currency catalog) without seeding demo data.",
        )

    def handle(self, *args, **options):
        if options["clean"] or options["clean_only"]:
            self._clean_database()
            self.stdout.write(self.style.SUCCESS("Cleaned database (Currency catalog preserved)."))

        if options["clean_only"]:
            return

        if User.objects.exists():
            self.stdout.write(
                self.style.WARNING("Users already exist. Re-run with --clean to reset and populate fresh data.")
            )
            return

        summary = self._seed_portfolio_data()
        self.stdout.write(self.style.SUCCESS("Populated portfolio dataset successfully:"))
        for line in summary:
            self.stdout.write(f"  - {line}")

    @transaction.atomic
    def _clean_database(self):
        SyncMutationReceipt.objects.all().delete()
        PersonalTransactionShare.objects.all().delete()
        PersonalBalanceSharePermission.objects.all().delete()
        SavingsContribution.objects.all().delete()
        SavingsGoal.objects.all().delete()
        CategoryBudget.objects.all().delete()
        Transfer.objects.update(fee_transaction=None)
        Transaction.objects.all().delete()
        Transfer.objects.all().delete()
        Category.objects.all().delete()
        Wallet.objects.all().delete()
        ExchangeRate.objects.all().delete()
        SharedSpaceInvitation.objects.all().delete()
        SharedSpaceMembership.objects.all().delete()
        UserPreference.objects.all().delete()
        SharedSpace.objects.all().delete()
        AuthRateLimitAttempt.objects.all().delete()
        Session.objects.all().delete()
        User.objects.all().delete()

    @transaction.atomic
    def _seed_portfolio_data(self) -> list[str]:
        currencies = {c.code: c for c in Currency.objects.all()}
        usd = currencies["USD"]
        eur = currencies["EUR"]
        jpy = currencies["JPY"]
        php = currencies["PHP"]
        twd = currencies["TWD"]

        today = timezone.localdate()
        y_m2, m_m2 = _shift_month(today, 2)
        y_m1, m_m1 = _shift_month(today, 1)
        y_cur, m_cur = _shift_month(today, 0)

        opening_date = date(y_m2, m_m2, 1) - timedelta(days=15)

        # 1. Demo Users
        franz = User.objects.create_user(
            username="franz",
            email="franz@jubao.app",
            password="JubaoDemo!2026",
            first_name="Franz",
            last_name="Rivera",
        )
        maya = User.objects.create_user(
            username="maya",
            email="maya@jubao.app",
            password="JubaoDemo!2026",
            first_name="Maya",
            last_name="Lin",
        )

        UserPreference.objects.create(
            user=franz,
            personal_base_currency=usd,
            display_currency=usd,
            timezone="Asia/Manila",
            week_starts_on=UserPreference.WeekStartChoice.MONDAY,
            active_scope=UserPreference.ScopeChoice.PERSONAL,
        )
        UserPreference.objects.create(
            user=maya,
            personal_base_currency=usd,
            display_currency=usd,
            timezone="Asia/Manila",
            week_starts_on=UserPreference.WeekStartChoice.MONDAY,
            active_scope=UserPreference.ScopeChoice.PERSONAL,
        )

        # 2. Shared Space & Memberships
        home_space = create_shared_space(user=franz, name="Home & Travels", base_currency=usd)
        invite_maya = create_shared_space_invitation(
            user=franz,
            shared_space=home_space,
            partner_hint="Maya",
            expiry="7d",
        )
        accept_shared_space_invitation(user=maya, code=invite_maya.code)

        # 3. Exchange Rates (global provider rates across every day from opening_date..today + 1 personal override)
        now_ts = timezone.now()
        tz = timezone.get_current_timezone()
        rate_rows = []
        cursor = opening_date
        while cursor <= today:
            day_ts = timezone.make_aware(datetime.combine(cursor, time(9, 0)), tz)
            if day_ts > now_ts:
                day_ts = now_ts
            for code, to_cur in currencies.items():
                if code == "USD":
                    continue
                usd_to_target = USD_BASE_RATES[code]
                target_to_usd = (Decimal("1") / usd_to_target).quantize(Decimal("0.00000001"))
                rate_rows.append(
                    ExchangeRate(
                        from_currency=usd,
                        to_currency=to_cur,
                        rate=usd_to_target,
                        provider="frankfurter_v2",
                        is_manual=False,
                        scope=ExchangeRate.Scope.PROVIDER,
                        rate_timestamp=day_ts,
                    )
                )
                rate_rows.append(
                    ExchangeRate(
                        from_currency=to_cur,
                        to_currency=usd,
                        rate=target_to_usd,
                        provider="frankfurter_v2",
                        is_manual=False,
                        scope=ExchangeRate.Scope.PROVIDER,
                        rate_timestamp=day_ts,
                    )
                )
            cursor += timedelta(days=1)

        # Cross-pair provider rates for today so the Currency Rate table looks rich
        for f_code, f_cur in currencies.items():
            for t_code, t_cur in currencies.items():
                if f_code == t_code or f_code == "USD" or t_code == "USD":
                    continue
                cross = (USD_BASE_RATES[t_code] / USD_BASE_RATES[f_code]).quantize(Decimal("0.00000001"))
                rate_rows.append(
                    ExchangeRate(
                        from_currency=f_cur,
                        to_currency=t_cur,
                        rate=cross,
                        provider="frankfurter_v2",
                        is_manual=False,
                        scope=ExchangeRate.Scope.PROVIDER,
                        rate_timestamp=now_ts,
                    )
                )

        ExchangeRate.objects.bulk_create(rate_rows)

        # One explicit personal manual override rate
        ExchangeRate.objects.create(
            from_currency=usd,
            to_currency=twd,
            rate=Decimal("32.40000000"),
            provider="Manual (Airport FX)",
            is_manual=True,
            scope=ExchangeRate.Scope.PERSONAL,
            rate_timestamp=now_ts,
            created_by=franz,
        )

        # 4. Personal Wallets (Franz + Maya)
        w_checking = create_wallet_record(
            user=franz,
            name="Main Checking",
            wallet_type=Wallet.WalletType.BANK_ACCOUNT,
            currency=usd,
            opening_balance=Decimal("4250.00"),
            opening_balance_date=opening_date,
        )
        w_cash = create_wallet_record(
            user=franz,
            name="Cash Wallet",
            wallet_type=Wallet.WalletType.CASH,
            currency=usd,
            opening_balance=Decimal("280.00"),
            opening_balance_date=opening_date,
        )
        w_gcash = create_wallet_record(
            user=franz,
            name="Manila E-Wallet",
            wallet_type=Wallet.WalletType.E_WALLET,
            currency=php,
            opening_balance=Decimal("26500.00"),
            opening_balance_date=opening_date,
        )
        w_kyoto = create_wallet_record(
            user=franz,
            name="Kyoto Trip Yen",
            wallet_type=Wallet.WalletType.CASH,
            currency=jpy,
            opening_balance=Decimal("85000"),
            opening_balance_date=opening_date,
        )
        w_legacy = create_wallet_record(
            user=franz,
            name="Legacy Euro Card",
            wallet_type=Wallet.WalletType.OTHER,
            currency=eur,
            opening_balance=Decimal("185.00"),
            opening_balance_date=opening_date,
        )
        update_wallet_record(
            user=franz,
            wallet=w_legacy,
            name="Legacy Euro Card",
            wallet_type=Wallet.WalletType.OTHER,
            is_archived=True,
        )

        maya_checking = create_wallet_record(
            user=maya,
            name="Maya Savings",
            wallet_type=Wallet.WalletType.BANK_ACCOUNT,
            currency=usd,
            opening_balance=Decimal("3150.00"),
            opening_balance_date=opening_date,
        )

        # 5. Personal Categories
        cat_salary = create_category(user=franz, name="Salary", kind=Category.Kind.INCOME)
        cat_freelance = create_category(user=franz, name="Freelance Design", kind=Category.Kind.INCOME)
        cat_housing = create_category(user=franz, name="Housing & Rent", kind=Category.Kind.EXPENSE)
        cat_groceries = create_category(user=franz, name="Groceries", kind=Category.Kind.EXPENSE)
        cat_dining = create_category(user=franz, name="Dining & Coffee", kind=Category.Kind.EXPENSE)
        cat_utilities = create_category(user=franz, name="Utilities & Fiber", kind=Category.Kind.EXPENSE)
        cat_transport = create_category(user=franz, name="Transport & Transit", kind=Category.Kind.EXPENSE)
        cat_software = create_category(user=franz, name="Software & Tools", kind=Category.Kind.EXPENSE)
        cat_travel = create_category(user=franz, name="Travel & Culture", kind=Category.Kind.EXPENSE)
        cat_wellness = create_category(user=franz, name="Health & Wellness", kind=Category.Kind.EXPENSE)

        maya_coffee = create_category(user=maya, name="Dining & Coffee", kind=Category.Kind.EXPENSE)
        maya_income = create_category(user=maya, name="Consulting", kind=Category.Kind.INCOME)

        # 6. Personal Transactions across 3 months
        personal_tx_specs = [
            # Two months ago
            (y_m2, m_m2, 1, "income", "3850.00", w_checking, cat_salary, "Monthly Salary — Studio", "Direct deposit"),
            (y_m2, m_m2, 3, "expense", "1150.00", w_checking, cat_housing, "Apartment Rent", ""),
            (y_m2, m_m2, 6, "expense", "210.40", w_checking, cat_groceries, "Trader Joe's Weekly Run", ""),
            (y_m2, m_m2, 9, "expense", "74.50", w_checking, cat_utilities, "Fiber Internet & Electric", ""),
            (y_m2, m_m2, 14, "income", "640.00", w_checking, cat_freelance, "Brand Identity Sprint", "Client invoice #104"),
            (y_m2, m_m2, 16, "expense", "48.20", w_cash, cat_dining, "Ramen & Matcha with friends", ""),
            (y_m2, m_m2, 21, "expense", "4650.00", w_gcash, cat_groceries, "S&R Pantry Stock-up", ""),
            (y_m2, m_m2, 25, "expense", "89.00", w_checking, cat_software, "Cloud & Figma Suite", ""),

            # Last month
            (y_m1, m_m1, 1, "income", "3850.00", w_checking, cat_salary, "Monthly Salary — Studio", "Direct deposit"),
            (y_m1, m_m1, 2, "expense", "1150.00", w_checking, cat_housing, "Apartment Rent", ""),
            (y_m1, m_m1, 5, "expense", "195.80", w_checking, cat_groceries, "Whole Foods & Produce", ""),
            (y_m1, m_m1, 8, "expense", "68.90", w_checking, cat_utilities, "City Power & Water", ""),
            (y_m1, m_m1, 11, "expense", "38.50", w_cash, cat_dining, "Specialty Coffee & Pastries", ""),
            (y_m1, m_m1, 15, "income", "920.00", w_checking, cat_freelance, "Portfolio Microsite UI", "Final milestone"),
            (y_m1, m_m1, 18, "expense", "18500", w_kyoto, cat_travel, "JR Shinkansen Ticket Reserve", "Tokyo -> Kyoto leg"),
            (y_m1, m_m1, 20, "expense", "6200", w_kyoto, cat_dining, "Izakaya & Tea House Deposit", ""),
            (y_m1, m_m1, 24, "expense", "92.00", w_checking, cat_software, "Design Tools & Hosting", ""),
            (y_m1, m_m1, 27, "expense", "64.00", w_checking, cat_wellness, "Gym & Physiotherapy", ""),

            # Current month (capped at today)
            (y_cur, m_cur, 1, "income", "3950.00", w_checking, cat_salary, "Monthly Salary — Studio", "November payroll"),
            (y_cur, m_cur, 2, "expense", "1150.00", w_checking, cat_housing, "Apartment Rent", "Auto-debit"),
            (y_cur, m_cur, 3, "expense", "72.40", w_checking, cat_utilities, "Fiber & Municipal Water", ""),
            (y_cur, m_cur, 4, "expense", "168.50", w_checking, cat_groceries, "Weekly Farmers Market & Groceries", "Organic produce & pantry"),
            (y_cur, m_cur, 5, "expense", "42.80", w_cash, cat_dining, "Pour-over Coffee & Croissants", "Morning catchup"),
            (y_cur, m_cur, 6, "expense", "129.00", w_checking, cat_software, "Annual Productivity & AI Suite", "Over monthly budget cap"),
            (y_cur, m_cur, 7, "expense", "58.20", w_checking, cat_transport, "Metro Pass & Airport Shuttle", ""),
            (y_cur, m_cur, 8, "expense", "5400.00", w_gcash, cat_groceries, "Local Market & Pantry Restock", "Paid via PHP e-wallet"),
            (y_cur, m_cur, 9, "expense", "84.00", w_checking, cat_dining, "Bistro Dinner", "Celebrated milestone"),
            (y_cur, m_cur, 9, "expense", "14800", w_kyoto, cat_travel, "Ryokan Museum & Temple Passes", "Kyoto autumn booking"),
            (y_cur, m_cur, 10, "income", "750.00", w_checking, cat_freelance, "Design System Audit", "Consulting retainer"),
            (y_cur, m_cur, 10, "expense", "68.50", w_checking, cat_wellness, "Dental Checkup & Vitamins", ""),
            (y_cur, m_cur, 11, "expense", "94.00", w_checking, cat_dining, "Weekend Brunch & Matcha", "With Maya"),
        ]

        created_personal_txs = []
        for yr, mo, dy, ttype, amt, wal, cat, desc, note in personal_tx_specs:
            cap = today if (yr == y_cur and mo == m_cur) else None
            tx_date = _safe_day(yr, mo, dy, cap_date=cap)
            tx = create_transaction_entry(
                user=franz,
                transaction_type=ttype,
                amount=Decimal(amt),
                wallet=wal,
                transaction_date=tx_date,
                category=cat,
                description=desc,
                notes=note,
            )
            created_personal_txs.append(tx)

        # Personal Transfers (1 same-currency, 1 cross-currency with fee)
        create_transfer_entry(
            user=franz,
            source_wallet=w_checking,
            destination_wallet=w_cash,
            amount_sent=Decimal("200.00"),
            transfer_date=_safe_day(y_cur, m_cur, 3, cap_date=today),
            notes="Monthly pocket cash top-up",
        )
        create_transfer_entry(
            user=franz,
            source_wallet=w_checking,
            destination_wallet=w_kyoto,
            amount_sent=Decimal("300.00"),
            amount_received=Decimal("46020"),
            fee_amount=Decimal("4.50"),
            transfer_date=_safe_day(y_m1, m_m1, 14),
            notes="Topped up Kyoto travel yen",
        )

        # Maya's personal transactions (for sharing into Shared Space)
        maya_tx_1 = create_transaction_entry(
            user=maya,
            transaction_type=Transaction.Type.EXPENSE,
            amount=Decimal("64.50"),
            wallet=maya_checking,
            transaction_date=_safe_day(y_cur, m_cur, 8, cap_date=today),
            category=maya_coffee,
            description="Artisan Bakery & Matcha Boxes",
            notes="Picked up treats for Sunday brunch",
        )
        maya_tx_2 = create_transaction_entry(
            user=maya,
            transaction_type=Transaction.Type.INCOME,
            amount=Decimal("480.00"),
            wallet=maya_checking,
            transaction_date=_safe_day(y_cur, m_cur, 5, cap_date=today),
            category=maya_income,
            description="UX Workshop Facilitation",
            notes="Weekend workshop honorarium",
        )

        # 7. Personal Category Budgets (cur month + prev month)
        cur_period = _period_str(y_cur, m_cur)
        prev_period = _period_str(y_m1, m_m1)
        budget_specs = [
            (cat_groceries, cur_period, "450.00"),   # ~58% used (healthy)
            (cat_dining, cur_period, "260.00"),      # ~85% used (near limit)
            (cat_software, cur_period, "120.00"),    # ~108% used (over budget)
            (cat_transport, cur_period, "140.00"),   # ~42% used
            (cat_travel, cur_period, "320.00"),      # ~30% used
            (cat_groceries, prev_period, "450.00"),
            (cat_dining, prev_period, "250.00"),
        ]
        for cat, period, limit in budget_specs:
            save_category_budget(
                user=franz,
                category=cat,
                period=period,
                currency=usd,
                limit_amount=Decimal(limit),
            )

        # 8. Shared Space Wallets, Categories, Transactions, Budgets
        sh_joint = create_wallet_record(
            user=franz,
            name="Joint Household Account",
            wallet_type=Wallet.WalletType.BANK_ACCOUNT,
            currency=usd,
            opening_balance=Decimal("2400.00"),
            opening_balance_date=opening_date,
            ownership_scope=Wallet.OwnershipScope.SHARED,
            shared_space=home_space,
        )
        sh_petty = create_wallet_record(
            user=maya,
            name="Shared Petty Cash",
            wallet_type=Wallet.WalletType.CASH,
            currency=usd,
            opening_balance=Decimal("180.00"),
            opening_balance_date=opening_date,
            ownership_scope=Wallet.OwnershipScope.SHARED,
            shared_space=home_space,
        )

        sh_cat_contrib = create_category(
            user=franz,
            name="Shared Contribution",
            kind=Category.Kind.INCOME,
            scope=Category.Scope.SHARED,
            shared_space=home_space,
        )
        sh_cat_groceries = create_category(
            user=franz,
            name="Household Groceries",
            kind=Category.Kind.EXPENSE,
            scope=Category.Scope.SHARED,
            shared_space=home_space,
        )
        sh_cat_utilities = create_category(
            user=maya,
            name="Home Utilities",
            kind=Category.Kind.EXPENSE,
            scope=Category.Scope.SHARED,
            shared_space=home_space,
        )
        sh_cat_dates = create_category(
            user=maya,
            name="Date Nights & Trips",
            kind=Category.Kind.EXPENSE,
            scope=Category.Scope.SHARED,
            shared_space=home_space,
        )
        sh_cat_supplies = create_category(
            user=franz,
            name="Home Supplies",
            kind=Category.Kind.EXPENSE,
            scope=Category.Scope.SHARED,
            shared_space=home_space,
        )

        shared_tx_specs = [
            (franz, franz, y_m1, m_m1, 2, "income", "900.00", sh_joint, sh_cat_contrib, "Franz Monthly Pool"),
            (maya, maya, y_m1, m_m1, 2, "income", "900.00", sh_joint, sh_cat_contrib, "Maya Monthly Pool"),
            (franz, franz, y_m1, m_m1, 10, "expense", "245.60", sh_joint, sh_cat_groceries, "Costco Household Run"),
            (maya, maya, y_m1, m_m1, 16, "expense", "134.00", sh_joint, sh_cat_utilities, "Electricity & Water"),
            (maya, maya, y_m1, m_m1, 22, "expense", "118.50", sh_joint, sh_cat_dates, "Anniversary Sushi Dinner"),

            (franz, franz, y_cur, m_cur, 1, "income", "950.00", sh_joint, sh_cat_contrib, "Franz November Pool"),
            (maya, maya, y_cur, m_cur, 1, "income", "950.00", sh_joint, sh_cat_contrib, "Maya November Pool"),
            (franz, franz, y_cur, m_cur, 4, "expense", "182.40", sh_joint, sh_cat_groceries, "Weekly Farmers Market"),
            (maya, maya, y_cur, m_cur, 6, "expense", "128.00", sh_joint, sh_cat_utilities, "Shared Fiber & Gas"),
            (franz, franz, y_cur, m_cur, 7, "expense", "86.50", sh_petty, sh_cat_supplies, "Ceramic Bowls & Tea Towels"),
            (maya, maya, y_cur, m_cur, 9, "expense", "142.00", sh_joint, sh_cat_dates, "Jazz Night & Ramen"),
            (franz, franz, y_cur, m_cur, 10, "expense", "112.30", sh_joint, sh_cat_groceries, "Mid-week Grocery Top-up"),
        ]
        for actor, payer, yr, mo, dy, ttype, amt, wal, cat, desc in shared_tx_specs:
            cap = today if (yr == y_cur and mo == m_cur) else None
            create_transaction_entry(
                user=actor,
                paid_by=payer if ttype == "expense" else None,
                transaction_type=ttype,
                amount=Decimal(amt),
                wallet=wal,
                transaction_date=_safe_day(yr, mo, dy, cap_date=cap),
                category=cat,
                scope=Transaction.Scope.SHARED,
                shared_space=home_space,
                description=desc,
            )

        save_category_budget(
            user=franz,
            category=sh_cat_groceries,
            period=cur_period,
            currency=usd,
            limit_amount=Decimal("500.00"),
            scope=CategoryBudget.Scope.SHARED,
            shared_space=home_space,
        )
        save_category_budget(
            user=maya,
            category=sh_cat_dates,
            period=cur_period,
            currency=usd,
            limit_amount=Decimal("220.00"),
            scope=CategoryBudget.Scope.SHARED,
            shared_space=home_space,
        )
        save_category_budget(
            user=franz,
            category=sh_cat_supplies,
            period=cur_period,
            currency=usd,
            limit_amount=Decimal("150.00"),
            scope=CategoryBudget.Scope.SHARED,
            shared_space=home_space,
        )

        # 9. Savings Goals & Contributions (Personal + Shared)
        g_emergency = create_savings_goal(
            user=franz,
            name="Emergency Reserve",
            currency=usd,
            target_amount=Decimal("6000.00"),
            target_date=today + timedelta(days=180),
        )
        record_savings_contribution(
            user=franz,
            goal=g_emergency,
            amount=Decimal("2500.00"),
            contribution_date=_safe_day(y_m2, m_m2, 15),
            notes="Initial safety net transfer",
        )
        record_savings_contribution(
            user=franz,
            goal=g_emergency,
            amount=Decimal("1000.00"),
            contribution_date=_safe_day(y_m1, m_m1, 15),
            notes="October paycheck allocation",
        )
        record_savings_contribution(
            user=franz,
            goal=g_emergency,
            amount=Decimal("850.00"),
            contribution_date=_safe_day(y_cur, m_cur, 5, cap_date=today),
            notes="November savings transfer",
        )

        g_kyoto = create_savings_goal(
            user=franz,
            name="Kyoto Autumn Trip",
            currency=jpy,
            target_amount=Decimal("150000"),
            target_date=today + timedelta(days=25),
        )
        record_savings_contribution(
            user=franz,
            goal=g_kyoto,
            amount=Decimal("90000"),
            contribution_date=_safe_day(y_m1, m_m1, 10),
            notes="Flight & hotel reserve",
        )
        record_savings_contribution(
            user=franz,
            goal=g_kyoto,
            amount=Decimal("60000"),
            contribution_date=_safe_day(y_cur, m_cur, 4, cap_date=today),
            notes="Goal reached!",
        )

        g_studio = create_savings_goal(
            user=franz,
            name="Studio Display Upgrade",
            currency=usd,
            target_amount=Decimal("1600.00"),
            target_date=today + timedelta(days=120),
        )
        record_savings_contribution(
            user=franz,
            goal=g_studio,
            amount=Decimal("640.00"),
            contribution_date=_safe_day(y_cur, m_cur, 7, cap_date=today),
            notes="Freelance bonus allocation",
        )

        g_shared_home = create_savings_goal(
            user=franz,
            name="Apartment Deposit Fund",
            currency=usd,
            target_amount=Decimal("4000.00"),
            target_date=today + timedelta(days=150),
            scope=SavingsGoal.Scope.SHARED,
            shared_space=home_space,
        )
        record_savings_contribution(
            user=franz,
            goal=g_shared_home,
            amount=Decimal("1500.00"),
            contribution_date=_safe_day(y_m1, m_m1, 20),
            notes="Franz contribution",
        )
        record_savings_contribution(
            user=maya,
            goal=g_shared_home,
            amount=Decimal("1250.00"),
            contribution_date=_safe_day(y_cur, m_cur, 6, cap_date=today),
            notes="Maya contribution",
        )

        # 10. Personal Transaction Shares & Balance Disclosure (for Shared Activity & Privacy pages)
        share_personal_transaction(
            user=franz,
            transaction_id=created_personal_txs[-1].id,  # Weekend Brunch & Matcha
            shared_space_id=home_space.id,
            include_notes=True,
        )
        share_personal_transaction(
            user=franz,
            transaction_id=created_personal_txs[-3].id,  # Ryokan Museum & Temple Passes
            shared_space_id=home_space.id,
            include_notes=False,
        )
        share_personal_transaction(
            user=maya,
            transaction_id=maya_tx_1.id,
            shared_space_id=home_space.id,
            include_notes=True,
        )
        share_personal_transaction(
            user=maya,
            transaction_id=maya_tx_2.id,
            shared_space_id=home_space.id,
            include_notes=False,
        )

        save_personal_balance_share_permission(
            user=franz,
            shared_space_id=home_space.id,
            show_balance=True,
            wallet_ids=[w_checking.id, w_cash.id],
        )
        save_personal_balance_share_permission(
            user=maya,
            shared_space_id=home_space.id,
            show_balance=True,
            wallet_ids=[maya_checking.id],
        )

        return [
            "Users: franz / maya (password: JubaoDemo!2026)",
            f"Shared Space: {home_space.name} (2 active members)",
            f"Wallets: {Wallet.objects.count()} (personal multi-currency + archived + shared)",
            f"Transactions: {Transaction.objects.count()} (including {Transfer.objects.count()} transfers)",
            f"Budgets: {CategoryBudget.objects.count()} across {prev_period} and {cur_period}",
            f"Savings Goals: {SavingsGoal.objects.count()} ({SavingsContribution.objects.count()} contributions)",
            f"Exchange Rates: {ExchangeRate.objects.count()} (provider + manual rate)",
            f"Sharing Grants: {PersonalTransactionShare.objects.count()} shared txs, {PersonalBalanceSharePermission.objects.count()} balance disclosures",
        ]
