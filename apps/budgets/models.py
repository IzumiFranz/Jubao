from decimal import Decimal, InvalidOperation
import re
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Sum


currency_code_validator = RegexValidator(
    regex=r"^[A-Z]{3}$",
    message="Currency code must be exactly 3 uppercase letters (for example, USD).",
)

budget_period_validator = RegexValidator(
    regex=r"^\d{4}-(0[1-9]|1[0-2])$",
    message="Period must be in YYYY-MM format (for example, 2026-10).",
)


def validate_supported_decimal_places(value):
    if value not in (0, 2, 3):
        raise ValidationError(
            "Unsupported decimal precision. Supported values are 0, 2, or 3 decimal places."
        )


def validate_currency_amount(raw_val, currency, *, field_name="amount", require_positive=False):
    if raw_val is None:
        return None
    if isinstance(raw_val, float):
        raise ValidationError({field_name: "Amount must be a decimal value, not float."})
    try:
        dec_val = raw_val if isinstance(raw_val, Decimal) else Decimal(str(raw_val))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationError({field_name: "Enter a valid decimal amount."}) from exc

    if not dec_val.is_finite():
        raise ValidationError({field_name: "Amount must be a finite number."})

    if require_positive and dec_val <= Decimal("0"):
        raise ValidationError({field_name: "Amount must be greater than zero."})

    if currency is not None:
        allowed_places = currency.decimal_places
        scaled = dec_val * (Decimal(10) ** allowed_places)
        if scaled % 1 != 0:
            if allowed_places == 0:
                msg = f"{currency.code} does not allow decimal places."
            else:
                msg = (
                    f"{currency.code} supports at most {allowed_places} "
                    f"decimal place{'s' if allowed_places != 1 else ''}."
                )
            raise ValidationError({field_name: msg})
    return dec_val


def format_currency_amount(amount, currency):
    if amount is None or currency is None:
        return "—"
    quantum = Decimal(10) ** (-currency.decimal_places)
    return f"{Decimal(amount).quantize(quantum):f}"


class Currency(models.Model):
    SUPPORTED_DECIMAL_PLACES = (
        (0, "0 decimal places"),
        (2, "2 decimal places"),
        (3, "3 decimal places"),
    )

    code = models.CharField(
        max_length=3,
        unique=True,
        validators=[currency_code_validator],
        help_text="Unique three-letter uppercase ISO currency code (for example, USD, EUR, JPY).",
    )
    name = models.CharField(
        max_length=64,
        help_text="Human-readable display name for the currency.",
    )
    decimal_places = models.PositiveSmallIntegerField(
        default=2,
        choices=SUPPORTED_DECIMAL_PLACES,
        validators=[validate_supported_decimal_places],
        help_text="Minor-unit decimal precision (0, 2, or 3 decimal places).",
    )
    is_active = models.BooleanField(
        default=True,
        help_text="Whether this currency can be selected for new wallets.",
    )

    class Meta:
        ordering = ["code"]
        verbose_name_plural = "currencies"

    def clean(self):
        super().clean()
        if not isinstance(self.code, str) or not currency_code_validator.regex.match(self.code):
            raise ValidationError(
                {"code": "Currency code must be exactly 3 uppercase letters (for example, USD)."}
            )
        validate_supported_decimal_places(self.decimal_places)

    def __str__(self):
        return f"{self.code} — {self.name}"


class SharedSpace(models.Model):
    MAX_ACTIVE_MEMBERS = 2

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100)
    base_currency = models.ForeignKey(
        Currency,
        on_delete=models.PROTECT,
        related_name="shared_spaces",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_shared_spaces",
    )
    revision = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "created_at"]

    def __str__(self):
        return f"{self.name} ({self.base_currency.code})"


