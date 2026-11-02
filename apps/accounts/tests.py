from datetime import date
from decimal import Decimal

from django.contrib.auth.models import User
from django.db.models.deletion import ProtectedError
from django.test import Client, TestCase
from django.urls import reverse

from apps.budgets.models import Currency, Wallet

from .models import UserPreference


class AccountAuthenticationTests(TestCase):
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
        self.login_url = reverse("accounts:login")
        self.register_url = reverse("accounts:register")
        self.logout_url = reverse("accounts:logout")
        self.dashboard_url = reverse("budgets:dashboard")
        self.settings_url = reverse("accounts:settings")
        self.currencies_url = reverse("budgets:currencies")

    def test_valid_registration_creates_user_with_hashed_password_and_redirects(self):
        raw_password = "Str0ng!Password#2026"
        response = self.client.post(
            self.register_url,
            {
                "username": "new_budget_user",
                "password1": raw_password,
                "password2": raw_password,
            },
            follow=True,
        )
        self.assertRedirects(response, self.login_url)
        user = User.objects.get(username="new_budget_user")
        self.assertNotEqual(user.password, raw_password)
        self.assertTrue(user.check_password(raw_password))
        self.assertEqual(UserPreference.objects.count(), 0)
        self.assertContains(response, "Your account has been created.")

    def test_registration_rejects_duplicate_username(self):
        User.objects.create_user(username="existing_user", password="Str0ng!Password#2026")
        response = self.client.post(
            self.register_url,
            {
                "username": "existing_user",
                "password1": "An0ther!Pass#2026",
                "password2": "An0ther!Pass#2026",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(User.objects.filter(username="existing_user").count(), 1)
        self.assertFormError(
            response.context["form"],
            "username",
            "A user with that username already exists.",
        )

    def test_registration_rejects_mismatched_passwords(self):
        response = self.client.post(
            self.register_url,
            {
                "username": "mismatch_user",
                "password1": "Str0ng!Password#2026",
                "password2": "Different!Password#2026",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username="mismatch_user").exists())
        self.assertTrue(response.context["form"].errors.get("password2"))

    def test_registration_rejects_weak_passwords(self):
        response = self.client.post(
            self.register_url,
            {
                "username": "weak_pass_user",
                "password1": "12345",
                "password2": "12345",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username="weak_pass_user").exists())
        self.assertTrue(response.context["form"].errors.get("password2"))

    def test_login_succeeds_and_invalid_login_fails(self):
        User.objects.create_user(username="valid_user", password="Str0ng!Password#2026")

        invalid_response = self.client.post(
            self.login_url,
            {"username": "valid_user", "password": "WrongPassword!123"},
        )
        self.assertEqual(invalid_response.status_code, 200)
        self.assertContains(invalid_response, "Invalid username or password. Please try again.")
        self.assertFalse(invalid_response.wsgi_request.user.is_authenticated)

        valid_response = self.client.post(
            self.login_url,
            {"username": "valid_user", "password": "Str0ng!Password#2026"},
        )
        self.assertRedirects(valid_response, self.dashboard_url)

    def test_login_rejects_external_next_url(self):
        User.objects.create_user(username="redirect_user", password="Str0ng!Password#2026")
        response = self.client.post(
            f"{self.login_url}?next=https://malicious.example.com/phish",
            {
                "username": "redirect_user",
                "password": "Str0ng!Password#2026",
                "next": "https://malicious.example.com/phish",
            },
        )
        self.assertRedirects(response, self.dashboard_url)

    def test_anonymous_requests_redirect_to_login_for_all_application_routes(self):
        for route_name in self.APPLICATION_ROUTES:
            with self.subTest(route=route_name):
                target_url = reverse(route_name)
                response = self.client.get(target_url)
                expected_redirect = f"{self.login_url}?next={target_url}"
                self.assertRedirects(response, expected_redirect)

    def test_authenticated_requests_return_200_for_all_application_routes(self):
        user = User.objects.create_user(username="authed_user", password="Str0ng!Password#2026")
        self.client.force_login(user)

        for route_name in self.APPLICATION_ROUTES:
            with self.subTest(route=route_name):
                response = self.client.get(reverse(route_name))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "authed_user")

    def test_user_sees_own_identity_and_not_another_users_identity(self):
        user_alice = User.objects.create_user(
            username="alice_tracker",
            password="Str0ng!Password#2026",
            first_name="Alice",
            last_name="Walker",
            email="alice@example.com",
        )
        user_bob = User.objects.create_user(
            username="bob_tracker",
            password="Str0ng!Password#2026",
        )

        self.client.force_login(user_alice)
        alice_settings = self.client.get(self.settings_url)
        self.assertContains(alice_settings, "alice_tracker")
        self.assertContains(alice_settings, 'value="Alice Walker"')
        self.assertContains(alice_settings, 'value="alice@example.com"')
        self.assertNotContains(alice_settings, "bob_tracker")

        self.client.force_login(user_bob)
        bob_settings = self.client.get(self.settings_url)
        self.assertContains(bob_settings, "bob_tracker")
        self.assertContains(bob_settings, 'value="bob_tracker"')
        self.assertContains(bob_settings, 'name="email"')
        self.assertNotContains(bob_settings, "alice_tracker")
        self.assertNotContains(bob_settings, "Alice Walker")
        self.assertNotContains(bob_settings, "alice@example.com")

    def test_get_logout_does_not_log_user_out_and_post_logout_works(self):
        user = User.objects.create_user(username="logout_user", password="Str0ng!Password#2026")
        self.client.force_login(user)

        get_response = self.client.get(self.logout_url)
        self.assertEqual(get_response.status_code, 405)
        self.assertEqual(self.client.get(self.dashboard_url).status_code, 200)

        post_response = self.client.post(self.logout_url)
        self.assertRedirects(post_response, self.login_url)
        dashboard_after_logout = self.client.get(self.dashboard_url)
        self.assertRedirects(dashboard_after_logout, f"{self.login_url}?next={self.dashboard_url}")

    def test_csrf_enforced_on_login_register_and_logout_posts(self):
        csrf_client = Client(enforce_csrf_checks=True)

        login_post = csrf_client.post(
            self.login_url,
            {"username": "any_user", "password": "Str0ng!Password#2026"},
        )
        self.assertEqual(login_post.status_code, 403)

        register_post = csrf_client.post(
            self.register_url,
            {
                "username": "csrf_user",
                "password1": "Str0ng!Password#2026",
                "password2": "Str0ng!Password#2026",
            },
        )
        self.assertEqual(register_post.status_code, 403)

        user = User.objects.create_user(username="csrf_logout_user", password="Str0ng!Password#2026")
        csrf_client.force_login(user)
        logout_post = csrf_client.post(self.logout_url)
        self.assertEqual(logout_post.status_code, 403)


class PersonalCurrencyPreferenceTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user(
            username="alice_pref",
            password="Str0ng!Password#2026",
        )
        self.bob = User.objects.create_user(
            username="bob_pref",
            password="Str0ng!Password#2026",
        )
        self.currencies_url = reverse("budgets:currencies")
        self.dashboard_url = reverse("budgets:dashboard")
        self.wallets_url = reverse("budgets:wallets")
        self.login_url = reverse("accounts:login")

    def test_anonymous_currencies_get_and_post_redirect_to_login(self):
        php = Currency.objects.get(code="PHP")
        get_resp = self.client.get(self.currencies_url)
        self.assertRedirects(get_resp, f"{self.login_url}?next={self.currencies_url}")

        post_resp = self.client.post(
            self.currencies_url,
            {"personal_base_currency": php.pk},
        )
        self.assertRedirects(post_resp, f"{self.login_url}?next={self.currencies_url}")
        self.assertEqual(UserPreference.objects.count(), 0)

    def test_get_does_not_create_preferences_and_shows_unconfigured_state(self):
        self.client.force_login(self.alice)
        response = self.client.get(self.currencies_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(UserPreference.objects.count(), 0)
        self.assertContains(response, "Not configured (—)")
        self.assertContains(response, "Current preference: <strong>Not configured</strong>")

    def test_valid_post_saves_preference_for_authenticated_user_and_persists_across_requests(self):
        php = Currency.objects.get(code="PHP")
        eur = Currency.objects.get(code="EUR")
        self.client.force_login(self.alice)

        post_resp = self.client.post(
            self.currencies_url,
            {
                "user": self.bob.pk,
                "personal_base_currency": php.pk,
            },
            follow=True,
        )
        self.assertRedirects(post_resp, self.currencies_url)
        self.assertEqual(UserPreference.objects.count(), 1)
        pref = UserPreference.objects.get(user=self.alice)
        self.assertEqual(pref.personal_base_currency, php)
        self.assertFalse(UserPreference.objects.filter(user=self.bob).exists())
        self.assertContains(post_resp, "PHP (Philippine Peso)")

        dash_resp = self.client.get(self.dashboard_url)
        self.assertContains(dash_resp, "PHP (Philippine Peso)")

        update_resp = self.client.post(
            self.currencies_url,
            {"personal_base_currency": eur.pk},
            follow=True,
        )
        self.assertRedirects(update_resp, self.currencies_url)
        self.assertEqual(UserPreference.objects.count(), 1)
        pref.refresh_from_db()
        self.assertEqual(pref.personal_base_currency, eur)
        self.assertContains(update_resp, "EUR (Euro)")

        self.client.force_login(self.bob)
        bob_dash = self.client.get(self.dashboard_url)
        self.assertContains(bob_dash, "Not configured (—)")
        self.assertNotContains(bob_dash, "<option value=\"EUR\" selected>")

    def test_invalid_or_inactive_currency_rejected_on_preference_post(self):
        jpy = Currency.objects.get(code="JPY")
        jpy.is_active = False
        jpy.save()

        self.client.force_login(self.alice)

        for bad_value in ("", 999999, jpy.pk):
            with self.subTest(bad_value=bad_value):
                resp = self.client.post(
                    self.currencies_url,
                    {"personal_base_currency": bad_value},
                )
                self.assertEqual(resp.status_code, 200)
                self.assertTrue(resp.context["form"].errors.get("personal_base_currency"))
                self.assertEqual(UserPreference.objects.count(), 0)

    def test_changing_base_currency_leaves_wallets_and_balances_unchanged(self):
        usd = Currency.objects.get(code="USD")
        jpy = Currency.objects.get(code="JPY")
        php = Currency.objects.get(code="PHP")

        wallet = Wallet.objects.create(
            owner=self.alice,
            name="USD Savings",
            wallet_type=Wallet.WalletType.BANK_ACCOUNT,
            currency=usd,
            opening_balance=Decimal("450.25"),
            opening_balance_date=date(2026, 10, 1),
        )

        self.client.force_login(self.alice)
        self.client.post(self.currencies_url, {"personal_base_currency": php.pk})
        self.client.post(self.currencies_url, {"personal_base_currency": jpy.pk})

        wallet.refresh_from_db()
        self.assertEqual(wallet.currency, usd)
        self.assertEqual(wallet.opening_balance, Decimal("450.250"))
        self.assertEqual(wallet.formatted_opening_balance, "450.25")

    def test_csrf_and_unsupported_methods_on_currencies_page(self):
        php = Currency.objects.get(code="PHP")
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.alice)

        csrf_resp = csrf_client.post(
            self.currencies_url,
            {"personal_base_currency": php.pk},
        )
        self.assertEqual(csrf_resp.status_code, 403)
        self.assertEqual(UserPreference.objects.count(), 0)

        self.client.force_login(self.alice)
        for method in ("put", "delete", "patch"):
            with self.subTest(method=method):
                resp = getattr(self.client, method)(self.currencies_url)
                self.assertEqual(resp.status_code, 405)
                self.assertEqual(UserPreference.objects.count(), 0)

    def test_currency_referenced_by_user_preference_is_protected(self):
        php = Currency.objects.get(code="PHP")
        UserPreference.objects.create(user=self.alice, personal_base_currency=php)
        with self.assertRaises(ProtectedError):
            php.delete()


class AccountSettingsAndRateLimitTests(TestCase):
    def setUp(self):
        self.password = "Str0ng!Password#2026"
        self.user = User.objects.create_user(
            username="settings_user",
            password=self.password,
        )
        self.other = User.objects.create_user(
            username="other_settings_user",
            password=self.password,
        )
        self.settings_url = reverse("accounts:settings")
        self.password_change_url = reverse("accounts:password_change")
        self.switch_scope_url = reverse("accounts:switch_scope")
        self.export_url = reverse("accounts:export_data")
        self.login_url = reverse("accounts:login")

    def test_update_profile_display_name_and_preferences(self):
        self.client.force_login(self.user)

        prof_resp = self.client.post(
            self.settings_url,
            {"action": "save_profile", "display_name": "Maria Santos"},
            follow=True,
        )
        self.assertRedirects(prof_resp, self.settings_url)
        self.user.refresh_from_db()
        self.assertEqual(self.user.get_full_name(), "Maria Santos")

        pref_resp = self.client.post(
            self.settings_url,
            {
                "action": "save_preferences",
                "timezone": "Asia/Manila",
                "week_starts_on": UserPreference.WeekStartChoice.SUNDAY,
            },
            follow=True,
        )
        self.assertRedirects(pref_resp, self.settings_url)
        pref = UserPreference.objects.get(user=self.user)
        self.assertEqual(pref.timezone, "Asia/Manila")
        self.assertEqual(pref.week_starts_on, UserPreference.WeekStartChoice.SUNDAY)

    def test_password_change_flow_preserves_session(self):
        self.client.force_login(self.user)
        new_pass = "Upd@ted!Password#2027"
        resp = self.client.post(
            self.password_change_url,
            {
                "old_password": self.password,
                "new_password1": new_pass,
                "new_password2": new_pass,
            },
            follow=True,
        )
        self.assertRedirects(resp, reverse("accounts:password_change_done"))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(new_pass))
        self.assertEqual(self.client.get(self.settings_url).status_code, 200)

    def test_login_rate_limiting_blocks_excessive_failed_attempts(self):
        for _ in range(10):
            resp = self.client.post(
                self.login_url,
                {"username": "settings_user", "password": "WrongPassword!"},
            )
            self.assertEqual(resp.status_code, 200)

        blocked = self.client.post(
            self.login_url,
            {"username": "settings_user", "password": self.password},
        )
        self.assertEqual(blocked.status_code, 429)
        self.assertContains(blocked, "Too many sign-in attempts", status_code=429)

    def test_account_export_csv_and_json_with_formula_sanitization(self):
        from apps.budgets.models import Wallet
        from apps.budgets.services import create_transaction_entry

        usd = Currency.objects.get(code="USD")
        wallet = Wallet.objects.create(
            owner=self.user,
            name="=CMD|' /C calc'!A0",
            wallet_type=Wallet.WalletType.CASH,
            currency=usd,
            opening_balance=Decimal("100.00"),
            opening_balance_date=date(2026, 10, 1),
        )
        create_transaction_entry(
            user=self.user,
            transaction_type="expense",
            amount=Decimal("15.00"),
            wallet=wallet,
            transaction_date=date(2026, 10, 5),
            description="+SUM(A1:A10)",
            notes="@malicious",
        )

        self.client.force_login(self.user)
        csv_resp = self.client.get(f"{self.export_url}?format=csv")
        self.assertEqual(csv_resp.status_code, 200)
        csv_body = csv_resp.content.decode("utf-8")
        self.assertIn("'=CMD|' /C calc'!A0", csv_body)
        self.assertIn("'+SUM(A1:A10)", csv_body)
        self.assertIn("'@malicious", csv_body)

        json_resp = self.client.get(f"{self.export_url}?format=json")
        self.assertEqual(json_resp.status_code, 200)
        self.assertEqual(json_resp["Content-Type"], "application/json; charset=utf-8")


