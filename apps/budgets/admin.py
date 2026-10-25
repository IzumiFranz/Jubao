from django.contrib import admin

from .models import (
    Category,
    CategoryBudget,
    Currency,
    ExchangeRate,
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


@admin.register(Currency)
class CurrencyAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "decimal_places", "is_active")
    list_filter = ("is_active", "decimal_places")
    search_fields = ("code", "name")
    ordering = ("code",)


@admin.register(SharedSpace)
class SharedSpaceAdmin(admin.ModelAdmin):
    list_display = ("name", "base_currency", "created_by", "revision", "created_at")
    search_fields = ("name", "created_by__username")
    readonly_fields = ("id", "revision", "created_at", "updated_at")


@admin.register(SharedSpaceMembership)
class SharedSpaceMembershipAdmin(admin.ModelAdmin):
    list_display = ("shared_space", "user", "role", "is_active", "joined_at", "left_at")
    list_filter = ("role", "is_active")
    search_fields = ("shared_space__name", "user__username")


@admin.register(SharedSpaceInvitation)
class SharedSpaceInvitationAdmin(admin.ModelAdmin):
    list_display = ("shared_space", "created_by", "accepted_by", "status", "expires_at", "created_at")
    list_filter = ("status",)
    readonly_fields = ("id", "code", "created_at", "responded_at")


@admin.register(Wallet)
class WalletAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "owner",
        "ownership_scope",
        "shared_space",
        "wallet_type",
        "currency",
        "opening_balance",
        "opening_balance_date",
        "is_archived",
        "created_at",
    )
    list_filter = ("ownership_scope", "wallet_type", "currency", "is_archived")
    search_fields = ("name", "owner__username")
    readonly_fields = ("id", "revision", "created_at", "updated_at")
    ordering = ("name", "created_at")


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "scope", "owner", "shared_space", "kind", "is_archived")
    list_filter = ("scope", "kind", "is_archived")
    search_fields = ("name", "owner__username")
    readonly_fields = ("id", "revision", "created_at", "updated_at")


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = (
        "transaction_date",
        "transaction_type",
        "amount",
        "currency",
        "wallet",
        "category",
        "scope",
        "owner",
        "is_deleted",
    )
    list_filter = ("transaction_type", "scope", "currency", "conversion_status", "is_deleted")
    search_fields = ("description", "notes", "owner__username")
    readonly_fields = ("id", "revision", "created_at", "updated_at")


@admin.register(Transfer)
class TransferAdmin(admin.ModelAdmin):
    list_display = (
        "transfer_date",
        "source_wallet",
        "amount_sent",
        "source_currency",
        "destination_wallet",
        "amount_received",
        "destination_currency",
        "created_by",
        "is_deleted",
    )
    list_filter = ("scope", "is_deleted")
    readonly_fields = ("id", "revision", "created_at", "updated_at")


@admin.register(CategoryBudget)
class CategoryBudgetAdmin(admin.ModelAdmin):
    list_display = ("category", "period", "scope", "limit_amount", "currency", "owner", "is_archived")
    list_filter = ("scope", "currency", "is_archived")
    readonly_fields = ("id", "revision", "created_at", "updated_at")


@admin.register(SavingsGoal)
class SavingsGoalAdmin(admin.ModelAdmin):
    list_display = ("name", "scope", "target_amount", "currency", "target_date", "owner", "is_archived")
    list_filter = ("scope", "currency", "is_archived")
    readonly_fields = ("id", "revision", "created_at", "updated_at")


@admin.register(SavingsContribution)
class SavingsContributionAdmin(admin.ModelAdmin):
    list_display = ("goal", "entry_type", "amount", "contribution_date", "created_by")
    list_filter = ("entry_type",)
    readonly_fields = ("id", "created_at")


@admin.register(ExchangeRate)
class ExchangeRateAdmin(admin.ModelAdmin):
    list_display = ("from_currency", "to_currency", "rate", "provider", "is_manual", "rate_timestamp", "retrieved_at")
    list_filter = ("provider", "is_manual", "from_currency", "to_currency")
    readonly_fields = ("id", "retrieved_at")


@admin.register(SyncMutationReceipt)
class SyncMutationReceiptAdmin(admin.ModelAdmin):
    list_display = ("user", "mutation_id", "entity_type", "entity_id", "status", "created_at")
    list_filter = ("entity_type", "status")
    readonly_fields = ("created_at",)