class SharedSpaceMembership(models.Model):
    class Role(models.TextChoices):
        CREATOR = "creator", "Creator"
        MEMBER = "member", "Member"

    shared_space = models.ForeignKey(
        SharedSpace,
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="shared_space_memberships",
    )
    role = models.CharField(
        max_length=16,
        choices=Role.choices,
        default=Role.MEMBER,
    )
    is_active = models.BooleanField(default=True)
    joined_at = models.DateTimeField(auto_now_add=True)
    left_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["joined_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["shared_space", "user"],
                name="unique_shared_space_user_membership",
            )
        ]

    def clean(self):
        super().clean()
        if self.is_active and self.shared_space_id:
            active_qs = SharedSpaceMembership.objects.filter(
                shared_space_id=self.shared_space_id,
                is_active=True,
            )
            if self.pk:
                active_qs = active_qs.exclude(pk=self.pk)
            if active_qs.count() >= SharedSpace.MAX_ACTIVE_MEMBERS:
                raise ValidationError(
                    "A shared space can have at most two active members."
                )

    def __str__(self):
        status = "Active" if self.is_active else "Former"
        return f"{self.user.username} in {self.shared_space.name} ({self.role}, {status})"


class SharedSpaceInvitation(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        ACCEPTED = "accepted", "Accepted"
        REVOKED = "revoked", "Revoked"
        EXPIRED = "expired", "Expired"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    shared_space = models.ForeignKey(
        SharedSpace,
        on_delete=models.CASCADE,
        related_name="invitations",
    )
    code = models.CharField(max_length=64, unique=True)
    partner_hint = models.CharField(max_length=100, blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_shared_invitations",
    )
    accepted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="accepted_shared_invitations",
    )
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
    )
    expires_at = models.DateTimeField()
    responded_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Invite {self.code[:8]}... ({self.get_status_display()})"


