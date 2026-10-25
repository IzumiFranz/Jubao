from datetime import date
from decimal import Decimal
from importlib import import_module

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection
from django.db.models.deletion import ProtectedError
from django.test import Client, TestCase
from django.urls import reverse

from apps.accounts.models import UserPreference

from .currency_catalog import SUPPORTED_CURRENCIES
from .models import Currency, Wallet

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
        self.assertEqual(wallet.formatted_opening_balance, "3250.50")

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
        self.assertEqual(wallet.formatted_opening_balance, "18500.25")

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
        self.assertEqual(wallet.formatted_opening_balance, "1250.50")
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
            ("Yen Cash", jpy, "15000", "15000"),
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
        self.assertEqual(wallet.formatted_current_balance, "1374.75")

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
        self.assertEqual(goal.formatted_saved_amount, "5000.00")
        self.assertEqual(goal.formatted_remaining_amount, "45000.00")
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

        # Service worker uses bumped cache v3 and deletes only budget-tracker-shell-* caches
        sw_resp = self.client.get("/service-worker.js")
        sw_js = sw_resp.content.decode("utf-8")
        self.assertIn('const CACHE_NAME = "budget-tracker-shell-v3"', sw_js)
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
        self.assertEqual(goal.formatted_target_amount, "200000")
        self.assertEqual(goal.formatted_saved_amount, "50000")
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
            transaction_date=date(2026, 10, 5),
            description="Consulting USD",
        )
        tx_eur = create_transaction_entry(
            user=self.alice,
            transaction_type="expense",
            amount=Decimal("50.00"),
            wallet=wallet_eur,
            transaction_date=date(2026, 10, 6),
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
        self.assertContains(resp_php, "+5650.00 PHP")
        self.assertContains(resp_php, f"Booked: {tx_usd.formatted_converted_amount} {tx_usd.base_currency.code}")

        # 3. View in TWD via ?display_currency=TWD
        resp_twd = self.client.get("/transactions/?display_currency=TWD")
        self.assertEqual(resp_twd.status_code, 200)
        self.assertContains(resp_twd, "Converted (TWD)")
        self.assertContains(resp_twd, "+3200.00 TWD")
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
        self.assertContains(resp_filtered, "+5650.00 PHP")

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
                transaction_date=date(2026, 10, 10),
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
            transaction_date=date(2026, 10, 5),
            scope="personal",
            description="Personal Grocery",
        )
        tx_space_a1 = create_transaction_entry(
            user=self.alice,
            transaction_type="expense",
            amount=Decimal("100.00"),
            wallet=wallet_a,
            transaction_date=date(2026, 10, 6),
            scope="shared",
            shared_space=space_a,
            description="Alpha Server",
        )
        tx_space_a2 = create_transaction_entry(
            user=self.alice,
            transaction_type="expense",
            amount=Decimal("50.00"),
            wallet=wallet_a,
            transaction_date=date(2026, 10, 7),
            scope="shared",
            shared_space=space_a,
            description="Alpha Cables",
        )
        tx_space_b = create_transaction_entry(
            user=self.bob,
            transaction_type="expense",
            amount=Decimal("100.00"),
            wallet=wallet_b,
            transaction_date=date(2026, 10, 8),
            scope="shared",
            shared_space=space_b,
            description="Beta Subscription",
        )
        tx_twd = create_transaction_entry(
            user=self.alice,
            transaction_type="expense",
            amount=Decimal("500.00"),
            wallet=wallet_twd,
            transaction_date=date(2026, 10, 9),
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
        self.assertEqual(tx_personal.formatted_display_amount, "3200.00")
        # Space A1: 100 * 30.00 = 3000.00 TWD
        self.assertEqual(tx_space_a1.display_amount, Decimal("3000.00"))
        self.assertEqual(tx_space_a1.formatted_display_amount, "3000.00")
        # Space B: 100 * 35.00 = 3500.00 TWD
        self.assertEqual(tx_space_b.display_amount, Decimal("3500.00"))
        self.assertEqual(tx_space_b.formatted_display_amount, "3500.00")
        # Space A2: 50 * 30.00 = 1500.00 TWD (reused Space A lookup)
        self.assertEqual(tx_space_a2.display_amount, Decimal("1500.00"))
        self.assertEqual(tx_space_a2.formatted_display_amount, "1500.00")
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
        self.assertContains(resp_p, "-3200.00 TWD")  # Personal row (rate 32)
        self.assertContains(resp_p, "-3000.00 TWD")  # Space A row (rate 30)
        self.assertContains(resp_p, "-1500.00 TWD")  # Space A2 row (rate 30)
        self.assertContains(resp_p, "-3500.00 TWD")  # Space B row (rate 35)
        self.assertNotContains(resp_p, "9900.00")    # Unauthorized private rate excluded
        self.assertNotContains(resp_p, "8800.00")    # Unauthorized private rate excluded

        # Case B: Active workspace is Shared Space A
        pref.active_scope = "shared"
        pref.active_shared_space = space_a
        pref.save()
        resp_a = self.client.get("/transactions/?display_currency=TWD")
        self.assertEqual(resp_a.status_code, 200)
        self.assertContains(resp_a, "-3200.00 TWD")  # Personal row STILL uses 32
        self.assertContains(resp_a, "-3000.00 TWD")  # Space A row STILL uses 30
        self.assertContains(resp_a, "-1500.00 TWD")  # Space A2 row STILL uses 30
        self.assertContains(resp_a, "-3500.00 TWD")  # Space B row STILL uses 35

        # Case C: Active workspace is Shared Space B
        pref.active_scope = "shared"
        pref.active_shared_space = space_b
        pref.save()
        resp_b = self.client.get("/transactions/?display_currency=TWD")
        self.assertEqual(resp_b.status_code, 200)
        self.assertContains(resp_b, "-3200.00 TWD")  # Personal row STILL uses 32
        self.assertContains(resp_b, "-3000.00 TWD")  # Space A row STILL uses 30
        self.assertContains(resp_b, "-1500.00 TWD")  # Space A2 row STILL uses 30
        self.assertContains(resp_b, "-3500.00 TWD")  # Space B row STILL uses 35


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
        from .exchange_rates import refresh_exchange_rates, get_latest_rate

        def mock_fetcher(url):
            self.assertIn("api.frankfurter.dev/v2/rates", url)
            return [
                {"date": "2026-10-25", "base": "PHP", "quote": "EUR", "rate": Decimal("0.01404000")},
                {"date": "2026-10-25", "base": "PHP", "quote": "JPY", "rate": Decimal("2.47390000")},
                {"date": "2026-10-25", "base": "PHP", "quote": "KWD", "rate": Decimal("0.00486000")},
                {"date": "2026-10-25", "base": "PHP", "quote": "TWD", "rate": Decimal("0.51249000")},
                {"date": "2026-10-25", "base": "PHP", "quote": "USD", "rate": Decimal("0.01575000")},
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
        self.assertEqual(lookup_php_twd.rate_timestamp.date(), date(2026, 10, 25))
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
            transaction_date=date(2026, 10, 15),
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
        self.assertContains(bob_view, "4857.50")
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
        self.assertContains(bob_view2, "4857.50")
        self.assertNotContains(bob_view2, "29857.50")
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
        self.assertNotContains(bob_view3, "4857.50")

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
        self.assertContains(preview_resp, "4857.50")
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
        self.assertNotContains(bob_view, "4857.50")

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
        self.assertContains(preview_resp, "728625")
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
        self.assertContains(preview_fallback, "4857.50")
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
        self.assertContains(preview_no_pref, "4857.50")
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
        self.assertContains(resp_same, "4857.50")
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




