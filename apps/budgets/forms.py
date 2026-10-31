from decimal import Decimal, InvalidOperation
import re

from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.accounts.models import UserPreference

from .models import (
    Category,
    CategoryBudget,
    Currency,
    SavingsContribution,
    SavingsGoal,
    SharedSpace,
    Transaction,
    Wallet,
    budget_period_validator,
    validate_currency_amount,
)


class NonRoundingDecimalField(forms.Field):
    default_error_messages = {
        "invalid": "Enter a valid decimal amount.",
        "required": "This field is required.",
    }

    def to_python(self, value):
        if value in self.empty_values:
            return None
        if isinstance(value, float):
            raise ValidationError(self.error_messages["invalid"], code="invalid")
        if isinstance(value, str):
            value = value.strip()
        try:
            dec = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise ValidationError(self.error_messages["invalid"], code="invalid") from exc
        if not dec.is_finite():
            raise ValidationError("Enter a finite decimal amount.", code="invalid")
        return dec


class GroupedDecimalField(NonRoundingDecimalField):
    """
    A NonRoundingDecimalField that validates and normalizes thousands grouping.
    Accepts standard thousands grouping with commas (e.g. 100,000.00) and un-grouped input (e.g. 100000.00).
    Rejects malformed grouping such as 10,00.00 or 1,,000 before Decimal conversion.
    Preserves exact Decimal arithmetic without converting monetary values through float.
    """

    def to_python(self, value):
        if value in self.empty_values:
            return None
        if isinstance(value, float):
            raise ValidationError(self.error_messages["invalid"], code="invalid")
        if isinstance(value, str):
            value = value.strip()
            if "," in value:
                if not re.match(r"^[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?$", value):
                    raise ValidationError(self.error_messages["invalid"], code="invalid")
                value = value.replace(",", "")
        return super().to_python(value)


class CurrencySelect(forms.Select):
    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex=subindex, attrs=attrs)
        if value and hasattr(value, "instance"):
            option["attrs"]["data-decimal-places"] = value.instance.decimal_places
        return option


class WalletSelect(forms.Select):
    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex=subindex, attrs=attrs)
        if value and hasattr(value, "instance") and getattr(value.instance, "currency", None):
            option["attrs"]["data-decimal-places"] = value.instance.currency.decimal_places
        return option


class SavingsGoalSelect(forms.Select):
    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex=subindex, attrs=attrs)
        if value and hasattr(value, "instance") and getattr(value.instance, "currency", None):
            option["attrs"]["data-decimal-places"] = value.instance.currency.decimal_places
        return option


def _check_integer_digits(amount: Decimal, max_digits: int = Wallet.MAX_INTEGER_DIGITS):
    sign, digits, exponent = amount.as_tuple()
    if not isinstance(exponent, int):
        raise ValidationError("Enter a finite decimal amount.")
    decimal_places = max(0, -exponent)
    integer_digits = max(0, len(digits) - decimal_places)
    if exponent > 0:
        integer_digits = len(digits) + exponent
    if integer_digits > max_digits:
        raise ValidationError(
            f"Ensure that there are no more than {max_digits} digits before the decimal point."
        )