class Wallet(models.Model):
    """
    Personal or shared wallet tracking a manually entered opening balance as of a chosen date.
    `opening_balance` uses DecimalField(max_digits=18, decimal_places=3) to accommodate
    up to 15 integer digits and up to 3 minor-unit decimal places without floating-point error.
    """

    MAX_DIGITS = 18
    MAX_DECIMAL_PLACES = 3
    MAX_INTEGER_DIGITS = MAX_DIGITS - MAX_DECIMAL_PLACES

    class WalletType(models.TextChoices):
        CASH = "cash", "Cash"
        BANK_ACCOUNT = "bank_account", "Bank account"
        E_WALLET = "e_wallet", "E-wallet"
        OTHER = "other", "Other"

    class OwnershipScope(models.TextChoices):
        PERSONAL = "personal", "Personal"
        SHARED = "shared", "Shared"

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="wallets",
    )
    ownership_scope = models.CharField(
        max_length=16,
        choices=OwnershipScope.choices,
        default=OwnershipScope.PERSONAL,
    )
    shared_space = models.ForeignKey(
        SharedSpace,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="wallets",
    )
    name = models.CharField(max_length=100)
    wallet_type = models.CharField(
        max_length=20,
        choices=WalletType.choices,
    )
    currency = models.ForeignKey(
        Currency,
        on_delete=models.PROTECT,
        related_name="wallets",
    )
    opening_balance = models.DecimalField(
        max_digits=MAX_DIGITS,
        decimal_places=MAX_DECIMAL_PLACES,
    )
    opening_balance_date = models.DateField()
    is_archived = models.BooleanField(default=False)
    revision = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "created_at"]

    def clean_fields(self, exclude=None):
        exclude = set(exclude or ())
        if "opening_balance" not in exclude:
            raw_val = self.__dict__.get("opening_balance")
            if isinstance(raw_val, float):
                raise ValidationError(
                    {"opening_balance": ["Opening balance must be a decimal value, not float."]}
                )
            if isinstance(raw_val, Decimal) and not raw_val.is_finite():
                raise ValidationError(
                    {"opening_balance": ["Enter a finite decimal amount."]}
                )
        super().clean_fields(exclude=exclude)

    def clean(self):
        super().clean()
        if self.ownership_scope == self.OwnershipScope.SHARED and not self.shared_space_id:
            raise ValidationError(
                {"shared_space": "A shared space is required for a shared wallet."}
            )
        if self.ownership_scope == self.OwnershipScope.PERSONAL and self.shared_space_id:
            raise ValidationError(
                {"shared_space": "Personal wallets cannot belong to a shared space."}
            )

        raw_val = self.__dict__.get("opening_balance")
        if raw_val is not None:
            if self.currency_id and self._state.adding and not self.currency.is_active:
                raise ValidationError({"currency": "Selected currency is not active."})
            validate_currency_amount(
                raw_val,
                self.currency if self.currency_id else None,
                field_name="opening_balance",
                require_positive=False,
            )

        if not self._state.adding and self.pk:
            previous = Wallet.objects.filter(pk=self.pk).first()
            if previous is not None and previous.has_ledger_activity:
                if self.currency_id != previous.currency_id:
                    raise ValidationError(
                        {"currency": "Wallet currency cannot be changed after transactions or transfers exist."}
                    )
                if Decimal(str(self.opening_balance)) != previous.opening_balance:
                    raise ValidationError(
                        {"opening_balance": "Opening balance cannot be changed after transactions or transfers exist."}
                    )
                earliest_tx = (
                    self.transactions.filter(is_deleted=False)
                    .order_by("transaction_date")
                    .values_list("transaction_date", flat=True)
                    .first()
                )
                if earliest_tx and self.opening_balance_date > earliest_tx:
                    raise ValidationError(
                        {
                            "opening_balance_date": (
                                f"Opening-balance date cannot be later than earliest transaction ({earliest_tx})."
                            )
                        }
                    )

    @property
    def has_ledger_activity(self):
        return self.transactions.filter(is_deleted=False).exists()

    @property
    def current_balance(self):
        if hasattr(self, "_prefetched_current_balance"):
            return self._prefetched_current_balance
        active_tx = self.transactions.filter(is_deleted=False)
        inflows = (
            active_tx.filter(
                transaction_type__in=[
                    Transaction.Type.INCOME,
                    Transaction.Type.TRANSFER_IN,
                ]
            ).aggregate(total=Sum("amount"))["total"]
            or Decimal("0.000")
        )
        outflows = (
            active_tx.filter(
                transaction_type__in=[
                    Transaction.Type.EXPENSE,
                    Transaction.Type.TRANSFER_OUT,
                ]
            ).aggregate(total=Sum("amount"))["total"]
            or Decimal("0.000")
        )
        return self.opening_balance + inflows - outflows

    @property
    def formatted_opening_balance(self):
        return format_currency_amount(self.opening_balance, self.currency)

    @property
    def formatted_current_balance(self):
        return format_currency_amount(self.current_balance, self.currency)

    def __str__(self):
        return f"{self.name} ({self.currency.code})"


class Category(models.Model):
    class Kind(models.TextChoices):
        EXPENSE = "expense", "Expense"
        INCOME = "income", "Income"
        BOTH = "both", "Income & Expense"

    class Scope(models.TextChoices):
        PERSONAL = "personal", "Personal"
        SHARED = "shared", "Shared"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="categories",
    )
    scope = models.CharField(
        max_length=16,
        choices=Scope.choices,
        default=Scope.PERSONAL,
    )
    shared_space = models.ForeignKey(
        SharedSpace,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="categories",
    )
    name = models.CharField(max_length=80)
    kind = models.CharField(
        max_length=16,
        choices=Kind.choices,
        default=Kind.BOTH,
    )
    is_archived = models.BooleanField(default=False)
    revision = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "created_at"]
        verbose_name_plural = "categories"

    def clean(self):
        super().clean()
        self.name = (self.name or "").strip()
        if not self.name:
            raise ValidationError({"name": "Category name is required."})
        if self.scope == self.Scope.SHARED and not self.shared_space_id:
            raise ValidationError({"shared_space": "A shared space is required for shared categories."})
        if self.scope == self.Scope.PERSONAL and self.shared_space_id:
            raise ValidationError({"shared_space": "Personal categories cannot belong to a shared space."})

    def __str__(self):
        return f"{self.name} ({self.get_scope_display()})"