class TopBarScopeAndViewControlsTests(TestCase):
    def setUp(self):
        from apps.budgets.models import SharedSpace, SharedSpaceMembership

        self.password = "Str0ng!Password#2026"
        self.alice = User.objects.create_user(username="alice_topbar", password=self.password)
        self.bob = User.objects.create_user(username="bob_topbar", password=self.password)
        self.stranger = User.objects.create_user(username="stranger_topbar", password=self.password)

        self.php, _ = Currency.objects.get_or_create(
            code="PHP", defaults={"name": "Philippine Peso", "decimal_places": 2, "is_active": True}
        )
        self.usd, _ = Currency.objects.get_or_create(
            code="USD", defaults={"name": "US Dollar", "decimal_places": 2, "is_active": True}
        )
        self.eur, _ = Currency.objects.get_or_create(
            code="EUR", defaults={"name": "Euro", "decimal_places": 2, "is_active": True}
        )
        self.twd, _ = Currency.objects.get_or_create(
            code="TWD", defaults={"name": "New Taiwan Dollar", "decimal_places": 2, "is_active": True}
        )
        self.jpy, _ = Currency.objects.get_or_create(
            code="JPY", defaults={"name": "Japanese Yen", "decimal_places": 0, "is_active": True}
        )
        self.inactive_curr, _ = Currency.objects.get_or_create(
            code="ZZZ", defaults={"name": "Inactive Currency", "decimal_places": 2, "is_active": False}
        )

        self.space_a = SharedSpace.objects.create(
            name="Family Space", created_by=self.alice, base_currency=self.php
        )
        SharedSpaceMembership.objects.create(
            shared_space=self.space_a,
            user=self.alice,
            role=SharedSpaceMembership.Role.CREATOR,
            is_active=True,
        )

        self.space_b = SharedSpace.objects.create(
            name="Vacation Space", created_by=self.bob, base_currency=self.usd
        )
        SharedSpaceMembership.objects.create(
            shared_space=self.space_b,
            user=self.bob,
            role=SharedSpaceMembership.Role.CREATOR,
            is_active=True,
        )
        SharedSpaceMembership.objects.create(
            shared_space=self.space_b,
            user=self.alice,
            role=SharedSpaceMembership.Role.MEMBER,
            is_active=True,
        )

        self.private_space = SharedSpace.objects.create(
            name="Stranger Space", created_by=self.stranger, base_currency=self.eur
        )
        SharedSpaceMembership.objects.create(
            shared_space=self.private_space,
            user=self.stranger,
            role=SharedSpaceMembership.Role.CREATOR,
            is_active=True,
        )

        self.switch_scope_url = reverse("accounts:switch_scope")
        self.set_display_currency_url = reverse("accounts:set_display_currency")

    def test_view_scope_switching_between_personal_and_multiple_shared_spaces(self):
        self.client.force_login(self.alice)

        # 1. Switch to Space A
        resp = self.client.post(
            self.switch_scope_url,
            {"view_choice": f"shared:{self.space_a.id}", "next": "/"},
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        pref = UserPreference.objects.get(user=self.alice)
        self.assertEqual(pref.active_scope, "shared")
        self.assertEqual(pref.active_shared_space_id, self.space_a.id)
        self.assertContains(resp, f'value="shared:{self.space_a.id}" selected')

        # 2. Switch to Space B
        resp2 = self.client.post(
            self.switch_scope_url,
            {"view_choice": f"shared:{self.space_b.id}", "next": "/"},
            follow=True,
        )
        self.assertEqual(resp2.status_code, 200)
        pref.refresh_from_db()
        self.assertEqual(pref.active_scope, "shared")
        self.assertEqual(pref.active_shared_space_id, self.space_b.id)
        self.assertContains(resp2, f'value="shared:{self.space_b.id}" selected')

        # 3. Switch back to Personal
        resp3 = self.client.post(
            self.switch_scope_url,
            {"view_choice": "personal", "next": "/"},
            follow=True,
        )
        self.assertEqual(resp3.status_code, 200)
        pref.refresh_from_db()
        self.assertEqual(pref.active_scope, "personal")
        self.assertContains(resp3, 'value="personal" selected')

    def test_nonmembers_and_invalid_space_ids_are_rejected_without_500(self):
        self.client.force_login(self.alice)

        # Non-numeric ID
        resp_nan = self.client.post(
            self.switch_scope_url,
            {"view_choice": "shared:notanumber", "next": "/"},
            follow=True,
        )
        self.assertEqual(resp_nan.status_code, 200)
        self.assertContains(resp_nan, "Invalid shared space ID")

        # Nonexistent ID
        resp_missing = self.client.post(
            self.switch_scope_url,
            {"view_choice": "shared:00000000-0000-0000-0000-000000000000", "next": "/"},
            follow=True,
        )
        self.assertEqual(resp_missing.status_code, 200)
        self.assertContains(resp_missing, "Selected shared space was not found")

        # Nonmember space
        resp_nonmember = self.client.post(
            self.switch_scope_url,
            {"view_choice": f"shared:{self.private_space.id}", "next": "/"},
            follow=True,
        )
        self.assertEqual(resp_nonmember.status_code, 200)
        self.assertContains(resp_nonmember, "You are not a member of that shared space")

        # Unauthenticated rejected to login
        self.client.logout()
        resp_unauth = self.client.post(
            self.switch_scope_url,
            {"view_choice": "personal"},
        )
        self.assertEqual(resp_unauth.status_code, 302)
        self.assertIn("/login/", resp_unauth.url)

    def test_guidance_link_shown_when_user_has_no_shared_spaces(self):
        lonely_user = User.objects.create_user(username="lonely_user", password=self.password)
        self.client.force_login(lonely_user)

        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Join shared space")
        self.assertContains(resp, reverse("budgets:shared_space"))

    def test_display_preference_saves_only_for_authenticated_user(self):
        self.client.force_login(self.alice)

        resp = self.client.post(
            self.set_display_currency_url,
            {"currency_code": "EUR", "next": "/"},
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Display currency updated to EUR")

        pref_alice = UserPreference.objects.get(user=self.alice)
        self.assertEqual(pref_alice.display_currency, self.eur)

        # Bob's preferences must remain unaffected
        pref_bob = UserPreference.objects.filter(user=self.bob).first()
        self.assertTrue(pref_bob is None or pref_bob.display_currency != self.eur)

        # Unauthenticated request redirects to login
        self.client.logout()
        resp_unauth = self.client.post(
            self.set_display_currency_url,
            {"currency_code": "USD"},
        )
        self.assertEqual(resp_unauth.status_code, 302)
        self.assertIn("/login/", resp_unauth.url)

    def test_invalid_and_inactive_currency_ids_and_missing_csrf_are_rejected(self):
        self.client.force_login(self.alice)

        # Nonexistent currency
        resp_invalid = self.client.post(
            self.set_display_currency_url,
            {"currency_code": "INVALID", "next": "/"},
            follow=True,
        )
        self.assertEqual(resp_invalid.status_code, 200)
        self.assertContains(resp_invalid, "invalid or inactive")

        # Inactive currency
        resp_inactive = self.client.post(
            self.set_display_currency_url,
            {"currency_code": "ZZZ", "next": "/"},
            follow=True,
        )
        self.assertEqual(resp_inactive.status_code, 200)
        self.assertContains(resp_inactive, "invalid or inactive")

        # Missing CSRF token is rejected with HTTP 403
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.alice)
        resp_csrf = csrf_client.post(
            self.set_display_currency_url,
            {"currency_code": "EUR"},
        )
        self.assertEqual(resp_csrf.status_code, 403)

    def test_selected_display_currency_persists_across_pages(self):
        self.client.force_login(self.alice)

        # Set display currency to JPY
        self.client.post(
            self.set_display_currency_url,
            {"currency_code": "JPY", "next": "/"},
            follow=True,
        )

        for url in ["/", "/transactions/", "/reports/", "/wallets/"]:
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 200)
            self.assertContains(resp, 'value="JPY" selected')

    def test_existing_transaction_amounts_and_booked_conversions_remain_unchanged(self):
        from apps.budgets.models import Transaction, Wallet
        from apps.budgets.services import create_transaction_entry

        wallet_usd = Wallet.objects.create(
            owner=self.alice,
            name="Alice USD Checking",
            wallet_type=Wallet.WalletType.BANK_ACCOUNT,
            currency=self.usd,
            opening_balance=Decimal("1000.00"),
            opening_balance_date=date(2026, 10, 1),
        )

        tx = create_transaction_entry(
            user=self.alice,
            transaction_type="income",
            amount=Decimal("150.00"),
            wallet=wallet_usd,
            transaction_date=date(2026, 10, 10),
            description="Salary Payment",
        )
        initial_converted_amt = tx.converted_amount
        initial_base_curr = tx.base_currency

        self.client.force_login(self.alice)
        # Switch display currency in top bar to EUR
        self.client.post(
            self.set_display_currency_url,
            {"currency_code": "EUR", "next": "/transactions/"},
            follow=True,
        )

        # Refresh from database and verify financial record is untouched
        tx.refresh_from_db()
        self.assertEqual(tx.amount, Decimal("150.000"))
        self.assertEqual(tx.currency, self.usd)
        self.assertEqual(tx.converted_amount, initial_converted_amt)
        self.assertEqual(tx.base_currency, initial_base_curr)

    def test_old_query_parameters_do_not_defeat_topbar_display_currency_choice(self):
        self.client.force_login(self.alice)

        # Set top-bar display preference to TWD
        self.client.post(
            self.set_display_currency_url,
            {"currency_code": "TWD", "next": "/transactions/"},
            follow=True,
        )

        # Request /transactions/?display_currency=USD
        resp = self.client.get("/transactions/?display_currency=USD")
        self.assertEqual(resp.status_code, 200)
        # Table converted header and top-bar must show TWD, not USD
        self.assertContains(resp, "Converted (TWD)")
        self.assertContains(resp, 'value="TWD" selected')

        # When switching to EUR with next URL containing stale display_currency=USD
        resp_switch = self.client.post(
            self.set_display_currency_url,
            {"currency_code": "EUR", "next": "/transactions/?page=2&type=expense&display_currency=USD"},
        )
        # Redirect URL drops display_currency parameter and keeps other filters
        self.assertEqual(resp_switch.status_code, 302)
        self.assertNotIn("display_currency=", resp_switch.url)
        self.assertIn("page=2", resp_switch.url)
        self.assertIn("type=expense", resp_switch.url)

    def test_exchange_rate_table_follows_topbar_display_currency(self):
        from apps.budgets.models import ExchangeRate
        from django.utils import timezone

        now = timezone.now()
        # Seed direct USD -> EUR rate
        ExchangeRate.objects.create(
            from_currency=self.usd,
            to_currency=self.eur,
            rate=Decimal("0.92000000"),
            provider="frankfurter_v2",
            rate_timestamp=now,
            scope=ExchangeRate.Scope.PROVIDER,
        )

        self.client.force_login(self.alice)

        # 1. Alice sets display currency to USD
        self.client.post(
            self.set_display_currency_url,
            {"currency_code": "USD", "next": "/currencies/"},
            follow=True,
        )
        resp_usd = self.client.get("/currencies/")
        self.assertEqual(resp_usd.status_code, 200)
        self.assertContains(resp_usd, "Exchange rates — USD base")
        self.assertContains(resp_usd, "1 USD =")
        self.assertNotContains(resp_usd, "1 USD = <span class=\"fw-semibold\">1.00000000</span> USD")

        # 2. Switch display currency to TWD
        self.client.post(
            self.set_display_currency_url,
            {"currency_code": "TWD", "next": "/currencies/"},
            follow=True,
        )
        resp_twd = self.client.get("/currencies/")
        self.assertEqual(resp_twd.status_code, 200)
        self.assertContains(resp_twd, "Exchange rates — TWD base")
        self.assertNotContains(resp_twd, "1 TWD = <span class=\"fw-semibold\">1.00000000</span> TWD")

        # 3. Unconfigured display currency prompt
        lonely = User.objects.create_user(username="unconfigured_user", password=self.password)
        self.client.force_login(lonely)
        resp_unconf = self.client.get("/currencies/")
        self.assertEqual(resp_unconf.status_code, 200)
        self.assertContains(resp_unconf, "No display currency selected")
        self.assertContains(resp_unconf, "Please select a <strong>Display currency</strong>")

