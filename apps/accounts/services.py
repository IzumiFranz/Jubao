from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from .models import AuthRateLimitAttempt, UserPreference


RATE_LIMIT_WINDOW = timedelta(minutes=15)
MAX_LOGIN_ATTEMPTS = 10
MAX_REGISTER_ATTEMPTS = 8


def get_client_identifier(request, username=""):
    ip = (request.META.get("REMOTE_ADDR") or "unknown").strip()
    normalized_user = (username or "").strip().lower()
    return f"{ip}:{normalized_user}" if normalized_user else ip


def is_rate_limited(action, identifier, max_attempts):
    cutoff = timezone.now() - RATE_LIMIT_WINDOW
    AuthRateLimitAttempt.objects.filter(created_at__lt=cutoff).delete()
    recent_count = AuthRateLimitAttempt.objects.filter(
        action=action,
        identifier=identifier,
        created_at__gte=cutoff,
    ).count()
    return recent_count >= max_attempts


def record_auth_attempt(action, identifier):
    AuthRateLimitAttempt.objects.create(action=action, identifier=identifier)


def clear_auth_attempts(action, identifier):
    AuthRateLimitAttempt.objects.filter(action=action, identifier=identifier).delete()


@transaction.atomic
def update_user_profile(*, user, display_name):
    cleaned = (display_name or "").strip()
    if not cleaned:
        raise ValidationError({"display_name": "Display name cannot be blank."})
    if len(cleaned) > 150:
        raise ValidationError({"display_name": "Display name must be 150 characters or fewer."})

    parts = cleaned.split(None, 1)
    user.first_name = parts[0]
    user.last_name = parts[1] if len(parts) > 1 else ""
    user.save(update_fields=["first_name", "last_name"])
    return user


@transaction.atomic
def update_user_preferences(*, user, timezone_name, week_starts_on):
    pref, _ = UserPreference.objects.select_for_update().get_or_create(user=user)
    pref.timezone = timezone_name
    pref.week_starts_on = int(week_starts_on)
    pref.full_clean()
    pref.save()
    return pref


@transaction.atomic
def switch_workspace_scope(*, user, scope, shared_space=None):
    from apps.budgets.models import SharedSpaceMembership

    if scope not in (
        UserPreference.ScopeChoice.PERSONAL,
        UserPreference.ScopeChoice.SHARED,
    ):
        raise ValidationError("Invalid workspace scope.")

    pref, _ = UserPreference.objects.select_for_update().get_or_create(user=user)

    if scope == UserPreference.ScopeChoice.PERSONAL:
        pref.active_scope = UserPreference.ScopeChoice.PERSONAL
        pref.save(update_fields=["active_scope", "updated_at"])
        return pref

    if shared_space is None:
        membership = (
            SharedSpaceMembership.objects.select_related("shared_space")
            .filter(user=user, is_active=True)
            .order_by("joined_at")
            .first()
        )
        if membership is None:
            raise ValidationError("Join or create a shared space before switching to Shared view.")
        shared_space = membership.shared_space
    else:
        is_member = SharedSpaceMembership.objects.filter(
            shared_space=shared_space,
            user=user,
            is_active=True,
        ).exists()
        if not is_member:
            raise PermissionDenied("You are not a member of that shared space.")

    pref.active_scope = UserPreference.ScopeChoice.SHARED
    pref.active_shared_space = shared_space
    pref.save(update_fields=["active_scope", "active_shared_space", "updated_at"])
    return pref