class Transfer(models.Model):
    class Scope(models.TextChoices):
        PERSONAL = "personal", "Personal"
        SHARED = "shared", "Shared"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="transfers",
    )
    scope = models.CharField(
        max_length=16,
        choices=Scope.choices,
        default=Scope.PERSONAL,
    )
    shared_space = models.ForeignKey(
        SharedSpace,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="transfers",
    )
    source_wallet = models.ForeignKey(
        Wallet,
        on_delete=models.PROTECT,
        related_name="outgoing_transfers",
    )
    destination_wallet = models.ForeignKey(
        Wallet,
        on_delete=models.PROTECT,
        related_name="incoming_transfers",
    )
    amount_sent = models.DecimalField(max_digits=18, decimal_places=3)
    source_currency = models.ForeignKey(
        Currency,
        on_delete=models.PROTECT,
        related_name="outgoing_transfers",
    )
    amount_received = models.DecimalField(max_digits=18, decimal_places=3)
    destination_currency = models.ForeignKey(
        Currency,
        on_delete=models.PROTECT,
        related_name="incoming_transfers",
    )
    effective_rate = models.DecimalField(
        max_digits=18,
        decimal_places=8,
        default=Decimal("1.00000000"),
    )
    rate_timestamp = models.DateTimeField(null=True, blank=True)
    fee_amount = models.DecimalField(
        max_digits=18,
        decimal_places=3,
        default=Decimal("0.000"),
    )
    fee_transaction = models.OneToOneField(
        "Transaction",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="fee_for_transfer",
    )
    transfer_date = models.DateField()
    notes = models.TextField(blank=True, default="")
    is_deleted = models.BooleanField(default=False)
    deleted_at = models.DateTimeField(null=True, blank=True)
    revision = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-transfer_date", "-created_at"]

    @property
    def formatted_amount_sent(self):
        return format_currency_amount(self.amount_sent, self.source_currency)

    @property
    def formatted_amount_received(self):
        return format_currency_amount(self.amount_received, self.destination_currency)

    @property
    def formatted_fee_amount(self):
        return format_currency_amount(self.fee_amount, self.source_currency)

    def __str__(self):
        return (
            f"Transfer {self.formatted_amount_sent} {self.source_currency.code} -> "
            f"{self.formatted_amount_received} {self.destination_currency.code}"
        )


