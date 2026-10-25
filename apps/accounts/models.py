from zoneinfo import available_timezones

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


COMMON_TIMEZONES = (
    "UTC",
    "Asia/Manila",
    "Asia/Taipei",
    "Asia/Tokyo",
    "Asia/Kuwait",
    "Europe/London",
    "Europe/Berlin",
    "America/New_York",
    "America/Chicago",
    "America/Los_Angeles",
)


def validate_timezone_name(value):
    valid_zones = available_timezones() | set(COMMON_TIMEZONES)
    if value not in valid_zones:
        raise ValidationError("Select a valid IANA time zone.")


class UserPreference(models.Model):
    class ScopeChoice(models.TextChoices):
        PERSONAL = "personal", "Personal"
        SHARED = "shared", "Shared"

    class WeekStartChoice(models.IntegerChoices):
        MONDAY = 0, "Monday"
        SATURDAY = 5, "Saturday"
        SUNDAY = 6, "Sunday"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="preference",
    )
    personal_base_currency = models.ForeignKey(
        "budgets.Currency",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="preferred_by_users",
    )
    display_currency = models.ForeignKey(
        "budgets.Currency",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="preferred_display_by_users",
    )
    timezone = models.CharField(
        max_length=64,
        default="UTC",
        validators=[validate_timezone_name],
    )
    week_starts_on = models.PositiveSmallIntegerField(
        choices=WeekStartChoice.choices,
        default=WeekStartChoice.MONDAY,
    )
    active_scope = models.CharField(
        max_length=16,
        choices=ScopeChoice.choices,
        default=ScopeChoice.PERSONAL,
    )
    active_shared_space = models.ForeignKey(
        "budgets.SharedSpace",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="active_for_users",
    )
    last_sync_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["user__username"]
        verbose_name = "user preference"
        verbose_name_plural = "user preferences"

    def __str__(self):
        currency_code = (
            self.personal_base_currency.code
            if self.personal_base_currency_id
            else "Unconfigured"
        )
        return f"{self.user.username} ({currency_code})"


class AuthRateLimitAttempt(models.Model):
    action = models.CharField(max_length=32)
    identifier = models.CharField(max_length=128)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["action", "identifier", "created_at"]),
        ]
