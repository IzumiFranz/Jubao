from datetime import date
from decimal import Decimal
from importlib import import_module

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection
from django.db.models.deletion import ProtectedError
from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse

from django.utils import timezone
from apps.accounts.models import UserPreference

from .currency_catalog import SUPPORTED_CURRENCIES
from .models import Category, Currency, Transaction, Wallet, format_currency_amount

seed_migration = import_module("apps.budgets.migrations.0002_seed_supported_currencies")
twd_migration = import_module("apps.budgets.migrations.0003_add_twd_currency")

User = get_user_model()


class CurrencyAndWalletTests(TestCase):
    APPLICATION_ROUTES = [
        "budgets:dashboard",
        "budgets:transactions",
        "budgets:budgets",
        "budgets:shared_space",
        "budgets:wallets",
        "budgets:savings_goals",
        "budgets:reports",
        "budgets:currencies",
        "accounts:settings",
    ]

    def setUp(self):
        self.user = User.objects.create_user(
            username="wallet_owner",
            password="Str0ng!Password#2026",
        )
        self.other_user = User.objects.create_user(
            username="other_user",
            password="Str0ng!Password#2026",
        )
        self.wallets_url = reverse("budgets:wallets")
        self.currencies_url = reverse("budgets:currencies")
        self.login_url = reverse("accounts:login")

    def _get_or_create_currency(self, code="USD", name="US Dollar", decimal_places=2, is_active=True):
        currency, created = Currency.objects.get_or_create(
            code=code,
            defaults={
                "name": name,
                "decimal_places": decimal_places,
                "is_active": is_active,
            },
        )
        if not created:
            currency.name = name
            currency.decimal_places = decimal_places
            currency.is_active = is_active
            currency.full_clean()
            currency.save()
        return currency

    def test_reference_currencies_populated_by_migration(self):
        expected = {
            "EUR": ("Euro", 2, True),
            "JPY": ("Japanese Yen", 0, True),
            "KWD": ("Kuwaiti Dinar", 3, True),
            "PHP": ("Philippine Peso", 2, True),
            "TWD": ("New Taiwan dollar", 2, True),
            "USD": ("US Dollar", 2, True),
        }
        actual = {
            c.code: (c.name, c.decimal_places, c.is_active)
            for c in Currency.objects.all()
        }
        self.assertEqual(actual, expected)
        catalog_map = {
            spec.code: (spec.name, spec.decimal_places, True)
            for spec in SUPPORTED_CURRENCIES
        }
        self.assertEqual(actual, catalog_map)

    def test_twd_migration_and_wallet_and_preference_support(self):
        twd = Currency.objects.get(code="TWD")
        self.assertEqual(twd.name, "New Taiwan dollar")
        self.assertEqual(twd.decimal_places, 2)
        self.assertTrue(twd.is_active)

        self.client.force_login(self.user)

        pref_resp = self.client.post(
            self.currencies_url,
            {"personal_base_currency": twd.pk},
            follow=True,
        )
        self.assertRedirects(pref_resp, self.currencies_url)
        self.assertEqual(
            UserPreference.objects.get(user=self.user).personal_base_currency,
            twd,
        )
        self.assertContains(pref_resp, "TWD (New Taiwan dollar)")

        wallet_resp = self.client.post(
            self.wallets_url,
            {
                "name": "Taipei Cash Wallet",
                "wallet_type": Wallet.WalletType.CASH,
                "currency": twd.pk,
                "opening_balance": "3250.50",
                "opening_balance_date": "2026-10-24",
            },
        )
        self.assertRedirects(wallet_resp, self.wallets_url)
        wallet = Wallet.objects.get(owner=self.user, name="Taipei Cash Wallet")
        self.assertEqual(wallet.currency, twd)
        self.assertEqual(wallet.formatted_opening_balance, "3,250.50")

        excess_resp = self.client.post(
            self.wallets_url,
            {
                "name": "Invalid TWD Precision",
                "wallet_type": Wallet.WalletType.CASH,
                "currency": twd.pk,
                "opening_balance": "100.125",
                "opening_balance_date": "2026-10-24",
            },
        )
        self.assertEqual(excess_resp.status_code, 200)
        self.assertTrue(excess_resp.context["form"].errors.get("opening_balance"))

        twd.is_active = False
        twd.name = "Custom Taiwan Dollar"
        twd.save()
        with self.assertLogs(twd_migration.logger, level="WARNING") as cm:
            twd_migration.add_twd_currency(apps, connection.schema_editor())
        twd.refresh_from_db()
        self.assertFalse(twd.is_active)
        self.assertEqual(twd.name, "Custom Taiwan Dollar")
        self.assertTrue(any("TWD" in msg for msg in cm.output))

    def test_seed_migration_preserves_existing_definitions_and_inactive_states(self):
        usd = Currency.objects.get(code="USD")
        usd.is_active = False
        usd.save()

        php = Currency.objects.get(code="PHP")
        php.name = "Custom Peso Label"
        php.save()

        Currency.objects.filter(code="KWD").delete()

        with self.assertLogs(seed_migration.logger, level="WARNING") as cm:
            seed_migration.seed_supported_currencies(apps, connection.schema_editor())

        usd.refresh_from_db()
        php.refresh_from_db()
        kwd = Currency.objects.get(code="KWD")

        self.assertFalse(usd.is_active)
        self.assertEqual(php.name, "Custom Peso Label")
        self.assertTrue(kwd.is_active)
        self.assertEqual(kwd.decimal_places, 3)
        self.assertTrue(any("PHP" in msg for msg in cm.output))

    def test_wallet_creation_works_with_migrated_currencies_without_admin_setup(self):
        php = Currency.objects.get(code="PHP")
        self.client.force_login(self.user)

        response = self.client.post(
            self.wallets_url,
            {
                "name": "Manila Payroll Account",
                "wallet_type": Wallet.WalletType.BANK_ACCOUNT,
                "currency": php.pk,
                "opening_balance": "18500.25",
                "opening_balance_date": "2026-10-24",
            },
        )
        self.assertRedirects(response, self.wallets_url)
        wallet = Wallet.objects.get(owner=self.user, name="Manila Payroll Account")
        self.assertEqual(wallet.currency, php)
        self.assertEqual(wallet.formatted_opening_balance, "18,500.25")

    def test_anonymous_wallet_get_and_post_redirect_to_login(self):
        usd = Currency.objects.get(code="USD")
        get_response = self.client.get(self.wallets_url)
        self.assertRedirects(get_response, f"{self.login_url}?next={self.wallets_url}")

        post_response = self.client.post(
            self.wallets_url,
            {
                "name": "Anon Wallet",
                "wallet_type": Wallet.WalletType.CASH,
                "currency": usd.pk,
                "opening_balance": "50.00",
                "opening_balance_date": "2026-10-23",
            },
        )
        self.assertRedirects(post_response, f"{self.login_url}?next={self.wallets_url}")
        self.assertEqual(Wallet.objects.count(), 0)

    def test_currency_code_and_precision_validation(self):
        for places, code in [(0, "KRW"), (2, "GBP"), (3, "BHD")]:
            currency = Currency(code=code, name=f"Currency {code}", decimal_places=places)
            currency.full_clean()

        invalid_codes = ["usd", "US", "USDD", "U1D", "123", ""]
        for bad_code in invalid_codes:
            with self.subTest(bad_code=bad_code):
                currency = Currency(code=bad_code, name="Invalid Code", decimal_places=2)
                with self.assertRaises(ValidationError):
                    currency.full_clean()

        invalid_precisions = [1, 4, 5, 8]
        for bad_places in invalid_precisions:
            with self.subTest(bad_places=bad_places):
                currency = Currency(code="TST", name="Invalid Precision", decimal_places=bad_places)
                with self.assertRaises(ValidationError):
                    currency.full_clean()

    def test_wallet_creation_disabled_and_rejected_when_no_active_currencies_exist(self):
        Currency.objects.update(is_active=False)
        self.client.force_login(self.user)

        get_response = self.client.get(self.wallets_url)
        self.assertEqual(get_response.status_code, 200)
        self.assertContains(
            get_response,
            "No active currencies are currently available for wallet creation.",
        )

        post_response = self.client.post(
            self.wallets_url,
            {
                "name": "Forged Wallet",
                "wallet_type": Wallet.WalletType.CASH,
                "currency": "",
                "opening_balance": "10.00",
                "opening_balance_date": "2026-10-23",
            },
        )
        self.assertEqual(post_response.status_code, 200)
        self.assertContains(
            post_response,
            "No active currencies are currently available for wallet creation.",
        )
        self.assertEqual(Wallet.objects.count(), 0)

    def test_successful_wallet_creation_assigns_authenticated_owner_and_redirects(self):
        usd = Currency.objects.get(code="USD")
        self.client.force_login(self.user)

        response = self.client.post(
            self.wallets_url,
            {
                "name": "Primary Checking",
                "wallet_type": Wallet.WalletType.BANK_ACCOUNT,
                "currency": usd.pk,
                "opening_balance": "1250.50",
                "opening_balance_date": "2026-10-01",
            },
        )
        self.assertRedirects(response, self.wallets_url)
        self.assertEqual(Wallet.objects.count(), 1)

        wallet = Wallet.objects.get()
        self.assertEqual(wallet.owner, self.user)
        self.assertEqual(wallet.name, "Primary Checking")
        self.assertEqual(wallet.wallet_type, Wallet.WalletType.BANK_ACCOUNT)
        self.assertEqual(wallet.currency, usd)
        self.assertEqual(wallet.opening_balance, Decimal("1250.500"))
        self.assertEqual(wallet.formatted_opening_balance, "1,250.50")
        self.assertEqual(wallet.opening_balance_date, date(2026, 10, 1))

    def test_submitted_owner_id_cannot_assign_wallet_to_another_user(self):
        usd = Currency.objects.get(code="USD")
        self.client.force_login(self.user)

        response = self.client.post(
            self.wallets_url,
            {
                "owner": self.other_user.pk,
                "name": "Attempted Spoof Wallet",
                "wallet_type": Wallet.WalletType.CASH,
                "currency": usd.pk,
                "opening_balance": "40.00",
                "opening_balance_date": "2026-10-23",
            },
        )
        self.assertRedirects(response, self.wallets_url)
        wallet = Wallet.objects.get()
        self.assertEqual(wallet.owner, self.user)
        self.assertNotEqual(wallet.owner, self.other_user)

    def test_users_cannot_see_each_others_wallets(self):
        usd = Currency.objects.get(code="USD")
        Wallet.objects.create(
            owner=self.user,
            name="Alice Private Cash",
            wallet_type=Wallet.WalletType.CASH,
            currency=usd,
            opening_balance=Decimal("95.25"),
            opening_balance_date=date(2026, 10, 15),
        )
        Wallet.objects.create(
            owner=self.other_user,
            name="Bob Secret Bank",
            wallet_type=Wallet.WalletType.BANK_ACCOUNT,
            currency=usd,
            opening_balance=Decimal("500.00"),
            opening_balance_date=date(2026, 10, 16),
        )

        self.client.force_login(self.user)
        user_response = self.client.get(self.wallets_url)
        self.assertContains(user_response, "Alice Private Cash")
        self.assertContains(user_response, "95.25")
        self.assertNotContains(user_response, "Bob Secret Bank")

        self.client.force_login(self.other_user)
        other_response = self.client.get(self.wallets_url)
        self.assertContains(other_response, "Bob Secret Bank")
        self.assertContains(other_response, "500.00")
        self.assertNotContains(other_response, "Alice Private Cash")

    def test_inactive_and_nonexistent_currencies_rejected_on_forged_posts(self):
        inactive = self._get_or_create_currency(
            code="CHF", name="Swiss Franc", decimal_places=2, is_active=False
        )
        self.client.force_login(self.user)

        inactive_resp = self.client.post(
            self.wallets_url,
            {
                "name": "Inactive Currency Wallet",
                "wallet_type": Wallet.WalletType.CASH,
                "currency": inactive.pk,
                "opening_balance": "100.00",
                "opening_balance_date": "2026-10-23",
            },
        )
        self.assertEqual(inactive_resp.status_code, 200)
        self.assertTrue(inactive_resp.context["form"].errors.get("currency"))
        self.assertEqual(Wallet.objects.count(), 0)

        nonexistent_resp = self.client.post(
            self.wallets_url,
            {
                "name": "Nonexistent Currency Wallet",
                "wallet_type": Wallet.WalletType.CASH,
                "currency": 999999,
                "opening_balance": "100.00",
                "opening_balance_date": "2026-10-23",
            },
        )
        self.assertEqual(nonexistent_resp.status_code, 200)
        self.assertTrue(nonexistent_resp.context["form"].errors.get("currency"))
        self.assertEqual(Wallet.objects.count(), 0)

    def test_opening_balances_for_0_2_and_3_decimal_currencies_and_negative_balances(self):
        jpy = Currency.objects.get(code="JPY")
        usd = Currency.objects.get(code="USD")
        kwd = Currency.objects.get(code="KWD")
        self.client.force_login(self.user)

        cases = [
            ("Yen Cash", jpy, "15000", "15,000"),
            ("USD Card", usd, "-245.75", "-245.75"),
            ("KWD Account", kwd, "89.125", "89.125"),
        ]
        for name, curr, raw_amount, expected_display in cases:
            with self.subTest(currency=curr.code):
                resp = self.client.post(
                    self.wallets_url,
                    {
                        "name": name,
                        "wallet_type": Wallet.WalletType.BANK_ACCOUNT,
                        "currency": curr.pk,
                        "opening_balance": raw_amount,
                        "opening_balance_date": "2026-10-23",
                    },
                )
                self.assertRedirects(resp, self.wallets_url)
                wallet = Wallet.objects.get(name=name)
                self.assertEqual(wallet.formatted_opening_balance, expected_display)

    def test_excess_decimal_precision_rejected_without_silent_rounding(self):
        jpy = Currency.objects.get(code="JPY")
        usd = Currency.objects.get(code="USD")
        kwd = Currency.objects.get(code="KWD")
        self.client.force_login(self.user)

        excess_cases = [
            (jpy, "100.5"),
            (jpy, "100.0"),
            (usd, "25.555"),
            (usd, "25.500"),
            (kwd, "10.1234"),
        ]
        for curr, bad_amount in excess_cases:
            with self.subTest(currency=curr.code, amount=bad_amount):
                resp = self.client.post(
                    self.wallets_url,
                    {
                        "name": f"Precision Test {curr.code}",
                        "wallet_type": Wallet.WalletType.CASH,
                        "currency": curr.pk,
                        "opening_balance": bad_amount,
                        "opening_balance_date": "2026-10-23",
                    },
                )
                self.assertEqual(resp.status_code, 200)
                self.assertTrue(resp.context["form"].errors.get("opening_balance"))
                self.assertEqual(Wallet.objects.count(), 0)

    def test_invalid_non_finite_amounts_missing_fields_and_invalid_dates_rejected(self):
        usd = Currency.objects.get(code="USD")
        self.client.force_login(self.user)

        for bad_amount in ["not-a-number", "NaN", "Infinity", "-Infinity", ""]:
            with self.subTest(bad_amount=bad_amount):
                resp = self.client.post(
                    self.wallets_url,
                    {
                        "name": "Invalid Amount Wallet",
                        "wallet_type": Wallet.WalletType.CASH,
                        "currency": usd.pk,
                        "opening_balance": bad_amount,
                        "opening_balance_date": "2026-10-23",
                    },
                )
                self.assertEqual(resp.status_code, 200)
                self.assertTrue(resp.context["form"].errors.get("opening_balance"))

        bad_date_resp = self.client.post(
            self.wallets_url,
            {
                "name": "Invalid Date Wallet",
                "wallet_type": Wallet.WalletType.CASH,
                "currency": usd.pk,
                "opening_balance": "10.00",
                "opening_balance_date": "not-a-date",
            },
        )
        self.assertEqual(bad_date_resp.status_code, 200)
        self.assertTrue(bad_date_resp.context["form"].errors.get("opening_balance_date"))

        missing_name_resp = self.client.post(
            self.wallets_url,
            {
                "name": "   ",
                "wallet_type": Wallet.WalletType.CASH,
                "currency": usd.pk,
                "opening_balance": "10.00",
                "opening_balance_date": "2026-10-23",
            },
        )
        self.assertEqual(missing_name_resp.status_code, 200)
        self.assertTrue(missing_name_resp.context["form"].errors.get("name"))
        self.assertEqual(Wallet.objects.count(), 0)

        float_wallet = Wallet(
            owner=self.user,
            name="Float Wallet",
            wallet_type=Wallet.WalletType.CASH,
            currency=usd,
            opening_balance=12.34,
            opening_balance_date=date(2026, 10, 23),
        )
        with self.assertRaises(ValidationError):
            float_wallet.full_clean()

    def test_csrf_enforced_on_wallet_creation(self):
        usd = Currency.objects.get(code="USD")
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.user)

        response = csrf_client.post(
            self.wallets_url,
            {
                "name": "CSRF Wallet",
                "wallet_type": Wallet.WalletType.CASH,
                "currency": usd.pk,
                "opening_balance": "50.00",
                "opening_balance_date": "2026-10-23",
            },
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(Wallet.objects.count(), 0)

    def test_get_and_unsupported_http_methods_create_no_records(self):
        self.client.force_login(self.user)

        get_resp = self.client.get(self.wallets_url)
        self.assertEqual(get_resp.status_code, 200)
        self.assertEqual(Wallet.objects.count(), 0)

        for method in ("put", "delete", "patch"):
            with self.subTest(method=method):
                resp = getattr(self.client, method)(self.wallets_url)
                self.assertEqual(resp.status_code, 405)
                self.assertEqual(Wallet.objects.count(), 0)

    def test_referenced_currency_cannot_be_deleted(self):
        usd = Currency.objects.get(code="USD")
        Wallet.objects.create(
            owner=self.user,
            name="Protected Currency Wallet",
            wallet_type=Wallet.WalletType.E_WALLET,
            currency=usd,
            opening_balance=Decimal("20.00"),
            opening_balance_date=date(2026, 10, 23),
        )
        with self.assertRaises(ProtectedError):
            usd.delete()

    def test_reloaded_wallets_with_trailing_db_zeros_validate_and_allow_edits(self):
        jpy = Currency.objects.get(code="JPY")
        usd = Currency.objects.get(code="USD")
        kwd = Currency.objects.get(code="KWD")

        valid_cases = [
            (jpy, Decimal("100")),
            (jpy, Decimal("0")),
            (jpy, Decimal("-500")),
            (usd, Decimal("25.50")),
            (usd, Decimal("0.00")),
            (usd, Decimal("-19.95")),
            (kwd, Decimal("12.345")),
            (kwd, Decimal("0.000")),
            (kwd, Decimal("-4.500")),
        ]
        for curr, balance in valid_cases:
            with self.subTest(currency=curr.code, balance=balance):
                created = Wallet.objects.create(
                    owner=self.user,
                    name=f"Initial {curr.code}",
                    wallet_type=Wallet.WalletType.CASH,
                    currency=curr,
                    opening_balance=balance,
                    opening_balance_date=date(2026, 10, 23),
                )
                reloaded = Wallet.objects.get(pk=created.pk)
                reloaded.full_clean()

                reloaded.name = f"Renamed {curr.code}"
                reloaded.full_clean()
                reloaded.save()

        meaningful_excess_cases = [
            (jpy, Decimal("100.001")),
            (jpy, Decimal("-100.500")),
            (usd, Decimal("25.501")),
            (usd, Decimal("-25.005")),
            (kwd, Decimal("1.0001")),
        ]
        for curr, bad_balance in meaningful_excess_cases:
            with self.subTest(currency=curr.code, bad_balance=bad_balance):
                invalid_wallet = Wallet(
                    owner=self.user,
                    name=f"Invalid {curr.code}",
                    wallet_type=Wallet.WalletType.CASH,
                    currency=curr,
                    opening_balance=bad_balance,
                    opening_balance_date=date(2026, 10, 23),
                )
                with self.assertRaises(ValidationError):
                    invalid_wallet.full_clean()

    def test_all_nine_application_pages_render_for_authenticated_user(self):
        self.client.force_login(self.user)
        for route_name in self.APPLICATION_ROUTES:
            with self.subTest(route=route_name):
                response = self.client.get(reverse(route_name))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(UserPreference.objects.count(), 0)


class SharedSpaceAndMembershipTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user(username="alice_space", password="Str0ng!Password#2026")
        self.bob = User.objects.create_user(username="bob_space", password="Str0ng!Password#2026")
        self.carol = User.objects.create_user(username="carol_space", password="Str0ng!Password#2026")
        self.php = Currency.objects.get(code="PHP")
        self.usd = Currency.objects.get(code="USD")
        self.twd = Currency.objects.get(code="TWD")
        self.shared_url = reverse("budgets:shared_space")

    def test_shared_space_creation_invitation_acceptance_revocation_two_member_limit_and_leaving(self):
        from .models import SharedSpaceInvitation, SharedSpaceMembership
        from .services import (
            accept_shared_space_invitation,
            create_shared_space,
            create_shared_space_invitation,
            leave_shared_space,
            revoke_shared_space_invitation,
            update_shared_space,
        )

        UserPreference.objects.create(user=self.alice, personal_base_currency=self.usd)
        space = create_shared_space(user=self.alice, name="Household Space", base_currency=self.php)
        self.assertEqual(space.base_currency, self.php)
        self.assertEqual(
            UserPreference.objects.get(user=self.alice).personal_base_currency,
            self.usd,
        )

        inv_to_revoke = create_shared_space_invitation(
            user=self.alice, shared_space=space, partner_hint="bob_space"
        )
        revoke_shared_space_invitation(user=self.alice, invitation=inv_to_revoke)
        with self.assertRaises(ValidationError):
            accept_shared_space_invitation(user=self.bob, code=inv_to_revoke.code)

        valid_invite = create_shared_space_invitation(
            user=self.alice, shared_space=space, partner_hint="bob_space"
        )
        membership = accept_shared_space_invitation(user=self.bob, code=valid_invite.code)
        self.assertTrue(membership.is_active)
        self.assertEqual(membership.role, SharedSpaceMembership.Role.MEMBER)

        # Single-use invitation cannot be reused
        with self.assertRaises(ValidationError):
            accept_shared_space_invitation(user=self.carol, code=valid_invite.code)

        # Two-member limit blocks generating another invite or adding a 3rd member
        with self.assertRaises(ValidationError):
            create_shared_space_invitation(user=self.alice, shared_space=space)

        # Non-creator cannot update space settings
        from django.core.exceptions import PermissionDenied

        with self.assertRaises(PermissionDenied):
            update_shared_space(
                user=self.bob,
                shared_space=space,
                name="Bob Hijack",
                base_currency=self.twd,
            )

        # Creator cannot leave while managing space
        with self.assertRaises(ValidationError):
            leave_shared_space(user=self.alice, shared_space=space)

        # Member can leave safely; historical membership record remains with is_active=False
        left_membership = leave_shared_space(user=self.bob, shared_space=space)
        self.assertFalse(left_membership.is_active)
        self.assertIsNotNone(left_membership.left_at)


class TransactionsTransfersBudgetsGoalsAndSyncTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user(username="alice_ledger", password="Str0ng!Password#2026")
        self.bob = User.objects.create_user(username="bob_ledger", password="Str0ng!Password#2026")
        self.usd = Currency.objects.get(code="USD")
        self.eur = Currency.objects.get(code="EUR")
        self.php = Currency.objects.get(code="PHP")
        self.jpy = Currency.objects.get(code="JPY")
        self.kwd = Currency.objects.get(code="KWD")
        self.twd = Currency.objects.get(code="TWD")

    def test_wallet_activity_locks_currency_and_opening_balance_and_derives_balances(self):
        from .services import (
            create_category,
            create_transaction_entry,
            delete_transaction_entry,
            update_wallet_record,
        )

        wallet = Wallet.objects.create(
            owner=self.alice,
            name="Alice USD Checking",
            wallet_type=Wallet.WalletType.BANK_ACCOUNT,
            currency=self.usd,
            opening_balance=Decimal("1000.00"),
            opening_balance_date=date(2026, 10, 1),
        )
        cat = create_category(user=self.alice, name="Dining", kind="expense")

        # Transaction earlier than wallet opening_balance_date is rejected
        with self.assertRaises(ValidationError):
            create_transaction_entry(
                user=self.alice,
                transaction_type="expense",
                amount=Decimal("20.00"),
                wallet=wallet,
                transaction_date=date(2026, 9, 30),
                category=cat,
            )

        tx_income = create_transaction_entry(
            user=self.alice,
            transaction_type="income",
            amount=Decimal("500.25"),
            wallet=wallet,
            transaction_date=date(2026, 10, 5),
            description="Freelance payout",
        )
        tx_expense = create_transaction_entry(
            user=self.alice,
            transaction_type="expense",
            amount=Decimal("125.50"),
            wallet=wallet,
            transaction_date=date(2026, 10, 6),
            category=cat,
            description="Team dinner",
        )

        self.assertEqual(wallet.current_balance, Decimal("1374.750"))
        self.assertEqual(wallet.formatted_current_balance, "1,374.75")

        # Once ledger activity exists, changing currency or opening_balance is blocked
        with self.assertRaises(ValidationError):
            update_wallet_record(
                user=self.alice,
                wallet=wallet,
                name="Alice USD Checking",
                wallet_type=wallet.wallet_type,
                currency=self.php,
            )
        with self.assertRaises(ValidationError):
            update_wallet_record(
                user=self.alice,
                wallet=wallet,
                name="Alice USD Checking",
                wallet_type=wallet.wallet_type,
                opening_balance=Decimal("2000.00"),
            )

        # Deleting a transaction creates a tombstone and updates derived balance
        delete_transaction_entry(user=self.alice, tx=tx_expense)
        tx_expense.refresh_from_db()
        self.assertTrue(tx_expense.is_deleted)
        self.assertEqual(wallet.current_balance, Decimal("1500.250"))

    def test_transfers_same_and_cross_currency_with_fees_and_atomic_deletion(self):
        from .selectors import compute_income_expense_summary, get_accessible_transactions_qs
        from .services import create_transfer_entry, delete_transfer_entry

        w_usd = Wallet.objects.create(
            owner=self.alice,
            name="USD Wallet",
            wallet_type=Wallet.WalletType.BANK_ACCOUNT,
            currency=self.usd,
            opening_balance=Decimal("500.00"),
            opening_balance_date=date(2026, 10, 1),
        )
        w_jpy = Wallet.objects.create(
            owner=self.alice,
            name="JPY Travel Cash",
            wallet_type=Wallet.WalletType.CASH,
            currency=self.jpy,
            opening_balance=Decimal("10000"),
            opening_balance_date=date(2026, 10, 1),
        )

        # Cross-currency transfer without received amount or manual rate is rejected
        with self.assertRaises(ValidationError):
            create_transfer_entry(
                user=self.alice,
                source_wallet=w_usd,
                destination_wallet=w_jpy,
                amount_sent=Decimal("100.00"),
                transfer_date=date(2026, 10, 10),
            )

        transfer = create_transfer_entry(
            user=self.alice,
            source_wallet=w_usd,
            destination_wallet=w_jpy,
            amount_sent=Decimal("100.00"),
            amount_received=Decimal("15000"),
            fee_amount=Decimal("5.00"),
            transfer_date=date(2026, 10, 10),
            notes="ATM exchange",
        )
        self.assertEqual(w_usd.current_balance, Decimal("395.000"))
        self.assertEqual(w_jpy.current_balance, Decimal("25000.000"))

        # Transfers do not count as income or expense; only the optional fee is an expense
        all_txs = list(get_accessible_transactions_qs(self.alice))
        summary = compute_income_expense_summary(all_txs, self.usd)
        self.assertEqual(summary["formatted_income"], "0.00")
        self.assertEqual(summary["formatted_expense"], "5.00")

        # Deleting the transfer soft-deletes both legs and the fee transaction
        delete_transfer_entry(user=self.alice, transfer_obj=transfer)
        self.assertEqual(w_usd.current_balance, Decimal("500.000"))
        self.assertEqual(w_jpy.current_balance, Decimal("10000.000"))

    def test_budgets_and_savings_goals_and_historical_rate_snapshots(self):
        from django.utils import timezone
        from .exchange_rates import record_manual_exchange_rate
        from .selectors import get_budget_overview_for_period, get_savings_goals_overview
        from .services import (
            create_category,
            create_savings_goal,
            create_transaction_entry,
            record_savings_contribution,
            save_category_budget,
        )

        UserPreference.objects.create(user=self.alice, personal_base_currency=self.usd)
        record_manual_exchange_rate(
            from_currency=self.twd,
            to_currency=self.usd,
            rate=Decimal("0.03125000"),
            user=self.alice,
            rate_timestamp=timezone.now(),
        )

        w_twd = Wallet.objects.create(
            owner=self.alice,
            name="TWD Card",
            wallet_type=Wallet.WalletType.BANK_ACCOUNT,
            currency=self.twd,
            opening_balance=Decimal("10000.00"),
            opening_balance_date=date(2026, 10, 1),
        )
        cat_food = create_category(user=self.alice, name="Groceries", kind="expense")
        today = timezone.localdate()
        period_str = today.strftime("%Y-%m")

        save_category_budget(
            user=self.alice,
            category=cat_food,
            period=period_str,
            currency=self.usd,
            limit_amount=Decimal("200.00"),
        )

        tx = create_transaction_entry(
            user=self.alice,
            transaction_type="expense",
            amount=Decimal("3200.00"),
            wallet=w_twd,
            transaction_date=today,
            category=cat_food,
            description="Supermarket",
        )
        # 3200 TWD * 0.03125 = 100.00 USD booked at entry time
        self.assertEqual(tx.formatted_converted_amount, "100.00")

        # Recording a newer exchange rate must NOT overwrite the historical transaction snapshot
        record_manual_exchange_rate(
            from_currency=self.twd,
            to_currency=self.usd,
            rate=Decimal("0.05000000"),
            user=self.alice,
            rate_timestamp=timezone.now() + timezone.timedelta(minutes=5),
        )
        tx.refresh_from_db()
        self.assertEqual(tx.formatted_converted_amount, "100.00")

        overview = get_budget_overview_for_period(self.alice, period=period_str)
        self.assertEqual(len(overview["rows"]), 1)
        self.assertEqual(overview["rows"][0]["formatted_spent"], "100.00")
        self.assertEqual(overview["rows"][0]["formatted_remaining"], "100.00")

        # Savings goals earmark funds without altering wallet balances
        goal = create_savings_goal(
            user=self.alice,
            name="Emergency Reserve",
            currency=self.twd,
            target_amount=Decimal("50000.00"),
        )
        record_savings_contribution(
            user=self.alice,
            goal=goal,
            amount=Decimal("5000.00"),
            contribution_date=today,
            entry_type="contribution",
        )
        self.assertEqual(goal.formatted_saved_amount, "5,000.00")
        self.assertEqual(goal.formatted_remaining_amount, "45,000.00")
        # Wallet balance is untouched by savings contribution
        self.assertEqual(w_twd.current_balance, Decimal("6800.000"))

        # Reversal exceeding saved amount is rejected
        with self.assertRaises(ValidationError):
            record_savings_contribution(
                user=self.alice,
                goal=goal,
                amount=Decimal("6000.00"),
                contribution_date=today,
                entry_type="reversal",
            )

        goals_data = get_savings_goals_overview(self.alice)
        self.assertEqual(goals_data["total_goals"], 1)

    def test_offline_pwa_endpoints_and_idempotent_sync_with_conflict_detection(self):
        import json
        import uuid

        wallet = Wallet.objects.create(
            owner=self.alice,
            name="Offline Cash",
            wallet_type=Wallet.WalletType.CASH,
            currency=self.kwd,
            opening_balance=Decimal("50.000"),
            opening_balance_date=date(2026, 10, 1),
        )

        sw_resp = self.client.get("/service-worker.js")
        self.assertEqual(sw_resp.status_code, 200)
        self.assertEqual(sw_resp["Service-Worker-Allowed"], "/")

        offline_resp = self.client.get(reverse("budgets:offline"))
        self.assertEqual(offline_resp.status_code, 200)

        self.client.force_login(self.alice)
        state_resp = self.client.get(reverse("budgets:sync_state_api"))
        self.assertEqual(state_resp.status_code, 200)
        self.assertEqual(state_resp.json()["username"], "alice_ledger")

        tx_uuid = str(uuid.uuid4())
        mutation_id = "mut-create-1"
        payload = {
            "mutations": [
                {
                    "mutation_id": mutation_id,
                    "operation": "create_transaction",
                    "entity_id": tx_uuid,
                    "payload": {
                        "transaction_type": "expense",
                        "amount": "4.250",
                        "wallet_id": str(wallet.id),
                        "transaction_date": "2026-10-15",
                        "description": "Offline Coffee",
                    },
                }
            ]
        }
        sync_url = reverse("budgets:sync_mutations_api")
        r1 = self.client.post(
            sync_url,
            data=json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(r1.status_code, 200)
        res1 = r1.json()["results"][0]
        self.assertEqual(res1["status"], "applied")
        self.assertEqual(wallet.current_balance, Decimal("45.750"))

        # Replaying the same mutation_id is idempotent and does not duplicate the transaction
        r2 = self.client.post(
            sync_url,
            data=json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(r2.status_code, 200)
        self.assertTrue(r2.json()["results"][0].get("idempotent_replay"))
        self.assertEqual(wallet.current_balance, Decimal("45.750"))

        # Stale base_revision triggers explicit conflict status
        conflict_payload = {
            "mutations": [
                {
                    "mutation_id": "mut-update-stale",
                    "operation": "update_transaction",
                    "entity_id": tx_uuid,
                    "base_revision": 999,
                    "payload": {
                        "amount": "9.000",
                    },
                }
            ]
        }
        r3 = self.client.post(
            sync_url,
            data=json.dumps(conflict_payload),
            content_type="application/json",
        )
        self.assertEqual(r3.status_code, 200)
        self.assertEqual(r3.json()["results"][0]["status"], "conflict")

    def test_issue1_manual_exchange_rate_isolation_personal_shared_provider_and_legacy(self):
        from django.core.exceptions import PermissionDenied
        from django.utils import timezone
        from .exchange_rates import (
            get_accessible_exchange_rates_qs,
            get_latest_rate,
            record_manual_exchange_rate,
        )
        from .models import ExchangeRate
        from .selectors import (
            get_budget_overview_for_period,
            get_dashboard_context,
            get_reports_context,
        )
        from .services import (
            accept_shared_space_invitation,
            create_category,
            create_shared_space,
            create_shared_space_invitation,
            create_transaction_entry,
            leave_shared_space,
            save_category_budget,
        )

        UserPreference.objects.create(user=self.alice, personal_base_currency=self.usd)
        UserPreference.objects.create(user=self.bob, personal_base_currency=self.usd)

        # Alice records a private personal manual rate: 1 EUR = 1.20 USD
        alice_rate = record_manual_exchange_rate(
            from_currency=self.eur,
            to_currency=self.usd,
            rate=Decimal("1.20000000"),
            user=self.alice,
            scope="personal",
        )
        self.assertEqual(alice_rate.scope, ExchangeRate.Scope.PERSONAL)

        # Alice's personal rate lookup sees 1.20, but Bob's personal lookup does NOT
        alice_lookup = get_latest_rate(
            self.eur, self.usd, user=self.alice, scope="personal"
        )
        self.assertEqual(alice_lookup.rate, Decimal("1.20000000"))

        bob_lookup = get_latest_rate(
            self.eur, self.usd, user=self.bob, scope="personal"
        )
        self.assertIsNone(bob_lookup.rate)

        # Bob creates an EUR wallet and books an EUR expense: must remain PENDING (never uses Alice's rate)
        bob_eur_wallet = Wallet.objects.create(
            owner=self.bob,
            name="Bob EUR Wallet",
            wallet_type=Wallet.WalletType.CASH,
            currency=self.eur,
            opening_balance=Decimal("100.00"),
            opening_balance_date=date(2026, 10, 1),
        )
        today = timezone.localdate()
        bob_tx = create_transaction_entry(
            user=self.bob,
            transaction_type="expense",
            amount=Decimal("50.00"),
            wallet=bob_eur_wallet,
            transaction_date=today,
            description="Bob Cafe",
        )
        self.assertEqual(bob_tx.conversion_status, "pending")
        self.assertIsNone(bob_tx.converted_amount)

        # Bob's dashboard, budgets, reports, and currency converter must NOT use Alice's personal manual rate
        bob_cat = create_category(user=self.bob, name="Bob Dining", kind="expense")
        bob_tx.category = bob_cat
        bob_tx.save(update_fields=["category", "updated_at"])
        save_category_budget(
            user=self.bob,
            category=bob_cat,
            period=today.strftime("%Y-%m"),
            currency=self.usd,
            limit_amount=Decimal("200.00"),
        )
        bob_dash = get_dashboard_context(self.bob)
        self.assertFalse(bob_dash["wallet_total_complete"])
        self.assertFalse(bob_dash["month_summary"]["is_complete"])

        bob_budget = get_budget_overview_for_period(self.bob, period=today.strftime("%Y-%m"))
        self.assertTrue(bob_budget["rows"][0]["incomplete_conversion"])

        bob_reports = get_reports_context(self.bob, {"currency": "USD"})
        self.assertFalse(bob_reports["summary"]["is_complete"])

        # Shared space manual rate isolation
        space1 = create_shared_space(
            user=self.alice, name="Space One", base_currency=self.usd
        )
        inv = create_shared_space_invitation(user=self.alice, shared_space=space1)
        accept_shared_space_invitation(user=self.bob, code=inv.code)

        space2 = create_shared_space(
            user=self.alice, name="Space Two", base_currency=self.usd
        )

        shared_rate = record_manual_exchange_rate(
            from_currency=self.jpy,
            to_currency=self.usd,
            rate=Decimal("0.00680000"),
            user=self.alice,
            scope="shared",
            shared_space=space1,
        )
        self.assertEqual(shared_rate.scope, ExchangeRate.Scope.SHARED)

        # Active member Bob can use space1's shared rate inside space1
        bob_space1_lookup = get_latest_rate(
            self.jpy, self.usd, user=self.bob, scope="shared", shared_space=space1
        )
        self.assertEqual(bob_space1_lookup.rate, Decimal("0.00680000"))

        # Space1's shared rate does NOT apply to Bob's personal conversions or to Space2
        self.assertIsNone(
            get_latest_rate(self.jpy, self.usd, user=self.bob, scope="personal").rate
        )
        self.assertIsNone(
            get_latest_rate(
                self.jpy, self.usd, user=self.alice, scope="shared", shared_space=space2
            ).rate
        )

        # Non-member Bob cannot record or look up rates in space2
        with self.assertRaises(PermissionDenied):
            record_manual_exchange_rate(
                from_currency=self.jpy,
                to_currency=self.usd,
                rate=Decimal("0.00700000"),
                user=self.bob,
                scope="shared",
                shared_space=space2,
            )

        # Once Bob leaves space1, he can no longer use space1's shared manual rates
        leave_shared_space(user=self.bob, shared_space=space1)
        self.assertIsNone(
            get_latest_rate(
                self.jpy, self.usd, user=self.bob, scope="shared", shared_space=space1
            ).rate
        )

        # Legacy unscoped manual record is never globally eligible
        legacy_rate = ExchangeRate.objects.create(
            from_currency=self.kwd,
            to_currency=self.usd,
            rate=Decimal("3.25000000"),
            provider="manual",
            is_manual=True,
            scope=ExchangeRate.Scope.LEGACY_UNSCOPED,
            created_by=None,
            shared_space=None,
            rate_timestamp=timezone.now(),
        )
        self.assertIsNone(
            get_latest_rate(self.kwd, self.usd, user=self.alice, scope="personal").rate
        )
        self.assertNotIn(legacy_rate, list(get_accessible_exchange_rates_qs(self.alice)))

        # Global provider rate IS available to both Alice and Bob
        provider_rate = ExchangeRate.objects.create(
            from_currency=self.php,
            to_currency=self.usd,
            rate=Decimal("0.01780000"),
            provider="frankfurter",
            is_manual=False,
            scope=ExchangeRate.Scope.PROVIDER,
            created_by=None,
            shared_space=None,
            rate_timestamp=timezone.now(),
        )
        self.assertEqual(
            get_latest_rate(self.php, self.usd, user=self.alice, scope="personal").rate,
            provider_rate.rate,
        )
        self.assertEqual(
            get_latest_rate(self.php, self.usd, user=self.bob, scope="personal").rate,
            provider_rate.rate,
        )

    def test_issue2_and_3_offline_shell_neutrality_cache_invalidation_and_user_isolation(self):
        import json
        import uuid
        from .services import create_shared_space

        UserPreference.objects.create(user=self.alice, personal_base_currency=self.usd)
        create_shared_space(user=self.alice, name="SecretPrivateHousehold", base_currency=self.usd)
        alice_wallet = Wallet.objects.create(
            owner=self.alice,
            name="AlicePrivateVault",
            wallet_type=Wallet.WalletType.CASH,
            currency=self.usd,
            opening_balance=Decimal("9876.54"),
            opening_balance_date=date(2026, 10, 1),
        )

        # Even when requested by an authenticated session, /offline/ must be 100% account-neutral
        self.client.force_login(self.alice)
        offline_resp = self.client.get(reverse("budgets:offline"))
        self.assertEqual(offline_resp.status_code, 200)
        html = offline_resp.content.decode("utf-8")
        self.assertNotIn("alice_ledger", html)
        self.assertNotIn("SecretPrivateHousehold", html)
        self.assertNotIn("AlicePrivateVault", html)
        self.assertNotIn("9,876.54", html)
        self.assertNotIn("csrfmiddlewaretoken", html)
        self.assertIn('id="offline-entry-form"', html)
        self.assertIn('id="offline-queued-list"', html)
        self.assertIn('id="offline-bounded-notice"', html)

        # Service worker uses hash-derived cache name and deletes only budget-tracker-shell-* caches
        from .views import compute_service_worker_cache_name
        sw_resp = self.client.get("/service-worker.js")
        sw_js = sw_resp.content.decode("utf-8")
        expected_cache_name = compute_service_worker_cache_name()
        self.assertIn(f'const CACHE_NAME = "{expected_cache_name}"', sw_js)
        self.assertIn('const CACHE_PREFIX = "budget-tracker-shell-"', sw_js)
        self.assertIn("k.indexOf(CACHE_PREFIX) === 0 && k !== CACHE_NAME", sw_js)

        # Cross-account upload protection: Alice's queued batch rejected if session is Bob
        self.client.force_login(self.bob)
        mismatch_resp = self.client.post(
            reverse("budgets:sync_mutations_api"),
            data=json.dumps(
                {
                    "expected_user_id": self.alice.pk,
                    "mutations": [
                        {
                            "mutation_id": "cross-user-mut-1",
                            "operation": "create_transaction",
                            "entity_id": str(uuid.uuid4()),
                            "payload": {
                                "transaction_type": "expense",
                                "amount": "10.00",
                                "wallet_id": str(alice_wallet.id),
                                "transaction_date": "2026-10-15",
                            },
                        }
                    ],
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(mismatch_resp.status_code, 409)
        self.assertEqual(mismatch_resp.json()["batch_status"], "user_mismatch")
        self.assertEqual(mismatch_resp.json()["applied_count"], 0)

    def test_issue3_4_and_5_sync_pagination_honest_outcomes_and_strict_revisions(self):
        import json
        import uuid
        from .models import Transaction
        from .services import (
            accept_shared_space_invitation,
            create_shared_space,
            create_shared_space_invitation,
            create_transaction_entry,
        )

        UserPreference.objects.create(user=self.alice, personal_base_currency=self.usd)
        space = create_shared_space(user=self.alice, name="Sync Shared", base_currency=self.usd)
        inv = create_shared_space_invitation(user=self.alice, shared_space=space)
        accept_shared_space_invitation(user=self.bob, code=inv.code)

        shared_eur_wallet = Wallet.objects.create(
            owner=self.alice,
            ownership_scope=Wallet.OwnershipScope.SHARED,
            shared_space=space,
            name="Shared EUR Wallet",
            wallet_type=Wallet.WalletType.BANK_ACCOUNT,
            currency=self.eur,
            opening_balance=Decimal("500.00"),
            opening_balance_date=date(2026, 10, 1),
        )
        for idx in range(5):
            create_transaction_entry(
                user=self.alice,
                transaction_type="expense",
                amount=Decimal("10.00"),
                wallet=shared_eur_wallet,
                transaction_date=date(2026, 10, 10),
                scope="shared",
                shared_space=space,
                description=f"Item {idx}",
            )

        self.client.force_login(self.alice)
        # Bounded snapshot pagination test
        page_resp = self.client.get(reverse("budgets:sync_state_api") + "?limit=2&offset=0")
        self.assertEqual(page_resp.status_code, 200)
        page_data = page_resp.json()
        self.assertEqual(page_data["user_id"], self.alice.pk)
        self.assertEqual(page_data["total_transactions_count"], 5)
        self.assertEqual(page_data["returned_transactions_count"], 2)
        self.assertTrue(page_data["has_more_transactions"])
        self.assertEqual(page_data["next_offset"], 2)
        self.assertTrue(page_data["is_bounded_snapshot"])

        # Offline creation preserving shared scope, shared_space_id, paid_by_id, and manual_rate
        sync_url = reverse("budgets:sync_mutations_api")
        shared_tx_id = str(uuid.uuid4())
        create_resp = self.client.post(
            sync_url,
            data=json.dumps(
                {
                    "expected_user_id": self.alice.pk,
                    "mutations": [
                        {
                            "mutation_id": "mut-shared-create-1",
                            "operation": "create_transaction",
                            "entity_id": shared_tx_id,
                            "payload": {
                                "transaction_type": "expense",
                                "amount": "25.00",
                                "wallet_id": str(shared_eur_wallet.id),
                                "transaction_date": "2026-10-12",
                                "scope": "shared",
                                "shared_space_id": str(space.id),
                                "paid_by_id": self.bob.pk,
                                "manual_rate": "1.10000000",
                                "description": "Shared Dinner",
                            },
                        }
                    ],
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(create_resp.status_code, 200)
        create_json = create_resp.json()
        self.assertEqual(create_json["batch_status"], "success")
        self.assertEqual(create_json["applied_count"], 1)
        created_tx = Transaction.objects.get(pk=shared_tx_id)
        self.assertEqual(created_tx.scope, "shared")
        self.assertEqual(created_tx.shared_space_id, space.id)
        self.assertEqual(created_tx.paid_by_id, self.bob.pk)
        self.assertEqual(created_tx.formatted_converted_amount, "27.50")

        # Reusing mutation_id with a DIFFERENT payload or operation must be rejected
        reuse_diff_resp = self.client.post(
            sync_url,
            data=json.dumps(
                {
                    "mutations": [
                        {
                            "mutation_id": "mut-shared-create-1",
                            "operation": "create_transaction",
                            "entity_id": shared_tx_id,
                            "payload": {
                                "transaction_type": "expense",
                                "amount": "99.00",
                                "wallet_id": str(shared_eur_wallet.id),
                                "transaction_date": "2026-10-12",
                            },
                        }
                    ]
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(reuse_diff_resp.status_code, 200)
        self.assertEqual(reuse_diff_resp.json()["batch_status"], "failed")
        self.assertEqual(reuse_diff_resp.json()["results"][0]["status"], "error")
        created_tx.refresh_from_db()
        self.assertEqual(created_tx.amount, Decimal("25.000"))

        # Strict base_revision enforcement on update_transaction and delete_transaction:
        # missing, None, True, False, 0, negative, float, non-integer string must all be rejected
        for idx, invalid_rev in enumerate([None, True, False, 0, -1, "abc", "1.5", []]):
            bad_rev_resp = self.client.post(
                sync_url,
                data=json.dumps(
                    {
                        "mutations": [
                            {
                                "mutation_id": f"mut-bad-rev-{idx}",
                                "operation": "update_transaction",
                                "entity_id": shared_tx_id,
                                "base_revision": invalid_rev,
                                "payload": {"amount": "30.00"},
                            }
                        ]
                    }
                ),
                content_type="application/json",
            )
            self.assertEqual(bad_rev_resp.status_code, 200)
            self.assertEqual(bad_rev_resp.json()["results"][0]["status"], "error")

        bad_del_resp = self.client.post(
            sync_url,
            data=json.dumps(
                {
                    "mutations": [
                        {
                            "mutation_id": "mut-del-missing-rev",
                            "operation": "delete_transaction",
                            "entity_id": shared_tx_id,
                            "payload": {},
                        }
                    ]
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(bad_del_resp.json()["results"][0]["status"], "error")
        created_tx.refresh_from_db()
        self.assertFalse(created_tx.is_deleted)
        self.assertEqual(created_tx.amount, Decimal("25.000"))

        # Partial batch outcome test (1 applied + 1 conflict -> batch_status == "partial")
        partial_resp = self.client.post(
            sync_url,
            data=json.dumps(
                {
                    "mutations": [
                        {
                            "mutation_id": "mut-valid-update-rev1",
                            "operation": "update_transaction",
                            "entity_id": shared_tx_id,
                            "base_revision": 1,
                            "payload": {"amount": "30.00"},
                        },
                        {
                            "mutation_id": "mut-stale-delete-rev1",
                            "operation": "delete_transaction",
                            "entity_id": shared_tx_id,
                            "base_revision": 1,
                            "payload": {},
                        },
                    ]
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(partial_resp.status_code, 200)
        p_json = partial_resp.json()
        self.assertEqual(p_json["batch_status"], "partial")
        self.assertEqual(p_json["applied_count"], 1)
        self.assertEqual(p_json["conflict_count"], 1)

        # Malformed top-level JSON or oversized batch returns HTTP 400 (never 500)
        non_obj_resp = self.client.post(
            sync_url, data=json.dumps([1, 2, 3]), content_type="application/json"
        )
        self.assertEqual(non_obj_resp.status_code, 400)

        oversized_resp = self.client.post(
            sync_url,
            data=json.dumps({"mutations": [{} for _ in range(105)]}),
            content_type="application/json",
        )
        self.assertEqual(oversized_resp.status_code, 400)

    def test_issue6_transfer_editing_and_savings_goal_editing(self):
        from .selectors import compute_income_expense_summary, get_accessible_transactions_qs
        from .services import (
            create_savings_goal,
            create_transfer_entry,
            record_savings_contribution,
            update_savings_goal,
            update_transfer_entry,
        )

        w1 = Wallet.objects.create(
            owner=self.alice,
            name="Checking USD",
            wallet_type=Wallet.WalletType.BANK_ACCOUNT,
            currency=self.usd,
            opening_balance=Decimal("1000.00"),
            opening_balance_date=date(2026, 10, 1),
        )
        w2 = Wallet.objects.create(
            owner=self.alice,
            name="Savings JPY",
            wallet_type=Wallet.WalletType.BANK_ACCOUNT,
            currency=self.jpy,
            opening_balance=Decimal("10000"),
            opening_balance_date=date(2026, 10, 1),
        )

        tr = create_transfer_entry(
            user=self.alice,
            source_wallet=w1,
            destination_wallet=w2,
            amount_sent=Decimal("100.00"),
            amount_received=Decimal("15000"),
            fee_amount=Decimal("2.00"),
            transfer_date=date(2026, 10, 5),
            notes="Initial transfer",
        )
        self.assertEqual(w1.current_balance, Decimal("898.000"))
        self.assertEqual(w2.current_balance, Decimal("25000.000"))

        # Edit transfer: change sent to 200.00, received to 30000, remove fee (0), update notes
        updated_tr = update_transfer_entry(
            user=self.alice,
            transfer_obj=tr,
            source_wallet=w1,
            destination_wallet=w2,
            amount_sent=Decimal("200.00"),
            amount_received=Decimal("30000"),
            fee_amount=Decimal("0.00"),
            transfer_date=date(2026, 10, 6),
            notes="Updated transfer without fee",
            expected_revision=1,
        )
        self.assertEqual(updated_tr.revision, 2)
        self.assertEqual(updated_tr.fee_amount, Decimal("0.000"))
        self.assertTrue(updated_tr.fee_transaction.is_deleted)
        self.assertEqual(w1.current_balance, Decimal("800.000"))
        self.assertEqual(w2.current_balance, Decimal("40000.000"))

        # Stale revision on update_transfer_entry raises ValidationError
        with self.assertRaises(ValidationError):
            update_transfer_entry(
                user=self.alice,
                transfer_obj=updated_tr,
                source_wallet=w1,
                destination_wallet=w2,
                amount_sent=Decimal("50.00"),
                amount_received=Decimal("7500"),
                transfer_date=date(2026, 10, 6),
                expected_revision=1,
            )

        # Also test update_transfer via the view POST action
        self.client.force_login(self.alice)
        post_tr_resp = self.client.post(
            reverse("budgets:transactions"),
            {
                "action": "update_transfer",
                "transfer_id": str(updated_tr.id),
                "expected_revision": str(updated_tr.revision),
                "source_wallet": str(w1.id),
                "destination_wallet": str(w2.id),
                "amount_sent": "150.00",
                "amount_received": "22500",
                "fee_amount": "3.50",
                "transfer_date": "2026-10-07",
                "notes": "Edited via view",
            },
        )
        self.assertEqual(post_tr_resp.status_code, 302)
        self.assertEqual(w1.current_balance, Decimal("846.500"))
        self.assertEqual(w2.current_balance, Decimal("32500.000"))

        # Transfer legs still excluded from income/expense summary; only the new fee (3.50) counts
        summary = compute_income_expense_summary(
            list(get_accessible_transactions_qs(self.alice)), self.usd, user=self.alice
        )
        self.assertEqual(summary["formatted_income"], "0.00")
        self.assertEqual(summary["formatted_expense"], "3.50")

        # Savings goal editing test
        goal = create_savings_goal(
            user=self.alice,
            name="Vacation Fund",
            currency=self.usd,
            target_amount=Decimal("1000.00"),
            target_date=date(2026, 12, 31),
        )
        # Before contributions exist, currency and target amount can be updated
        goal = update_savings_goal(
            user=self.alice,
            goal=goal,
            name="Japan Trip Fund",
            currency=self.jpy,
            target_amount=Decimal("150000"),
            target_date=date(2027, 3, 31),
            expected_revision=1,
        )
        self.assertEqual(goal.name, "Japan Trip Fund")
        self.assertEqual(goal.currency, self.jpy)
        self.assertEqual(goal.revision, 2)

        # Record a contribution, then verify currency change is blocked while target/name edit works
        record_savings_contribution(
            user=self.alice,
            goal=goal,
            amount=Decimal("50000"),
            contribution_date=date(2026, 10, 10),
            entry_type="contribution",
        )
        goal.refresh_from_db()
        with self.assertRaises(ValidationError):
            update_savings_goal(
                user=self.alice,
                goal=goal,
                name="Japan Trip Fund",
                currency=self.usd,
                target_amount=Decimal("2000.00"),
                expected_revision=goal.revision,
            )

        # Update via view POST action (same currency, higher target)
        post_goal_resp = self.client.post(
            reverse("budgets:savings_goals"),
            {
                "action": "update_goal",
                "goal_id": str(goal.id),
                "expected_revision": str(goal.revision),
                "name": "Deluxe Japan Trip",
                "currency": str(self.jpy.id),
                "target_amount": "200000",
                "target_date": "2027-04-15",
            },
        )
        self.assertEqual(post_goal_resp.status_code, 302)
        goal.refresh_from_db()
        self.assertEqual(goal.name, "Deluxe Japan Trip")
        self.assertEqual(goal.formatted_target_amount, "200,000")
        self.assertEqual(goal.formatted_saved_amount, "50,000")
        # Wallet balances remain completely unchanged by savings goal edits
        self.assertEqual(w1.current_balance, Decimal("846.500"))
        self.assertEqual(w2.current_balance, Decimal("32500.000"))

    def test_issue7_production_secret_key_validation(self):
        from django.core.exceptions import ImproperlyConfigured
        from config.settings import DEVELOPMENT_FALLBACK_SECRET_KEY, resolve_secret_key

        # DEBUG=True allows fallback key for local development
        self.assertEqual(
            resolve_secret_key(debug=True, raw_secret_key=None),
            DEVELOPMENT_FALLBACK_SECRET_KEY,
        )
        self.assertEqual(
            resolve_secret_key(debug=True, raw_secret_key=""),
            DEVELOPMENT_FALLBACK_SECRET_KEY,
        )

        # DEBUG=False rejects missing, blank, or development fallback keys
        for bad_key in [None, "", "   ", DEVELOPMENT_FALLBACK_SECRET_KEY, "django-insecure-xyz"]:
            with self.assertRaises(ImproperlyConfigured):
                resolve_secret_key(debug=False, raw_secret_key=bad_key)

        # DEBUG=False accepts a strong explicit production secret key
        prod_key = "prod-secret-998877665544332211-very-strong-random-key"
        self.assertEqual(
            resolve_secret_key(debug=False, raw_secret_key=prod_key),
            prod_key,
        )

    def test_offline_sync_queues_100_101_250_and_dependency_blocking(self):
        import json
        import uuid
        from .models import SyncMutationReceipt, Transaction
        from .services import MAX_SYNC_BATCH_SIZE, process_sync_batch

        self.assertEqual(MAX_SYNC_BATCH_SIZE, 100)
        wallet = Wallet.objects.create(
            owner=self.alice,
            name="Alice Sync Wallet",
            wallet_type=Wallet.WalletType.CASH,
            currency=self.usd,
            opening_balance=Decimal("0.00"),
            opening_balance_date=date(2026, 10, 1),
        )

        def make_create_mut(seq_num, amount_str="1.00", entity_id=None, mutation_id=None):
            return {
                "mutation_id": mutation_id or str(uuid.uuid4()),
                "queue_seq": seq_num,
                "operation": "create_transaction",
                "entity_id": entity_id or str(uuid.uuid4()),
                "base_revision": None,
                "payload": {
                    "transaction_type": "income",
                    "amount": amount_str,
                    "wallet_id": str(wallet.id),
                    "transaction_date": "2026-10-20",
                    "scope": "personal",
                    "description": f"Sync item #{seq_num}",
                },
            }

        # 1. Server still enforces MAX_SYNC_BATCH_SIZE = 100
        oversized = [make_create_mut(i) for i in range(1, 102)]
        with self.assertRaises(ValidationError):
            process_sync_batch(
                user=self.alice,
                mutations=oversized,
                expected_user_id=self.alice.pk,
            )

        # 2. Queue of 100 items in 1 batch
        batch_100 = [make_create_mut(i, "1.00") for i in range(1, 101)]
        res_100 = process_sync_batch(
            user=self.alice,
            mutations=batch_100,
            expected_user_id=self.alice.pk,
            is_final_batch=True,
        )
        self.assertEqual(res_100["batch_status"], "success")
        self.assertEqual(res_100["applied_count"], 100)
        self.assertIsNotNone(res_100["last_sync_at"])
        wallet.refresh_from_db()
        self.assertEqual(wallet.current_balance, Decimal("100.000"))

        # Lost response retry idempotency on the same 100 mutations
        replay_100 = process_sync_batch(
            user=self.alice,
            mutations=batch_100,
            expected_user_id=self.alice.pk,
            is_final_batch=True,
        )
        self.assertEqual(replay_100["batch_status"], "success")
        self.assertEqual(replay_100["applied_count"], 100)
        self.assertTrue(all(r.get("idempotent_replay") for r in replay_100["results"]))
        wallet.refresh_from_db()
        self.assertEqual(wallet.current_balance, Decimal("100.000"))

        # 3. Queue of 101 items sent sequentially (100 + 1) via sync_mutations_api
        self.client.force_login(self.alice)
        queue_101 = [make_create_mut(i, "2.00") for i in range(101, 202)]
        resp_b1 = self.client.post(
            reverse("budgets:sync_mutations_api"),
            data=json.dumps(
                {
                    "expected_user_id": self.alice.pk,
                    "is_final_batch": False,
                    "mutations": queue_101[:100],
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(resp_b1.status_code, 200)
        self.assertEqual(resp_b1.json()["applied_count"], 100)

        resp_b2 = self.client.post(
            reverse("budgets:sync_mutations_api"),
            data=json.dumps(
                {
                    "expected_user_id": self.alice.pk,
                    "is_final_batch": True,
                    "mutations": queue_101[100:],
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(resp_b2.status_code, 200)
        self.assertEqual(resp_b2.json()["applied_count"], 1)
        wallet.refresh_from_db()
        self.assertEqual(wallet.current_balance, Decimal("302.000"))

        # 4. Queue of 250 items sent sequentially (100 + 100 + 50)
        queue_250 = [make_create_mut(i, "1.00") for i in range(202, 452)]
        for idx in range(0, 250, 100):
            chunk = queue_250[idx : idx + 100]
            is_last = (idx + 100) >= 250
            resp = self.client.post(
                reverse("budgets:sync_mutations_api"),
                data=json.dumps(
                    {
                        "expected_user_id": self.alice.pk,
                        "is_final_batch": is_last,
                        "mutations": chunk,
                    }
                ),
                content_type="application/json",
            )
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["applied_count"], len(chunk))
        wallet.refresh_from_db()
        self.assertEqual(wallet.current_balance, Decimal("552.000"))

        # 5. Dependent operations on the same entity stop when an earlier operation fails/conflicts,
        # while independent operations on other entities continue
        dep_entity_id = str(uuid.uuid4())
        indep_entity_id = str(uuid.uuid4())
        dep_batch = [
            make_create_mut(500, "10.00", entity_id=dep_entity_id),
            {
                "mutation_id": str(uuid.uuid4()),
                "queue_seq": 501,
                "operation": "update_transaction",
                "entity_id": dep_entity_id,
                "base_revision": 99,  # Stale revision -> conflict!
                "payload": {
                    "transaction_type": "income",
                    "amount": "20.00",
                    "wallet_id": str(wallet.id),
                    "transaction_date": "2026-10-20",
                    "scope": "personal",
                    "description": "Conflicting update",
                },
            },
            {
                "mutation_id": str(uuid.uuid4()),
                "queue_seq": 502,
                "operation": "delete_transaction",
                "entity_id": dep_entity_id,
                "base_revision": 1,  # Would otherwise match rev 1 if not blocked!
                "payload": {},
            },
            make_create_mut(503, "5.00", entity_id=indep_entity_id),
        ]
        dep_outcome = process_sync_batch(
            user=self.alice,
            mutations=dep_batch,
            expected_user_id=self.alice.pk,
        )
        self.assertEqual(dep_outcome["batch_status"], "partial")
        self.assertEqual(dep_outcome["applied_count"], 2)  # create on dep_entity + create on indep_entity
        self.assertEqual(dep_outcome["conflict_count"], 1)
        self.assertEqual(dep_outcome["blocked_count"], 1)
        statuses = [r["status"] for r in dep_outcome["results"]]
        self.assertEqual(statuses, ["applied", "conflict", "blocked_by_dependency", "applied"])
        # Verify the dependent delete_transaction was NOT executed
        dep_tx = Transaction.objects.get(pk=dep_entity_id)
        self.assertFalse(dep_tx.is_deleted)
        self.assertEqual(dep_tx.amount, Decimal("10.000"))
        self.assertTrue(Transaction.objects.filter(pk=indep_entity_id, is_deleted=False).exists())

        # 6. Cross-account upload attempt is rejected with 409 user_mismatch and never applies under Bob
        self.client.force_login(self.bob)
        mismatch_mut = make_create_mut(600, "99.00")
        mismatch_resp = self.client.post(
            reverse("budgets:sync_mutations_api"),
            data=json.dumps(
                {
                    "expected_user_id": self.alice.pk,
                    "mutations": [mismatch_mut],
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(mismatch_resp.status_code, 409)
        self.assertEqual(mismatch_resp.json()["batch_status"], "user_mismatch")
        self.assertFalse(
            SyncMutationReceipt.objects.filter(mutation_id=mismatch_mut["mutation_id"]).exists()
        )

        # 7. Server idempotency vs. follow-up edits made during an in-flight upload (Scenarios A-F)
        inflight_entity_id = str(uuid.uuid4())
        create_mut_id = str(uuid.uuid4())
        initial_create = make_create_mut(
            700, "10.00", entity_id=inflight_entity_id, mutation_id=create_mut_id
        )
        initial_create["payload"]["description"] = "Initial create before in-flight edit"

        create_outcome = process_sync_batch(
            user=self.alice,
            mutations=[initial_create],
            expected_user_id=self.alice.pk,
            is_final_batch=False,
        )
        self.assertEqual(create_outcome["batch_status"], "success")
        self.assertEqual(create_outcome["results"][0]["status"], "applied")
        self.assertEqual(create_outcome["results"][0]["revision"], 1)

        # Scenario F: Reusing the committed mutation_id with a changed operation/base_revision/payload is rejected
        bad_reuse_mut = {
            "mutation_id": create_mut_id,
            "queue_seq": 700,
            "operation": "update_transaction",
            "entity_id": inflight_entity_id,
            "base_revision": 1,
            "payload": {
                "transaction_type": "income",
                "amount": "99.95",
                "wallet_id": str(wallet.id),
                "transaction_date": "2026-10-20",
                "scope": "personal",
                "description": "Bad reuse of committed mutation_id",
            },
        }
        bad_reuse_outcome = process_sync_batch(
            user=self.alice,
            mutations=[bad_reuse_mut],
            expected_user_id=self.alice.pk,
        )
        self.assertEqual(bad_reuse_outcome["batch_status"], "failed")
        self.assertEqual(bad_reuse_outcome["results"][0]["status"], "error")
        self.assertEqual(bad_reuse_outcome["results"][0]["code"], "mutation_id_reused")
        tx_after_bad_reuse = Transaction.objects.get(pk=inflight_entity_id)
        self.assertEqual(tx_after_bad_reuse.amount, Decimal("10.000"))
        self.assertEqual(tx_after_bad_reuse.revision, 1)

        # Scenario D: Replaying the original immutable create request (lost response retry) succeeds idempotently
        retry_create_outcome = process_sync_batch(
            user=self.alice,
            mutations=[initial_create],
            expected_user_id=self.alice.pk,
        )
        self.assertEqual(retry_create_outcome["batch_status"], "success")
        self.assertTrue(retry_create_outcome["results"][0].get("idempotent_replay"))
        self.assertEqual(retry_create_outcome["results"][0]["revision"], 1)

        # Scenario A: Uploading the in-flight edit as a NEW mutation with base_revision=1 and update_transaction succeeds
        followup_update_1 = {
            "mutation_id": str(uuid.uuid4()),
            "queue_seq": 700,
            "operation": "update_transaction",
            "entity_id": inflight_entity_id,
            "base_revision": 1,
            "payload": {
                "transaction_type": "income",
                "amount": "99.95",
                "wallet_id": str(wallet.id),
                "transaction_date": "2026-10-20",
                "scope": "personal",
                "description": "Edited locally during create upload",
            },
        }
        followup_1_outcome = process_sync_batch(
            user=self.alice,
            mutations=[followup_update_1],
            expected_user_id=self.alice.pk,
            is_final_batch=True,
        )
        self.assertEqual(followup_1_outcome["batch_status"], "success")
        self.assertEqual(followup_1_outcome["results"][0]["status"], "applied")
        self.assertEqual(followup_1_outcome["results"][0]["revision"], 2)
        tx_after_a = Transaction.objects.get(pk=inflight_entity_id)
        self.assertEqual(tx_after_a.amount, Decimal("99.950"))
        self.assertEqual(tx_after_a.description, "Edited locally during create upload")
        self.assertEqual(tx_after_a.revision, 2)

        # Scenario B: Update -> in-flight second edit -> new mutation with base_revision=3 succeeds
        update_b1 = {
            "mutation_id": str(uuid.uuid4()),
            "queue_seq": 701,
            "operation": "update_transaction",
            "entity_id": inflight_entity_id,
            "base_revision": 2,
            "payload": {
                "transaction_type": "income",
                "amount": "110.00",
                "wallet_id": str(wallet.id),
                "transaction_date": "2026-10-20",
                "scope": "personal",
                "description": "Update B1",
            },
        }
        b1_outcome = process_sync_batch(
            user=self.alice,
            mutations=[update_b1],
            expected_user_id=self.alice.pk,
            is_final_batch=False,
        )
        self.assertEqual(b1_outcome["results"][0]["revision"], 3)

        update_b2_followup = {
            "mutation_id": str(uuid.uuid4()),
            "queue_seq": 701,
            "operation": "update_transaction",
            "entity_id": inflight_entity_id,
            "base_revision": 3,
            "payload": {
                "transaction_type": "income",
                "amount": "125.50",
                "wallet_id": str(wallet.id),
                "transaction_date": "2026-10-20",
                "scope": "personal",
                "description": "Update B2 edited during B1 upload",
            },
        }
        b2_outcome = process_sync_batch(
            user=self.alice,
            mutations=[update_b2_followup],
            expected_user_id=self.alice.pk,
            is_final_batch=True,
        )
        self.assertEqual(b2_outcome["batch_status"], "success")
        self.assertEqual(b2_outcome["results"][0]["revision"], 4)
        tx_after_b = Transaction.objects.get(pk=inflight_entity_id)
        self.assertEqual(tx_after_b.amount, Decimal("125.500"))
        self.assertEqual(tx_after_b.description, "Update B2 edited during B1 upload")
        self.assertEqual(tx_after_b.revision, 4)

        # Scenario C: Delete while an earlier create/update is uploading -> new mutation with delete_transaction & base_revision=4
        delete_c_followup = {
            "mutation_id": str(uuid.uuid4()),
            "queue_seq": 702,
            "operation": "delete_transaction",
            "entity_id": inflight_entity_id,
            "base_revision": 4,
            "payload": {},
        }
        c_outcome = process_sync_batch(
            user=self.alice,
            mutations=[delete_c_followup],
            expected_user_id=self.alice.pk,
            is_final_batch=True,
        )
        self.assertEqual(c_outcome["batch_status"], "success")
        self.assertEqual(c_outcome["results"][0]["status"], "applied")
        self.assertEqual(c_outcome["results"][0]["revision"], 5)
        tx_after_c = Transaction.objects.get(pk=inflight_entity_id)
        self.assertTrue(tx_after_c.is_deleted)
        self.assertEqual(tx_after_c.revision, 5)

    def test_transaction_display_currency_conversion_and_isolation(self):
        from .models import ExchangeRate, Transaction, Wallet
        from .services import create_transaction_entry
        from .exchange_rates import record_manual_exchange_rate
        from apps.accounts.models import UserPreference
        from django.utils import timezone
        from datetime import timedelta

        UserPreference.objects.update_or_create(
            user=self.alice,
            defaults={"personal_base_currency": self.usd, "active_scope": "personal"},
        )

        wallet_usd = Wallet.objects.create(
            owner=self.alice,
            name="Alice USD",
            wallet_type=Wallet.WalletType.BANK_ACCOUNT,
            currency=self.usd,
            opening_balance=Decimal("1000.00"),
            opening_balance_date=date(2026, 10, 1),
        )
        wallet_eur = Wallet.objects.create(
            owner=self.alice,
            name="Alice EUR",
            wallet_type=Wallet.WalletType.BANK_ACCOUNT,
            currency=self.eur,
            opening_balance=Decimal("1000.00"),
            opening_balance_date=date(2026, 10, 1),
        )

        tx_usd = create_transaction_entry(
            user=self.alice,
            transaction_type="income",
            amount=Decimal("100.00"),
            wallet=wallet_usd,
            transaction_date=timezone.localdate(),
            description="Consulting USD",
        )
        tx_eur = create_transaction_entry(
            user=self.alice,
            transaction_type="expense",
            amount=Decimal("50.00"),
            wallet=wallet_eur,
            transaction_date=timezone.localdate(),
            description="Server hosting EUR",
        )

        tx_usd_initial_converted = tx_usd.converted_amount
        tx_usd_initial_base_curr = tx_usd.base_currency

        ExchangeRate.objects.create(
            from_currency=self.usd,
            to_currency=self.php,
            rate=Decimal("56.50000000"),
            provider="openexchangerates",
            rate_timestamp=timezone.now(),
            scope=ExchangeRate.Scope.PROVIDER,
            is_manual=False,
        )

        record_manual_exchange_rate(
            user=self.alice,
            from_currency=self.usd,
            to_currency=self.twd,
            rate=Decimal("32.00000000"),
            scope="personal",
        )

        # Bob records a private manual rate EUR -> TWD; Alice must NOT see or use it
        record_manual_exchange_rate(
            user=self.bob,
            from_currency=self.eur,
            to_currency=self.twd,
            rate=Decimal("99.00000000"),
            scope="personal",
        )

        self.client.force_login(self.alice)

        # 1. Default view uses Alice's base currency (USD)
        resp_default = self.client.get("/transactions/")
        self.assertEqual(resp_default.status_code, 200)
        self.assertContains(resp_default, "Converted (USD)")
        self.assertContains(resp_default, "+100.00 USD")
        self.assertContains(resp_default, "Same currency (1:1)")

        # 2. View in PHP via ?display_currency=PHP
        resp_php = self.client.get("/transactions/?display_currency=PHP")
        self.assertEqual(resp_php.status_code, 200)
        self.assertContains(resp_php, "Converted (PHP)")
        self.assertContains(resp_php, "+100.00 USD")
        self.assertContains(resp_php, "+5,650.00 PHP")
        self.assertContains(resp_php, f"Booked: {tx_usd.formatted_converted_amount} {tx_usd.base_currency.code}")

        # 3. View in TWD via ?display_currency=TWD
        resp_twd = self.client.get("/transactions/?display_currency=TWD")
        self.assertEqual(resp_twd.status_code, 200)
        self.assertContains(resp_twd, "Converted (TWD)")
        self.assertContains(resp_twd, "+3,200.00 TWD")
        self.assertContains(resp_twd, "Valuation: Available (manual)")

        # For EUR -> TWD, Bob has rate 99.00, but Alice has no access; must show unavailable
        self.assertContains(resp_twd, "Conversion unavailable")
        self.assertNotContains(resp_twd, "4950.00 TWD")

        # 4. Database records remain untouched
        tx_usd_after = Transaction.objects.get(pk=tx_usd.pk)
        self.assertEqual(tx_usd_after.amount, Decimal("100.000"))
        self.assertEqual(tx_usd_after.currency, self.usd)
        self.assertEqual(tx_usd_after.converted_amount, tx_usd_initial_converted)
        self.assertEqual(tx_usd_after.base_currency, tx_usd_initial_base_curr)

        # 5. Filter separation: ?currency=USD&display_currency=PHP
        resp_filtered = self.client.get("/transactions/?currency=USD&display_currency=PHP")
        self.assertEqual(resp_filtered.status_code, 200)
        self.assertContains(resp_filtered, "Consulting USD")
        self.assertNotContains(resp_filtered, "Server hosting EUR")
        self.assertContains(resp_filtered, "+5,650.00 PHP")

        # 6. Stale rate warning
        stale_time = timezone.now() - timedelta(hours=50)
        ExchangeRate.objects.filter(from_currency=self.usd, to_currency=self.php).update(
            rate_timestamp=stale_time
        )
        resp_stale = self.client.get("/transactions/?display_currency=PHP")
        self.assertEqual(resp_stale.status_code, 200)
        self.assertContains(resp_stale, "Stale rate")

        # 7. Rate caching and identity bypass verification
        from unittest.mock import patch
        import apps.budgets.selectors as selectors_module

        # Add additional transactions: 2 more USD transactions and 2 PHP transactions
        create_transaction_entry(
            user=self.alice,
            transaction_type="expense",
            amount=Decimal("15.00"),
            wallet=wallet_usd,
            transaction_date=date(2026, 10, 7),
            description="Coffee USD",
        )
        create_transaction_entry(
            user=self.alice,
            transaction_type="expense",
            amount=Decimal("25.00"),
            wallet=wallet_usd,
            transaction_date=date(2026, 10, 8),
            description="Book USD",
        )
        wallet_php = Wallet.objects.create(
            owner=self.alice,
            name="Alice PHP",
            wallet_type=Wallet.WalletType.CASH,
            currency=self.php,
            opening_balance=Decimal("5000.00"),
            opening_balance_date=date(2026, 10, 1),
        )
        create_transaction_entry(
            user=self.alice,
            transaction_type="expense",
            amount=Decimal("150.00"),
            wallet=wallet_php,
            transaction_date=date(2026, 10, 9),
            description="Snack PHP",
        )

        real_get_latest_rate = selectors_module.get_latest_rate
        with patch.object(selectors_module, "get_latest_rate", wraps=real_get_latest_rate) as mock_rate_lookup:
            resp_batch = self.client.get("/transactions/?currency=USD&display_currency=PHP")
            self.assertEqual(resp_batch.status_code, 200)
            # 3 USD transactions were rendered, but USD->PHP rate was fetched exactly ONCE
            self.assertEqual(mock_rate_lookup.call_count, 1)

        with patch.object(selectors_module, "get_latest_rate", wraps=real_get_latest_rate) as mock_rate_lookup:
            resp_identity = self.client.get("/transactions/?currency=PHP&display_currency=PHP")
            self.assertEqual(resp_identity.status_code, 200)
            # PHP transactions displayed in PHP: 0 rate lookups (identity bypass)
            self.assertEqual(mock_rate_lookup.call_count, 0)

        # 8. Pagination preserves display_currency parameter
        for i in range(25):
            create_transaction_entry(
                user=self.alice,
                transaction_type="expense",
                amount=Decimal("1.00"),
                wallet=wallet_usd,
                transaction_date=timezone.localdate(),
                description=f"Pagination test item {i}",
            )
        resp_paginated = self.client.get("/transactions/?display_currency=TWD")
        self.assertEqual(resp_paginated.status_code, 200)
        self.assertContains(resp_paginated, "display_currency=TWD")
        self.assertContains(resp_paginated, "page=2")

    def test_mixed_scope_transaction_display_conversion_and_cache_isolation(self):
        from .models import ExchangeRate, SharedSpace, SharedSpaceMembership, Transaction, Wallet
        from .services import create_transaction_entry
        from .exchange_rates import record_manual_exchange_rate
        from .selectors import enrich_transactions_with_display_currency
        import apps.budgets.selectors as selectors_module
        from apps.accounts.models import UserPreference
        from unittest.mock import patch

        # Create two distinct shared spaces where Alice is an active member
        space_a = SharedSpace.objects.create(
            name="Alpha Shared Space",
            created_by=self.alice,
            base_currency=self.usd,
        )
        SharedSpaceMembership.objects.create(
            shared_space=space_a,
            user=self.alice,
            role=SharedSpaceMembership.Role.CREATOR,
            is_active=True,
        )

        space_b = SharedSpace.objects.create(
            name="Beta Shared Space",
            created_by=self.bob,
            base_currency=self.usd,
        )
        SharedSpaceMembership.objects.create(
            shared_space=space_b,
            user=self.bob,
            role=SharedSpaceMembership.Role.CREATOR,
            is_active=True,
        )
        SharedSpaceMembership.objects.create(
            shared_space=space_b,
            user=self.alice,
            role=SharedSpaceMembership.Role.MEMBER,
            is_active=True,
        )

        # Wallets in USD for personal, space A, and space B
        wallet_p = Wallet.objects.create(
            owner=self.alice,
            name="Alice Personal USD",
            wallet_type=Wallet.WalletType.BANK_ACCOUNT,
            ownership_scope=Wallet.OwnershipScope.PERSONAL,
            currency=self.usd,
            opening_balance=Decimal("1000.00"),
            opening_balance_date=date(2026, 10, 1),
        )
        wallet_a = Wallet.objects.create(
            owner=self.alice,
            name="Alpha USD Wallet",
            wallet_type=Wallet.WalletType.BANK_ACCOUNT,
            ownership_scope=Wallet.OwnershipScope.SHARED,
            shared_space=space_a,
            currency=self.usd,
            opening_balance=Decimal("1000.00"),
            opening_balance_date=date(2026, 10, 1),
        )
        wallet_b = Wallet.objects.create(
            owner=self.bob,
            name="Beta USD Wallet",
            wallet_type=Wallet.WalletType.BANK_ACCOUNT,
            ownership_scope=Wallet.OwnershipScope.SHARED,
            shared_space=space_b,
            currency=self.usd,
            opening_balance=Decimal("1000.00"),
            opening_balance_date=date(2026, 10, 1),
        )
        wallet_twd = Wallet.objects.create(
            owner=self.alice,
            name="Alice TWD Cash",
            wallet_type=Wallet.WalletType.CASH,
            ownership_scope=Wallet.OwnershipScope.PERSONAL,
            currency=self.twd,
            opening_balance=Decimal("2000.00"),
            opening_balance_date=date(2026, 10, 1),
        )

        # Authorized rates for Alice's contexts (USD -> TWD with deliberately different rates)
        # 1) Personal manual rate: 32.00
        record_manual_exchange_rate(
            user=self.alice,
            from_currency=self.usd,
            to_currency=self.twd,
            rate=Decimal("32.00000000"),
            scope="personal",
        )
        # 2) Shared manual rate for Space A: 30.00
        record_manual_exchange_rate(
            user=self.alice,
            from_currency=self.usd,
            to_currency=self.twd,
            rate=Decimal("30.00000000"),
            scope="shared",
            shared_space=space_a,
        )
        # 3) Shared manual rate for Space B: 35.00
        record_manual_exchange_rate(
            user=self.bob,
            from_currency=self.usd,
            to_currency=self.twd,
            rate=Decimal("35.00000000"),
            scope="shared",
            shared_space=space_b,
        )

        # Unauthorized rates that MUST be excluded from Alice's display conversions:
        # - Bob's private personal manual rate: 99.00
        record_manual_exchange_rate(
            user=self.bob,
            from_currency=self.usd,
            to_currency=self.twd,
            rate=Decimal("99.00000000"),
            scope="personal",
        )
        # - Space C manual rate (Alice is not a member): 88.00
        space_c = SharedSpace.objects.create(
            name="Gamma Space C",
            created_by=self.bob,
            base_currency=self.usd,
        )
        SharedSpaceMembership.objects.create(
            shared_space=space_c,
            user=self.bob,
            role=SharedSpaceMembership.Role.CREATOR,
            is_active=True,
        )
        record_manual_exchange_rate(
            user=self.bob,
            from_currency=self.usd,
            to_currency=self.twd,
            rate=Decimal("88.00000000"),
            scope="shared",
            shared_space=space_c,
        )

        # Transactions across personal, Space A, Space B, and a same-currency row
        tx_personal = create_transaction_entry(
            user=self.alice,
            transaction_type="expense",
            amount=Decimal("100.00"),
            wallet=wallet_p,
            transaction_date=timezone.localdate(),
            scope="personal",
            description="Personal Grocery",
        )
        tx_space_a1 = create_transaction_entry(
            user=self.alice,
            transaction_type="expense",
            amount=Decimal("100.00"),
            wallet=wallet_a,
            transaction_date=timezone.localdate(),
            scope="shared",
            shared_space=space_a,
            description="Alpha Server",
        )
        tx_space_a2 = create_transaction_entry(
            user=self.alice,
            transaction_type="expense",
            amount=Decimal("50.00"),
            wallet=wallet_a,
            transaction_date=timezone.localdate(),
            scope="shared",
            shared_space=space_a,
            description="Alpha Cables",
        )
        tx_space_b = create_transaction_entry(
            user=self.bob,
            transaction_type="expense",
            amount=Decimal("100.00"),
            wallet=wallet_b,
            transaction_date=timezone.localdate(),
            scope="shared",
            shared_space=space_b,
            description="Beta Subscription",
        )
        tx_twd = create_transaction_entry(
            user=self.alice,
            transaction_type="expense",
            amount=Decimal("500.00"),
            wallet=wallet_twd,
            transaction_date=timezone.localdate(),
            scope="personal",
            description="TWD Lunch",
        )

        # Baseline snapshots before enrichment
        initial_p_converted = tx_personal.converted_amount
        initial_p_currency = tx_personal.currency
        initial_a1_converted = tx_space_a1.converted_amount
        initial_b_converted = tx_space_b.converted_amount

        # 1. Direct selector verification: rate cache isolation and lookup reuse
        mixed_list = [tx_personal, tx_space_a1, tx_space_b, tx_space_a2, tx_twd]
        real_get_latest_rate = selectors_module.get_latest_rate
        cache_dict = {}

        with patch.object(selectors_module, "get_latest_rate", wraps=real_get_latest_rate) as mock_lookup:
            enrich_transactions_with_display_currency(
                mixed_list,
                self.twd,
                user=self.alice,
                rates_cache=cache_dict,
            )
            # Must perform exactly 3 lookups (Personal, Space A, Space B).
            # Space A2 reuses Space A1 cached rate.
            # tx_twd bypasses lookup (identity).
            self.assertEqual(mock_lookup.call_count, 3)

        # Check converted valuations:
        # Personal: 100 * 32.00 = 3200.00 TWD
        self.assertEqual(tx_personal.display_amount, Decimal("3200.00"))
        self.assertEqual(tx_personal.formatted_display_amount, "3,200.00")
        # Space A1: 100 * 30.00 = 3000.00 TWD
        self.assertEqual(tx_space_a1.display_amount, Decimal("3000.00"))
        self.assertEqual(tx_space_a1.formatted_display_amount, "3,000.00")
        # Space B: 100 * 35.00 = 3500.00 TWD
        self.assertEqual(tx_space_b.display_amount, Decimal("3500.00"))
        self.assertEqual(tx_space_b.formatted_display_amount, "3,500.00")
        # Space A2: 50 * 30.00 = 1500.00 TWD (reused Space A lookup)
        self.assertEqual(tx_space_a2.display_amount, Decimal("1500.00"))
        self.assertEqual(tx_space_a2.formatted_display_amount, "1,500.00")
        # Same-currency row: 500.00 TWD
        self.assertEqual(tx_twd.display_amount, Decimal("500.00"))
        self.assertEqual(tx_twd.display_status, "identity")

        # Verify cache keys did not bleed between spaces
        self.assertIn((self.alice.id, self.usd.id, self.twd.id, "personal", None), cache_dict)
        self.assertIn((self.alice.id, self.usd.id, self.twd.id, "shared", space_a.id), cache_dict)
        self.assertIn((self.alice.id, self.usd.id, self.twd.id, "shared", space_b.id), cache_dict)
        self.assertEqual(len(cache_dict), 3)

        # Verify original and booked database fields are completely untouched
        tx_personal.refresh_from_db()
        tx_space_a1.refresh_from_db()
        tx_space_b.refresh_from_db()
        self.assertEqual(tx_personal.amount, Decimal("100.000"))
        self.assertEqual(tx_personal.currency, initial_p_currency)
        self.assertEqual(tx_personal.converted_amount, initial_p_converted)
        self.assertEqual(tx_space_a1.converted_amount, initial_a1_converted)
        self.assertEqual(tx_space_b.converted_amount, initial_b_converted)

        # 2. HTTP view verification: switching active workspace does not alter row rate contexts
        self.client.force_login(self.alice)
        pref, _ = UserPreference.objects.get_or_create(user=self.alice)

        # Case A: Active workspace is Personal
        pref.active_scope = "personal"
        pref.active_shared_space = None
        pref.save()
        resp_p = self.client.get("/transactions/?display_currency=TWD")
        self.assertEqual(resp_p.status_code, 200)
        self.assertContains(resp_p, "-3,200.00 TWD")  # Personal row (rate 32)
        self.assertContains(resp_p, "-3,000.00 TWD")  # Space A row (rate 30)
        self.assertContains(resp_p, "-1,500.00 TWD")  # Space A2 row (rate 30)
        self.assertContains(resp_p, "-3,500.00 TWD")  # Space B row (rate 35)
        self.assertNotContains(resp_p, "9,900.00")    # Unauthorized private rate excluded
        self.assertNotContains(resp_p, "8,800.00")    # Unauthorized private rate excluded

        # Case B: Active workspace is Shared Space A
        pref.active_scope = "shared"
        pref.active_shared_space = space_a
        pref.save()
        resp_a = self.client.get("/transactions/?display_currency=TWD")
        self.assertEqual(resp_a.status_code, 200)
        self.assertContains(resp_a, "-3,200.00 TWD")  # Personal row STILL uses 32
        self.assertContains(resp_a, "-3,000.00 TWD")  # Space A row STILL uses 30
        self.assertContains(resp_a, "-1,500.00 TWD")  # Space A2 row STILL uses 30
        self.assertContains(resp_a, "-3,500.00 TWD")  # Space B row STILL uses 35

        # Case C: Active workspace is Shared Space B
        pref.active_scope = "shared"
        pref.active_shared_space = space_b
        pref.save()
        resp_b = self.client.get("/transactions/?display_currency=TWD")
        self.assertEqual(resp_b.status_code, 200)
        self.assertContains(resp_b, "-3,200.00 TWD")  # Personal row STILL uses 32
        self.assertContains(resp_b, "-3,000.00 TWD")  # Space A row STILL uses 30
        self.assertContains(resp_b, "-1,500.00 TWD")  # Space A2 row STILL uses 30
        self.assertContains(resp_b, "-3,500.00 TWD")  # Space B row STILL uses 35


class ExchangeRateProviderAndCoverageTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user(username="alice_rates", password="Str0ng!Password#2026")
        self.bob = User.objects.create_user(username="bob_rates", password="Str0ng!Password#2026")
        self.usd = Currency.objects.get(code="USD")
        self.eur = Currency.objects.get(code="EUR")
        self.php = Currency.objects.get(code="PHP")
        self.jpy = Currency.objects.get(code="JPY")
        self.kwd = Currency.objects.get(code="KWD")
        self.twd = Currency.objects.get(code="TWD")
        from apps.accounts.models import UserPreference
        UserPreference.objects.update_or_create(
            user=self.alice,
            defaults={"personal_base_currency": self.usd, "active_scope": "personal"},
        )
        UserPreference.objects.update_or_create(
            user=self.bob,
            defaults={"personal_base_currency": self.usd, "active_scope": "personal"},
        )

    def test_frankfurter_v2_twd_response_parsing_and_cross_rates(self):
        from decimal import ROUND_HALF_UP
        from django.utils import timezone
        from .exchange_rates import refresh_exchange_rates, get_latest_rate

        today = timezone.now().date()
        today_str = today.isoformat()

        def mock_fetcher(url):
            self.assertIn("api.frankfurter.dev/v2/rates", url)
            return [
                {"date": today_str, "base": "PHP", "quote": "EUR", "rate": Decimal("0.01404000")},
                {"date": today_str, "base": "PHP", "quote": "JPY", "rate": Decimal("2.47390000")},
                {"date": today_str, "base": "PHP", "quote": "KWD", "rate": Decimal("0.00486000")},
                {"date": today_str, "base": "PHP", "quote": "TWD", "rate": Decimal("0.51249000")},
                {"date": today_str, "base": "PHP", "quote": "USD", "rate": Decimal("0.01575000")},
            ]

        result = refresh_exchange_rates(base_currency_code="PHP", force=True, http_fetcher=mock_fetcher)
        self.assertEqual(result["status"], "updated")
        self.assertEqual(result["provider"], "frankfurter_v2")
        self.assertTrue(result["refreshed"])

        # Direct rate PHP -> TWD is available from frankfurter_v2 with Decimal precision
        lookup_php_twd = get_latest_rate(self.php, self.twd, user=self.alice)
        self.assertEqual(lookup_php_twd.status, "available")
        self.assertEqual(lookup_php_twd.provider, "frankfurter_v2")
        self.assertEqual(lookup_php_twd.rate, Decimal("0.51249000"))
        self.assertEqual(lookup_php_twd.rate_timestamp.date(), today)
        self.assertFalse(lookup_php_twd.is_manual)

        # Cross rate USD -> TWD derived from same-date inputs: 0.51249 / 0.01575
        expected_usd_twd = (Decimal("0.51249") / Decimal("0.01575")).quantize(
            Decimal("0.00000001"), rounding=ROUND_HALF_UP
        )
        lookup_usd_twd = get_latest_rate(self.usd, self.twd, user=self.alice)
        self.assertEqual(lookup_usd_twd.status, "available")
        self.assertEqual(lookup_usd_twd.provider, "frankfurter_v2")
        self.assertEqual(lookup_usd_twd.rate, expected_usd_twd)

    def test_mixed_date_api_responses_retain_correct_individual_dates(self):
        from .exchange_rates import refresh_exchange_rates
        from .models import ExchangeRate

        def mock_fetcher(url):
            return [
                {"date": "2026-10-25", "base": "PHP", "quote": "USD", "rate": Decimal("0.01575000")},
                {"date": "2026-10-24", "base": "PHP", "quote": "TWD", "rate": Decimal("0.51249000")},
            ]

        result = refresh_exchange_rates(base_currency_code="PHP", force=True, http_fetcher=mock_fetcher)
        self.assertEqual(result["status"], "updated")

        rec_usd = ExchangeRate.objects.get(provider="frankfurter_v2", from_currency=self.php, to_currency=self.usd)
        rec_twd = ExchangeRate.objects.get(provider="frankfurter_v2", from_currency=self.php, to_currency=self.twd)

        # Each quote retains its own actual effective date independently
        self.assertEqual(rec_usd.rate_timestamp.date(), date(2026, 10, 25))
        self.assertEqual(rec_twd.rate_timestamp.date(), date(2026, 10, 24))

    def test_mixed_date_inputs_do_not_produce_misleading_same_date_cross_rates(self):
        from .exchange_rates import refresh_exchange_rates
        from .models import ExchangeRate

        def mock_fetcher(url):
            return [
                {"date": "2026-10-25", "base": "PHP", "quote": "USD", "rate": Decimal("0.01575000")},
                {"date": "2026-10-24", "base": "PHP", "quote": "TWD", "rate": Decimal("0.51249000")},
            ]

        refresh_exchange_rates(base_currency_code="PHP", force=True, http_fetcher=mock_fetcher)

        # Mismatched dates (2026-10-25 != 2026-10-24): cross-rate USD -> TWD must NOT be synthesized!
        self.assertFalse(
            ExchangeRate.objects.filter(
                provider="frankfurter_v2",
                from_currency=self.usd,
                to_currency=self.twd,
            ).exists()
        )

    def test_invalid_and_missing_dates_raise_explicit_validation_failure(self):
        from .exchange_rates import refresh_exchange_rates

        # Missing date
        def mock_missing_date(url):
            return [
                {"base": "PHP", "quote": "USD", "rate": Decimal("0.01575")},
            ]

        res1 = refresh_exchange_rates(base_currency_code="PHP", force=True, http_fetcher=mock_missing_date)
        self.assertEqual(res1["status"], "error")
        self.assertIn("missing", res1["message"].lower())

        # Invalid date format
        def mock_bad_date(url):
            return [
                {"date": "invalid-date", "base": "PHP", "quote": "USD", "rate": Decimal("0.01575")},
            ]

        res2 = refresh_exchange_rates(base_currency_code="PHP", force=True, http_fetcher=mock_bad_date)
        self.assertEqual(res2["status"], "error")
        self.assertIn("invalid date", res2["message"].lower())

    def test_decimal_json_parsing_preserves_precision(self):
        from .exchange_rates import _fetch_json_url
        from unittest.mock import patch
        import io

        json_bytes = b'[{"date": "2026-10-25", "base": "PHP", "quote": "USD", "rate": 0.015748031496}]'
        mock_resp = io.BytesIO(json_bytes)
        mock_resp.status = 200

        with patch("apps.budgets.exchange_rates.urlopen") as mock_urlopen:
            mock_urlopen.return_value.__enter__.return_value = mock_resp
            payload = _fetch_json_url("https://api.frankfurter.dev/v2/rates")

        rate_val = payload[0]["rate"]
        # Must be Decimal instance directly from json.loads(parse_float=Decimal)
        self.assertIsInstance(rate_val, Decimal)
        self.assertEqual(rate_val, Decimal("0.015748031496"))

    def test_refresh_deduplication_on_repeated_refreshes(self):
        from .exchange_rates import refresh_exchange_rates
        from .models import ExchangeRate

        def mock_fetcher(url):
            return [
                {"date": "2026-10-25", "base": "PHP", "quote": "EUR", "rate": Decimal("0.01404000")},
                {"date": "2026-10-25", "base": "PHP", "quote": "TWD", "rate": Decimal("0.51249000")},
            ]

        # First refresh stores records
        res1 = refresh_exchange_rates(base_currency_code="PHP", force=True, http_fetcher=mock_fetcher)
        self.assertEqual(res1["status"], "updated")
        count_first = ExchangeRate.objects.filter(provider="frankfurter_v2").count()
        self.assertGreater(count_first, 0)

        # Repeated refresh on the same effective date with updated rate
        def mock_fetcher_updated(url):
            return [
                {"date": "2026-10-25", "base": "PHP", "quote": "EUR", "rate": Decimal("0.01405000")},
                {"date": "2026-10-25", "base": "PHP", "quote": "TWD", "rate": Decimal("0.51260000")},
            ]

        res2 = refresh_exchange_rates(base_currency_code="PHP", force=True, http_fetcher=mock_fetcher_updated)
        self.assertEqual(res2["status"], "updated")
        count_second = ExchangeRate.objects.filter(provider="frankfurter_v2").count()
        # Must NOT create duplicate records!
        self.assertEqual(count_first, count_second)

        # Verify rate was updated in place
        rate_record = ExchangeRate.objects.get(
            provider="frankfurter_v2", from_currency=self.php, to_currency=self.twd
        )
        self.assertEqual(rate_record.rate, Decimal("0.51260000"))

    def test_legacy_provider_cooldown_does_not_block_current_provider(self):
        from .exchange_rates import refresh_exchange_rates
        from .models import ExchangeRate
        from datetime import timedelta
        from django.utils import timezone

        # A recent legacy ECB refresh happened 2 minutes ago
        recent_ecb_time = timezone.now() - timedelta(minutes=2)
        ExchangeRate.objects.create(
            from_currency=self.eur,
            to_currency=self.usd,
            rate=Decimal("1.10000000"),
            provider="frankfurter_ecb",
            is_manual=False,
            scope=ExchangeRate.Scope.PROVIDER,
            rate_timestamp=recent_ecb_time,
            retrieved_at=recent_ecb_time,
        )

        def mock_fetcher(url):
            return [
                {"date": "2026-10-25", "base": "PHP", "quote": "USD", "rate": Decimal("0.01575000")},
                {"date": "2026-10-25", "base": "PHP", "quote": "TWD", "rate": Decimal("0.51249000")},
            ]

        # Should NOT be blocked by legacy ECB cooldown!
        res = refresh_exchange_rates(base_currency_code="PHP", force=False, http_fetcher=mock_fetcher)
        self.assertEqual(res["status"], "updated")
        self.assertEqual(res["provider"], "frankfurter_v2")

    def test_currencies_page_exchange_rates_table_follows_display_currency(self):
        from .models import ExchangeRate
        from datetime import datetime, time, timezone as dt_timezone
        from django.utils import timezone
        from apps.accounts.models import UserPreference

        now = timezone.now()
        eff_date = date(2026, 10, 25)
        eff_ts = datetime.combine(eff_date, time.min, tzinfo=dt_timezone.utc)

        # Setup PHP rates
        for curr, rate_val in [
            (self.usd, Decimal("0.01575000")),
            (self.twd, Decimal("0.51249000")),
            (self.eur, Decimal("0.01404000")),
            (self.jpy, Decimal("2.47390000")),
            (self.kwd, Decimal("0.00486000")),
        ]:
            ExchangeRate.objects.create(
                from_currency=self.php,
                to_currency=curr,
                rate=rate_val,
                provider="frankfurter_v2",
                is_manual=False,
                scope=ExchangeRate.Scope.PROVIDER,
                rate_timestamp=eff_ts,
                retrieved_at=now,
            )

        self.client.force_login(self.alice)

        # 1. With PHP as display currency
        UserPreference.objects.filter(user=self.alice).update(display_currency=self.php)
        resp_php = self.client.get("/currencies/?from_currency=USD")
        self.assertEqual(resp_php.status_code, 200)
        self.assertContains(resp_php, "Exchange rates — PHP base")
        self.assertNotContains(resp_php, 'id="select-from-currency"')
        self.assertContains(resp_php, "1 PHP = <span class=\"fw-semibold\">0.01575000</span> USD")
        self.assertContains(resp_php, "1 PHP = <span class=\"fw-semibold\">0.51249000</span> TWD")
        self.assertNotContains(resp_php, "1 PHP = <span class=\"fw-semibold\">1.00000000</span> PHP")

        # 2. Switch to TWD as display currency
        UserPreference.objects.filter(user=self.alice).update(display_currency=self.twd)
        resp_twd = self.client.get("/currencies/")
        self.assertEqual(resp_twd.status_code, 200)
        self.assertContains(resp_twd, "Exchange rates — TWD base")
        self.assertNotContains(resp_twd, "1 TWD = <span class=\"fw-semibold\">1.00000000</span> TWD")
        # Direct or safely derived same-date cross rate TWD -> USD (0.01575 / 0.51249)
        self.assertContains(resp_twd, "1 TWD =")

        # 3. Switch to USD as display currency
        UserPreference.objects.filter(user=self.alice).update(display_currency=self.usd)
        resp_usd = self.client.get("/currencies/")
        self.assertEqual(resp_usd.status_code, 200)
        self.assertContains(resp_usd, "Exchange rates — USD base")
        self.assertNotContains(resp_usd, "1 USD = <span class=\"fw-semibold\">1.00000000</span> USD")

        # 4. Refresh action requests selected display currency as base
        from unittest.mock import patch
        with patch("apps.budgets.views.refresh_exchange_rates") as mock_refresh:
            mock_refresh.return_value = {"status": "updated", "message": "Rates updated."}
            self.client.post("/currencies/", {"action": "refresh_rates"})
            mock_refresh.assert_called_once_with(base_currency_code="USD", force=False)

        # 5. When no display currency can be resolved, shows selection prompt
        UserPreference.objects.filter(user=self.alice).update(
            personal_base_currency=None,
            display_currency=None,
        )
        resp_unconf = self.client.get("/currencies/")
        self.assertEqual(resp_unconf.status_code, 200)
        self.assertContains(resp_unconf, "No display currency selected")

    def test_currencies_page_manual_rate_authorization_isolation(self):
        from .exchange_rates import record_manual_exchange_rate
        from apps.accounts.models import UserPreference

        # Set Alice's display currency to PHP
        UserPreference.objects.filter(user=self.alice).update(display_currency=self.php)

        # Bob sets a manual rate PHP -> TWD of 0.99
        record_manual_exchange_rate(
            user=self.bob,
            from_currency=self.php,
            to_currency=self.twd,
            rate=Decimal("0.99000000"),
            scope="personal",
        )

        self.client.force_login(self.alice)
        resp_alice = self.client.get("/currencies/")
        self.assertEqual(resp_alice.status_code, 200)
        # Alice must NOT see Bob's 0.99 manual rate
        self.assertNotContains(resp_alice, "0.99000000")

        # Alice sets her own manual rate PHP -> TWD of 0.52
        record_manual_exchange_rate(
            user=self.alice,
            from_currency=self.php,
            to_currency=self.twd,
            rate=Decimal("0.52000000"),
            scope="personal",
        )
        resp_alice_updated = self.client.get("/currencies/")
        self.assertEqual(resp_alice_updated.status_code, 200)
        self.assertContains(resp_alice_updated, "0.52000000")
        self.assertContains(resp_alice_updated, "Manual rate")

    def test_live_frankfurter_provider_twd_check(self):
        from .exchange_rates import _fetch_json_url, FRANKFURTER_V2_ENDPOINT, ExchangeRateRefreshError
        from urllib.error import URLError

        try:
            payload = _fetch_json_url(f"{FRANKFURTER_V2_ENDPOINT}?base=USD&quotes=TWD,EUR")
        except (ExchangeRateRefreshError, URLError, TimeoutError, OSError) as exc:
            self.skipTest(f"Live network access unavailable or timed out: {exc}")

        self.assertIsInstance(payload, list)
        twd_entry = next(
            (item for item in payload if isinstance(item, dict) and item.get("quote") == "TWD"), None
        )
        self.assertIsNotNone(twd_entry, "Live Frankfurter v2 response did not include TWD quote.")
        self.assertEqual(twd_entry["base"], "USD")
        self.assertEqual(twd_entry["quote"], "TWD")
        self.assertGreater(Decimal(str(twd_entry["rate"])), Decimal("0"))


class PersonalTransactionSharingTests(TestCase):
    def setUp(self):
        from .models import (
            Category,
            Currency,
            PersonalTransactionShare,
            SharedSpace,
            SharedSpaceMembership,
            Transaction,
            Wallet,
        )
        from .services import (
            accept_shared_space_invitation,
            create_category,
            create_shared_space,
            create_shared_space_invitation,
            create_transaction_entry,
        )

        self.alice = User.objects.create_user(
            username="alice_owner",
            password="Str0ng!Password#2026",
        )
        self.bob = User.objects.create_user(
            username="bob_partner",
            password="Str0ng!Password#2026",
        )
        self.carol = User.objects.create_user(
            username="carol_stranger",
            password="Str0ng!Password#2026",
        )

        self.usd = Currency.objects.get(code="USD")
        self.eur = Currency.objects.get(code="EUR")
        self.php = Currency.objects.get(code="PHP")

        UserPreference.objects.create(user=self.alice, personal_base_currency=self.usd)
        UserPreference.objects.create(user=self.bob, personal_base_currency=self.usd)
        UserPreference.objects.create(user=self.carol, personal_base_currency=self.usd)

        # Alice creates a shared space and invites Bob
        self.space = create_shared_space(
            user=self.alice,
            name="Alice & Bob Shared Suite",
            base_currency=self.usd,
        )
        inv = create_shared_space_invitation(
            user=self.alice,
            shared_space=self.space,
            partner_hint="bob_partner",
        )
        accept_shared_space_invitation(user=self.bob, code=inv.code)

        # Alice's personal wallet and category
        self.alice_wallet = Wallet.objects.create(
            owner=self.alice,
            name="Alice Secret Swiss Vault",
            wallet_type=Wallet.WalletType.BANK_ACCOUNT,
            currency=self.usd,
            opening_balance=Decimal("5000.00"),
            opening_balance_date=date(2026, 10, 1),
            ownership_scope=Wallet.OwnershipScope.PERSONAL,
        )
        self.alice_category = create_category(
            user=self.alice,
            name="Private Healthcare",
            kind=Category.Kind.EXPENSE,
            scope=Category.Scope.PERSONAL,
        )

        # Alice's personal expense transaction
        self.alice_tx = create_transaction_entry(
            user=self.alice,
            transaction_type=Transaction.Type.EXPENSE,
            wallet=self.alice_wallet,
            category=self.alice_category,
            amount=Decimal("142.50"),
            transaction_date=timezone.localdate(),
            description="Dentist checkup",
            notes="Root canal consultation and antibiotics prescription",
            scope=Transaction.Scope.PERSONAL,
        )

        self.transactions_url = reverse("budgets:transactions")
        self.shared_space_url = reverse("budgets:shared_space")

    def test_private_by_default(self):
        client_bob = Client()
        client_bob.force_login(self.bob)

        # Bob views Shared Space - should show empty shared personal activity
        resp = client_bob.get(self.shared_space_url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Shared personal activity")
        self.assertContains(resp, "No personal transactions have been shared in this space yet.")
        self.assertNotContains(resp, "Dentist checkup")
        self.assertNotContains(resp, "Root canal consultation")

        # Bob views Transactions - should not see Alice's personal transaction
        resp_tx = client_bob.get(self.transactions_url)
        self.assertEqual(resp_tx.status_code, 200)
        self.assertNotContains(resp_tx, "Dentist checkup")

    def test_share_transaction_owner_interface_and_partner_view(self):
        from .models import PersonalTransactionShare

        client_alice = Client()
        client_alice.force_login(self.alice)

        # Alice shares transaction with Bob in the shared space, notes excluded by default
        share_resp = client_alice.post(
            self.transactions_url,
            {
                "action": "share_personal_transaction",
                "transaction_id": str(self.alice_tx.id),
                "shared_space_id": str(self.space.id),
                # include_notes omitted / unchecked
            },
            follow=True,
        )
        self.assertEqual(share_resp.status_code, 200)
        self.assertContains(share_resp, "Personal transaction shared with bob_partner")

        # Verify PersonalTransactionShare record
        share = PersonalTransactionShare.objects.get(
            transaction=self.alice_tx,
            shared_space=self.space,
            recipient=self.bob,
        )
        self.assertFalse(share.is_revoked)
        self.assertFalse(share.include_notes)

        # Verify Alice's Transactions page shows sharing status badge
        self.assertContains(share_resp, "Shared with bob_partner")
        self.assertContains(share_resp, "Notes hidden")
        self.assertContains(share_resp, "Share notes")
        self.assertContains(share_resp, "Stop sharing")

        # Bob views Shared Space
        client_bob = Client()
        client_bob.force_login(self.bob)
        bob_resp = client_bob.get(self.shared_space_url)
        self.assertEqual(bob_resp.status_code, 200)
        self.assertContains(bob_resp, "Shared personal activity")
        self.assertContains(bob_resp, "Recorded by")
        self.assertContains(bob_resp, "Dentist checkup")
        self.assertContains(bob_resp, "-142.50 USD")
        self.assertContains(bob_resp, "alice_owner")
        self.assertContains(bob_resp, "Expense")
        self.assertContains(bob_resp, "Private notes omitted")
        # Ensure Alice's actual note is NOT displayed
        self.assertNotContains(bob_resp, "Root canal consultation and antibiotics prescription")

        # Alice also views Shared Space and sees her outgoing share labeled "You"
        alice_shared_resp = client_alice.get(self.shared_space_url)
        self.assertEqual(alice_shared_resp.status_code, 200)
        self.assertContains(alice_shared_resp, "Shared personal activity")
        self.assertContains(alice_shared_resp, "Recorded by")
        self.assertContains(alice_shared_resp, "Dentist checkup")
        self.assertContains(alice_shared_resp, "You")
        self.assertContains(alice_shared_resp, "Private notes omitted")

    def test_allowlisted_fields_only_no_sensitive_metadata_leakage(self):
        from .services import share_personal_transaction

        share_personal_transaction(
            user=self.alice,
            transaction_id=self.alice_tx.id,
            shared_space_id=self.space.id,
            include_notes=False,
        )

        client_bob = Client()
        client_bob.force_login(self.bob)
        resp = client_bob.get(self.shared_space_url)
        self.assertEqual(resp.status_code, 200)

        # Must NOT contain wallet name or category or booking rate info
        self.assertNotContains(resp, "Alice Secret Swiss Vault")
        self.assertNotContains(resp, "Private Healthcare")
        self.assertNotContains(resp, "Valuation:")
        self.assertNotContains(resp, "Booked:")

    def test_notes_toggle_update_personal_transaction_share(self):
        from .models import PersonalTransactionShare
        from .services import share_personal_transaction

        share = share_personal_transaction(
            user=self.alice,
            transaction_id=self.alice_tx.id,
            shared_space_id=self.space.id,
            include_notes=False,
        )

        client_alice = Client()
        client_alice.force_login(self.alice)

        # Alice updates share to include notes
        update_resp = client_alice.post(
            self.transactions_url,
            {
                "action": "update_personal_transaction_share",
                "share_id": str(share.id),
                "include_notes": "true",
            },
            follow=True,
        )
        self.assertEqual(update_resp.status_code, 200)
        self.assertContains(update_resp, "Personal notes are now included")

        share.refresh_from_db()
        self.assertTrue(share.include_notes)

        # Bob views Shared Space and now sees the notes
        client_bob = Client()
        client_bob.force_login(self.bob)
        bob_resp = client_bob.get(self.shared_space_url)
        self.assertEqual(bob_resp.status_code, 200)
        self.assertContains(bob_resp, "Root canal consultation and antibiotics prescription")

        # Alice toggles notes back off
        client_alice.post(
            self.transactions_url,
            {
                "action": "update_personal_transaction_share",
                "share_id": str(share.id),
                "include_notes": "false",
            },
            follow=True,
        )
        bob_resp2 = client_bob.get(self.shared_space_url)
        self.assertContains(bob_resp2, "Private notes omitted")
        self.assertNotContains(bob_resp2, "Root canal consultation and antibiotics prescription")

    def test_recipient_cannot_modify_or_delete_shared_personal_transaction(self):
        from .services import share_personal_transaction

        share_personal_transaction(
            user=self.alice,
            transaction_id=self.alice_tx.id,
            shared_space_id=self.space.id,
        )

        client_bob = Client()
        client_bob.force_login(self.bob)

        # Bob tries to delete Alice's transaction
        del_resp = client_bob.post(
            self.transactions_url,
            {
                "action": "delete_transaction",
                "transaction_id": str(self.alice_tx.id),
                "expected_revision": self.alice_tx.revision,
            },
            follow=True,
        )
        # Transaction must NOT be deleted
        self.alice_tx.refresh_from_db()
        self.assertFalse(self.alice_tx.is_deleted)

        # Bob tries to update Alice's transaction
        upd_resp = client_bob.post(
            self.transactions_url,
            {
                "action": "update_transaction",
                "transaction_id": str(self.alice_tx.id),
                "description": "Forged update by Bob",
            },
            follow=True,
        )
        self.alice_tx.refresh_from_db()
        self.assertEqual(self.alice_tx.description, "Dentist checkup")

    def test_non_owner_cannot_grant_or_alter_or_revoke_share(self):
        from .models import PersonalTransactionShare
        from .services import share_personal_transaction

        share = share_personal_transaction(
            user=self.alice,
            transaction_id=self.alice_tx.id,
            shared_space_id=self.space.id,
        )

        client_bob = Client()
        client_bob.force_login(self.bob)

        # Bob tries to revoke Alice's share
        bob_revoke_resp = client_bob.post(
            self.transactions_url,
            {
                "action": "revoke_personal_transaction_share",
                "share_id": str(share.id),
            },
            follow=True,
        )
        share.refresh_from_db()
        self.assertFalse(share.is_revoked)

        # Carol (stranger) tries to update Alice's share
        client_carol = Client()
        client_carol.force_login(self.carol)
        carol_upd_resp = client_carol.post(
            self.transactions_url,
            {
                "action": "update_personal_transaction_share",
                "share_id": str(share.id),
                "include_notes": "true",
            },
            follow=True,
        )
        share.refresh_from_db()
        self.assertFalse(share.include_notes)

    def test_revocation_and_deletion(self):
        from .models import PersonalTransactionShare
        from .services import delete_transaction_entry, share_personal_transaction

        share = share_personal_transaction(
            user=self.alice,
            transaction_id=self.alice_tx.id,
            shared_space_id=self.space.id,
        )

        client_alice = Client()
        client_alice.force_login(self.alice)

        # Alice revokes share
        revoke_resp = client_alice.post(
            self.transactions_url,
            {
                "action": "revoke_personal_transaction_share",
                "share_id": str(share.id),
            },
            follow=True,
        )
        self.assertEqual(revoke_resp.status_code, 200)
        share.refresh_from_db()
        self.assertTrue(share.is_revoked)

        # Bob views Shared Space -> empty state
        client_bob = Client()
        client_bob.force_login(self.bob)
        bob_resp = client_bob.get(self.shared_space_url)
        self.assertContains(bob_resp, "No personal transactions have been shared in this space yet.")

        # Re-sharing reactivates
        share_personal_transaction(
            user=self.alice,
            transaction_id=self.alice_tx.id,
            shared_space_id=self.space.id,
        )
        bob_resp2 = client_bob.get(self.shared_space_url)
        self.assertContains(bob_resp2, "Dentist checkup")

        # Deleting transaction revokes share
        delete_transaction_entry(user=self.alice, tx=self.alice_tx)
        share.refresh_from_db()
        self.assertTrue(share.is_revoked)
        bob_resp3 = client_bob.get(self.shared_space_url)
        self.assertContains(bob_resp3, "No personal transactions have been shared in this space yet.")

    def test_leaving_shared_space_revokes_grants_and_rejoining_does_not_reactivate(self):
        from .models import PersonalTransactionShare
        from .services import (
            accept_shared_space_invitation,
            create_shared_space_invitation,
            leave_shared_space,
            share_personal_transaction,
        )

        share = share_personal_transaction(
            user=self.alice,
            transaction_id=self.alice_tx.id,
            shared_space_id=self.space.id,
        )
        self.assertFalse(share.is_revoked)

        # Bob leaves shared space
        leave_shared_space(user=self.bob, shared_space=self.space)
        share.refresh_from_db()
        self.assertTrue(share.is_revoked)

        # Bob is re-invited and rejoins
        inv2 = create_shared_space_invitation(
            user=self.alice,
            shared_space=self.space,
            partner_hint="bob_partner",
        )
        accept_shared_space_invitation(user=self.bob, code=inv2.code)

        # Share must NOT be reactivated automatically
        share.refresh_from_db()
        self.assertTrue(share.is_revoked)

        client_bob = Client()
        client_bob.force_login(self.bob)
        bob_resp = client_bob.get(self.shared_space_url)
        self.assertContains(bob_resp, "No personal transactions have been shared in this space yet.")

    def test_absent_from_partner_exports_and_sync_snapshots(self):
        from .selectors import get_sync_snapshot_for_user
        from .services import share_personal_transaction

        share_personal_transaction(
            user=self.alice,
            transaction_id=self.alice_tx.id,
            shared_space_id=self.space.id,
        )

        # Bob's sync snapshot
        snapshot = get_sync_snapshot_for_user(self.bob)
        snapshot_tx_ids = [tx["id"] for tx in snapshot["transactions"]]
        self.assertNotIn(str(self.alice_tx.id), snapshot_tx_ids)

        # Bob's export data
        client_bob = Client()
        client_bob.force_login(self.bob)
        export_resp = client_bob.get(reverse("accounts:export_data"))
        self.assertEqual(export_resp.status_code, 200)
        self.assertNotContains(export_resp, str(self.alice_tx.id))
        self.assertNotContains(export_resp, "Dentist checkup")

    def test_idempotent_sharing_and_error_handling(self):
        client_alice = Client()
        client_alice.force_login(self.alice)

        # First share
        resp1 = client_alice.post(
            self.transactions_url,
            {
                "action": "share_personal_transaction",
                "transaction_id": str(self.alice_tx.id),
                "shared_space_id": str(self.space.id),
                "include_notes": "false",
            },
            follow=True,
        )
        self.assertEqual(resp1.status_code, 200)

        # Duplicate share submission is idempotent
        resp2 = client_alice.post(
            self.transactions_url,
            {
                "action": "share_personal_transaction",
                "transaction_id": str(self.alice_tx.id),
                "shared_space_id": str(self.space.id),
                "include_notes": "false",
            },
            follow=True,
        )
        self.assertEqual(resp2.status_code, 200)

        # Invalid UUID handles gracefully
        resp3 = client_alice.post(
            self.transactions_url,
            {
                "action": "share_personal_transaction",
                "transaction_id": "not-a-uuid",
                "shared_space_id": str(self.space.id),
            },
            follow=True,
        )
        self.assertEqual(resp3.status_code, 200)
        self.assertContains(resp3, "Invalid transaction ID.")

    def test_deactivate_owner_membership_directly_bypassing_leave_service_loses_access(self):
        from .models import SharedSpaceMembership
        from .services import share_personal_transaction

        share = share_personal_transaction(
            user=self.alice,
            transaction_id=self.alice_tx.id,
            shared_space_id=self.space.id,
        )

        client_bob = Client()
        client_bob.force_login(self.bob)
        resp1 = client_bob.get(self.shared_space_url)
        self.assertContains(resp1, "Dentist checkup")

        # Directly deactivate owner's membership in the database (bypassing leave_shared_space hook)
        SharedSpaceMembership.objects.filter(shared_space=self.space, user=self.alice).update(is_active=False)

        # Bob requests Shared Space again: must immediately lose access
        resp2 = client_bob.get(self.shared_space_url)
        self.assertEqual(resp2.status_code, 200)
        self.assertNotContains(resp2, "Dentist checkup")
        self.assertContains(resp2, "No personal transactions have been shared in this space yet.")

    def test_deactivate_recipient_membership_access_denied(self):
        from .models import SharedSpaceMembership
        from .services import share_personal_transaction

        share_personal_transaction(
            user=self.alice,
            transaction_id=self.alice_tx.id,
            shared_space_id=self.space.id,
        )

        # Directly deactivate recipient's membership in the database
        SharedSpaceMembership.objects.filter(shared_space=self.space, user=self.bob).update(is_active=False)

        client_bob = Client()
        client_bob.force_login(self.bob)
        resp = client_bob.get(self.shared_space_url)
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "Dentist checkup")

    def test_inactive_owner_or_recipient_cannot_update_notes_sharing_options(self):
        from .models import SharedSpaceMembership
        from .services import share_personal_transaction, update_personal_transaction_share

        share = share_personal_transaction(
            user=self.alice,
            transaction_id=self.alice_tx.id,
            shared_space_id=self.space.id,
            include_notes=False,
        )

        # Deactivate owner directly
        SharedSpaceMembership.objects.filter(shared_space=self.space, user=self.alice).update(is_active=False)
        with self.assertRaises(ValidationError) as cm:
            update_personal_transaction_share(
                user=self.alice,
                share_id=share.id,
                include_notes=True,
            )
        self.assertIn("granting owner is no longer an active member", str(cm.exception))

        # Reactivate owner, deactivate recipient
        SharedSpaceMembership.objects.filter(shared_space=self.space, user=self.alice).update(is_active=True)
        SharedSpaceMembership.objects.filter(shared_space=self.space, user=self.bob).update(is_active=False)
        with self.assertRaises(ValidationError) as cm2:
            update_personal_transaction_share(
                user=self.alice,
                share_id=share.id,
                include_notes=True,
            )
        self.assertIn("partner is no longer an active member", str(cm2.exception))

    def test_owner_can_revoke_obsolete_grant(self):
        from .models import SharedSpaceMembership
        from .services import revoke_personal_transaction_share, share_personal_transaction

        share = share_personal_transaction(
            user=self.alice,
            transaction_id=self.alice_tx.id,
            shared_space_id=self.space.id,
        )

        # Deactivate recipient, rendering the grant obsolete
        SharedSpaceMembership.objects.filter(shared_space=self.space, user=self.bob).update(is_active=False)

        # Owner can still revoke the obsolete grant
        revoked = revoke_personal_transaction_share(user=self.alice, share_id=share.id)
        self.assertEqual(len(revoked), 1)
        share.refresh_from_db()
        self.assertTrue(share.is_revoked)

    def test_shared_space_responses_include_private_and_no_store_cache_directives(self):
        client_bob = Client()
        client_bob.force_login(self.bob)
        resp = client_bob.get(self.shared_space_url)
        self.assertEqual(resp.status_code, 200)

        cache_control = resp.headers.get("Cache-Control", "")
        self.assertIn("private", cache_control.lower())
        self.assertIn("no-store", cache_control.lower())

    def test_offline_shell_remains_account_neutral_and_cacheable(self):
        client = Client()
        resp = client.get("/offline/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Offline")
        cache_control = resp.headers.get("Cache-Control", "")
        # The offline shell must remain cacheable (must NOT have no-store)
        self.assertNotIn("no-store", cache_control.lower())

    def test_sharing_controls_marked_for_online_only_handling(self):
        client_alice = Client()
        client_alice.force_login(self.alice)
        resp = client_alice.get(self.transactions_url)
        self.assertEqual(resp.status_code, 200)

        # Check for online-only markers in HTML
        self.assertContains(resp, 'data-online-only="true"')
        self.assertContains(resp, "sharing-action-btn")
        self.assertContains(resp, "sharing-offline-alert")
        self.assertContains(resp, "sharing-action-form")

    def test_both_members_see_all_active_shares_in_shared_space_and_pagination(self):
        from .models import Transaction, Wallet
        from .selectors import get_shared_personal_activity_for_space
        from .services import (
            create_category,
            create_shared_space,
            create_shared_space_invitation,
            create_transaction_entry,
            share_personal_transaction,
        )

        # Bob creates personal wallet and category
        bob_wallet = Wallet.objects.create(
            owner=self.bob,
            name="Bob Secret Wallet",
            wallet_type=Wallet.WalletType.BANK_ACCOUNT,
            currency=self.usd,
            opening_balance=Decimal("2000.00"),
            opening_balance_date=date(2026, 10, 1),
            ownership_scope=Wallet.OwnershipScope.PERSONAL,
        )
        bob_category = create_category(
            user=self.bob,
            name="Bob Music Gear",
            scope="personal",
        )

        # Alice shares tx1 with Bob (include_notes=False)
        share_personal_transaction(
            user=self.alice,
            transaction_id=self.alice_tx.id,
            shared_space_id=self.space.id,
            include_notes=False,
        )

        # Bob creates tx2 and shares with Alice (include_notes=True)
        bob_tx = create_transaction_entry(
            user=self.bob,
            transaction_type=Transaction.Type.EXPENSE,
            wallet=bob_wallet,
            category=bob_category,
            amount=Decimal("45.00"),
            transaction_date=date(2026, 10, 16),
            description="Bob guitar strings",
            notes="Custom nickel gauge",
            scope=Transaction.Scope.PERSONAL,
        )
        share_personal_transaction(
            user=self.bob,
            transaction_id=bob_tx.id,
            shared_space_id=self.space.id,
            include_notes=True,
        )

        # Alice creates unshared tx3
        alice_unshared = create_transaction_entry(
            user=self.alice,
            transaction_type=Transaction.Type.EXPENSE,
            wallet=self.alice_wallet,
            category=self.alice_category,
            amount=Decimal("25.00"),
            transaction_date=date(2026, 10, 17),
            description="Alice secret lunch",
            notes="Should never leak",
            scope=Transaction.Scope.PERSONAL,
        )

        # Bob creates unshared tx4
        bob_unshared = create_transaction_entry(
            user=self.bob,
            transaction_type=Transaction.Type.EXPENSE,
            wallet=bob_wallet,
            category=bob_category,
            amount=Decimal("60.00"),
            transaction_date=date(2026, 10, 18),
            description="Bob secret gadget",
            notes="Private hobby",
            scope=Transaction.Scope.PERSONAL,
        )

        # 1. Alice views Shared Space
        client_alice = Client()
        client_alice.force_login(self.alice)
        alice_resp = client_alice.get(self.shared_space_url)
        self.assertEqual(alice_resp.status_code, 200)

        # Alice sees her own share labeled "You", notes hidden
        self.assertContains(alice_resp, "Dentist checkup")
        self.assertContains(alice_resp, "-142.50 USD")
        self.assertContains(alice_resp, "You")
        self.assertContains(alice_resp, "Private notes omitted")

        # Alice sees Bob's share labeled "bob_partner", notes visible
        self.assertContains(alice_resp, "Bob guitar strings")
        self.assertContains(alice_resp, "-45.00 USD")
        self.assertContains(alice_resp, "bob_partner")
        self.assertContains(alice_resp, "Custom nickel gauge")

        # Alice does NOT see unshared transactions in this section
        self.assertNotContains(alice_resp, "Alice secret lunch")
        self.assertNotContains(alice_resp, "Bob secret gadget")

        # 2. Bob views Shared Space
        client_bob = Client()
        client_bob.force_login(self.bob)
        bob_resp = client_bob.get(self.shared_space_url)
        self.assertEqual(bob_resp.status_code, 200)

        # Bob sees Alice's share labeled "alice_owner", notes hidden
        self.assertContains(bob_resp, "Dentist checkup")
        self.assertContains(bob_resp, "-142.50 USD")
        self.assertContains(bob_resp, "alice_owner")
        self.assertContains(bob_resp, "Private notes omitted")

        # Bob sees his own share labeled "You", notes visible
        self.assertContains(bob_resp, "Bob guitar strings")
        self.assertContains(bob_resp, "-45.00 USD")
        self.assertContains(bob_resp, "You")
        self.assertContains(bob_resp, "Custom nickel gauge")

        # Bob does NOT see unshared transactions
        self.assertNotContains(bob_resp, "Alice secret lunch")
        self.assertNotContains(bob_resp, "Bob secret gadget")

        # 3. Third-party nonmember isolation (Carol)
        client_carol = Client()
        client_carol.force_login(self.carol)
        carol_resp = client_carol.get(self.shared_space_url)
        self.assertNotContains(carol_resp, "Dentist checkup")
        self.assertNotContains(carol_resp, "Bob guitar strings")

        # 4. Another shared space does not see these shares
        space2 = create_shared_space(user=self.alice, name="Alice & Carol Vacation", base_currency=self.usd)
        page_space2 = get_shared_personal_activity_for_space(self.alice, space2)
        self.assertEqual(len(page_space2.object_list), 0)

        # 5. Pagination contains no duplicate entries
        page1 = get_shared_personal_activity_for_space(self.alice, self.space, page=1, per_page=1)
        self.assertEqual(page1.paginator.num_pages, 2)
        self.assertEqual(len(page1.object_list), 1)

        page2 = get_shared_personal_activity_for_space(self.alice, self.space, page=2, per_page=1)
        self.assertEqual(len(page2.object_list), 1)
        self.assertNotEqual(page1.object_list[0].share_id, page2.object_list[0].share_id)

    def test_shared_activity_view_and_filtering(self):
        from .models import Category, ExchangeRate, PersonalTransactionShare, Transaction, Wallet
        from .services import create_category, create_transaction_entry, share_personal_transaction

        # 1. Unauthenticated request redirects to login
        anon_client = Client()
        anon_resp = anon_client.get(reverse("budgets:shared_activity"))
        self.assertEqual(anon_resp.status_code, 302)
        self.assertTrue(anon_resp.url.startswith(reverse("accounts:login")))

        # 2. Authenticated headers: private, no-cache, no-store, must-revalidate
        client_alice = Client()
        client_alice.force_login(self.alice)
        alice_resp = client_alice.get(reverse("budgets:shared_activity"))
        self.assertEqual(alice_resp.status_code, 200)
        cache_control = alice_resp.headers.get("Cache-Control", "")
        self.assertIn("private", cache_control)
        self.assertIn("no-cache", cache_control)
        self.assertIn("no-store", cache_control)
        self.assertIn("must-revalidate", cache_control)

        # 3. User with no shared space (Carol) sees guidance
        client_carol = Client()
        client_carol.force_login(self.carol)
        carol_resp = client_carol.get(reverse("budgets:shared_activity"))
        self.assertEqual(carol_resp.status_code, 200)
        self.assertContains(carol_resp, "No active shared spaces")

        # 4. Share transactions: Alice shares self.alice_tx (Dentist checkup, EXPENSE, 142.50 USD)
        share_personal_transaction(
            user=self.alice,
            transaction_id=self.alice_tx.id,
            shared_space_id=self.space.id,
            include_notes=False,
        )

        # Bob creates and shares an income transaction
        bob_wallet = Wallet.objects.create(
            owner=self.bob,
            name="Bob Checking",
            wallet_type=Wallet.WalletType.BANK_ACCOUNT,
            currency=self.usd,
            opening_balance=Decimal("1000.00"),
            opening_balance_date=date(2026, 10, 1),
            ownership_scope=Wallet.OwnershipScope.PERSONAL,
        )
        bob_cat = create_category(
            user=self.bob,
            name="Freelance",
            kind=Category.Kind.INCOME,
            scope=Category.Scope.PERSONAL,
        )
        bob_tx = create_transaction_entry(
            user=self.bob,
            transaction_type=Transaction.Type.INCOME,
            wallet=bob_wallet,
            category=bob_cat,
            amount=Decimal("300.00"),
            transaction_date=date(2026, 10, 20),
            description="Web design gig",
            notes="Client paid via ACH",
            scope=Transaction.Scope.PERSONAL,
        )
        share_personal_transaction(
            user=self.bob,
            transaction_id=bob_tx.id,
            shared_space_id=self.space.id,
            include_notes=True,
        )

        # Alice views shared activity: both rows visible
        resp_all = client_alice.get(reverse("budgets:shared_activity") + f"?space={self.space.id}")
        self.assertEqual(resp_all.status_code, 200)
        self.assertContains(resp_all, "Dentist checkup")
        self.assertContains(resp_all, "Web design gig")

        # Filtering by ownership: 'me'
        resp_me = client_alice.get(reverse("budgets:shared_activity") + f"?space={self.space.id}&ownership=me")
        self.assertContains(resp_me, "Dentist checkup")
        self.assertNotContains(resp_me, "Web design gig")

        # Filtering by ownership: 'other'
        resp_other = client_alice.get(reverse("budgets:shared_activity") + f"?space={self.space.id}&ownership=other")
        self.assertNotContains(resp_other, "Dentist checkup")
        self.assertContains(resp_other, "Web design gig")

        # Filtering by type: 'expense'
        resp_exp = client_alice.get(reverse("budgets:shared_activity") + f"?space={self.space.id}&type=expense")
        self.assertContains(resp_exp, "Dentist checkup")
        self.assertNotContains(resp_exp, "Web design gig")

        # Filtering by type: 'income'
        resp_inc = client_alice.get(reverse("budgets:shared_activity") + f"?space={self.space.id}&type=income")
        self.assertNotContains(resp_inc, "Dentist checkup")
        self.assertContains(resp_inc, "Web design gig")

        # Filtering by search query: 'Dentist'
        resp_q = client_alice.get(reverse("budgets:shared_activity") + f"?space={self.space.id}&q=Dentist")
        self.assertContains(resp_q, "Dentist checkup")
        self.assertNotContains(resp_q, "Web design gig")

        # Filtering by currency: 'EUR' (none match)
        resp_curr = client_alice.get(reverse("budgets:shared_activity") + f"?space={self.space.id}&currency=EUR")
        self.assertNotContains(resp_curr, "Dentist checkup")
        self.assertNotContains(resp_curr, "Web design gig")

    def test_personal_balance_disclosure_lifecycle_and_permissions(self):
        from .models import PersonalBalanceSharePermission, Wallet
        from .services import leave_shared_space

        client_alice = Client()
        client_alice.force_login(self.alice)
        client_bob = Client()
        client_bob.force_login(self.bob)

        # 1. Default state: no balances shared
        bob_resp = client_bob.get(reverse("budgets:shared_activity") + f"?space={self.space.id}")
        self.assertEqual(bob_resp.status_code, 200)
        self.assertContains(bob_resp, "This member has not shared a personal balance")

        alice_resp = client_alice.get(reverse("budgets:shared_activity") + f"?space={self.space.id}")
        self.assertEqual(alice_resp.status_code, 200)
        self.assertContains(alice_resp, "You have not shared your personal balance")
        self.assertContains(alice_resp, "Sharing OFF")

        # 2. Sharing & Privacy headers
        resp_privacy = client_alice.get(reverse("budgets:sharing_privacy") + f"?space={self.space.id}")
        self.assertEqual(resp_privacy.status_code, 200)
        cache_control = resp_privacy.headers.get("Cache-Control", "")
        self.assertIn("private", cache_control)
        self.assertIn("no-store", cache_control)

        # 3. Alice enables balance sharing for her wallet
        post_resp = client_alice.post(
            reverse("budgets:sharing_privacy") + f"?space={self.space.id}",
            {
                "action": "save_balance_permission",
                "show_balance": "on",
                "wallet_ids": [str(self.alice_wallet.id)],
            },
        )
        self.assertEqual(post_resp.status_code, 302)

        perm = PersonalBalanceSharePermission.objects.get(
            owner=self.alice,
            shared_space=self.space,
            recipient=self.bob,
        )
        self.assertTrue(perm.show_balance)
        self.assertFalse(perm.is_revoked)
        self.assertEqual(list(perm.selected_wallets.all()), [self.alice_wallet])

        # 4. Bob views Shared Activity: sees Alice's disclosed balance
        # Alice wallet: 5000.00 opening - 142.50 tx = 4857.50 USD
        bob_view = client_bob.get(reverse("budgets:shared_activity") + f"?space={self.space.id}")
        self.assertEqual(bob_view.status_code, 200)
        self.assertContains(bob_view, "4,857.50")
        self.assertContains(bob_view, "USD")
        self.assertContains(bob_view, "Personal balance from selected wallets")
        # Ensure zero leakage of wallet name or private details
        self.assertNotContains(bob_view, "Alice Secret Swiss Vault")
        self.assertNotContains(bob_view, str(self.alice_wallet.id))

        # 5. Alice creates a new second personal wallet (not selected)
        alice_wallet_2 = Wallet.objects.create(
            owner=self.alice,
            name="Alice Offshore Stash",
            wallet_type=Wallet.WalletType.BANK_ACCOUNT,
            currency=self.usd,
            opening_balance=Decimal("25000.00"),
            opening_balance_date=date(2026, 10, 1),
            ownership_scope=Wallet.OwnershipScope.PERSONAL,
        )
        # Bob still sees only 4857.50 USD, NOT 29857.50 USD
        bob_view2 = client_bob.get(reverse("budgets:shared_activity") + f"?space={self.space.id}")
        self.assertContains(bob_view2, "4,857.50")
        self.assertNotContains(bob_view2, "29,857.50")
        self.assertNotContains(bob_view2, "Alice Offshore Stash")

        # 6. Stop sharing immediately revokes disclosure
        stop_resp = client_alice.post(
            reverse("budgets:sharing_privacy") + f"?space={self.space.id}",
            {"action": "stop_sharing_balance"},
        )
        self.assertEqual(stop_resp.status_code, 302)
        perm.refresh_from_db()
        self.assertFalse(perm.show_balance)
        self.assertTrue(perm.is_revoked)

        bob_view3 = client_bob.get(reverse("budgets:shared_activity") + f"?space={self.space.id}")
        self.assertContains(bob_view3, "This member has not shared a personal balance")
        self.assertNotContains(bob_view3, "4,857.50")

        # 7. Leaving shared space revokes permission permanently
        client_alice.post(
            reverse("budgets:sharing_privacy") + f"?space={self.space.id}",
            {
                "action": "save_balance_permission",
                "show_balance": "on",
                "wallet_ids": [str(self.alice_wallet.id)],
            },
        )
        perm = PersonalBalanceSharePermission.objects.get(
            owner=self.alice,
            shared_space=self.space,
            recipient=self.bob,
            is_revoked=False,
        )
        self.assertTrue(perm.show_balance)

        leave_shared_space(user=self.bob, shared_space=self.space)
        perm.refresh_from_db()
        self.assertTrue(perm.is_revoked)
        self.assertFalse(perm.show_balance)

    def test_balance_disclosure_multi_currency_and_conversion_required(self):
        from django.utils import timezone
        from .models import ExchangeRate, PersonalBalanceSharePermission, Wallet
        from .services import save_personal_balance_share_permission

        # Alice creates EUR wallet
        alice_eur_wallet = Wallet.objects.create(
            owner=self.alice,
            name="Alice Euro Pocket",
            wallet_type=Wallet.WalletType.BANK_ACCOUNT,
            currency=self.eur,
            opening_balance=Decimal("200.00"),
            opening_balance_date=date(2026, 10, 1),
            ownership_scope=Wallet.OwnershipScope.PERSONAL,
        )

        # Exchange rate: 1 EUR = 1.10 USD
        ExchangeRate.objects.create(
            from_currency=self.eur,
            to_currency=self.usd,
            rate=Decimal("1.10000000"),
            provider="frankfurter",
            rate_timestamp=timezone.now(),
        )

        # Save permission selecting EUR wallet only
        save_personal_balance_share_permission(
            user=self.alice,
            shared_space_id=str(self.space.id),
            show_balance=True,
            wallet_ids=[str(alice_eur_wallet.id)],
        )

        # Bob's display currency is USD -> Bob sees 200 EUR * 1.10 = 220.00 USD
        client_bob = Client()
        client_bob.force_login(self.bob)
        bob_resp = client_bob.get(reverse("budgets:shared_activity") + f"?space={self.space.id}")
        self.assertEqual(bob_resp.status_code, 200)
        self.assertContains(bob_resp, "220.00")
        self.assertContains(bob_resp, "USD")

        # Now test missing conversion rate: Alice adds a KWD wallet without rate
        from django.db.models import Q
        kwd = Currency.objects.get(code="KWD")
        alice_kwd_wallet = Wallet.objects.create(
            owner=self.alice,
            name="Alice Dinar Travel",
            wallet_type=Wallet.WalletType.CASH,
            currency=kwd,
            opening_balance=Decimal("50.00"),
            opening_balance_date=date(2026, 10, 1),
            ownership_scope=Wallet.OwnershipScope.PERSONAL,
        )
        save_personal_balance_share_permission(
            user=self.alice,
            shared_space_id=str(self.space.id),
            show_balance=True,
            wallet_ids=[str(alice_eur_wallet.id), str(alice_kwd_wallet.id)],
        )
        # Ensure no KWD rates exist
        ExchangeRate.objects.filter(Q(from_currency=kwd) | Q(to_currency=kwd)).delete()

        bob_resp2 = client_bob.get(reverse("budgets:shared_activity") + f"?space={self.space.id}")
        self.assertEqual(bob_resp2.status_code, 200)
        self.assertContains(bob_resp2, "Balance unavailable")
        self.assertContains(bob_resp2, "conversion required")
        # Does not leak raw amounts
        self.assertNotContains(bob_resp2, "50.00")

        # Zero balance disclosure
        alice_zero_wallet = Wallet.objects.create(
            owner=self.alice,
            name="Alice Empty Wallet",
            wallet_type=Wallet.WalletType.CASH,
            currency=self.usd,
            opening_balance=Decimal("0.00"),
            opening_balance_date=date(2026, 10, 1),
            ownership_scope=Wallet.OwnershipScope.PERSONAL,
        )
        save_personal_balance_share_permission(
            user=self.alice,
            shared_space_id=str(self.space.id),
            show_balance=True,
            wallet_ids=[str(alice_zero_wallet.id)],
        )
        bob_resp_zero = client_bob.get(reverse("budgets:shared_activity") + f"?space={self.space.id}")
        self.assertEqual(bob_resp_zero.status_code, 200)
        self.assertContains(bob_resp_zero, "0.00")
        self.assertContains(bob_resp_zero, "USD")

    def test_sharing_privacy_preview(self):
        from .models import PersonalBalanceSharePermission

        client_alice = Client()
        client_alice.force_login(self.alice)

        # Alice previews balance disclosure without saving
        preview_resp = client_alice.post(
            reverse("budgets:sharing_privacy") + f"?space={self.space.id}",
            {
                "action": "preview_balance",
                "show_balance": "on",
                "wallet_ids": [str(self.alice_wallet.id)],
            },
        )
        self.assertEqual(preview_resp.status_code, 200)
        self.assertContains(preview_resp, "Unsaved Preview")
        self.assertContains(preview_resp, "4,857.50")
        self.assertContains(preview_resp, "USD")

        # Crucially: no permission committed to database!
        self.assertEqual(PersonalBalanceSharePermission.objects.filter(owner=self.alice).count(), 0)

    def test_foreign_wallet_and_ineligible_wallet_handling(self):
        from .models import PersonalBalanceSharePermission, Wallet
        from .services import save_personal_balance_share_permission

        # Bob's wallet
        bob_wallet = Wallet.objects.create(
            owner=self.bob,
            name="Bob Secret Wallet",
            wallet_type=Wallet.WalletType.BANK_ACCOUNT,
            currency=self.usd,
            opening_balance=Decimal("1000.00"),
            opening_balance_date=date(2026, 10, 1),
            ownership_scope=Wallet.OwnershipScope.PERSONAL,
        )

        client_alice = Client()
        client_alice.force_login(self.alice)

        # Alice tries to save Bob's wallet ID in wallet_ids
        alice_post = client_alice.post(
            reverse("budgets:sharing_privacy") + f"?space={self.space.id}",
            {
                "action": "save_balance_permission",
                "show_balance": "on",
                "wallet_ids": [str(bob_wallet.id)],
            },
        )
        self.assertEqual(alice_post.status_code, 200)
        self.assertContains(alice_post, "You can only select personal wallets that you own.")
        self.assertEqual(PersonalBalanceSharePermission.objects.filter(owner=self.alice).count(), 0)

        # Now save valid wallet, then archive it
        save_personal_balance_share_permission(
            user=self.alice,
            shared_space_id=str(self.space.id),
            show_balance=True,
            wallet_ids=[str(self.alice_wallet.id)],
        )
        self.alice_wallet.is_archived = True
        self.alice_wallet.save()

        # Alice visits privacy page: shows ineligible wallet warning
        privacy_resp = client_alice.get(reverse("budgets:sharing_privacy") + f"?space={self.space.id}")
        self.assertEqual(privacy_resp.status_code, 200)
        self.assertContains(privacy_resp, "Ineligible wallet selection detected:")
        self.assertContains(privacy_resp, "Alice Secret Swiss Vault")

        # Bob views Shared Activity: archived wallet is excluded -> no active wallets selected
        client_bob = Client()
        client_bob.force_login(self.bob)
        bob_view = client_bob.get(reverse("budgets:shared_activity") + f"?space={self.space.id}")
        self.assertEqual(bob_view.status_code, 200)
        self.assertContains(bob_view, "This member enabled balance sharing, but has no wallets currently selected.")
        self.assertNotContains(bob_view, "4,857.50")

    def test_security_headers_and_export_absence(self):
        client_alice = Client()
        client_alice.force_login(self.alice)

        # Cache-Control on shared_activity
        sa_resp = client_alice.get(reverse("budgets:shared_activity") + f"?space={self.space.id}")
        cc = sa_resp.headers.get("Cache-Control", "")
        self.assertIn("private", cc)
        self.assertIn("no-cache", cc)
        self.assertIn("no-store", cc)
        self.assertIn("must-revalidate", cc)

        # Cache-Control on sharing_privacy
        sp_resp = client_alice.get(reverse("budgets:sharing_privacy") + f"?space={self.space.id}")
        cc_sp = sp_resp.headers.get("Cache-Control", "")
        self.assertIn("private", cc_sp)
        self.assertIn("no-cache", cc_sp)
        self.assertIn("no-store", cc_sp)
        self.assertIn("must-revalidate", cc_sp)

        # Recipient transaction exports must not leak personal balance disclosures
        client_bob = Client()
        client_bob.force_login(self.bob)
        export_resp = client_bob.get(reverse("budgets:export_transactions"))
        self.assertEqual(export_resp.status_code, 200)
        self.assertNotContains(export_resp, "Alice Secret Swiss Vault")

    def test_preview_display_currency_resolution_and_fallback(self):
        from .models import Currency, ExchangeRate, PersonalBalanceSharePermission, Transaction, Wallet
        from django.utils import timezone

        jpy = Currency.objects.get(code="JPY")

        # 1. Recipient has saved display currency distinct from space base and owner
        bob_pref = UserPreference.objects.get(user=self.bob)
        bob_pref.display_currency = jpy
        bob_pref.save()

        # Add provider rate USD -> JPY
        ExchangeRate.objects.create(
            from_currency=self.usd,
            to_currency=jpy,
            rate=Decimal("150.00000000"),
            provider="frankfurter",
            rate_timestamp=timezone.now(),
        )

        pref_count_before = UserPreference.objects.count()
        tx_count_before = Transaction.objects.count()
        wallet_count_before = Wallet.objects.count()

        client_alice = Client()
        client_alice.force_login(self.alice)

        preview_resp = client_alice.post(
            reverse("budgets:sharing_privacy") + f"?space={self.space.id}",
            {
                "action": "preview_balance",
                "show_balance": "on",
                "wallet_ids": [str(self.alice_wallet.id)],
            },
        )
        self.assertEqual(preview_resp.status_code, 200)
        self.assertContains(preview_resp, "Unsaved Preview")
        # 4857.50 USD * 150 = 728625 JPY (quantized to 0 decimal places for JPY)
        self.assertContains(preview_resp, "728,625")
        self.assertContains(preview_resp, "JPY")
        self.assertContains(preview_resp, "displayed value may differ if they use an authorized personal rate")

        # Ensure preview created/modified NO records
        self.assertEqual(UserPreference.objects.count(), pref_count_before)
        self.assertEqual(Transaction.objects.count(), tx_count_before)
        self.assertEqual(Wallet.objects.count(), wallet_count_before)
        self.assertEqual(PersonalBalanceSharePermission.objects.filter(owner=self.alice).count(), 0)

        # 2. Inactive display currency fallback: if recipient's display currency becomes inactive
        jpy.is_active = False
        jpy.save()

        preview_fallback = client_alice.post(
            reverse("budgets:sharing_privacy") + f"?space={self.space.id}",
            {
                "action": "preview_balance",
                "show_balance": "on",
                "wallet_ids": [str(self.alice_wallet.id)],
            },
        )
        self.assertEqual(preview_fallback.status_code, 200)
        # Should fall back to Bob's personal base currency (USD)
        self.assertContains(preview_fallback, "4,857.50")
        self.assertContains(preview_fallback, "USD")
        jpy.is_active = True
        jpy.save()

        # 3. If recipient has no preferences at all: falls back to shared space base currency
        bob_pref.delete()
        self.assertEqual(UserPreference.objects.filter(user=self.bob).count(), 0)

        preview_no_pref = client_alice.post(
            reverse("budgets:sharing_privacy") + f"?space={self.space.id}",
            {
                "action": "preview_balance",
                "show_balance": "on",
                "wallet_ids": [str(self.alice_wallet.id)],
            },
        )
        self.assertEqual(preview_no_pref.status_code, 200)
        self.assertContains(preview_no_pref, "4,857.50")
        self.assertContains(preview_no_pref, "USD")
        # Ensure NO UserPreference was created for Bob
        self.assertEqual(UserPreference.objects.filter(user=self.bob).count(), 0)

    def test_prevent_private_manual_rate_disclosure_through_preview(self):
        from .models import Currency, ExchangeRate, PersonalBalanceSharePermission, Wallet
        from .services import save_personal_balance_share_permission
        from django.db.models import Q
        from django.utils import timezone

        # Ensure Bob has preference in USD
        bob_pref, _ = UserPreference.objects.get_or_create(user=self.bob)
        bob_pref.display_currency = self.usd
        bob_pref.personal_base_currency = self.usd
        bob_pref.save()

        # Alice creates a EUR personal wallet with 100 EUR
        alice_eur_wallet = Wallet.objects.create(
            owner=self.alice,
            name="Alice Euro Holding",
            wallet_type=Wallet.WalletType.BANK_ACCOUNT,
            currency=self.eur,
            opening_balance=Decimal("100.00"),
            opening_balance_date=date(2026, 10, 1),
            ownership_scope=Wallet.OwnershipScope.PERSONAL,
        )

        # Clear existing EUR <-> USD rates
        ExchangeRate.objects.filter(
            Q(from_currency=self.eur, to_currency=self.usd) |
            Q(from_currency=self.usd, to_currency=self.eur)
        ).delete()

        # Bob creates a PRIVATE manual rate: 1 EUR = 2.50 USD
        ExchangeRate.objects.create(
            from_currency=self.eur,
            to_currency=self.usd,
            rate=Decimal("2.50000000"),
            provider="manual",
            is_manual=True,
            scope=ExchangeRate.Scope.PERSONAL,
            created_by=self.bob,
            rate_timestamp=timezone.now(),
        )

        # Alice requests preview for her EUR wallet:
        client_alice = Client()
        client_alice.force_login(self.alice)

        preview_resp = client_alice.post(
            reverse("budgets:sharing_privacy") + f"?space={self.space.id}",
            {
                "action": "preview_balance",
                "show_balance": "on",
                "wallet_ids": [str(alice_eur_wallet.id)],
            },
        )
        self.assertEqual(preview_resp.status_code, 200)
        # MUST NOT use Bob's private manual rate (2.50)!
        self.assertNotContains(preview_resp, "250.00")
        self.assertNotContains(preview_resp, "2.50")
        # Must show unavailable state
        self.assertContains(preview_resp, "Balance unavailable")

        # Now create a SHARED space manual rate: 1 EUR = 1.25 USD
        ExchangeRate.objects.create(
            from_currency=self.eur,
            to_currency=self.usd,
            rate=Decimal("1.25000000"),
            provider="manual",
            is_manual=True,
            scope=ExchangeRate.Scope.SHARED,
            shared_space=self.space,
            created_by=self.alice,
            rate_timestamp=timezone.now(),
        )

        # Alice previews again: shared space rate is permitted!
        preview_resp2 = client_alice.post(
            reverse("budgets:sharing_privacy") + f"?space={self.space.id}",
            {
                "action": "preview_balance",
                "show_balance": "on",
                "wallet_ids": [str(alice_eur_wallet.id)],
            },
        )
        self.assertEqual(preview_resp2.status_code, 200)
        self.assertContains(preview_resp2, "125.00")
        self.assertContains(preview_resp2, "USD")

        # Save the permission
        save_personal_balance_share_permission(
            user=self.alice,
            shared_space_id=str(self.space.id),
            show_balance=True,
            wallet_ids=[str(alice_eur_wallet.id)],
        )

        # On actual Shared Activity page: Bob (recipient) views Alice's disclosure
        # Bob CAN use Bob's own authorized personal rate (2.50) which takes priority for Bob
        client_bob = Client()
        client_bob.force_login(self.bob)
        bob_resp = client_bob.get(reverse("budgets:shared_activity") + f"?space={self.space.id}")
        self.assertEqual(bob_resp.status_code, 200)
        self.assertContains(bob_resp, "250.00")
        self.assertContains(bob_resp, "USD")

        # Alice viewing her own disclosed balance does NOT get Bob's private rate
        alice_sa_resp = client_alice.get(reverse("budgets:shared_activity") + f"?space={self.space.id}")
        self.assertEqual(alice_sa_resp.status_code, 200)
        self.assertContains(alice_sa_resp, "125.00")
        self.assertNotContains(alice_sa_resp, "250.00")

    def test_fresh_stale_mixed_and_same_currency_rate_freshness(self):
        from .models import Currency, ExchangeRate, Wallet
        from .services import save_personal_balance_share_permission
        from datetime import timedelta
        from django.db.models import Q
        from django.utils import timezone

        # Alice has USD wallet, EUR wallet, and PHP wallet
        alice_eur_wallet = Wallet.objects.create(
            owner=self.alice,
            name="Alice Euro Fresh",
            wallet_type=Wallet.WalletType.BANK_ACCOUNT,
            currency=self.eur,
            opening_balance=Decimal("100.00"),
            opening_balance_date=date(2026, 10, 1),
            ownership_scope=Wallet.OwnershipScope.PERSONAL,
        )

        alice_php_wallet = Wallet.objects.create(
            owner=self.alice,
            name="Alice Peso Stale",
            wallet_type=Wallet.WalletType.CASH,
            currency=self.php,
            opening_balance=Decimal("5000.00"),
            opening_balance_date=date(2026, 10, 1),
            ownership_scope=Wallet.OwnershipScope.PERSONAL,
        )

        client_alice = Client()
        client_alice.force_login(self.alice)
        client_bob = Client()
        client_bob.force_login(self.bob)

        # 1. Fresh rate: rate_timestamp is recent (now)
        ExchangeRate.objects.filter(
            Q(from_currency=self.eur, to_currency=self.usd) |
            Q(from_currency=self.usd, to_currency=self.eur)
        ).delete()
        ExchangeRate.objects.create(
            from_currency=self.eur,
            to_currency=self.usd,
            rate=Decimal("1.10000000"),
            provider="frankfurter",
            rate_timestamp=timezone.now(),
        )

        # Save EUR wallet only
        save_personal_balance_share_permission(
            user=self.alice,
            shared_space_id=str(self.space.id),
            show_balance=True,
            wallet_ids=[str(alice_eur_wallet.id)],
        )

        resp_fresh = client_bob.get(reverse("budgets:shared_activity") + f"?space={self.space.id}")
        self.assertEqual(resp_fresh.status_code, 200)
        self.assertContains(resp_fresh, "110.00")
        self.assertNotContains(resp_fresh, "Uses stale exchange rates")

        # Preview also fresh
        prev_fresh = client_alice.post(
            reverse("budgets:sharing_privacy") + f"?space={self.space.id}",
            {
                "action": "preview_balance",
                "show_balance": "on",
                "wallet_ids": [str(alice_eur_wallet.id)],
            },
        )
        self.assertEqual(prev_fresh.status_code, 200)
        self.assertContains(prev_fresh, "110.00")
        self.assertNotContains(prev_fresh, "Uses stale exchange rates")

        # 2. Stale rate: rate_timestamp is 5 days old (> 48 hours)
        ExchangeRate.objects.filter(
            Q(from_currency=self.eur, to_currency=self.usd) |
            Q(from_currency=self.usd, to_currency=self.eur)
        ).delete()
        ExchangeRate.objects.create(
            from_currency=self.eur,
            to_currency=self.usd,
            rate=Decimal("1.10000000"),
            provider="frankfurter",
            rate_timestamp=timezone.now() - timedelta(days=5),
        )

        resp_stale = client_bob.get(reverse("budgets:shared_activity") + f"?space={self.space.id}")
        self.assertEqual(resp_stale.status_code, 200)
        self.assertContains(resp_stale, "110.00")
        self.assertContains(resp_stale, "Uses stale exchange rates")

        prev_stale = client_alice.post(
            reverse("budgets:sharing_privacy") + f"?space={self.space.id}",
            {
                "action": "preview_balance",
                "show_balance": "on",
                "wallet_ids": [str(alice_eur_wallet.id)],
            },
        )
        self.assertEqual(prev_stale.status_code, 200)
        self.assertContains(prev_stale, "110.00")
        self.assertContains(prev_stale, "Uses stale exchange rates")

        # 3. Mixed freshness: EUR is fresh, PHP is stale -> combined balance must be marked STALE
        ExchangeRate.objects.filter(
            Q(from_currency=self.eur, to_currency=self.usd) |
            Q(from_currency=self.usd, to_currency=self.eur)
        ).delete()
        ExchangeRate.objects.create(
            from_currency=self.eur,
            to_currency=self.usd,
            rate=Decimal("1.10000000"),
            provider="frankfurter",
            rate_timestamp=timezone.now(),  # Fresh!
        )

        ExchangeRate.objects.filter(
            Q(from_currency=self.php, to_currency=self.usd) |
            Q(from_currency=self.usd, to_currency=self.php)
        ).delete()
        ExchangeRate.objects.create(
            from_currency=self.php,
            to_currency=self.usd,
            rate=Decimal("0.02000000"),
            provider="frankfurter",
            rate_timestamp=timezone.now() - timedelta(days=5),  # Stale!
        )

        save_personal_balance_share_permission(
            user=self.alice,
            shared_space_id=str(self.space.id),
            show_balance=True,
            wallet_ids=[str(alice_eur_wallet.id), str(alice_php_wallet.id)],
        )

        # 100 EUR * 1.10 = 110.00 USD; 5000 PHP * 0.02 = 100.00 USD. Total = 210.00 USD
        resp_mixed = client_bob.get(reverse("budgets:shared_activity") + f"?space={self.space.id}")
        self.assertEqual(resp_mixed.status_code, 200)
        self.assertContains(resp_mixed, "210.00")
        self.assertContains(resp_mixed, "Uses stale exchange rates")

        # 4. Same currency wallet: requires no rate and must NOT be marked stale
        save_personal_balance_share_permission(
            user=self.alice,
            shared_space_id=str(self.space.id),
            show_balance=True,
            wallet_ids=[str(self.alice_wallet.id)],  # USD wallet into USD display
        )
        resp_same = client_bob.get(reverse("budgets:shared_activity") + f"?space={self.space.id}")
        self.assertEqual(resp_same.status_code, 200)
        self.assertContains(resp_same, "4,857.50")
        self.assertNotContains(resp_same, "Uses stale exchange rates")







class OfflineSyncBrowserRuntimeTests(TestCase):
    """
    Executes static/js/offline.js inside a real headless Chromium browser
    (Chrome or Edge) to verify multi-batch queue sync (100, 101, 250 items),
    legacy queue migration, dependency blocking, lost-response idempotent
    retries, concurrent tab locks, in-flight local edits/deletes (Scenarios A-F
    including second upload and queue clearing), snapshot refresh failures,
    and stale-tab account-switch/logout invalidation without oscillation.
    """

    @staticmethod
    def _find_browser_binary():
        import os
        import shutil

        candidates = [
            shutil.which("chrome"),
            shutil.which("msedge"),
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        ]
        for c in candidates:
            if c and os.path.exists(c):
                return c
        return None

    def test_offline_js_multi_batch_and_stale_tab_isolation_in_headless_browser(self):
        import http.server
        import json
        import pathlib
        import shutil
        import socket
        import subprocess
        import tempfile
        import threading
        from django.conf import settings

        browser_bin = self._find_browser_binary()
        if not browser_bin:
            self.skipTest("No Chromium browser binary found for headless JS verification.")

        offline_js_path = pathlib.Path(settings.BASE_DIR) / "static" / "js" / "offline.js"
        offline_js_code = offline_js_path.read_text(encoding="utf-8")

        results_holder = {"last_scenario": "startup"}
        done_event = threading.Event()

        harness_html = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Offline JS Verification</title></head>
<body
    class="app-body"
    data-authenticated-user-id="1"
    data-authenticated-username="alice"
    data-sync-state-url="/api/sync/state/"
    data-sync-mutations-url="/api/sync/mutations/"
    data-disable-auto-sync-on-load="true"
>
    <header>
        <span id="offline-queue-badge" class="d-none"></span>
        <form id="logout-form"><button type="submit">Log out</button></form>
    </header>
    <main id="main-content">
        <div class="container-fluid">
            <div id="offline-sync-banner" class="d-none"><span id="offline-sync-banner-text"></span></div>
            <div id="offline-queue-review-panel" class="d-none"></div>
            <div id="private-wallet-card" class="dashboard-panel">$1,234.56 Alice Private Balance</div>
        </div>
    </main>
    <script src="/static/js/offline.js"></script>
    <script src="/test-runner.js"></script>
</body>
</html>
"""

        runner_js = r"""
(async function () {
    var origFetch = window.fetch.bind(window);
    var report = { assertions: {}, errors: [], last_scenario: "init" };

    window.addEventListener("error", function (ev) {
        report.errors.push("window.onerror (" + report.last_scenario + "): " + String(ev && ev.message ? ev.message : ev));
    });
    window.addEventListener("unhandledrejection", function (ev) {
        var reason = ev && ev.reason;
        report.errors.push("unhandledrejection (" + report.last_scenario + "): " + String(reason && reason.stack ? reason.stack : reason));
    });

    async function setScenario(name) {
        report.last_scenario = name;
        try {
            await origFetch("/__test_progress__", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ scenario: name })
            });
        } catch (e) {}
    }

    function withTimeout(promise, ms, label) {
        return new Promise(function (resolve, reject) {
            var timer = setTimeout(function () {
                reject(new Error("Step timed out after " + ms + "ms: " + label + " (scenario: " + report.last_scenario + ")"));
            }, ms);
            Promise.resolve(promise).then(
                function (val) { clearTimeout(timer); resolve(val); },
                function (err) { clearTimeout(timer); reject(err); }
            );
        });
    }

    function assertEq(name, actual, expected) {
        var ok = JSON.stringify(actual) === JSON.stringify(expected);
        report.assertions[name] = { ok: ok, actual: actual, expected: expected };
        if (!ok) {
            report.errors.push("[" + report.last_scenario + "] " + name + ": expected " + JSON.stringify(expected) + " got " + JSON.stringify(actual));
        }
    }

    function stableStringify(val) {
        if (val === null || val === undefined || typeof val !== "object") {
            return JSON.stringify(val);
        }
        if (Array.isArray(val)) {
            return "[" + val.map(stableStringify).join(",") + "]";
        }
        var keys = Object.keys(val).sort();
        return "{" + keys.map(function (k) {
            return JSON.stringify(k) + ":" + stableStringify(val[k]);
        }).join(",") + "}";
    }

    function computeRequestFingerprint(m) {
        return stableStringify({
            operation: String(m.operation || ""),
            entity_id: String(m.entity_id || ""),
            base_revision: m.base_revision === undefined ? null : m.base_revision,
            payload: m.payload || {}
        });
    }

    try {
        await setScenario("bootstrap");
        var api = window.BudgetOfflineSync;
        assertEq("max_batch_size_constant", api.MAX_SYNC_BATCH_SIZE, 100);
        localStorage.clear();
        document.body.setAttribute("data-authenticated-user-id", "1");
        document.body.setAttribute("data-authenticated-username", "alice");
        api.initializePageIdentity();

        // Controllable server state matching Django's process_sync_batch idempotency and revision rules
        var serverCommittedReceipts = {};
        var serverEntities = {};
        var reusedMutationIdErrors = 0;
        var batchCalls = [];
        var stateCalls = [];
        var currentServerUserId = 1;
        var currentServerUsername = "alice";
        var simulateLostResponseOnce = false;
        var simulateSnapshotFailure = false;
        var inFlightUploadHook = null;

        window.fetch = async function (url, opts) {
            var method = (opts && opts.method) || "GET";
            if (url.indexOf("/api/sync/state/") !== -1) {
                stateCalls.push({ url: url, method: method });
                if (simulateSnapshotFailure && url.indexOf("limit=1") === -1) {
                    return {
                        ok: false,
                        status: 503,
                        headers: { get: function () { return "application/json"; } },
                        json: async function () { return { error: "Snapshot service unavailable" }; }
                    };
                }
                var txList = Object.keys(serverEntities).map(function (eid) {
                    var ent = serverEntities[eid];
                    return {
                        id: ent.id,
                        transaction_date: "2026-10-20",
                        description: ent.description || "",
                        transaction_type: "expense",
                        amount: ent.amount || "0.00",
                        currency_code: "USD",
                        wallet_id: "w-1",
                        wallet_name: "Alice Cash",
                        scope: "personal",
                        revision: ent.revision,
                        is_deleted: Boolean(ent.is_deleted)
                    };
                });
                return {
                    ok: true,
                    status: 200,
                    headers: { get: function () { return "application/json"; } },
                    json: async function () {
                        return {
                            user_id: currentServerUserId,
                            username: currentServerUsername,
                            wallets: [{ id: "w-1", name: "Alice Cash", currency_code: "USD", decimal_places: 2, ownership_scope: "personal", current_balance: "500.00", revision: 1, is_archived: false }],
                            categories: [],
                            transactions: txList,
                            shared_spaces: [],
                            last_sync_at: "2026-10-25T12:00:00Z"
                        };
                    }
                };
            }

            if (url.indexOf("/api/sync/mutations/") !== -1) {
                var body = JSON.parse(opts.body || "{}");
                batchCalls.push({
                    expected_user_id: body.expected_user_id,
                    is_final_batch: body.is_final_batch,
                    count: (body.mutations || []).length,
                    seqs: (body.mutations || []).map(function (m) { return m.queue_seq; }),
                    ids: (body.mutations || []).map(function (m) { return m.mutation_id; }),
                    ops: (body.mutations || []).map(function (m) { return m.operation; })
                });

                if (body.expected_user_id !== currentServerUserId) {
                    return {
                        ok: false,
                        status: 409,
                        headers: { get: function () { return "application/json"; } },
                        json: async function () {
                            return { batch_status: "user_mismatch", error: "User mismatch" };
                        }
                    };
                }

                if ((body.mutations || []).length > 100) {
                    return {
                        ok: false,
                        status: 400,
                        headers: { get: function () { return "application/json"; } },
                        json: async function () {
                            return { batch_status: "failed", error: "Exceeds 100 items" };
                        }
                    };
                }

                if (inFlightUploadHook) {
                    var hook = inFlightUploadHook;
                    inFlightUploadHook = null;
                    await hook(body.mutations);
                }

                var failedEntitiesInBatch = {};
                var results = [];
                var appliedCount = 0;
                var errorCount = 0;
                var conflictCount = 0;
                var blockedCount = 0;

                (body.mutations || []).forEach(function (m) {
                    var eid = String(m.entity_id || "");
                    var fp = computeRequestFingerprint(m);

                    if (failedEntitiesInBatch[eid]) {
                        blockedCount += 1;
                        errorCount += 1;
                        results.push({
                            mutation_id: m.mutation_id,
                            entity_id: eid,
                            status: "blocked_by_dependency",
                            error: "Blocked by earlier failure on same entity"
                        });
                        return;
                    }

                    // Strict server idempotency check: reject reused mutation_id if fingerprint differs
                    if (serverCommittedReceipts[m.mutation_id]) {
                        var existingReceipt = serverCommittedReceipts[m.mutation_id];
                        if (existingReceipt.fingerprint !== fp) {
                            reusedMutationIdErrors += 1;
                            errorCount += 1;
                            failedEntitiesInBatch[eid] = true;
                            results.push({
                                mutation_id: m.mutation_id,
                                entity_id: eid,
                                status: "error",
                                code: "mutation_id_reused",
                                error: "This mutation_id was already committed with a different payload."
                            });
                            return;
                        }
                        appliedCount += 1;
                        var cached = Object.assign({}, existingReceipt.response, { idempotent_replay: true });
                        results.push(cached);
                        return;
                    }

                    if (m.payload && m.payload.force_conflict) {
                        conflictCount += 1;
                        failedEntitiesInBatch[eid] = true;
                        results.push({
                            mutation_id: m.mutation_id,
                            entity_id: eid,
                            status: "conflict",
                            error: "Revision conflict",
                            server_revision: 5
                        });
                        return;
                    }

                    var op = String(m.operation || "");
                    var existingEntity = serverEntities[eid];

                    if (op === "create_transaction") {
                        if (existingEntity && !existingEntity.is_deleted) {
                            conflictCount += 1;
                            failedEntitiesInBatch[eid] = true;
                            results.push({
                                mutation_id: m.mutation_id,
                                entity_id: eid,
                                status: "conflict",
                                error: "Transaction already exists",
                                server_revision: existingEntity.revision
                            });
                            return;
                        }
                        serverEntities[eid] = {
                            id: eid,
                            amount: (m.payload && m.payload.amount) || "0.00",
                            description: (m.payload && m.payload.description) || "",
                            revision: 1,
                            is_deleted: false
                        };
                    } else if (op === "update_transaction") {
                        if (!existingEntity || existingEntity.is_deleted) {
                            errorCount += 1;
                            failedEntitiesInBatch[eid] = true;
                            results.push({
                                mutation_id: m.mutation_id,
                                entity_id: eid,
                                status: "error",
                                code: "not_found",
                                error: "Transaction not found"
                            });
                            return;
                        }
                        if (Number(m.base_revision) !== Number(existingEntity.revision)) {
                            conflictCount += 1;
                            failedEntitiesInBatch[eid] = true;
                            results.push({
                                mutation_id: m.mutation_id,
                                entity_id: eid,
                                status: "conflict",
                                error: "Revision conflict",
                                server_revision: existingEntity.revision
                            });
                            return;
                        }
                        existingEntity.amount = (m.payload && m.payload.amount) || existingEntity.amount;
                        if (m.payload && m.payload.description !== undefined) {
                            existingEntity.description = m.payload.description;
                        }
                        existingEntity.revision += 1;
                    } else if (op === "delete_transaction") {
                        if (!existingEntity) {
                            errorCount += 1;
                            failedEntitiesInBatch[eid] = true;
                            results.push({
                                mutation_id: m.mutation_id,
                                entity_id: eid,
                                status: "error",
                                code: "not_found",
                                error: "Transaction not found"
                            });
                            return;
                        }
                        if (Number(m.base_revision) !== Number(existingEntity.revision)) {
                            conflictCount += 1;
                            failedEntitiesInBatch[eid] = true;
                            results.push({
                                mutation_id: m.mutation_id,
                                entity_id: eid,
                                status: "conflict",
                                error: "Revision conflict on delete",
                                server_revision: existingEntity.revision
                            });
                            return;
                        }
                        existingEntity.is_deleted = true;
                        existingEntity.revision += 1;
                    }

                    var out = {
                        mutation_id: m.mutation_id,
                        entity_id: eid,
                        status: "applied",
                        revision: serverEntities[eid].revision
                    };
                    serverCommittedReceipts[m.mutation_id] = {
                        fingerprint: fp,
                        response: out
                    };
                    appliedCount += 1;
                    results.push(out);
                });

                if (simulateLostResponseOnce) {
                    simulateLostResponseOnce = false;
                    throw new Error("Simulated lost network response after server commit");
                }

                var bStatus = (errorCount === 0 && conflictCount === 0 && blockedCount === 0)
                    ? "success"
                    : (appliedCount > 0 ? "partial" : "failed");

                return {
                    ok: true,
                    status: 200,
                    headers: { get: function () { return "application/json"; } },
                    json: async function () {
                        return {
                            batch_status: bStatus,
                            applied_count: appliedCount,
                            error_count: errorCount,
                            conflict_count: conflictCount,
                            blocked_count: blockedCount,
                            results: results
                        };
                    }
                };
            }
            throw new Error("Unexpected URL: " + url);
        };

        // SCENARIO 1: 100, 101, and 250 item queues with strict queue_seq ordering
        await setScenario("scenario_1_multi_batch_100_101_250");
        for (var i = 1; i <= 100; i++) {
            await withTimeout(api.saveMutationToQueue("1", {
                mutation_id: "m100-" + String(1000 - i) + "-" + i,
                operation: "create_transaction",
                entity_id: "ent-100-" + i,
                base_revision: null,
                sync_status: "pending",
                queued_at: "2026-10-20T10:00:00Z",
                payload: { amount: String(i) + ".00", transaction_type: "expense", wallet_id: "w-1", transaction_date: "2026-10-20", scope: "personal" }
            }), 5000, "save_100_" + i);
        }
        batchCalls = [];
        var res100 = await withTimeout(api.flushMutationQueue(), 8000, "flush_100");
        assertEq("queue_100_status", res100.status, "success");
        assertEq("queue_100_applied", res100.applied_count, 100);
        assertEq("queue_100_batches", batchCalls.length, 1);
        assertEq("queue_100_first_seq_order", batchCalls[0].ids[0], "m100-999-1");
        assertEq("queue_100_last_seq_order", batchCalls[0].ids[99], "m100-900-100");
        assertEq("queue_100_remaining", (await withTimeout(api.getQueuedMutations("1"), 5000, "get_100")).length, 0);

        // Queue of 101 items
        for (var j = 1; j <= 101; j++) {
            await withTimeout(api.saveMutationToQueue("1", {
                mutation_id: "m101-" + j,
                operation: "create_transaction",
                entity_id: "ent-101-" + j,
                base_revision: null,
                sync_status: "pending",
                queued_at: "2026-10-20T11:00:00Z",
                payload: { amount: "5.00", transaction_type: "expense", wallet_id: "w-1", transaction_date: "2026-10-20", scope: "personal" }
            }), 5000, "save_101_" + j);
        }
        batchCalls = [];
        var res101 = await withTimeout(api.flushMutationQueue(), 8000, "flush_101");
        assertEq("queue_101_status", res101.status, "success");
        assertEq("queue_101_applied", res101.applied_count, 101);
        assertEq("queue_101_batches_count", batchCalls.length, 2);
        assertEq("queue_101_batch_sizes", [batchCalls[0].count, batchCalls[1].count], [100, 1]);
        assertEq("queue_101_final_flags", [batchCalls[0].is_final_batch, batchCalls[1].is_final_batch], [false, true]);

        // Queue of 250 items
        var progressEvents = [];
        for (var k = 1; k <= 250; k++) {
            await withTimeout(api.saveMutationToQueue("1", {
                mutation_id: "m250-" + k,
                operation: "create_transaction",
                entity_id: "ent-250-" + k,
                base_revision: null,
                sync_status: "pending",
                queued_at: "2026-10-20T12:00:00Z",
                payload: { amount: "1.00", transaction_type: "income", wallet_id: "w-1", transaction_date: "2026-10-20", scope: "personal" }
            }), 5000, "save_250_" + k);
        }
        batchCalls = [];
        var res250 = await withTimeout(api.flushMutationQueue({
            onProgress: function (p) { progressEvents.push(p.batch_index); }
        }), 10000, "flush_250");
        assertEq("queue_250_status", res250.status, "success");
        assertEq("queue_250_applied", res250.applied_count, 250);
        assertEq("queue_250_batch_sizes", batchCalls.map(function (b) { return b.count; }), [100, 100, 50]);
        assertEq("queue_250_progress_events", progressEvents, [1, 2, 3]);
        assertEq("queue_250_remaining", (await withTimeout(api.getQueuedMutations("1"), 5000, "get_250")).length, 0);

        // SCENARIO 2: Lost response after server commit -> idempotent retry with stable mutation_id
        await setScenario("scenario_2_lost_response_retry");
        await withTimeout(api.saveMutationToQueue("1", {
            mutation_id: "m-lost-1",
            operation: "create_transaction",
            entity_id: "ent-lost-1",
            base_revision: null,
            sync_status: "pending",
            queued_at: "2026-10-20T13:00:00Z",
            payload: { amount: "42.00", transaction_type: "expense", wallet_id: "w-1", transaction_date: "2026-10-20", scope: "personal" }
        }), 5000, "save_lost_1");
        simulateLostResponseOnce = true;
        var lostAttempt = await withTimeout(api.flushMutationQueue(), 5000, "flush_lost_attempt");
        assertEq("lost_response_first_status", lostAttempt.status, "failed");
        var stillQueued = await withTimeout(api.getQueuedMutations("1"), 5000, "get_after_lost");
        assertEq("lost_response_preserved_in_queue", stillQueued.length, 1);
        assertEq("lost_response_stable_mutation_id", stillQueued[0].mutation_id, "m-lost-1");

        var retryLostAttempt = await withTimeout(api.flushMutationQueue(), 5000, "flush_retry_lost");
        assertEq("lost_response_retry_status", retryLostAttempt.status, "success");
        assertEq("lost_response_retry_replay", Boolean(retryLostAttempt.results[0].idempotent_replay), true);
        assertEq("lost_response_queue_empty_after_retry", (await withTimeout(api.getQueuedMutations("1"), 5000, "get_after_retry_lost")).length, 0);

        // SCENARIO 3: Partial failure + dependent mutation stopping + no infinite retry loop
        await setScenario("scenario_3_partial_failure_dependency_blocking");
        serverEntities["ent-dep-A"] = { id: "ent-dep-A", amount: "5.00", description: "Seed", revision: 5, is_deleted: false };
        await withTimeout(api.saveMutationToQueue("1", {
            mutation_id: "m-dep-1-conflict",
            operation: "update_transaction",
            entity_id: "ent-dep-A",
            base_revision: 1, // Stale revision (server is at revision 5) -> conflict
            sync_status: "pending",
            queued_at: "2026-10-20T14:00:00Z",
            payload: { amount: "10.00" }
        }), 5000, "save_dep_1");
        await withTimeout(api.saveMutationToQueue("1", {
            mutation_id: "m-dep-2-delete",
            operation: "delete_transaction",
            entity_id: "ent-dep-A",
            base_revision: 5,
            sync_status: "pending",
            queued_at: "2026-10-20T14:01:00Z",
            payload: {}
        }), 5000, "save_dep_2");
        await withTimeout(api.saveMutationToQueue("1", {
            mutation_id: "m-indep-3-ok",
            operation: "create_transaction",
            entity_id: "ent-indep-B",
            base_revision: null,
            sync_status: "pending",
            queued_at: "2026-10-20T14:02:00Z",
            payload: { amount: "15.00" }
        }), 5000, "save_indep_3");
        batchCalls = [];
        var partialRes = await withTimeout(api.flushMutationQueue(), 5000, "flush_partial");
        assertEq("partial_sync_status", partialRes.status, "partial");
        assertEq("partial_sync_applied", partialRes.applied_count, 1);
        assertEq("partial_sync_conflicts", partialRes.conflict_count, 1);
        assertEq("partial_sync_blocked", partialRes.blocked_count, 1);

        var remainingAfterPartial = await withTimeout(api.getQueuedMutations("1"), 5000, "get_remaining_partial");
        assertEq("partial_remaining_count", remainingAfterPartial.length, 2);
        assertEq("partial_remaining_statuses", remainingAfterPartial.map(function (x) { return x.sync_status; }), ["conflict", "blocked_by_dependency"]);

        // Calling automatic flushMutationQueue() again must NOT send any network batch (no infinite retry loop)
        batchCalls = [];
        var autoRetryRes = await withTimeout(api.flushMutationQueue(), 5000, "flush_no_infinite_retry");
        assertEq("no_infinite_retry_batches_sent", batchCalls.length, 0);
        assertEq("no_infinite_retry_status", autoRetryRes.status, "failed");

        await withTimeout(api.removeMutationFromQueue("1", "m-dep-1-conflict"), 5000, "rm_dep_1");
        await withTimeout(api.removeMutationFromQueue("1", "m-dep-2-delete"), 5000, "rm_dep_2");

        // SCENARIO 4A: Create -> upload starts -> edit locally -> creation acknowledged -> synchronize again
        await setScenario("scenario_4A_create_inflight_edit_and_second_sync");
        await withTimeout(api.saveMutationToQueue("1", {
            mutation_id: "m-inflight-create-A",
            operation: "create_transaction",
            entity_id: "ent-inflight-A",
            base_revision: null,
            sync_status: "pending",
            queued_at: "2026-10-20T15:00:00Z",
            payload: { amount: "10.00", description: "Original A" }
        }), 5000, "save_inflight_create_A");
        var origSeqA = (await withTimeout(api.getQueuedMutations("1"), 5000, "get_seq_A"))[0].queue_seq;

        inFlightUploadHook = async function () {
            await withTimeout(api.saveMutationToQueue("1", {
                mutation_id: "m-inflight-create-A",
                operation: "create_transaction",
                entity_id: "ent-inflight-A",
                base_revision: null,
                sync_status: "pending",
                queued_at: "2026-10-20T15:00:00Z",
                payload: { amount: "99.95", description: "Edited A locally during upload" }
            }), 5000, "save_inflight_edit_A");
        };
        var inflightResA1 = await withTimeout(api.flushMutationQueue(), 5000, "flush_inflight_A1");
        assertEq("inflight_A1_honest_partial_status", inflightResA1.status, "partial");
        assertEq("inflight_A1_has_pending_followup", inflightResA1.has_pending_followup, true);
        assertEq("inflight_A1_pending_count", inflightResA1.pending_count, 1);

        var postInflightQueueA = await withTimeout(api.getQueuedMutations("1"), 5000, "get_post_inflight_A");
        assertEq("inflight_A_followup_queued_count", postInflightQueueA.length, 1);
        assertEq("inflight_A_new_mutation_id_assigned", postInflightQueueA[0].mutation_id !== "m-inflight-create-A", true);
        assertEq("inflight_A_same_entity_id", postInflightQueueA[0].entity_id, "ent-inflight-A");
        assertEq("inflight_A_preserved_queue_seq", postInflightQueueA[0].queue_seq, origSeqA);
        assertEq("inflight_A_transitioned_to_update", postInflightQueueA[0].operation, "update_transaction");
        assertEq("inflight_A_base_revision_is_1", postInflightQueueA[0].base_revision, 1);
        assertEq("inflight_A_payload_amount", postInflightQueueA[0].payload.amount, "99.95");

        // Second sync uploads the replacement mutation and clears the queue
        var inflightResA2 = await withTimeout(api.flushMutationQueue(), 5000, "flush_inflight_A2");
        assertEq("inflight_A2_second_sync_status", inflightResA2.status, "success");
        assertEq("inflight_A2_second_sync_applied", inflightResA2.applied_count, 1);
        assertEq("inflight_A2_queue_cleared", (await withTimeout(api.getQueuedMutations("1"), 5000, "get_A2_cleared")).length, 0);
        assertEq("inflight_A2_server_entity_amount", serverEntities["ent-inflight-A"].amount, "99.95");
        assertEq("inflight_A2_server_entity_desc", serverEntities["ent-inflight-A"].description, "Edited A locally during upload");
        assertEq("inflight_A2_server_entity_rev", serverEntities["ent-inflight-A"].revision, 2);

        // SCENARIO 4B: Update -> upload starts -> edit again -> acknowledge -> synchronize again
        await setScenario("scenario_4B_update_inflight_edit_and_second_sync");
        await withTimeout(api.saveMutationToQueue("1", {
            mutation_id: "m-inflight-update-B",
            operation: "update_transaction",
            entity_id: "ent-inflight-A",
            base_revision: 2,
            sync_status: "pending",
            queued_at: "2026-10-20T15:05:00Z",
            payload: { amount: "110.00", description: "First update B" }
        }), 5000, "save_inflight_update_B");

        inFlightUploadHook = async function () {
            await withTimeout(api.saveMutationToQueue("1", {
                mutation_id: "m-inflight-update-B",
                operation: "update_transaction",
                entity_id: "ent-inflight-A",
                base_revision: 2,
                sync_status: "pending",
                queued_at: "2026-10-20T15:05:00Z",
                payload: { amount: "125.50", description: "Second update B during upload" }
            }), 5000, "save_inflight_edit_B");
        };
        var inflightResB1 = await withTimeout(api.flushMutationQueue(), 5000, "flush_inflight_B1");
        assertEq("inflight_B1_status", inflightResB1.status, "partial");
        var postInflightQueueB = await withTimeout(api.getQueuedMutations("1"), 5000, "get_post_inflight_B");
        assertEq("inflight_B_new_mutation_id", postInflightQueueB[0].mutation_id !== "m-inflight-update-B", true);
        assertEq("inflight_B_operation_update", postInflightQueueB[0].operation, "update_transaction");
        assertEq("inflight_B_base_revision_3", postInflightQueueB[0].base_revision, 3);
        assertEq("inflight_B_payload_amount", postInflightQueueB[0].payload.amount, "125.50");

        var inflightResB2 = await withTimeout(api.flushMutationQueue(), 5000, "flush_inflight_B2");
        assertEq("inflight_B2_second_sync_status", inflightResB2.status, "success");
        assertEq("inflight_B2_queue_cleared", (await withTimeout(api.getQueuedMutations("1"), 5000, "get_B2_cleared")).length, 0);
        assertEq("inflight_B2_server_entity_amount", serverEntities["ent-inflight-A"].amount, "125.50");
        assertEq("inflight_B2_server_entity_rev", serverEntities["ent-inflight-A"].revision, 4);

        // SCENARIO 4C: Delete while an earlier create/update is uploading -> acknowledge -> synchronize again
        await setScenario("scenario_4C_inflight_delete_and_second_sync");
        await withTimeout(api.saveMutationToQueue("1", {
            mutation_id: "m-inflight-update-C",
            operation: "update_transaction",
            entity_id: "ent-inflight-A",
            base_revision: 4,
            sync_status: "pending",
            queued_at: "2026-10-20T15:10:00Z",
            payload: { amount: "140.00", description: "Update before in-flight delete" }
        }), 5000, "save_inflight_update_C");

        inFlightUploadHook = async function () {
            // User deletes the entity locally while the update is in flight (even using a different or same mutation_id)
            await withTimeout(api.saveMutationToQueue("1", {
                mutation_id: "m-inflight-delete-request-C",
                operation: "delete_transaction",
                entity_id: "ent-inflight-A",
                base_revision: 4,
                sync_status: "pending",
                queued_at: "2026-10-20T15:10:05Z",
                payload: {}
            }), 5000, "save_inflight_delete_C");
        };
        var inflightResC1 = await withTimeout(api.flushMutationQueue(), 5000, "flush_inflight_C1");
        assertEq("inflight_C1_status", inflightResC1.status, "partial");
        var postInflightQueueC = await withTimeout(api.getQueuedMutations("1"), 5000, "get_post_inflight_C");
        assertEq("inflight_C_single_followup_item", postInflightQueueC.length, 1);
        assertEq("inflight_C_new_mutation_id", postInflightQueueC[0].mutation_id !== "m-inflight-update-C", true);
        assertEq("inflight_C_operation_delete", postInflightQueueC[0].operation, "delete_transaction");
        assertEq("inflight_C_base_revision_5", postInflightQueueC[0].base_revision, 5);

        var inflightResC2 = await withTimeout(api.flushMutationQueue(), 5000, "flush_inflight_C2");
        assertEq("inflight_C2_second_sync_status", inflightResC2.status, "success");
        assertEq("inflight_C2_queue_cleared", (await withTimeout(api.getQueuedMutations("1"), 5000, "get_C2_cleared")).length, 0);
        assertEq("inflight_C2_server_entity_deleted", serverEntities["ent-inflight-A"].is_deleted, true);
        assertEq("inflight_C2_server_entity_rev", serverEntities["ent-inflight-A"].revision, 6);

        // SCENARIO 4D: Lost response during in-flight edit -> retry original request unchanged -> then upload newer edit
        await setScenario("scenario_4D_lost_response_with_inflight_edit");
        await withTimeout(api.saveMutationToQueue("1", {
            mutation_id: "m-inflight-lost-D",
            operation: "create_transaction",
            entity_id: "ent-inflight-D",
            base_revision: null,
            sync_status: "pending",
            queued_at: "2026-10-20T15:20:00Z",
            payload: { amount: "40.00", description: "Initial D" }
        }), 5000, "save_inflight_lost_D");

        simulateLostResponseOnce = true;
        inFlightUploadHook = async function () {
            await withTimeout(api.saveMutationToQueue("1", {
                mutation_id: "m-inflight-lost-D",
                operation: "create_transaction",
                entity_id: "ent-inflight-D",
                base_revision: null,
                sync_status: "pending",
                queued_at: "2026-10-20T15:20:00Z",
                payload: { amount: "88.00", description: "Edited D during lost response" }
            }), 5000, "save_inflight_edit_D");
        };
        var lostInflightAttempt = await withTimeout(api.flushMutationQueue(), 5000, "flush_lost_D1");
        assertEq("inflight_D1_lost_status", lostInflightAttempt.status, "failed");

        // Verify original mutation_id and payload are kept immutable for retry while staged_followup holds the edit
        var queuedAfterLostD = await withTimeout(api.getQueuedMutations("1"), 5000, "get_after_lost_D1");
        assertEq("inflight_D1_queue_len", queuedAfterLostD.length, 1);
        assertEq("inflight_D1_immutable_mutation_id", queuedAfterLostD[0].mutation_id, "m-inflight-lost-D");
        assertEq("inflight_D1_immutable_op", queuedAfterLostD[0].operation, "create_transaction");
        assertEq("inflight_D1_immutable_amount", queuedAfterLostD[0].payload.amount, "40.00");
        assertEq("inflight_D1_staged_followup_preserved", queuedAfterLostD[0].staged_followup.payload.amount, "88.00");

        // Retry replays original request idempotently and promotes the staged follow-up
        var retryInflightD2 = await withTimeout(api.flushMutationQueue(), 5000, "flush_retry_D2");
        assertEq("inflight_D2_retry_status", retryInflightD2.status, "partial");
        assertEq("inflight_D2_idempotent_replay", Boolean(retryInflightD2.results[0].idempotent_replay), true);

        // Next sync uploads the promoted follow-up update and clears the queue
        var followupInflightD3 = await withTimeout(api.flushMutationQueue(), 5000, "flush_followup_D3");
        assertEq("inflight_D3_followup_status", followupInflightD3.status, "success");
        assertEq("inflight_D3_queue_cleared", (await withTimeout(api.getQueuedMutations("1"), 5000, "get_D3_cleared")).length, 0);
        assertEq("inflight_D3_server_amount", serverEntities["ent-inflight-D"].amount, "88.00");
        assertEq("inflight_D3_server_rev", serverEntities["ent-inflight-D"].revision, 2);

        // SCENARIO 4E: Reload after acknowledgment/replacement AND reload after server commit before acknowledgment
        await setScenario("scenario_4E_reload_recovery_after_and_before_ack");
        await withTimeout(api.saveMutationToQueue("1", {
            mutation_id: "m-inflight-reload-E1",
            operation: "create_transaction",
            entity_id: "ent-inflight-E1",
            base_revision: null,
            sync_status: "pending",
            queued_at: "2026-10-20T15:30:00Z",
            payload: { amount: "15.00", description: "Initial E1" }
        }), 5000, "save_E1");
        inFlightUploadHook = async function () {
            await withTimeout(api.saveMutationToQueue("1", {
                mutation_id: "m-inflight-reload-E1",
                operation: "create_transaction",
                entity_id: "ent-inflight-E1",
                base_revision: null,
                sync_status: "pending",
                queued_at: "2026-10-20T15:30:00Z",
                payload: { amount: "25.00", description: "Edited E1 before reload" }
            }), 5000, "edit_E1");
        };
        await withTimeout(api.flushMutationQueue(), 5000, "flush_E1_first");
        // Simulate page reload after acknowledgment/replacement
        api.initializePageIdentity();
        var resE1AfterReload = await withTimeout(api.flushMutationQueue(), 5000, "flush_E1_after_reload");
        assertEq("inflight_E1_after_reload_status", resE1AfterReload.status, "success");
        assertEq("inflight_E1_queue_cleared", (await withTimeout(api.getQueuedMutations("1"), 5000, "get_E1_cleared")).length, 0);
        assertEq("inflight_E1_server_amount", serverEntities["ent-inflight-E1"].amount, "25.00");
        assertEq("inflight_E1_server_rev", serverEntities["ent-inflight-E1"].revision, 2);

        // E2: Attempt to save a mutation reusing an already-acknowledged mutation_id after reload is automatically re-ID'd
        await setScenario("scenario_4F_acknowledged_id_never_reused");
        var reIdSaved = await withTimeout(api.saveMutationToQueue("1", {
            mutation_id: "m-inflight-reload-E1", // already committed & acknowledged!
            operation: "create_transaction",
            entity_id: "ent-inflight-E1",
            base_revision: null,
            sync_status: "pending",
            queued_at: "2026-10-20T15:35:00Z",
            payload: { amount: "35.00", description: "Post-ack edit with stale ID" }
        }), 5000, "save_stale_ack_id");
        assertEq("stale_ack_id_replaced_with_fresh_id", reIdSaved.mutation_id !== "m-inflight-reload-E1", true);
        assertEq("stale_ack_id_converted_to_update", reIdSaved.operation, "update_transaction");
        var resE2 = await withTimeout(api.flushMutationQueue(), 5000, "flush_E2");
        assertEq("stale_ack_id_sync_status", resE2.status, "success");
        assertEq("stale_ack_id_server_amount", serverEntities["ent-inflight-E1"].amount, "35.00");
        assertEq("stale_ack_id_server_rev", serverEntities["ent-inflight-E1"].revision, 3);
        assertEq("zero_reused_mutation_id_errors", reusedMutationIdErrors, 0);

        // SCENARIO 5: Two tabs starting sync concurrently
        await setScenario("scenario_5_concurrent_tab_locks");
        await withTimeout(api.saveMutationToQueue("1", {
            mutation_id: "m-concurrent-1",
            operation: "create_transaction",
            entity_id: "ent-concurrent-1",
            base_revision: null,
            sync_status: "pending",
            queued_at: "2026-10-20T16:00:00Z",
            payload: { amount: "20.00" }
        }), 5000, "save_conc_1");
        var p1 = api.flushMutationQueue();
        var p2 = api.flushMutationQueue();
        var concResults = await withTimeout(Promise.all([p1, p2]), 5000, "flush_concurrent_same_tab");
        assertEq("concurrent_same_tab_statuses", [concResults[0].status, concResults[1].status], ["success", "sync_in_progress"]);

        // Simulate another tab holding an active cross-tab lock lease in localStorage
        await withTimeout(api.saveMutationToQueue("1", {
            mutation_id: "m-concurrent-cross-tab",
            operation: "create_transaction",
            entity_id: "ent-concurrent-2",
            base_revision: null,
            sync_status: "pending",
            queued_at: "2026-10-20T16:01:00Z",
            payload: { amount: "30.00" }
        }), 5000, "save_conc_2");
        localStorage.setItem("budget_tracker_sync_lock_u_1", JSON.stringify({
            tab_id: "other-tab-uuid-999",
            user_id: "1",
            acquired_at: Date.now(),
            expires_at: Date.now() + 25000
        }));
        batchCalls = [];
        var lockedRes = await withTimeout(api.flushMutationQueue(), 5000, "flush_cross_tab_locked");
        assertEq("cross_tab_lock_respected", lockedRes.status, "sync_in_progress");
        assertEq("cross_tab_lock_zero_batches", batchCalls.length, 0);
        localStorage.removeItem("budget_tracker_sync_lock_u_1");

        // SCENARIO 6: Snapshot refresh failure after successful uploads
        await setScenario("scenario_6_snapshot_refresh_failure");
        simulateSnapshotFailure = true;
        var snapFailRes = await withTimeout(api.flushMutationQueue(), 5000, "flush_snap_fail");
        assertEq("snapshot_refresh_failed_distinct_status", snapFailRes.status, "snapshot_refresh_failed");
        assertEq("snapshot_refresh_failed_applied_count", snapFailRes.applied_count, 1);
        simulateSnapshotFailure = false;

        // SCENARIO 7: Alice active with queued changes -> Bob signs in in another tab
        await setScenario("scenario_7_stale_tab_account_switch");
        await withTimeout(api.saveMutationToQueue("1", {
            mutation_id: "m-alice-unsynced",
            operation: "create_transaction",
            entity_id: "ent-alice-pending",
            base_revision: null,
            sync_status: "pending",
            queued_at: "2026-10-20T17:00:00Z",
            payload: { amount: "77.00", description: "Alice private offline entry" }
        }), 5000, "save_alice_unsynced");

        // Simulate Tab 2 signing in as Bob (user_id = 2) and broadcasting account_switched
        currentServerUserId = 2;
        currentServerUsername = "bob";
        localStorage.setItem("budget_tracker_active_user_id", "2");
        localStorage.setItem("budget_tracker_active_username", "bob");
        localStorage.setItem("budget_tracker_auth_state", "authenticated");
        var bc = new BroadcastChannel("budget_tracker_sync_channel");
        bc.postMessage({
            type: "account_switched",
            previous_user_id: "1",
            user_id: "2",
            username: "bob",
            source_tab_id: "tab-2-bob"
        });
        await new Promise(function (r) { setTimeout(r, 100); });

        assertEq("stale_tab_invalidated", api.isTabInvalidated(), true);
        assertEq("stale_tab_dom_attr", document.body.getAttribute("data-tab-invalidated"), "true");
        assertEq("private_wallet_card_cleared", document.getElementById("private-wallet-card"), null);
        var idCheckAfterSwitch = api.getActiveIdentity();
        assertEq("getActiveIdentity_returns_invalidated", idCheckAfterSwitch.invalidated, true);
        assertEq("getActiveIdentity_never_restores_alice_in_localStorage", localStorage.getItem("budget_tracker_active_user_id"), "2");

        batchCalls = [];
        var blockedSyncOnStaleTab = await withTimeout(api.flushMutationQueueForUser("1"), 5000, "flush_stale_tab");
        assertEq("stale_tab_sync_blocked", blockedSyncOnStaleTab.status, "tab_invalidated");
        assertEq("stale_tab_zero_uploads_under_bob", batchCalls.length, 0);

        // Verify Alice's pending item is still intact in Alice's IndexedDB database
        var rawDbCheck = await withTimeout(new Promise(function (resolve, reject) {
            var req = window.indexedDB.open("budget_tracker_offline_u_1", 2);
            req.onerror = function () { reject(req.error || new Error("IDB open failed")); };
            req.onblocked = function () { reject(new Error("IDB open blocked")); };
            req.onsuccess = function (ev) {
                var db = ev.target.result;
                var tx = db.transaction("mutation_queue", "readonly");
                var getAllReq = tx.objectStore("mutation_queue").getAll();
                getAllReq.onsuccess = function () {
                    db.close();
                    resolve(getAllReq.result || []);
                };
                getAllReq.onerror = function () {
                    db.close();
                    reject(getAllReq.error || new Error("IDB getAll failed"));
                };
            };
        }), 5000, "raw_db_check_alice");
        assertEq("alice_pending_changes_preserved_in_idb", rawDbCheck.length, 1);
        assertEq("alice_pending_mutation_id_intact", rawDbCheck[0].mutation_id, "m-alice-unsynced");

        // SCENARIO 8: Older queued entries lacking queue_seq are migrated in place without deletion
        await setScenario("scenario_8_legacy_queue_migration");
        var channelMessageCount = 0;
        var monitorBc = new BroadcastChannel("budget_tracker_sync_channel");
        monitorBc.onmessage = function () { channelMessageCount += 1; };

        await withTimeout(new Promise(function (resolve, reject) {
            var req = window.indexedDB.open("budget_tracker_offline_u_1", 2);
            req.onerror = function () { reject(req.error || new Error("IDB open failed")); };
            req.onblocked = function () { reject(new Error("IDB open blocked")); };
            req.onsuccess = function (ev) {
                var db = ev.target.result;
                var tx = db.transaction("mutation_queue", "readwrite");
                tx.objectStore("mutation_queue").put({
                    mutation_id: "a-legacy-no-seq",
                    operation: "create_transaction",
                    entity_id: "ent-legacy-1",
                    base_revision: null,
                    sync_status: "pending",
                    queued_at: "2026-10-19T08:00:00Z",
                    payload: { amount: "12.34", description: "Legacy entry without queue_seq" }
                });
                tx.oncomplete = function () { db.close(); resolve(); };
                tx.onerror = function () { db.close(); reject(tx.error || new Error("IDB put failed")); };
            };
        }), 5000, "insert_legacy_no_seq");

        document.body.removeAttribute("data-tab-invalidated");
        document.body.setAttribute("data-authenticated-user-id", "1");
        document.body.setAttribute("data-authenticated-username", "alice");
        api.initializePageIdentity();
        var migratedItems = await withTimeout(api.getQueuedMutations("1"), 5000, "get_migrated_items");
        assertEq("legacy_item_not_deleted_and_preserved", migratedItems.length, 2);
        var legacyFound = migratedItems.filter(function (x) { return x.mutation_id === "a-legacy-no-seq"; })[0];
        assertEq("legacy_item_assigned_numeric_queue_seq", typeof legacyFound.queue_seq === "number" && legacyFound.queue_seq > 0, true);

        // SCENARIO 9: Offline shell reload works for valid local profile, and stays locked when logged_out
        await setScenario("scenario_9_offline_shell_reload_and_logout_lock");
        document.body.removeAttribute("data-authenticated-user-id");
        document.body.removeAttribute("data-authenticated-username");
        document.body.setAttribute("data-offline-shell", "true");
        localStorage.setItem("budget_tracker_active_user_id", "1");
        localStorage.setItem("budget_tracker_active_username", "alice");
        localStorage.setItem("budget_tracker_auth_state", "authenticated");
        var validOfflineId = api.initializePageIdentity();
        assertEq("offline_reload_valid_profile_user", validOfflineId.userId, "1");
        assertEq("offline_reload_valid_profile_not_invalidated", validOfflineId.invalidated, false);

        localStorage.removeItem("budget_tracker_active_user_id");
        localStorage.removeItem("budget_tracker_active_username");
        localStorage.setItem("budget_tracker_auth_state", "logged_out");
        channelMessageCount = 0;
        var lockedOfflineId = api.initializePageIdentity();
        assertEq("offline_reload_logged_out_locked", lockedOfflineId.invalidated, true);
        assertEq("offline_reload_logged_out_empty_user", lockedOfflineId.userId, "");
        assertEq("offline_reload_zero_broadcast_loop", channelMessageCount, 0);

        // SCENARIO 10: Stale lock expiration when tab crashes or closes without releasing
        await setScenario("scenario_10_stale_lock_expiration");
        localStorage.setItem("budget_tracker_sync_lock_u_1", JSON.stringify({
            tab_id: "crashed-tab-uuid-888",
            user_id: "1",
            acquired_at: Date.now() - 40000,
            expires_at: Date.now() - 10000
        }));
        // Restore server identity as Alice (user_id = 1)
        currentServerUserId = 1;
        currentServerUsername = "alice";
        document.body.removeAttribute("data-tab-invalidated");
        document.body.removeAttribute("data-offline-shell");
        document.body.setAttribute("data-authenticated-user-id", "1");
        document.body.setAttribute("data-authenticated-username", "alice");
        localStorage.setItem("budget_tracker_active_user_id", "1");
        localStorage.setItem("budget_tracker_active_username", "alice");
        localStorage.setItem("budget_tracker_auth_state", "authenticated");
        api.initializePageIdentity();
        batchCalls = [];
        var staleLockFlushRes = await withTimeout(api.flushMutationQueue(), 5000, "flush_stale_lock");
        assertEq("stale_lock_safely_expired_and_overridden", staleLockFlushRes.status, "success");
        var activeLockAfterStaleOverride = JSON.parse(localStorage.getItem("budget_tracker_sync_lock_u_1") || "{}");
        // Lock should be released or owned by active tab (not crashed tab)
        assertEq("stale_crashed_tab_lock_not_present", activeLockAfterStaleOverride.tab_id === "crashed-tab-uuid-888", false);
        bc.close();
        monitorBc.close();
        await setScenario("completed");
    } catch (err) {
        report.errors.push("Unhandled error in " + report.last_scenario + ": " + String(err && err.stack ? err.stack : err));
    }

    await origFetch("/__test_done__", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(report)
    });
})();
"""

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_GET(self):
                if self.path == "/" or self.path.startswith("/?"):
                    data = harness_html.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                elif self.path == "/static/js/offline.js":
                    data = offline_js_code.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/javascript; charset=utf-8")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                elif self.path == "/test-runner.js":
                    data = runner_js.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/javascript; charset=utf-8")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                elif self.path.startswith("/api/sync/state/"):
                    data = b'{"user_id":1,"username":"alice","wallets":[],"categories":[],"transactions":[],"shared_spaces":[]}'
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                else:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()

            def do_POST(self):
                if self.path == "/__test_progress__":
                    length = int(self.headers.get("Content-Length", "0"))
                    raw = self.rfile.read(length).decode("utf-8")
                    try:
                        payload = json.loads(raw)
                        results_holder["last_scenario"] = payload.get("scenario", "unknown")
                    except Exception:
                        pass
                    resp = b'{"ok":true}'
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(resp)))
                    self.end_headers()
                    self.wfile.write(resp)
                elif self.path == "/__test_done__":
                    length = int(self.headers.get("Content-Length", "0"))
                    raw = self.rfile.read(length).decode("utf-8")
                    results_holder["report"] = json.loads(raw)
                    resp = b'{"ok":true}'
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(resp)))
                    self.end_headers()
                    self.wfile.write(resp)
                    done_event.set()
                else:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()

        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
        server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        server_thread.start()

        user_data_dir = tempfile.mkdtemp(prefix="bt_headless_test_")
        proc = None
        stderr_output = ""
        try:
            proc = subprocess.Popen(
                [
                    browser_bin,
                    "--headless=new",
                    "--disable-gpu",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-background-timer-throttling",
                    "--disable-renderer-backgrounding",
                    "--disable-backgrounding-occluded-windows",
                    f"--user-data-dir={user_data_dir}",
                    f"http://127.0.0.1:{port}/",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            finished = done_event.wait(timeout=30)
            if not finished and proc.poll() is not None:
                _, err_bytes = proc.communicate(timeout=2)
                stderr_output = err_bytes.decode("utf-8", errors="replace")
            self.assertTrue(
                finished,
                f"Headless browser JS test timed out during scenario "
                f"'{results_holder.get('last_scenario')}'. Browser stderr: {stderr_output[:500]}",
            )
            report = results_holder.get("report", {})
            self.assertEqual(
                report.get("errors"),
                [],
                f"Browser JS errors in scenario '{report.get('last_scenario')}': {report.get('errors')}",
            )
            self.assertGreaterEqual(len(report.get("assertions", {})), 55)
        finally:
            if proc is not None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
                try:
                    if proc.stdout:
                        proc.stdout.close()
                    if proc.stderr:
                        proc.stderr.close()
                except Exception:
                    pass
            httpd.shutdown()
            httpd.server_close()
            shutil.rmtree(user_data_dir, ignore_errors=True)


class JapaneseStationeryDashboardTests(TestCase):
    def setUp(self):
        from django.utils import timezone
        self.user = User.objects.create_user(
            username="stationery_user",
            password="Str0ng!Password#2026",
        )
        self.usd, _ = Currency.objects.get_or_create(
            code="USD",
            defaults={"name": "US Dollar", "decimal_places": 2, "is_active": True},
        )
        self.dashboard_url = reverse("budgets:dashboard")

    def test_empty_dashboard_renders_stationery_layout_and_empty_states(self):
        self.client.force_login(self.user)
        response = self.client.get(self.dashboard_url)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode("utf-8")

        # Visual layout classes
        self.assertIn("dashboard-stationery", html)
        self.assertIn("dashboard-period-header", html)
        self.assertIn("summary-strip-card", html)
        self.assertIn("summary-strip-col", html)
        self.assertIn("stationery-panel", html)

        # Monthly overview dropdown & header
        self.assertIn("Monthly overview", html)
        self.assertIn('name="period"', html)
        self.assertIn("No recorded periods", html)
        self.assertIn("disabled", html)

        # No broken mascot images or previous decorative watercolor banners
        self.assertNotIn("banner-mascot.png", html)
        self.assertNotIn("empty-spending.png", html)
        self.assertNotIn("empty-budgets.png", html)
        self.assertNotIn("empty-transactions.png", html)
        self.assertNotIn("empty-goals.png", html)
        self.assertNotIn("A little saved, a little happier", html)

        # Main content area does not render an h1 (topbar handles the h1)
        content_after_start = html.split('class="dashboard-stationery')[1].split('id="transactionModal"')[0]
        self.assertNotIn("<h1", content_after_start)

        # Calm empty states
        self.assertIn("No spending recorded yet", html)
        self.assertIn("No budgets created", html)
        self.assertIn("No transactions yet", html)
        self.assertIn("Set a goal to start saving", html)

        # Working modal trigger and links
        self.assertIn('data-bs-target="#transactionModal"', html)
        self.assertIn(reverse("budgets:budgets"), html)
        self.assertIn(reverse("budgets:transactions"), html)
        self.assertIn(reverse("budgets:savings_goals"), html)

    def test_populated_dashboard_renders_real_metrics_and_breakdowns(self):
        from django.utils import timezone
        from .services import (
            create_category,
            create_savings_goal,
            create_transaction_entry,
            record_savings_contribution,
            save_category_budget,
        )
        UserPreference.objects.create(user=self.user, personal_base_currency=self.usd)
        wallet = Wallet.objects.create(
            owner=self.user,
            name="Main Checking",
            wallet_type=Wallet.WalletType.BANK_ACCOUNT,
            currency=self.usd,
            opening_balance=Decimal("1500.00"),
            opening_balance_date=date(2026, 1, 1),
        )
        today = timezone.localdate()
        cat_food = create_category(user=self.user, name="Groceries", kind="expense")
        cat_tech = create_category(user=self.user, name="Electronics", kind="expense")
        cat_salary = create_category(user=self.user, name="Salary", kind="income")

        create_transaction_entry(
            user=self.user,
            transaction_type="income",
            amount=Decimal("3000.00"),
            wallet=wallet,
            transaction_date=today,
            category=cat_salary,
            description="Monthly Paycheck",
        )
        create_transaction_entry(
            user=self.user,
            transaction_type="expense",
            amount=Decimal("200.00"),
            wallet=wallet,
            transaction_date=today,
            category=cat_food,
            description="Supermarket trip",
        )
        create_transaction_entry(
            user=self.user,
            transaction_type="expense",
            amount=Decimal("100.00"),
            wallet=wallet,
            transaction_date=today,
            category=cat_tech,
            description="USB-C Hub",
        )
        save_category_budget(
            user=self.user,
            category=cat_food,
            period=today.strftime("%Y-%m"),
            currency=self.usd,
            limit_amount=Decimal("500.00"),
        )
        goal = create_savings_goal(
            user=self.user,
            name="Emergency Fund",
            target_amount=Decimal("5000.00"),
            currency=self.usd,
        )
        record_savings_contribution(
            user=self.user,
            goal=goal,
            amount=Decimal("1250.00"),
            contribution_date=today,
        )

        self.client.force_login(self.user)
        response = self.client.get(self.dashboard_url)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode("utf-8")

        # Summary strip numbers
        self.assertIn("3,000.00", html)
        self.assertIn("300.00", html)

        # Spending this month category breakdown
        self.assertIn("Groceries", html)
        self.assertIn("200.00", html)
        self.assertIn("66.7%", html)
        self.assertIn("Electronics", html)
        self.assertIn("100.00", html)
        self.assertIn("33.3%", html)

        # Monthly budgets
        self.assertIn("500.00", html)
        self.assertIn("Remaining: 300.00 USD", html)

        # Recent transactions table
        self.assertIn("Supermarket trip", html)
        self.assertIn("Main Checking", html)
        self.assertIn("-200.00 USD", html)
        self.assertIn("+3,000.00 USD", html)

        # Savings goals
        self.assertIn("Emergency Fund", html)
        self.assertIn("1,250.00", html)
        self.assertIn("5,000.00", html)

    def test_period_filter_via_get_param_and_invalid_fallback(self):
        from django.utils import timezone
        from .services import (
            create_category,
            create_transaction_entry,
            save_category_budget,
        )
        UserPreference.objects.create(user=self.user, personal_base_currency=self.usd)
        wallet = Wallet.objects.create(
            owner=self.user,
            name="Bank",
            wallet_type=Wallet.WalletType.BANK_ACCOUNT,
            currency=self.usd,
            opening_balance=Decimal("1000.00"),
            opening_balance_date=date(2026, 1, 1),
        )
        cat = create_category(user=self.user, name="Books", kind="expense")

        # Create past transaction in 2026-03
        create_transaction_entry(
            user=self.user,
            transaction_type="expense",
            amount=Decimal("45.00"),
            wallet=wallet,
            transaction_date=date(2026, 3, 15),
            category=cat,
            description="Design Book",
        )
        save_category_budget(
            user=self.user,
            category=cat,
            period="2026-03",
            currency=self.usd,
            limit_amount=Decimal("100.00"),
        )

        self.client.force_login(self.user)

        # 1. Query for 2026-03
        resp_mar = self.client.get(f"{self.dashboard_url}?period=2026-03")
        self.assertEqual(resp_mar.status_code, 200)
        html_mar = resp_mar.content.decode("utf-8")
        self.assertIn("March 2026", html_mar)
        self.assertIn("45.00", html_mar)
        self.assertIn("Books", html_mar)

        # 2. Query with invalid period falls back to latest recorded month when current month is not available
        resp_invalid = self.client.get(f"{self.dashboard_url}?period=invalid-period")
        self.assertEqual(resp_invalid.status_code, 200)
        self.assertIn("March 2026", resp_invalid.content.decode("utf-8"))

        # 3. When current month IS available, invalid period falls back to current month
        today = timezone.localdate()
        create_transaction_entry(
            user=self.user,
            transaction_type="expense",
            amount=Decimal("15.00"),
            wallet=wallet,
            transaction_date=today,
            category=cat,
            description="Today Item",
        )
        resp_invalid_today = self.client.get(f"{self.dashboard_url}?period=invalid-period")
        self.assertEqual(resp_invalid_today.status_code, 200)
        self.assertIn(today.strftime("%B %Y"), resp_invalid_today.content.decode("utf-8"))

    def test_distinguish_zero_from_missing_data(self):
        self.client.force_login(self.user)

        # When no base currency is set, show dash "—" and prompt to set base currency
        resp_unconfigured = self.client.get(self.dashboard_url)
        self.assertEqual(resp_unconfigured.status_code, 200)
        html_unconf = resp_unconfigured.content.decode("utf-8")
        self.assertIn("Set base currency on Currencies page", html_unconf)

        # When base currency is set and user has 0 activity, show "0.00", not "—"
        UserPreference.objects.create(user=self.user, personal_base_currency=self.usd)
        resp_zero = self.client.get(self.dashboard_url)
        self.assertEqual(resp_zero.status_code, 200)
        html_zero = resp_zero.content.decode("utf-8")
        self.assertIn("0.00", html_zero)
        self.assertIn("USD &middot; Personal scope", html_zero)

    def test_empty_chart_has_no_fabricated_axis_labels(self):
        self.client.force_login(self.user)
        response = self.client.get(self.dashboard_url)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode("utf-8")
        self.assertNotIn("50k", html)
        self.assertNotIn("25k", html)
        self.assertIn("chart-wire-svg", html)

    def test_two_expenses_different_currencies_where_one_rate_missing(self):
        from django.utils import timezone
        from .services import (
            create_category,
            create_transaction_entry,
        )
        eur, _ = Currency.objects.get_or_create(
            code="EUR",
            defaults={"name": "Euro", "decimal_places": 2, "is_active": True},
        )
        UserPreference.objects.create(user=self.user, personal_base_currency=self.usd)
        w_usd = Wallet.objects.create(
            owner=self.user,
            name="USD Wallet",
            wallet_type=Wallet.WalletType.BANK_ACCOUNT,
            currency=self.usd,
            opening_balance=Decimal("1000.00"),
            opening_balance_date=date(2026, 1, 1),
        )
        w_eur = Wallet.objects.create(
            owner=self.user,
            name="EUR Wallet",
            wallet_type=Wallet.WalletType.BANK_ACCOUNT,
            currency=eur,
            opening_balance=Decimal("1000.00"),
            opening_balance_date=date(2026, 1, 1),
        )
        today = timezone.localdate()
        cat_groceries = create_category(user=self.user, name="Groceries", kind="expense")
        cat_dining = create_category(user=self.user, name="Dining", kind="expense")

        create_transaction_entry(
            user=self.user,
            transaction_type="expense",
            amount=Decimal("100.00"),
            wallet=w_usd,
            transaction_date=today,
            category=cat_groceries,
            description="Supermarket",
        )
        create_transaction_entry(
            user=self.user,
            transaction_type="expense",
            amount=Decimal("50.00"),
            wallet=w_eur,
            transaction_date=today,
            category=cat_dining,
            description="Paris Bistro",
        )

        self.client.force_login(self.user)
        resp = self.client.get(self.dashboard_url)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")

        # Mixed total 150.00 should NEVER exist
        self.assertNotIn("150.00", html)
        # Converted expense 100.00 USD is aggregated for Groceries
        self.assertIn("100.00", html)
        # Visible incomplete-data notice
        self.assertIn("1 expense pending conversion. Percentages and distribution omitted.", html)
        # Proportion bar and percentage are omitted
        self.assertNotIn("stationery-bar-spend", html)
        self.assertNotIn("%", html.split('id="panel-spending-title"')[1].split('</section>')[0])

    def test_all_expenses_missing_conversion_shows_unavailable_state(self):
        from django.utils import timezone
        from .services import (
            create_category,
            create_transaction_entry,
        )
        eur, _ = Currency.objects.get_or_create(
            code="EUR",
            defaults={"name": "Euro", "decimal_places": 2, "is_active": True},
        )
        UserPreference.objects.create(user=self.user, personal_base_currency=self.usd)
        w_eur = Wallet.objects.create(
            owner=self.user,
            name="EUR Wallet",
            wallet_type=Wallet.WalletType.BANK_ACCOUNT,
            currency=eur,
            opening_balance=Decimal("1000.00"),
            opening_balance_date=date(2026, 1, 1),
        )
        today = timezone.localdate()
        cat = create_category(user=self.user, name="Dining", kind="expense")

        create_transaction_entry(
            user=self.user,
            transaction_type="expense",
            amount=Decimal("50.00"),
            wallet=w_eur,
            transaction_date=today,
            category=cat,
            description="Paris Bistro",
        )

        self.client.force_login(self.user)
        resp = self.client.get(self.dashboard_url)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")

        self.assertIn("Spending unavailable: exchange rates missing", html)
        self.assertNotIn("No spending recorded yet", html)

    def test_personal_and_selected_shared_space_isolation(self):
        from django.utils import timezone
        from .services import (
            create_category,
            create_shared_space,
            create_shared_space_invitation,
            accept_shared_space_invitation,
            create_transaction_entry,
            share_personal_transaction,
        )
        bob = User.objects.create_user(username="bob_iso", password="Str0ng!Password#2026")
        pref = UserPreference.objects.create(user=self.user, personal_base_currency=self.usd)
        UserPreference.objects.create(user=bob, personal_base_currency=self.usd)

        # 1. Personal wallet & transaction
        w_personal = Wallet.objects.create(
            owner=self.user,
            name="Alice Personal Wallet",
            wallet_type=Wallet.WalletType.BANK_ACCOUNT,
            currency=self.usd,
            opening_balance=Decimal("500.00"),
            opening_balance_date=date(2026, 1, 1),
        )
        today = timezone.localdate()
        cat_p = create_category(user=self.user, name="Personal Stuff", kind="expense", scope="personal")
        tx_personal = create_transaction_entry(
            user=self.user,
            transaction_type="expense",
            amount=Decimal("50.00"),
            wallet=w_personal,
            transaction_date=today,
            category=cat_p,
            description="Alice Personal Coffee",
        )

        # 2. Shared Space 1 with wallet & transaction
        space1 = create_shared_space(user=self.user, name="Space One", base_currency=self.usd)
        w_space1 = Wallet.objects.create(
            owner=self.user,
            name="Space One Wallet",
            wallet_type=Wallet.WalletType.BANK_ACCOUNT,
            currency=self.usd,
            ownership_scope=Wallet.OwnershipScope.SHARED,
            shared_space=space1,
            opening_balance=Decimal("800.00"),
            opening_balance_date=date(2026, 1, 1),
        )
        cat_s1 = create_category(user=self.user, name="Space One Groceries", kind="expense", scope="shared", shared_space=space1)
        create_transaction_entry(
            user=self.user,
            transaction_type="expense",
            amount=Decimal("80.00"),
            wallet=w_space1,
            transaction_date=today,
            category=cat_s1,
            scope="shared",
            shared_space=space1,
            description="Space One Milk",
        )

        # 3. Shared Space 2 with wallet & transaction
        space2 = create_shared_space(user=self.user, name="Space Two", base_currency=self.usd)
        w_space2 = Wallet.objects.create(
            owner=self.user,
            name="Space Two Wallet",
            wallet_type=Wallet.WalletType.BANK_ACCOUNT,
            currency=self.usd,
            ownership_scope=Wallet.OwnershipScope.SHARED,
            shared_space=space2,
            opening_balance=Decimal("1200.00"),
            opening_balance_date=date(2026, 1, 1),
        )
        cat_s2 = create_category(user=self.user, name="Space Two Tools", kind="expense", scope="shared", shared_space=space2)
        create_transaction_entry(
            user=self.user,
            transaction_type="expense",
            amount=Decimal("120.00"),
            wallet=w_space2,
            transaction_date=today,
            category=cat_s2,
            scope="shared",
            shared_space=space2,
            description="Space Two Hammer",
        )

        # 4. Empty Shared Space 3
        space3 = create_shared_space(user=self.user, name="Space Three", base_currency=self.usd)

        self.client.force_login(self.user)

        # Verification A: Personal View
        pref.active_scope = "personal"
        pref.active_shared_space = None
        pref.save()
        resp_p = self.client.get(self.dashboard_url)
        html_p = resp_p.content.decode("utf-8")
        self.assertIn("Alice Personal Coffee", html_p)
        self.assertIn("1 active wallet", html_p)
        self.assertNotIn("Space One Milk", html_p)
        self.assertNotIn("Space Two Hammer", html_p)
        self.assertIn("50.00", html_p)
        self.assertNotIn("80.00", html_p)

        # Verification B: Shared Space 1 View
        pref.active_scope = "shared"
        pref.active_shared_space = space1
        pref.save()
        resp_s1 = self.client.get(self.dashboard_url)
        html_s1 = resp_s1.content.decode("utf-8")
        self.assertIn("Space One Milk", html_s1)
        self.assertIn("1 active wallet", html_s1)
        self.assertNotIn("Alice Personal Coffee", html_s1)
        self.assertNotIn("Space Two Hammer", html_s1)
        self.assertIn("80.00", html_s1)
        self.assertNotIn("120.00", html_s1)

        # Verification C: Empty Shared Space 3 stays empty with records elsewhere
        pref.active_scope = "shared"
        pref.active_shared_space = space3
        pref.save()
        resp_s3 = self.client.get(self.dashboard_url)
        html_s3 = resp_s3.content.decode("utf-8")
        self.assertIn("0 active wallets", html_s3)
        self.assertIn("No transactions yet", html_s3)
        self.assertIn("No spending recorded yet", html_s3)
        self.assertNotIn("Space One Milk", html_s3)
        self.assertNotIn("Alice Personal Coffee", html_s3)

        # Verification D: Disclosed personal transactions for visibility are excluded from shared financial totals
        inv = create_shared_space_invitation(user=self.user, shared_space=space1)
        accept_shared_space_invitation(user=bob, code=inv.code)
        w_bob = Wallet.objects.create(
            owner=bob,
            name="Bob Personal",
            wallet_type=Wallet.WalletType.BANK_ACCOUNT,
            currency=self.usd,
            opening_balance=Decimal("999.00"),
            opening_balance_date=date(2026, 1, 1),
        )
        bob_tx = create_transaction_entry(
            user=bob,
            transaction_type="expense",
            amount=Decimal("333.00"),
            wallet=w_bob,
            transaction_date=today,
            description="Bob Secret Dinner",
        )
        share_personal_transaction(user=bob, transaction_id=bob_tx.id, shared_space_id=space1.id)

        pref.active_scope = "shared"
        pref.active_shared_space = space1
        pref.save()
        resp_s1_after = self.client.get(self.dashboard_url)
        html_s1_after = resp_s1_after.content.decode("utf-8")
        # Disclosed personal transaction 333.00 must NOT be added to Space One expenses or balance
        self.assertNotIn("333.00", html_s1_after)
        self.assertNotIn("413.00", html_s1_after)
        self.assertIn("80.00", html_s1_after)

    def test_month_selector_deduplication_and_newest_first_ordering(self):
        from .services import create_category, create_transaction_entry, save_category_budget
        UserPreference.objects.create(user=self.user, personal_base_currency=self.usd)
        wallet = Wallet.objects.create(
            owner=self.user,
            name="Bank",
            wallet_type=Wallet.WalletType.BANK_ACCOUNT,
            currency=self.usd,
            opening_balance=Decimal("1000.00"),
            opening_balance_date=date(2025, 1, 1),
        )
        cat = create_category(user=self.user, name="General", kind="expense")

        # Multiple transactions and budgets in 2026-02
        create_transaction_entry(
            user=self.user,
            transaction_type="expense",
            amount=Decimal("10.00"),
            wallet=wallet,
            transaction_date=date(2026, 2, 5),
            category=cat,
        )
        create_transaction_entry(
            user=self.user,
            transaction_type="expense",
            amount=Decimal("20.00"),
            wallet=wallet,
            transaction_date=date(2026, 2, 20),
            category=cat,
        )
        save_category_budget(
            user=self.user,
            category=cat,
            period="2026-02",
            currency=self.usd,
            limit_amount=Decimal("100.00"),
        )

        # Multiple transactions and budgets in 2026-05
        create_transaction_entry(
            user=self.user,
            transaction_type="expense",
            amount=Decimal("30.00"),
            wallet=wallet,
            transaction_date=date(2026, 5, 10),
            category=cat,
        )
        save_category_budget(
            user=self.user,
            category=cat,
            period="2026-05",
            currency=self.usd,
            limit_amount=Decimal("200.00"),
        )

        # Single transaction in 2025-11
        create_transaction_entry(
            user=self.user,
            transaction_type="expense",
            amount=Decimal("40.00"),
            wallet=wallet,
            transaction_date=date(2025, 11, 15),
            category=cat,
        )

        self.client.force_login(self.user)
        response = self.client.get(self.dashboard_url)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode("utf-8")

        # Each month appears exactly once in the period selector options (deduplication)
        self.assertEqual(html.count('value="2026-05"'), 1)
        self.assertEqual(html.count('value="2026-02"'), 1)
        self.assertEqual(html.count('value="2025-11"'), 1)

        # Newest-first ordering: 2026-05 before 2026-02 before 2025-11
        pos_05 = html.find('value="2026-05"')
        pos_02 = html.find('value="2026-02"')
        pos_11 = html.find('value="2025-11"')
        self.assertTrue(pos_05 < pos_02 < pos_11)

    def test_month_selector_budget_only_and_transaction_only_months(self):
        from .services import create_category, create_transaction_entry, save_category_budget
        UserPreference.objects.create(user=self.user, personal_base_currency=self.usd)
        wallet = Wallet.objects.create(
            owner=self.user,
            name="Bank",
            wallet_type=Wallet.WalletType.BANK_ACCOUNT,
            currency=self.usd,
            opening_balance=Decimal("1000.00"),
            opening_balance_date=date(2026, 1, 1),
        )
        cat = create_category(user=self.user, name="Subscriptions", kind="expense")

        # Month 2026-04 has only a budget (no transactions)
        save_category_budget(
            user=self.user,
            category=cat,
            period="2026-04",
            currency=self.usd,
            limit_amount=Decimal("75.00"),
        )

        # Month 2026-08 has only a transaction (no budgets)
        create_transaction_entry(
            user=self.user,
            transaction_type="expense",
            amount=Decimal("35.00"),
            wallet=wallet,
            transaction_date=date(2026, 8, 12),
            category=cat,
            description="Book Purchase",
        )

        self.client.force_login(self.user)
        response = self.client.get(self.dashboard_url)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode("utf-8")

        # Both months are options in the selector
        self.assertIn('value="2026-08"', html)
        self.assertIn('value="2026-04"', html)

        # Query budget-only month 2026-04
        resp_budget = self.client.get(f"{self.dashboard_url}?period=2026-04")
        html_b = resp_budget.content.decode("utf-8")
        self.assertIn("April 2026", html_b)
        self.assertIn("75.00", html_b)
        self.assertIn("Subscriptions", html_b)
        self.assertIn("No spending recorded yet", html_b)

        # Query transaction-only month 2026-08
        resp_tx = self.client.get(f"{self.dashboard_url}?period=2026-08")
        html_t = resp_tx.content.decode("utf-8")
        self.assertIn("August 2026", html_t)
        self.assertIn("35.00", html_t)
        self.assertIn("No budgets created", html_t)

    def test_month_selector_no_records_disables_selector_and_preserves_wallet(self):
        from django.utils import timezone
        UserPreference.objects.create(user=self.user, personal_base_currency=self.usd)
        Wallet.objects.create(
            owner=self.user,
            name="Emergency Cash",
            wallet_type=Wallet.WalletType.CASH,
            currency=self.usd,
            opening_balance=Decimal("650.00"),
            opening_balance_date=date(2026, 1, 1),
        )

        self.client.force_login(self.user)
        response = self.client.get(self.dashboard_url)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode("utf-8")

        # Selector is disabled and shows 'No recorded periods'
        self.assertIn("disabled", html)
        self.assertIn("No recorded periods", html)
        self.assertIn("disabled-period-wrap", html)

        # Does not invent a selectable month
        today = timezone.localdate()
        today_val = today.strftime("%Y-%m")
        self.assertNotIn(f'value="{today_val}"', html)

        # Current wallet balance is preserved
        self.assertIn("650.00", html)
        self.assertIn("1 active wallet", html)

        # Add transaction action and modal trigger are preserved
        self.assertIn('data-bs-target="#transactionModal"', html)
        self.assertIn("+ Add transaction", html)

        # Empty states
        self.assertIn("No spending recorded yet", html)
        self.assertIn("No budgets created", html)
        self.assertIn("No transactions yet", html)

    def test_month_selector_workspace_and_disclosure_isolation(self):
        from .services import (
            create_category,
            create_shared_space,
            create_shared_space_invitation,
            accept_shared_space_invitation,
            create_transaction_entry,
            save_category_budget,
            share_personal_transaction,
        )
        bob = User.objects.create_user(username="bob_month_iso", password="Str0ng!Password#2026")
        pref = UserPreference.objects.create(user=self.user, personal_base_currency=self.usd)
        UserPreference.objects.create(user=bob, personal_base_currency=self.usd)

        # 1. Alice personal wallet, transaction (2026-01), budget (2026-03)
        w_alice = Wallet.objects.create(
            owner=self.user,
            name="Alice Wallet",
            wallet_type=Wallet.WalletType.BANK_ACCOUNT,
            currency=self.usd,
            opening_balance=Decimal("1000.00"),
            opening_balance_date=date(2026, 1, 1),
        )
        cat_p = create_category(user=self.user, name="Personal Stuff", kind="expense", scope="personal")
        create_transaction_entry(
            user=self.user,
            transaction_type="expense",
            amount=Decimal("20.00"),
            wallet=w_alice,
            transaction_date=date(2026, 1, 10),
            category=cat_p,
        )
        save_category_budget(
            user=self.user,
            category=cat_p,
            period="2026-03",
            currency=self.usd,
            limit_amount=Decimal("100.00"),
        )

        # Alice soft-deleted personal transaction (2026-11)
        del_tx = create_transaction_entry(
            user=self.user,
            transaction_type="expense",
            amount=Decimal("99.00"),
            wallet=w_alice,
            transaction_date=date(2026, 11, 5),
            category=cat_p,
        )
        del_tx.is_deleted = True
        del_tx.save()

        # Alice archived personal budget (2026-12)
        arch_budget = save_category_budget(
            user=self.user,
            category=cat_p,
            period="2026-12",
            currency=self.usd,
            limit_amount=Decimal("150.00"),
        )
        arch_budget.is_archived = True
        arch_budget.save()

        # 2. Shared Space 1 with transaction (2026-05) and budget (2026-06)
        space1 = create_shared_space(user=self.user, name="Space Alpha", base_currency=self.usd)
        w_space1 = Wallet.objects.create(
            owner=self.user,
            name="Space Alpha Wallet",
            wallet_type=Wallet.WalletType.BANK_ACCOUNT,
            currency=self.usd,
            ownership_scope=Wallet.OwnershipScope.SHARED,
            shared_space=space1,
            opening_balance=Decimal("500.00"),
            opening_balance_date=date(2026, 1, 1),
        )
        cat_s1 = create_category(user=self.user, name="Alpha Food", kind="expense", scope="shared", shared_space=space1)
        create_transaction_entry(
            user=self.user,
            transaction_type="expense",
            amount=Decimal("50.00"),
            wallet=w_space1,
            transaction_date=date(2026, 5, 14),
            category=cat_s1,
            scope="shared",
            shared_space=space1,
        )
        save_category_budget(
            user=self.user,
            category=cat_s1,
            period="2026-06",
            currency=self.usd,
            limit_amount=Decimal("200.00"),
            scope="shared",
            shared_space=space1,
        )

        # 3. Shared Space 2 (another space Alice is in) with transaction (2026-07)
        space2 = create_shared_space(user=self.user, name="Space Beta", base_currency=self.usd)
        w_space2 = Wallet.objects.create(
            owner=self.user,
            name="Space Beta Wallet",
            wallet_type=Wallet.WalletType.BANK_ACCOUNT,
            currency=self.usd,
            ownership_scope=Wallet.OwnershipScope.SHARED,
            shared_space=space2,
            opening_balance=Decimal("700.00"),
            opening_balance_date=date(2026, 1, 1),
        )
        cat_s2 = create_category(user=self.user, name="Beta Stuff", kind="expense", scope="shared", shared_space=space2)
        create_transaction_entry(
            user=self.user,
            transaction_type="expense",
            amount=Decimal("70.00"),
            wallet=w_space2,
            transaction_date=date(2026, 7, 20),
            category=cat_s2,
            scope="shared",
            shared_space=space2,
        )

        # 4. Bob joins Space 1 and shares his personal transaction (2026-09) for visibility
        inv = create_shared_space_invitation(user=self.user, shared_space=space1)
        accept_shared_space_invitation(user=bob, code=inv.code)
        w_bob = Wallet.objects.create(
            owner=bob,
            name="Bob Wallet",
            wallet_type=Wallet.WalletType.BANK_ACCOUNT,
            currency=self.usd,
            opening_balance=Decimal("300.00"),
            opening_balance_date=date(2026, 1, 1),
        )
        bob_tx = create_transaction_entry(
            user=bob,
            transaction_type="expense",
            amount=Decimal("90.00"),
            wallet=w_bob,
            transaction_date=date(2026, 9, 10),
            description="Bob Personal Disclosed Dinner",
        )
        share_personal_transaction(user=bob, transaction_id=bob_tx.id, shared_space_id=space1.id)

        self.client.force_login(self.user)

        # --- Check Personal View ---
        pref.active_scope = "personal"
        pref.active_shared_space = None
        pref.save()
        resp_p = self.client.get(self.dashboard_url)
        html_p = resp_p.content.decode("utf-8")
        # Personal months present
        self.assertIn('value="2026-03"', html_p)
        self.assertIn('value="2026-01"', html_p)
        # Excluded from Personal view
        self.assertNotIn('value="2026-05"', html_p)  # Space 1
        self.assertNotIn('value="2026-06"', html_p)  # Space 1
        self.assertNotIn('value="2026-07"', html_p)  # Space 2
        self.assertNotIn('value="2026-09"', html_p)  # Disclosed personal transaction from Bob
        self.assertNotIn('value="2026-11"', html_p)  # Deleted transaction
        self.assertNotIn('value="2026-12"', html_p)  # Archived budget
        self.assertNotIn('value="2035-01"', html_p)  # Unrecorded future month

        # Requesting an out-of-scope period in Personal view (e.g. 2026-05) falls back to authorized latest
        resp_p_hack = self.client.get(f"{self.dashboard_url}?period=2026-05")
        html_p_hack = resp_p_hack.content.decode("utf-8")
        self.assertIn("March 2026", html_p_hack)

        # --- Check Shared Space 1 View ---
        pref.active_scope = "shared"
        pref.active_shared_space = space1
        pref.save()
        resp_s1 = self.client.get(self.dashboard_url)
        html_s1 = resp_s1.content.decode("utf-8")
        # Space 1 months present
        self.assertIn('value="2026-06"', html_s1)
        self.assertIn('value="2026-05"', html_s1)
        # Excluded from Space 1 view
        self.assertNotIn('value="2026-01"', html_s1)  # Personal
        self.assertNotIn('value="2026-03"', html_s1)  # Personal
        self.assertNotIn('value="2026-07"', html_s1)  # Space 2
        self.assertNotIn('value="2026-09"', html_s1)  # Disclosed personal transaction for visibility
        self.assertNotIn('value="2026-11"', html_s1)  # Deleted
        self.assertNotIn('value="2026-12"', html_s1)  # Archived

        # --- Check Future Month when real record exists ---
        create_transaction_entry(
            user=self.user,
            transaction_type="expense",
            amount=Decimal("10.00"),
            wallet=w_space1,
            transaction_date=date(2035, 3, 1),
            category=cat_s1,
            scope="shared",
            shared_space=space1,
        )
        resp_future = self.client.get(self.dashboard_url)
        html_future = resp_future.content.decode("utf-8")
        self.assertIn('value="2035-03"', html_future)
        # Ordered newest first
        pos_future = html_future.find('value="2035-03"')
        pos_s1_06 = html_future.find('value="2026-06"')
        self.assertTrue(pos_future < pos_s1_06)

    def test_consistent_v12_cache_version_references(self):
        import shutil
        import tempfile
        from pathlib import Path
        from django.conf import settings
        from django.test import override_settings
        from .views import compute_service_worker_cache_name

        sw_resp = self.client.get("/service-worker.js")
        sw_js = sw_resp.content.decode("utf-8")
        expected_cache_name = compute_service_worker_cache_name()
        self.assertIn(f'const CACHE_NAME = "{expected_cache_name}"', sw_js)
        self.assertIn('const CACHE_PREFIX = "budget-tracker-shell-"', sw_js)
        self.assertIn('k.indexOf(CACHE_PREFIX) === 0 && k !== CACHE_NAME', sw_js)
        self.assertIn('caches.delete(k)', sw_js)
        self.assertIn('"/static/css/dashboard.css?v=v12"', sw_js)
        self.assertIn('"/static/css/app.css?v=v12"', sw_js)
        self.assertIn('"/static/js/app.js?v=v12"', sw_js)
        self.assertIn('"/static/js/offline.js?v=v12"', sw_js)
        self.assertIn('"/static/js/pages/transaction.js?v=v12"', sw_js)
        self.assertIn('"/static/images/logo/jubao_text.png?v=v12"', sw_js)
        self.assertIn('"/static/images/logo/jubao_text_light.png?v=v12"', sw_js)
        self.assertIn('"/static/images/logo/jubao_logo.png?v=v12"', sw_js)
        self.assertIn('"/static/images/logo/jubao_logo_light.png?v=v12"', sw_js)
        self.assertIn('"/static/images/favicon.ico"', sw_js)
        self.assertIn('"/static/images/favicon-32.png"', sw_js)
        self.assertIn('"/static/images/favicon.svg"', sw_js)
        self.assertIn('"/static/images/apple-touch-icon.png"', sw_js)
        self.assertIn('"/static/images/icon-192.png"', sw_js)
        self.assertIn('"/static/images/icon-512.png"', sw_js)

        # Verify that if any precached file changes, the service-worker CACHE_NAME automatically changes
        temp_base = Path(tempfile.mkdtemp(prefix="sw_hash_test_"))
        try:
            shutil.copytree(Path(settings.BASE_DIR) / "static", temp_base / "static")
            shutil.copytree(Path(settings.BASE_DIR) / "templates", temp_base / "templates")
            self.assertEqual(compute_service_worker_cache_name(temp_base), expected_cache_name)

            # Mutate a precached CSS file and verify CACHE_NAME changes both via helper and /service-worker.js
            app_css_copy = temp_base / "static" / "css" / "app.css"
            app_css_copy.write_text(app_css_copy.read_text(encoding="utf-8") + "\n/* cache hash bust test */\n", encoding="utf-8")
            mutated_cache_name = compute_service_worker_cache_name(temp_base)
            self.assertNotEqual(mutated_cache_name, expected_cache_name)

            with override_settings(BASE_DIR=temp_base):
                sw_mut_resp = self.client.get("/service-worker.js")
                sw_mut_js = sw_mut_resp.content.decode("utf-8")
                self.assertIn(f'const CACHE_NAME = "{mutated_cache_name}"', sw_mut_js)
                self.assertNotIn(f'const CACHE_NAME = "{expected_cache_name}"', sw_mut_js)

                # Verify repeated calls return cached result without error
                self.assertEqual(compute_service_worker_cache_name(temp_base), mutated_cache_name)
        finally:
            shutil.rmtree(temp_base, ignore_errors=True)

        # Verify that compute_service_worker_cache_name hashes files in STATIC_ROOT when configured
        temp_static_root = Path(tempfile.mkdtemp(prefix="sw_static_root_"))
        try:
            (temp_static_root / "css").mkdir(parents=True)
            custom_app_css = temp_static_root / "css" / "app.css"
            custom_app_css.write_text("/* collectstatic bundled app.css */\n", encoding="utf-8")
            with override_settings(STATIC_ROOT=temp_static_root):
                static_root_cache_name = compute_service_worker_cache_name()
                self.assertNotEqual(static_root_cache_name, expected_cache_name)
                # Verify cached return on repeated call
                self.assertEqual(compute_service_worker_cache_name(), static_root_cache_name)
        finally:
            shutil.rmtree(temp_static_root, ignore_errors=True)

        self.client.force_login(self.user)
        dash_resp = self.client.get(self.dashboard_url)
        dash_html = dash_resp.content.decode("utf-8")
        self.assertIn('css/dashboard.css?v=v12', dash_html)
        self.assertIn('css/app.css?v=v12', dash_html)
        self.assertIn('js/app.js?v=v12', dash_html)
        self.assertIn('js/offline.js?v=v12', dash_html)
        self.assertIn('js/pages/transaction.js?v=v12', dash_html)

        tx_resp = self.client.get(reverse("budgets:transactions"))
        tx_html = tx_resp.content.decode("utf-8")
        self.assertIn('css/dashboard.css?v=v12', tx_html)
        self.assertIn('js/pages/transaction.js?v=v12', tx_html)

        off_resp = self.client.get(reverse("budgets:offline"))
        off_html = off_resp.content.decode("utf-8")
        self.assertIn('css/app.css?v=v12', off_html)
        self.assertIn('js/offline.js?v=v12', off_html)

        login_resp = self.client.get(reverse("accounts:login"))
        login_html = login_resp.content.decode("utf-8")
        self.assertIn('css/app.css?v=v12', login_html)

    def test_dashboard_budgets_independent_of_display_currency_and_scoped(self):
        from .services import (
            create_category,
            create_shared_space,
            create_transaction_entry,
            save_category_budget,
        )

        pref = UserPreference.objects.create(user=self.user, personal_base_currency=self.usd)
        twd, _ = Currency.objects.get_or_create(
            code="TWD",
            defaults={"name": "New Taiwan Dollar", "decimal_places": 0, "is_active": True},
        )
        eur, _ = Currency.objects.get_or_create(
            code="EUR",
            defaults={"name": "Euro", "decimal_places": 2, "is_active": True},
        )

        cat_twd = create_category(user=self.user, name="Franz Books", kind="expense")
        save_category_budget(
            user=self.user,
            category=cat_twd,
            period="2026-10",
            currency=twd,
            limit_amount=Decimal("5000.00"),
        )
        w_twd = Wallet.objects.create(
            owner=self.user,
            name="Franz TWD Wallet",
            wallet_type=Wallet.WalletType.CASH,
            currency=twd,
            opening_balance=Decimal("10000.00"),
            opening_balance_date=date(2026, 10, 1),
        )

        # 1. TWD budget appears on USD-display dashboard for same month and workspace
        self.client.force_login(self.user)
        resp = self.client.get(f"{self.dashboard_url}?period=2026-10")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")
        self.assertIn("Franz Books", html)
        self.assertIn("5,000", html)
        self.assertIn("TWD", html)
        self.assertNotIn("No budgets created", html)

        # 2. Switching display currency does not change which budget records appear
        pref.personal_base_currency = eur
        pref.save()
        resp_eur = self.client.get(f"{self.dashboard_url}?period=2026-10")
        self.assertEqual(resp_eur.status_code, 200)
        html_eur = resp_eur.content.decode("utf-8")
        self.assertIn("Franz Books", html_eur)
        self.assertIn("5,000", html_eur)
        self.assertIn("TWD", html_eur)
        self.assertNotIn("No budgets created", html_eur)

        # 3. Another month remains excluded
        cat_nov = create_category(user=self.user, name="Nov Holiday", kind="expense")
        save_category_budget(
            user=self.user,
            category=cat_nov,
            period="2026-11",
            currency=twd,
            limit_amount=Decimal("2000.00"),
        )
        resp_oct = self.client.get(f"{self.dashboard_url}?period=2026-10")
        html_oct = resp_oct.content.decode("utf-8")
        budget_panel_oct = html_oct.split('id="panel-budgets-title"')[1].split("</section>")[0]
        self.assertIn("Franz Books", budget_panel_oct)
        self.assertNotIn("Nov Holiday", budget_panel_oct)
        self.assertEqual(
            [r["category"].name for r in resp_oct.context["budget_overview"]["rows"]],
            ["Franz Books"],
        )

        # 4. Another shared space remains excluded
        space1 = create_shared_space(user=self.user, name="Flat Share", base_currency=self.usd)
        cat_s1 = create_category(
            user=self.user, name="Flat Rent", kind="expense", scope="shared", shared_space=space1
        )
        save_category_budget(
            user=self.user,
            category=cat_s1,
            period="2026-10",
            currency=self.usd,
            limit_amount=Decimal("800.00"),
            scope="shared",
            shared_space=space1,
        )
        # While in personal workspace, shared budget is excluded
        resp_personal = self.client.get(f"{self.dashboard_url}?period=2026-10")
        budget_panel_p = resp_personal.content.decode("utf-8").split('id="panel-budgets-title"')[1].split("</section>")[0]
        self.assertNotIn("Flat Rent", budget_panel_p)
        self.assertEqual(
            [r["category"].name for r in resp_personal.context["budget_overview"]["rows"]],
            ["Franz Books"],
        )

        # Switch to space1: Flat Rent appears, space2 and personal are excluded
        self.client.post(reverse("accounts:switch_scope"), {"view_choice": f"shared:{space1.id}"})
        space2 = create_shared_space(user=self.user, name="Project Share", base_currency=self.usd)
        cat_s2 = create_category(
            user=self.user, name="Server Hosting", kind="expense", scope="shared", shared_space=space2
        )
        save_category_budget(
            user=self.user,
            category=cat_s2,
            period="2026-10",
            currency=self.usd,
            limit_amount=Decimal("150.00"),
            scope="shared",
            shared_space=space2,
        )
        resp_s1 = self.client.get(f"{self.dashboard_url}?period=2026-10")
        html_s1 = resp_s1.content.decode("utf-8")
        budget_panel_s1 = html_s1.split('id="panel-budgets-title"')[1].split("</section>")[0]
        self.assertIn("Flat Rent", budget_panel_s1)
        self.assertNotIn("Server Hosting", budget_panel_s1)
        self.assertNotIn("Franz Books", budget_panel_s1)
        self.assertEqual(
            [r["category"].name for r in resp_s1.context["budget_overview"]["rows"]],
            ["Flat Rent"],
        )

        # 5. Another user's private budgets remain excluded
        self.client.post(reverse("accounts:switch_scope"), {"view_choice": "personal"})
        bob = User.objects.create_user(username="bob_privacy_check", password="Str0ng!Password#2026")
        cat_bob = create_category(user=bob, name="Bob Secret Category", kind="expense")
        save_category_budget(
            user=bob,
            category=cat_bob,
            period="2026-10",
            currency=twd,
            limit_amount=Decimal("9999.00"),
        )
        resp_p = self.client.get(f"{self.dashboard_url}?period=2026-10")
        html_p = resp_p.content.decode("utf-8")
        self.assertNotIn("Bob Secret Category", html_p)
        self.assertNotIn(
            "Bob Secret Category",
            [r["category"].name for r in resp_p.context["budget_overview"]["rows"]],
        )

        # 6. Explicit currency filtering on Budgets page still works
        cat_usd = create_category(user=self.user, name="Franz USD Gadgets", kind="expense")
        save_category_budget(
            user=self.user,
            category=cat_usd,
            period="2026-10",
            currency=self.usd,
            limit_amount=Decimal("300.00"),
        )
        b_twd = self.client.get(reverse("budgets:budgets") + "?period=2026-10&currency=TWD")
        self.assertEqual(b_twd.status_code, 200)
        html_b_twd = b_twd.content.decode("utf-8")
        table_twd = html_b_twd.split('aria-labelledby="category-budgets-heading"')[1].split("</section>")[0]
        self.assertIn("Franz Books", table_twd)
        self.assertNotIn("Franz USD Gadgets", table_twd)
        self.assertEqual([r["category"].name for r in b_twd.context["overview"]["rows"]], ["Franz Books"])

        b_usd = self.client.get(reverse("budgets:budgets") + "?period=2026-10&currency=USD&scope=personal")
        self.assertEqual(b_usd.status_code, 200)
        html_b_usd = b_usd.content.decode("utf-8")
        table_usd = html_b_usd.split('aria-labelledby="category-budgets-heading"')[1].split("</section>")[0]
        self.assertIn("Franz USD Gadgets", table_usd)
        self.assertNotIn("Franz Books", table_usd)
        self.assertEqual([r["category"].name for r in b_usd.context["overview"]["rows"]], ["Franz USD Gadgets"])

        # 7. Mixed-currency budgets keep summary unavailable with clear guidance and show overspent state
        resp_mixed = self.client.get(f"{self.dashboard_url}?period=2026-10")
        html_mixed = resp_mixed.content.decode("utf-8")
        self.assertIn("Franz Books", html_mixed)
        self.assertIn("Franz USD Gadgets", html_mixed)
        self.assertIn("Multi-currency budgets", html_mixed)

        create_transaction_entry(
            user=self.user,
            wallet=w_twd,
            transaction_type="expense",
            amount=Decimal("6000.00"),
            currency=twd,
            transaction_date=date(2026, 10, 15),
            category=cat_twd,
            description="Huge book haul",
        )
        resp_over = self.client.get(f"{self.dashboard_url}?period=2026-10")
        html_over = resp_over.content.decode("utf-8")
        self.assertIn("Over budget", html_over)

    def test_twd_budget_and_goal_displayed_in_php_then_usd(self):
        from .exchange_rates import record_manual_exchange_rate
        from .models import CategoryBudget, SavingsGoal
        from .services import (
            create_category,
            create_savings_goal,
            create_transaction_entry,
            record_savings_contribution,
            save_category_budget,
        )

        php, _ = Currency.objects.get_or_create(
            code="PHP", defaults={"name": "Philippine Peso", "decimal_places": 2, "is_active": True}
        )
        twd, _ = Currency.objects.get_or_create(
            code="TWD", defaults={"name": "New Taiwan Dollar", "decimal_places": 0, "is_active": True}
        )

        pref = UserPreference.objects.create(user=self.user, personal_base_currency=php)

        # 1 TWD = 1.75 PHP; 1 TWD = 0.03125 USD
        record_manual_exchange_rate(
            user=self.user,
            from_currency=twd,
            to_currency=php,
            rate=Decimal("1.75000000"),
            scope="personal",
        )
        record_manual_exchange_rate(
            user=self.user,
            from_currency=twd,
            to_currency=self.usd,
            rate=Decimal("0.03125000"),
            scope="personal",
        )

        w_twd = Wallet.objects.create(
            owner=self.user,
            name="Cash TWD",
            wallet_type=Wallet.WalletType.CASH,
            currency=twd,
            opening_balance=Decimal("50000.00"),
            opening_balance_date=date(2026, 10, 1),
        )
        cat_travel = create_category(user=self.user, name="TWD Travel", kind="expense")
        budget_twd = save_category_budget(
            user=self.user,
            category=cat_travel,
            period="2026-10",
            currency=twd,
            limit_amount=Decimal("10000.00"),
        )
        create_transaction_entry(
            user=self.user,
            wallet=w_twd,
            transaction_type="expense",
            amount=Decimal("4000.00"),
            currency=twd,
            transaction_date=date(2026, 10, 10),
            category=cat_travel,
            description="Train tickets",
        )

        goal_twd = create_savings_goal(
            user=self.user,
            name="Taipei Trip",
            target_amount=Decimal("20000.00"),
            currency=twd,
        )
        record_savings_contribution(
            user=self.user,
            goal=goal_twd,
            amount=Decimal("5000.00"),
            contribution_date=date(2026, 10, 12),
        )

        self.client.force_login(self.user)

        # View in PHP
        resp_php = self.client.get(f"{self.dashboard_url}?period=2026-10")
        self.assertEqual(resp_php.status_code, 200)
        html_php = resp_php.content.decode("utf-8")

        # Converted budget: Limit 10000*1.75 = 17,500.00 PHP, Spent 4000*1.75 = 7,000.00 PHP, Rem 10,500.00 PHP
        self.assertIn("17,500.00 PHP", html_php)
        self.assertIn("7,000.00", html_php)
        self.assertIn("Remaining: 10,500.00 PHP", html_php)
        self.assertIn("Orig: 4,000.00 / 10,000.00 TWD", html_php)
        self.assertIn("Orig: 6,000.00 TWD", html_php)
        self.assertIn("40.0%", html_php)

        # Converted goal: Target 20000*1.75 = 35,000.00 PHP, Saved 5000*1.75 = 8,750.00 PHP, Rem 26,250.00 PHP
        self.assertIn("35,000.00 PHP", html_php)
        self.assertIn("8,750.00", html_php)
        self.assertIn("Remaining: 26,250.00 PHP", html_php)
        self.assertIn("Orig: 5,000.00 / 20,000.00 TWD", html_php)
        self.assertIn("Orig: 15,000.00 TWD", html_php)
        self.assertIn("25.0% saved", html_php)

        # Summary strip in PHP
        self.assertIn("10,500.00", html_php)
        self.assertIn("PHP &middot; 1 active budget", html_php)

        # View in USD
        pref.personal_base_currency = self.usd
        pref.save()

        resp_usd = self.client.get(f"{self.dashboard_url}?period=2026-10")
        self.assertEqual(resp_usd.status_code, 200)
        html_usd = resp_usd.content.decode("utf-8")

        # Converted budget: Limit 10000*0.03125 = 312.50 USD, Spent 4000*0.03125 = 125.00 USD, Rem 187.50 USD
        self.assertIn("312.50 USD", html_usd)
        self.assertIn("125.00", html_usd)
        self.assertIn("Remaining: 187.50 USD", html_usd)
        self.assertIn("Orig: 4,000.00 / 10,000.00 TWD", html_usd)

        # Converted goal: Target 20000*0.03125 = 625.00 USD, Saved 5000*0.03125 = 156.25 USD, Rem 468.75 USD
        self.assertIn("625.00 USD", html_usd)
        self.assertIn("156.25", html_usd)
        self.assertIn("Remaining: 468.75 USD", html_usd)
        self.assertIn("Orig: 5,000.00 / 20,000.00 TWD", html_usd)

        # Summary strip in USD
        self.assertIn("187.50", html_usd)
        self.assertIn("USD &middot; 1 active budget", html_usd)

        # Verify original database records unchanged
        budget_db = CategoryBudget.objects.get(id=budget_twd.id)
        self.assertEqual(budget_db.limit_amount, Decimal("10000.000"))
        self.assertEqual(budget_db.currency, twd)

        goal_db = SavingsGoal.objects.get(id=goal_twd.id)
        self.assertEqual(goal_db.target_amount, Decimal("20000.000"))
        self.assertEqual(goal_db.currency, twd)

    def test_mixed_currency_budget_summary_and_missing_rates(self):
        from .exchange_rates import record_manual_exchange_rate
        from .models import ExchangeRate
        from .services import (
            create_category,
            create_transaction_entry,
            save_category_budget,
        )

        UserPreference.objects.create(user=self.user, personal_base_currency=self.usd)
        eur, _ = Currency.objects.get_or_create(
            code="EUR", defaults={"name": "Euro", "decimal_places": 2, "is_active": True}
        )

        w_usd = Wallet.objects.create(
            owner=self.user,
            name="W USD",
            wallet_type=Wallet.WalletType.CASH,
            currency=self.usd,
            opening_balance=Decimal("2000.00"),
            opening_balance_date=date(2026, 10, 1),
        )
        w_eur = Wallet.objects.create(
            owner=self.user,
            name="W EUR",
            wallet_type=Wallet.WalletType.CASH,
            currency=eur,
            opening_balance=Decimal("2000.00"),
            opening_balance_date=date(2026, 10, 1),
        )

        cat_usd = create_category(user=self.user, name="Dining USD", kind="expense")
        cat_eur = create_category(user=self.user, name="Travel EUR", kind="expense")

        save_category_budget(
            user=self.user, category=cat_usd, period="2026-10", currency=self.usd, limit_amount=Decimal("500.00")
        )
        create_transaction_entry(
            user=self.user,
            wallet=w_usd,
            transaction_type="expense",
            amount=Decimal("200.00"),
            currency=self.usd,
            transaction_date=date(2026, 10, 5),
            category=cat_usd,
            description="Dinner",
        )

        save_category_budget(
            user=self.user, category=cat_eur, period="2026-10", currency=eur, limit_amount=Decimal("200.00")
        )
        create_transaction_entry(
            user=self.user,
            wallet=w_eur,
            transaction_type="expense",
            amount=Decimal("50.00"),
            currency=eur,
            transaction_date=date(2026, 10, 6),
            category=cat_eur,
            description="Train",
        )

        # Complete rate: 1 EUR = 1.10 USD
        rate_record = record_manual_exchange_rate(
            user=self.user,
            from_currency=eur,
            to_currency=self.usd,
            rate=Decimal("1.10000000"),
            scope="personal",
        )

        self.client.force_login(self.user)
        resp_complete = self.client.get(f"{self.dashboard_url}?period=2026-10")
        self.assertEqual(resp_complete.status_code, 200)
        html_complete = resp_complete.content.decode("utf-8")

        # Converted EUR budget: 200 * 1.10 = 220.00 USD, spent 50 * 1.10 = 55.00 USD, rem 165.00 USD
        # USD budget: 500.00 USD, spent 200.00 USD, rem 300.00 USD
        # Total remaining = 300.00 + 165.00 = 465.00 USD
        self.assertIn("465.00", html_complete)
        self.assertIn("USD &middot; 2 active budgets", html_complete)
        self.assertIn("220.00 USD", html_complete)
        self.assertIn("Orig: 50.00 / 200.00 EUR", html_complete)

        # Now remove the exchange rate: missing rate scenario
        ExchangeRate.objects.filter(id=rate_record.id).delete()

        resp_missing = self.client.get(f"{self.dashboard_url}?period=2026-10")
        self.assertEqual(resp_missing.status_code, 200)
        html_missing = resp_missing.content.decode("utf-8")

        # Summary strip shows "—" and missing rate explanation; raw amounts (500+200=700) never summed
        self.assertNotIn("465.00", html_missing)
        self.assertNotIn("700.00", html_missing)
        self.assertIn("Multi-currency budgets &middot; missing exchange rate", html_missing)

        # EUR row shows original EUR currency with "Conversion unavailable" badge/label; never relabeled as USD
        budget_panel = html_missing.split('id="panel-budgets-title"')[1].split("</section>")[0]
        self.assertIn("200.00 EUR", budget_panel)
        self.assertNotIn("200.00 USD", budget_panel)
        self.assertIn("Conversion unavailable", budget_panel)

    def test_same_currency_negative_remaining_and_progress(self):
        from .services import create_category, create_transaction_entry, save_category_budget

        UserPreference.objects.create(user=self.user, personal_base_currency=self.usd)
        wallet = Wallet.objects.create(
            owner=self.user,
            name="Cash USD",
            wallet_type=Wallet.WalletType.CASH,
            currency=self.usd,
            opening_balance=Decimal("1000.00"),
            opening_balance_date=date(2026, 10, 1),
        )
        cat = create_category(user=self.user, name="Shopping", kind="expense")

        save_category_budget(
            user=self.user, category=cat, period="2026-10", currency=self.usd, limit_amount=Decimal("200.00")
        )
        create_transaction_entry(
            user=self.user,
            wallet=wallet,
            transaction_type="expense",
            amount=Decimal("350.00"),
            currency=self.usd,
            transaction_date=date(2026, 10, 15),
            category=cat,
            description="Clothes",
        )

        self.client.force_login(self.user)
        resp = self.client.get(f"{self.dashboard_url}?period=2026-10")
        html = resp.content.decode("utf-8")

        # Same currency: no "Orig:" line
        self.assertNotIn("Orig:", html)
        # Negative remaining budget: 200 - 350 = -150.00 USD
        self.assertIn("Remaining: -150.00 USD", html)
        self.assertIn("-150.00", html)
        self.assertIn("Over budget", html)
        # Progress 175.0%
        self.assertIn("status-bar-danger", html)

    def test_personal_shared_rate_authorization_and_workspace_isolation(self):
        from .exchange_rates import record_manual_exchange_rate
        from .services import (
            create_category,
            save_category_budget,
        )

        bob = User.objects.create_user(username="bob_rate_auth", password="Str0ng!Password#2026")
        UserPreference.objects.create(user=self.user, personal_base_currency=self.usd)
        UserPreference.objects.create(user=bob, personal_base_currency=self.usd)

        twd, _ = Currency.objects.get_or_create(
            code="TWD", defaults={"name": "New Taiwan Dollar", "decimal_places": 0, "is_active": True}
        )

        # Bob creates a private manual rate: 1 TWD = 0.50 USD
        record_manual_exchange_rate(
            user=bob,
            from_currency=twd,
            to_currency=self.usd,
            rate=Decimal("0.50000000"),
            scope="personal",
        )

        # self.user has a TWD budget
        cat = create_category(user=self.user, name="Personal TWD", kind="expense")
        save_category_budget(
            user=self.user, category=cat, period="2026-10", currency=twd, limit_amount=Decimal("1000.00")
        )

        self.client.force_login(self.user)
        resp = self.client.get(f"{self.dashboard_url}?period=2026-10")
        html = resp.content.decode("utf-8")

        # self.user must NOT use Bob's private personal rate -> Conversion unavailable
        self.assertIn("Conversion unavailable", html)
        self.assertNotIn("500.00 USD", html)


class GlobalToastNotificationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="toast_user",
            password="Str0ng!Password#2026",
        )
        self.usd, _ = Currency.objects.get_or_create(
            code="USD",
            defaults={"name": "US Dollar", "decimal_places": 2, "is_active": True},
        )
        self.dashboard_url = reverse("budgets:dashboard")
        self.switch_scope_url = reverse("accounts:switch_scope")

    def _find_browser_binary(self):
        import os
        candidates = [
            os.environ.get("CHROME_BIN"),
            os.environ.get("EDGE_BIN"),
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        ]
        for c in candidates:
            if c and os.path.exists(c):
                return c
        return None

    def test_workspace_switch_displays_one_toast_instead_of_page_banner(self):
        from .services import create_shared_space
        space = create_shared_space(user=self.user, name="Family Space", base_currency=self.usd)
        self.client.force_login(self.user)

        # POST to switch scope to the shared space
        response = self.client.post(
            self.switch_scope_url,
            {"view_choice": f"shared:{space.id}", "next": self.dashboard_url},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        html = response.content.decode("utf-8")

        # 1. Toast container is present
        self.assertIn('id="app-toast-container"', html)
        self.assertIn("toast-container", html)

        # 2. Displays exactly one toast for this switch
        self.assertEqual(html.count("app-toast-success"), 1)
        self.assertEqual(html.count('class="toast show app-toast'), 1)
        self.assertIn('class="toast show app-toast app-toast-success', html)
        self.assertIn('role="status"', html)
        self.assertIn('Switched workspace to Shared Space &quot;Family Space&quot;.', html)

        # 3. Toast attributes: auto-dismiss after ~4 seconds, polite semantics
        self.assertIn('data-bs-autohide="true"', html)
        self.assertIn('data-bs-delay="4000"', html)
        self.assertIn('aria-live="polite"', html)

        # 4. Accessible close button present
        self.assertIn('data-bs-dismiss="toast"', html)
        self.assertIn('aria-label="Close"', html)

        # 5. Page banner alert is NOT present in content
        self.assertNotIn('<div class="alert alert-success', html)
        self.assertNotIn('alert-danger', html)

        # 6. Persistent offline banner and review panel remain present and preserved
        self.assertIn('id="offline-sync-banner"', html)
        self.assertIn('id="offline-queue-review-panel"', html)

    def test_message_levels_semantics_and_persistence(self):
        from django.contrib.messages import success, info, warning, error
        from django.contrib.sessions.middleware import SessionMiddleware
        from django.contrib.messages.middleware import MessageMiddleware
        from django.test import RequestFactory
        from django.template.loader import render_to_string

        factory = RequestFactory()
        request = factory.get(self.dashboard_url)
        request.user = self.user
        SessionMiddleware(lambda req: None).process_request(request)
        MessageMiddleware(lambda req: None).process_request(request)

        success(request, "Transaction saved successfully.")
        info(request, "Display currency set to USD.")
        warning(request, "Exchange rate is older than 24 hours.")
        error(request, "Unable to connect to synchronization service.")

        html = render_to_string("base.html", {"page_title": "Test Page"}, request=request)

        # Verify container
        self.assertIn('id="app-toast-container"', html)

        # Verify routine success & info auto-hide and use polite status semantics
        self.assertIn("Transaction saved successfully.", html)
        self.assertIn("Display currency set to USD.", html)
        self.assertIn("app-toast-success", html)
        self.assertIn("app-toast-info", html)

        # Verify warning and error are persistent (data-bs-autohide="false") and use assertive alert semantics
        self.assertIn("Exchange rate is older than 24 hours.", html)
        self.assertIn("Unable to connect to synchronization service.", html)
        self.assertIn("app-toast-warning", html)
        self.assertIn("app-toast-error", html)
        self.assertIn('role="alert"', html)
        self.assertIn('aria-live="assertive"', html)
        self.assertIn('data-bs-autohide="false"', html)

        # Verify close buttons on each toast
        self.assertEqual(html.count('data-bs-dismiss="toast"'), 4)
        self.assertEqual(html.count('aria-label="Close"'), 4)

        # Verify rendered once only
        self.assertEqual(html.count("Transaction saved successfully."), 1)
        self.assertEqual(html.count("Unable to connect to synchronization service."), 1)

    def test_html_escaping_in_toast_messages(self):
        from django.contrib.messages import success
        from django.contrib.sessions.middleware import SessionMiddleware
        from django.contrib.messages.middleware import MessageMiddleware
        from django.test import RequestFactory
        from django.template.loader import render_to_string

        factory = RequestFactory()
        request = factory.get(self.dashboard_url)
        request.user = self.user
        SessionMiddleware(lambda req: None).process_request(request)
        MessageMiddleware(lambda req: None).process_request(request)

        success(request, "<script>alert('xss')</script><b>Dangerous</b> & unsafe \"text\"")

        html = render_to_string("base.html", {"page_title": "Test Page"}, request=request)

        # Must not contain unescaped script tag or bold tag
        self.assertNotIn("<script>alert('xss')</script>", html)
        self.assertNotIn("<b>Dangerous</b>", html)
        # Must contain escaped entities
        self.assertIn("&lt;script&gt;alert(&#x27;xss&#x27;)&lt;/script&gt;", html)
        self.assertIn("&lt;b&gt;Dangerous&lt;/b&gt;", html)

    def test_noscript_fallback_present_when_messages_exist(self):
        from django.contrib.messages import info
        from django.contrib.sessions.middleware import SessionMiddleware
        from django.contrib.messages.middleware import MessageMiddleware
        from django.test import RequestFactory
        from django.template.loader import render_to_string

        # 1. With messages -> noscript fallback included
        factory = RequestFactory()
        request = factory.get(self.dashboard_url)
        request.user = self.user
        SessionMiddleware(lambda req: None).process_request(request)
        MessageMiddleware(lambda req: None).process_request(request)
        info(request, "Offline changes pending.")

        html_with_msg = render_to_string("base.html", {"page_title": "Test Page"}, request=request)
        self.assertIn("<noscript>", html_with_msg)
        self.assertIn(".app-toast-container", html_with_msg)
        self.assertIn("position: static !important", html_with_msg)
        self.assertIn("display: block !important", html_with_msg)

        # 2. Without messages -> toast container and noscript not rendered
        request_empty = factory.get(self.dashboard_url)
        request_empty.user = self.user
        SessionMiddleware(lambda req: None).process_request(request_empty)
        MessageMiddleware(lambda req: None).process_request(request_empty)

        html_no_msg = render_to_string("base.html", {"page_title": "Test Page"}, request=request_empty)
        self.assertNotIn('id="app-toast-container"', html_no_msg)

    def test_server_messages_rendered_visibly_by_default(self):
        from django.contrib.messages import success, error
        from django.contrib.sessions.middleware import SessionMiddleware
        from django.contrib.messages.middleware import MessageMiddleware
        from django.test import RequestFactory
        from django.template.loader import render_to_string

        factory = RequestFactory()
        request = factory.get(self.dashboard_url)
        request.user = self.user
        SessionMiddleware(lambda req: None).process_request(request)
        MessageMiddleware(lambda req: None).process_request(request)

        success(request, "Default visible success note")
        error(request, "Default visible error note")

        html = render_to_string("base.html", {"page_title": "Test Page"}, request=request)
        # Server-rendered toasts must include 'show' class so they are displayed visibly without JS
        self.assertIn('class="toast show app-toast app-toast-success', html)
        self.assertIn('class="toast show app-toast app-toast-error', html)

    def test_login_and_register_pages_retain_existing_banners_not_toasts(self):
        from django.contrib.auth.models import AnonymousUser
        from django.contrib.messages import error, success
        from django.contrib.sessions.middleware import SessionMiddleware
        from django.contrib.messages.middleware import MessageMiddleware
        from django.test import RequestFactory
        from django.template.loader import render_to_string

        factory = RequestFactory()
        req_login = factory.get(reverse("accounts:login"))
        req_login.user = AnonymousUser()
        SessionMiddleware(lambda req: None).process_request(req_login)
        MessageMiddleware(lambda req: None).process_request(req_login)
        error(req_login, "Invalid login credentials.")

        html_login = render_to_string("accounts/login.html", request=req_login)
        # Login page retains existing alert banner
        self.assertIn("alert-danger", html_login)
        self.assertIn("Invalid login credentials.", html_login)
        # Login page does NOT use toast container or toasts
        self.assertNotIn("app-toast", html_login)
        self.assertNotIn("app-toast-container", html_login)

        req_reg = factory.get(reverse("accounts:register"))
        req_reg.user = AnonymousUser()
        SessionMiddleware(lambda req: None).process_request(req_reg)
        MessageMiddleware(lambda req: None).process_request(req_reg)
        success(req_reg, "Registration note.")

        html_reg = render_to_string("accounts/register.html", request=req_reg)
        self.assertIn("alert-success", html_reg)
        self.assertNotIn("app-toast", html_reg)
        self.assertNotIn("app-toast-container", html_reg)

    def test_form_validation_errors_remain_beside_their_fields(self):
        self.client.force_login(self.user)
        # Post invalid transaction (missing required amount)
        resp = self.client.post(
            reverse("budgets:transactions"),
            {"action": "create_transaction", "amount": "", "scope": "personal"},
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")
        # Form field error remains on the form / beside fields
        self.assertIn("This field is required.", html)

    def test_toast_browser_interaction_and_mobile_layout(self):
        import http.server
        import json
        import pathlib
        import shutil
        import socket
        import subprocess
        import tempfile
        import threading
        from django.conf import settings

        browser_bin = self._find_browser_binary()
        if not browser_bin:
            self.skipTest("No browser binary found for headless verification.")

        static_dir = pathlib.Path(settings.BASE_DIR) / "static"
        bs_js = (static_dir / "vendor" / "bootstrap" / "bootstrap.bundle.min.js").read_text(encoding="utf-8")
        bs_css = (static_dir / "vendor" / "bootstrap" / "bootstrap.min.css").read_text(encoding="utf-8")
        app_css = (static_dir / "css" / "app.css").read_text(encoding="utf-8")
        app_js = (static_dir / "js" / "app.js").read_text(encoding="utf-8")

        results_holder = {}
        done_event = threading.Event()

        harness_html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>{bs_css}</style>
    <style>{app_css}</style>
</head>
<body class="app-body">
    <div id="main-content">
        <div id="app-toast-container" class="toast-container position-fixed p-3 app-toast-container" aria-label="Notifications">
            <div id="toast-success" class="toast show app-toast app-toast-success fade" role="status" aria-live="polite" data-bs-autohide="true" data-bs-delay="600">
                <div class="d-flex align-items-center p-2">
                    <div class="toast-body app-toast-body p-0 me-2">Workspace switched</div>
                    <button id="btn-close-success" type="button" class="btn-close ms-auto flex-shrink-0" data-bs-dismiss="toast" aria-label="Close"></button>
                </div>
            </div>
            <div id="toast-error" class="toast show app-toast app-toast-error fade" role="alert" aria-live="assertive" data-bs-autohide="false">
                <div class="d-flex align-items-center p-2">
                    <div class="toast-body app-toast-body p-0 me-2">Persistent system error</div>
                    <button id="btn-close-error" type="button" class="btn-close ms-auto flex-shrink-0" data-bs-dismiss="toast" aria-label="Close"></button>
                </div>
            </div>
        </div>
    </div>
    <script>{bs_js}</script>
    <script>{app_js}</script>
    <script>
    window.addEventListener('DOMContentLoaded', async function () {{
        await new Promise(r => setTimeout(r, 100));

        var sToast = document.getElementById('toast-success');
        var eToast = document.getElementById('toast-error');
        var container = document.getElementById('app-toast-container');

        var sShown = sToast ? (getComputedStyle(sToast).display !== 'none' && sToast.classList.contains('show')) : false;
        var eShown = eToast ? (getComputedStyle(eToast).display !== 'none' && eToast.classList.contains('show')) : false;

        var containerRect = container.getBoundingClientRect();
        var fitsMobile = containerRect.right <= (window.innerWidth + 5);

        var closeErrorBtn = document.getElementById('btn-close-error');
        if (closeErrorBtn) {{
            closeErrorBtn.click();
        }}
        await new Promise(r => setTimeout(r, 300));
        var eHidden = eToast ? (!eToast.classList.contains('show') || getComputedStyle(eToast).display === 'none') : false;

        // Wait for auto-dismiss of success toast (delay was 600ms)
        await new Promise(r => setTimeout(r, 600));
        var sHiddenAfterDelay = sToast ? (!sToast.classList.contains('show') || getComputedStyle(sToast).display === 'none') : false;

        fetch('/__test_done__', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{
                sShown: sShown,
                eShown: eShown,
                fitsMobile: fitsMobile,
                eHiddenAfterClick: eHidden,
                sHiddenAfterDelay: sHiddenAfterDelay
            }})
        }}).catch(function () {{}});
    }});
    </script>
</body>
</html>"""

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                data = harness_html.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length).decode("utf-8")
                results_holder["report"] = json.loads(raw)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "11")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')
                done_event.set()

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()

        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
        server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        server_thread.start()

        user_data_dir = tempfile.mkdtemp(prefix="toast_browser_test_")
        proc = None
        try:
            proc = subprocess.Popen(
                [
                    browser_bin,
                    "--headless=new",
                    "--disable-gpu",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--window-size=375,667",
                    f"--user-data-dir={user_data_dir}",
                    f"http://127.0.0.1:{port}/",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            finished = done_event.wait(timeout=20)
            self.assertTrue(finished, "Headless browser toast verification timed out")
            rep = results_holder.get("report", {})
            self.assertTrue(rep.get("sShown"), "Success toast should be visible initially")
            self.assertTrue(rep.get("eShown"), "Error toast should be visible initially")
            self.assertTrue(rep.get("fitsMobile"), "Toast container should fit within mobile viewport width")
            self.assertTrue(rep.get("eHiddenAfterClick"), "Error toast should hide when close button is clicked")
            self.assertTrue(rep.get("sHiddenAfterDelay"), "Success toast should auto-dismiss after delay")
        finally:
            if proc:
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except Exception:
                    pass
            httpd.shutdown()
            httpd.server_close()
            shutil.rmtree(user_data_dir, ignore_errors=True)

    def test_toast_hover_and_focus_pause_and_resume_and_persistent_errors(self):
        import http.server
        import json
        import pathlib
        import shutil
        import socket
        import subprocess
        import tempfile
        import threading
        from django.conf import settings

        browser_bin = self._find_browser_binary()
        if not browser_bin:
            self.skipTest("No browser binary found for headless verification.")

        static_dir = pathlib.Path(settings.BASE_DIR) / "static"
        bs_js = (static_dir / "vendor" / "bootstrap" / "bootstrap.bundle.min.js").read_text(encoding="utf-8")
        bs_css = (static_dir / "vendor" / "bootstrap" / "bootstrap.min.css").read_text(encoding="utf-8")
        app_css = (static_dir / "css" / "app.css").read_text(encoding="utf-8")
        app_js = (static_dir / "js" / "app.js").read_text(encoding="utf-8")

        results_holder = {}
        done_event = threading.Event()

        harness_html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <style>{bs_css}</style>
    <style>{app_css}</style>
</head>
<body class="app-body">
    <div id="main-content">
        <div id="app-toast-container" class="toast-container position-fixed p-3 app-toast-container">
            <div id="toast-hover" class="toast show app-toast app-toast-success fade" role="status" aria-live="polite" data-bs-autohide="true" data-bs-delay="500">
                <div class="d-flex align-items-center p-2">
                    <div class="toast-body app-toast-body p-0 me-2">Hover pause test</div>
                    <button type="button" class="btn-close ms-auto flex-shrink-0" data-bs-dismiss="toast"></button>
                </div>
            </div>
            <div id="toast-focus" class="toast show app-toast app-toast-info fade" role="status" aria-live="polite" data-bs-autohide="true" data-bs-delay="500">
                <div class="d-flex align-items-center p-2">
                    <div class="toast-body app-toast-body p-0 me-2">Focus pause test</div>
                    <button id="focus-btn-close" type="button" class="btn-close ms-auto flex-shrink-0" data-bs-dismiss="toast"></button>
                </div>
            </div>
            <div id="toast-warning" class="toast show app-toast app-toast-warning fade" role="alert" aria-live="assertive" data-bs-autohide="false">
                <div class="d-flex align-items-center p-2">
                    <div class="toast-body app-toast-body p-0 me-2">Persistent warning</div>
                    <button id="btn-close-warn" type="button" class="btn-close ms-auto flex-shrink-0" data-bs-dismiss="toast"></button>
                </div>
            </div>
        </div>
    </div>
    <script>{bs_js}</script>
    <script>{app_js}</script>
    <script>
    window.addEventListener('DOMContentLoaded', async function () {{
        await new Promise(r => setTimeout(r, 50));

        var tHover = document.getElementById('toast-hover');
        var tFocus = document.getElementById('toast-focus');
        var tWarn = document.getElementById('toast-warning');

        // 1. Hover pause on tHover at 100ms
        await new Promise(r => setTimeout(r, 100));
        tHover.dispatchEvent(new MouseEvent('mouseenter', {{ bubbles: false }}));

        // 2. Focus pause on tFocus
        tFocus.dispatchEvent(new FocusEvent('focusin', {{ bubbles: true }}));

        // 3. Wait past the 500ms delay mark (at 700ms total)
        await new Promise(r => setTimeout(r, 600));
        var hoverStillShown = tHover.classList.contains('show') && getComputedStyle(tHover).display !== 'none';
        var focusStillShown = tFocus.classList.contains('show') && getComputedStyle(tFocus).display !== 'none';
        var warnStillShown = tWarn.classList.contains('show') && getComputedStyle(tWarn).display !== 'none';

        // 4. Resume hover: mouseleave
        tHover.dispatchEvent(new MouseEvent('mouseleave', {{ bubbles: false }}));

        // 5. Resume focus: focusout
        tFocus.dispatchEvent(new FocusEvent('focusout', {{ bubbles: true }}));

        // 6. Wait for remaining duration and transition to finish (~700ms)
        await new Promise(r => setTimeout(r, 700));
        var hoverDismissed = !tHover.classList.contains('show') || getComputedStyle(tHover).display === 'none';
        var focusDismissed = !tFocus.classList.contains('show') || getComputedStyle(tFocus).display === 'none';
        var warnPersistent = tWarn.classList.contains('show') && getComputedStyle(tWarn).display !== 'none';

        // 7. Click warning close button
        document.getElementById('btn-close-warn').click();
        await new Promise(r => setTimeout(r, 300));
        var warnDismissed = !tWarn.classList.contains('show') || getComputedStyle(tWarn).display === 'none';

        fetch('/__test_done__', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{
                hoverStillShown: hoverStillShown,
                focusStillShown: focusStillShown,
                warnStillShown: warnStillShown,
                hoverDismissed: hoverDismissed,
                focusDismissed: focusDismissed,
                warnPersistent: warnPersistent,
                warnDismissed: warnDismissed
            }})
        }}).catch(function () {{}});
    }});
    </script>
</body>
</html>"""

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                data = harness_html.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                results_holder["report"] = json.loads(self.rfile.read(length).decode("utf-8"))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "11")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')
                done_event.set()

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()

        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
        server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        server_thread.start()

        user_data_dir = tempfile.mkdtemp(prefix="toast_hover_test_")
        proc = None
        try:
            proc = subprocess.Popen(
                [
                    browser_bin,
                    "--headless=new",
                    "--disable-gpu",
                    "--no-first-run",
                    "--no-default-browser-check",
                    f"--user-data-dir={user_data_dir}",
                    f"http://127.0.0.1:{port}/",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            finished = done_event.wait(timeout=20)
            self.assertTrue(finished, "Headless browser hover/focus test timed out")
            rep = results_holder.get("report", {})
            self.assertTrue(rep.get("hoverStillShown"), "Toast should remain visible while hovered beyond initial delay")
            self.assertTrue(rep.get("focusStillShown"), "Toast should remain visible while focused beyond initial delay")
            self.assertTrue(rep.get("warnStillShown"), "Warning toast should remain visible beyond delay")
            self.assertTrue(rep.get("hoverDismissed"), "Toast should dismiss after mouseleave completes delay")
            self.assertTrue(rep.get("focusDismissed"), "Toast should dismiss after focusout completes delay")
            self.assertTrue(rep.get("warnPersistent"), "Warning toast should persist indefinitely until clicked")
            self.assertTrue(rep.get("warnDismissed"), "Warning toast should dismiss after close button click")
        finally:
            if proc:
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except Exception:
                    pass
            httpd.shutdown()
            httpd.server_close()
            shutil.rmtree(user_data_dir, ignore_errors=True)

    def test_toast_fallback_when_app_js_or_bootstrap_blocked(self):
        import http.server
        import json
        import pathlib
        import shutil
        import socket
        import subprocess
        import tempfile
        import threading
        from django.conf import settings

        browser_bin = self._find_browser_binary()
        if not browser_bin:
            self.skipTest("No browser binary found for headless verification.")

        static_dir = pathlib.Path(settings.BASE_DIR) / "static"
        bs_js = (static_dir / "vendor" / "bootstrap" / "bootstrap.bundle.min.js").read_text(encoding="utf-8")
        bs_css = (static_dir / "vendor" / "bootstrap" / "bootstrap.min.css").read_text(encoding="utf-8")
        app_css = (static_dir / "css" / "app.css").read_text(encoding="utf-8")
        app_js = (static_dir / "js" / "app.js").read_text(encoding="utf-8")

        def run_harness(include_bs_js, include_app_js):
            results = {}
            done = threading.Event()
            harness = f"""<!DOCTYPE html>
<html>
<head>
<style>{bs_css}</style>
<style>{app_css}</style>
</head>
<body class="app-body">
<div id="main-content">
  <div id="app-toast-container" class="toast-container position-fixed p-3 app-toast-container">
    <div id="fb-toast" class="toast show app-toast app-toast-info fade" role="status" data-bs-autohide="false">
      <div class="d-flex align-items-center p-2">
        <div class="toast-body app-toast-body p-0 me-2">Fallback test message</div>
        <button id="fb-close" type="button" class="btn-close ms-auto flex-shrink-0" data-bs-dismiss="toast" aria-label="Close"></button>
      </div>
    </div>
  </div>
</div>
{f'<script>{bs_js}</script>' if include_bs_js else ''}
{f'<script>{app_js}</script>' if include_app_js else ''}
<script>
window.addEventListener('DOMContentLoaded', async function () {{
    await new Promise(r => setTimeout(r, 100));
    var el = document.getElementById('fb-toast');
    var isVisible = el ? (getComputedStyle(el).display !== 'none' && el.classList.contains('show')) : false;
    var btn = document.getElementById('fb-close');
    if (btn) btn.click();
    await new Promise(r => setTimeout(r, 300));
    var isHiddenAfterClose = el ? (getComputedStyle(el).display === 'none' || !el.classList.contains('show')) : false;

    fetch('/__test_done__', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ isVisible: isVisible, isHiddenAfterClose: isHiddenAfterClose }})
    }}).catch(function () {{}});
}});
</script>
</body>
</html>"""

            class H(http.server.BaseHTTPRequestHandler):
                def log_message(self, *args): pass
                def do_GET(self):
                    d = harness.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(d)))
                    self.end_headers()
                    self.wfile.write(d)
                def do_POST(self):
                    l = int(self.headers.get("Content-Length", 0))
                    results["data"] = json.loads(self.rfile.read(l).decode("utf-8"))
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"ok":true}')
                    done.set()

            s = socket.socket()
            s.bind(("127.0.0.1", 0))
            p = s.getsockname()[1]
            s.close()
            srv = http.server.ThreadingHTTPServer(("127.0.0.1", p), H)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            tmp = tempfile.mkdtemp()
            pr = None
            try:
                pr = subprocess.Popen([
                    browser_bin,
                    "--headless=new",
                    "--disable-gpu",
                    f"--user-data-dir={tmp}",
                    f"http://127.0.0.1:{p}/"
                ])
                done.wait(timeout=15)
                return results.get("data", {})
            finally:
                if pr:
                    try:
                        pr.kill()
                        pr.wait(timeout=5)
                    except Exception: pass
                srv.shutdown()
                srv.server_close()
                shutil.rmtree(tmp, ignore_errors=True)

        # 1. Block app.js (Bootstrap JS only): server message must be visible and close button works
        rep_no_app = run_harness(include_bs_js=True, include_app_js=False)
        self.assertTrue(rep_no_app.get("isVisible"), "Message must remain visible when app.js is blocked")
        self.assertTrue(rep_no_app.get("isHiddenAfterClose"), "Close button should work via Bootstrap when app.js is blocked")

        # 2. Block Bootstrap JS (app.js only): server message must be visible and close button works via app.js fallback
        rep_no_bs = run_harness(include_bs_js=False, include_app_js=True)
        self.assertTrue(rep_no_bs.get("isVisible"), "Message must remain visible when Bootstrap JS is blocked")
        self.assertTrue(rep_no_bs.get("isHiddenAfterClose"), "Close button should work via app.js fallback when Bootstrap is blocked")

        # 3. Block both (pure CSS / no JS): server message must remain visible
        rep_no_js = run_harness(include_bs_js=False, include_app_js=False)
        self.assertTrue(rep_no_js.get("isVisible"), "Message must remain visible when all JS fails to load")


class CurrencyAmountFormatterTests(SimpleTestCase):
    def setUp(self):
        self.jpy = Currency(code="JPY", name="Japanese Yen", decimal_places=0)
        self.usd = Currency(code="USD", name="US Dollar", decimal_places=2)
        self.php = Currency(code="PHP", name="Philippine Peso", decimal_places=2)
        self.twd = Currency(code="TWD", name="New Taiwan Dollar", decimal_places=2)
        self.eur = Currency(code="EUR", name="Euro", decimal_places=2)
        self.kwd = Currency(code="KWD", name="Kuwaiti Dinar", decimal_places=3)

    def test_thousands_grouping_and_currency_precision(self):
        # Primary requirement cases
        self.assertEqual(format_currency_amount(Decimal("175140.59"), self.usd), "175,140.59")
        self.assertEqual(format_currency_amount(Decimal("-12345.67"), self.usd), "-12,345.67")

        # 2 decimal currencies (USD, PHP, TWD, EUR)
        self.assertEqual(format_currency_amount(Decimal("175140.59"), self.php), "175,140.59")
        self.assertEqual(format_currency_amount(Decimal("175140.59"), self.twd), "175,140.59")
        self.assertEqual(format_currency_amount(Decimal("175140.59"), self.eur), "175,140.59")

        # 0 decimal currencies (JPY)
        self.assertEqual(format_currency_amount(Decimal("15000"), self.jpy), "15,000")
        self.assertEqual(format_currency_amount(Decimal("1234567"), self.jpy), "1,234,567")
        self.assertEqual(format_currency_amount(Decimal("999"), self.jpy), "999")

        # 3 decimal currencies (KWD)
        self.assertEqual(format_currency_amount(Decimal("89.125"), self.kwd), "89.125")
        self.assertEqual(format_currency_amount(Decimal("12345.678"), self.kwd), "12,345.678")
        self.assertEqual(format_currency_amount(Decimal("175140.590"), self.kwd), "175,140.590")

    def test_negative_values(self):
        self.assertEqual(format_currency_amount(Decimal("-12345.67"), self.usd), "-12,345.67")
        self.assertEqual(format_currency_amount(Decimal("-245.75"), self.usd), "-245.75")
        self.assertEqual(format_currency_amount(Decimal("-50000"), self.jpy), "-50,000")
        self.assertEqual(format_currency_amount(Decimal("-12345.678"), self.kwd), "-12,345.678")

    def test_zero_values(self):
        self.assertEqual(format_currency_amount(Decimal("0"), self.usd), "0.00")
        self.assertEqual(format_currency_amount(Decimal("-0"), self.usd), "0.00")
        self.assertEqual(format_currency_amount(Decimal("0.00"), self.usd), "0.00")
        self.assertEqual(format_currency_amount(Decimal("0"), self.jpy), "0")
        self.assertEqual(format_currency_amount(Decimal("0"), self.kwd), "0.000")
        self.assertEqual(format_currency_amount(0, self.usd), "0.00")

    def test_unavailable_value_em_dashes(self):
        self.assertEqual(format_currency_amount(None, self.usd), "—")
        self.assertEqual(format_currency_amount(Decimal("100"), None), "—")
        self.assertEqual(format_currency_amount(None, None), "—")
        self.assertEqual(format_currency_amount("", self.usd), "—")
        self.assertEqual(format_currency_amount("invalid-amount", self.usd), "—")

    def test_rounding_preservation(self):
        self.assertEqual(format_currency_amount(Decimal("1234.566"), self.usd), "1,234.57")
        self.assertEqual(format_currency_amount(Decimal("1234.5"), self.jpy), "1,234")

    def test_plain_ungrouped_formatting_for_editable_inputs(self):
        self.assertEqual(
            format_currency_amount(Decimal("175140.59"), self.usd, grouping=False),
            "175140.59",
        )
        self.assertEqual(
            format_currency_amount(Decimal("-12345.67"), self.usd, grouping=False),
            "-12345.67",
        )
        self.assertEqual(
            format_currency_amount(Decimal("15000"), self.jpy, grouping=False),
            "15000",
        )


class MonetarySerializationBoundaryTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="serial_user",
            password="Str0ng!Password#2026",
        )
        self.usd, _ = Currency.objects.get_or_create(code="USD", defaults={"name": "US Dollar", "decimal_places": 2, "is_active": True})
        self.jpy, _ = Currency.objects.get_or_create(code="JPY", defaults={"name": "Japanese Yen", "decimal_places": 0, "is_active": True})
        self.kwd, _ = Currency.objects.get_or_create(code="KWD", defaults={"name": "Kuwaiti Dinar", "decimal_places": 3, "is_active": True})
        self.eur, _ = Currency.objects.get_or_create(code="EUR", defaults={"name": "Euro", "decimal_places": 2, "is_active": True})

        self.wallet_usd = Wallet.objects.create(
            owner=self.user,
            name="USD Main Wallet",
            wallet_type=Wallet.WalletType.BANK_ACCOUNT,
            currency=self.usd,
            opening_balance=Decimal("175140.59"),
            opening_balance_date=date(2026, 1, 1),
        )
        self.wallet_jpy = Wallet.objects.create(
            owner=self.user,
            name="JPY Cash Wallet",
            wallet_type=Wallet.WalletType.CASH,
            currency=self.jpy,
            opening_balance=Decimal("15000"),
            opening_balance_date=date(2026, 1, 1),
        )
        self.wallet_kwd = Wallet.objects.create(
            owner=self.user,
            name="KWD Savings Wallet",
            wallet_type=Wallet.WalletType.OTHER,
            currency=self.kwd,
            opening_balance=Decimal("1089.125"),
            opening_balance_date=date(2026, 1, 1),
        )

    def test_model_properties_grouping_vs_ungrouped(self):
        from apps.budgets.models import Transaction, Transfer, CategoryBudget, SavingsGoal, SavingsContribution
        from apps.budgets.models import Category

        # Wallet opening & current balances
        self.assertEqual(self.wallet_usd.formatted_opening_balance, "175,140.59")
        self.assertEqual(self.wallet_usd.plain_opening_balance, "175140.59")
        self.assertEqual(self.wallet_usd.formatted_current_balance, "175,140.59")
        self.assertEqual(self.wallet_usd.plain_current_balance, "175140.59")

        self.assertEqual(self.wallet_jpy.formatted_opening_balance, "15,000")
        self.assertEqual(self.wallet_jpy.plain_opening_balance, "15000")

        self.assertEqual(self.wallet_kwd.formatted_opening_balance, "1,089.125")
        self.assertEqual(self.wallet_kwd.plain_opening_balance, "1089.125")

        # Transaction amounts (positive > 1000, negative, 0/2/3 decimals, converted amount, null converted)
        tx_pos = Transaction.objects.create(
            owner=self.user,
            wallet=self.wallet_usd,
            transaction_type=Transaction.Type.INCOME,
            amount=Decimal("175140.59"),
            currency=self.usd,
            base_currency=self.eur,
            converted_amount=Decimal("160000.50"),
            transaction_date=date(2026, 10, 1),
            description="Big consulting income",
        )
        self.assertEqual(tx_pos.formatted_amount, "175,140.59")
        self.assertEqual(tx_pos.plain_amount, "175140.59")
        self.assertEqual(tx_pos.formatted_converted_amount, "160,000.50")
        self.assertEqual(tx_pos.plain_converted_amount, "160000.50")

        tx_neg = Transaction.objects.create(
            owner=self.user,
            wallet=self.wallet_usd,
            transaction_type=Transaction.Type.EXPENSE,
            amount=Decimal("-12345.67"),
            currency=self.usd,
            transaction_date=date(2026, 10, 2),
            description="Negative adjustment",
        )
        self.assertEqual(tx_neg.formatted_amount, "-12,345.67")
        self.assertEqual(tx_neg.plain_amount, "-12345.67")
        self.assertIsNone(tx_neg.formatted_converted_amount)
        self.assertIsNone(tx_neg.plain_converted_amount)

        tx_jpy = Transaction.objects.create(
            owner=self.user,
            wallet=self.wallet_jpy,
            transaction_type=Transaction.Type.EXPENSE,
            amount=Decimal("15000"),
            currency=self.jpy,
            transaction_date=date(2026, 10, 3),
            description="Dinner in Tokyo",
        )
        self.assertEqual(tx_jpy.formatted_amount, "15,000")
        self.assertEqual(tx_jpy.plain_amount, "15000")

        tx_kwd = Transaction.objects.create(
            owner=self.user,
            wallet=self.wallet_kwd,
            transaction_type=Transaction.Type.INCOME,
            amount=Decimal("89.125"),
            currency=self.kwd,
            transaction_date=date(2026, 10, 4),
            description="Dividend",
        )
        self.assertEqual(tx_kwd.formatted_amount, "89.125")
        self.assertEqual(tx_kwd.plain_amount, "89.125")

        # Transfer amounts and fees
        transfer = Transfer.objects.create(
            created_by=self.user,
            source_wallet=self.wallet_usd,
            destination_wallet=self.wallet_usd,
            source_currency=self.usd,
            destination_currency=self.usd,
            amount_sent=Decimal("12345.67"),
            amount_received=Decimal("12300.00"),
            fee_amount=Decimal("45.67"),
            transfer_date=date(2026, 10, 5),
        )
        self.assertEqual(transfer.formatted_amount_sent, "12,345.67")
        self.assertEqual(transfer.plain_amount_sent, "12345.67")
        self.assertEqual(transfer.formatted_amount_received, "12,300.00")
        self.assertEqual(transfer.plain_amount_received, "12300.00")
        self.assertEqual(transfer.formatted_fee_amount, "45.67")
        self.assertEqual(transfer.plain_fee_amount, "45.67")

        # CategoryBudget
        cat = Category.objects.create(name="Groceries", owner=self.user)
        budget = CategoryBudget.objects.create(
            owner=self.user,
            category=cat,
            currency=self.usd,
            limit_amount=Decimal("2500.00"),
            period="2026-10",
        )
        self.assertEqual(budget.formatted_limit_amount, "2,500.00")
        self.assertEqual(budget.plain_limit_amount, "2500.00")

        # SavingsGoal & Contribution
        goal = SavingsGoal.objects.create(
            owner=self.user,
            name="Emergency Fund",
            currency=self.usd,
            target_amount=Decimal("50000.00"),
        )
        self.assertEqual(goal.formatted_target_amount, "50,000.00")
        self.assertEqual(goal.plain_target_amount, "50000.00")
        self.assertEqual(goal.formatted_saved_amount, "0.00")
        self.assertEqual(goal.plain_saved_amount, "0.00")

        contrib = SavingsContribution.objects.create(
            goal=goal,
            created_by=self.user,
            amount=Decimal("1500.00"),
            contribution_date=date(2026, 10, 6),
        )
        self.assertEqual(contrib.formatted_amount, "1,500.00")
        self.assertEqual(contrib.plain_amount, "1500.00")
        self.assertEqual(goal.formatted_saved_amount, "1,500.00")
        self.assertEqual(goal.plain_saved_amount, "1500.00")
        self.assertEqual(goal.formatted_remaining_amount, "48,500.00")
        self.assertEqual(goal.plain_remaining_amount, "48500.00")

    def test_null_and_empty_handling(self):
        from apps.budgets.models import Transaction, Transfer, Wallet, SavingsGoal

        tx_null = Transaction(owner=self.user, wallet=self.wallet_usd, currency=self.usd)
        self.assertEqual(tx_null.plain_amount, "")
        self.assertIsNone(tx_null.plain_converted_amount)

        w_null = Wallet(owner=self.user, currency=self.usd)
        self.assertEqual(w_null.plain_opening_balance, "")
        self.assertEqual(w_null.plain_current_balance, "")

        tr_null = Transfer(created_by=self.user)
        self.assertEqual(tr_null.plain_amount_sent, "")
        self.assertEqual(tr_null.plain_amount_received, "")
        self.assertEqual(tr_null.plain_fee_amount, "")

        g_null = SavingsGoal(owner=self.user, currency=self.usd)
        self.assertEqual(g_null.plain_target_amount, "")

    def test_transactions_export_csv_and_json_serialization(self):
        from apps.budgets.models import Transaction
        from apps.budgets.selectors import build_transactions_export_rows

        Transaction.objects.create(
            owner=self.user,
            wallet=self.wallet_usd,
            transaction_type=Transaction.Type.INCOME,
            amount=Decimal("175140.59"),
            currency=self.usd,
            base_currency=self.eur,
            converted_amount=Decimal("160000.50"),
            transaction_date=timezone.localdate(),
            description="Large client payment",
        )
        Transaction.objects.create(
            owner=self.user,
            wallet=self.wallet_usd,
            transaction_type=Transaction.Type.EXPENSE,
            amount=Decimal("-12345.67"),
            currency=self.usd,
            transaction_date=timezone.localdate(),
            description="Chargeback",
        )
        Transaction.objects.create(
            owner=self.user,
            wallet=self.wallet_jpy,
            transaction_type=Transaction.Type.EXPENSE,
            amount=Decimal("15000"),
            currency=self.jpy,
            transaction_date=timezone.localdate(),
            description="Hotel reservation",
        )
        Transaction.objects.create(
            owner=self.user,
            wallet=self.wallet_kwd,
            transaction_type=Transaction.Type.EXPENSE,
            amount=Decimal("1089.125"),
            currency=self.kwd,
            transaction_date=timezone.localdate(),
            description="Fuel",
        )

        rows = build_transactions_export_rows(self.user, {})
        row_by_desc = {r["description"]: r for r in rows}

        self.assertEqual(row_by_desc["Large client payment"]["amount"], "175140.59")
        self.assertEqual(row_by_desc["Large client payment"]["converted_amount"], "160000.50")
        self.assertEqual(row_by_desc["Chargeback"]["amount"], "-12345.67")
        self.assertEqual(row_by_desc["Chargeback"]["converted_amount"], "")
        self.assertEqual(row_by_desc["Hotel reservation"]["amount"], "15000")
        self.assertEqual(row_by_desc["Fuel"]["amount"], "1089.125")

        self.client.force_login(self.user)

        # CSV endpoint
        csv_resp = self.client.get(reverse("budgets:export_transactions") + "?format=csv")
        self.assertEqual(csv_resp.status_code, 200)
        csv_content = csv_resp.content.decode("utf-8")
        self.assertIn("175140.59", csv_content)
        self.assertNotIn("175,140.59", csv_content)
        self.assertIn("-12345.67", csv_content)
        self.assertNotIn("-12,345.67", csv_content)
        self.assertIn("15000", csv_content)
        self.assertNotIn("15,000", csv_content)
        self.assertIn("1089.125", csv_content)
        self.assertNotIn("1,089.125", csv_content)

        # JSON endpoint
        json_resp = self.client.get(reverse("budgets:export_transactions") + "?format=json")
        self.assertEqual(json_resp.status_code, 200)
        json_data = json_resp.json()
        exported_txs = {t["description"]: t for t in json_data["transactions"]}
        self.assertEqual(exported_txs["Large client payment"]["amount"], "175140.59")
        self.assertEqual(exported_txs["Large client payment"]["converted_amount"], "160000.50")
        self.assertEqual(exported_txs["Chargeback"]["amount"], "-12345.67")
        self.assertEqual(exported_txs["Chargeback"]["converted_amount"], "")
        self.assertEqual(exported_txs["Hotel reservation"]["amount"], "15000")
        self.assertEqual(exported_txs["Fuel"]["amount"], "1089.125")

    def test_sync_snapshot_and_account_export_serialization(self):
        from apps.budgets.models import Transaction, SavingsGoal, SavingsContribution
        from apps.budgets.selectors import get_sync_snapshot_for_user

        Transaction.objects.create(
            owner=self.user,
            wallet=self.wallet_usd,
            transaction_type=Transaction.Type.INCOME,
            amount=Decimal("175140.59"),
            currency=self.usd,
            transaction_date=date(2026, 10, 1),
            description="Snapshot tx",
        )
        goal = SavingsGoal.objects.create(
            owner=self.user,
            name="House Downpayment",
            currency=self.usd,
            target_amount=Decimal("250000.00"),
        )
        SavingsContribution.objects.create(
            goal=goal,
            created_by=self.user,
            amount=Decimal("50000.00"),
            contribution_date=date(2026, 10, 2),
        )

        snapshot = get_sync_snapshot_for_user(self.user)
        snap_wallets = {w["name"]: w for w in snapshot["wallets"]}
        snap_txs = {t["description"]: t for t in snapshot["transactions"]}

        # Check wallets in sync snapshot have ungrouped balances
        self.assertEqual(snap_wallets["USD Main Wallet"]["opening_balance"], "175140.59")
        self.assertEqual(snap_wallets["USD Main Wallet"]["current_balance"], "350281.18")
        self.assertEqual(snap_wallets["JPY Cash Wallet"]["opening_balance"], "15000")
        self.assertEqual(snap_wallets["KWD Savings Wallet"]["opening_balance"], "1089.125")
        # Check transactions in sync snapshot have ungrouped amount
        self.assertEqual(snap_txs["Snapshot tx"]["amount"], "175140.59")

        # Decimal parses cleanly without ValueError from comma
        self.assertEqual(Decimal(snap_wallets["USD Main Wallet"]["current_balance"]), Decimal("350281.18"))
        self.assertEqual(Decimal(snap_txs["Snapshot tx"]["amount"]), Decimal("175140.59"))

        # Account export endpoint (/accounts/export/?format=json)
        self.client.force_login(self.user)
        resp = self.client.get(reverse("accounts:export_data") + "?format=json")
        self.assertEqual(resp.status_code, 200)
        acc_data = resp.json()

        acc_wallets = {w["name"]: w for w in acc_data["wallets"]}
        self.assertEqual(acc_wallets["USD Main Wallet"]["opening_balance"], "175140.59")
        self.assertEqual(acc_wallets["USD Main Wallet"]["current_balance"], "350281.18")
        self.assertEqual(acc_wallets["JPY Cash Wallet"]["opening_balance"], "15000")
        self.assertEqual(acc_wallets["KWD Savings Wallet"]["opening_balance"], "1089.125")

        acc_goals = {g["name"]: g for g in acc_data["savings_goals"]}
        self.assertEqual(acc_goals["House Downpayment"]["target_amount"], "250000.00")
        self.assertEqual(acc_goals["House Downpayment"]["saved_amount"], "50000.00")
        self.assertEqual(acc_goals["House Downpayment"]["remaining_amount"], "200000.00")

    def test_contrast_website_html_displays_grouping_separators(self):
        from apps.budgets.models import Transaction

        Transaction.objects.create(
            owner=self.user,
            wallet=self.wallet_usd,
            transaction_type=Transaction.Type.INCOME,
            amount=Decimal("175140.59"),
            currency=self.usd,
            transaction_date=timezone.localdate(),
            description="Paycheck",
        )
        self.client.force_login(self.user)

        # Website transactions page must render with comma separators
        tx_resp = self.client.get(reverse("budgets:transactions"))
        self.assertEqual(tx_resp.status_code, 200)
        self.assertContains(tx_resp, "175,140.59")

        # Website wallets page must render with comma separators
        w_resp = self.client.get(reverse("budgets:wallets"))
        self.assertEqual(w_resp.status_code, 200)
        self.assertContains(w_resp, "175,140.59")


class GlobalNavbarRestylingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="navbar_tester",
            password="Str0ng!Password#2026",
        )
        self.usd, _ = Currency.objects.get_or_create(
            code="USD",
            defaults={"name": "US Dollar", "decimal_places": 2, "is_active": True},
        )
        self.eur, _ = Currency.objects.get_or_create(
            code="EUR",
            defaults={"name": "Euro", "decimal_places": 2, "is_active": True},
        )
        self.dashboard_url = reverse("budgets:dashboard")

    def test_navbar_rendering_authenticated(self):
        self.client.force_login(self.user)
        resp = self.client.get(self.dashboard_url)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")

        # Topbar container & sticky positioning
        self.assertIn('class="app-topbar sticky-top"', html)

        # 1. Single h1 page title on the left
        self.assertEqual(html.count("<h1"), 1)
        self.assertIn('class="topbar-page-title', html)
        self.assertIn("Dashboard</h1>", html)

        # 2. Mobile sidebar toggle
        self.assertIn('btn-topbar-menu', html)
        self.assertIn('data-bs-toggle="offcanvas"', html)
        self.assertIn('data-bs-target="#appSidebar"', html)
        self.assertIn('aria-label="Open navigation menu"', html)

        # 3. Labeled Workspace selector
        self.assertIn('id="workspace-view-form"', html)
        self.assertIn(f'action="{reverse("accounts:switch_scope")}"', html)
        self.assertIn('for="workspace-view-select"', html)
        self.assertIn('class="topbar-select-label', html)
        self.assertIn('id="workspace-view-select"', html)
        self.assertIn('name="view_choice"', html)
        self.assertIn('onchange="this.form.requestSubmit()"', html)

        # 4. Labeled Display currency selector
        self.assertIn('id="display-currency-form"', html)
        self.assertIn(f'action="{reverse("accounts:set_display_currency")}"', html)
        self.assertIn('for="display-currency-select"', html)
        self.assertIn('id="display-currency-select"', html)
        self.assertIn('name="currency_code"', html)
        self.assertIn('onchange="this.form.requestSubmit()"', html)

        # 5. Navy "+ Add transaction" button
        self.assertIn('btn-topbar-action', html)
        self.assertIn('data-bs-target="#transactionModal"', html)
        self.assertIn("Add transaction", html)

        # 6. Compact account dropdown
        self.assertIn('topbar-account-dropdown', html)
        self.assertIn('id="topbarAccountDropdown"', html)
        self.assertIn('data-bs-toggle="dropdown"', html)
        # Circular initial avatar with first letter capitalized
        self.assertIn('class="topbar-avatar"', html)
        self.assertIn('>N<', html)
        # Username
        self.assertIn('class="topbar-username', html)
        self.assertIn("navbar_tester", html)
        # Dropdown menu items
        self.assertIn('topbar-dropdown-menu', html)
        self.assertIn("Signed in as", html)
        self.assertIn(reverse("accounts:settings"), html)
        self.assertIn("Settings", html)

        # 7. Offline queue badge preserved
        self.assertIn('id="offline-queue-badge"', html)

        # 8. Topbar and sidebar logout forms present
        self.assertIn('id="topbar-logout-form"', html)
        self.assertIn('id="sidebar-logout-form"', html)
        self.assertIn('Log out', html)

    def test_navbar_workspace_and_currency_switching(self):
        from .services import create_shared_space

        space = create_shared_space(user=self.user, name="Team Space", base_currency=self.usd)
        self.client.force_login(self.user)

        # Switch workspace
        switch_resp = self.client.post(
            reverse("accounts:switch_scope"),
            {"view_choice": f"shared:{space.id}", "next": self.dashboard_url},
            follow=True,
        )
        self.assertEqual(switch_resp.status_code, 200)
        switch_html = switch_resp.content.decode("utf-8")
        self.assertIn(f'value="shared:{space.id}" selected', switch_html)

        # Set display currency
        curr_resp = self.client.post(
            reverse("accounts:set_display_currency"),
            {"currency_code": "EUR", "next": self.dashboard_url},
            follow=True,
        )
        self.assertEqual(curr_resp.status_code, 200)
        curr_html = curr_resp.content.decode("utf-8")
        self.assertIn('value="EUR" selected', curr_html)

    def test_navbar_unauthenticated(self):
        resp = self.client.get(reverse("accounts:login"))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")

        # Public auth pages do not render private selector forms or account dropdown
        self.assertNotIn('id="workspace-view-form"', html)
        self.assertNotIn('id="display-currency-form"', html)
        self.assertNotIn('id="topbarAccountDropdown"', html)

    def test_navbar_responsive_layout_in_headless_browser(self):
        import http.server
        import json
        import os
        import shutil
        import socket
        import subprocess
        import tempfile
        import threading

        browser_bin = OfflineSyncBrowserRuntimeTests._find_browser_binary()
        if not browser_bin:
            self.skipTest("No Chrome/Edge browser binary found for headless inspection")

        self.client.force_login(self.user)
        resp = self.client.get(self.dashboard_url)
        html_content = resp.content.decode("utf-8")

        temp_dir = tempfile.mkdtemp(prefix="navbar_test_")
        try:
            doc_file = os.path.join(temp_dir, "index.html")
            with open(doc_file, "w", encoding="utf-8") as f:
                f.write(html_content)

            static_dir = os.path.join(temp_dir, "static")
            repo_static = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "static")
            shutil.copytree(repo_static, static_dir)

            results_holder = {}
            done_event = threading.Event()

            class QuietHandler(http.server.SimpleHTTPRequestHandler):
                def __init__(self, *args, **kwargs):
                    super().__init__(*args, directory=temp_dir, **kwargs)

                def log_message(self, format, *args):
                    pass

                def do_POST(self):
                    if self.path == "/__test_done__":
                        length = int(self.headers.get("Content-Length", "0"))
                        raw = self.rfile.read(length).decode("utf-8")
                        results_holder["report"] = json.loads(raw)
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", "11")
                        self.end_headers()
                        self.wfile.write(b'{"ok":true}')
                        done_event.set()
                    else:
                        self.send_response(404)
                        self.end_headers()

            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            sock.close()

            httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), QuietHandler)
            server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            server_thread.start()

            for width, height, is_mobile in [(1280, 800, False), (375, 667, True)]:
                done_event.clear()
                results_holder.clear()

                runner_html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0; padding:0;">
<iframe id="test-frame" src="http://127.0.0.1:{port}/index.html" style="width:{width}px; height:{height}px; border:none;"></iframe>
<script>
var frame = document.getElementById('test-frame');
frame.onload = function() {{
    setTimeout(function() {{
        try {{
            var doc = frame.contentDocument;
            var topbar = doc.querySelector('.app-topbar');
            var pageTitle = doc.querySelector('.topbar-page-title');
            var menuBtn = doc.querySelector('.btn-topbar-menu');
            var actionBtn = doc.querySelector('.btn-topbar-action');
            var accountDropdown = doc.querySelector('.topbar-account-dropdown');
            var menuDisplay = menuBtn ? frame.contentWindow.getComputedStyle(menuBtn).display : 'none';
            var scrollW = doc.documentElement.scrollWidth;
            var clientW = doc.documentElement.clientWidth;

            var result = {{
                width: {width},
                is_mobile: {str(is_mobile).lower()},
                has_topbar: !!topbar,
                topbar_height: topbar ? topbar.getBoundingClientRect().height : 0,
                has_title: !!pageTitle,
                has_action_btn: !!actionBtn,
                has_account_dropdown: !!accountDropdown,
                menu_btn_visible: menuDisplay !== 'none',
                zero_overflow: scrollW <= clientW + 2,
                scrollW: scrollW,
                clientW: clientW
            }};
            fetch('/__test_done__', {{ method: 'POST', body: JSON.stringify(result) }});
        }} catch(err) {{
            fetch('/__test_done__', {{ method: 'POST', body: JSON.stringify({{error: err.toString()}}) }});
        }}
    }}, 400);
}};
</script>
</body>
</html>"""
                runner_path = os.path.join(temp_dir, f"runner_{width}.html")
                with open(runner_path, "w", encoding="utf-8") as rf:
                    rf.write(runner_html)

                proc = subprocess.Popen(
                    [
                        browser_bin,
                        "--headless=new",
                        "--disable-gpu",
                        "--no-first-run",
                        "--no-default-browser-check",
                        f"--window-size={width},{height}",
                        f"http://127.0.0.1:{port}/runner_{width}.html",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                try:
                    done_event.wait(timeout=10)
                finally:
                    proc.kill()
                    proc.wait()

                self.assertIn("report", results_holder, f"Headless browser timed out at {width}x{height}")
                data = results_holder["report"]
                self.assertNotIn("error", data, f"Browser inspection error: {data.get('error')}")

                self.assertTrue(data["has_topbar"])
                self.assertTrue(data["has_title"])
                self.assertTrue(data["has_action_btn"])
                self.assertTrue(data["has_account_dropdown"])
                self.assertTrue(data["zero_overflow"], f"Horizontal overflow at {width}px: scrollW={data['scrollW']}, clientW={data['clientW']}")
                if is_mobile:
                    self.assertTrue(data["menu_btn_visible"], "Mobile sidebar toggle should be visible on 375px")
                else:
                    self.assertFalse(data["menu_btn_visible"], "Mobile sidebar toggle should be hidden on 1280px")

            httpd.shutdown()
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


class DashboardRecentTransactionsConversionAndMonthSelectorTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="recent_tx_user",
            password="Str0ng!Password#2026",
        )
        self.usd, _ = Currency.objects.get_or_create(
            code="USD",
            defaults={"name": "US Dollar", "decimal_places": 2, "is_active": True},
        )
        self.eur, _ = Currency.objects.get_or_create(
            code="EUR",
            defaults={"name": "Euro", "decimal_places": 2, "is_active": True},
        )
        self.twd, _ = Currency.objects.get_or_create(
            code="TWD",
            defaults={"name": "New Taiwan Dollar", "decimal_places": 2, "is_active": True},
        )
        self.dashboard_url = reverse("budgets:dashboard")

    def test_month_selector_single_arrow_and_accessibility(self):
        self.client.force_login(self.user)
        resp = self.client.get(self.dashboard_url)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")

        # Duplicate custom chevron SVG removed
        self.assertNotIn("period-chevron-icon", html)

        # Single select remains with accessible attributes and submission binding
        self.assertIn('class="period-select"', html)
        self.assertIn('name="period"', html)
        self.assertIn('aria-label="Select month"', html)

    def test_recent_transactions_foreign_currency_conversion_and_signs(self):
        from .exchange_rates import record_manual_exchange_rate
        from .services import create_category, create_transaction_entry

        UserPreference.objects.create(user=self.user, personal_base_currency=self.usd)
        w_eur = Wallet.objects.create(
            owner=self.user,
            name="Euro Wallet",
            wallet_type=Wallet.WalletType.CASH,
            currency=self.eur,
            opening_balance=Decimal("1000.00"),
            opening_balance_date=date(2026, 10, 1),
        )
        cat_food = create_category(user=self.user, name="Food", kind="both")

        # 1 EUR = 1.10 USD
        record_manual_exchange_rate(
            user=self.user,
            from_currency=self.eur,
            to_currency=self.usd,
            rate=Decimal("1.10000000"),
            scope="personal",
        )

        # Expense: 50.00 EUR -> Converted: 55.00 USD
        tx_expense = create_transaction_entry(
            user=self.user,
            wallet=w_eur,
            transaction_type="expense",
            amount=Decimal("50.00"),
            currency=self.eur,
            transaction_date=date(2026, 10, 15),
            category=cat_food,
            description="Paris Lunch",
        )
        # Income: 100.00 EUR -> Converted: 110.00 USD
        tx_income = create_transaction_entry(
            user=self.user,
            wallet=w_eur,
            transaction_type="income",
            amount=Decimal("100.00"),
            currency=self.eur,
            transaction_date=date(2026, 10, 16),
            category=cat_food,
            description="Consulting EUR",
        )

        self.client.force_login(self.user)
        resp = self.client.get(f"{self.dashboard_url}?period=2026-10")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")

        # Converted expense: primary -55.00 USD, secondary Orig: -50.00 EUR
        self.assertIn("-55.00 USD", html)
        self.assertIn("Orig: -50.00 EUR", html)

        # Converted income: primary +110.00 USD, secondary Orig: +100.00 EUR
        self.assertIn("+110.00 USD", html)
        self.assertIn("Orig: +100.00 EUR", html)

        # Verify original database records and snapshots untouched
        tx_expense.refresh_from_db()
        tx_income.refresh_from_db()
        self.assertEqual(tx_expense.amount, Decimal("50.000"))
        self.assertEqual(tx_expense.currency, self.eur)
        self.assertEqual(tx_income.amount, Decimal("100.000"))
        self.assertEqual(tx_income.currency, self.eur)

    def test_recent_transactions_same_currency_no_secondary_line(self):
        from .services import create_category, create_transaction_entry

        UserPreference.objects.create(user=self.user, personal_base_currency=self.usd)
        w_usd = Wallet.objects.create(
            owner=self.user,
            name="USD Wallet",
            wallet_type=Wallet.WalletType.CASH,
            currency=self.usd,
            opening_balance=Decimal("2000.00"),
            opening_balance_date=date(2026, 10, 1),
        )
        cat_usd = create_category(user=self.user, name="Local", kind="expense")

        create_transaction_entry(
            user=self.user,
            wallet=w_usd,
            transaction_type="expense",
            amount=Decimal("250.00"),
            currency=self.usd,
            transaction_date=date(2026, 10, 10),
            category=cat_usd,
            description="Office Supplies",
        )

        self.client.force_login(self.user)
        resp = self.client.get(f"{self.dashboard_url}?period=2026-10")
        html = resp.content.decode("utf-8")

        # Primary amount shown
        self.assertIn("-250.00 USD", html)
        # No secondary line for this same-currency transaction
        recent_section = html.split('id="panel-transactions-title"')[1].split("</section>")[0]
        self.assertNotIn("Orig:", recent_section)
        self.assertNotIn("Conversion unavailable", recent_section)

    def test_recent_transactions_missing_rate_and_stale_rate(self):
        from django.utils import timezone
        from .exchange_rates import record_manual_exchange_rate
        from .models import ExchangeRate
        from .services import create_category, create_transaction_entry

        UserPreference.objects.create(user=self.user, personal_base_currency=self.usd)
        w_twd = Wallet.objects.create(
            owner=self.user,
            name="TWD Wallet",
            wallet_type=Wallet.WalletType.CASH,
            currency=self.twd,
            opening_balance=Decimal("10000.00"),
            opening_balance_date=date(2026, 10, 1),
        )
        cat_twd = create_category(user=self.user, name="Trip", kind="expense")

        create_transaction_entry(
            user=self.user,
            wallet=w_twd,
            transaction_type="expense",
            amount=Decimal("1500.00"),
            currency=self.twd,
            transaction_date=date(2026, 10, 12),
            category=cat_twd,
            description="Night Market",
        )

        self.client.force_login(self.user)

        # 1. Missing rate: shows original currency with "Conversion unavailable"
        resp_missing = self.client.get(f"{self.dashboard_url}?period=2026-10")
        html_missing = resp_missing.content.decode("utf-8")
        self.assertIn("-1,500.00 TWD", html_missing)
        self.assertIn("Conversion unavailable", html_missing)
        self.assertNotIn("-1,500.00 USD", html_missing)

        # 2. Add stale rate: 1 TWD = 0.03125 USD with an old timestamp (> 24h)
        stale_time = timezone.now() - timezone.timedelta(hours=48)
        rate_rec = record_manual_exchange_rate(
            user=self.user,
            from_currency=self.twd,
            to_currency=self.usd,
            rate=Decimal("0.03125000"),
            scope="personal",
        )
        ExchangeRate.objects.filter(id=rate_rec.id).update(rate_timestamp=stale_time)

        resp_stale = self.client.get(f"{self.dashboard_url}?period=2026-10")
        html_stale = resp_stale.content.decode("utf-8")
        # 1500 * 0.03125 = 46.88 USD
        self.assertIn("-46.88 USD", html_stale)
        self.assertIn("Orig: -1,500.00 TWD", html_stale)
        self.assertIn("Stale rate", html_stale)

    def test_recent_transactions_workspace_authorization(self):
        from .exchange_rates import record_manual_exchange_rate
        from .services import create_category, create_transaction_entry

        bob = User.objects.create_user(username="bob_tx_auth", password="Str0ng!Password#2026")
        UserPreference.objects.create(user=self.user, personal_base_currency=self.usd)
        UserPreference.objects.create(user=bob, personal_base_currency=self.usd)

        w_eur = Wallet.objects.create(
            owner=self.user,
            name="User EUR Wallet",
            wallet_type=Wallet.WalletType.CASH,
            currency=self.eur,
            opening_balance=Decimal("1000.00"),
            opening_balance_date=date(2026, 10, 1),
        )
        cat = create_category(user=self.user, name="Shopping", kind="expense")
        create_transaction_entry(
            user=self.user,
            wallet=w_eur,
            transaction_type="expense",
            amount=Decimal("80.00"),
            currency=self.eur,
            transaction_date=date(2026, 10, 20),
            category=cat,
            description="Coat",
        )

        # Bob creates a personal rate 1 EUR = 2.00 USD
        record_manual_exchange_rate(
            user=bob,
            from_currency=self.eur,
            to_currency=self.usd,
            rate=Decimal("2.00000000"),
            scope="personal",
        )

        self.client.force_login(self.user)
        resp = self.client.get(f"{self.dashboard_url}?period=2026-10")
        html = resp.content.decode("utf-8")

        # self.user must NOT use Bob's personal rate: 80 * 2.00 = 160.00 USD must not appear
        self.assertNotIn("-160.00 USD", html)
        self.assertIn("-80.00 EUR", html)
        self.assertIn("Conversion unavailable", html)

    def test_recent_transactions_transfer_signs_and_comma_grouping(self):
        from .exchange_rates import record_manual_exchange_rate
        from .services import create_transfer_entry

        UserPreference.objects.create(user=self.user, personal_base_currency=self.usd)
        w_usd = Wallet.objects.create(
            owner=self.user,
            name="USD Main",
            wallet_type=Wallet.WalletType.BANK_ACCOUNT,
            currency=self.usd,
            opening_balance=Decimal("10000.00"),
            opening_balance_date=date(2026, 10, 1),
        )
        w_eur = Wallet.objects.create(
            owner=self.user,
            name="EUR Main",
            wallet_type=Wallet.WalletType.BANK_ACCOUNT,
            currency=self.eur,
            opening_balance=Decimal("10000.00"),
            opening_balance_date=date(2026, 10, 1),
        )

        record_manual_exchange_rate(
            user=self.user,
            from_currency=self.eur,
            to_currency=self.usd,
            rate=Decimal("1.10000000"),
            scope="personal",
        )

        # Transfer 1,500.00 EUR from w_eur to w_usd (destination 1,650.00 USD)
        create_transfer_entry(
            user=self.user,
            source_wallet=w_eur,
            destination_wallet=w_usd,
            amount_sent=Decimal("1500.00"),
            amount_received=Decimal("1650.00"),
            transfer_date=date(2026, 10, 22),
            notes="Inter-account funding",
        )

        self.client.force_login(self.user)
        resp = self.client.get(f"{self.dashboard_url}?period=2026-10")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")

        # Destination leg in USD: +1,650.00 USD (same currency)
        self.assertIn("+1,650.00 USD", html)
        # Source leg in EUR converted: -1,650.00 USD, Orig: -1,500.00 EUR
        self.assertIn("-1,650.00 USD", html)
        self.assertIn("Orig: -1,500.00 EUR", html)


class ExtendedMonetaryGroupingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="test_grouping_user",
            password="StrongPassword123!",
        )
        self.usd = Currency.objects.get(code="USD")
        self.jpy = Currency.objects.get(code="JPY")
        self.kwd = Currency.objects.get(code="KWD")
        self.wallet = Wallet.objects.create(
            owner=self.user,
            name="Checking",
            wallet_type=Wallet.WalletType.BANK_ACCOUNT,
            currency=self.usd,
            opening_balance=Decimal("1000.00"),
            opening_balance_date=date(2026, 1, 1),
        )
        self.wallet_jpy = Wallet.objects.create(
            owner=self.user,
            name="JPY Cash",
            wallet_type=Wallet.WalletType.CASH,
            currency=self.jpy,
            opening_balance=Decimal("50000"),
            opening_balance_date=date(2026, 1, 1),
        )
        self.wallet_dest = Wallet.objects.create(
            owner=self.user,
            name="Savings",
            wallet_type=Wallet.WalletType.BANK_ACCOUNT,
            currency=self.usd,
            opening_balance=Decimal("2000.00"),
            opening_balance_date=date(2026, 1, 1),
        )
        from .models import Category, CategoryBudget, SavingsGoal, SavingsContribution, Transaction, Transfer
        self.category = Category.objects.create(
            name="Dining",
            kind=Category.Kind.EXPENSE,
            owner=self.user,
            scope=Category.Scope.PERSONAL,
        )
        self.goal = SavingsGoal.objects.create(
            name="Vacation Fund",
            target_amount=Decimal("5000.00"),
            currency=self.usd,
            owner=self.user,
            scope=SavingsGoal.Scope.PERSONAL,
        )

    def test_wallet_form_opening_balance_grouping(self):
        from .forms import WalletForm
        # Valid positive grouped
        form_pos = WalletForm(
            data={
                "name": "New Checking",
                "wallet_type": Wallet.WalletType.BANK_ACCOUNT,
                "currency": self.usd.pk,
                "opening_balance": "1,500.00",
                "opening_balance_date": "2026-01-01",
                "ownership_scope": Wallet.OwnershipScope.PERSONAL,
            },
            user=self.user,
        )
        self.assertTrue(form_pos.is_valid(), form_pos.errors)
        self.assertEqual(form_pos.cleaned_data["opening_balance"], Decimal("1500.00"))

        # Valid integer grouped
        form_int = WalletForm(
            data={
                "name": "New Checking 2",
                "wallet_type": Wallet.WalletType.BANK_ACCOUNT,
                "currency": self.usd.pk,
                "opening_balance": "1,500",
                "opening_balance_date": "2026-01-01",
                "ownership_scope": Wallet.OwnershipScope.PERSONAL,
            },
            user=self.user,
        )
        self.assertTrue(form_int.is_valid(), form_int.errors)
        self.assertEqual(form_int.cleaned_data["opening_balance"], Decimal("1500"))

        # Valid negative grouped
        form_neg = WalletForm(
            data={
                "name": "Credit Card",
                "wallet_type": Wallet.WalletType.OTHER,
                "currency": self.usd.pk,
                "opening_balance": "-2,450.75",
                "opening_balance_date": "2026-01-01",
                "ownership_scope": Wallet.OwnershipScope.PERSONAL,
            },
            user=self.user,
        )
        self.assertTrue(form_neg.is_valid(), form_neg.errors)
        self.assertEqual(form_neg.cleaned_data["opening_balance"], Decimal("-2450.75"))

        # Valid zero and negative zero
        form_zero = WalletForm(
            data={
                "name": "Zero Wallet",
                "wallet_type": Wallet.WalletType.OTHER,
                "currency": self.usd.pk,
                "opening_balance": "0.00",
                "opening_balance_date": "2026-01-01",
                "ownership_scope": Wallet.OwnershipScope.PERSONAL,
            },
            user=self.user,
        )
        self.assertTrue(form_zero.is_valid(), form_zero.errors)
        self.assertEqual(form_zero.cleaned_data["opening_balance"], Decimal("0.00"))

        # Malformed grouping rejected
        for bad in ["10,00.00", "1,,000", "1000,00", ",100", "1,00"]:
            f_bad = WalletForm(
                data={
                    "name": "Bad Wallet",
                    "wallet_type": Wallet.WalletType.BANK_ACCOUNT,
                    "currency": self.usd.pk,
                    "opening_balance": bad,
                    "opening_balance_date": "2026-01-01",
                    "ownership_scope": Wallet.OwnershipScope.PERSONAL,
                },
                user=self.user,
            )
            self.assertFalse(f_bad.is_valid(), f"Expected {bad} to be rejected")
            self.assertIn("opening_balance", f_bad.errors)

        # Currency precision: JPY rejected for decimals
        f_jpy_bad = WalletForm(
            data={
                "name": "JPY Wallet",
                "wallet_type": Wallet.WalletType.CASH,
                "currency": self.jpy.pk,
                "opening_balance": "1,500.50",
                "opening_balance_date": "2026-01-01",
                "ownership_scope": Wallet.OwnershipScope.PERSONAL,
            },
            user=self.user,
        )
        self.assertFalse(f_jpy_bad.is_valid())
        self.assertIn("opening_balance", f_jpy_bad.errors)
        self.assertIn("JPY does not allow decimal places", str(f_jpy_bad.errors["opening_balance"]))

        # JPY accepted with whole number grouping
        f_jpy_good = WalletForm(
            data={
                "name": "JPY Wallet 2",
                "wallet_type": Wallet.WalletType.CASH,
                "currency": self.jpy.pk,
                "opening_balance": "150,000",
                "opening_balance_date": "2026-01-01",
                "ownership_scope": Wallet.OwnershipScope.PERSONAL,
            },
            user=self.user,
        )
        self.assertTrue(f_jpy_good.is_valid(), f_jpy_good.errors)
        self.assertEqual(f_jpy_good.cleaned_data["opening_balance"], Decimal("150000"))

        # HTTP POST create_wallet with grouped opening balance
        self.client.force_login(self.user)
        resp = self.client.post(
            reverse("budgets:wallets"),
            data={
                "action": "create_wallet",
                "name": "Grouped Wallet",
                "wallet_type": Wallet.WalletType.BANK_ACCOUNT,
                "currency": self.usd.pk,
                "opening_balance": "1,750.50",
                "opening_balance_date": "2026-01-01",
                "ownership_scope": Wallet.OwnershipScope.PERSONAL,
            },
        )
        self.assertEqual(resp.status_code, 302)
        created_w = Wallet.objects.get(name="Grouped Wallet", owner=self.user)
        self.assertEqual(created_w.opening_balance, Decimal("1750.500"))

        # HTTP POST update_wallet with grouped opening balance
        resp_up = self.client.post(
            reverse("budgets:wallets"),
            data={
                "action": "update_wallet",
                "wallet_id": created_w.pk,
                "name": "Grouped Wallet Updated",
                "wallet_type": Wallet.WalletType.BANK_ACCOUNT,
                "currency": self.usd.pk,
                "opening_balance": "3,200.50",
                "opening_balance_date": "2026-01-01",
            },
        )
        self.assertEqual(resp_up.status_code, 302)
        created_w.refresh_from_db()
        self.assertEqual(created_w.opening_balance, Decimal("3200.500"))
        self.assertEqual(created_w.name, "Grouped Wallet Updated")

    def test_transaction_entry_form_amount_grouping(self):
        from .forms import TransactionEntryForm
        from .models import Transaction
        # Valid grouped amounts
        for val, expected in [("1,100", Decimal("1100")), ("1,100.00", Decimal("1100.00"))]:
            f = TransactionEntryForm(
                data={
                    "transaction_type": Transaction.Type.EXPENSE,
                    "amount": val,
                    "wallet": self.wallet.pk,
                    "category": self.category.pk,
                    "transaction_date": "2026-10-25",
                    "scope": Transaction.Scope.PERSONAL,
                },
                user=self.user,
            )
            self.assertTrue(f.is_valid(), f.errors)
            self.assertEqual(f.cleaned_data["amount"], expected)

        # Malformed grouping rejected
        for bad in ["10,00", "1,,000", "10,00.00", ",100"]:
            f_bad = TransactionEntryForm(
                data={
                    "transaction_type": Transaction.Type.EXPENSE,
                    "amount": bad,
                    "wallet": self.wallet.pk,
                    "category": self.category.pk,
                    "transaction_date": "2026-10-25",
                    "scope": Transaction.Scope.PERSONAL,
                },
                user=self.user,
            )
            self.assertFalse(f_bad.is_valid())
            self.assertIn("amount", f_bad.errors)

        # Non-positive amounts rejected
        for zero_or_neg in ["0.00", "-1,100.00", "-50"]:
            f_np = TransactionEntryForm(
                data={
                    "transaction_type": Transaction.Type.EXPENSE,
                    "amount": zero_or_neg,
                    "wallet": self.wallet.pk,
                    "category": self.category.pk,
                    "transaction_date": "2026-10-25",
                    "scope": Transaction.Scope.PERSONAL,
                },
                user=self.user,
            )
            self.assertFalse(f_np.is_valid())
            self.assertIn("amount", f_np.errors)

        # JPY currency precision
        f_jpy_bad = TransactionEntryForm(
            data={
                "transaction_type": Transaction.Type.EXPENSE,
                "amount": "1,100.50",
                "wallet": self.wallet_jpy.pk,
                "category": self.category.pk,
                "transaction_date": "2026-10-25",
                "scope": Transaction.Scope.PERSONAL,
            },
            user=self.user,
        )
        self.assertFalse(f_jpy_bad.is_valid())
        self.assertIn("amount", f_jpy_bad.errors)

        f_jpy_good = TransactionEntryForm(
            data={
                "transaction_type": Transaction.Type.EXPENSE,
                "amount": "1,100",
                "wallet": self.wallet_jpy.pk,
                "category": self.category.pk,
                "transaction_date": "2026-10-25",
                "scope": Transaction.Scope.PERSONAL,
            },
            user=self.user,
        )
        self.assertTrue(f_jpy_good.is_valid(), f_jpy_good.errors)

        # HTTP POST create_transaction with grouped amount
        self.client.force_login(self.user)
        resp = self.client.post(
            reverse("budgets:transactions"),
            data={
                "action": "create_transaction",
                "transaction_type": Transaction.Type.EXPENSE,
                "amount": "1,100.00",
                "wallet": self.wallet.pk,
                "category": self.category.pk,
                "transaction_date": "2026-10-25",
                "scope": Transaction.Scope.PERSONAL,
                "description": "Grouped Grocery Run",
            },
        )
        self.assertEqual(resp.status_code, 302)
        tx = Transaction.objects.get(description="Grouped Grocery Run", owner=self.user)
        self.assertEqual(tx.amount, Decimal("1100.000"))

        # HTTP POST update_transaction with grouped amount
        resp_up = self.client.post(
            reverse("budgets:transactions"),
            data={
                "action": "update_transaction",
                "transaction_id": tx.pk,
                "expected_revision": tx.revision,
                "transaction_type": Transaction.Type.EXPENSE,
                "amount": "2,200.00",
                "wallet": self.wallet.pk,
                "category": self.category.pk,
                "transaction_date": "2026-10-25",
                "scope": Transaction.Scope.PERSONAL,
                "description": "Grouped Grocery Run Updated",
            },
        )
        self.assertEqual(resp_up.status_code, 302)
        tx.refresh_from_db()
        self.assertEqual(tx.amount, Decimal("2200.000"))
        self.assertEqual(tx.description, "Grouped Grocery Run Updated")

    def test_transfer_entry_form_amount_grouping(self):
        from .forms import TransferEntryForm
        from .models import Transfer
        # Valid grouped amounts
        f_trans = TransferEntryForm(
            data={
                "source_wallet": self.wallet.pk,
                "destination_wallet": self.wallet_dest.pk,
                "amount_sent": "1,200.00",
                "amount_received": "1,200.00",
                "fee_amount": "25.00",
                "transfer_date": "2026-10-26",
            },
            user=self.user,
        )
        self.assertTrue(f_trans.is_valid(), f_trans.errors)
        self.assertEqual(f_trans.cleaned_data["amount_sent"], Decimal("1200.00"))
        self.assertEqual(f_trans.cleaned_data["amount_received"], Decimal("1200.00"))
        self.assertEqual(f_trans.cleaned_data["fee_amount"], Decimal("25.00"))

        # Zero fee is valid
        f_zero_fee = TransferEntryForm(
            data={
                "source_wallet": self.wallet.pk,
                "destination_wallet": self.wallet_dest.pk,
                "amount_sent": "1,200.00",
                "fee_amount": "0.00",
                "transfer_date": "2026-10-26",
            },
            user=self.user,
        )
        self.assertTrue(f_zero_fee.is_valid(), f_zero_fee.errors)
        self.assertEqual(f_zero_fee.cleaned_data["fee_amount"], Decimal("0.00"))

        # Negative fee rejected
        f_neg_fee = TransferEntryForm(
            data={
                "source_wallet": self.wallet.pk,
                "destination_wallet": self.wallet_dest.pk,
                "amount_sent": "1,200.00",
                "fee_amount": "-25.00",
                "transfer_date": "2026-10-26",
            },
            user=self.user,
        )
        self.assertFalse(f_neg_fee.is_valid())
        self.assertIn("fee_amount", f_neg_fee.errors)

        # Malformed grouping rejected
        f_bad = TransferEntryForm(
            data={
                "source_wallet": self.wallet.pk,
                "destination_wallet": self.wallet_dest.pk,
                "amount_sent": "12,00.00",
                "transfer_date": "2026-10-26",
            },
            user=self.user,
        )
        self.assertFalse(f_bad.is_valid())
        self.assertIn("amount_sent", f_bad.errors)

        # HTTP POST create_transfer with grouped amounts
        self.client.force_login(self.user)
        resp = self.client.post(
            reverse("budgets:transactions"),
            data={
                "action": "create_transfer",
                "source_wallet": self.wallet.pk,
                "destination_wallet": self.wallet_dest.pk,
                "amount_sent": "1,200.00",
                "amount_received": "1,200.00",
                "fee_amount": "25.00",
                "transfer_date": "2026-10-26",
                "notes": "Grouped inter-wallet transfer",
            },
        )
        self.assertEqual(resp.status_code, 302)
        tr = Transfer.objects.get(notes="Grouped inter-wallet transfer")
        self.assertEqual(tr.amount_sent, Decimal("1200.000"))
        self.assertEqual(tr.amount_received, Decimal("1200.000"))
        self.assertEqual(tr.fee_amount, Decimal("25.000"))

        # HTTP POST update_transfer with grouped amounts
        resp_up = self.client.post(
            reverse("budgets:transactions"),
            data={
                "action": "update_transfer",
                "transfer_id": tr.pk,
                "expected_revision": tr.revision,
                "source_wallet": self.wallet.pk,
                "destination_wallet": self.wallet_dest.pk,
                "amount_sent": "1,350.00",
                "amount_received": "1,350.00",
                "fee_amount": "30.00",
                "transfer_date": "2026-10-26",
                "notes": "Grouped transfer updated",
            },
        )
        self.assertEqual(resp_up.status_code, 302)
        tr.refresh_from_db()
        self.assertEqual(tr.amount_sent, Decimal("1350.000"))
        self.assertEqual(tr.amount_received, Decimal("1350.000"))
        self.assertEqual(tr.fee_amount, Decimal("30.000"))

    def test_category_budget_form_limit_amount_grouping(self):
        from .forms import CategoryBudgetForm
        from .models import CategoryBudget
        # Valid grouped limit
        f_b = CategoryBudgetForm(
            data={
                "category": self.category.pk,
                "period": "2026-10",
                "currency": self.usd.pk,
                "limit_amount": "2,500.00",
                "scope": CategoryBudget.Scope.PERSONAL,
            },
            user=self.user,
        )
        self.assertTrue(f_b.is_valid(), f_b.errors)
        self.assertEqual(f_b.cleaned_data["limit_amount"], Decimal("2500.00"))

        # Malformed grouping rejected
        f_b_bad = CategoryBudgetForm(
            data={
                "category": self.category.pk,
                "period": "2026-10",
                "currency": self.usd.pk,
                "limit_amount": "25,00.00",
                "scope": CategoryBudget.Scope.PERSONAL,
            },
            user=self.user,
        )
        self.assertFalse(f_b_bad.is_valid())
        self.assertIn("limit_amount", f_b_bad.errors)

        # Zero or negative rejected
        f_b_zero = CategoryBudgetForm(
            data={
                "category": self.category.pk,
                "period": "2026-10",
                "currency": self.usd.pk,
                "limit_amount": "0.00",
                "scope": CategoryBudget.Scope.PERSONAL,
            },
            user=self.user,
        )
        self.assertFalse(f_b_zero.is_valid())
        self.assertIn("limit_amount", f_b_zero.errors)

        # HTTP POST save_budget with grouped limit
        self.client.force_login(self.user)
        resp = self.client.post(
            reverse("budgets:budgets"),
            data={
                "action": "save_budget",
                "category": self.category.pk,
                "period": "2026-10",
                "currency": self.usd.pk,
                "limit_amount": "2,500.00",
                "scope": CategoryBudget.Scope.PERSONAL,
            },
        )
        self.assertEqual(resp.status_code, 302)
        budget = CategoryBudget.objects.get(category=self.category, period="2026-10")
        self.assertEqual(budget.limit_amount, Decimal("2500.000"))

    def test_savings_goal_and_contribution_amount_grouping(self):
        from .forms import SavingsGoalForm, SavingsContributionForm
        from .models import SavingsContribution, SavingsGoal
        # Goal target amount grouped
        f_goal = SavingsGoalForm(
            data={
                "name": "New Goal",
                "currency": self.usd.pk,
                "target_amount": "100,000.00",
                "scope": SavingsGoal.Scope.PERSONAL,
            }
        )
        self.assertTrue(f_goal.is_valid(), f_goal.errors)
        self.assertEqual(f_goal.cleaned_data["target_amount"], Decimal("100000.00"))

        # Goal target amount malformed
        f_goal_bad = SavingsGoalForm(
            data={
                "name": "Bad Goal",
                "currency": self.usd.pk,
                "target_amount": "10,00.00",
                "scope": SavingsGoal.Scope.PERSONAL,
            }
        )
        self.assertFalse(f_goal_bad.is_valid())
        self.assertIn("target_amount", f_goal_bad.errors)

        # HTTP POST create_goal with grouped target amount
        self.client.force_login(self.user)
        resp = self.client.post(
            reverse("budgets:savings_goals"),
            data={
                "action": "create_goal",
                "name": "House Downpayment",
                "currency": self.usd.pk,
                "target_amount": "100,000.00",
                "scope": SavingsGoal.Scope.PERSONAL,
            },
        )
        self.assertEqual(resp.status_code, 302)
        goal = SavingsGoal.objects.get(name="House Downpayment", owner=self.user)
        self.assertEqual(goal.target_amount, Decimal("100000.000"))

        # HTTP POST update_goal with grouped target amount
        resp_up = self.client.post(
            reverse("budgets:savings_goals"),
            data={
                "action": "update_goal",
                "goal_id": goal.pk,
                "expected_revision": goal.revision,
                "name": "House Downpayment Updated",
                "currency": self.usd.pk,
                "target_amount": "150,000.00",
                "scope": SavingsGoal.Scope.PERSONAL,
            },
        )
        self.assertEqual(resp_up.status_code, 302)
        goal.refresh_from_db()
        self.assertEqual(goal.target_amount, Decimal("150000.000"))

        # Savings contribution form grouped amount
        f_c = SavingsContributionForm(
            data={
                "goal": self.goal.pk,
                "entry_type": SavingsContribution.EntryType.CONTRIBUTION,
                "amount": "1,100.00",
                "contribution_date": "2026-10-26",
            },
            user=self.user,
        )
        self.assertTrue(f_c.is_valid(), f_c.errors)
        self.assertEqual(f_c.cleaned_data["amount"], Decimal("1100.00"))

        # Contribution malformed grouping rejected
        f_c_bad = SavingsContributionForm(
            data={
                "goal": self.goal.pk,
                "entry_type": SavingsContribution.EntryType.CONTRIBUTION,
                "amount": "11,00.00",
                "contribution_date": "2026-10-26",
            },
            user=self.user,
        )
        self.assertFalse(f_c_bad.is_valid())
        self.assertIn("amount", f_c_bad.errors)

        # Contribution non-positive rejected
        f_c_neg = SavingsContributionForm(
            data={
                "goal": self.goal.pk,
                "entry_type": SavingsContribution.EntryType.CONTRIBUTION,
                "amount": "-1,100.00",
                "contribution_date": "2026-10-26",
            },
            user=self.user,
        )
        self.assertFalse(f_c_neg.is_valid())
        self.assertIn("amount", f_c_neg.errors)

        # HTTP POST record_contribution with grouped amount
        resp_c = self.client.post(
            reverse("budgets:savings_goals"),
            data={
                "action": "record_contribution",
                "goal": self.goal.pk,
                "entry_type": SavingsContribution.EntryType.CONTRIBUTION,
                "amount": "1,100.00",
                "contribution_date": "2026-10-26",
                "notes": "Grouped contribution note",
            },
        )
        self.assertEqual(resp_c.status_code, 302)
        c = SavingsContribution.objects.get(goal=self.goal, notes="Grouped contribution note")
        self.assertEqual(c.amount, Decimal("1100.000"))

        # HTTP POST record_contribution with reversal and grouped amount
        resp_rev = self.client.post(
            reverse("budgets:savings_goals"),
            data={
                "action": "record_contribution",
                "goal": self.goal.pk,
                "entry_type": SavingsContribution.EntryType.REVERSAL,
                "amount": "500.00",
                "contribution_date": "2026-10-27",
                "notes": "Grouped reversal note",
            },
        )
        self.assertEqual(resp_rev.status_code, 302)
        rev = SavingsContribution.objects.get(goal=self.goal, notes="Grouped reversal note")
        self.assertEqual(rev.amount, Decimal("500.000"))
        self.goal.refresh_from_db()
        self.assertEqual(self.goal.saved_amount, Decimal("600.000"))

    def test_currency_converter_form_amount_grouping(self):
        from .forms import CurrencyConverterForm
        f_conv = CurrencyConverterForm(
            data={
                "amount": "1,500.00",
                "from_currency": self.usd.pk,
                "to_currency": self.jpy.pk,
                "scope": "personal",
            }
        )
        self.assertTrue(f_conv.is_valid(), f_conv.errors)
        self.assertEqual(f_conv.cleaned_data["amount"], Decimal("1500.00"))

        # Malformed grouping rejected
        f_conv_bad = CurrencyConverterForm(
            data={
                "amount": "15,00.00",
                "from_currency": self.usd.pk,
                "to_currency": self.jpy.pk,
                "scope": "personal",
            }
        )
        self.assertFalse(f_conv_bad.is_valid())
        self.assertIn("amount", f_conv_bad.errors)

    def test_sync_mutation_payload_is_ungrouped(self):
        from .models import Transaction
        import json
        self.client.force_login(self.user)
        # Verify sync mutation API processes ungrouped amount
        resp = self.client.post(
            reverse("budgets:sync_mutations_api"),
            data=json.dumps({
                "expected_user_id": self.user.pk,
                "mutations": [
                    {
                        "mutation_id": "00000000-0000-0000-0000-000000000099",
                        "operation": "create_transaction",
                        "entity_id": "00000000-0000-0000-0000-000000000088",
                        "base_revision": None,
                        "payload": {
                            "transaction_type": "expense",
                            "amount": "1100.00",
                            "wallet_id": str(self.wallet.pk),
                            "category_id": str(self.category.pk),
                            "transaction_date": "2026-10-27",
                            "scope": "personal",
                            "description": "Offline sync clean amount",
                        },
                    }
                ],
            }),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        sync_tx = Transaction.objects.get(description="Offline sync clean amount")
        self.assertEqual(sync_tx.amount, Decimal("1100.000"))


class TransactionsPageReorganizationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="reorg_user",
            password="StrongPassword123!",
        )
        self.usd = Currency.objects.get(code="USD")
        self.eur, _ = Currency.objects.get_or_create(
            code="EUR",
            defaults={"name": "Euro", "decimal_places": 2, "is_active": True},
        )
        from apps.accounts.models import UserPreference
        UserPreference.objects.create(
            user=self.user,
            personal_base_currency=self.usd,
            display_currency=None,
        )
        self.wallet1 = Wallet.objects.create(
            owner=self.user,
            name="Main Checking",
            wallet_type=Wallet.WalletType.BANK_ACCOUNT,
            currency=self.usd,
            opening_balance=Decimal("5000.00"),
            opening_balance_date=date(2026, 1, 1),
        )
        self.wallet2 = Wallet.objects.create(
            owner=self.user,
            name="Savings Jar",
            wallet_type=Wallet.WalletType.CASH,
            currency=self.usd,
            opening_balance=Decimal("2000.00"),
            opening_balance_date=date(2026, 1, 1),
        )
        from django.utils import timezone
        from .models import Category, Transaction, Transfer
        from .services import create_category, create_transaction_entry, create_transfer_entry

        today = timezone.localdate()
        self.category = create_category(user=self.user, name="Groceries", kind="expense", scope="personal")
        self.tx1 = create_transaction_entry(
            user=self.user,
            transaction_type="expense",
            amount=Decimal("125.50"),
            wallet=self.wallet1,
            transaction_date=today,
            category=self.category,
            description="Supermarket haul",
            notes="Weekly grocery run",
            scope="personal",
        )
        self.transfer1 = create_transfer_entry(
            user=self.user,
            source_wallet=self.wallet1,
            destination_wallet=self.wallet2,
            amount_sent=Decimal("300.00"),
            transfer_date=today,
            notes="Emergency savings allocation",
        )
        self.client.force_login(self.user)
        self.transactions_url = reverse("budgets:transactions")

    def _find_browser_binary(self):
        import os
        candidates = [
            os.environ.get("CHROME_BIN"),
            os.environ.get("EDGE_BIN"),
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        ]
        for c in candidates:
            if c and os.path.exists(c):
                return c
        return None

    def test_topbar_add_transaction_retained_and_content_duplicate_removed(self):
        resp = self.client.get(self.transactions_url)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")

        # Topbar retains the Add transaction button
        self.assertIn('btn-topbar-action', html)
        self.assertIn('data-bs-target="#transactionModal"', html)
        self.assertIn('Add transaction', html)

        # Content area duplicate button is removed
        self.assertNotIn('id="btn-open-new-transaction"', html)

    def test_compact_toolbar_and_filter_placement_above_transaction_list(self):
        resp = self.client.get(self.transactions_url)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")

        # Toolbar heading / form is placed BEFORE the transaction history list
        pos_toolbar = html.find('id="transactions-toolbar-heading"')
        pos_filter_form = html.find('id="transaction-filter-form"')
        pos_list = html.find('id="transactions-list-heading"')
        pos_recent_transfers = html.find('id="recent-transfers-heading"')

        self.assertNotEqual(pos_toolbar, -1)
        self.assertNotEqual(pos_filter_form, -1)
        self.assertNotEqual(pos_list, -1)
        self.assertNotEqual(pos_recent_transfers, -1)

        self.assertTrue(pos_toolbar < pos_list, "Toolbar heading should be placed above transaction list")
        self.assertTrue(pos_filter_form < pos_list, "Filter form should be placed above transaction list")
        self.assertTrue(pos_list < pos_recent_transfers, "Transaction list should be placed above recent transfers")

        # Toolbar controls are present
        self.assertIn('id="btn-export-csv"', html)
        self.assertIn('id="btn-export-json"', html)
        self.assertIn('id="btn-record-transfer"', html)
        self.assertIn('id="btn-manage-categories"', html)
        self.assertIn('id="btn-apply-filters"', html)
        self.assertIn('id="btn-reset-filters"', html)

        # Filter fields preserved
        self.assertIn('id="filter-search-notes"', html)
        self.assertIn('id="filter-start-date"', html)
        self.assertIn('id="filter-end-date"', html)
        self.assertIn('id="filter-type"', html)
        self.assertIn('id="filter-scope"', html)
        self.assertIn('id="filter-category"', html)
        self.assertIn('id="filter-wallet"', html)
        self.assertIn('id="filter-currency"', html)
        self.assertNotIn('id="filter-display-currency"', html)

        # Filter form action and export URLs
        self.assertIn('format=csv', html)
        self.assertIn('format=json', html)

        # Financial values have text-nowrap and table has table-responsive
        self.assertIn('table-responsive', html)
        self.assertIn('text-nowrap', html)

    def test_transfer_modal_rendering_and_action(self):
        resp = self.client.get(self.transactions_url)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")

        # Transfer modal exists in modals block
        self.assertIn('id="transferModal"', html)
        self.assertIn('aria-labelledby="transferModalLabel"', html)
        self.assertIn('id="transferModalLabel"', html)
        self.assertIn('id="transfer-form"', html)
        self.assertIn('name="action" value="create_transfer"', html)
        self.assertIn('id="id_source_wallet"', html)
        self.assertIn('id="id_destination_wallet"', html)
        self.assertIn('id="id_amount_sent"', html)
        self.assertIn('id="id_amount_received"', html)
        self.assertIn('id="id_manual_rate"', html)
        self.assertIn('id="id_fee_amount"', html)
        self.assertIn('id="id_transfer_date"', html)
        self.assertIn('id="id_notes"', html)
        self.assertIn('id="btn-submit-transfer"', html)

    def test_transfer_validation_errors_reopens_modal(self):
        # Post invalid transfer data (missing amount_sent and destination_wallet)
        post_data = {
            "action": "create_transfer",
            "source_wallet": str(self.wallet1.pk),
            "destination_wallet": "",
            "amount_sent": "",
            "transfer_date": "2026-10-22",
            "notes": "Invalid transfer test memo",
        }
        resp = self.client.post(self.transactions_url, post_data)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")

        # Modal is marked to auto-open with data-auto-open="true"
        self.assertIn('data-auto-open="true"', html)

        # Submitted notes and date preserved
        self.assertIn("Invalid transfer test memo", html)
        self.assertIn("2026-10-22", html)

        # Validation errors displayed inside the modal
        self.assertIn('This field is required.', html)

    def test_category_collapse_and_error_expansion(self):
        # GET: Category section is collapsed by default (no show class in manageCategoriesCollapse)
        resp_get = self.client.get(self.transactions_url)
        self.assertEqual(resp_get.status_code, 200)
        html_get = resp_get.content.decode("utf-8")
        self.assertIn('id="manageCategoriesCollapse"', html_get)
        # Verify it has class="collapse mb-4 " without "show"
        collapse_decl = html_get.split('id="manageCategoriesCollapse"')[0].split('<div')[-1]
        self.assertNotIn('show', collapse_decl)
        self.assertIn('aria-expanded="false"', html_get)

        # POST invalid category: empty name
        post_data = {
            "action": "create_category",
            "name": "",
            "kind": "expense",
            "scope": "personal",
        }
        resp_post = self.client.post(self.transactions_url, post_data)
        self.assertEqual(resp_post.status_code, 200)
        html_post = resp_post.content.decode("utf-8")

        # When category submission fails, manageCategoriesCollapse has class "show"
        collapse_post_decl = html_post.split('id="manageCategoriesCollapse"')[0].split('<div')[-1]
        self.assertIn('show', collapse_post_decl)
        self.assertIn('aria-expanded="true"', html_post)
        self.assertIn('This field is required.', html_post)

    def test_transaction_table_info_actions_and_transfer_history_below(self):
        resp = self.client.get(self.transactions_url)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")

        # Primary transaction list contains transaction info
        self.assertIn("Supermarket haul", html)
        self.assertIn("Weekly grocery run", html)
        self.assertIn("Groceries", html)
        self.assertIn("Main Checking", html)
        self.assertIn("btn-edit-tx", html)
        self.assertIn('data-tx-id="' + str(self.tx1.id) + '"', html)
        self.assertIn('action" value="delete_transaction"', html)

        # Recent transfers list contains transfer info and edit controls below primary list
        self.assertIn("Recent transfers", html)
        self.assertIn("Emergency savings allocation", html)
        self.assertIn(f'id="edit-transfer-{self.transfer1.id}"', html)
        self.assertIn('action" value="update_transfer"', html)
        self.assertIn('action" value="delete_transfer"', html)

    def test_default_collapsed_state_when_no_active_filters(self):
        resp = self.client.get(self.transactions_url)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")

        # Filters button exists with aria-expanded="false" and chevron
        self.assertIn('id="btn-toggle-filters"', html)
        self.assertIn('aria-expanded="false"', html)
        self.assertIn('filter-chevron', html)

        # Filter collapse section is collapsed by default (no "show" class in its declaration)
        collapse_decl = html.split('id="transaction-filter-collapse"')[0].split('<div')[-1]
        self.assertNotIn('show', collapse_decl)

        # No active filters indicator displayed
        self.assertNotIn('id="active-filters-indicator"', html)

        # Noscript fallback ensures form is permanently visible without JS
        self.assertIn('<noscript>', html)
        self.assertIn('#transaction-filter-collapse', html)
        self.assertIn('display: block !important', html)

    def test_pagination_and_default_currency_alone_do_not_expand_filters(self):
        # 1. Pagination only: ?page=1
        resp_page = self.client.get(self.transactions_url + "?page=1")
        self.assertEqual(resp_page.status_code, 200)
        html_page = resp_page.content.decode("utf-8")
        collapse_page = html_page.split('id="transaction-filter-collapse"')[0].split('<div')[-1]
        self.assertNotIn('show', collapse_page)
        self.assertNotIn('id="active-filters-indicator"', html_page)

        # 2. Default display currency only: ?display_currency=USD
        resp_curr = self.client.get(self.transactions_url + "?display_currency=USD")
        self.assertEqual(resp_curr.status_code, 200)
        html_curr = resp_curr.content.decode("utf-8")
        collapse_curr = html_curr.split('id="transaction-filter-collapse"')[0].split('<div')[-1]
        self.assertNotIn('show', collapse_curr)
        self.assertNotIn('id="active-filters-indicator"', html_curr)

        # 3. Both pagination and default currency: ?page=1&display_currency=USD
        resp_both = self.client.get(self.transactions_url + "?page=1&display_currency=USD")
        self.assertEqual(resp_both.status_code, 200)
        html_both = resp_both.content.decode("utf-8")
        collapse_both = html_both.split('id="transaction-filter-collapse"')[0].split('<div')[-1]
        self.assertNotIn('show', collapse_both)
        self.assertNotIn('id="active-filters-indicator"', html_both)

    def test_non_default_display_currency_expands_filters(self):
        # Non-default display currency (EUR) works as URL override but does not expand filters drawer
        resp = self.client.get(self.transactions_url + "?display_currency=EUR")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")

        # Collapse remains collapsed
        collapse_decl = html.split('id="transaction-filter-collapse"')[0].split('<div')[-1]
        self.assertNotIn('show', collapse_decl)

        # Toggle button has aria-expanded="false"
        self.assertIn('aria-expanded="false"', html)

        # Active filters indicator is NOT displayed
        self.assertNotIn('id="active-filters-indicator"', html)

        # The display currency converted amounts and headers reflect EUR override
        self.assertIn('Converted (EUR)', html)
        self.assertNotIn('id="filter-display-currency"', html)

        # Topbar display currency selector reflects effective override currency EUR
        self.assertIn('<option value="EUR" selected>', html)

    def test_active_filter_expansion_and_retained_values(self):
        # Multiple active filters
        url = self.transactions_url + "?q=Supermarket&start_date=2026-10-01&end_date=2026-10-31&type=expense"
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")

        # Expanded with show class
        collapse_decl = html.split('id="transaction-filter-collapse"')[0].split('<div')[-1]
        self.assertIn('show', collapse_decl)
        self.assertIn('id="active-filters-indicator"', html)

        # Values retained
        self.assertIn('value="Supermarket"', html)
        self.assertIn('value="2026-10-01"', html)
        self.assertIn('value="2026-10-31"', html)

    def test_summary_totals_across_multiple_pages(self):
        """Summary strip totals must compute over the full filtered queryset across pages, excluding transfers."""
        from datetime import timedelta
        from django.utils import timezone
        from .services import create_transaction_entry
        today = timezone.localdate()
        for i in range(30):
            create_transaction_entry(
                user=self.user,
                transaction_type="expense",
                amount=Decimal("10.00"),
                wallet=self.wallet1,
                transaction_date=today,
                description=f"Pagination expense {i}",
                scope="personal",
            )
        resp_p1 = self.client.get(self.transactions_url + "?page=1")
        self.assertEqual(resp_p1.status_code, 200)
        self.assertContains(resp_p1, "-425.50 USD")
        self.assertNotIn("Current page", resp_p1.content.decode("utf-8"))

        resp_p2 = self.client.get(self.transactions_url + "?page=2")
        self.assertEqual(resp_p2.status_code, 200)
        self.assertContains(resp_p2, "-425.50 USD")

    def test_stale_rate_banner_shown_and_hidden(self):
        """Banner appears above table if visible rows have stale rate, hidden otherwise."""
        from datetime import timedelta
        from django.utils import timezone
        from .models import ExchangeRate
        from .services import create_transaction_entry

        wallet_eur = Wallet.objects.create(
            owner=self.user,
            name="EUR Wallet",
            wallet_type=Wallet.WalletType.BANK_ACCOUNT,
            currency=self.eur,
            opening_balance=Decimal("1000.00"),
            opening_balance_date=date(2026, 1, 1),
        )
        today = timezone.localdate()
        create_transaction_entry(
            user=self.user,
            transaction_type="expense",
            amount=Decimal("50.00"),
            wallet=wallet_eur,
            transaction_date=today,
            description="EUR Coffee",
            scope="personal",
        )
        fresh_time = timezone.now() - timedelta(hours=1)
        rate_obj, _ = ExchangeRate.objects.update_or_create(
            from_currency=self.eur,
            to_currency=self.usd,
            scope=ExchangeRate.Scope.PROVIDER,
            is_manual=False,
            defaults={"rate": Decimal("1.10000000"), "rate_timestamp": fresh_time, "provider": "frankfurter_v2"},
        )
        resp_fresh = self.client.get(self.transactions_url)
        self.assertEqual(resp_fresh.status_code, 200)
        html_fresh = resp_fresh.content.decode("utf-8")
        banner_tag_fresh = html_fresh.split('id="stale-rates-banner"')[1].split('>')[0]
        self.assertIn("d-none", banner_tag_fresh)

        stale_time = timezone.now() - timedelta(hours=72)
        rate_obj.rate_timestamp = stale_time
        rate_obj.save()

        resp_stale = self.client.get(self.transactions_url)
        self.assertEqual(resp_stale.status_code, 200)
        html_stale = resp_stale.content.decode("utf-8")
        banner_tag_stale = html_stale.split('id="stale-rates-banner"')[1].split('>')[0]
        self.assertNotIn("d-none", banner_tag_stale)
        self.assertIn("tx-stale-banner", html_stale)
        self.assertIn("Refresh rates", html_stale)

    def test_delete_confirmation_deleting_correct_row(self):
        """Submitting delete action deletes the target transaction and preserves other transactions."""
        from .models import Transaction
        from .services import create_transaction_entry

        target_tx = create_transaction_entry(
            user=self.user,
            transaction_type="expense",
            amount=Decimal("42.00"),
            wallet=self.wallet1,
            transaction_date=date(2026, 10, 24),
            description="Deletable item",
            scope="personal",
        )
        post_data = {
            "action": "delete_transaction",
            "transaction_id": str(target_tx.id),
            "expected_revision": str(target_tx.revision),
        }
        resp = self.client.post(self.transactions_url, post_data, follow=True)
        self.assertEqual(resp.status_code, 200)

        target_tx.refresh_from_db()
        self.assertTrue(target_tx.is_deleted)
        self.tx1.refresh_from_db()
        self.assertFalse(self.tx1.is_deleted)

    def test_delete_modal_submits_id_of_opened_row_menu(self):
        """Delete confirmation modal submits the exact transaction ID of the row whose menu was opened."""
        from .services import create_transaction_entry
        import pathlib
        from django.conf import settings

        from django.utils import timezone
        today = timezone.localdate()
        tx2 = create_transaction_entry(
            user=self.user,
            transaction_type="expense",
            amount=Decimal("88.00"),
            wallet=self.wallet1,
            transaction_date=today,
            description="Item to be deleted specifically",
            scope="personal",
        )

        resp = self.client.get(self.transactions_url)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")

        # 1. Template validation: Each row has an independent tx-delete-form with its own transaction_id
        self.assertIn(f'value="{self.tx1.id}"', html)
        self.assertIn(f'value="{tx2.id}"', html)
        self.assertIn('id="deleteConfirmModal"', html)
        self.assertIn('id="btn-confirm-delete-tx"', html)

        # 2. Browser-level interaction test verifying modal opens for tx2 and submits tx2's ID
        browser_bin = self._find_browser_binary()
        if not browser_bin:
            return

        static_dir = pathlib.Path(settings.BASE_DIR) / "static"
        bs_js = (static_dir / "vendor" / "bootstrap" / "bootstrap.bundle.min.js").read_text(encoding="utf-8")
        bs_css = (static_dir / "vendor" / "bootstrap" / "bootstrap.min.css").read_text(encoding="utf-8")
        app_css = (static_dir / "css" / "app.css").read_text(encoding="utf-8")
        dash_css = (static_dir / "css" / "dashboard.css").read_text(encoding="utf-8")
        tx_js = (static_dir / "js" / "pages" / "transaction.js").read_text(encoding="utf-8")

        tx1_id = str(self.tx1.id)
        tx2_id = str(tx2.id)

        harness_html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <style>{bs_css}</style>
    <style>{app_css}</style>
    <style>{dash_css}</style>
    <style>*, *::before, *::after {{ transition: none !important; animation: none !important; }}</style>
</head>
<body class="app-body">
    {html}
    <script>{bs_js}</script>
    <script>
    if (window.bootstrap && window.bootstrap.Popover && window.bootstrap.Popover.Default) {{
        window.bootstrap.Popover.Default.animation = false;
    }}
    </script>
    <script>{tx_js}</script>
    <script>
    window.addEventListener('DOMContentLoaded', async function () {{
        await new Promise(r => setTimeout(r, 200));
        var results = {{}};

        var submittedPayload = null;
        document.querySelectorAll('form.tx-delete-form').forEach(function (f) {{
            f.submit = function () {{
                var fd = new FormData(f);
                submittedPayload = {{
                    action: fd.get('action'),
                    transaction_id: fd.get('transaction_id')
                }};
            }};
        }});

        // Find delete trigger inside tx2's row form
        var tx2Input = document.querySelector('form.tx-delete-form input[value="{tx2_id}"]');
        var tx2Form = tx2Input ? tx2Input.form : null;
        var tx2Trigger = tx2Form ? tx2Form.querySelector('.tx-delete-trigger') : null;

        if (tx2Trigger) {{
            tx2Trigger.click();
            await new Promise(r => setTimeout(r, 300));

            var modalEl = document.getElementById('deleteConfirmModal');
            var descEl = document.getElementById('delete-confirm-desc');
            results.modalShown = modalEl && modalEl.classList.contains('show');
            results.modalDesc = descEl ? descEl.textContent.trim() : '';

            var confirmBtn = document.getElementById('btn-confirm-delete-tx');
            if (confirmBtn) {{
                confirmBtn.click();
                await new Promise(r => setTimeout(r, 200));
            }}
        }}

        results.submittedPayload = submittedPayload;

        fetch('/__test_done__', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify(results)
        }}).catch(function () {{}});
    }});
    </script>
</body>
</html>"""

        rep = self._run_browser_harness(browser_bin, 1280, 800, harness_html)
        self.assertIsNotNone(rep, "Browser delete modal harness timed out")
        self.assertTrue(rep.get("modalShown"), "Delete confirmation modal should be shown after clicking delete trigger")
        self.assertEqual(rep.get("modalDesc"), "Item to be deleted specifically")
        self.assertIsNotNone(rep.get("submittedPayload"), "Delete form should have been submitted")
        self.assertEqual(rep.get("submittedPayload", {}).get("transaction_id"), tx2_id)
        self.assertNotEqual(rep.get("submittedPayload", {}).get("transaction_id"), tx1_id)

    def test_popover_content_escaped_and_dismisses_on_esc_and_click_outside(self):
        """Popover content is plain-text escaped and dismisses when pressing Escape or clicking outside."""
        from django.utils import timezone
        from .models import ExchangeRate
        from .services import create_transaction_entry
        import pathlib
        from django.conf import settings

        ExchangeRate.objects.update_or_create(
            from_currency=self.usd,
            to_currency=self.eur,
            scope=ExchangeRate.Scope.PROVIDER,
            is_manual=False,
            defaults={
                "rate": Decimal("0.92000000"),
                "rate_timestamp": timezone.now() - timezone.timedelta(hours=72),
                "provider": "frankfurter_v2",
            },
        )
        resp = self.client.get(self.transactions_url + "?display_currency=EUR")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")

        browser_bin = self._find_browser_binary()
        if not browser_bin:
            return

        static_dir = pathlib.Path(settings.BASE_DIR) / "static"
        bs_js = (static_dir / "vendor" / "bootstrap" / "bootstrap.bundle.min.js").read_text(encoding="utf-8")
        bs_css = (static_dir / "vendor" / "bootstrap" / "bootstrap.min.css").read_text(encoding="utf-8")
        app_css = (static_dir / "css" / "app.css").read_text(encoding="utf-8")
        dash_css = (static_dir / "css" / "dashboard.css").read_text(encoding="utf-8")
        tx_js = (static_dir / "js" / "pages" / "transaction.js").read_text(encoding="utf-8")

        harness_html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <style>{bs_css}</style>
    <style>{app_css}</style>
    <style>{dash_css}</style>
    <style>*, *::before, *::after {{ transition: none !important; animation: none !important; }}</style>
</head>
<body class="app-body">
    {html}
    <script>{bs_js}</script>
    <script>
    if (window.bootstrap && window.bootstrap.Popover && window.bootstrap.Popover.Default) {{
        window.bootstrap.Popover.Default.animation = false;
    }}
    </script>
    <script>{tx_js}</script>
    <script>
    window.addEventListener('DOMContentLoaded', async function () {{
        await new Promise(r => setTimeout(r, 200));
        var results = {{}};

        var popoverTrigger = document.querySelector('[data-bs-toggle="popover"]');
        if (popoverTrigger) {{
            // 1. Open popover
            var triggerWithNewline = document.querySelector('tr[data-tx-row-id="{self.tx1.id}"] .tx-col-converted [data-bs-toggle="popover"]') || popoverTrigger;
            triggerWithNewline.click();
            await new Promise(r => setTimeout(r, 300));
            var openPopover = document.querySelector('.popover.show');
            var popBody = openPopover ? openPopover.querySelector('.popover-body') : null;
            results.openedOnFirstClick = !!openPopover;
            results.popText = popBody ? popBody.textContent : '';
            results.popWhiteSpace = popBody ? window.getComputedStyle(popBody).whiteSpace : '';
            results.popoverKeepsLineBreaks = !!(
                popBody &&
                popBody.textContent.indexOf('\\n') !== -1 &&
                window.getComputedStyle(popBody).whiteSpace === 'pre-line'
            );

            // 2. Press Escape
            document.dispatchEvent(new KeyboardEvent('keydown', {{ key: 'Escape', bubbles: true }}));
            await new Promise(r => setTimeout(r, 300));
            var popoverAfterEsc = document.querySelector('.popover.show');
            results.closedOnEscape = !popoverAfterEsc;

            // 3. Open popover again
            popoverTrigger.click();
            await new Promise(r => setTimeout(r, 300));
            var openPopover2 = document.querySelector('.popover.show');
            results.openedOnSecondClick = !!openPopover2;

            // 4. Click outside
            document.body.click();
            await new Promise(r => setTimeout(r, 300));
            var popoverAfterOutsideClick = document.querySelector('.popover.show');
            results.closedOnOutsideClick = !popoverAfterOutsideClick;
        }}

        fetch('/__test_done__', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify(results)
        }}).catch(function () {{}});
    }});
    </script>
</body>
</html>"""

        rep = self._run_browser_harness(browser_bin, 1280, 800, harness_html)
        self.assertIsNotNone(rep, "Popover browser harness timed out")
        self.assertTrue(rep.get("openedOnFirstClick"), "Popover should open on click")
        self.assertTrue(rep.get("popoverKeepsLineBreaks"), f"Popover text should keep line breaks: text={rep.get('popText')!r}, ws={rep.get('popWhiteSpace')!r}")
        self.assertTrue(rep.get("closedOnEscape"), "Popover should close when Escape key is pressed")
        self.assertTrue(rep.get("openedOnSecondClick"), "Popover should re-open on click")
        self.assertTrue(rep.get("closedOnOutsideClick"), "Popover should close when clicking outside")

    def test_summary_totals_match_displayed_row_conversions_across_dates(self):
        """(a) Summary totals equal the sum of displayed row conversions with transactions on different dates in one foreign currency."""
        from django.utils import timezone
        from .models import ExchangeRate
        from .services import create_transaction_entry

        wallet_eur = Wallet.objects.create(
            owner=self.user,
            name="EUR Checking",
            wallet_type=Wallet.WalletType.BANK_ACCOUNT,
            currency=self.eur,
            opening_balance=Decimal("2000.00"),
            opening_balance_date=date(2026, 1, 1),
        )
        # Rate 1 EUR = 1.12 USD
        ExchangeRate.objects.update_or_create(
            from_currency=self.eur,
            to_currency=self.usd,
            scope=ExchangeRate.Scope.PROVIDER,
            is_manual=False,
            defaults={
                "rate": Decimal("1.12000000"),
                "rate_timestamp": timezone.now(),
                "provider": "frankfurter_v2",
            },
        )
        create_transaction_entry(
            user=self.user,
            transaction_type="expense",
            amount=Decimal("25.00"),
            wallet=wallet_eur,
            transaction_date=date(2026, 11, 1),
            description="EUR Dinner Nov 1",
            scope="personal",
        )
        create_transaction_entry(
            user=self.user,
            transaction_type="expense",
            amount=Decimal("40.00"),
            wallet=wallet_eur,
            transaction_date=date(2026, 11, 2),
            description="EUR Train Nov 2",
            scope="personal",
        )

        resp = self.client.get(self.transactions_url + "?currency=EUR")
        self.assertEqual(resp.status_code, 200)
        tx_list = resp.context["transactions_list"]
        self.assertEqual(len(tx_list), 2)

        row_sum = sum(tx.display_amount for tx in tx_list)
        summary_expense = resp.context["summary_totals"]["expense"]
        # 25.00 * 1.12 = 28.00 USD; 40.00 * 1.12 = 44.80 USD; sum = 72.80 USD
        self.assertEqual(row_sum, Decimal("72.80"))
        self.assertEqual(summary_expense, row_sum)

    def test_topbar_currency_post_redirects_without_display_currency_param(self):
        """(b) Topbar currency POST while ?display_currency=XXX is active redirects without that parameter."""
        resp = self.client.post(
            reverse("accounts:set_display_currency"),
            {
                "currency_code": "EUR",
                "next": "/transactions/?q=Supermarket&display_currency=USD",
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("display_currency=", resp.url)
        self.assertIn("q=Supermarket", resp.url)

    def test_inline_filter_submission_drops_page_param_and_search_refocuses_at_end(self):
        """(c) Submitting an inline filter drops ?page=; (d) after search auto-submit the input is refocused with cursor at the end."""
        import pathlib
        from django.conf import settings

        resp = self.client.get(self.transactions_url + "?page=2&q=Supermarket")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")

        # Filter form has no hidden or visible input named "page", so submitting it drops ?page=
        filter_form_segment = html.split('id="transaction-filter-form"')[1].split("</form>")[0]
        self.assertNotIn('name="page"', filter_form_segment)

        browser_bin = self._find_browser_binary()
        if not browser_bin:
            return

        static_dir = pathlib.Path(settings.BASE_DIR) / "static"
        bs_js = (static_dir / "vendor" / "bootstrap" / "bootstrap.bundle.min.js").read_text(encoding="utf-8")
        bs_css = (static_dir / "vendor" / "bootstrap" / "bootstrap.min.css").read_text(encoding="utf-8")
        app_css = (static_dir / "css" / "app.css").read_text(encoding="utf-8")
        dash_css = (static_dir / "css" / "dashboard.css").read_text(encoding="utf-8")
        tx_js = (static_dir / "js" / "pages" / "transaction.js").read_text(encoding="utf-8")

        harness_html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <style>{bs_css}</style>
    <style>{app_css}</style>
    <style>{dash_css}</style>
</head>
<body class="app-body">
    {html}
    <script>
    try {{ sessionStorage.setItem('tx_search_autofocus', '1'); }} catch (e) {{}}
    </script>
    <script>{bs_js}</script>
    <script>{tx_js}</script>
    <script>
    window.addEventListener('DOMContentLoaded', async function () {{
        await new Promise(r => setTimeout(r, 150));
        var results = {{}};
        var qInput = document.getElementById('filter-search-notes');
        var filterForm = document.getElementById('transaction-filter-form');

        // Check refocus & cursor at end on page load after search auto-submit
        results.isFocusedOnLoad = (document.activeElement === qInput);
        results.cursorAtEndOnLoad = qInput ? (qInput.selectionStart === qInput.value.length && qInput.selectionEnd === qInput.value.length) : false;

        // Check inline filter submission drops ?page=
        var submittedQuery = null;
        if (filterForm) {{
            filterForm.submit = function () {{
                var fd = new FormData(filterForm);
                var params = new URLSearchParams(fd);
                submittedQuery = params.toString();
            }};
        }}
        if (qInput) {{
            qInput.value = 'Groceries';
            qInput.dispatchEvent(new Event('input', {{ bubbles: true }}));
            await new Promise(r => setTimeout(r, 500));
            results.submittedQuery = submittedQuery;
            results.droppedPageParam = submittedQuery !== null && submittedQuery.indexOf('page=') === -1;
            results.isFocusedAfterAutoSubmit = (document.activeElement === qInput);
            results.cursorAtEndAfterAutoSubmit = (qInput.selectionStart === qInput.value.length && qInput.selectionEnd === qInput.value.length);
        }}

        fetch('/__test_done__', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify(results)
        }}).catch(function () {{}});
    }});
    </script>
</body>
</html>"""

        rep = self._run_browser_harness(browser_bin, 1280, 800, harness_html)
        self.assertIsNotNone(rep, "Search autofocus / filter submission harness timed out")
        self.assertTrue(rep.get("isFocusedOnLoad"), "Search input should be refocused after search auto-submit reload")
        self.assertTrue(rep.get("cursorAtEndOnLoad"), "Cursor should be placed at the end of search input")
        self.assertTrue(rep.get("droppedPageParam"), "Submitting inline filter must drop ?page=")
        self.assertTrue(rep.get("isFocusedAfterAutoSubmit"), "Search input remains focused on auto-submit")
        self.assertTrue(rep.get("cursorAtEndAfterAutoSubmit"), "Cursor remains at the end of search input")

    def test_missing_conversion_badge_when_rate_unavailable(self):
        """When an exchange rate is missing, row displays 'Conversion unavailable' and summary badge shows unconverted count."""
        from .models import Currency
        from .services import create_transaction_entry

        jpy, _ = Currency.objects.get_or_create(code="JPY", defaults={"name": "Yen", "decimal_places": 0, "is_active": True})
        wallet_jpy = Wallet.objects.create(
            owner=self.user,
            name="JPY Wallet",
            wallet_type=Wallet.WalletType.BANK_ACCOUNT,
            currency=jpy,
            opening_balance=Decimal("10000"),
            opening_balance_date=date(2026, 1, 1),
        )
        from django.utils import timezone
        today = timezone.localdate()
        create_transaction_entry(
            user=self.user,
            transaction_type="expense",
            amount=Decimal("5000"),
            wallet=wallet_jpy,
            transaction_date=today,
            description="JPY Item No Rate",
            scope="personal",
        )
        resp = self.client.get(self.transactions_url)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")
        self.assertIn("Conversion unavailable", html)
        self.assertIn("Excludes 1 unconverted", html)

    def test_date_group_rows_hidden_when_empty_and_sorted_descending(self):
        """Date group rows group transactions by date and all transactions are ordered descending."""
        from .services import create_transaction_entry

        self.tx1.transaction_date = date(2026, 11, 2)
        self.tx1.save()

        t_earlier = create_transaction_entry(
            user=self.user,
            transaction_type="income",
            amount=Decimal("50.00"),
            wallet=self.wallet1,
            transaction_date=date(2026, 11, 1),
            description="Early income",
            scope="personal",
        )
        t_later = create_transaction_entry(
            user=self.user,
            transaction_type="income",
            amount=Decimal("75.00"),
            wallet=self.wallet1,
            transaction_date=date(2026, 11, 3),
            description="Late income",
            scope="personal",
        )
        resp = self.client.get(self.transactions_url)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")

        pos_later = html.find("Late income")
        pos_tx1 = html.find("Supermarket haul")
        pos_earlier = html.find("Early income")
        self.assertTrue(pos_later < pos_tx1 < pos_earlier, "Transactions must be sorted by date descending")

        self.assertIn('data-date-group="2026-11-03"', html)
        self.assertIn('data-date-group="2026-11-02"', html)
        self.assertIn('data-date-group="2026-11-01"', html)

    def test_converted_column_visibility_consistent_across_pages_when_foreign_currency_on_page_2(self):
        """Converted column visibility is computed over the full filtered queryset so page 1 and page 2 match."""
        from django.utils import timezone
        from .models import Wallet
        from .services import create_transaction_entry

        eur_wallet = Wallet.objects.create(
            owner=self.user,
            name="EUR Savings",
            wallet_type=Wallet.WalletType.BANK_ACCOUNT,
            currency=self.eur,
            opening_balance=Decimal("1000.00"),
            opening_balance_date=date(2026, 1, 1),
        )
        # Create 25 newer USD transactions so page 1 has only USD rows
        today = timezone.localdate()
        for idx in range(25):
            create_transaction_entry(
                user=self.user,
                transaction_type="expense",
                amount=Decimal("10.00"),
                wallet=self.wallet1,
                transaction_date=today,
                description=f"USD Page1 Item {idx:02d}",
                scope="personal",
            )
        # Create 1 older EUR transaction that lands on page 2 in the same month
        create_transaction_entry(
            user=self.user,
            transaction_type="expense",
            amount=Decimal("45.00"),
            wallet=eur_wallet,
            transaction_date=date(today.year, today.month, 1),
            description="EUR Page2 Item",
            scope="personal",
        )

        resp_p1 = self.client.get(f"{self.transactions_url}?page=1")
        resp_p2 = self.client.get(f"{self.transactions_url}?page=2")
        self.assertEqual(resp_p1.status_code, 200)
        self.assertEqual(resp_p2.status_code, 200)

        # Page 1 has only USD rows rendered, and Page 2 has the EUR row,
        # yet both pages must have hide_converted_column == False
        self.assertFalse(resp_p1.context["hide_converted_column"])
        self.assertFalse(resp_p2.context["hide_converted_column"])
        html_p1 = resp_p1.content.decode("utf-8")
        html_p2 = resp_p2.content.decode("utf-8")
        self.assertIn('data-hide-converted="false"', html_p1)
        self.assertIn('data-hide-converted="false"', html_p2)
        self.assertNotIn("tx-col-converted d-none", html_p1)
        self.assertNotIn("tx-col-converted d-none", html_p2)

        # When filtered to USD only (all 26 USD rows across page 1 and page 2),
        # hide_converted_column is True on both pages
        resp_usd_p1 = self.client.get(f"{self.transactions_url}?currency=USD&page=1")
        resp_usd_p2 = self.client.get(f"{self.transactions_url}?currency=USD&page=2")
        self.assertTrue(resp_usd_p1.context["hide_converted_column"])
        self.assertTrue(resp_usd_p2.context["hide_converted_column"])
        self.assertIn('data-hide-converted="true"', resp_usd_p1.content.decode("utf-8"))
        self.assertIn('data-hide-converted="true"', resp_usd_p2.content.decode("utf-8"))

    def test_browser_desktop_and_mobile_layout_and_modal_interaction(self):
        import pathlib
        from django.conf import settings

        browser_bin = self._find_browser_binary()
        if not browser_bin:
            self.skipTest("No browser binary found for headless verification.")

        static_dir = pathlib.Path(settings.BASE_DIR) / "static"
        bs_js = (static_dir / "vendor" / "bootstrap" / "bootstrap.bundle.min.js").read_text(encoding="utf-8")
        bs_css = (static_dir / "vendor" / "bootstrap" / "bootstrap.min.css").read_text(encoding="utf-8")
        app_css = (static_dir / "css" / "app.css").read_text(encoding="utf-8")
        dash_css = (static_dir / "css" / "dashboard.css").read_text(encoding="utf-8")
        tx_js = (static_dir / "js" / "pages" / "transaction.js").read_text(encoding="utf-8")

        # -------------------------------------------------------------
        # 1. Desktop Viewport Test (1280 x 800)
        # -------------------------------------------------------------
        resp_desktop = self.client.get(self.transactions_url)
        page_desktop_html = resp_desktop.content.decode("utf-8")

        desktop_harness = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>{bs_css}</style>
    <style>{app_css}</style>
    <style>{dash_css}</style>
    <style>
    .transactions-table .text-nowrap {{ white-space: nowrap; }}
    </style>
</head>
<body class="app-body">
    {page_desktop_html}
    <script>{bs_js}</script>
    <script>{tx_js}</script>
    <script>
    window.addEventListener('DOMContentLoaded', async function () {{
        await new Promise(r => setTimeout(r, 200));

        var results = {{}};
        results.viewportWidth = window.innerWidth;
        results.viewportHeight = window.innerHeight;

        var filterBtn = document.getElementById('btn-toggle-filters');
        var filterCollapse = document.getElementById('transaction-filter-collapse');
        var recordBtn = document.getElementById('btn-record-transfer');
        var transferModal = document.getElementById('transferModal');

        // Initial filter state (collapsed)
        results.filterCollapseInitialHidden = filterCollapse ? !filterCollapse.classList.contains('show') : false;
        results.filterBtnInitialAria = filterBtn ? filterBtn.getAttribute('aria-expanded') : '';

        // Toggle filter panel open
        if (filterBtn && filterCollapse) {{
            filterBtn.click();
            await new Promise(r => setTimeout(r, 500));
            results.filterCollapseShownAfterClick = filterCollapse.classList.contains('show');
            results.filterBtnAriaAfterClick = filterBtn.getAttribute('aria-expanded');

            // Toggle filter panel closed
            filterBtn.click();
            await new Promise(r => setTimeout(r, 500));
            results.filterCollapseHiddenAfterSecondClick = !filterCollapse.classList.contains('show');
        }}

        // Transfer modal open and close
        if (recordBtn && transferModal) {{
            recordBtn.click();
            await new Promise(r => setTimeout(r, 500));
            results.modalShown = transferModal.classList.contains('show') || getComputedStyle(transferModal).display !== 'none';
            results.modalHasSensibleFocus = transferModal.contains(document.activeElement);

            var closeBtn = transferModal.querySelector('.btn-close');
            if (closeBtn) {{
                closeBtn.click();
                await new Promise(r => setTimeout(r, 500));
                results.modalClosed = !transferModal.classList.contains('show');
            }}
        }}

        fetch('/__test_done__', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify(results)
        }}).catch(function () {{}});
    }});
    </script>
</body>
</html>"""

        rep_desktop = self._run_browser_harness(browser_bin, 1280, 800, desktop_harness)
        self.assertIsNotNone(rep_desktop, "Desktop browser verification timed out")
        # Verify actual viewport dimensions
        self.assertGreaterEqual(rep_desktop.get("viewportWidth", 0), 1024, "Desktop viewport width should be >= 1024")
        self.assertGreaterEqual(rep_desktop.get("viewportHeight", 0), 600, "Desktop viewport height should be >= 600")
        # Verify filter toggle
        self.assertTrue(rep_desktop.get("filterCollapseInitialHidden"), "Filter collapse should be initially hidden")
        self.assertEqual(rep_desktop.get("filterBtnInitialAria"), "false", "Filter button aria-expanded initially false")
        self.assertTrue(rep_desktop.get("filterCollapseShownAfterClick"), "Filter collapse should expand on click")
        self.assertEqual(rep_desktop.get("filterBtnAriaAfterClick"), "true", "Filter button aria-expanded true when open")
        self.assertTrue(rep_desktop.get("filterCollapseHiddenAfterSecondClick"), "Filter collapse should close on second click")
        # Verify modal open/close
        self.assertTrue(rep_desktop.get("modalShown"), "Transfer modal should open on Record transfer click")
        self.assertTrue(rep_desktop.get("modalHasSensibleFocus"), "Sensible focus should be directed to the modal")
        self.assertTrue(rep_desktop.get("modalClosed"), "Transfer modal should close on close button click")

        # -------------------------------------------------------------
        # 2. Invalid Transfer Auto-Reopen & Focus Test
        # -------------------------------------------------------------
        post_bad_transfer = {
            "action": "create_transfer",
            "source_wallet": str(self.wallet1.pk),
            "destination_wallet": "",
            "amount_sent": "",
            "transfer_date": "2026-10-22",
        }
        resp_bad = self.client.post(self.transactions_url, post_bad_transfer)
        page_bad_html = resp_bad.content.decode("utf-8")

        bad_transfer_harness = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>{bs_css}</style>
    <style>{app_css}</style>
    <style>{dash_css}</style>
</head>
<body class="app-body">
    {page_bad_html}
    <script>{bs_js}</script>
    <script>{tx_js}</script>
    <script>
    window.addEventListener('DOMContentLoaded', async function () {{
        await new Promise(r => setTimeout(r, 600));
        var results = {{}};
        results.viewportWidth = window.innerWidth;

        var transferModal = document.getElementById('transferModal');
        results.autoReopenedModalShown = transferModal && (transferModal.classList.contains('show') || getComputedStyle(transferModal).display !== 'none');

        var errorAlert = transferModal ? transferModal.querySelector('.alert-danger') : null;
        results.hasVisibleErrors = !!(errorAlert && errorAlert.textContent.trim().length > 0);

        var activeEl = document.activeElement;
        results.sensibleFocusInModal = transferModal && (transferModal.contains(activeEl) || activeEl === transferModal);

        fetch('/__test_done__', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify(results)
        }}).catch(function () {{}});
    }});
    </script>
</body>
</html>"""

        rep_auto = self._run_browser_harness(browser_bin, 1280, 800, bad_transfer_harness)
        self.assertIsNotNone(rep_auto, "Auto-reopen browser verification timed out")
        self.assertTrue(rep_auto.get("autoReopenedModalShown"), "Modal should automatically reopen when validation fails")
        self.assertTrue(rep_auto.get("hasVisibleErrors"), "Modal should display visible validation errors")
        self.assertTrue(rep_auto.get("sensibleFocusInModal"), "Sensible focus should be directed within reopened modal")

        # -------------------------------------------------------------
        # 3. Mobile Viewport Test (375 x 667)
        # -------------------------------------------------------------
        mobile_harness = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>{bs_css}</style>
    <style>{app_css}</style>
    <style>{dash_css}</style>
    <style>
    .transactions-table .text-nowrap {{ white-space: nowrap; }}
    </style>
</head>
<body class="app-body">
    {page_desktop_html}
    <script>{bs_js}</script>
    <script>{tx_js}</script>
    <script>
    window.addEventListener('DOMContentLoaded', async function () {{
        await new Promise(r => setTimeout(r, 200));

        var results = {{}};
        results.viewportWidth = window.innerWidth;
        results.viewportHeight = window.innerHeight;

        var filterBtn = document.getElementById('btn-toggle-filters');
        var exportCsv = document.getElementById('btn-export-csv');
        var exportJson = document.getElementById('btn-export-json');
        var recordBtn = document.getElementById('btn-record-transfer');
        var manageCatBtn = document.getElementById('btn-manage-categories');
        var transferModal = document.getElementById('transferModal');
        var txTable = document.querySelector('.transactions-table');
        var tableResponsive = document.querySelector('.table-responsive');

        // Verify toolbar wrapping & all buttons reachable
        results.allToolbarButtonsReachable = !!(filterBtn && exportCsv && exportJson && recordBtn && manageCatBtn);
        results.noHorizontalWindowOverflow = document.documentElement.scrollWidth <= (window.innerWidth + 5);

        // Transaction table horizontal scrolling & non-wrapped monetary cells
        results.tableResponsiveExists = !!tableResponsive;
        var nonWrapCells = txTable ? txTable.querySelectorAll('.text-nowrap') : [];
        results.hasNonWrapCells = nonWrapCells.length > 0;

        // Modal fit on mobile
        if (recordBtn && transferModal) {{
            recordBtn.click();
            await new Promise(r => setTimeout(r, 500));
            var modalDialog = transferModal.querySelector('.modal-dialog');
            var dialogRect = modalDialog ? modalDialog.getBoundingClientRect() : null;
            results.mobileModalFitsViewport = dialogRect ? (dialogRect.right <= window.innerWidth + 5) : false;
        }}

        fetch('/__test_done__', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify(results)
        }}).catch(function () {{}});
    }});
    </script>
</body>
</html>"""

        rep_mobile = self._run_browser_harness(browser_bin, 375, 667, mobile_harness)
        self.assertIsNotNone(rep_mobile, "Mobile browser verification timed out")
        # Verify actual mobile viewport dimensions (Bootstrap xs mobile breakpoint is < 576px)
        self.assertLessEqual(rep_mobile.get("viewportWidth", 999), 575, "Mobile viewport width should be <= 575 (Bootstrap mobile breakpoint)")
        self.assertGreaterEqual(rep_mobile.get("viewportHeight", 0), 500, "Mobile viewport height should be >= 500")
        # Verify wrapping & no window overflow
        self.assertTrue(rep_mobile.get("allToolbarButtonsReachable"), "All toolbar buttons should be reachable on mobile")
        self.assertTrue(rep_mobile.get("noHorizontalWindowOverflow"), "Layout should wrap cleanly without overflowing mobile viewport")
        # Verify table scrolling and non-wrap amounts
        self.assertTrue(rep_mobile.get("tableResponsiveExists"), "Table responsive wrapper should exist")
        self.assertTrue(rep_mobile.get("hasNonWrapCells"), "Financial amounts should have text-nowrap to avoid clipping")
        self.assertTrue(rep_mobile.get("mobileModalFitsViewport"), "Transfer modal should fit within mobile viewport")

    def test_responsive_viewports_and_zoom_levels(self):
        """Headless-browser verification across all 14 viewports and zoom cases (150% & 200%)."""
        import http.server
        import json
        import os
        import pathlib
        import shutil
        import socket
        import subprocess
        import tempfile
        import threading
        from django.conf import settings
        from django.utils import timezone
        from .models import ExchangeRate
        from .services import create_transaction_entry

        browser_bin = self._find_browser_binary()
        if not browser_bin:
            self.skipTest("No browser binary found for headless responsive verification.")

        # Add a second currency transaction with stale rate so Converted column, stale icon, and long description are tested
        wallet_eur = Wallet.objects.create(
            owner=self.user,
            name="EUR Travel Account",
            wallet_type=Wallet.WalletType.BANK_ACCOUNT,
            currency=self.eur,
            opening_balance=Decimal("3000.00"),
            opening_balance_date=date(2026, 1, 1),
        )
        ExchangeRate.objects.update_or_create(
            from_currency=self.eur,
            to_currency=self.usd,
            scope=ExchangeRate.Scope.PROVIDER,
            is_manual=False,
            defaults={
                "rate": Decimal("1.08500000"),
                "rate_timestamp": timezone.now() - timezone.timedelta(hours=96),
                "provider": "frankfurter_v2",
            },
        )
        today = timezone.localdate()
        create_transaction_entry(
            user=self.user,
            transaction_type="expense",
            amount=Decimal("245.80"),
            wallet=wallet_eur,
            transaction_date=today,
            category=self.category,
            description="International conference hotel booking and catering services with long description text",
            notes="Receipt #9042-B attached",
            scope="personal",
        )
        create_transaction_entry(
            user=self.user,
            transaction_type="income",
            amount=Decimal("2850.00"),
            wallet=self.wallet1,
            transaction_date=today,
            description="Monthly consulting retainer deposit",
            scope="personal",
        )

        # Verify server-side Converted-column hiding when all filtered rows match display currency
        resp_same_curr = self.client.get(self.transactions_url + "?currency=USD")
        self.assertEqual(resp_same_curr.status_code, 200)
        self.assertTrue(resp_same_curr.context["hide_converted_column"])
        same_curr_html = resp_same_curr.content.decode("utf-8")
        self.assertIn('data-hide-converted="true"', same_curr_html)
        self.assertIn('tx-col-converted d-none', same_curr_html)
        self.assertIn('<td colspan="7">', same_curr_html)

        # Verify real @media (pointer: coarse) rules remain in app.css and transactions.html
        app_css_text = (pathlib.Path(settings.BASE_DIR) / "static" / "css" / "app.css").read_text(encoding="utf-8")
        tx_tpl_text = (pathlib.Path(settings.BASE_DIR) / "templates" / "budgets" / "transactions.html").read_text(encoding="utf-8")
        self.assertIn("@media (min-width: 992px) and (max-width: 1199.98px) and (pointer: coarse)", app_css_text)
        self.assertIn("font-size: 0.75rem;", app_css_text)
        self.assertIn("@media (pointer: coarse)", app_css_text)
        self.assertIn("@media (pointer: coarse)", tx_tpl_text)
        self.assertNotIn("emulate-coarse-pointer", app_css_text)
        self.assertNotIn("emulate-coarse-pointer", tx_tpl_text)

        resp = self.client.get(self.transactions_url)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.context["hide_converted_column"])
        page_html = resp.content.decode("utf-8").replace('data-sync-state-url="', 'data-sync-state-url-disabled="')
        # Inject test-only CSS to disable transitions/animations during browser inspection
        test_no_anim_css = "<style>*, *::before, *::after { transition: none !important; animation: none !important; }</style></head>"
        page_html_clean = page_html.replace("</head>", test_no_anim_css)

        screenshots_dir = pathlib.Path(tempfile.mkdtemp(prefix="tx_responsive_screenshots_"))
        artifact_dir = pathlib.Path(r"C:\Users\franz\.gemini\antigravity\brain\9adec51b-2e14-4ba0-89bc-7ad056968045")

        viewports = [
            {"name": "360x800", "w": 360, "h": 800, "zoom": 1.0, "coarse": True},
            {"name": "390x844", "w": 390, "h": 844, "zoom": 1.0, "coarse": True},
            {"name": "430x932", "w": 430, "h": 932, "zoom": 1.0, "coarse": True},
            {"name": "844x390", "w": 844, "h": 390, "zoom": 1.0, "coarse": False},
            {"name": "768x1024", "w": 768, "h": 1024, "zoom": 1.0, "coarse": True},
            {"name": "1024x768", "w": 1024, "h": 768, "zoom": 1.0, "coarse": False},
            {"name": "1180x820", "w": 1180, "h": 820, "zoom": 1.0, "coarse": False},
            {"name": "1280x720", "w": 1280, "h": 720, "zoom": 1.0, "coarse": False},
            {"name": "1366x768", "w": 1366, "h": 768, "zoom": 1.0, "coarse": False},
            {"name": "1536x864", "w": 1536, "h": 864, "zoom": 1.0, "coarse": False},
            {"name": "1920x1080", "w": 1920, "h": 1080, "zoom": 1.0, "coarse": False},
            {"name": "2560x1440", "w": 2560, "h": 1440, "zoom": 1.0, "coarse": False},
            {"name": "1280x720_zoom150", "w": 1280, "h": 720, "zoom": 1.5, "coarse": False},
            {"name": "1280x720_zoom200", "w": 1280, "h": 720, "zoom": 2.0, "coarse": False},
        ]

        temp_dir = tempfile.mkdtemp(prefix="tx_resp_suite_")
        try:
            static_dst = os.path.join(temp_dir, "static")
            repo_static = os.path.join(settings.BASE_DIR, "static")
            shutil.copytree(repo_static, static_dst)

            with open(os.path.join(temp_dir, "tx_fine.html"), "w", encoding="utf-8") as f:
                f.write(page_html_clean)
            with open(os.path.join(temp_dir, "tx_coarse.html"), "w", encoding="utf-8") as f:
                f.write(page_html_clean)

            results_holder = {}
            done_event = threading.Event()

            class QuietHandler(http.server.SimpleHTTPRequestHandler):
                def __init__(self, *args, **kwargs):
                    super().__init__(*args, directory=temp_dir, **kwargs)

                def log_message(self, format, *args):
                    pass

                def do_POST(self):
                    if self.path == "/__test_done__":
                        length = int(self.headers.get("Content-Length", "0"))
                        raw = self.rfile.read(length).decode("utf-8")
                        results_holder["report"] = json.loads(raw)
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", "11")
                        self.end_headers()
                        self.wfile.write(b'{"ok":true}')
                        done_event.set()
                    else:
                        self.send_response(404)
                        self.end_headers()

            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            sock.close()

            httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), QuietHandler)
            server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            server_thread.start()

            try:
                # 1. Run single fast multi-viewport inspection pass in headless Chrome
                viewports_json = json.dumps(viewports)
                runner_html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0; padding:0; background:#fff;">
<iframe id="vp-frame" style="border:none; display:block;"></iframe>
<script>
const viewports = __VIEWPORTS_JSON__;
const includeExtras = __INCLUDE_EXTRAS__;
const frame = document.getElementById('vp-frame');
const allReports = {{}};

function loadFrame(url) {{
    return new Promise((resolve) => {{
        frame.onload = function() {{
            setTimeout(resolve, 250);
        }};
        frame.src = url;
    }});
}}

function inspectViewport(vp) {{
    return new Promise((resolve) => {{
        const cssW = Math.round(vp.w / vp.zoom);
        const cssH = Math.round(vp.h / vp.zoom);
        frame.style.width = cssW + 'px';
        frame.style.height = cssH + 'px';
        const doc = frame.contentDocument;
        const win = frame.contentWindow;
        setTimeout(function() {{
            try {{
                const pointerCoarseMatches = !!(win.matchMedia && win.matchMedia('(pointer: coarse)').matches);
                const scrollW = doc.documentElement.scrollWidth;
                const clientW = doc.documentElement.clientWidth;

                // Check no element in main content overflows its container horizontally
                const mainEl = doc.getElementById('main-content');
                const mainRect = mainEl.getBoundingClientRect();
                let overflowingElements = [];
                mainEl.querySelectorAll('.stationery-panel, #tx-summary-strip, .transactions-table, tr.tx-data-row').forEach(function(el) {{
                    const style = win.getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden') return;
                    const r = el.getBoundingClientRect();
                    if (r.width > 0 && r.right > mainRect.right + 2) {{
                        overflowingElements.push(el.tagName + '.' + el.className + ' (right=' + Math.round(r.right) + ' > mainRight=' + Math.round(mainRect.right) + ')');
                    }}
                }});

                // Check key interactive controls are visible and clickable
                const requiredSelectors = [
                    '#filter-search-notes',
                    '#btn-toggle-filters',
                    '#btn-more-actions',
                    '#btn-record-transfer',
                    '.btn-topbar-action',
                    '#topbarAccountDropdown',
                    '#workspace-view-select',
                    '#display-currency-select',
                    '.btn-edit-tx'
                ];
                let missingControls = [];
                requiredSelectors.forEach(function(sel) {{
                    const el = doc.querySelector(sel);
                    if (!el) {{
                        missingControls.push(sel + ' (missing)');
                        return;
                    }}
                    const st = win.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    if (st.display === 'none' || st.visibility === 'hidden' || rect.width === 0 || rect.height === 0) {{
                        missingControls.push(sel + ' (not visible)');
                    }}
                }});

                // On coarse pointer emulation, verify primary interactive controls are >= 40px high
                // (small controls inside dense tables and .btn-sm use pseudo-element hit-areas to avoid growing rows)
                let smallButtons = [];
                if (vp.coarse) {{
                    doc.querySelectorAll('button:not(.btn-sm):not(table button):not(.tx-icon-btn), .btn:not(.btn-sm):not(table .btn):not(.tx-icon-btn), .tx-stale-rate-icon').forEach(function(btn) {{
                        const st = win.getComputedStyle(btn);
                        if (st.display === 'none' || st.visibility === 'hidden') return;
                        const rect = elRect(btn);
                        if (rect.width === 0 || rect.height === 0) return;
                        if (rect.height < 39.5) {{
                            smallButtons.push((btn.id || btn.className || btn.tagName) + ' (h=' + rect.height.toFixed(1) + ')');
                        }}
                    }});
                }}
                function elRect(b) {{ return b.getBoundingClientRect(); }}

                // Check main content max-width at 2560px (<= 1600px)
                const mainMaxWidthOk = mainRect.width <= 1602;

                // Check none of the modals or any position:fixed element sits inside a container-type element
                let fixedInsideContainer = [];
                doc.querySelectorAll('.modal, #transactionModal, #deleteConfirmModal, #transferModal, .app-toast-container, .btn-topbar-action').forEach(function(el) {{
                    const st = win.getComputedStyle(el);
                    const isModalOrFixed = el.classList.contains('modal') || st.position === 'fixed';
                    if (!isModalOrFixed) return;
                    let parent = el.parentElement;
                    while (parent) {{
                        const pst = win.getComputedStyle(parent);
                        if (pst.containerType && pst.containerType !== 'normal') {{
                            fixedInsideContainer.push((el.id || el.className) + ' inside ' + (parent.id || parent.className));
                            break;
                        }}
                        parent = parent.parentElement;
                    }}
                }});

                // Under coarse pointer emulation, test touch area: document.elementFromPoint at 18px above and below center of small table button
                let tableButtonTouchAreaOk = true;
                let tableButtonTouchDetails = null;
                if (vp.coarse) {{
                    const visibleBtns = Array.from(doc.querySelectorAll('.transactions-table td button, .transactions-table .btn, .btn.btn-sm, table button')).filter(function(b) {{
                        return b.offsetWidth > 0 && b.offsetHeight > 0;
                    }});
                    const tableBtn = visibleBtns[0];
                    if (tableBtn) {{
                        const pageY = tableBtn.getBoundingClientRect().top + (win.pageYOffset || doc.documentElement.scrollTop || 0);
                        const targetScroll = Math.max(0, pageY - 200);
                        win.scrollTo(0, targetScroll);
                        if (doc.documentElement) doc.documentElement.scrollTop = targetScroll;
                        if (doc.body) doc.body.scrollTop = targetScroll;
                        const tr = tableBtn.getBoundingClientRect();
                        const cx = tr.left + tr.width / 2;
                        const cy = tr.top + tr.height / 2;
                        const elAbove = doc.elementFromPoint(cx, cy - 18);
                        const elBelow = doc.elementFromPoint(cx, cy + 18);
                        const aboveHit = !!(elAbove && (elAbove === tableBtn || tableBtn.contains(elAbove)));
                        const belowHit = !!(elBelow && (elBelow === tableBtn || tableBtn.contains(elBelow)));
                        tableButtonTouchAreaOk = aboveHit && belowHit;
                        tableButtonTouchDetails = {{
                            cx: Math.round(cx),
                            cy: Math.round(cy),
                            w: Math.round(tr.width),
                            h: Math.round(tr.height),
                            aboveHit: aboveHit,
                            belowHit: belowHit,
                            aboveEl: elAbove ? (elAbove.id || elAbove.className || elAbove.tagName) : null,
                            belowEl: elBelow ? (elBelow.id || elBelow.className || elBelow.tagName) : null
                        }};
                    }}
                }}

                // Measure control height to verify pointer emulation affects rules (e.g. .btn-topbar-action)
                const topbarActionEl = doc.querySelector('.btn-topbar-action');
                const topbarActionH = topbarActionEl ? Math.round(topbarActionEl.getBoundingClientRect().height) : null;

                resolve({{
                    name: vp.name,
                    cssW: cssW,
                    cssH: cssH,
                    scrollW: scrollW,
                    clientW: clientW,
                    pointerCoarseMatches: pointerCoarseMatches,
                    noHorizontalScroll: scrollW <= clientW,
                    overflowingElements: overflowingElements,
                    missingControls: missingControls,
                    smallButtons: smallButtons,
                    tableButtonTouchAreaOk: tableButtonTouchAreaOk,
                    tableButtonTouchDetails: tableButtonTouchDetails,
                    topbarActionH: topbarActionH,
                    mainWidth: Math.round(mainRect.width),
                    mainMaxWidthOk: mainMaxWidthOk,
                    fixedInsideContainer: fixedInsideContainer
                }});
            }} catch (e) {{
                resolve({{ name: vp.name, error: e.toString() }});
            }}
        }}, 80);
    }});
}}

(async function() {{
    try {{
        await loadFrame('http://127.0.0.1:{port}/tx_fine.html');
        for (const vp of viewports) {{
            allReports[vp.name] = await inspectViewport(vp);
        }}

        if (includeExtras) {{
            // Verify Converted-hidden column width redistribution at >=1100px container widths (1366, 1920, 2560)
            const convHiddenReports = {{}};
            const doc = frame.contentDocument;
            const win = frame.contentWindow;
            const txTable = doc.querySelector('.transactions-table');
            if (txTable) {{
                txTable.classList.add('tx-converted-hidden');
                doc.querySelectorAll('.tx-col-converted').forEach(function(el) {{ el.classList.add('d-none'); }});
                for (const cw of [1366, 1920, 2560]) {{
                    frame.style.width = cw + 'px';
                    frame.style.height = '900px';
                    await new Promise(function(r) {{ setTimeout(r, 50); }});
                    const tRect = txTable.getBoundingClientRect();
                    const descTh = txTable.querySelector('th.tx-col-desc');
                    const dRect = descTh.getBoundingClientRect();
                    const descShare = tRect.width > 0 ? (dRect.width / tRect.width) : 1;
                    convHiddenReports[String(cw)] = {{
                        tableW: Math.round(tRect.width),
                        descW: Math.round(dRect.width),
                        descShare: Number(descShare.toFixed(3)),
                        descAtMost35Pct: descShare <= 0.35
                    }};
                }}
                txTable.classList.remove('tx-converted-hidden');
                doc.querySelectorAll('.tx-col-converted').forEach(function(el) {{ el.classList.remove('d-none'); }});
            }}
            allReports['__conv_hidden__'] = convHiddenReports;

            // Verify opening Edit (#transactionModal), Delete-confirm (#deleteConfirmModal), and Transfer (#transferModal)
            // at 360x800, 768x1024, and 1366x768: modal bounding box is centered in viewport and backdrop covers full viewport.
            const modalViewports = [
                {{ name: '360x800', w: 360, h: 800 }},
                {{ name: '768x1024', w: 768, h: 1024 }},
                {{ name: '1366x768', w: 1366, h: 768 }}
            ];
            const modalIds = ['transactionModal', 'deleteConfirmModal', 'transferModal'];
            const modalReports = {{}};
            const bs = win.bootstrap;

            for (const mvp of modalViewports) {{
                frame.style.width = mvp.w + 'px';
                frame.style.height = mvp.h + 'px';
                await new Promise(function(r) {{ setTimeout(r, 60); }});
                modalReports[mvp.name] = {{}};

                for (const mid of modalIds) {{
                    const topbarEl = doc.querySelector('.app-topbar');
                    const fabEl = doc.querySelector('.btn-topbar-action');
                    const beforeTopbarRect = topbarEl ? topbarEl.getBoundingClientRect() : null;
                    const beforeFabRect = fabEl ? fabEl.getBoundingClientRect() : null;

                    const mEl = doc.getElementById(mid);
                    const inst = bs.Modal.getOrCreateInstance(mEl);
                    inst.show();
                    await new Promise(function(r) {{ setTimeout(r, 220); }});

                    const dialog = mEl.querySelector('.modal-dialog');
                    const dRect = dialog.getBoundingClientRect();
                    const mClientW = mEl.clientWidth || mvp.w;
                    const backdrop = doc.querySelector('.modal-backdrop');
                    const bRect = backdrop ? backdrop.getBoundingClientRect() : null;

                    const dialogCenterX = dRect.left + dRect.width / 2;
                    const vpCenterX = mClientW / 2;
                    const centeredHorizontally = Math.abs(dialogCenterX - vpCenterX) <= 4;
                    const fitsViewport = dRect.left >= 0 && dRect.right <= mvp.w + 1;
                    const backdropCoversFullViewport = !!(
                        bRect &&
                        Math.abs(bRect.left) <= 1 &&
                        Math.abs(bRect.top) <= 1 &&
                        Math.abs(bRect.width - mvp.w) <= 2 &&
                        Math.abs(bRect.height - mvp.h) <= 2
                    );

                    const afterTopbarRect = topbarEl ? topbarEl.getBoundingClientRect() : null;
                    const afterFabRect = fabEl ? fabEl.getBoundingClientRect() : null;
                    const fabSt = fabEl ? win.getComputedStyle(fabEl) : null;
                    const fabHiddenOnMobile = mvp.w < 576 ? (fabSt && fabSt.visibility === 'hidden' && fabSt.display !== 'none') : true;
                    const desktopActionStaysVisible = mvp.w >= 576 ? !!(fabSt && fabSt.visibility === 'visible' && fabSt.display !== 'none' && afterFabRect && afterFabRect.width > 0) : true;
                    const desktopTopbarNoReflow = mvp.w >= 576 ? !!(
                        beforeTopbarRect && afterTopbarRect &&
                        Math.abs(beforeTopbarRect.height - afterTopbarRect.height) <= 1 &&
                        beforeFabRect && afterFabRect &&
                        Math.abs(beforeFabRect.top - afterFabRect.top) <= 1 &&
                        Math.abs(beforeFabRect.height - afterFabRect.height) <= 1
                    ) : true;

                    modalReports[mvp.name][mid] = {{
                        centeredHorizontally: centeredHorizontally,
                        fitsViewport: fitsViewport,
                        backdropCoversFullViewport: backdropCoversFullViewport,
                        fabHiddenOnMobile: fabHiddenOnMobile,
                        desktopActionStaysVisible: desktopActionStaysVisible,
                        desktopTopbarNoReflow: desktopTopbarNoReflow,
                        dialogLeft: Math.round(dRect.left),
                        dialogRight: Math.round(dRect.right),
                        backdropW: bRect ? Math.round(bRect.width) : 0,
                        backdropH: bRect ? Math.round(bRect.height) : 0
                    }};

                    inst.hide();
                    await new Promise(function(r) {{ setTimeout(r, 220); }});
                }}
            }}
            allReports['__modals__'] = modalReports;
        }}
    }} catch (err) {{
        allReports['__runner_error__'] = err.toString();
    }}

    fetch('/__test_done__', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify(allReports)
    }});
}})();
</script>
</body>
</html>"""
                def run_runner_pass(subset_viewports, coarse_mode=False, include_extras=False):
                    done_event.clear()
                    results_holder.clear()
                    vp_json = json.dumps(subset_viewports)
                    inc_extras_js = "true" if include_extras else "false"
                    r_html = runner_html.replace("__VIEWPORTS_JSON__", vp_json).replace("__INCLUDE_EXTRAS__", inc_extras_js)
                    r_path = os.path.join(temp_dir, "runner.html")
                    with open(r_path, "w", encoding="utf-8") as f:
                        f.write(r_html)
                    u_dir = tempfile.mkdtemp(prefix="tx_resp_chrome_")
                    cmd = [
                        browser_bin,
                        "--headless=new",
                        "--disable-gpu",
                        "--no-first-run",
                        "--no-default-browser-check",
                        "--window-size=2600,1500",
                        f"--user-data-dir={u_dir}",
                    ]
                    if coarse_mode:
                        cmd.append("--blink-settings=primaryPointerType=2,availablePointerTypes=2")
                    cmd.append(f"http://127.0.0.1:{port}/runner.html")
                    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    try:
                        fin = done_event.wait(timeout=45)
                        self.assertTrue(fin, "Responsive viewport suite timed out")
                    finally:
                        p.kill()
                        p.wait(timeout=5)
                        shutil.rmtree(u_dir, ignore_errors=True)
                    return results_holder.get("report", {})

                fine_vps = [vp for vp in viewports if not vp["coarse"]]
                coarse_vps = [vp for vp in viewports if vp["coarse"]]
                rep_fine = run_runner_pass(fine_vps, coarse_mode=False, include_extras=True)
                rep_coarse = run_runner_pass(coarse_vps, coarse_mode=True, include_extras=False)
                reports = {**rep_fine, **rep_coarse}

                self.assertNotIn("__runner_error__", reports, f"Runner error: {reports.get('__runner_error__')}")
                for vp in viewports:
                    name = vp["name"]
                    with self.subTest(viewport=name):
                        rep = reports.get(name)
                        self.assertIsNotNone(rep, f"Missing report for viewport {name}")
                        self.assertNotIn("error", rep, f"Error in viewport {name}: {rep.get('error')}")
                        self.assertTrue(
                            rep.get("noHorizontalScroll"),
                            f"Horizontal scroll detected at {name}: scrollWidth={rep.get('scrollW')} > clientWidth={rep.get('clientW')}",
                        )
                        self.assertEqual(
                            rep.get("overflowingElements"),
                            [],
                            f"Overflowing elements in main content at {name}: {rep.get('overflowingElements')}",
                        )
                        self.assertEqual(
                            rep.get("missingControls"),
                            [],
                            f"Interactive controls not visible/clickable at {name}: {rep.get('missingControls')}",
                        )
                        if vp["coarse"]:
                            self.assertTrue(
                                rep.get("pointerCoarseMatches"),
                                f"Expected matchMedia('(pointer: coarse)').matches to be True at {name}",
                            )
                            self.assertEqual(
                                rep.get("smallButtons"),
                                [],
                                f"Coarse pointer buttons < 40px high at {name}: {rep.get('smallButtons')}",
                            )
                            self.assertTrue(
                                rep.get("tableButtonTouchAreaOk"),
                                f"Small table button 18px above/below touch test failed at {name}: {rep.get('tableButtonTouchDetails')}",
                            )
                            if rep.get("topbarActionH") is not None:
                                self.assertGreaterEqual(
                                    rep.get("topbarActionH"),
                                    40,
                                    f"Expected .btn-topbar-action height >= 40 under coarse pointer at {name}",
                                )
                        else:
                            if rep.get("topbarActionH") is not None and vp["w"] >= 576:
                                self.assertLess(
                                    rep.get("topbarActionH"),
                                    40,
                                    f"Expected .btn-topbar-action height < 40 under fine pointer at {name}",
                                )
                        if vp["w"] == 2560:
                            self.assertTrue(
                                rep.get("mainMaxWidthOk"),
                                f"Main content stretched wider than 1600px at 2560px: width={rep.get('mainWidth')}",
                            )
                        self.assertEqual(
                            rep.get("fixedInsideContainer"),
                            [],
                            f"Modal or position:fixed element inside container-type parent at {name}: {rep.get('fixedInsideContainer')}",
                        )

                conv_hidden_reports = reports.get("__conv_hidden__", {})
                for cw_key in ["1366", "1920", "2560"]:
                    with self.subTest(converted_hidden_width=cw_key):
                        self.assertIn(cw_key, conv_hidden_reports)
                        self.assertTrue(
                            conv_hidden_reports[cw_key].get("descAtMost35Pct"),
                            f"Description column exceeded 35% when Converted hidden at {cw_key}px: {conv_hidden_reports[cw_key]}",
                        )

                modal_reports = reports.get("__modals__", {})
                for mvp_name in ["360x800", "768x1024", "1366x768"]:
                    self.assertIn(mvp_name, modal_reports)
                    for mid in ["transactionModal", "deleteConfirmModal", "transferModal"]:
                        with self.subTest(modal_viewport=mvp_name, modal_id=mid):
                            mrep = modal_reports[mvp_name].get(mid, {})
                            self.assertTrue(
                                mrep.get("centeredHorizontally"),
                                f"Modal {mid} not centered horizontally at {mvp_name}: {mrep}",
                            )
                            self.assertTrue(
                                mrep.get("fitsViewport"),
                                f"Modal {mid} overflows viewport at {mvp_name}: {mrep}",
                            )
                            self.assertTrue(
                                mrep.get("backdropCoversFullViewport"),
                                f"Modal backdrop for {mid} does not cover full viewport at {mvp_name}: {mrep}",
                            )
                            self.assertTrue(
                                mrep.get("fabHiddenOnMobile"),
                                f"Floating Add transaction button should have visibility:hidden while modal {mid} is open at {mvp_name}: {mrep}",
                            )
                            self.assertTrue(
                                mrep.get("desktopActionStaysVisible"),
                                f"Topbar Add transaction button should stay visible while modal {mid} is open at {mvp_name}: {mrep}",
                            )
                            self.assertTrue(
                                mrep.get("desktopTopbarNoReflow"),
                                f"Topbar should not reflow when modal {mid} opens at {mvp_name}: {mrep}",
                            )

                # 2. Save a screenshot per viewport to screenshots_dir (and copy requested ones to artifact_dir)
                shot_profile = tempfile.mkdtemp(prefix="shot_shared_profile_")
                try:
                    for vp in viewports:
                        name = vp["name"]
                        css_w = round(vp["w"] / vp["zoom"])
                        css_h = max(round(vp["h"] / vp["zoom"]), 900)
                        shot_win_h = max(vp["h"], round(900 * vp["zoom"]))
                        page_file = "tx_coarse.html" if vp["coarse"] else "tx_fine.html"
                        shot_html_name = f"shot_{name}.html"
                        shot_html_path = os.path.join(temp_dir, shot_html_name)
                        with open(shot_html_path, "w", encoding="utf-8") as f:
                            f.write(f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;overflow:hidden;background:#f6f7f9;">
<iframe src="http://127.0.0.1:{port}/{page_file}" style="width:{css_w}px;height:{css_h}px;border:none;display:block;transform:scale({vp['zoom']});transform-origin:0 0;"></iframe>
</body>
</html>""")
                        out_png = screenshots_dir / f"transactions_{name}.png"
                        shot_cmd = [
                            browser_bin,
                            "--headless=new",
                            "--disable-gpu",
                            "--no-first-run",
                            "--no-default-browser-check",
                            "--hide-scrollbars",
                        ]
                        if vp["coarse"]:
                            shot_cmd.append("--blink-settings=primaryPointerType=2,availablePointerTypes=2")
                        shot_cmd.extend([
                            f"--window-size={vp['w']},{shot_win_h}",
                            f"--user-data-dir={shot_profile}",
                            f"--screenshot={out_png}",
                            f"http://127.0.0.1:{port}/{shot_html_name}",
                        ])
                        subprocess.run(
                            shot_cmd,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=20,
                            check=False,
                        )
                        self.assertTrue(out_png.exists(), f"Screenshot not saved for {name}")
                        if artifact_dir.exists():
                            shutil.copy2(out_png, artifact_dir / f"transactions_{name}.png")

                    # Save 360x800 screenshot with Delete confirmation modal open
                    del_html_path = os.path.join(temp_dir, "shot_360_delete_modal.html")
                    with open(del_html_path, "w", encoding="utf-8") as f:
                        f.write(f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;overflow:hidden;background:#f6f7f9;">
<iframe id="del-frame" src="http://127.0.0.1:{port}/tx_coarse.html" style="width:360px;height:800px;border:none;display:block;"></iframe>
<script>
const df = document.getElementById('del-frame');
df.onload = function() {{
    const doc = df.contentDocument;
    const mEl = doc.getElementById('deleteConfirmModal');
    if (mEl) mEl.classList.remove('fade');
    const trig = doc.querySelector('.tx-delete-trigger');
    if (trig) trig.click();
}};
</script>
</body>
</html>""")
                    del_png = screenshots_dir / "transactions_360x800_delete_modal.png"
                    subprocess.run(
                        [
                            browser_bin,
                            "--headless=new",
                            "--disable-gpu",
                            "--no-first-run",
                            "--no-default-browser-check",
                            "--hide-scrollbars",
                            "--blink-settings=primaryPointerType=2,availablePointerTypes=2",
                            "--window-size=360,800",
                            f"--user-data-dir={shot_profile}",
                            f"--screenshot={del_png}",
                            f"http://127.0.0.1:{port}/shot_360_delete_modal.html",
                        ],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=20,
                        check=False,
                    )
                    if artifact_dir.exists() and del_png.exists():
                        shutil.copy2(del_png, artifact_dir / "transactions_360x800_delete_modal.png")
                finally:
                    shutil.rmtree(shot_profile, ignore_errors=True)
            finally:
                httpd.shutdown()
                httpd.server_close()
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
            shutil.rmtree(screenshots_dir, ignore_errors=True)

    def _run_browser_harness(self, browser_bin, width, height, html_content):
        import http.server
        import json
        import shutil
        import socket
        import subprocess
        import tempfile
        import threading

        results_holder = {}
        done_event = threading.Event()

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                data = html_content.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length).decode("utf-8")
                results_holder["report"] = json.loads(raw)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "11")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')
                done_event.set()

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()

        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
        server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        server_thread.start()

        user_data_dir = tempfile.mkdtemp(prefix=f"trans_browser_{width}_{height}_")
        proc = None
        try:
            proc = subprocess.Popen(
                [
                    browser_bin,
                    "--headless=new",
                    "--disable-gpu",
                    "--no-first-run",
                    "--no-default-browser-check",
                    f"--window-size={width},{height}",
                    f"--user-data-dir={user_data_dir}",
                    f"http://127.0.0.1:{port}/",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            finished = done_event.wait(timeout=25)
            if not finished:
                return None
            return results_holder.get("report")
        finally:
            if proc:
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except Exception:
                    pass
            httpd.shutdown()
            httpd.server_close()
            shutil.rmtree(user_data_dir, ignore_errors=True)



    def test_month_navigation_and_filtering_behavior(self):
        """Comprehensive unit and integration tests for Transactions month-by-month filtering."""
        from django.utils import timezone
        from .services import create_transaction_entry, create_transfer_entry

        today = timezone.localdate()
        cur_month_str = today.strftime("%Y-%m")
        prev_month_date = date(today.year - (1 if today.month == 1 else 0), 12 if today.month == 1 else today.month - 1, 15)
        prev_month_str = prev_month_date.strftime("%Y-%m")

        # 1. Create transactions in current month and previous month
        tx_current = create_transaction_entry(
            user=self.user,
            transaction_type="expense",
            amount=Decimal("50.00"),
            wallet=self.wallet1,
            transaction_date=today,
            description="November current expense",
            scope="personal",
        )
        tx_prev = create_transaction_entry(
            user=self.user,
            transaction_type="expense",
            amount=Decimal("80.00"),
            wallet=self.wallet1,
            transaction_date=prev_month_date,
            description="October previous expense",
            scope="personal",
        )
        tr_prev = create_transfer_entry(
            user=self.user,
            source_wallet=self.wallet1,
            destination_wallet=self.wallet2,
            amount_sent=Decimal("150.00"),
            transfer_date=prev_month_date,
            notes="Previous month transfer",
        )

        # A. Missing month parameter -> defaults to current month
        resp_def = self.client.get(self.transactions_url)
        self.assertEqual(resp_def.status_code, 200)
        self.assertEqual(resp_def.context["filters"]["month"], cur_month_str)
        self.assertEqual(resp_def.context["filters"]["month_mode"], "month")
        self.assertTrue(resp_def.context["filters"]["is_default_month"])
        self.assertEqual(resp_def.context["active_filters_count"], 0)
        self.assertFalse(resp_def.context["has_active_filters"])
        def_tx_ids = [tx.id for tx in resp_def.context["transactions_list"]]
        self.assertIn(tx_current.id, def_tx_ids)
        self.assertNotIn(tx_prev.id, def_tx_ids)
        # Summary totals match current month only
        self.assertContains(resp_def, "-175.50 USD")  # self.tx1 (125.50) + tx_current (50.00)
        self.assertContains(resp_def, f"· {today.strftime('%B %Y')}")

        # Month bar controls
        html_def = resp_def.content.decode("utf-8")
        self.assertIn('id="btn-prev-month"', html_def)
        self.assertIn('id="btn-next-month"', html_def)
        self.assertIn('disabled', html_def)  # Next button disabled when current month
        self.assertIn('id="btn-all-time"', html_def)
        self.assertIn('id="native-month-input"', html_def)
        self.assertNotIn('active', html_def.split('id="btn-all-time"')[0].split('<a')[-1])

        # B. Specific month parameter -> ?month=YYYY-MM
        resp_prev = self.client.get(f"{self.transactions_url}?month={prev_month_str}")
        self.assertEqual(resp_prev.status_code, 200)
        self.assertEqual(resp_prev.context["filters"]["month"], prev_month_str)
        self.assertFalse(resp_prev.context["filters"]["is_default_month"])
        prev_tx_ids = [tx.id for tx in resp_prev.context["transactions_list"]]
        self.assertIn(tx_prev.id, prev_tx_ids)
        self.assertNotIn(tx_current.id, prev_tx_ids)
        self.assertContains(resp_prev, "-80.00 USD")
        self.assertContains(resp_prev, f"· {prev_month_date.strftime('%B %Y')}")
        # Next button is NOT disabled for previous month
        html_prev = resp_prev.content.decode("utf-8")
        next_btn_chunk = html_prev.split('id="btn-next-month"')[0].split('<')[-1]
        self.assertNotIn('disabled', next_btn_chunk)

        # Transfers section follows the same month
        transfers_prev = resp_prev.context["recent_transfers"]
        self.assertEqual(len(transfers_prev), 1)
        self.assertEqual(transfers_prev[0].id, tr_prev.id)

        # C. All time -> ?month=all
        resp_all = self.client.get(f"{self.transactions_url}?month=all")
        self.assertEqual(resp_all.status_code, 200)
        self.assertEqual(resp_all.context["filters"]["month_mode"], "all")
        self.assertEqual(resp_all.context["filters"]["month"], "all")
        all_tx_ids = [tx.id for tx in resp_all.context["transactions_list"]]
        self.assertIn(tx_current.id, all_tx_ids)
        self.assertIn(tx_prev.id, all_tx_ids)
        self.assertContains(resp_all, "· All time")
        html_all = resp_all.content.decode("utf-8")
        all_time_pill_class = html_all.split('id="btn-all-time"')[0].split('<a')[-1]
        self.assertIn('active', all_time_pill_class)

        # D. Custom date range overrides month
        resp_custom = self.client.get(f"{self.transactions_url}?month={cur_month_str}&start_date={prev_month_str}-10&end_date={prev_month_str}-20")
        self.assertEqual(resp_custom.status_code, 200)
        self.assertEqual(resp_custom.context["filters"]["month_mode"], "custom")
        self.assertContains(resp_custom, "· Custom range")
        self.assertEqual(resp_custom.context["active_filters_count"], 2)  # start_date, end_date
        custom_tx_ids = [tx.id for tx in resp_custom.context["transactions_list"]]
        self.assertIn(tx_prev.id, custom_tx_ids)
        self.assertNotIn(tx_current.id, custom_tx_ids)

        # E. Invalid month fallback to current month
        resp_invalid = self.client.get(f"{self.transactions_url}?month=invalid-date")
        self.assertEqual(resp_invalid.status_code, 200)
        self.assertEqual(resp_invalid.context["filters"]["month"], cur_month_str)

        # F. Empty month state
        future_month = f"{today.year + 2}-01"
        resp_empty = self.client.get(f"{self.transactions_url}?month={future_month}")
        self.assertEqual(resp_empty.status_code, 200)
        self.assertContains(resp_empty, f"No transactions in January {today.year + 2}")
        self.assertContains(resp_empty, 'id="btn-empty-prev-month"')
        self.assertContains(resp_empty, 'id="btn-empty-all-time"')

        # G. Dashboard "View all >" links to month=all
        resp_dash = self.client.get(reverse("budgets:dashboard"))
        self.assertEqual(resp_dash.status_code, 200)
        self.assertContains(resp_dash, 'href="/transactions/?month=all"')

        # H. Export respects month & export all time
        resp_exp_cur = self.client.get(reverse("budgets:export_transactions") + f"?format=csv&month={cur_month_str}")
        self.assertEqual(resp_exp_cur.status_code, 200)
        content_cur = resp_exp_cur.content.decode("utf-8")
        self.assertIn("November current expense", content_cur)
        self.assertNotIn("October previous expense", content_cur)

        resp_exp_all = self.client.get(reverse("budgets:export_transactions") + "?format=csv&all_time=1")
        self.assertEqual(resp_exp_all.status_code, 200)
        content_all = resp_exp_all.content.decode("utf-8")
        self.assertIn("November current expense", content_all)
        self.assertIn("October previous expense", content_all)

class BudgetsPageStationeryRedesignTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model
        from apps.accounts.models import UserPreference
        from .models import Currency, Wallet
        from decimal import Decimal
        from datetime import date

        User = get_user_model()
        self.user = User.objects.create_user(
            username="budgets_stationery_user",
            password="StrongPassword123!",
        )
        self.usd = Currency.objects.get(code="USD")
        self.jpy, _ = Currency.objects.get_or_create(
            code="JPY",
            defaults={"name": "Japanese Yen", "decimal_places": 0, "is_active": True},
        )
        UserPreference.objects.create(
            user=self.user,
            personal_base_currency=self.usd,
            display_currency=None,
        )
        self.wallet = Wallet.objects.create(
            owner=self.user,
            name="Checking",
            wallet_type=Wallet.WalletType.BANK_ACCOUNT,
            currency=self.usd,
            opening_balance=Decimal("10000.00"),
            opening_balance_date=date(2026, 1, 1),
        )
        self.client.force_login(self.user)
        self.budgets_url = reverse("budgets:budgets")

    def _find_browser_binary(self):
        import os
        candidates = [
            os.environ.get("CHROME_BIN"),
            os.environ.get("EDGE_BIN"),
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        ]
        for c in candidates:
            if c and os.path.exists(c):
                return c
        return None

    def _run_browser_harness(self, browser_bin, width, height, html_content):
        import http.server
        import json
        import shutil
        import socket
        import subprocess
        import tempfile
        import threading

        results_holder = {}
        done_event = threading.Event()

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                data = html_content.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length).decode("utf-8")
                results_holder["report"] = json.loads(raw)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "11")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')
                done_event.set()

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()

        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
        server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        server_thread.start()

        user_data_dir = tempfile.mkdtemp(prefix=f"budgets_browser_{width}_{height}_")
        proc = None
        try:
            proc = subprocess.Popen(
                [
                    browser_bin,
                    "--headless=new",
                    "--disable-gpu",
                    "--no-first-run",
                    "--no-default-browser-check",
                    f"--window-size={width},{height}",
                    f"--user-data-dir={user_data_dir}",
                    f"http://127.0.0.1:{port}/",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            finished = done_event.wait(timeout=25)
            if not finished:
                return None
            return results_holder.get("report")
        finally:
            if proc:
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except Exception:
                    pass
            httpd.shutdown()
            httpd.server_close()
            shutil.rmtree(user_data_dir, ignore_errors=True)

    def test_budgets_page_structure_and_stationery_styling(self):
        resp = self.client.get(self.budgets_url)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")

        # 1. Generic page summary subtitle removed, controls bar starts cleanly
        self.assertNotIn("Plan monthly category spending limits and monitor progress across personal and shared spaces.", html)
        self.assertNotIn("Set monthly category spending limits for a chosen period across both personal and shared spaces.", html)

        # 2. Stationery classes and styling
        self.assertIn("budgets-stationery", html)
        self.assertIn("stationery-panel", html)
        self.assertIn("stationery-summary-card", html)
        self.assertIn("budgets-table", html)

        # 3. Panel headings & sections
        self.assertIn('id="budget-create-heading"', html)
        self.assertIn("Set monthly category budget", html)
        self.assertIn('id="budget-controls-heading"', html)
        self.assertIn("Filter budget period", html)
        self.assertIn('id="budget-overview-heading"', html)
        self.assertIn('id="category-budgets-heading"', html)
        self.assertIn("Quick add category", html)

        # 4. Summary cards
        self.assertIn("Planned Budget", html)
        self.assertIn("Spent", html)
        self.assertIn("Remaining", html)

        # 5. Table columns (9 columns)
        for col_header in ["Category", "Scope", "Period", "Currency", "Limit", "Spent", "Remaining", "Status", "Actions"]:
            self.assertIn(f">{col_header}</th>", html)

        # 6. Action buttons
        self.assertIn("Save budget", html)
        self.assertIn("View period", html)
        self.assertIn("Add category", html)

    def test_preserved_filter_values(self):
        resp = self.client.get(f"{self.budgets_url}?period=2026-11&scope=personal")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["overview"]["period"], "2026-11")
        self.assertEqual(resp.context["overview"]["scope"], "personal")

        html = resp.content.decode("utf-8")
        self.assertIn('name="period"', html)
        self.assertIn('value="2026-11"', html)
        self.assertIn('value="personal" selected', html)

    def test_budget_form_validation_errors(self):
        resp = self.client.post(self.budgets_url, {
            "action": "save_budget",
            "period": "2026-11",
            "limit_amount": "-25.00",
        })
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")
        self.assertTrue("invalid-feedback" in html or "alert-danger" in html)

    def test_quick_add_category_validation_errors(self):
        resp = self.client.post(self.budgets_url, {
            "action": "create_category",
            "name": "",
            "kind": "expense",
        })
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")
        self.assertTrue("invalid-feedback" in html or "alert-danger" in html)

    def test_empty_state_and_populated_budget_rows(self):
        from .services import create_category, save_category_budget
        from django.utils import timezone
        from decimal import Decimal

        resp_empty = self.client.get(self.budgets_url)
        self.assertEqual(resp_empty.status_code, 200)
        self.assertContains(resp_empty, "No budgets created for this period.")

        cat = create_category(user=self.user, name="Groceries", kind="expense", scope="personal")
        cur_period = timezone.localdate().strftime("%Y-%m")
        b = save_category_budget(
            user=self.user,
            category=cat,
            period=cur_period,
            currency=self.usd,
            limit_amount=Decimal("350.00"),
            scope="personal",
        )

        resp_pop = self.client.get(self.budgets_url)
        self.assertEqual(resp_pop.status_code, 200)
        self.assertNotContains(resp_pop, "No budgets created for this period.")
        html_pop = resp_pop.content.decode("utf-8")

        self.assertIn("Groceries", html_pop)
        self.assertIn("Personal", html_pop)
        self.assertIn(cur_period, html_pop)
        self.assertIn("USD", html_pop)
        self.assertIn("350.00", html_pop)
        self.assertIn("0.00", html_pop)
        self.assertIn("On track", html_pop)
        self.assertIn('name="action" value="archive_budget"', html_pop)
        self.assertIn(f'name="budget_id" value="{b.id}"', html_pop)
        self.assertIn("Remove", html_pop)

        resp_arch = self.client.post(self.budgets_url, {
            "action": "archive_budget",
            "budget_id": b.id,
        }, follow=True)
        self.assertEqual(resp_arch.status_code, 200)
        self.assertContains(resp_arch, "No budgets created for this period.")

    def test_desktop_and_mobile_responsiveness(self):
        import pathlib
        from django.conf import settings
        from .services import create_category, save_category_budget
        from django.utils import timezone
        from decimal import Decimal

        browser_bin = self._find_browser_binary()
        if not browser_bin:
            self.skipTest("No browser binary found for headless verification.")

        cat = create_category(user=self.user, name="Healthcare & Medical", kind="expense", scope="personal")
        cur_period = timezone.localdate().strftime("%Y-%m")
        save_category_budget(
            user=self.user,
            category=cat,
            period=cur_period,
            currency=self.usd,
            limit_amount=Decimal("500.00"),
            scope="personal",
        )

        static_dir = pathlib.Path(settings.BASE_DIR) / "static"
        bs_js = (static_dir / "vendor" / "bootstrap" / "bootstrap.bundle.min.js").read_text(encoding="utf-8")
        bs_css = (static_dir / "vendor" / "bootstrap" / "bootstrap.min.css").read_text(encoding="utf-8")
        app_css = (static_dir / "css" / "app.css").read_text(encoding="utf-8")
        dash_css = (static_dir / "css" / "dashboard.css").read_text(encoding="utf-8")

        resp = self.client.get(self.budgets_url)
        page_html = resp.content.decode("utf-8")

        def make_harness(html):
            return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>{bs_css}</style>
    <style>{app_css}</style>
    <style>{dash_css}</style>
</head>
<body class="app-body">
    {html}
    <script>{bs_js}</script>
    <script>
    window.addEventListener('DOMContentLoaded', async function () {{
        await new Promise(r => setTimeout(r, 200));
        var results = {{
            viewportWidth: window.innerWidth,
            viewportHeight: window.innerHeight,
            hasHorizontalDocumentOverflow: document.documentElement.scrollWidth > window.innerWidth,
            hasTableResponsive: !!document.querySelector('.table-responsive'),
            hasBudgetsTable: !!document.querySelector('.budgets-table'),
            hasSummaryCards: document.querySelectorAll('.stationery-summary-card').length === 3,
            hasPanels: document.querySelectorAll('.stationery-panel').length >= 2,
        }};
        fetch('/__test_done__', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify(results)
        }}).catch(function () {{}});
    }});
    </script>
</body>
</html>"""

        # 1. Desktop Viewport (1366 x 768)
        desktop_harness = make_harness(page_html)
        rep_desktop = self._run_browser_harness(browser_bin, 1366, 768, desktop_harness)
        self.assertIsNotNone(rep_desktop, "Desktop browser verification timed out")
        self.assertGreaterEqual(rep_desktop.get("viewportWidth", 0), 1024, "Desktop viewport width should be >= 1024")
        self.assertGreaterEqual(rep_desktop.get("viewportHeight", 0), 600, "Desktop viewport height should be >= 600")
        self.assertFalse(rep_desktop.get("hasHorizontalDocumentOverflow"), "Desktop should have no horizontal overflow")
        self.assertTrue(rep_desktop.get("hasTableResponsive"), "Table responsive wrapper must exist")
        self.assertTrue(rep_desktop.get("hasBudgetsTable"), "Budgets table must exist")
        self.assertTrue(rep_desktop.get("hasSummaryCards"), "3 summary cards must exist")
        self.assertTrue(rep_desktop.get("hasPanels"), "Panels must exist")

        # 2. Mobile Viewport (360 x 800) - Chromium headless on Windows clamps Aura window min-width to <= 575px (Bootstrap mobile breakpoint)
        mobile_harness = make_harness(page_html)
        rep_mobile = self._run_browser_harness(browser_bin, 360, 800, mobile_harness)
        self.assertIsNotNone(rep_mobile, "Mobile browser verification timed out")
        self.assertLessEqual(rep_mobile.get("viewportWidth", 999), 575, "Mobile viewport width should be <= 575 (Bootstrap mobile breakpoint)")
        self.assertGreaterEqual(rep_mobile.get("viewportHeight", 0), 600, "Mobile viewport height should be >= 600")
        self.assertFalse(rep_mobile.get("hasHorizontalDocumentOverflow"), "Mobile should have no horizontal page overflow")
        self.assertTrue(rep_mobile.get("hasTableResponsive"), "Table responsive wrapper must exist on mobile")
        self.assertTrue(rep_mobile.get("hasBudgetsTable"), "Budgets table must exist on mobile")
        self.assertTrue(rep_mobile.get("hasSummaryCards"), "3 summary cards must exist on mobile")
        self.assertTrue(rep_mobile.get("hasPanels"), "Panels must exist on mobile")
class SharedSpaceStationeryRedesignTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model
        from apps.accounts.models import UserPreference
        from .models import Currency, Wallet, SharedSpace, SharedSpaceMembership
        from .services import create_shared_space, create_category, create_transaction_entry
        from decimal import Decimal
        from datetime import date

        User = get_user_model()
        self.alice = User.objects.create_user(username="alice_stationery", password="Str0ng!Password#2026")
        self.bob = User.objects.create_user(username="bob_stationery", password="Str0ng!Password#2026")
        self.carol = User.objects.create_user(username="carol_stationery", password="Str0ng!Password#2026")

        self.php = Currency.objects.get(code="PHP")
        self.usd = Currency.objects.get(code="USD")

        self.pref_alice = UserPreference.objects.create(
            user=self.alice,
            personal_base_currency=self.usd,
            active_scope="shared",
        )
        self.pref_bob = UserPreference.objects.create(
            user=self.bob,
            personal_base_currency=self.usd,
            active_scope="shared",
        )
        self.pref_carol = UserPreference.objects.create(
            user=self.carol,
            personal_base_currency=self.usd,
            active_scope="personal",
        )

        self.space = create_shared_space(user=self.alice, name="Home Household", base_currency=self.php)
        self.pref_alice.active_shared_space = self.space
        self.pref_alice.save(update_fields=["active_shared_space"])

        self.wallet = Wallet.objects.create(
            owner=self.alice,
            name="Household Cash",
            wallet_type=Wallet.WalletType.CASH,
            currency=self.php,
            ownership_scope=Wallet.OwnershipScope.SHARED,
            shared_space=self.space,
            opening_balance=Decimal("5000.00"),
            opening_balance_date=date(2026, 1, 1),
        )

        self.category = create_category(
            user=self.alice,
            name="Groceries",
            kind="expense",
            scope="shared",
            shared_space=self.space,
        )

        self.tx = create_transaction_entry(
            user=self.alice,
            wallet=self.wallet,
            transaction_type="expense",
            amount=Decimal("1250.00"),
            transaction_date=date(2026, 11, 2),
            category=self.category,
            description="Weekly fresh groceries",
            scope="shared",
            shared_space=self.space,
        )

        self.shared_url = reverse("budgets:shared_space")

    def _find_browser_binary(self):
        import os
        candidates = [
            os.environ.get("CHROME_BIN"),
            os.environ.get("EDGE_BIN"),
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        ]
        for c in candidates:
            if c and os.path.exists(c):
                return c
        return None

    def _run_browser_harness(self, browser_bin, width, height, html_content):
        import http.server
        import json
        import shutil
        import socket
        import subprocess
        import tempfile
        import threading

        results_holder = {}
        done_event = threading.Event()

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                data = html_content.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length).decode("utf-8")
                results_holder["report"] = json.loads(raw)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "11")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')
                done_event.set()

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()

        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
        server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        server_thread.start()

        user_data_dir = tempfile.mkdtemp(prefix=f"sp_browser_{width}_{height}_")
        proc = None
        try:
            proc = subprocess.Popen(
                [
                    browser_bin,
                    "--headless=new",
                    "--disable-gpu",
                    "--no-first-run",
                    "--no-default-browser-check",
                    f"--window-size={width},{height}",
                    f"--user-data-dir={user_data_dir}",
                    f"http://127.0.0.1:{port}/",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            finished = done_event.wait(timeout=25)
            if not finished:
                return None
            return results_holder.get("report")
        finally:
            if proc:
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except Exception:
                    pass
            httpd.shutdown()
            httpd.server_close()
            shutil.rmtree(user_data_dir, ignore_errors=True)

    def test_shared_space_page_structure_and_stationery_theme(self):
        self.client.force_login(self.alice)
        resp = self.client.get(self.shared_url)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")

        # Visual theme classes
        self.assertIn("shared-space-stationery", html)
        self.assertIn("stationery-panel", html)
        self.assertIn("stationery-summary-card", html)
        self.assertIn("sp-table", html)

        # Headings
        self.assertIn("Shared Space", html)
        self.assertIn("Home Household", html)
        self.assertIn("Shared budget progress", html)
        self.assertIn("Members", html)
        self.assertIn("Partner invitations", html)
        self.assertIn("Shared spending &amp; income", html)
        self.assertIn("Shared personal activity", html)

        # Summary cards
        self.assertIn("Members", html)
        self.assertIn("Shared Wallets", html)
        self.assertIn("This Month's Spending", html)
        self.assertIn("Base Currency", html)
        self.assertIn("PHP", html)

        # Actions
        self.assertIn("Add shared transaction", html)

        # Header hierarchy & standardized summary cards
        self.assertNotIn('<h1 class="sp-page-title', html)
        self.assertNotIn("A shared space lets two people track joint budgets", html)
        self.assertIn("summary-card-icon-box", html)
        self.assertIn("summary-card-dot", html)

    def test_authorized_member_visibility(self):
        from .services import create_shared_space_invitation, accept_shared_space_invitation

        # Alice invites Bob; Bob accepts
        inv = create_shared_space_invitation(user=self.alice, shared_space=self.space, partner_hint="bob_stationery")
        accept_shared_space_invitation(user=self.bob, code=inv.code)
        self.pref_bob.refresh_from_db()
        self.pref_bob.active_shared_space = self.space
        self.pref_bob.save(update_fields=["active_shared_space"])

        # Alice (creator) views page
        self.client.force_login(self.alice)
        resp_a = self.client.get(self.shared_url)
        self.assertEqual(resp_a.status_code, 200)
        html_a = resp_a.content.decode("utf-8")
        self.assertIn("alice_stationery", html_a)
        self.assertIn("bob_stationery", html_a)
        self.assertIn('name="action" value="update_space"', html_a)
        self.assertIn("Update shared space", html_a)
        self.assertNotIn("Leave space", html_a)

        # Bob (member) views page
        self.client.force_login(self.bob)
        resp_b = self.client.get(self.shared_url)
        self.assertEqual(resp_b.status_code, 200)
        html_b = resp_b.content.decode("utf-8")
        self.assertIn("alice_stationery", html_b)
        self.assertIn("bob_stationery", html_b)
        self.assertNotIn('name="action" value="update_space"', html_b)
        self.assertIn('name="action" value="leave_space"', html_b)
        self.assertIn("Leave space", html_b)

    def test_nonmember_and_inactive_isolation(self):
        from .services import create_shared_space_invitation, accept_shared_space_invitation, leave_shared_space

        # Carol (not in any shared space)
        self.client.force_login(self.carol)
        resp_c = self.client.get(self.shared_url)
        self.assertEqual(resp_c.status_code, 200)
        html_c = resp_c.content.decode("utf-8")
        self.assertIn("Create a shared space", html_c)
        self.assertIn("Accept partner invitation", html_c)
        self.assertNotIn("Home Household", html_c)
        self.assertNotIn("Household Cash", html_c)
        self.assertNotIn("Weekly fresh groceries", html_c)

        # Bob joins then leaves
        inv = create_shared_space_invitation(user=self.alice, shared_space=self.space)
        accept_shared_space_invitation(user=self.bob, code=inv.code)
        leave_shared_space(user=self.bob, shared_space=self.space)

        # Alice views members: Bob is shown as former
        self.client.force_login(self.alice)
        resp_a = self.client.get(self.shared_url)
        self.assertEqual(resp_a.status_code, 200)
        html_a = resp_a.content.decode("utf-8")
        self.assertIn("bob_stationery", html_a)
        self.assertIn("Former", html_a)

    def test_invitation_actions_and_limits(self):
        from .services import accept_shared_space_invitation
        from .models import SharedSpaceInvitation

        self.client.force_login(self.alice)

        # 1. Generate invitation
        resp_inv = self.client.post(self.shared_url, {
            "action": "create_invitation",
            "partner_hint": "Roommate Bob",
            "expires_in_days": 7,
        }, follow=True)
        self.assertEqual(resp_inv.status_code, 200)
        inv = SharedSpaceInvitation.objects.filter(shared_space=self.space, status="pending").first()
        self.assertIsNotNone(inv)
        self.assertContains(resp_inv, inv.code)
        self.assertContains(resp_inv, "Revoke")

        # 2. Revoke invitation
        resp_rev = self.client.post(self.shared_url, {
            "action": "revoke_invitation",
            "invitation_id": inv.id,
        }, follow=True)
        self.assertEqual(resp_rev.status_code, 200)
        inv.refresh_from_db()
        self.assertEqual(inv.status, SharedSpaceInvitation.Status.REVOKED)

        # 3. Create fresh invite and Bob joins -> 2-member limit notice shown
        resp_inv2 = self.client.post(self.shared_url, {
            "action": "create_invitation",
            "partner_hint": "Roommate Bob",
            "expires_in_days": 7,
        }, follow=True)
        inv2 = SharedSpaceInvitation.objects.filter(shared_space=self.space, status="pending").first()
        accept_shared_space_invitation(user=self.bob, code=inv2.code)

        resp_limit = self.client.get(self.shared_url)
        self.assertEqual(resp_limit.status_code, 200)
        self.assertContains(resp_limit, "This shared space has reached the two-member maximum.")

    def test_desktop_and_mobile_responsiveness(self):
        import pathlib
        from django.conf import settings

        browser_bin = self._find_browser_binary()
        if not browser_bin:
            self.skipTest("No browser binary found for headless verification.")

        static_dir = pathlib.Path(settings.BASE_DIR) / "static"
        bs_js = (static_dir / "vendor" / "bootstrap" / "bootstrap.bundle.min.js").read_text(encoding="utf-8")
        bs_css = (static_dir / "vendor" / "bootstrap" / "bootstrap.min.css").read_text(encoding="utf-8")
        app_css = (static_dir / "css" / "app.css").read_text(encoding="utf-8")
        dash_css = (static_dir / "css" / "dashboard.css").read_text(encoding="utf-8")

        self.client.force_login(self.alice)
        resp = self.client.get(self.shared_url)
        page_html = resp.content.decode("utf-8")

        def make_harness(html):
            return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>{bs_css}</style>
    <style>{app_css}</style>
    <style>{dash_css}</style>
</head>
<body class="app-body">
    {html}
    <script>{bs_js}</script>
    <script>
    window.addEventListener('DOMContentLoaded', async function () {{
        await new Promise(r => setTimeout(r, 200));
        var results = {{
            viewportWidth: window.innerWidth,
            viewportHeight: window.innerHeight,
            hasHorizontalDocumentOverflow: document.documentElement.scrollWidth > window.innerWidth,
            hasTableResponsive: document.querySelectorAll('.table-responsive').length >= 2,
            hasSummaryCards: document.querySelectorAll('.stationery-summary-card').length === 4,
            hasPanels: document.querySelectorAll('.stationery-panel').length >= 3,
        }};
        fetch('/__test_done__', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify(results)
        }}).catch(function () {{}});
    }});
    </script>
</body>
</html>"""

        # 1. Desktop Viewport (1366 x 768)
        desktop_harness = make_harness(page_html)
        rep_desktop = self._run_browser_harness(browser_bin, 1366, 768, desktop_harness)
        self.assertIsNotNone(rep_desktop, "Desktop browser verification timed out")
        self.assertGreaterEqual(rep_desktop.get("viewportWidth", 0), 1024, "Desktop viewport width should be >= 1024")
        self.assertGreaterEqual(rep_desktop.get("viewportHeight", 0), 600, "Desktop viewport height should be >= 600")
        self.assertFalse(rep_desktop.get("hasHorizontalDocumentOverflow"), "Desktop should have no horizontal overflow")
        self.assertTrue(rep_desktop.get("hasTableResponsive"), "Table responsive wrapper must exist")
        self.assertTrue(rep_desktop.get("hasSummaryCards"), "4 summary cards must exist")
        self.assertTrue(rep_desktop.get("hasPanels"), "Panels must exist")

        # 2. Mobile Viewport (360 x 800)
        mobile_harness = make_harness(page_html)
        rep_mobile = self._run_browser_harness(browser_bin, 360, 800, mobile_harness)
        self.assertIsNotNone(rep_mobile, "Mobile browser verification timed out")
        self.assertLessEqual(rep_mobile.get("viewportWidth", 999), 575, "Mobile viewport width should be <= 575 (Bootstrap mobile breakpoint)")
        self.assertGreaterEqual(rep_mobile.get("viewportHeight", 0), 600, "Mobile viewport height should be >= 600")
        self.assertFalse(rep_mobile.get("hasHorizontalDocumentOverflow"), "Mobile should have no horizontal page overflow")
        self.assertTrue(rep_mobile.get("hasTableResponsive"), "Table responsive wrapper must exist on mobile")
        self.assertTrue(rep_mobile.get("hasSummaryCards"), "4 summary cards must exist on mobile")
        self.assertTrue(rep_mobile.get("hasPanels"), "Panels must exist on mobile")


class SiteWideStationeryDesignSystemTests(TestCase):
    """
    Validates site-wide design system consistency across all user-facing views:
    - Warm off-white background (#F8F7F4)
    - Crisp white cards/panels with subtle cool-gray borders and soft shadows
    - Deep navy (#1E3A5F) for primary actions, headings, and emphasis
    - Muted semantic colors (green, amber, red)
    - Preserved forms, fields, CSRF tokens, and security rules
    """

    def setUp(self):
        self.user = User.objects.create_user(
            username="designuser",
            email="design@example.com",
            password="TestPassword123!",
        )
        self.currency_jpy, _ = Currency.objects.get_or_create(
            code="JPY",
            defaults={"name": "Japanese Yen", "decimal_places": 0, "is_active": True},
        )
        self.currency_usd, _ = Currency.objects.get_or_create(
            code="USD",
            defaults={"name": "US Dollar", "decimal_places": 2, "is_active": True},
        )
        self.wallet = Wallet.objects.create(
            owner=self.user,
            name="Main Cash",
            wallet_type="cash",
            currency=self.currency_jpy,
            opening_balance=Decimal("10000"),
            opening_balance_date=date(2026, 11, 1),
            ownership_scope="personal",
        )
        self.category = Category.objects.create(
            owner=self.user,
            name="Groceries",
            kind="expense",
        )
        self.tx = Transaction.objects.create(
            owner=self.user,
            paid_by=self.user,
            wallet=self.wallet,
            category=self.category,
            transaction_type="expense",
            amount=Decimal("1500"),
            currency=self.currency_jpy,
            transaction_date=date(2026, 11, 2),
            scope="personal",
            description="Supermarket groceries",
        )

    def test_global_css_design_tokens_present(self):
        """Verify static/css/app.css contains all required design tokens and components."""
        import pathlib
        from django.conf import settings
        app_css_path = pathlib.Path(settings.BASE_DIR) / "static" / "css" / "app.css"
        self.assertTrue(app_css_path.exists(), "app.css must exist")
        css_text = app_css_path.read_text(encoding="utf-8")

        self.assertIn("--stationery-bg: #F8F7F4", css_text)
        self.assertIn("--stationery-navy: #1E3A5F", css_text)
        self.assertIn("--stationery-border: #E5E3DE", css_text)
        self.assertIn(".stationery-panel", css_text)
        self.assertIn(".summary-card", css_text)
        self.assertIn(".btn-primary", css_text)
        self.assertIn(".status-pill", css_text)
        self.assertIn(".table-stationery", css_text)

    def test_dashboard_uses_stationery_design(self):
        """Dashboard renders with status 200 and stationery classes."""
        self.client.force_login(self.user)
        resp = self.client.get(reverse("budgets:dashboard"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "dashboard-stationery")
        self.assertContains(resp, "summary-strip-card")
        self.assertContains(resp, "stationery-panel")

    def test_transactions_page_uses_stationery_design(self):
        """Transactions page renders with status 200 and toolbar stationery design."""
        self.client.force_login(self.user)
        resp = self.client.get(reverse("budgets:transactions"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "transactions-stationery")
        self.assertContains(resp, "stationery-panel")
        self.assertContains(resp, "transactions-table")

    def test_budgets_page_uses_stationery_design(self):
        """Budgets page renders with status 200 and stationery design system."""
        self.client.force_login(self.user)
        resp = self.client.get(reverse("budgets:budgets"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "budgets-stationery")
        self.assertContains(resp, "stationery-panel")
        self.assertContains(resp, "stationery-summary-card")

    def test_shared_space_page_uses_stationery_design(self):
        """Shared space page renders with status 200 and stationery design."""
        self.client.force_login(self.user)
        resp = self.client.get(reverse("budgets:shared_space"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "shared-space-stationery")
        self.assertContains(resp, "stationery-panel")

    def test_wallets_page_uses_stationery_design(self):
        """Wallets page renders with status 200 and stationery styling."""
        self.client.force_login(self.user)
        resp = self.client.get(reverse("budgets:wallets"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "dashboard-panel")
        self.assertContains(resp, "Your wallets")
        self.assertContains(resp, "Main Cash")

    def test_savings_goals_page_uses_stationery_design(self):
        """Savings goals page renders with status 200 and stationery styling."""
        self.client.force_login(self.user)
        resp = self.client.get(reverse("budgets:savings_goals"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "dashboard-panel")
        self.assertContains(resp, "Create savings goal")
        self.assertContains(resp, "Your savings goals")

    def test_reports_page_uses_stationery_design(self):
        """Reports page renders with status 200 and summary cards."""
        self.client.force_login(self.user)
        resp = self.client.get(reverse("budgets:reports"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "dashboard-panel")
        self.assertContains(resp, "summary-card")
        self.assertContains(resp, "Report controls")

    def test_currencies_page_uses_stationery_design(self):
        """Currencies page renders with status 200 and stationery panels."""
        self.client.force_login(self.user)
        resp = self.client.get(reverse("budgets:currencies"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "dashboard-panel")
        self.assertContains(resp, "Currency preferences")
        self.assertContains(resp, "Currency converter")

    def test_settings_page_uses_stationery_design(self):
        """Settings page renders with status 200 and stationery panels."""
        self.client.force_login(self.user)
        resp = self.client.get(reverse("accounts:settings"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "dashboard-panel")
        self.assertContains(resp, "Profile")
        self.assertContains(resp, "Preferences")

    def test_offline_page_uses_stationery_design(self):
        """Offline page renders with status 200 and stationery layout."""
        resp = self.client.get(reverse("budgets:offline"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Offline Workspace")
        self.assertContains(resp, "dashboard-panel")

    def test_auth_pages_use_stationery_design(self):
        """Login and register pages render with status 200 and Jubao auth styling."""
        resp_login = self.client.get(reverse("accounts:login"))
        self.assertEqual(resp_login.status_code, 200)
        self.assertContains(resp_login, "jubao_auth_art.webp")
        self.assertContains(resp_login, "auth-card-wrapper")

        resp_register = self.client.get(reverse("accounts:register"))
        self.assertEqual(resp_register.status_code, 200)
        self.assertContains(resp_register, "jubao_auth_art.webp")
        self.assertContains(resp_register, "auth-card-wrapper")

    def test_generic_page_summaries_removed_across_pages(self):
        """Generic descriptive subtitles are absent while meaningful help/form text remains."""
        self.client.force_login(self.user)

        # Budgets
        resp_b = self.client.get(reverse("budgets:budgets"))
        html_b = resp_b.content.decode("utf-8")
        self.assertNotIn("Plan monthly category spending limits", html_b)
        self.assertContains(resp_b, "Set monthly category budget")

        # Shared Space
        resp_sp = self.client.get(reverse("budgets:shared_space"))
        html_sp = resp_sp.content.decode("utf-8")
        self.assertNotIn("Manage your household's shared wallets, budgets, members, and activity.", html_sp)
        self.assertNotIn("A shared space lets two people track joint budgets", html_sp)
        self.assertNotIn('<h1 class="sp-page-title', html_sp)
        self.assertContains(resp_sp, "Create a shared space")

        # Wallets
        resp_w = self.client.get(reverse("budgets:wallets"))
        html_w = resp_w.content.decode("utf-8")
        self.assertNotIn("Wallets track your manually entered balances", html_w)
        self.assertContains(resp_w, "Each wallet uses its own currency so balances remain accurate")

        # Savings Goals
        resp_g = self.client.get(reverse("budgets:savings_goals"))
        html_g = resp_g.content.decode("utf-8")
        self.assertNotIn("Savings goals track earmarked funds toward a target amount", html_g)
        self.assertContains(resp_g, "Create savings goal")

        # Reports
        resp_r = self.client.get(reverse("budgets:reports"))
        html_r = resp_r.content.decode("utf-8")
        self.assertNotIn("Reports summarize your manually recorded income", html_r)
        self.assertContains(resp_r, "Report controls")

        # Currencies
        resp_c = self.client.get(reverse("budgets:currencies"))
        html_c = resp_c.content.decode("utf-8")
        self.assertNotIn("Selecting a personal base currency records your preferred reference currency", html_c)
        self.assertContains(resp_c, "Currency preferences")

        # Settings
        resp_s = self.client.get(reverse("accounts:settings"))
        html_s = resp_s.content.decode("utf-8")
        self.assertNotIn("Manage your profile display name, time zone", html_s)
        self.assertContains(resp_s, "Profile")

    def test_typography_system_rules(self):
        """Verify global typography CSS rules enforce sans-serif font stack and tabular numerals."""
        import pathlib
        from django.conf import settings
        app_css_path = pathlib.Path(settings.BASE_DIR) / "static" / "css" / "app.css"
        css_text = app_css_path.read_text(encoding="utf-8")
        self.assertIn("--app-font-sans:", css_text)
        self.assertIn("font-variant-numeric: tabular-nums", css_text)
        self.assertIn("font-feature-settings: \"tnum\" 1", css_text)

    def test_standardized_summary_cards_across_views(self):
        """All pages with summary/metric cards follow the 4-part structure: label, value, footer with dot, and icon-box."""
        self.client.force_login(self.user)

        # 1. Budgets
        resp_b = self.client.get(reverse("budgets:budgets"))
        self.assertEqual(resp_b.status_code, 200)
        self.assertContains(resp_b, "stationery-summary-card")
        self.assertContains(resp_b, "summary-card-label")
        self.assertContains(resp_b, "summary-card-icon-box")
        self.assertContains(resp_b, "summary-card-footer")
        self.assertContains(resp_b, "summary-card-dot")

        # 2. Reports
        resp_r = self.client.get(reverse("budgets:reports"))
        self.assertEqual(resp_r.status_code, 200)
        self.assertContains(resp_r, "stationery-summary-card")
        self.assertContains(resp_r, "summary-card-label")
        self.assertContains(resp_r, "summary-card-icon-box")
        self.assertContains(resp_r, "summary-card-footer")
        self.assertContains(resp_r, "summary-card-dot")

        # 3. Transactions
        resp_t = self.client.get(reverse("budgets:transactions"))
        self.assertEqual(resp_t.status_code, 200)
        self.assertContains(resp_t, "tx-summary-card")
        self.assertContains(resp_t, "summary-card-label")
        self.assertContains(resp_t, "summary-card-icon-box")
        self.assertContains(resp_t, "summary-card-footer")
        self.assertContains(resp_t, "summary-card-dot")

        # 4. Dashboard
        resp_d = self.client.get(reverse("budgets:dashboard"))
        self.assertEqual(resp_d.status_code, 200)
        self.assertContains(resp_d, "summary-strip-card")
        self.assertContains(resp_d, "summary-card-label")
        self.assertContains(resp_d, "summary-card-icon-box")
        self.assertContains(resp_d, "summary-card-footer")
        self.assertContains(resp_d, "summary-card-dot")


class PageConsolidationAndNavigationHubTests(TestCase):
    def setUp(self):
        from .models import SharedSpace, SharedSpaceMembership

        self.password = "Str0ng!Password#2026"
        self.user = User.objects.create_user(username="hub_tester", password=self.password)
        self.partner = User.objects.create_user(username="hub_partner", password=self.password)
        self.stranger = User.objects.create_user(username="hub_stranger", password=self.password)
        self.client.force_login(self.user)

        self.usd, _ = Currency.objects.get_or_create(
            code="USD",
            defaults={"name": "US Dollar", "decimal_places": 2, "is_active": True},
        )
        self.php, _ = Currency.objects.get_or_create(
            code="PHP",
            defaults={"name": "Philippine Peso", "decimal_places": 2, "is_active": True},
        )

        # Create space for user and partner
        self.space = SharedSpace.objects.create(
            name="Household Treasury",
            base_currency=self.usd,
            created_by=self.user,
        )
        SharedSpaceMembership.objects.create(
            shared_space=self.space,
            user=self.user,
            role=SharedSpaceMembership.Role.CREATOR,
            is_active=True,
        )
        SharedSpaceMembership.objects.create(
            shared_space=self.space,
            user=self.partner,
            role=SharedSpaceMembership.Role.MEMBER,
            is_active=True,
        )

        # Stranger's private space
        self.stranger_space = SharedSpace.objects.create(
            name="Secret Fortress",
            base_currency=self.usd,
            created_by=self.stranger,
        )
        SharedSpaceMembership.objects.create(
            shared_space=self.stranger_space,
            user=self.stranger,
            role=SharedSpaceMembership.Role.CREATOR,
            is_active=True,
        )

    def test_sidebar_consolidation(self):
        """Verify sidebar no longer shows Shared Activity, Reports, or Currencies as primary pages."""
        resp = self.client.get(reverse("budgets:dashboard"))
        html = resp.content.decode("utf-8")

        sidebar_start = html.find('id="appSidebar"')
        sidebar_end = html.find('</aside>', sidebar_start)
        sidebar_html = html[sidebar_start:sidebar_end] if sidebar_start != -1 and sidebar_end != -1 else html

        # Removed primary sidebar items
        self.assertNotIn('href="/shared-activity/"', sidebar_html)
        self.assertNotIn('href="/reports/"', sidebar_html)
        self.assertNotIn('href="/currencies/"', sidebar_html)

        # Retained primary sidebar items
        self.assertIn(reverse("budgets:dashboard"), sidebar_html)
        self.assertIn(reverse("budgets:transactions"), sidebar_html)
        self.assertIn(reverse("budgets:budgets"), sidebar_html)
        self.assertIn(reverse("budgets:shared_space"), sidebar_html)
        self.assertIn(reverse("budgets:wallets"), sidebar_html)
        self.assertIn(reverse("budgets:savings_goals"), sidebar_html)
        self.assertIn(reverse("accounts:settings"), sidebar_html)

        # Dashboard sidebar item is active when viewing /reports/
        resp_reports = self.client.get(reverse("budgets:reports"))
        html_reports = resp_reports.content.decode("utf-8")
        self.assertIn('title="Dashboard"', html_reports)
        self.assertIn('href="/"\n                        class="nav-link sidebar-nav-link active"', html_reports.replace("\r\n", "\n"))

        # Shared Space sidebar item is active when viewing /shared-activity/ and /sharing-privacy/
        resp_sa = self.client.get(reverse("budgets:shared_activity"))
        self.assertContains(resp_sa, 'title="Shared Space"')
        self.assertIn('href="/shared-space/"\n                        class="nav-link sidebar-nav-link active"', resp_sa.content.decode("utf-8").replace("\r\n", "\n"))

        resp_sp = self.client.get(reverse("budgets:sharing_privacy"))
        self.assertContains(resp_sp, 'title="Shared Space"')
        self.assertIn('href="/shared-space/"\n                        class="nav-link sidebar-nav-link active"', resp_sp.content.decode("utf-8").replace("\r\n", "\n"))

        # Settings sidebar item is active when viewing /currencies/
        resp_curr = self.client.get(reverse("budgets:currencies"))
        self.assertContains(resp_curr, 'title="Settings"')
        self.assertIn('href="/accounts/settings/"\n                        class="nav-link sidebar-nav-link active"', resp_curr.content.decode("utf-8").replace("\r\n", "\n"))

    def test_shared_space_hub_tabs_and_authorization(self):
        """Verify Shared Space hub tabs, selected-space preservation, and membership isolation."""
        # 1. Overview tab
        resp_overview = self.client.get(reverse("budgets:shared_space") + f"?space={self.space.id}")
        self.assertEqual(resp_overview.status_code, 200)
        self.assertContains(resp_overview, "Household Treasury")
        self.assertContains(resp_overview, "id=\"tab-shared-overview\"")
        self.assertContains(resp_overview, "id=\"tab-shared-activity\"")
        self.assertContains(resp_overview, "id=\"tab-shared-privacy\"")
        self.assertContains(resp_overview, "nav-link settings-nav-link active")

        # 2. Activity tab via legacy URL
        resp_act = self.client.get(reverse("budgets:shared_activity") + f"?space={self.space.id}")
        self.assertEqual(resp_act.status_code, 200)
        self.assertContains(resp_act, "Household Treasury")
        self.assertContains(resp_act, "id=\"tab-shared-activity\"")
        self.assertContains(resp_act, "id=\"sharedSpaceTabs\"")

        # 3. Privacy tab via legacy URL
        resp_priv = self.client.get(reverse("budgets:sharing_privacy") + f"?space={self.space.id}")
        self.assertEqual(resp_priv.status_code, 200)
        self.assertContains(resp_priv, "Household Treasury")
        self.assertContains(resp_priv, "id=\"tab-shared-privacy\"")
        self.assertContains(resp_priv, "id=\"sharedSpaceTabs\"")

        # 4. Tab query param delegation on /shared-space/
        resp_tab_act = self.client.get(reverse("budgets:shared_space") + f"?tab=activity&space={self.space.id}")
        self.assertEqual(resp_tab_act.status_code, 200)
        self.assertContains(resp_tab_act, "id=\"tab-shared-activity\"")

        resp_tab_priv = self.client.get(reverse("budgets:shared_space") + f"?tab=privacy&space={self.space.id}")
        self.assertEqual(resp_tab_priv.status_code, 200)
        self.assertContains(resp_tab_priv, "id=\"tab-shared-privacy\"")

        # 5. Non-member cannot manipulate or view stranger's private space
        resp_stranger = self.client.get(reverse("budgets:shared_space") + f"?space={self.stranger_space.id}")
        self.assertEqual(resp_stranger.status_code, 200)
        self.assertNotContains(resp_stranger, "Secret Fortress")

    def test_settings_currency_tab_and_legacy_compatibility(self):
        """Verify Settings Currency tab behavior, forms, validation, and /currencies/ compatibility."""
        # 1. Settings page has Currency tab
        resp_settings = self.client.get(reverse("accounts:settings"))
        self.assertEqual(resp_settings.status_code, 200)
        self.assertContains(resp_settings, "id=\"currency-tab\"")
        self.assertContains(resp_settings, "id=\"tab-currency\"")
        self.assertContains(resp_settings, "Currency preferences")
        self.assertContains(resp_settings, "Currency converter")
        self.assertContains(resp_settings, "Supported currencies")
        self.assertContains(resp_settings, "Exchange-rate provider")

        # 2. Direct /currencies/ renders Settings with Currency tab active
        resp_curr = self.client.get(reverse("budgets:currencies"))
        self.assertEqual(resp_curr.status_code, 200)
        self.assertContains(resp_curr, "id=\"tab-currency\"")
        self.assertContains(resp_curr, "Currency preferences")
        self.assertContains(resp_curr, "Currency converter")

        # 3. Currency POST saves base currency and redirects
        save_resp = self.client.post(
            reverse("budgets:currencies"),
            {"action": "save_base_currency", "personal_base_currency": self.php.pk},
            follow=True,
        )
        self.assertEqual(save_resp.status_code, 200)
        pref = UserPreference.objects.get(user=self.user)
        self.assertEqual(pref.personal_base_currency, self.php)

    def test_dashboard_hub_reports_tab_and_legacy_compatibility(self):
        """Verify Dashboard Hub Overview and Reports tabs, exports, and query compatibility."""
        # 1. Dashboard Overview
        resp_dash = self.client.get(reverse("budgets:dashboard"))
        self.assertEqual(resp_dash.status_code, 200)
        self.assertContains(resp_dash, "id=\"dashboardHubTabs\"")
        self.assertContains(resp_dash, "id=\"tab-dashboard-overview\"")
        self.assertContains(resp_dash, "id=\"tab-dashboard-reports\"")

        # 2. Reports direct URL
        resp_rep = self.client.get(reverse("budgets:reports") + "?start_date=2026-10-01&end_date=2026-10-31")
        self.assertEqual(resp_rep.status_code, 200)
        self.assertContains(resp_rep, "id=\"dashboardHubTabs\"")
        self.assertContains(resp_rep, "Report controls")
        self.assertContains(resp_rep, "value=\"2026-10-01\"")
        self.assertContains(resp_rep, "value=\"2026-10-31\"")
        self.assertContains(resp_rep, "format=csv")
        self.assertContains(resp_rep, "format=json")

        # 3. Delegation via ?tab=reports on /
        resp_tab_rep = self.client.get(reverse("budgets:dashboard") + "?tab=reports")
        self.assertEqual(resp_tab_rep.status_code, 200)
        self.assertContains(resp_tab_rep, "Report controls")


import urllib.parse
from datetime import timedelta
from .models import SharedSpaceInvitation, SharedSpaceMembership
from .services import (
    accept_shared_space_invitation,
    create_shared_space,
    create_shared_space_invitation,
    revoke_shared_space_invitation,
)


class SharedSpaceInvitationV2Tests(TestCase):
    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.usd, _ = Currency.objects.get_or_create(code="USD", defaults={"name": "US Dollar", "decimal_places": 2})
        self.owner = User.objects.create_user(username="owner_user", password="Password123!")
        self.joiner = User.objects.create_user(username="joiner_user", password="Password123!")
        self.third_user = User.objects.create_user(username="third_user", password="Password123!")

        self.space = create_shared_space(user=self.owner, name="Family Treasury", base_currency=self.usd)
        self.client_owner = Client()
        self.client_owner.force_login(self.owner)

        self.client_joiner = Client()
        self.client_joiner.force_login(self.joiner)

    def tearDown(self):
        from django.core.cache import cache
        cache.clear()

    def test_code_generation_alphabet_length_uniqueness(self):
        """Generated codes are 6 characters, from INVITATION_ALPHABET (no 0, O, 1, I, L, U), and unique."""
        from .services import INVITATION_ALPHABET, generate_invitation_code

        disallowed = set("0O1ILU")
        codes = set()
        for _ in range(50):
            code = generate_invitation_code()
            self.assertEqual(len(code), 6)
            for ch in code:
                self.assertIn(ch, INVITATION_ALPHABET)
                self.assertNotIn(ch, disallowed)
            codes.add(code)
        self.assertEqual(len(codes), 50)

    def test_typed_redemption_with_lowercase_spaces_hyphens(self):
        """Redeeming a code formatted with lowercase, spaces, or hyphens successfully normalizes and joins."""
        inv = create_shared_space_invitation(user=self.owner, shared_space=self.space)
        raw = inv.code  # e.g. ABC234
        formatted_inputs = [
            raw.lower(),
            f"{raw[:3]}-{raw[3:]}".lower(),
            f" {raw[:2]} - {raw[2:4]} - {raw[4:]} ",
            f"{raw[:3]}   {raw[3:]}".lower(),
        ]
        for idx, typed in enumerate(formatted_inputs):
            # Clean up active membership for joiner between tests
            SharedSpaceMembership.objects.filter(shared_space=self.space, user=self.joiner).delete()
            inv.status = SharedSpaceInvitation.Status.PENDING
            inv.accepted_by = None
            inv.save(update_fields=["status", "accepted_by"])

            resp = self.client_joiner.post(
                reverse("budgets:shared_space"),
                {"action": "accept_invitation", "code": typed},
                follow=True,
            )
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(
                SharedSpaceMembership.objects.filter(shared_space=self.space, user=self.joiner, is_active=True).exists(),
                f"Failed to join with typed input: {typed!r}",
            )

    def test_link_redemption_requires_login_and_preserves_code(self):
        """GET on join link requires login and preserves the code parameter through next."""
        inv = create_shared_space_invitation(user=self.owner, shared_space=self.space)
        anon_client = Client()
        join_url = f"{reverse('budgets:shared_space')}?code={inv.code}"

        resp = anon_client.get(join_url)
        self.assertEqual(resp.status_code, 302)
        login_url = reverse("accounts:login")
        self.assertTrue(resp.url.startswith(login_url))
        self.assertIn(f"code={inv.code}", urllib.parse.unquote(resp.url))

        # After logging in, visiting the URL shows the confirmation prompt
        resp_confirm = self.client_joiner.get(join_url)
        self.assertEqual(resp_confirm.status_code, 200)
        self.assertContains(resp_confirm, f"Join {self.owner.username}'s shared space?")
        self.assertContains(resp_confirm, "Family Treasury")
        self.assertContains(resp_confirm, inv.formatted_code)

    def test_get_never_joins_post_joins(self):
        """GET request on join URL never mutates data; only POST with CSRF joins."""
        inv = create_shared_space_invitation(user=self.owner, shared_space=self.space)
        join_url = f"{reverse('budgets:shared_space')}?code={inv.code}"

        # GET request
        resp_get = self.client_joiner.get(join_url)
        self.assertEqual(resp_get.status_code, 200)
        inv.refresh_from_db()
        self.assertEqual(inv.status, SharedSpaceInvitation.Status.PENDING)
        self.assertFalse(
            SharedSpaceMembership.objects.filter(shared_space=self.space, user=self.joiner).exists()
        )

        # POST request
        resp_post = self.client_joiner.post(
            reverse("budgets:shared_space"),
            {"action": "accept_invitation", "code": inv.code},
            follow=True,
        )
        self.assertEqual(resp_post.status_code, 200)
        inv.refresh_from_db()
        self.assertEqual(inv.status, SharedSpaceInvitation.Status.ACCEPTED)
        self.assertEqual(inv.accepted_by, self.joiner)
        self.assertTrue(
            SharedSpaceMembership.objects.filter(shared_space=self.space, user=self.joiner, is_active=True).exists()
        )

    def test_expired_used_revoked_invalid_give_same_message(self):
        """Invalid, expired, used, and revoked codes all give the exact same generic error message."""
        from .services import INVITATION_ERROR_MESSAGE

        # 1. Invalid code
        resp_inv = self.client_joiner.post(
            reverse("budgets:shared_space"),
            {"action": "accept_invitation", "code": "ZZZZZZ"},
            follow=True,
        )
        self.assertContains(resp_inv, INVITATION_ERROR_MESSAGE)

        # 2. Expired code
        inv_exp = create_shared_space_invitation(user=self.owner, shared_space=self.space)
        inv_exp.expires_at = timezone.now() - timedelta(minutes=5)
        inv_exp.save(update_fields=["expires_at"])
        resp_exp = self.client_joiner.post(
            reverse("budgets:shared_space"),
            {"action": "accept_invitation", "code": inv_exp.code},
            follow=True,
        )
        self.assertContains(resp_exp, INVITATION_ERROR_MESSAGE)

        # 3. Revoked code
        inv_rev = create_shared_space_invitation(user=self.owner, shared_space=self.space)
        revoke_shared_space_invitation(user=self.owner, invitation=inv_rev)
        resp_rev = self.client_joiner.post(
            reverse("budgets:shared_space"),
            {"action": "accept_invitation", "code": inv_rev.code},
            follow=True,
        )
        self.assertContains(resp_rev, INVITATION_ERROR_MESSAGE)

        # 4. Used code
        inv_used = create_shared_space_invitation(user=self.owner, shared_space=self.space)
        accept_shared_space_invitation(user=self.joiner, code=inv_used.code)
        resp_used = self.client_joiner.post(
            reverse("budgets:shared_space"),
            {"action": "accept_invitation", "code": inv_used.code},
            follow=True,
        )
        self.assertContains(resp_used, INVITATION_ERROR_MESSAGE)

    def test_owner_cannot_join_own_space(self):
        """A space owner cannot join their own space."""
        inv = create_shared_space_invitation(user=self.owner, shared_space=self.space)
        resp = self.client_owner.post(
            reverse("budgets:shared_space"),
            {"action": "accept_invitation", "code": inv.code},
            follow=True,
        )
        self.assertContains(resp, "You cannot join your own shared space.")

    def test_single_use_under_repeated_submissions(self):
        """An invitation code can only be used once; subsequent attempts fail."""
        inv = create_shared_space_invitation(user=self.owner, shared_space=self.space)
        # First use by joiner
        resp1 = self.client_joiner.post(
            reverse("budgets:shared_space"),
            {"action": "accept_invitation", "code": inv.code},
            follow=True,
        )
        self.assertEqual(resp1.status_code, 200)

        # Second use by third user
        client_third = Client()
        client_third.force_login(self.third_user)
        resp2 = client_third.post(
            reverse("budgets:shared_space"),
            {"action": "accept_invitation", "code": inv.code},
            follow=True,
        )
        self.assertContains(resp2, "This code is invalid or has expired.")

    def test_rate_limit_lockout_after_five_failures(self):
        """Five failed redemption attempts result in a 15-minute lockout."""
        from .services import INVITATION_LOCKOUT_MESSAGE

        for i in range(5):
            resp = self.client_joiner.post(
                reverse("budgets:shared_space"),
                {"action": "accept_invitation", "code": f"BAD00{i}"},
                follow=True,
            )
            self.assertEqual(resp.status_code, 200)

        # 6th attempt should trigger lockout
        resp6 = self.client_joiner.post(
            reverse("budgets:shared_space"),
            {"action": "accept_invitation", "code": "BAD999"},
            follow=True,
        )
        self.assertContains(resp6, INVITATION_LOCKOUT_MESSAGE)

    def test_max_five_active_invitations(self):
        """An owner can have at most 5 active invitations; 6th raises error."""
        invites = []
        for i in range(5):
            inv = create_shared_space_invitation(user=self.owner, shared_space=self.space, partner_hint=f"note {i}")
            invites.append(inv)

        with self.assertRaises(ValidationError) as ctx:
            create_shared_space_invitation(user=self.owner, shared_space=self.space)
        self.assertIn("limit of 5 active invitations", str(ctx.exception))

        # Revoking one allows creating another
        revoke_shared_space_invitation(user=self.owner, invitation=invites[0])
        new_inv = create_shared_space_invitation(user=self.owner, shared_space=self.space)
        self.assertIsNotNone(new_inv)

    def test_qr_svg_present_and_contains_join_url(self):
        """Pending invitation renders an inline SVG QR code and contains the join URL."""
        inv = create_shared_space_invitation(user=self.owner, shared_space=self.space)
        resp = self.client_owner.get(reverse("budgets:shared_space"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, f'<div id="qr-svg-{inv.id}" class="d-none">')
        self.assertContains(resp, '<svg role="img"')
        self.assertContains(resp, 'width="246"')
        self.assertContains(resp, f'data-raw-code="{inv.code}"')
        self.assertContains(resp, f'data-qr-code="{inv.formatted_code}"')
        self.assertContains(resp, f'/shared-space/?code={inv.code}')

    def test_legacy_long_codes_still_redeem(self):
        """Legacy 32-character codes can still be redeemed until expiration."""
        legacy_code = "9f8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c"
        legacy_inv = SharedSpaceInvitation.objects.create(
            shared_space=self.space,
            code=legacy_code,
            created_by=self.owner,
            status=SharedSpaceInvitation.Status.PENDING,
            expires_at=timezone.now() + timedelta(days=2),
        )
        resp = self.client_joiner.post(
            reverse("budgets:shared_space"),
            {"action": "accept_invitation", "code": legacy_code},
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        legacy_inv.refresh_from_db()
        self.assertEqual(legacy_inv.status, SharedSpaceInvitation.Status.ACCEPTED)

    def test_revoke_still_works(self):
        """Revoking a pending invitation marks it revoked."""
        inv = create_shared_space_invitation(user=self.owner, shared_space=self.space)
        resp = self.client_owner.post(
            reverse("budgets:shared_space"),
            {"action": "revoke_invitation", "invitation_id": inv.id},
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        inv.refresh_from_db()
        self.assertEqual(inv.status, SharedSpaceInvitation.Status.REVOKED)

    def test_owner_sees_both_buttons_on_pending_legacy_and_short_invitations(self):
        """Owner sees Show QR and Revoke for both short and legacy pending invitations."""
        short_inv = create_shared_space_invitation(user=self.owner, shared_space=self.space)
        legacy_code = "8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c3d"
        legacy_inv = SharedSpaceInvitation.objects.create(
            shared_space=self.space,
            code=legacy_code,
            created_by=self.owner,
            status=SharedSpaceInvitation.Status.PENDING,
            expires_at=timezone.now() + timedelta(days=2),
        )

        resp = self.client_owner.get(reverse("budgets:shared_space"))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")

        # Both unmasked codes are visible to owner
        self.assertIn(short_inv.formatted_code, html)
        self.assertIn(legacy_code, html)

        # Show QR buttons present for both
        self.assertIn(f'data-inv-id="{short_inv.id}"', html)
        self.assertIn(f'data-inv-id="{legacy_inv.id}"', html)
        self.assertEqual(html.count("Show QR"), 2)

        # Revoke forms present for both
        self.assertIn(f'value="{short_inv.id}"', html)
        self.assertIn(f'value="{legacy_inv.id}"', html)
        self.assertIn("Revoke", html)

    def test_no_buttons_on_used_expired_revoked_rows_and_masked_codes(self):
        """Used, expired, and revoked rows display no action buttons and mask the code."""
        from django.utils.formats import date_format
        now = timezone.now()

        # Used invitation
        used_inv = SharedSpaceInvitation.objects.create(
            shared_space=self.space,
            code="USD123",
            created_by=self.owner,
            accepted_by=self.joiner,
            status=SharedSpaceInvitation.Status.ACCEPTED,
            expires_at=now + timedelta(days=1),
            responded_at=now - timedelta(hours=2),
        )

        # Revoked invitation
        revoked_inv = SharedSpaceInvitation.objects.create(
            shared_space=self.space,
            code="RVK456",
            created_by=self.owner,
            status=SharedSpaceInvitation.Status.REVOKED,
            expires_at=now + timedelta(days=1),
            responded_at=now - timedelta(hours=3),
        )

        # Expired invitation (status explicitly expired)
        expired_inv = SharedSpaceInvitation.objects.create(
            shared_space=self.space,
            code="EXP789",
            created_by=self.owner,
            status=SharedSpaceInvitation.Status.EXPIRED,
            expires_at=now - timedelta(days=1),
        )

        # Pending invitation that is expired by time
        time_expired_inv = SharedSpaceInvitation.objects.create(
            shared_space=self.space,
            code="TEX000",
            created_by=self.owner,
            status=SharedSpaceInvitation.Status.PENDING,
            expires_at=now - timedelta(minutes=10),
        )

        resp = self.client_owner.get(reverse("budgets:shared_space"))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")

        # Action buttons: neither Show QR nor Revoke should appear for these 4 non-pending rows
        self.assertNotIn("Show QR", html)
        self.assertNotIn(">Revoke<", html)

        # Masked codes (first 3 chars followed by dots)
        self.assertIn("USD...", html)
        self.assertIn("RVK...", html)
        self.assertIn("EXP...", html)
        self.assertIn("TEX...", html)

        # Full codes must NOT be rendered in HTML
        self.assertNotIn("USD-123", html)
        self.assertNotIn("RVK-456", html)
        self.assertNotIn("EXP-789", html)
        self.assertNotIn("TEX-000", html)

        # Expires column formatted labels
        joined_date = date_format(used_inv.responded_at, "M j, Y")
        self.assertIn(f"Joined by {self.joiner.username} on {joined_date}", html)

        rev_date = date_format(revoked_inv.responded_at, "M j, Y")
        self.assertIn(f"Revoked {rev_date}", html)

        exp_date = date_format(expired_inv.expires_at, "M j, Y")
        self.assertIn(f"Expired {exp_date}", html)

        tex_date = date_format(time_expired_inv.expires_at, "M j, Y")
        self.assertIn(f"Expired {tex_date}", html)

    def test_non_owner_member_cannot_see_codes_or_buttons_and_endpoints_reject(self):
        """Non-owner member sees only status (no codes, no QR, no buttons); endpoints return 403/404."""
        # Joiner joins the space
        init_inv = create_shared_space_invitation(user=self.owner, shared_space=self.space)
        accept_shared_space_invitation(user=self.joiner, code=init_inv.code)

        # Space now has 2 members: self.owner (creator) and self.joiner (member)
        # Create a pending invite in the space
        pending_inv = SharedSpaceInvitation.objects.create(
            shared_space=self.space,
            code="ABC777",
            created_by=self.owner,
            status=SharedSpaceInvitation.Status.PENDING,
            expires_at=timezone.now() + timedelta(days=2),
        )

        resp = self.client_joiner.get(reverse("budgets:shared_space"))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")

        # Non-owner member does NOT see codes or masked codes
        self.assertNotIn("ABC777", html)
        self.assertNotIn("ABC-777", html)
        self.assertNotIn("ABC...", html)
        self.assertNotIn(init_inv.code, html)

        # Non-owner member does NOT see action buttons or QR modal
        self.assertNotIn("Show QR", html)
        self.assertNotIn(">Revoke<", html)
        self.assertNotIn('id="qrModal"', html)

        # Non-owner member sees status pills, but NOT expiry/response dates or joiner names
        self.assertIn("Pending", html)
        self.assertIn("Used", html)
        self.assertNotIn(f"Joined by {self.joiner.username}", html)
        self.assertNotIn("remaining", html)

        # Non-owner member rejected on Revoke endpoint -> 403
        resp_revoke = self.client_joiner.post(
            reverse("budgets:shared_space"),
            {"action": "revoke_invitation", "invitation_id": pending_inv.id},
        )
        self.assertEqual(resp_revoke.status_code, 403)

        # Non-owner member rejected on Show QR endpoint -> 403
        resp_qr = self.client_joiner.get(
            reverse("budgets:shared_space"),
            {"action": "show_qr", "invitation_id": pending_inv.id},
        )
        self.assertEqual(resp_qr.status_code, 403)

        # Owner gets 200 on Show QR endpoint
        resp_owner_qr = self.client_owner.get(
            reverse("budgets:shared_space"),
            {"action": "show_qr", "invitation_id": pending_inv.id},
        )
        self.assertEqual(resp_owner_qr.status_code, 200)
        self.assertEqual(resp_owner_qr["Content-Type"], "image/svg+xml")

        # Non-existent invitation returns 404 for both endpoints
        import uuid
        dummy_id = uuid.uuid4()
        resp_404_rev = self.client_owner.post(
            reverse("budgets:shared_space"),
            {"action": "revoke_invitation", "invitation_id": dummy_id},
        )
        self.assertEqual(resp_404_rev.status_code, 404)

        resp_404_qr = self.client_owner.get(
            reverse("budgets:shared_space"),
            {"action": "show_qr", "invitation_id": dummy_id},
        )
        self.assertEqual(resp_404_qr.status_code, 404)

    def test_revoking_legacy_code_makes_it_unusable(self):
        """Revoking a legacy long-code invitation prevents redemption."""
        legacy_code = "7a6b5c4d3e2f1a0b9c8d7e6f5a4b3c2d"
        legacy_inv = SharedSpaceInvitation.objects.create(
            shared_space=self.space,
            code=legacy_code,
            created_by=self.owner,
            status=SharedSpaceInvitation.Status.PENDING,
            expires_at=timezone.now() + timedelta(days=2),
        )

        # Owner revokes legacy code
        resp = self.client_owner.post(
            reverse("budgets:shared_space"),
            {"action": "revoke_invitation", "invitation_id": legacy_inv.id},
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        legacy_inv.refresh_from_db()
        self.assertEqual(legacy_inv.status, SharedSpaceInvitation.Status.REVOKED)
        self.assertIsNotNone(legacy_inv.responded_at)

        # Attempt to accept via POST returns error
        from .services import INVITATION_ERROR_MESSAGE
        resp_attempt = self.client_joiner.post(
            reverse("budgets:shared_space"),
            {"action": "accept_invitation", "code": legacy_code},
            follow=True,
        )
        self.assertEqual(resp_attempt.status_code, 200)
        self.assertContains(resp_attempt, INVITATION_ERROR_MESSAGE)

        # Attempting via direct service call raises ValidationError
        from django.core.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            accept_shared_space_invitation(user=self.joiner, code=legacy_code)

    def test_create_another_shared_space(self):
        """User can create another shared space via the UI entry point, updating active space preference."""
        from apps.accounts.models import UserPreference
        from .models import SharedSpace
        from .selectors import get_user_active_memberships
        owner_pref, _ = UserPreference.objects.get_or_create(
            user=self.owner, defaults={"personal_base_currency": self.usd}
        )

        # Check initial page renders "Create another shared space" button and modal
        resp = self.client_owner.get(reverse("budgets:shared_space"))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")
        self.assertIn("Create another shared space", html)
        self.assertIn('id="createSpaceModal"', html)
        self.assertIn('id="btnCreateAnotherSpace"', html)

        # Post create_space action
        post_resp = self.client_owner.post(
            reverse("budgets:shared_space"),
            {
                "action": "create_space",
                "name": "Second Family Vacation",
                "base_currency": self.usd.pk,
            },
        )
        new_space = SharedSpace.objects.get(name="Second Family Vacation")
        self.assertEqual(new_space.created_by, self.owner)
        self.assertRedirects(post_resp, f"{reverse('budgets:shared_space')}?space={new_space.id}")

        # Owner preference is switched to the new shared space
        owner_pref.refresh_from_db()
        self.assertEqual(owner_pref.active_scope, "shared")
        self.assertEqual(owner_pref.active_shared_space, new_space)

        # User has active memberships in both spaces
        owner_spaces = [m.shared_space for m in get_user_active_memberships(self.owner)]
        self.assertIn(self.space, owner_spaces)
        self.assertIn(new_space, owner_spaces)

    def test_member_leave_shared_space_redirects_and_updates_preference(self):
        """A member can leave a shared space, updating their workspace preference to personal or remaining space."""
        from apps.accounts.models import UserPreference
        from .models import SharedSpace, SharedSpaceMembership
        joiner_pref, _ = UserPreference.objects.get_or_create(
            user=self.joiner, defaults={"personal_base_currency": self.usd}
        )

        # Joiner joins self.space
        inv = create_shared_space_invitation(user=self.owner, shared_space=self.space)
        accept_shared_space_invitation(user=self.joiner, code=inv.code)
        joiner_pref.active_scope = "shared"
        joiner_pref.active_shared_space = self.space
        joiner_pref.save()

        # Joiner page renders "Leave this shared space" button
        resp = self.client_joiner.get(reverse("budgets:shared_space"))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")
        self.assertIn("Leave this shared space", html)
        self.assertIn('id="btnLeaveSharedSpace"', html)

        # Member leaves the space (no remaining shared spaces)
        leave_resp = self.client_joiner.post(
            reverse("budgets:shared_space"),
            {"action": "leave_space"},
        )
        self.assertRedirects(leave_resp, reverse("budgets:shared_space"))

        # Verify membership status
        mem = SharedSpaceMembership.objects.get(shared_space=self.space, user=self.joiner)
        self.assertFalse(mem.is_active)
        self.assertIsNotNone(mem.left_at)

        # Verify preference updated to personal workspace
        joiner_pref.refresh_from_db()
        self.assertEqual(joiner_pref.active_scope, "personal")
        self.assertIsNone(joiner_pref.active_shared_space)

        # Multi-space scenario: joiner is member of space2 and space3; leaves space2 -> redirected to space3
        space2 = create_shared_space(user=self.owner, name="Space Two", base_currency=self.usd)
        space3 = create_shared_space(user=self.owner, name="Space Three", base_currency=self.usd)
        inv2 = create_shared_space_invitation(user=self.owner, shared_space=space2)
        inv3 = create_shared_space_invitation(user=self.owner, shared_space=space3)
        accept_shared_space_invitation(user=self.joiner, code=inv2.code)
        accept_shared_space_invitation(user=self.joiner, code=inv3.code)

        # Set active space to space2
        joiner_pref.active_scope = "shared"
        joiner_pref.active_shared_space = space2
        joiner_pref.save()

        # Leave space2
        leave_resp2 = self.client_joiner.post(
            f"{reverse('budgets:shared_space')}?space={space2.id}",
            {"action": "leave_space"},
        )
        self.assertRedirects(leave_resp2, f"{reverse('budgets:shared_space')}?space={space3.id}")

        joiner_pref.refresh_from_db()
        self.assertEqual(joiner_pref.active_scope, "shared")
        self.assertEqual(joiner_pref.active_shared_space, space3)

    def test_owner_with_active_member_safeguard(self):
        """Owner cannot leave while an active member remains; guidance is displayed and leave action is prevented."""
        from .models import SharedSpaceMembership

        # Joiner joins self.space
        inv = create_shared_space_invitation(user=self.owner, shared_space=self.space)
        accept_shared_space_invitation(user=self.joiner, code=inv.code)

        # Owner views page: guidance notice is shown, and "Leave this shared space" button is hidden
        resp = self.client_owner.get(reverse("budgets:shared_space"))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")
        self.assertIn("The shared-space creator cannot leave while managing the space.", html)
        self.assertNotIn("Leave this shared space", html)

        # Owner attempts POST action="leave_space"
        post_resp = self.client_owner.post(
            reverse("budgets:shared_space"),
            {"action": "leave_space"},
            follow=True,
        )
        self.assertEqual(post_resp.status_code, 200)
        self.assertContains(post_resp, "The shared-space creator cannot leave while managing the space.")

        # Owner and member memberships remain active
        owner_mem = SharedSpaceMembership.objects.get(shared_space=self.space, user=self.owner)
        self.assertTrue(owner_mem.is_active)
        self.assertIsNone(owner_mem.left_at)

        joiner_mem = SharedSpaceMembership.objects.get(shared_space=self.space, user=self.joiner)
        self.assertTrue(joiner_mem.is_active)


class TransactionDateEditConversionPreservationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="fx_editor", password="Password123!")
        self.usd = Currency.objects.get(code="USD")
        self.php = Currency.objects.get(code="PHP")
        UserPreference.objects.create(user=self.user, personal_base_currency=self.php)
        self.wallet = Wallet.objects.create(
            owner=self.user,
            ownership_scope=Wallet.OwnershipScope.PERSONAL,
            name="USD Wallet",
            wallet_type=Wallet.WalletType.BANK_ACCOUNT,
            currency=self.usd,
            opening_balance=Decimal("500.00"),
            opening_balance_date=date(2026, 1, 1),
        )
        self.category = Category.objects.create(
            owner=self.user,
            scope=Category.Scope.PERSONAL,
            name="Travel",
            kind=Category.Kind.EXPENSE,
        )

    def test_date_edit_preserves_manual_rate(self):
        from .services import create_transaction_entry, update_transaction_entry

        tx = create_transaction_entry(
            user=self.user,
            transaction_type=Transaction.Type.EXPENSE,
            amount=Decimal("60.00"),
            wallet=self.wallet,
            transaction_date=date(2026, 11, 10),
            category=self.category,
            manual_rate=Decimal("28.33333333"),
        )
        self.assertEqual(tx.conversion_status, Transaction.ConversionStatus.MANUAL)
        self.assertEqual(tx.converted_amount, Decimal("1700.00"))

        updated = update_transaction_entry(
            user=self.user,
            tx=tx,
            transaction_type=Transaction.Type.EXPENSE,
            amount=Decimal("60.00"),
            wallet=self.wallet,
            transaction_date=date(2026, 11, 8),
            category=self.category,
        )
        self.assertEqual(updated.conversion_status, Transaction.ConversionStatus.MANUAL)
        self.assertEqual(updated.conversion_rate, Decimal("28.33333333"))
        self.assertEqual(updated.converted_amount, Decimal("1700.00"))

    def test_date_edit_preserves_booked_rate_when_new_date_has_no_cached_rate(self):
        from datetime import datetime
        from .models import ExchangeRate
        from .services import create_transaction_entry, update_transaction_entry

        ExchangeRate.objects.create(
            from_currency=self.usd,
            to_currency=self.php,
            rate=Decimal("28.33333333"),
            provider="frankfurter",
            rate_timestamp=timezone.make_aware(datetime(2026, 11, 10, 12, 0)),
            is_manual=False,
            scope=ExchangeRate.Scope.PROVIDER,
        )
        tx = create_transaction_entry(
            user=self.user,
            transaction_type=Transaction.Type.EXPENSE,
            amount=Decimal("60.00"),
            wallet=self.wallet,
            transaction_date=date(2026, 11, 10),
            category=self.category,
        )
        self.assertEqual(tx.conversion_status, Transaction.ConversionStatus.CONVERTED)
        self.assertEqual(tx.converted_amount, Decimal("1700.00"))

        updated = update_transaction_entry(
            user=self.user,
            tx=tx,
            transaction_type=Transaction.Type.EXPENSE,
            amount=Decimal("60.00"),
            wallet=self.wallet,
            transaction_date=date(2026, 11, 8),
            category=self.category,
        )
        self.assertEqual(updated.conversion_status, Transaction.ConversionStatus.CONVERTED)
        self.assertEqual(updated.conversion_rate, Decimal("28.33333333"))
        self.assertEqual(updated.converted_amount, Decimal("1700.00"))

    def test_date_edit_updates_provider_rate_when_new_date_has_cached_rate(self):
        from datetime import datetime
        from .models import ExchangeRate
        from .services import create_transaction_entry, update_transaction_entry

        ExchangeRate.objects.create(
            from_currency=self.usd,
            to_currency=self.php,
            rate=Decimal("28.33333333"),
            provider="frankfurter",
            rate_timestamp=timezone.make_aware(datetime(2026, 11, 10, 12, 0)),
            is_manual=False,
            scope=ExchangeRate.Scope.PROVIDER,
        )
        ExchangeRate.objects.create(
            from_currency=self.usd,
            to_currency=self.php,
            rate=Decimal("26.66666667"),
            provider="frankfurter",
            rate_timestamp=timezone.make_aware(datetime(2026, 11, 8, 12, 0)),
            is_manual=False,
            scope=ExchangeRate.Scope.PROVIDER,
        )
        tx = create_transaction_entry(
            user=self.user,
            transaction_type=Transaction.Type.EXPENSE,
            amount=Decimal("60.00"),
            wallet=self.wallet,
            transaction_date=date(2026, 11, 10),
            category=self.category,
        )
        self.assertEqual(tx.converted_amount, Decimal("1700.00"))

        updated = update_transaction_entry(
            user=self.user,
            tx=tx,
            transaction_type=Transaction.Type.EXPENSE,
            amount=Decimal("60.00"),
            wallet=self.wallet,
            transaction_date=date(2026, 11, 8),
            category=self.category,
        )
        self.assertEqual(updated.conversion_status, Transaction.ConversionStatus.CONVERTED)
        self.assertEqual(updated.conversion_rate, Decimal("26.66666667"))
        self.assertEqual(updated.converted_amount, Decimal("1600.00"))