class Transaction(models.Model):
    class Type(models.TextChoices):
        INCOME = "income", "Income"
        EXPENSE = "expense", "Expense"
        TRANSFER_OUT = "transfer_out", "Transfer Out"
        TRANSFER_IN = "transfer_in", "Transfer In"

    class Scope(models.TextChoices):
        PERSONAL = "personal", "Personal"
        SHARED = "shared", "Shared"

    class ConversionStatus(models.TextChoices):
        IDENTITY = "identity", "Same currency"
        CONVERTED = "converted", "Converted"
        MANUAL = "manual", "Manual rate"
        PENDING = "pending", "Conversion pending"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="transactions",
    )
    paid_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="paid_transactions",
    )
    scope = models.CharField(
        max_length=16,
        choices=Scope.choices,
        default=Scope.PERSONAL,
    )
    shared_space = models.ForeignKey(
        SharedSpace,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="transactions",
    )
    wallet = models.ForeignKey(
        Wallet,
        on_delete=models.PROTECT,
        related_name="transactions",
    )
    category = models.ForeignKey(
        Category,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="transactions",
    )
    transfer = models.ForeignKey(
        Transfer,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="legs",
    )
    transaction_type = models.CharField(
        max_length=16,
        choices=Type.choices,
    )
    amount = models.DecimalField(max_digits=18, decimal_places=3)
    currency = models.ForeignKey(
        Currency,
        on_delete=models.PROTECT,
        related_name="transactions",
    )
    base_currency = models.ForeignKey(
        Currency,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="base_converted_transactions",
    )
    converted_amount = models.DecimalField(
        max_digits=18,
        decimal_places=3,
        null=True,
        blank=True,
    )
    conversion_rate = models.DecimalField(
        max_digits=18,
        decimal_places=8,
        null=True,
        blank=True,
    )
    conversion_rate_timestamp = models.DateTimeField(null=True, blank=True)
    conversion_provider = models.CharField(max_length=64, blank=True, default="")
    conversion_status = models.CharField(
        max_length=16,
        choices=ConversionStatus.choices,
        default=ConversionStatus.PENDING,
    )
    transaction_date = models.DateField()
    description = models.CharField(max_length=200, blank=True, default="")
    notes = models.TextField(blank=True, default="")
    is_fee = models.BooleanField(default=False)
    is_deleted = models.BooleanField(default=False)
    deleted_at = models.DateTimeField(null=True, blank=True)
    revision = models.PositiveIntegerField(default=1)
    client_mutation_id = models.CharField(max_length=64, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-transaction_date", "-created_at"]

    def clean(self):
        super().clean()
        if self.wallet_id and self.currency_id and self.wallet.currency_id != self.currency_id:
            raise ValidationError(
                {"currency": "Transaction currency must match the selected wallet's currency."}
            )
        if self.currency_id:
            validate_currency_amount(
                self.__dict__.get("amount"),
                self.currency,
                field_name="amount",
                require_positive=True,
            )
        if self.wallet_id and self.transaction_date:
            if self.transaction_date < self.wallet.opening_balance_date:
                raise ValidationError(
                    {
                        "transaction_date": (
                            f"Transaction date cannot be earlier than the wallet opening-balance date "
                            f"({self.wallet.opening_balance_date})."
                        )
                    }
                )
        if self.scope == self.Scope.SHARED and not self.shared_space_id:
            raise ValidationError({"shared_space": "Shared transactions require a shared space."})
        if self.scope == self.Scope.PERSONAL and self.shared_space_id:
            raise ValidationError({"shared_space": "Personal transactions cannot belong to a shared space."})

    @property
    def formatted_amount(self):
        return format_currency_amount(self.amount, self.currency)

    @property
    def formatted_converted_amount(self):
        if self.converted_amount is None or not self.base_currency_id:
            return None
        return format_currency_amount(self.converted_amount, self.base_currency)

    def __str__(self):
        return f"{self.get_transaction_type_display()} {self.formatted_amount} {self.currency.code}"


class CategoryBudget(models.Model):
    class Scope(models.TextChoices):
        PERSONAL = "personal", "Personal"
        SHARED = "shared", "Shared"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="category_budgets",
    )
    scope = models.CharField(
        max_length=16,
        choices=Scope.choices,
        default=Scope.PERSONAL,
    )
    shared_space = models.ForeignKey(
        SharedSpace,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="category_budgets",
    )
    category = models.ForeignKey(
        Category,
        on_delete=models.PROTECT,
        related_name="budgets",
    )
    period = models.CharField(
        max_length=7,
        validators=[budget_period_validator],
    )
    currency = models.ForeignKey(
        Currency,
        on_delete=models.PROTECT,
        related_name="category_budgets",
    )
    limit_amount = models.DecimalField(max_digits=18, decimal_places=3)
    is_archived = models.BooleanField(default=False)
    revision = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-period", "category__name"]

    def clean(self):
        super().clean()
        if not self.period or not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", str(self.period)):
            raise ValidationError({"period": "Period must be in YYYY-MM format."})
        if self.currency_id:
            validate_currency_amount(
                self.__dict__.get("limit_amount"),
                self.currency,
                field_name="limit_amount",
                require_positive=True,
            )
        if self.scope == self.Scope.SHARED and not self.shared_space_id:
            raise ValidationError({"shared_space": "Shared budgets require a shared space."})
        if self.scope == self.Scope.PERSONAL and self.shared_space_id:
            raise ValidationError({"shared_space": "Personal budgets cannot belong to a shared space."})

    @property
    def formatted_limit_amount(self):
        return format_currency_amount(self.limit_amount, self.currency)

    def __str__(self):
        return f"{self.category.name} ({self.period}): {self.formatted_limit_amount} {self.currency.code}"