class AuthenticationPageLayoutAndAccessibilityTests(TestCase):
    def setUp(self):
        self.login_url = reverse("accounts:login")
        self.register_url = reverse("accounts:register")

    def test_login_page_renders_mockup_art_and_overlay(self):
        resp = self.client.get(self.login_url)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")

        # Mockup art and overlay layout
        self.assertIn("auth-page-container", html)
        self.assertIn("auth-card-wrapper", html)
        self.assertIn('src="/static/images/auth/jubao_auth_art.svg"', html)
        self.assertIn('alt=""', html)
        self.assertIn("auth-form-overlay", html)
        self.assertIn("auth-form-column", html)
        self.assertIn("LOG IN", html)
        self.assertIn("welcome back, gather your treasure", html)

        # Login to register switch link
        self.assertIn(f'href="{self.register_url}"', html)
        self.assertIn("New here? Sign up", html)

        # Labels tied to inputs
        self.assertIn('<label for="id_username" class="auth-field-label">', html)
        self.assertIn('<label for="id_password" class="auth-field-label">', html)
        self.assertIn('id="id_username"', html)
        self.assertIn('id="id_password"', html)

        # Autocomplete attributes and autofocus
        self.assertIn('autocomplete="username"', html)
        self.assertIn('autocomplete="current-password"', html)
        self.assertIn("autofocus", html)

        # Password toggle button
        self.assertIn('class="btn-toggle-password"', html)
        self.assertIn('data-target="id_password"', html)
        self.assertIn('aria-label="Show password"', html)
        self.assertIn('aria-pressed="false"', html)

        # Title and favicons
        self.assertIn("<title>Log in | Jubao</title>", html)
        self.assertIn('href="/static/images/favicon.ico"', html)
        self.assertIn('href="/static/images/favicon.svg"', html)

    def test_register_page_renders_mockup_art_and_structure(self):
        resp = self.client.get(self.register_url)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")

        # No isolated back link above card
        self.assertNotIn("auth-back-link", html)

        # Art and heading
        self.assertIn('src="/static/images/auth/jubao_auth_art.svg"', html)
        self.assertIn("SIGN UP", html)
        self.assertIn("to track your budget &amp; gather treasure", html)

        # Register to login switch link
        self.assertIn("Already have an account? Log in", html)

        # Labels tied to inputs
        self.assertIn('<label for="id_username" class="auth-field-label">', html)
        self.assertIn('<label for="id_password1" class="auth-field-label">', html)
        self.assertIn('<label for="id_password2" class="auth-field-label">', html)

        # Autocomplete attributes
        self.assertIn('autocomplete="username"', html)
        self.assertIn('autocomplete="new-password"', html)

        # Password toggle on password1 and password2
        self.assertIn('data-target="id_password1"', html)
        self.assertIn('data-target="id_password2"', html)

        # Title
        self.assertIn("<title>Sign up | Jubao</title>", html)

    def test_invalid_login_shows_error_message_with_role_alert(self):
        resp = self.client.post(
            self.login_url,
            {"username": "nonexistent_user", "password": "WrongPassword123!"},
        )
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")
        self.assertIn('role="alert"', html)
        self.assertIn("Invalid username or password. Please try again.", html)