class WalletForm(forms.ModelForm):
    opening_balance = GroupedDecimalField(
        label="Opening balance",
        widget=forms.TextInput(
            attrs={
                "inputmode": "decimal",
                "placeholder": "0.00",
                "autocomplete": "off",
            }
        ),
    )
    ownership_scope = forms.ChoiceField(
        choices=Wallet.OwnershipScope.choices,
        required=False,
        initial=Wallet.OwnershipScope.PERSONAL,
        label="Scope",
    )
    shared_space = forms.ModelChoiceField(
        queryset=SharedSpace.objects.none(),
        required=False,
        empty_label="Select shared space",
        label="Shared space",
    )

    class Meta:
        model = Wallet
        fields = (
            "name",
            "wallet_type",
            "currency",
            "opening_balance",
            "opening_balance_date",
            "ownership_scope",
            "shared_space",
        )
        widgets = {
            "name": forms.TextInput(
                attrs={
                    "placeholder": "For example, Everyday Checking",
                    "autocomplete": "off",
                }
            ),
            "opening_balance_date": forms.DateInput(
                attrs={
                    "type": "date",
                }
            ),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        active_currencies = Currency.objects.filter(is_active=True).order_by("code")
        self.has_active_currencies = active_currencies.exists()
        self.fields["currency"].queryset = active_currencies
        self.fields["currency"].widget = CurrencySelect()
        self.fields["currency"].empty_label = (
            "Select currency" if self.has_active_currencies else "No currencies available"
        )
        self.fields["wallet_type"].choices = [
            ("", "Select wallet type"),
            *Wallet.WalletType.choices,
        ]

        if user is not None and user.is_authenticated:
            from .selectors import get_user_active_memberships

            space_ids = get_user_active_memberships(user).values_list("shared_space_id", flat=True)
            self.fields["shared_space"].queryset = SharedSpace.objects.filter(id__in=space_ids)

        for field_name, field in self.fields.items():
            base_class = (
                "form-select form-select-sm"
                if isinstance(field.widget, forms.Select)
                else "form-control form-control-sm"
            )
            if self.is_bound and field_name in self.errors:
                base_class += " is-invalid"
            field.widget.attrs["class"] = base_class
            if not self.has_active_currencies:
                field.widget.attrs["disabled"] = True
                field.widget.attrs["aria-disabled"] = "true"

    def clean_name(self):
        name = (self.cleaned_data.get("name") or "").strip()
        if not name:
            raise ValidationError("This field is required.")
        return name

    def clean_ownership_scope(self):
        scope = (self.cleaned_data.get("ownership_scope") or "").strip()
        return scope or Wallet.OwnershipScope.PERSONAL

    def clean_opening_balance(self):
        amount = self.cleaned_data.get("opening_balance")
        if amount is None:
            return amount
        _check_integer_digits(amount)
        return amount

    def clean(self):
        cleaned_data = super().clean()
        if not Currency.objects.filter(is_active=True).exists():
            raise ValidationError(
                "No active currencies are currently available for wallet creation."
            )

        currency = cleaned_data.get("currency")
        amount = cleaned_data.get("opening_balance")
        scope = cleaned_data.get("ownership_scope") or Wallet.OwnershipScope.PERSONAL
        cleaned_data["ownership_scope"] = scope

        if scope == Wallet.OwnershipScope.PERSONAL:
            cleaned_data["shared_space"] = None
            self.instance.ownership_scope = Wallet.OwnershipScope.PERSONAL
            self.instance.shared_space = None
        else:
            shared_space = cleaned_data.get("shared_space")
            if shared_space is None and self.user is not None and self.user.is_authenticated:
                first_space = self.fields["shared_space"].queryset.first()
                if first_space is not None:
                    cleaned_data["shared_space"] = first_space
                    self.instance.shared_space = first_space
            if cleaned_data.get("shared_space") is None:
                self.add_error("shared_space", "Select a shared space for a shared wallet.")

        if currency and amount is not None:
            allowed_places = currency.decimal_places
            exponent = amount.as_tuple().exponent
            input_places = max(0, -exponent) if isinstance(exponent, int) else 0
            scaled = amount * (Decimal(10) ** allowed_places)

            if input_places > allowed_places or scaled % 1 != 0:
                if allowed_places == 0:
                    msg = f"{currency.code} does not allow decimal places."
                else:
                    msg = (
                        f"{currency.code} supports at most {allowed_places} "
                        f"decimal place{'s' if allowed_places != 1 else ''}."
                    )
                self.add_error("opening_balance", msg)

        return cleaned_data


class PersonalBaseCurrencyForm(forms.ModelForm):
    class Meta:
        model = UserPreference
        fields = ("personal_base_currency",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        active_currencies = Currency.objects.filter(is_active=True).order_by("code")
        self.has_active_currencies = active_currencies.exists()

        field = self.fields["personal_base_currency"]
        field.queryset = active_currencies
        field.required = True
        field.label = "Personal base currency"
        field.empty_label = (
            "Select currency" if self.has_active_currencies else "No currencies available"
        )
        field.widget.attrs["id"] = "personal-base-currency"

        css_class = "form-select form-select-sm"
        if self.is_bound and "personal_base_currency" in self.errors:
            css_class += " is-invalid"
        field.widget.attrs["class"] = css_class

        if not self.has_active_currencies:
            field.widget.attrs["disabled"] = True
            field.widget.attrs["aria-disabled"] = "true"

    def clean(self):
        cleaned_data = super().clean()
        if not Currency.objects.filter(is_active=True).exists():
            raise ValidationError(
                "No active currencies are currently available to save a preference."
            )
        return cleaned_data


class SharedSpaceForm(forms.Form):
    name = forms.CharField(
        max_length=100,
        label="Shared space name",
        widget=forms.TextInput(
            attrs={
                "class": "form-control form-control-sm",
                "placeholder": "For example, Household Budget",
            }
        ),
    )
    base_currency = forms.ModelChoiceField(
        queryset=Currency.objects.none(),
        empty_label="Select currency",
        label="Shared base currency",
        widget=forms.Select(attrs={"class": "form-select form-select-sm"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["base_currency"].queryset = Currency.objects.filter(is_active=True).order_by("code")


class SharedSpaceInvitationForm(forms.Form):
    partner_hint = forms.CharField(
        max_length=100,
        required=False,
        label="Partner username or note (optional)",
        widget=forms.TextInput(
            attrs={
                "class": "form-control form-control-sm",
                "placeholder": "Partner username or note",
            }
        ),
    )
    expires_in_days = forms.IntegerField(
        min_value=1,
        max_value=30,
        initial=7,
        required=False,
        label="Expires in (days)",
        widget=forms.NumberInput(attrs={"class": "form-control form-control-sm"}),
    )


class AcceptInvitationForm(forms.Form):
    code = forms.CharField(
        max_length=64,
        label="Invitation code",
        widget=forms.TextInput(
            attrs={
                "class": "form-control form-control-sm font-monospace",
                "placeholder": "Paste single-use invitation code",
                "autocomplete": "off",
            }
        ),
    )


class CategoryForm(forms.Form):
    name = forms.CharField(
        max_length=80,
        label="Category name",
        widget=forms.TextInput(
            attrs={
                "class": "form-control form-control-sm",
                "placeholder": "For example, Groceries",
            }
        ),
    )
    kind = forms.ChoiceField(
        choices=Category.Kind.choices,
        initial=Category.Kind.EXPENSE,
        label="Type",
        widget=forms.Select(attrs={"class": "form-select form-select-sm"}),
    )
    scope = forms.ChoiceField(
        choices=Category.Scope.choices,
        initial=Category.Scope.PERSONAL,
        required=False,
        label="Scope",
        widget=forms.Select(attrs={"class": "form-select form-select-sm"}),
    )


class TransactionEntryForm(forms.Form):
    transaction_type = forms.ChoiceField(
        choices=[
            (Transaction.Type.EXPENSE, "Expense"),
            (Transaction.Type.INCOME, "Income"),
        ],
        initial=Transaction.Type.EXPENSE,
        label="Type",
        widget=forms.Select(attrs={"class": "form-select form-select-sm", "id": "tx-type"}),
    )
    amount = GroupedDecimalField(
        label="Amount",
        widget=forms.TextInput(
            attrs={
                "class": "form-control form-control-sm",
                "id": "tx-amount",
                "inputmode": "decimal",
                "placeholder": "0.00",
                "autocomplete": "off",
            }
        ),
    )
    wallet = forms.ModelChoiceField(
        queryset=Wallet.objects.none(),
        empty_label="Select wallet",
        label="Wallet",
        widget=WalletSelect(attrs={"class": "form-select form-select-sm", "id": "tx-wallet"}),
    )
    category = forms.ModelChoiceField(
        queryset=Category.objects.none(),
        required=False,
        empty_label="Uncategorized",
        label="Category",
        widget=forms.Select(attrs={"class": "form-select form-select-sm", "id": "tx-category"}),
    )
    transaction_date = forms.DateField(
        label="Date",
        initial=timezone.localdate,
        widget=forms.DateInput(
            attrs={"type": "date", "class": "form-control form-control-sm", "id": "tx-date"}
        ),
    )
    scope = forms.ChoiceField(
        choices=Transaction.Scope.choices,
        required=False,
        initial=Transaction.Scope.PERSONAL,
        label="Scope",
        widget=forms.Select(attrs={"class": "form-select form-select-sm", "id": "tx-scope"}),
    )
    description = forms.CharField(
        max_length=200,
        required=False,
        label="Description",
        widget=forms.TextInput(
            attrs={
                "class": "form-control form-control-sm",
                "id": "tx-description",
                "placeholder": "What was this transaction for?",
            }
        ),
    )
    notes = forms.CharField(
        required=False,
        label="Notes",
        widget=forms.Textarea(
            attrs={
                "class": "form-control form-control-sm",
                "id": "tx-notes",
                "rows": 2,
                "placeholder": "Optional details",
            }
        ),
    )
    manual_rate = NonRoundingDecimalField(
        required=False,
        label="Manual exchange rate (optional)",
        widget=forms.TextInput(
            attrs={
                "class": "form-control form-control-sm",
                "id": "tx-manual-rate",
                "inputmode": "decimal",
                "placeholder": "Optional override rate to base currency",
            }
        ),
    )
    paid_by = forms.ModelChoiceField(
        queryset=UserPreference._meta.get_field("user").related_model.objects.none(),
        required=False,
        empty_label="Current user (default)",
        label="Paid by (shared)",
        widget=forms.Select(attrs={"class": "form-select form-select-sm", "id": "tx-paid-by"}),
    )

    def __init__(self, *args, user=None, include_archived: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        if user is not None and user.is_authenticated:
            from django.contrib.auth import get_user_model
            from .models import SharedSpaceMembership
            from .selectors import (
                get_accessible_categories_qs,
                get_accessible_wallets_qs,
                get_user_active_memberships,
            )

            self.fields["wallet"].queryset = get_accessible_wallets_qs(
                user, include_archived=include_archived
            )
            self.fields["category"].queryset = get_accessible_categories_qs(
                user, include_archived=include_archived
            )
            space_ids = list(
                get_user_active_memberships(user).values_list("shared_space_id", flat=True)
            )
            member_user_ids = set(
                SharedSpaceMembership.objects.filter(
                    shared_space_id__in=space_ids, is_active=True
                ).values_list("user_id", flat=True)
            )
            member_user_ids.add(user.pk)
            User = get_user_model()
            self.fields["paid_by"].queryset = User.objects.filter(
                pk__in=member_user_ids
            ).order_by("username")

    def clean_amount(self):
        amt = self.cleaned_data.get("amount")
        if amt is not None:
            _check_integer_digits(amt)
            if amt <= Decimal("0"):
                raise ValidationError("Amount must be greater than zero.")
        return amt

    def clean(self):
        cleaned = super().clean()
        wallet = cleaned.get("wallet")
        amt = cleaned.get("amount")
        if wallet and amt is not None:
            try:
                validate_currency_amount(
                    amt, wallet.currency, field_name="amount", require_positive=True
                )
            except ValidationError as exc:
                for field, messages_list in exc.message_dict.items():
                    for m in messages_list:
                        self.add_error(field, m)
        return cleaned


class TransferEntryForm(forms.Form):
    source_wallet = forms.ModelChoiceField(
        queryset=Wallet.objects.none(),
        empty_label="Select source wallet",
        label="From wallet",
        widget=WalletSelect(attrs={"class": "form-select form-select-sm"}),
    )
    destination_wallet = forms.ModelChoiceField(
        queryset=Wallet.objects.none(),
        empty_label="Select destination wallet",
        label="To wallet",
        widget=WalletSelect(attrs={"class": "form-select form-select-sm"}),
    )
    amount_sent = GroupedDecimalField(
        label="Amount sent",
        widget=forms.TextInput(
            attrs={
                "class": "form-control form-control-sm",
                "inputmode": "decimal",
                "placeholder": "0.00",
            }
        ),
    )
    amount_received = GroupedDecimalField(
        required=False,
        label="Amount received (cross-currency)",
        widget=forms.TextInput(
            attrs={
                "class": "form-control form-control-sm",
                "inputmode": "decimal",
                "placeholder": "Same as sent if same currency",
            }
        ),
    )
    manual_rate = NonRoundingDecimalField(
        required=False,
        label="Exchange rate (optional if received amount entered)",
        widget=forms.TextInput(
            attrs={
                "class": "form-control form-control-sm",
                "inputmode": "decimal",
                "placeholder": "e.g. 56.25",
            }
        ),
    )
    fee_amount = GroupedDecimalField(
        required=False,
        label="Transfer fee (optional, in source currency)",
        widget=forms.TextInput(
            attrs={
                "class": "form-control form-control-sm",
                "inputmode": "decimal",
                "placeholder": "0.00",
            }
        ),
    )
    transfer_date = forms.DateField(
        label="Transfer date",
        initial=timezone.localdate,
        widget=forms.DateInput(attrs={"type": "date", "class": "form-control form-control-sm"}),
    )
    notes = forms.CharField(
        required=False,
        label="Notes",
        widget=forms.TextInput(
            attrs={
                "class": "form-control form-control-sm",
                "placeholder": "Optional transfer memo",
            }
        ),
    )

    def __init__(self, *args, user=None, include_archived: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        if user is not None and user.is_authenticated:
            from .selectors import get_accessible_wallets_qs

            qs = get_accessible_wallets_qs(user, include_archived=include_archived)
            self.fields["source_wallet"].queryset = qs
            self.fields["destination_wallet"].queryset = qs

    def clean_amount_sent(self):
        amt = self.cleaned_data.get("amount_sent")
        if amt is not None:
            _check_integer_digits(amt)
            if amt <= Decimal("0"):
                raise ValidationError("Amount must be greater than zero.")
        return amt

    def clean_amount_received(self):
        amt = self.cleaned_data.get("amount_received")
        if amt is not None:
            _check_integer_digits(amt)
            if amt <= Decimal("0"):
                raise ValidationError("Amount must be greater than zero.")
        return amt

    def clean_fee_amount(self):
        amt = self.cleaned_data.get("fee_amount")
        if amt is not None:
            _check_integer_digits(amt)
            if amt < Decimal("0"):
                raise ValidationError("Fee cannot be negative.")
        return amt

    def clean(self):
        cleaned = super().clean()
        source_wallet = cleaned.get("source_wallet")
        dest_wallet = cleaned.get("destination_wallet")
        amount_sent = cleaned.get("amount_sent")
        amount_received = cleaned.get("amount_received")
        fee_amount = cleaned.get("fee_amount")

        if source_wallet and amount_sent is not None:
            try:
                validate_currency_amount(
                    amount_sent, source_wallet.currency, field_name="amount_sent", require_positive=True
                )
            except ValidationError as exc:
                for field, msgs in exc.message_dict.items():
                    for m in msgs:
                        self.add_error(field, m)

        if dest_wallet and amount_received is not None:
            try:
                validate_currency_amount(
                    amount_received, dest_wallet.currency, field_name="amount_received", require_positive=True
                )
            except ValidationError as exc:
                for field, msgs in exc.message_dict.items():
                    for m in msgs:
                        self.add_error(field, m)

        if source_wallet and fee_amount is not None:
            try:
                validate_currency_amount(
                    fee_amount, source_wallet.currency, field_name="fee_amount", require_positive=False
                )
            except ValidationError as exc:
                for field, msgs in exc.message_dict.items():
                    for m in msgs:
                        self.add_error(field, m)

        return cleaned


class CategoryBudgetForm(forms.Form):
    category = forms.ModelChoiceField(
        queryset=Category.objects.none(),
        empty_label="Select category",
        label="Category",
        widget=forms.Select(attrs={"class": "form-select form-select-sm"}),
    )
    period = forms.CharField(
        max_length=7,
        validators=[budget_period_validator],
        label="Period (YYYY-MM)",
        widget=forms.TextInput(
            attrs={
                "type": "month",
                "class": "form-control form-control-sm",
            }
        ),
    )
    currency = forms.ModelChoiceField(
        queryset=Currency.objects.none(),
        empty_label="Select currency",
        label="Currency",
        widget=CurrencySelect(attrs={"class": "form-select form-select-sm"}),
    )
    limit_amount = GroupedDecimalField(
        label="Monthly limit",
        widget=forms.TextInput(
            attrs={
                "class": "form-control form-control-sm",
                "inputmode": "decimal",
                "placeholder": "0.00",
            }
        ),
    )
    scope = forms.ChoiceField(
        choices=CategoryBudget.Scope.choices,
        required=False,
        initial=CategoryBudget.Scope.PERSONAL,
        label="Scope",
        widget=forms.Select(attrs={"class": "form-select form-select-sm"}),
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["currency"].queryset = Currency.objects.filter(is_active=True).order_by("code")
        if not self.initial.get("period"):
            self.initial["period"] = timezone.localdate().strftime("%Y-%m")
        if user is not None and user.is_authenticated:
            from .selectors import get_accessible_categories_qs

            self.fields["category"].queryset = get_accessible_categories_qs(
                user, kind=Category.Kind.EXPENSE, include_archived=False
            )

    def clean_limit_amount(self):
        amt = self.cleaned_data.get("limit_amount")
        if amt is not None:
            _check_integer_digits(amt)
            if amt <= Decimal("0"):
                raise ValidationError("Limit amount must be greater than zero.")
        return amt

    def clean(self):
        cleaned = super().clean()
        currency = cleaned.get("currency")
        limit_amount = cleaned.get("limit_amount")
        if currency and limit_amount is not None:
            try:
                validate_currency_amount(
                    limit_amount, currency, field_name="limit_amount", require_positive=True
                )
            except ValidationError as exc:
                for field, msgs in exc.message_dict.items():
                    for m in msgs:
                        self.add_error(field, m)
        return cleaned


class SavingsGoalForm(forms.Form):
    name = forms.CharField(
        max_length=120,
        label="Goal name",
        widget=forms.TextInput(
            attrs={
                "class": "form-control form-control-sm",
                "placeholder": "For example, Emergency Fund",
            }
        ),
    )
    currency = forms.ModelChoiceField(
        queryset=Currency.objects.none(),
        empty_label="Select currency",
        label="Currency",
        widget=CurrencySelect(attrs={"class": "form-select form-select-sm"}),
    )
    target_amount = GroupedDecimalField(
        label="Target amount",
        widget=forms.TextInput(
            attrs={
                "class": "form-control form-control-sm",
                "inputmode": "decimal",
                "placeholder": "0.00",
            }
        ),
    )
    target_date = forms.DateField(
        required=False,
        label="Target date (optional)",
        widget=forms.DateInput(attrs={"type": "date", "class": "form-control form-control-sm"}),
    )
    scope = forms.ChoiceField(
        choices=SavingsGoal.Scope.choices,
        required=False,
        initial=SavingsGoal.Scope.PERSONAL,
        label="Scope",
        widget=forms.Select(attrs={"class": "form-select form-select-sm"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["currency"].queryset = Currency.objects.filter(is_active=True).order_by("code")

    def clean(self):
        cleaned = super().clean()
        currency = cleaned.get("currency")
        target_amount = cleaned.get("target_amount")
        if currency and target_amount is not None:
            try:
                validate_currency_amount(
                    target_amount, currency, field_name="target_amount", require_positive=True
                )
            except ValidationError as exc:
                for field, messages_list in exc.message_dict.items():
                    for m in messages_list:
                        self.add_error(field, m)
        return cleaned


class SavingsContributionForm(forms.Form):
    goal = forms.ModelChoiceField(
        queryset=SavingsGoal.objects.none(),
        label="Savings goal",
        widget=SavingsGoalSelect(attrs={"class": "form-select form-select-sm"}),
    )
    entry_type = forms.ChoiceField(
        choices=SavingsContribution.EntryType.choices,
        initial=SavingsContribution.EntryType.CONTRIBUTION,
        label="Entry type",
        widget=forms.Select(attrs={"class": "form-select form-select-sm"}),
    )
    amount = GroupedDecimalField(
        label="Amount",
        widget=forms.TextInput(
            attrs={
                "class": "form-control form-control-sm",
                "inputmode": "decimal",
                "placeholder": "0.00",
            }
        ),
    )
    contribution_date = forms.DateField(
        initial=timezone.localdate,
        label="Date",
        widget=forms.DateInput(attrs={"type": "date", "class": "form-control form-control-sm"}),
    )
    notes = forms.CharField(
        max_length=200,
        required=False,
        label="Notes",
        widget=forms.TextInput(
            attrs={
                "class": "form-control form-control-sm",
                "placeholder": "Optional note",
            }
        ),
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        if user is not None and user.is_authenticated:
            from .selectors import get_user_active_memberships

            space_ids = get_user_active_memberships(user).values_list("shared_space_id", flat=True)
            from django.db.models import Q

            self.fields["goal"].queryset = SavingsGoal.objects.filter(
                Q(scope=SavingsGoal.Scope.PERSONAL, owner=user)
                | Q(scope=SavingsGoal.Scope.SHARED, shared_space_id__in=space_ids),
                is_archived=False,
            ).select_related("currency")

    def clean_amount(self):
        amt = self.cleaned_data.get("amount")
        if amt is not None:
            _check_integer_digits(amt)
            if amt <= Decimal("0"):
                raise ValidationError("Amount must be greater than zero.")
        return amt

    def clean(self):
        cleaned = super().clean()
        goal = cleaned.get("goal")
        amount = cleaned.get("amount")
        if goal and amount is not None:
            try:
                validate_currency_amount(
                    amount, goal.currency, field_name="amount", require_positive=True
                )
            except ValidationError as exc:
                for field, msgs in exc.message_dict.items():
                    for m in msgs:
                        self.add_error(field, m)
        return cleaned


class ManualExchangeRateForm(forms.Form):
    from_currency = forms.ModelChoiceField(
        queryset=Currency.objects.none(),
        empty_label="Select currency",
        label="From currency",
        widget=forms.Select(attrs={"class": "form-select form-select-sm"}),
    )
    to_currency = forms.ModelChoiceField(
        queryset=Currency.objects.none(),
        empty_label="Select currency",
        label="To currency",
        widget=forms.Select(attrs={"class": "form-select form-select-sm"}),
    )
    rate = NonRoundingDecimalField(
        label="Exchange rate (1 From = X To)",
        widget=forms.TextInput(
            attrs={
                "class": "form-control form-control-sm",
                "inputmode": "decimal",
                "placeholder": "e.g. 56.25000000",
            }
        ),
    )
    rate_date = forms.DateField(
        required=False,
        initial=timezone.localdate,
        label="Effective date",
        widget=forms.DateInput(attrs={"type": "date", "class": "form-control form-control-sm"}),
    )
    scope = forms.ChoiceField(
        choices=[
            ("personal", "Personal"),
            ("shared", "Shared"),
        ],
        required=False,
        initial="personal",
        label="Rate scope",
        widget=forms.Select(attrs={"class": "form-select form-select-sm"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        qs = Currency.objects.filter(is_active=True).order_by("code")
        self.fields["from_currency"].queryset = qs
        self.fields["to_currency"].queryset = qs


class CurrencyConverterForm(forms.Form):
    amount = GroupedDecimalField(
        label="Amount",
        widget=forms.TextInput(
            attrs={
                "id": "converter-amount",
                "class": "form-control form-control-sm",
                "inputmode": "decimal",
                "placeholder": "0.00",
            }
        ),
    )
    from_currency = forms.ModelChoiceField(
        queryset=Currency.objects.none(),
        empty_label="Select currency",
        label="From currency",
        widget=CurrencySelect(
            attrs={"id": "converter-from", "class": "form-select form-select-sm"}
        ),
    )
    to_currency = forms.ModelChoiceField(
        queryset=Currency.objects.none(),
        empty_label="Select currency",
        label="To currency",
        widget=CurrencySelect(
            attrs={"id": "converter-to", "class": "form-select form-select-sm"}
        ),
    )
    scope = forms.ChoiceField(
        choices=[
            ("personal", "Personal"),
            ("shared", "Shared"),
        ],
        required=False,
        initial="personal",
        label="Scope",
        widget=forms.Select(
            attrs={"id": "converter-scope", "class": "form-select form-select-sm"}
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        qs = Currency.objects.filter(is_active=True).order_by("code")
        self.fields["from_currency"].queryset = qs
        self.fields["to_currency"].queryset = qs

    def clean_amount(self):
        amt = self.cleaned_data.get("amount")
        if amt is not None:
            _check_integer_digits(amt)
            if amt <= Decimal("0"):
                raise ValidationError("Amount must be greater than zero.")
        return amt

    def clean(self):
        cleaned = super().clean()
        from_curr = cleaned.get("from_currency")
        amount = cleaned.get("amount")
        if from_curr and amount is not None:
            try:
                validate_currency_amount(
                    amount, from_curr, field_name="amount", require_positive=True
                )
            except ValidationError as exc:
                for field, msgs in exc.message_dict.items():
                    for m in msgs:
                        self.add_error(field, m)
        return cleaned