class SavingsGoal(models.Model):
    class Scope(models.TextChoices):
        PERSONAL = "personal", "Personal"
        SHARED = "shared", "Shared"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="savings_goals",
    )
    scope = models.CharField(
        max_length=16,
        choices=Scope.choices,
        default=Scope.PERSONAL,
    )
    shared_space = models.ForeignKey(
        SharedSpace,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="savings_goals",
    )
    name = models.CharField(max_length=120)
    currency = models.ForeignKey(
        Currency,
        on_delete=models.PROTECT,
        related_name="savings_goals",
    )
    target_amount = models.DecimalField(max_digits=18, decimal_places=3)
    target_date = models.DateField(null=True, blank=True)
    is_archived = models.BooleanField(default=False)
    revision = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "created_at"]

    def clean(self):
        super().clean()
        self.name = (self.name or "").strip()
        if not self.name:
            raise ValidationError({"name": "Goal name is required."})
        if self.currency_id:
            validate_currency_amount(
                self.__dict__.get("target_amount"),
                self.currency,
                field_name="target_amount",
                require_positive=True,
            )
        if self.scope == self.Scope.SHARED and not self.shared_space_id:
            raise ValidationError({"shared_space": "Shared savings goals require a shared space."})
        if self.scope == self.Scope.PERSONAL and self.shared_space_id:
            raise ValidationError({"shared_space": "Personal savings goals cannot belong to a shared space."})

    @property
    def saved_amount(self):
        added = (
            self.contributions.filter(
                entry_type=SavingsContribution.EntryType.CONTRIBUTION
            ).aggregate(total=Sum("amount"))["total"]
            or Decimal("0.000")
        )
        reversed_amt = (
            self.contributions.filter(
                entry_type=SavingsContribution.EntryType.REVERSAL
            ).aggregate(total=Sum("amount"))["total"]
            or Decimal("0.000")
        )
        return max(Decimal("0.000"), added - reversed_amt)

    @property
    def remaining_amount(self):
        return max(Decimal("0.000"), self.target_amount - self.saved_amount)

    @property
    def formatted_target_amount(self):
        return format_currency_amount(self.target_amount, self.currency)

    @property
    def formatted_saved_amount(self):
        return format_currency_amount(self.saved_amount, self.currency)

    @property
    def formatted_remaining_amount(self):
        return format_currency_amount(self.remaining_amount, self.currency)

    def __str__(self):
        return f"{self.name} ({self.formatted_target_amount} {self.currency.code})"


class SavingsContribution(models.Model):
    class EntryType(models.TextChoices):
        CONTRIBUTION = "contribution", "Contribution"
        REVERSAL = "reversal", "Reversal"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    goal = models.ForeignKey(
        SavingsGoal,
        on_delete=models.CASCADE,
        related_name="contributions",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="savings_contributions",
    )
    entry_type = models.CharField(
        max_length=16,
        choices=EntryType.choices,
        default=EntryType.CONTRIBUTION,
    )
    amount = models.DecimalField(max_digits=18, decimal_places=3)
    contribution_date = models.DateField()
    notes = models.CharField(max_length=200, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-contribution_date", "-created_at"]

    def clean(self):
        super().clean()
        if self.goal_id:
            validate_currency_amount(
                self.__dict__.get("amount"),
                self.goal.currency,
                field_name="amount",
                require_positive=True,
            )

    @property
    def formatted_amount(self):
        return format_currency_amount(self.amount, self.goal.currency)

    def __str__(self):
        return f"{self.get_entry_type_display()} {self.formatted_amount} {self.goal.currency.code}"


class ExchangeRate(models.Model):
    class Scope(models.TextChoices):
        PROVIDER = "provider", "Provider (Global)"
        PERSONAL = "personal", "Personal"
        SHARED = "shared", "Shared"
        LEGACY_UNSCOPED = "legacy_unscoped", "Legacy Unscoped"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    from_currency = models.ForeignKey(
        Currency,
        on_delete=models.PROTECT,
        related_name="rates_from",
    )
    to_currency = models.ForeignKey(
        Currency,
        on_delete=models.PROTECT,
        related_name="rates_to",
    )
    rate = models.DecimalField(max_digits=18, decimal_places=8)
    provider = models.CharField(max_length=64)
    is_manual = models.BooleanField(default=False)
    scope = models.CharField(
        max_length=24,
        choices=Scope.choices,
        default=Scope.PROVIDER,
    )
    shared_space = models.ForeignKey(
        SharedSpace,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="exchange_rates",
    )
    rate_timestamp = models.DateTimeField()
    retrieved_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="entered_exchange_rates",
    )

    class Meta:
        ordering = ["-rate_timestamp", "-retrieved_at"]
        indexes = [
            models.Index(fields=["from_currency", "to_currency", "-rate_timestamp"]),
            models.Index(fields=["scope", "from_currency", "to_currency", "-rate_timestamp"]),
        ]

    def clean(self):
        super().clean()
        if self.from_currency_id and self.to_currency_id and self.from_currency_id == self.to_currency_id:
            if Decimal(str(self.rate)) != Decimal("1"):
                raise ValidationError({"rate": "Same-currency exchange rate must be 1."})
        raw_rate = self.__dict__.get("rate")
        if isinstance(raw_rate, float):
            raise ValidationError({"rate": "Rate must be a decimal value, not float."})
        if raw_rate is not None:
            try:
                dec = raw_rate if isinstance(raw_rate, Decimal) else Decimal(str(raw_rate))
            except (InvalidOperation, ValueError, TypeError) as exc:
                raise ValidationError({"rate": "Enter a valid decimal rate."}) from exc
            if not dec.is_finite() or dec <= Decimal("0"):
                raise ValidationError({"rate": "Exchange rate must be a finite number greater than zero."})

        if not self.is_manual:
            if self.scope != self.Scope.PROVIDER:
                raise ValidationError({"scope": "Provider exchange rates must use provider scope."})
            if self.shared_space_id is not None:
                raise ValidationError({"shared_space": "Provider exchange rates cannot belong to a shared space."})
        else:
            if self.scope == self.Scope.PROVIDER:
                raise ValidationError({"scope": "Manual exchange rates must be scoped to Personal or Shared."})
            if self.scope == self.Scope.PERSONAL:
                if not self.created_by_id:
                    raise ValidationError({"created_by": "Personal manual exchange rates require an owner."})
                if self.shared_space_id is not None:
                    raise ValidationError({"shared_space": "Personal manual exchange rates cannot belong to a shared space."})
            elif self.scope == self.Scope.SHARED:
                if not self.created_by_id:
                    raise ValidationError({"created_by": "Shared manual exchange rates require a creator."})
                if not self.shared_space_id:
                    raise ValidationError({"shared_space": "Shared manual exchange rates require a shared space."})

    def __str__(self):
        return f"1 {self.from_currency.code} = {self.rate} {self.to_currency.code} ({self.provider}, {self.scope})"


class SyncMutationReceipt(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="sync_mutation_receipts",
    )
    mutation_id = models.CharField(max_length=64)
    request_fingerprint = models.CharField(max_length=64, default="")
    entity_type = models.CharField(max_length=32)
    entity_id = models.UUIDField()
    status = models.CharField(max_length=24)
    response_payload = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "mutation_id"],
                name="unique_user_sync_mutation_id",
            )
        ]


class PersonalTransactionShare(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    transaction = models.ForeignKey(
        "budgets.Transaction",
        on_delete=models.CASCADE,
        related_name="personal_shares",
    )
    shared_space = models.ForeignKey(
        "budgets.SharedSpace",
        on_delete=models.CASCADE,
        related_name="personal_transaction_shares",
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="granted_personal_transaction_shares",
    )
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="received_personal_transaction_shares",
    )
    include_notes = models.BooleanField(default=False)
    is_revoked = models.BooleanField(default=False)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revision = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["shared_space", "recipient", "is_revoked"]),
            models.Index(fields=["transaction", "is_revoked"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["transaction", "shared_space", "recipient"],
                condition=models.Q(is_revoked=False),
                name="unique_active_personal_transaction_share",
            )
        ]

    def clean(self):
        super().clean()
        if self.owner_id and self.recipient_id and self.owner_id == self.recipient_id:
            raise ValidationError("You cannot share a personal transaction with yourself.")

        if self.transaction_id:
            tx = self.transaction
            if tx.scope != Transaction.Scope.PERSONAL:
                raise ValidationError("Only personal transactions can be shared.")
            if tx.transaction_type not in (Transaction.Type.INCOME, Transaction.Type.EXPENSE):
                raise ValidationError("Only personal income or expense transactions can be shared.")
            if tx.transfer_id is not None or tx.is_fee:
                raise ValidationError("Transfers and transfer fees cannot be shared.")
            if tx.is_deleted:
                raise ValidationError("Deleted transactions cannot be shared.")
            if self.owner_id and tx.owner_id != self.owner_id:
                raise ValidationError("You can only share personal transactions that you own.")

        if self.shared_space_id:
            if self.owner_id:
                is_owner_member = SharedSpaceMembership.objects.filter(
                    shared_space_id=self.shared_space_id,
                    user_id=self.owner_id,
                    is_active=True,
                ).exists()
                if not is_owner_member:
                    raise ValidationError("The granting owner must be an active member of the shared space.")
            if self.recipient_id:
                is_recipient_member = SharedSpaceMembership.objects.filter(
                    shared_space_id=self.shared_space_id,
                    user_id=self.recipient_id,
                    is_active=True,
                ).exists()
                if not is_recipient_member:
                    raise ValidationError("The recipient must be an active member of the shared space.")

    def __str__(self):
        status = "revoked" if self.is_revoked else "active"
        return f"Share of tx {self.transaction_id} by {self.owner} to {self.recipient} in {self.shared_space} ({status})"


class PersonalBalanceSharePermission(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="granted_balance_share_permissions",
    )
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="received_balance_share_permissions",
    )
    shared_space = models.ForeignKey(
        "budgets.SharedSpace",
        on_delete=models.CASCADE,
        related_name="personal_balance_share_permissions",
    )
    show_balance = models.BooleanField(default=False)
    selected_wallets = models.ManyToManyField(
        "budgets.Wallet",
        blank=True,
        related_name="balance_share_permissions",
    )
    is_revoked = models.BooleanField(default=False)
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["shared_space", "owner", "recipient", "is_revoked"]),
            models.Index(fields=["shared_space", "recipient", "is_revoked"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["shared_space", "owner", "recipient"],
                condition=models.Q(is_revoked=False),
                name="unique_active_balance_share_permission",
            )
        ]

    def clean(self):
        super().clean()
        if self.owner_id and self.recipient_id and self.owner_id == self.recipient_id:
            raise ValidationError("You cannot disclose a personal balance to yourself.")

        if self.shared_space_id:
            if self.owner_id:
                is_owner_member = SharedSpaceMembership.objects.filter(
                    shared_space_id=self.shared_space_id,
                    user_id=self.owner_id,
                    is_active=True,
                ).exists()
                if not is_owner_member:
                    raise ValidationError("The granting owner must be an active member of the shared space.")
            if self.recipient_id:
                is_recipient_member = SharedSpaceMembership.objects.filter(
                    shared_space_id=self.shared_space_id,
                    user_id=self.recipient_id,
                    is_active=True,
                ).exists()
                if not is_recipient_member:
                    raise ValidationError("The recipient must be an active member of the shared space.")

    def __str__(self):
        status = "revoked" if self.is_revoked else ("on" if self.show_balance else "off")
        return f"Balance disclosure by {self.owner} to {self.recipient} in {self.shared_space} ({status})"


