from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
import logging
import re
import secrets
import uuid

import segno
from django.core.cache import cache
from django.db.models import Q

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, models, transaction
from django.utils import timezone

from apps.accounts.models import UserPreference

from .exchange_rates import (
    RATE_QUANTUM,
    convert_decimal_amount,
    get_latest_rate,
)
from .models import (
    Category,
    CategoryBudget,
    Currency,
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
    format_currency_amount,
    validate_currency_amount,
)


class ConflictError(ValidationError):
    pass


MAX_SYNC_BATCH_SIZE = 100
FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")


def sanitize_spreadsheet_cell(value) -> str:
    if value is None:
        return ""
    text = str(value)
    if not text:
        return ""
    stripped = text.lstrip(" ")
    if text.startswith(FORMULA_TRIGGERS) or stripped.startswith(FORMULA_TRIGGERS):
        return f"'{text}"
    return text


def parse_positive_revision(raw_rev, *, required: bool = False) -> int | None:
    if raw_rev is None or (isinstance(raw_rev, str) and raw_rev.strip() == ""):
        if required:
            raise ValidationError("A valid positive integer base_revision is required.")
        return None
    if isinstance(raw_rev, bool) or isinstance(raw_rev, float) or isinstance(raw_rev, (dict, list)):
        raise ValidationError("Revision must be a positive integer.")
    if isinstance(raw_rev, int):
        rev = raw_rev
    elif isinstance(raw_rev, str):
        cleaned = raw_rev.strip()
        if not cleaned.isdigit():
            raise ValidationError("Revision must be a positive integer.")
        rev = int(cleaned)
    else:
        raise ValidationError("Revision must be a positive integer.")
    if rev < 1:
        raise ValidationError("Revision must be a positive integer greater than or equal to 1.")
    return rev


def _compute_mutation_fingerprint(op: str, entity_id_str: str, base_revision, payload: dict) -> str:
    canonical = json.dumps(
        {
            "op": op,
            "entity_id": entity_id_str,
            "base_revision": base_revision,
            "payload": payload,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def is_active_space_member(user, shared_space: SharedSpace) -> bool:
    if not user or not user.is_authenticated or shared_space is None:
        return False
    return SharedSpaceMembership.objects.filter(
        shared_space=shared_space,
        user=user,
        is_active=True,
    ).exists()


def assert_space_member(user, shared_space: SharedSpace) -> SharedSpaceMembership:
    if not user or not user.is_authenticated or shared_space is None:
        raise PermissionDenied("Shared space access requires membership.")
    membership = SharedSpaceMembership.objects.filter(
        shared_space=shared_space,
        user=user,
        is_active=True,
    ).first()
    if membership is None:
        raise PermissionDenied("You do not have access to this shared space.")
    return membership


def assert_space_creator(user, shared_space: SharedSpace) -> SharedSpaceMembership:
    membership = assert_space_member(user, shared_space)
    if shared_space.created_by_id != user.id or membership.role != SharedSpaceMembership.Role.CREATOR:
        raise PermissionDenied("Only the shared-space creator can perform this action.")
    return membership


def resolve_scope_and_space(user, scope: str, shared_space: SharedSpace | None = None):
    if scope not in ("personal", "shared"):
        raise ValidationError({"scope": "Select a valid scope (Personal or Shared)."})
    if scope == "personal":
        return "personal", None
    if shared_space is None:
        membership = (
            SharedSpaceMembership.objects.select_related("shared_space")
            .filter(user=user, is_active=True)
            .order_by("joined_at")
            .first()
        )
        if membership is None:
            raise PermissionDenied("You must belong to an active shared space to use Shared scope.")
        shared_space = membership.shared_space
    else:
        assert_space_member(user, shared_space)
    return "shared", shared_space


def assert_wallet_access(user, wallet: Wallet, *, expected_scope: str | None = None, shared_space: SharedSpace | None = None):
    if wallet.ownership_scope == Wallet.OwnershipScope.PERSONAL:
        if wallet.owner_id != user.id:
            raise PermissionDenied("You do not have access to this personal wallet.")
        if expected_scope == "shared":
            raise ValidationError({"wallet": "Select a shared wallet for shared entries."})
    else:
        assert_space_member(user, wallet.shared_space)
        if expected_scope == "personal":
            raise ValidationError({"wallet": "Select a personal wallet for personal entries."})
        if shared_space is not None and wallet.shared_space_id != shared_space.id:
            raise PermissionDenied("Wallet belongs to a different shared space.")


def assert_category_access(user, category: Category, *, expected_scope: str | None = None, shared_space: SharedSpace | None = None):
    if category.scope == Category.Scope.PERSONAL:
        if category.owner_id != user.id:
            raise PermissionDenied("You do not have access to this personal category.")
        if expected_scope == "shared":
            raise ValidationError({"category": "Select a shared category for shared entries."})
    else:
        assert_space_member(user, category.shared_space)
        if expected_scope == "personal":
            raise ValidationError({"category": "Select a personal category for personal entries."})
        if shared_space is not None and category.shared_space_id != shared_space.id:
            raise PermissionDenied("Category belongs to a different shared space.")


@transaction.atomic
def create_shared_space(*, user, name: str, base_currency: Currency) -> SharedSpace:
    cleaned_name = (name or "").strip()
    if not cleaned_name:
        raise ValidationError({"name": "Shared space name is required."})
    if not base_currency or not base_currency.is_active:
        raise ValidationError({"base_currency": "Select an active base currency."})

    space = SharedSpace(
        name=cleaned_name,
        base_currency=base_currency,
        created_by=user,
    )
    space.full_clean()
    space.save()

    membership = SharedSpaceMembership(
        shared_space=space,
        user=user,
        role=SharedSpaceMembership.Role.CREATOR,
        is_active=True,
    )
    membership.full_clean()
    membership.save()
    return space


@transaction.atomic
def update_shared_space(*, user, shared_space: SharedSpace, name: str, base_currency: Currency) -> SharedSpace:
    assert_space_creator(user, shared_space)
    cleaned_name = (name or "").strip()
    if not cleaned_name:
        raise ValidationError({"name": "Shared space name is required."})
    if not base_currency or not base_currency.is_active:
        raise ValidationError({"base_currency": "Select an active base currency."})

    shared_space.name = cleaned_name
    shared_space.base_currency = base_currency
    shared_space.revision += 1
    shared_space.full_clean()
    shared_space.save()
    return shared_space


logger = logging.getLogger(__name__)

INVITATION_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"
MAX_ACTIVE_INVITATIONS_PER_OWNER = 5
INVITATION_ERROR_MESSAGE = "This code is invalid or has expired."
INVITATION_RATE_LIMIT_MAX_ATTEMPTS = 5
INVITATION_RATE_LIMIT_WINDOW = 600  # 10 minutes
INVITATION_LOCKOUT_DURATION = 900  # 15 minutes
INVITATION_LOCKOUT_MESSAGE = "Too many failed attempts. Please wait 15 minutes before trying again."


def generate_invitation_code() -> str:
    now = timezone.now()
    for _ in range(50):
        code = "".join(secrets.choice(INVITATION_ALPHABET) for _ in range(6))
        # Unique across table
        if not SharedSpaceInvitation.objects.filter(code=code).exists():
            return code
    raise ValidationError("Could not generate a unique invitation code. Please try again.")


def normalize_invitation_code(raw_code: str) -> str:
    if not raw_code:
        return ""
    cleaned = str(raw_code).strip()
    compact = re.sub(r"[\s\-]", "", cleaned)
    if len(compact) == 6:
        return compact.upper()
    return cleaned


def get_client_ip(request) -> str:
    if not request:
        return "unknown"
    x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if x_forwarded_for:
        return x_forwarded_for.split(",")[0].strip()
    return (request.META.get("REMOTE_ADDR") or "unknown").strip()


def check_invitation_rate_limit(request) -> tuple[bool, str]:
    if not request:
        return False, ""
    user = getattr(request, "user", None)
    user_id = str(user.id) if user and user.is_authenticated else None
    ip = get_client_ip(request)
    if user_id and cache.get(f"inv_lock_user_{user_id}"):
        return True, INVITATION_LOCKOUT_MESSAGE
    if ip and ip != "unknown" and cache.get(f"inv_lock_ip_{ip}"):
        return True, INVITATION_LOCKOUT_MESSAGE
    return False, ""


def record_invitation_failure(request, raw_code: str = "") -> None:
    if not request:
        return
    user = getattr(request, "user", None)
    user_id = str(user.id) if user and user.is_authenticated else "anon"
    ip = get_client_ip(request)
    raw_str = str(raw_code or "").strip()
    masked_code = (raw_str[:2] + "****") if len(raw_str) >= 2 else "****"
    logger.warning(
        "Failed invitation code redemption attempt from user %s, IP %s (code prefix: %s)",
        user_id,
        ip,
        masked_code,
    )
    keys_to_update = []
    if user_id != "anon":
        keys_to_update.append((f"inv_fail_user_{user_id}", f"inv_lock_user_{user_id}"))
    if ip != "unknown":
        keys_to_update.append((f"inv_fail_ip_{ip}", f"inv_lock_ip_{ip}"))

    for fail_key, lock_key in keys_to_update:
        try:
            attempts = (cache.get(fail_key) or 0) + 1
            cache.set(fail_key, attempts, timeout=INVITATION_RATE_LIMIT_WINDOW)
            if attempts >= INVITATION_RATE_LIMIT_MAX_ATTEMPTS:
                cache.set(lock_key, True, timeout=INVITATION_LOCKOUT_DURATION)
        except Exception:
            pass


def clear_invitation_rate_limit(request) -> None:
    if not request:
        return
    user = getattr(request, "user", None)
    user_id = str(user.id) if user and user.is_authenticated else None
    ip = get_client_ip(request)
    if user_id:
        cache.delete(f"inv_fail_user_{user_id}")
        cache.delete(f"inv_lock_user_{user_id}")
    if ip and ip != "unknown":
        cache.delete(f"inv_fail_ip_{ip}")
        cache.delete(f"inv_lock_ip_{ip}")


def generate_invitation_qr_svg(join_url: str, owner_name: str) -> str:
    qr = segno.make(join_url, error="m")
    # scale=6 with border=4 gives 246px which is >= 200px
    svg = qr.svg_inline(border=4, scale=6, dark="#1E3A5F", light="#FFFFFF")
    aria_text = f"QR code to join {owner_name}'s shared space"
    return svg.replace("<svg ", f'<svg role="img" aria-label="{aria_text}" style="max-width: 100%; height: auto;" ')


@transaction.atomic
def create_shared_space_invitation(
    *,
    user,
    shared_space: SharedSpace,
    partner_hint: str = "",
    expiry: str = "24h",
    expires_in_days: int | None = None,
) -> SharedSpaceInvitation:
    assert_space_creator(user, shared_space)
    active_count = SharedSpaceMembership.objects.select_for_update().filter(
        shared_space=shared_space,
        is_active=True,
    ).count()
    if active_count >= SharedSpace.MAX_ACTIVE_MEMBERS:
        raise ValidationError("This shared space already has the maximum of two active members.")

    now = timezone.now()
    active_invites = SharedSpaceInvitation.objects.filter(
        created_by=user,
        status=SharedSpaceInvitation.Status.PENDING,
        expires_at__gt=now,
    ).count()
    if active_invites >= MAX_ACTIVE_INVITATIONS_PER_OWNER:
        raise ValidationError(
            "You have reached the limit of 5 active invitations. Revoke an unused invitation to create a new one."
        )

    if expires_in_days is not None:
        delta = timedelta(days=max(1, int(expires_in_days)))
    elif expiry == "1h":
        delta = timedelta(hours=1)
    elif expiry == "7d":
        delta = timedelta(days=7)
    else:
        delta = timedelta(hours=24)

    code = generate_invitation_code()
    invite = SharedSpaceInvitation(
        shared_space=shared_space,
        code=code,
        partner_hint=(partner_hint or "").strip()[:100],
        created_by=user,
        status=SharedSpaceInvitation.Status.PENDING,
        expires_at=now + delta,
    )
    invite.full_clean()
    invite.save()
    return invite


@transaction.atomic
def revoke_shared_space_invitation(*, user, invitation: SharedSpaceInvitation) -> SharedSpaceInvitation:
    assert_space_creator(user, invitation.shared_space)
    if invitation.status != SharedSpaceInvitation.Status.PENDING:
        raise ValidationError("Only pending invitations can be revoked.")
    invitation.status = SharedSpaceInvitation.Status.REVOKED
    invitation.responded_at = timezone.now()
    invitation.save(update_fields=["status", "responded_at"])
    return invitation


@transaction.atomic
def accept_shared_space_invitation(*, user, code: str, request=None) -> SharedSpaceMembership:
    if request:
        is_locked, lock_msg = check_invitation_rate_limit(request)
        if is_locked:
            raise ValidationError({"code": lock_msg})

    raw_code = (code or "").strip()
    if not raw_code:
        if request:
            record_invitation_failure(request, raw_code)
        raise ValidationError({"code": INVITATION_ERROR_MESSAGE})

    compact = re.sub(r"[\s\-]", "", raw_code)
    lookup_code = compact.upper() if len(compact) == 6 else raw_code

    invitation = (
        SharedSpaceInvitation.objects.select_for_update()
        .select_related("shared_space", "shared_space__created_by")
        .filter(Q(code=lookup_code) | Q(code=compact.upper()) | Q(code=raw_code))
        .first()
    )
    if invitation is None:
        if request:
            record_invitation_failure(request, raw_code)
        raise ValidationError({"code": INVITATION_ERROR_MESSAGE})

    now = timezone.now()
    if invitation.status == SharedSpaceInvitation.Status.PENDING and invitation.expires_at <= now:
        invitation.status = SharedSpaceInvitation.Status.EXPIRED
        invitation.responded_at = now
        invitation.save(update_fields=["status", "responded_at"])
        if request:
            record_invitation_failure(request, raw_code)
        raise ValidationError({"code": INVITATION_ERROR_MESSAGE})

    if invitation.status != SharedSpaceInvitation.Status.PENDING:
        if request:
            record_invitation_failure(request, raw_code)
        raise ValidationError({"code": INVITATION_ERROR_MESSAGE})

    space = invitation.shared_space
    if space.created_by == user or invitation.created_by == user:
        raise ValidationError({"code": "You cannot join your own shared space."})

    existing = SharedSpaceMembership.objects.select_for_update().filter(
        shared_space=space,
        user=user,
    ).first()
    if existing and existing.is_active:
        raise ValidationError({"code": "You are already an active member of this shared space."})

    active_count = SharedSpaceMembership.objects.select_for_update().filter(
        shared_space=space,
        is_active=True,
    ).count()
    if active_count >= SharedSpace.MAX_ACTIVE_MEMBERS:
        raise ValidationError({"code": "This shared space has already reached its two-member limit."})

    if existing:
        existing.is_active = True
        existing.left_at = None
        existing.role = SharedSpaceMembership.Role.MEMBER
        existing.joined_at = now
        existing.full_clean()
        existing.save(update_fields=["is_active", "left_at", "role", "joined_at"])
        membership = existing
    else:
        membership = SharedSpaceMembership(
            shared_space=space,
            user=user,
            role=SharedSpaceMembership.Role.MEMBER,
            is_active=True,
            joined_at=now,
        )
        membership.full_clean()
        membership.save()

    invitation.status = SharedSpaceInvitation.Status.ACCEPTED
    invitation.accepted_by = user
    invitation.responded_at = now
    invitation.save(update_fields=["status", "accepted_by", "responded_at"])
    if request:
        clear_invitation_rate_limit(request)
    return membership


@transaction.atomic
def leave_shared_space(*, user, shared_space: SharedSpace) -> SharedSpaceMembership:
    membership = assert_space_member(user, shared_space)
    if shared_space.created_by_id == user.id or membership.role == SharedSpaceMembership.Role.CREATOR:
        raise ValidationError(
            "The shared-space creator cannot leave while managing the space."
        )

    now = timezone.now()
    membership.is_active = False
    membership.left_at = now
    membership.save(update_fields=["is_active", "left_at"])

    PersonalTransactionShare.objects.filter(
        shared_space=shared_space,
        is_revoked=False,
    ).filter(
        models.Q(owner=user) | models.Q(recipient=user)
    ).update(
        is_revoked=True,
        revoked_at=now,
        revision=models.F("revision") + 1,
        updated_at=now,
    )

    PersonalBalanceSharePermission.objects.filter(
        shared_space=shared_space,
        is_revoked=False,
    ).filter(
        models.Q(owner=user) | models.Q(recipient=user)
    ).update(
        is_revoked=True,
        revoked_at=now,
        show_balance=False,
        updated_at=now,
    )

    UserPreference.objects.filter(
        user=user,
        active_shared_space=shared_space,
    ).update(
        active_scope=UserPreference.ScopeChoice.PERSONAL,
        active_shared_space=None,
    )
    return membership


@transaction.atomic
def create_category(
    *,
    user,
    name: str,
    kind: str = Category.Kind.BOTH,
    scope: str = Category.Scope.PERSONAL,
    shared_space: SharedSpace | None = None,
) -> Category:
    resolved_scope, resolved_space = resolve_scope_and_space(user, scope, shared_space)
    cleaned_name = (name or "").strip()
    if not cleaned_name:
        raise ValidationError({"name": "Category name is required."})

    dup_qs = Category.objects.filter(
        scope=resolved_scope,
        name__iexact=cleaned_name,
    )
    if resolved_scope == Category.Scope.PERSONAL:
        dup_qs = dup_qs.filter(owner=user)
    else:
        dup_qs = dup_qs.filter(shared_space=resolved_space)
    if dup_qs.exists():
        raise ValidationError({"name": "A category with this name already exists in this scope."})

    category = Category(
        owner=user,
        scope=resolved_scope,
        shared_space=resolved_space,
        name=cleaned_name,
        kind=kind,
    )
    category.full_clean()
    category.save()
    return category


@transaction.atomic
def update_category(*, user, category: Category, name: str, kind: str) -> Category:
    assert_category_access(user, category)
    if category.scope == Category.Scope.SHARED:
        if category.owner_id != user.id and category.shared_space.created_by_id != user.id:
            raise PermissionDenied("Only the category creator or shared-space creator may edit this category.")

    cleaned_name = (name or "").strip()
    if not cleaned_name:
        raise ValidationError({"name": "Category name is required."})

    category.name = cleaned_name
    category.kind = kind
    category.revision += 1
    category.full_clean()
    category.save()
    return category


@transaction.atomic
def archive_category(*, user, category: Category, archived: bool = True) -> Category:
    assert_category_access(user, category)
    if category.scope == Category.Scope.SHARED:
        if category.owner_id != user.id and category.shared_space.created_by_id != user.id:
            raise PermissionDenied("Only the category creator or shared-space creator may archive this category.")
    category.is_archived = bool(archived)
    category.revision += 1
    category.save(update_fields=["is_archived", "revision", "updated_at"])
    return category


@transaction.atomic
def create_wallet_record(
    *,
    user,
    name: str,
    wallet_type: str,
    currency: Currency,
    opening_balance: Decimal,
    opening_balance_date,
    ownership_scope: str = Wallet.OwnershipScope.PERSONAL,
    shared_space: SharedSpace | None = None,
) -> Wallet:
    resolved_scope, resolved_space = resolve_scope_and_space(user, ownership_scope, shared_space)
    wallet = Wallet(
        owner=user,
        ownership_scope=resolved_scope,
        shared_space=resolved_space,
        name=(name or "").strip(),
        wallet_type=wallet_type,
        currency=currency,
        opening_balance=opening_balance,
        opening_balance_date=opening_balance_date,
    )
    wallet.full_clean()
    wallet.save()
    return wallet


@transaction.atomic
def update_wallet_record(
    *,
    user,
    wallet: Wallet,
    name: str,
    wallet_type: str,
    currency: Currency | None = None,
    opening_balance: Decimal | None = None,
    opening_balance_date=None,
    is_archived: bool | None = None,
) -> Wallet:
    wallet = Wallet.objects.select_for_update().select_related("currency", "shared_space").get(pk=wallet.pk)
    assert_wallet_access(user, wallet)
    if wallet.ownership_scope == Wallet.OwnershipScope.SHARED:
        if wallet.owner_id != user.id and wallet.shared_space.created_by_id != user.id:
            raise PermissionDenied("Only the wallet creator or shared-space creator may edit this wallet.")

    wallet.name = (name or "").strip()
    wallet.wallet_type = wallet_type
    if currency is not None:
        wallet.currency = currency
    if opening_balance is not None:
        wallet.opening_balance = opening_balance
    if opening_balance_date is not None:
        wallet.opening_balance_date = opening_balance_date
    if is_archived is not None:
        wallet.is_archived = bool(is_archived)
    wallet.revision += 1
    wallet.full_clean()
    wallet.save()
    return wallet


def _resolve_booking_conversion(
    *,
    user,
    scope: str,
    shared_space: SharedSpace | None,
    tx_currency: Currency,
    amount: Decimal,
    transaction_date,
    manual_rate: Decimal | None = None,
):
    if scope == Transaction.Scope.SHARED and shared_space is not None:
        base_currency = shared_space.base_currency
    else:
        pref = (
            UserPreference.objects.select_related("personal_base_currency")
            .filter(user=user)
            .first()
        )
        base_currency = pref.personal_base_currency if pref and pref.personal_base_currency_id else None

    if base_currency is None:
        return {
            "base_currency": None,
            "converted_amount": None,
            "conversion_rate": None,
            "conversion_rate_timestamp": None,
            "conversion_provider": "",
            "conversion_status": Transaction.ConversionStatus.PENDING,
        }

    if base_currency.pk == tx_currency.pk:
        return {
            "base_currency": base_currency,
            "converted_amount": amount,
            "conversion_rate": Decimal("1.00000000"),
            "conversion_rate_timestamp": timezone.now(),
            "conversion_provider": "identity",
            "conversion_status": Transaction.ConversionStatus.IDENTITY,
        }

    if manual_rate is not None and str(manual_rate).strip() != "":
        if isinstance(manual_rate, float):
            raise ValidationError({"manual_rate": "Manual rate must be a decimal value, not float."})
        try:
            dec_rate = manual_rate if isinstance(manual_rate, Decimal) else Decimal(str(manual_rate))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise ValidationError({"manual_rate": "Enter a valid decimal exchange rate."}) from exc
        if not dec_rate.is_finite() or dec_rate <= Decimal("0"):
            raise ValidationError({"manual_rate": "Manual exchange rate must be greater than zero."})
        quantized_rate = dec_rate.quantize(RATE_QUANTUM, rounding=ROUND_HALF_UP)
        converted = convert_decimal_amount(amount, quantized_rate, base_currency)
        return {
            "base_currency": base_currency,
            "converted_amount": converted,
            "conversion_rate": quantized_rate,
            "conversion_rate_timestamp": timezone.now(),
            "conversion_provider": "manual",
            "conversion_status": Transaction.ConversionStatus.MANUAL,
        }

    today = timezone.localdate()
    is_historical = transaction_date < today
    lookup = get_latest_rate(
        tx_currency,
        base_currency,
        user=user,
        scope=scope,
        shared_space=shared_space,
        for_date=transaction_date if is_historical else None,
        require_date_match=is_historical,
    )
    if lookup.rate is not None:
        converted = convert_decimal_amount(amount, lookup.rate, base_currency)
        return {
            "base_currency": base_currency,
            "converted_amount": converted,
            "conversion_rate": lookup.rate,
            "conversion_rate_timestamp": lookup.rate_timestamp,
            "conversion_provider": lookup.provider,
            "conversion_status": (
                Transaction.ConversionStatus.MANUAL
                if lookup.is_manual
                else Transaction.ConversionStatus.CONVERTED
            ),
        }

    return {
        "base_currency": base_currency,
        "converted_amount": None,
        "conversion_rate": None,
        "conversion_rate_timestamp": None,
        "conversion_provider": "",
        "conversion_status": Transaction.ConversionStatus.PENDING,
    }


@transaction.atomic
def create_transaction_entry(
    *,
    user,
    transaction_type: str,
    amount: Decimal,
    wallet: Wallet,
    transaction_date,
    currency: Currency | None = None,
    category: Category | None = None,
    scope: str = Transaction.Scope.PERSONAL,
    shared_space: SharedSpace | None = None,
    paid_by=None,
    description: str = "",
    notes: str = "",
    manual_rate: Decimal | None = None,
    tx_id: uuid.UUID | None = None,
    client_mutation_id: str | None = None,
) -> Transaction:
    if transaction_type not in (Transaction.Type.INCOME, Transaction.Type.EXPENSE):
        raise ValidationError({"transaction_type": "Select Income or Expense."})

    resolved_scope, resolved_space = resolve_scope_and_space(user, scope, shared_space)
    assert_wallet_access(user, wallet, expected_scope=resolved_scope, shared_space=resolved_space)
    if wallet.is_archived:
        raise ValidationError({"wallet": "Archived wallets cannot receive new transactions."})

    effective_currency = currency or wallet.currency
    if effective_currency.pk != wallet.currency_id:
        raise ValidationError(
            {"currency": f"Transactions in {wallet.name} must use its currency ({wallet.currency.code})."}
        )

    validate_currency_amount(amount, effective_currency, field_name="amount", require_positive=True)

    if category is not None:
        assert_category_access(user, category, expected_scope=resolved_scope, shared_space=resolved_space)
        if category.is_archived:
            raise ValidationError({"category": "Archived categories cannot be used for new transactions."})
        if category.kind not in (Category.Kind.BOTH, transaction_type):
            raise ValidationError(
                {"category": f'Category "{category.name}" is not valid for {transaction_type} transactions.'}
            )

    resolved_payer = None
    if resolved_scope == Transaction.Scope.SHARED and transaction_type == Transaction.Type.EXPENSE:
        resolved_payer = paid_by or user
        assert_space_member(resolved_payer, resolved_space)
    elif transaction_type == Transaction.Type.EXPENSE:
        resolved_payer = user

    conversion_fields = _resolve_booking_conversion(
        user=user,
        scope=resolved_scope,
        shared_space=resolved_space,
        tx_currency=effective_currency,
        amount=Decimal(str(amount)),
        transaction_date=transaction_date,
        manual_rate=manual_rate,
    )

    tx = Transaction(
        id=tx_id or uuid.uuid4(),
        owner=user,
        paid_by=resolved_payer,
        scope=resolved_scope,
        shared_space=resolved_space,
        wallet=wallet,
        category=category,
        transaction_type=transaction_type,
        amount=Decimal(str(amount)),
        currency=effective_currency,
        transaction_date=transaction_date,
        description=(description or "").strip()[:200],
        notes=(notes or "").strip(),
        client_mutation_id=client_mutation_id,
        **conversion_fields,
    )
    tx.full_clean()
    tx.save()
    return tx


def assert_transaction_modify_permission(user, tx: Transaction):
    if tx.scope == Transaction.Scope.PERSONAL:
        if tx.owner_id != user.id:
            raise PermissionDenied("You cannot modify another user's personal transaction.")
    else:
        assert_space_member(user, tx.shared_space)
        if tx.owner_id != user.id:
            raise PermissionDenied("Shared-space members may only edit or delete their own transactions.")
    if tx.transfer_id is not None or tx.is_fee:
        raise ValidationError("Transfer legs and transfer fee entries must be managed through Transfers.")


@transaction.atomic
def update_transaction_entry(
    *,
    user,
    tx: Transaction,
    transaction_type: str,
    amount: Decimal,
    wallet: Wallet,
    transaction_date,
    currency: Currency | None = None,
    category: Category | None = None,
    paid_by=None,
    description: str = "",
    notes: str = "",
    manual_rate: Decimal | None = None,
    expected_revision: int | None = None,
    require_revision: bool = False,
) -> Transaction:
    expected_rev = parse_positive_revision(expected_revision, required=require_revision)
    tx = Transaction.objects.select_for_update().select_related("wallet", "shared_space").get(pk=tx.pk)
    assert_transaction_modify_permission(user, tx)
    if tx.is_deleted:
        raise ValidationError("Deleted transactions cannot be edited.")

    current_rev = tx.revision
    if expected_rev is not None and expected_rev != current_rev:
        raise ConflictError(
            f"Transaction has been modified (expected revision {expected_rev}, current revision {current_rev})."
        )

    assert_wallet_access(user, wallet, expected_scope=tx.scope, shared_space=tx.shared_space)
    if wallet.is_archived and wallet.pk != tx.wallet_id:
        raise ValidationError({"wallet": "Archived wallets cannot receive transactions."})

    effective_currency = currency or wallet.currency
    if effective_currency.pk != wallet.currency_id:
        raise ValidationError(
            {"currency": f"Transactions in {wallet.name} must use its currency ({wallet.currency.code})."}
        )

    validate_currency_amount(amount, effective_currency, field_name="amount", require_positive=True)

    if category is not None:
        assert_category_access(user, category, expected_scope=tx.scope, shared_space=tx.shared_space)
        if category.kind not in (Category.Kind.BOTH, transaction_type):
            raise ValidationError(
                {"category": f'Category "{category.name}" is not valid for {transaction_type} transactions.'}
            )

    resolved_payer = None
    if tx.scope == Transaction.Scope.SHARED and transaction_type == Transaction.Type.EXPENSE:
        resolved_payer = paid_by or tx.paid_by or user
        assert_space_member(resolved_payer, tx.shared_space)
    elif transaction_type == Transaction.Type.EXPENSE:
        resolved_payer = user

    prev_currency_id = tx.currency_id
    prev_date = tx.transaction_date
    rebook_needed = (
        Decimal(str(amount)) != tx.amount
        or effective_currency.pk != prev_currency_id
        or transaction_date != prev_date
        or (manual_rate is not None and str(manual_rate).strip() != "")
    )

    tx.transaction_type = transaction_type
    tx.amount = Decimal(str(amount))
    tx.wallet = wallet
    tx.currency = effective_currency
    tx.category = category
    tx.paid_by = resolved_payer
    tx.transaction_date = transaction_date
    tx.description = (description or "").strip()[:200]
    tx.notes = (notes or "").strip()

    has_manual_rate = manual_rate is not None and str(manual_rate).strip() != ""
    if rebook_needed:
        can_reuse_existing_rate = (
            not has_manual_rate
            and effective_currency.pk == prev_currency_id
            and tx.conversion_rate is not None
            and tx.base_currency_id is not None
        )
        if can_reuse_existing_rate and (
            transaction_date == prev_date
            or tx.conversion_status == Transaction.ConversionStatus.MANUAL
        ):
            tx.converted_amount = convert_decimal_amount(
                tx.amount, tx.conversion_rate, tx.base_currency
            )
        else:
            conv = _resolve_booking_conversion(
                user=user,
                scope=tx.scope,
                shared_space=tx.shared_space,
                tx_currency=effective_currency,
                amount=tx.amount,
                transaction_date=transaction_date,
                manual_rate=manual_rate,
            )
            if (
                can_reuse_existing_rate
                and conv["conversion_status"] == Transaction.ConversionStatus.PENDING
            ):
                # Preserve existing conversion rate rather than wiping a valid
                # historical/booked conversion to PENDING when no rate is cached
                # for the new date.
                tx.converted_amount = convert_decimal_amount(
                    tx.amount, tx.conversion_rate, tx.base_currency
                )
            else:
                for k, v in conv.items():
                    setattr(tx, k, v)

    tx.revision = current_rev + 1
    tx.full_clean()

    now = timezone.now()
    updated_rows = Transaction.objects.filter(
        pk=tx.pk,
        revision=current_rev,
        is_deleted=False,
    ).update(
        transaction_type=tx.transaction_type,
        amount=tx.amount,
        wallet=tx.wallet,
        currency=tx.currency,
        category=tx.category,
        paid_by=tx.paid_by,
        transaction_date=tx.transaction_date,
        description=tx.description,
        notes=tx.notes,
        base_currency=tx.base_currency,
        converted_amount=tx.converted_amount,
        conversion_rate=tx.conversion_rate,
        conversion_rate_timestamp=tx.conversion_rate_timestamp,
        conversion_provider=tx.conversion_provider,
        conversion_status=tx.conversion_status,
        revision=tx.revision,
        updated_at=now,
    )
    if updated_rows != 1:
        raise ConflictError("Transaction was modified concurrently. Please refresh and retry.")

    tx.updated_at = now
    return tx


@transaction.atomic
def delete_transaction_entry(
    *,
    user,
    tx: Transaction,
    expected_revision: int | None = None,
    require_revision: bool = False,
) -> Transaction:
    expected_rev = parse_positive_revision(expected_revision, required=require_revision)
    tx = Transaction.objects.select_for_update().select_related("shared_space").get(pk=tx.pk)
    assert_transaction_modify_permission(user, tx)
    current_rev = tx.revision
    if expected_rev is not None and expected_rev != current_rev:
        raise ConflictError(
            f"Transaction has been modified (expected revision {expected_rev}, current revision {current_rev})."
        )
    if not tx.is_deleted:
        now = timezone.now()
        new_rev = current_rev + 1
        updated_rows = Transaction.objects.filter(
            pk=tx.pk,
            revision=current_rev,
            is_deleted=False,
        ).update(
            is_deleted=True,
            deleted_at=now,
            revision=new_rev,
            updated_at=now,
        )
        if updated_rows != 1:
            raise ConflictError("Transaction was modified concurrently. Please refresh and retry.")
        tx.is_deleted = True
        tx.deleted_at = now
        tx.revision = new_rev
        tx.updated_at = now
        PersonalTransactionShare.objects.filter(
            transaction=tx,
            is_revoked=False,
        ).update(
            is_revoked=True,
            revoked_at=now,
            revision=models.F("revision") + 1,
            updated_at=now,
        )
    return tx


def _validate_and_resolve_transfer_amounts(
    *,
    source_wallet: Wallet,
    destination_wallet: Wallet,
    amount_sent,
    amount_received=None,
    manual_rate=None,
    fee_amount=None,
    transfer_date,
    allow_archived_wallet_ids=None,
):
    allow_archived_ids = set(allow_archived_wallet_ids or ())
    if source_wallet.pk == destination_wallet.pk:
        raise ValidationError({"destination_wallet": "Source and destination wallets must be different."})

    if source_wallet.ownership_scope != destination_wallet.ownership_scope:
        raise ValidationError(
            "Transfers between personal and shared wallets are not permitted."
        )

    if (
        source_wallet.is_archived and source_wallet.pk not in allow_archived_ids
    ) or (
        destination_wallet.is_archived and destination_wallet.pk not in allow_archived_ids
    ):
        raise ValidationError("Archived wallets cannot be used for transfers.")

    if transfer_date < source_wallet.opening_balance_date:
        raise ValidationError(
            {
                "transfer_date": (
                    f"Transfer date cannot be earlier than {source_wallet.name} opening-balance date "
                    f"({source_wallet.opening_balance_date})."
                )
            }
        )
    if transfer_date < destination_wallet.opening_balance_date:
        raise ValidationError(
            {
                "transfer_date": (
                    f"Transfer date cannot be earlier than {destination_wallet.name} opening-balance date "
                    f"({destination_wallet.opening_balance_date})."
                )
            }
        )

    sent_dec = validate_currency_amount(
        amount_sent,
        source_wallet.currency,
        field_name="amount_sent",
        require_positive=True,
    )

    fee_dec = Decimal("0.000")
    if fee_amount is not None and str(fee_amount).strip() != "":
        fee_dec = validate_currency_amount(
            fee_amount,
            source_wallet.currency,
            field_name="fee_amount",
            require_positive=False,
        )
        if fee_dec < Decimal("0"):
            raise ValidationError({"fee_amount": "Fee amount cannot be negative."})

    if source_wallet.currency_id == destination_wallet.currency_id:
        if amount_received is not None and str(amount_received).strip() != "":
            recv_dec = validate_currency_amount(
                amount_received,
                destination_wallet.currency,
                field_name="amount_received",
                require_positive=True,
            )
            if recv_dec != sent_dec:
                raise ValidationError(
                    {"amount_received": "Same-currency transfer received amount must equal sent amount."}
                )
        recv_dec = sent_dec
        eff_rate = Decimal("1.00000000")
    else:
        if amount_received is not None and str(amount_received).strip() != "":
            recv_dec = validate_currency_amount(
                amount_received,
                destination_wallet.currency,
                field_name="amount_received",
                require_positive=True,
            )
            eff_rate = (recv_dec / sent_dec).quantize(RATE_QUANTUM, rounding=ROUND_HALF_UP)
        elif manual_rate is not None and str(manual_rate).strip() != "":
            if isinstance(manual_rate, float):
                raise ValidationError({"manual_rate": "Rate must be a decimal value, not float."})
            try:
                rate_dec = manual_rate if isinstance(manual_rate, Decimal) else Decimal(str(manual_rate))
            except (InvalidOperation, ValueError, TypeError) as exc:
                raise ValidationError({"manual_rate": "Enter a valid decimal rate."}) from exc
            if not rate_dec.is_finite() or rate_dec <= Decimal("0"):
                raise ValidationError({"manual_rate": "Exchange rate must be greater than zero."})
            eff_rate = rate_dec.quantize(RATE_QUANTUM, rounding=ROUND_HALF_UP)
            recv_dec = convert_decimal_amount(sent_dec, eff_rate, destination_wallet.currency)
            if recv_dec <= Decimal("0"):
                raise ValidationError({"amount_received": "Converted received amount must be greater than zero."})
        else:
            raise ValidationError(
                {
                    "amount_received": (
                        "Cross-currency transfers require either the actual received amount or an explicit exchange rate."
                    )
                }
            )
    return sent_dec, recv_dec, eff_rate, fee_dec


@transaction.atomic
def create_transfer_entry(
    *,
    user,
    source_wallet: Wallet,
    destination_wallet: Wallet,
    amount_sent: Decimal,
    transfer_date,
    amount_received: Decimal | None = None,
    manual_rate: Decimal | None = None,
    fee_amount: Decimal | None = None,
    notes: str = "",
) -> Transfer:
    scope = source_wallet.ownership_scope
    shared_space = source_wallet.shared_space if scope == Wallet.OwnershipScope.SHARED else None
    if (
        source_wallet.ownership_scope == destination_wallet.ownership_scope == Wallet.OwnershipScope.SHARED
        and source_wallet.shared_space_id != destination_wallet.shared_space_id
    ):
        raise PermissionDenied("Transfers between different shared spaces are not permitted.")

    assert_wallet_access(user, source_wallet, expected_scope=scope, shared_space=shared_space)
    assert_wallet_access(user, destination_wallet, expected_scope=scope, shared_space=shared_space)

    sent_dec, recv_dec, eff_rate, fee_dec = _validate_and_resolve_transfer_amounts(
        source_wallet=source_wallet,
        destination_wallet=destination_wallet,
        amount_sent=amount_sent,
        amount_received=amount_received,
        manual_rate=manual_rate,
        fee_amount=fee_amount,
        transfer_date=transfer_date,
    )
    now = timezone.now()

    transfer_obj = Transfer.objects.create(
        created_by=user,
        scope=scope,
        shared_space=shared_space,
        source_wallet=source_wallet,
        destination_wallet=destination_wallet,
        amount_sent=sent_dec,
        source_currency=source_wallet.currency,
        amount_received=recv_dec,
        destination_currency=destination_wallet.currency,
        effective_rate=eff_rate,
        rate_timestamp=now,
        fee_amount=fee_dec,
        transfer_date=transfer_date,
        notes=(notes or "").strip(),
    )

    out_leg = Transaction(
        owner=user,
        scope=scope,
        shared_space=shared_space,
        wallet=source_wallet,
        transfer=transfer_obj,
        transaction_type=Transaction.Type.TRANSFER_OUT,
        amount=sent_dec,
        currency=source_wallet.currency,
        transaction_date=transfer_date,
        description=f"Transfer to {destination_wallet.name}",
        notes=(notes or "").strip(),
        conversion_status=Transaction.ConversionStatus.IDENTITY,
    )
    out_leg.full_clean()
    out_leg.save()

    in_leg = Transaction(
        owner=user,
        scope=scope,
        shared_space=shared_space,
        wallet=destination_wallet,
        transfer=transfer_obj,
        transaction_type=Transaction.Type.TRANSFER_IN,
        amount=recv_dec,
        currency=destination_wallet.currency,
        transaction_date=transfer_date,
        description=f"Transfer from {source_wallet.name}",
        notes=(notes or "").strip(),
        conversion_status=Transaction.ConversionStatus.IDENTITY,
    )
    in_leg.full_clean()
    in_leg.save()

    if fee_dec > Decimal("0"):
        fee_conv = _resolve_booking_conversion(
            user=user,
            scope=scope,
            shared_space=shared_space,
            tx_currency=source_wallet.currency,
            amount=fee_dec,
            transaction_date=transfer_date,
        )
        fee_tx = Transaction(
            owner=user,
            paid_by=user,
            scope=scope,
            shared_space=shared_space,
            wallet=source_wallet,
            transfer=transfer_obj,
            transaction_type=Transaction.Type.EXPENSE,
            amount=fee_dec,
            currency=source_wallet.currency,
            transaction_date=transfer_date,
            description=f"Transfer fee ({source_wallet.name} to {destination_wallet.name})",
            notes=(notes or "").strip(),
            is_fee=True,
            **fee_conv,
        )
        fee_tx.full_clean()
        fee_tx.save()
        transfer_obj.fee_transaction = fee_tx
        transfer_obj.save(update_fields=["fee_transaction"])

    return transfer_obj


@transaction.atomic
def update_transfer_entry(
    *,
    user,
    transfer_obj: Transfer,
    source_wallet: Wallet,
    destination_wallet: Wallet,
    amount_sent: Decimal,
    transfer_date,
    amount_received: Decimal | None = None,
    manual_rate: Decimal | None = None,
    fee_amount: Decimal | None = None,
    notes: str = "",
    expected_revision: int | None = None,
) -> Transfer:
    expected_rev = parse_positive_revision(expected_revision, required=False)
    transfer_obj = (
        Transfer.objects.select_for_update()
        .select_related("shared_space", "source_wallet", "destination_wallet", "fee_transaction")
        .get(pk=transfer_obj.pk)
    )
    if transfer_obj.is_deleted:
        raise ValidationError("Deleted transfers cannot be edited.")

    if transfer_obj.scope == Transfer.Scope.PERSONAL:
        if transfer_obj.created_by_id != user.id:
            raise PermissionDenied("You cannot edit another user's transfer.")
    else:
        assert_space_member(user, transfer_obj.shared_space)
        if transfer_obj.created_by_id != user.id:
            raise PermissionDenied("Shared-space members may only edit their own transfers.")

    current_rev = transfer_obj.revision
    if expected_rev is not None and expected_rev != current_rev:
        raise ConflictError(
            f"Transfer has been modified (expected revision {expected_rev}, current revision {current_rev})."
        )

    assert_wallet_access(
        user,
        source_wallet,
        expected_scope=transfer_obj.scope,
        shared_space=transfer_obj.shared_space,
    )
    assert_wallet_access(
        user,
        destination_wallet,
        expected_scope=transfer_obj.scope,
        shared_space=transfer_obj.shared_space,
    )

    sent_dec, recv_dec, eff_rate, fee_dec = _validate_and_resolve_transfer_amounts(
        source_wallet=source_wallet,
        destination_wallet=destination_wallet,
        amount_sent=amount_sent,
        amount_received=amount_received,
        manual_rate=manual_rate,
        fee_amount=fee_amount,
        transfer_date=transfer_date,
        allow_archived_wallet_ids={transfer_obj.source_wallet_id, transfer_obj.destination_wallet_id},
    )

    out_leg = (
        transfer_obj.legs.select_for_update()
        .filter(transaction_type=Transaction.Type.TRANSFER_OUT, is_deleted=False)
        .first()
    )
    in_leg = (
        transfer_obj.legs.select_for_update()
        .filter(transaction_type=Transaction.Type.TRANSFER_IN, is_deleted=False)
        .first()
    )
    if out_leg is None or in_leg is None:
        raise ValidationError("Transfer legs are missing or corrupted.")

    cleaned_notes = (notes or "").strip()
    now = timezone.now()

    out_leg.wallet = source_wallet
    out_leg.currency = source_wallet.currency
    out_leg.amount = sent_dec
    out_leg.transaction_date = transfer_date
    out_leg.description = f"Transfer to {destination_wallet.name}"
    out_leg.notes = cleaned_notes
    out_leg.revision += 1
    out_leg.full_clean()
    out_leg.save()

    in_leg.wallet = destination_wallet
    in_leg.currency = destination_wallet.currency
    in_leg.amount = recv_dec
    in_leg.transaction_date = transfer_date
    in_leg.description = f"Transfer from {source_wallet.name}"
    in_leg.notes = cleaned_notes
    in_leg.revision += 1
    in_leg.full_clean()
    in_leg.save()

    fee_tx = transfer_obj.fee_transaction
    if fee_dec > Decimal("0"):
        fee_conv = _resolve_booking_conversion(
            user=user,
            scope=transfer_obj.scope,
            shared_space=transfer_obj.shared_space,
            tx_currency=source_wallet.currency,
            amount=fee_dec,
            transaction_date=transfer_date,
        )
        if fee_tx is not None:
            fee_tx.wallet = source_wallet
            fee_tx.currency = source_wallet.currency
            fee_tx.amount = fee_dec
            fee_tx.transaction_date = transfer_date
            fee_tx.description = f"Transfer fee ({source_wallet.name} to {destination_wallet.name})"
            fee_tx.notes = cleaned_notes
            fee_tx.is_deleted = False
            fee_tx.deleted_at = None
            fee_tx.revision += 1
            for k, v in fee_conv.items():
                setattr(fee_tx, k, v)
            fee_tx.full_clean()
            fee_tx.save()
        else:
            fee_tx = Transaction(
                owner=user,
                paid_by=user,
                scope=transfer_obj.scope,
                shared_space=transfer_obj.shared_space,
                wallet=source_wallet,
                transfer=transfer_obj,
                transaction_type=Transaction.Type.EXPENSE,
                amount=fee_dec,
                currency=source_wallet.currency,
                transaction_date=transfer_date,
                description=f"Transfer fee ({source_wallet.name} to {destination_wallet.name})",
                notes=cleaned_notes,
                is_fee=True,
                **fee_conv,
            )
            fee_tx.full_clean()
            fee_tx.save()
    elif fee_tx is not None and not fee_tx.is_deleted:
        fee_tx.is_deleted = True
        fee_tx.deleted_at = now
        fee_tx.revision += 1
        fee_tx.save(update_fields=["is_deleted", "deleted_at", "revision", "updated_at"])

    new_rev = current_rev + 1
    updated_rows = Transfer.objects.filter(
        pk=transfer_obj.pk,
        revision=current_rev,
        is_deleted=False,
    ).update(
        source_wallet=source_wallet,
        destination_wallet=destination_wallet,
        amount_sent=sent_dec,
        source_currency=source_wallet.currency,
        amount_received=recv_dec,
        destination_currency=destination_wallet.currency,
        effective_rate=eff_rate,
        rate_timestamp=now,
        fee_amount=fee_dec,
        fee_transaction=fee_tx,
        transfer_date=transfer_date,
        notes=cleaned_notes,
        revision=new_rev,
        updated_at=now,
    )
    if updated_rows != 1:
        raise ConflictError("Transfer was modified concurrently. Please refresh and retry.")

    transfer_obj.refresh_from_db()
    return transfer_obj


@transaction.atomic
def delete_transfer_entry(*, user, transfer_obj: Transfer, expected_revision: int | None = None) -> Transfer:
    expected_rev = parse_positive_revision(expected_revision, required=False)
    transfer_obj = Transfer.objects.select_for_update().select_related("shared_space").get(pk=transfer_obj.pk)
    if transfer_obj.scope == Transfer.Scope.PERSONAL:
        if transfer_obj.created_by_id != user.id:
            raise PermissionDenied("You cannot delete another user's transfer.")
    else:
        assert_space_member(user, transfer_obj.shared_space)
        if transfer_obj.created_by_id != user.id:
            raise PermissionDenied("Shared-space members may only delete their own transfers.")

    current_rev = transfer_obj.revision
    if expected_rev is not None and expected_rev != current_rev:
        raise ConflictError(
            f"Transfer has been modified (expected revision {expected_rev}, current revision {current_rev})."
        )

    if not transfer_obj.is_deleted:
        now = timezone.now()
        new_rev = current_rev + 1
        updated_rows = Transfer.objects.filter(
            pk=transfer_obj.pk,
            revision=current_rev,
            is_deleted=False,
        ).update(
            is_deleted=True,
            deleted_at=now,
            revision=new_rev,
            updated_at=now,
        )
        if updated_rows != 1:
            raise ConflictError("Transfer was modified concurrently. Please refresh and retry.")
        transfer_obj.is_deleted = True
        transfer_obj.deleted_at = now
        transfer_obj.revision = new_rev

        for leg in transfer_obj.legs.filter(is_deleted=False):
            leg.is_deleted = True
            leg.deleted_at = now
            leg.revision += 1
            leg.save(update_fields=["is_deleted", "deleted_at", "revision", "updated_at"])

    return transfer_obj


@transaction.atomic
def save_category_budget(
    *,
    user,
    category: Category,
    period: str,
    currency: Currency,
    limit_amount: Decimal,
    scope: str = CategoryBudget.Scope.PERSONAL,
    shared_space: SharedSpace | None = None,
) -> CategoryBudget:
    resolved_scope, resolved_space = resolve_scope_and_space(user, scope, shared_space)
    assert_category_access(user, category, expected_scope=resolved_scope, shared_space=resolved_space)
    if category.is_archived:
        raise ValidationError({"category": "Archived categories cannot be budgeted."})
    if not currency.is_active:
        raise ValidationError({"currency": "Select an active currency."})

    validate_currency_amount(limit_amount, currency, field_name="limit_amount", require_positive=True)

    existing_qs = CategoryBudget.objects.select_for_update().filter(
        scope=resolved_scope,
        category=category,
        period=period,
        currency=currency,
    )
    if resolved_scope == CategoryBudget.Scope.PERSONAL:
        existing_qs = existing_qs.filter(owner=user)
    else:
        existing_qs = existing_qs.filter(shared_space=resolved_space)

    budget = existing_qs.first()
    if budget:
        if resolved_scope == CategoryBudget.Scope.SHARED:
            if budget.owner_id != user.id and resolved_space.created_by_id != user.id:
                raise PermissionDenied("Only the budget creator or shared-space creator may update this budget.")
        budget.limit_amount = Decimal(str(limit_amount))
        budget.is_archived = False
        budget.revision += 1
        budget.full_clean()
        budget.save()
        return budget

    budget = CategoryBudget(
        owner=user,
        scope=resolved_scope,
        shared_space=resolved_space,
        category=category,
        period=period,
        currency=currency,
        limit_amount=Decimal(str(limit_amount)),
    )
    budget.full_clean()
    budget.save()
    return budget


@transaction.atomic
def archive_category_budget(*, user, budget: CategoryBudget) -> CategoryBudget:
    if budget.scope == CategoryBudget.Scope.PERSONAL:
        if budget.owner_id != user.id:
            raise PermissionDenied("You cannot modify another user's budget.")
    else:
        assert_space_member(user, budget.shared_space)
        if budget.owner_id != user.id and budget.shared_space.created_by_id != user.id:
            raise PermissionDenied("Only the budget creator or shared-space creator may archive this budget.")
    budget.is_archived = True
    budget.revision += 1
    budget.save(update_fields=["is_archived", "revision", "updated_at"])
    return budget


@transaction.atomic
def create_savings_goal(
    *,
    user,
    name: str,
    currency: Currency,
    target_amount: Decimal,
    target_date=None,
    scope: str = SavingsGoal.Scope.PERSONAL,
    shared_space: SharedSpace | None = None,
) -> SavingsGoal:
    resolved_scope, resolved_space = resolve_scope_and_space(user, scope, shared_space)
    if not currency.is_active:
        raise ValidationError({"currency": "Select an active currency."})
    validate_currency_amount(target_amount, currency, field_name="target_amount", require_positive=True)

    goal = SavingsGoal(
        owner=user,
        scope=resolved_scope,
        shared_space=resolved_space,
        name=(name or "").strip(),
        currency=currency,
        target_amount=Decimal(str(target_amount)),
        target_date=target_date,
    )
    goal.full_clean()
    goal.save()
    return goal


@transaction.atomic
def update_savings_goal(
    *,
    user,
    goal: SavingsGoal,
    name: str,
    target_amount: Decimal,
    currency: Currency | None = None,
    target_date=None,
    expected_revision: int | None = None,
) -> SavingsGoal:
    expected_rev = parse_positive_revision(expected_revision, required=False)
    goal = SavingsGoal.objects.select_for_update().select_related("currency", "shared_space").get(pk=goal.pk)

    if goal.scope == SavingsGoal.Scope.PERSONAL:
        if goal.owner_id != user.id:
            raise PermissionDenied("You cannot edit another user's savings goal.")
    else:
        assert_space_member(user, goal.shared_space)
        if goal.owner_id != user.id and goal.shared_space.created_by_id != user.id:
            raise PermissionDenied("Only the goal creator or shared-space creator may edit this goal.")

    if goal.is_archived:
        raise ValidationError("Archived savings goals cannot be edited.")

    current_rev = goal.revision
    if expected_rev is not None and expected_rev != current_rev:
        raise ConflictError(
            f"Savings goal has been modified (expected revision {expected_rev}, current revision {current_rev})."
        )

    effective_currency = currency or goal.currency
    if effective_currency.pk != goal.currency_id:
        if goal.contributions.exists():
            raise ValidationError(
                {"currency": "Goal currency cannot be changed once contributions have been recorded."}
            )
        if not effective_currency.is_active:
            raise ValidationError({"currency": "Select an active currency."})

    cleaned_name = (name or "").strip()
    if not cleaned_name:
        raise ValidationError({"name": "Goal name is required."})

    target_dec = validate_currency_amount(
        target_amount,
        effective_currency,
        field_name="target_amount",
        require_positive=True,
    )

    goal.name = cleaned_name
    goal.currency = effective_currency
    goal.target_amount = target_dec
    goal.target_date = target_date
    goal.revision = current_rev + 1
    goal.full_clean()

    now = timezone.now()
    updated_rows = SavingsGoal.objects.filter(
        pk=goal.pk,
        revision=current_rev,
        is_archived=False,
    ).update(
        name=goal.name,
        currency=goal.currency,
        target_amount=goal.target_amount,
        target_date=goal.target_date,
        revision=goal.revision,
        updated_at=now,
    )
    if updated_rows != 1:
        raise ConflictError("Savings goal was modified concurrently. Please refresh and retry.")

    goal.updated_at = now
    return goal


@transaction.atomic
def archive_savings_goal(*, user, goal: SavingsGoal) -> SavingsGoal:
    if goal.scope == SavingsGoal.Scope.PERSONAL:
        if goal.owner_id != user.id:
            raise PermissionDenied("You cannot archive another user's savings goal.")
    else:
        assert_space_member(user, goal.shared_space)
        if goal.owner_id != user.id and goal.shared_space.created_by_id != user.id:
            raise PermissionDenied("Only the goal creator or shared-space creator may archive this goal.")
    goal.is_archived = True
    goal.revision += 1
    goal.save(update_fields=["is_archived", "revision", "updated_at"])
    return goal


@transaction.atomic
def record_savings_contribution(
    *,
    user,
    goal: SavingsGoal,
    amount: Decimal,
    contribution_date,
    entry_type: str = SavingsContribution.EntryType.CONTRIBUTION,
    notes: str = "",
) -> SavingsContribution:
    goal = SavingsGoal.objects.select_for_update().select_related("currency", "shared_space").get(pk=goal.pk)
    if goal.scope == SavingsGoal.Scope.PERSONAL:
        if goal.owner_id != user.id:
            raise PermissionDenied("You cannot contribute to another user's personal savings goal.")
    else:
        assert_space_member(user, goal.shared_space)

    if goal.is_archived:
        raise ValidationError("Cannot record contributions on an archived savings goal.")

    if entry_type not in (
        SavingsContribution.EntryType.CONTRIBUTION,
        SavingsContribution.EntryType.REVERSAL,
    ):
        raise ValidationError({"entry_type": "Select Contribution or Reversal."})

    dec_amt = validate_currency_amount(amount, goal.currency, field_name="amount", require_positive=True)
    if entry_type == SavingsContribution.EntryType.REVERSAL and dec_amt > goal.saved_amount:
        raise ValidationError(
            {
                "amount": (
                    f"Reversal cannot exceed current earmarked amount "
                    f"({format_currency_amount(goal.saved_amount, goal.currency)} {goal.currency.code})."
                )
            }
        )

    contrib = SavingsContribution(
        goal=goal,
        created_by=user,
        entry_type=entry_type,
        amount=dec_amt,
        contribution_date=contribution_date,
        notes=(notes or "").strip()[:200],
    )
    contrib.full_clean()
    contrib.save()

    goal.revision += 1
    goal.save(update_fields=["revision", "updated_at"])
    return contrib


def _parse_iso_date_strict(raw_val, field_name="transaction_date") -> date:
    if not isinstance(raw_val, str) or not raw_val.strip():
        raise ValidationError({field_name: "Enter a valid date in YYYY-MM-DD format."})
    try:
        return datetime.strptime(raw_val.strip(), "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValidationError({field_name: "Enter a valid date in YYYY-MM-DD format."}) from exc


def _resolve_sync_payer(user, payload: dict):
    User = get_user_model()
    paid_by_id = payload.get("paid_by_id")
    paid_by_username = payload.get("paid_by_username")
    if paid_by_id not in (None, ""):
        if isinstance(paid_by_id, (dict, list, bool)):
            raise ValidationError({"paid_by": "Invalid payer identifier."})
        try:
            payer = User.objects.filter(pk=int(paid_by_id)).first()
        except (ValueError, TypeError) as exc:
            raise ValidationError({"paid_by": "Invalid payer identifier."}) from exc
        if payer is None:
            raise ValidationError({"paid_by": "Selected payer not found."})
        return payer
    if isinstance(paid_by_username, str) and paid_by_username.strip():
        payer = User.objects.filter(username=paid_by_username.strip()).first()
        if payer is None:
            raise ValidationError({"paid_by": "Selected payer not found."})
        return payer
    return None


def process_sync_batch(
    *,
    user,
    mutations: list,
    expected_user_id=None,
    is_final_batch: bool = True,
) -> dict:
    if expected_user_id not in (None, ""):
        if str(expected_user_id).strip() != str(user.pk):
            return {
                "batch_status": "user_mismatch",
                "error": "Authenticated user does not match the offline queue owner.",
                "results": [],
                "applied_count": 0,
                "error_count": len(mutations) if isinstance(mutations, list) else 0,
                "conflict_count": 0,
                "blocked_count": 0,
                "last_sync_at": None,
            }

    if not isinstance(mutations, list):
        raise ValidationError('"mutations" must be a JSON list.')
    if len(mutations) > MAX_SYNC_BATCH_SIZE:
        raise ValidationError(f"Batch size cannot exceed {MAX_SYNC_BATCH_SIZE} mutations.")

    results = []
    applied_count = 0
    error_count = 0
    conflict_count = 0
    blocked_count = 0
    failed_entity_ids = set()

    for item in mutations:
        if not isinstance(item, dict):
            msg = "Each mutation item must be a JSON object."
            results.append({"mutation_id": "", "status": "error", "error": msg, "message": msg})
            error_count += 1
            continue

        raw_mut_id = item.get("mutation_id")
        if not isinstance(raw_mut_id, str) or not raw_mut_id.strip() or len(raw_mut_id.strip()) > 64:
            msg = "Missing or invalid mutation_id."
            results.append({"mutation_id": "", "status": "error", "error": msg, "message": msg})
            error_count += 1
            continue
        mutation_id = raw_mut_id.strip()

        raw_op = item.get("operation", "create_transaction")
        if not isinstance(raw_op, str) or not raw_op.strip():
            msg = "Invalid operation."
            results.append({"mutation_id": mutation_id, "status": "error", "error": msg, "message": msg})
            error_count += 1
            continue
        op = raw_op.strip()

        entity_id_raw = item.get("entity_id")
        if entity_id_raw is not None and not isinstance(entity_id_raw, str):
            msg = "Invalid entity UUID."
            results.append({"mutation_id": mutation_id, "status": "error", "error": msg, "message": msg})
            error_count += 1
            continue

        try:
            entity_uuid = uuid.UUID(entity_id_raw.strip()) if entity_id_raw else uuid.uuid4()
        except (ValueError, TypeError, AttributeError):
            msg = "Invalid entity UUID."
            results.append({"mutation_id": mutation_id, "status": "error", "error": msg, "message": msg})
            error_count += 1
            continue

        entity_key = str(entity_uuid)
        if entity_key in failed_entity_ids:
            msg = "Skipped because an earlier queued operation on the same record failed or conflicted."
            results.append(
                {
                    "mutation_id": mutation_id,
                    "entity_id": entity_key,
                    "status": "blocked_by_dependency",
                    "error": msg,
                    "message": msg,
                }
            )
            blocked_count += 1
            error_count += 1
            continue

        payload = item.get("payload")
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            msg = '"payload" must be a JSON object.'
            failed_entity_ids.add(entity_key)
            results.append(
                {
                    "mutation_id": mutation_id,
                    "entity_id": entity_key,
                    "status": "error",
                    "error": msg,
                    "message": msg,
                }
            )
            error_count += 1
            continue

        base_revision = item.get("base_revision")
        fingerprint = _compute_mutation_fingerprint(op, entity_key, base_revision, payload)

        existing_receipt = SyncMutationReceipt.objects.filter(
            user=user,
            mutation_id=mutation_id,
        ).first()
        if existing_receipt is not None:
            if existing_receipt.request_fingerprint and existing_receipt.request_fingerprint != fingerprint:
                msg = "Mutation ID was already used with a different operation or payload."
                failed_entity_ids.add(entity_key)
                results.append(
                    {
                        "mutation_id": mutation_id,
                        "entity_id": entity_key,
                        "status": "error",
                        "code": "mutation_id_reused",
                        "error": msg,
                        "message": msg,
                    }
                )
                error_count += 1
                continue
            cached = dict(existing_receipt.response_payload)
            cached["idempotent_replay"] = True
            results.append(cached)
            applied_count += 1
            continue

        try:
            with transaction.atomic():
                if op == "create_transaction":
                    existing_tx = Transaction.objects.select_for_update().filter(pk=entity_uuid).first()
                    if existing_tx is not None:
                        if existing_tx.owner_id != user.id:
                            raise PermissionDenied("UUID collision or unauthorized transaction reference.")
                        if existing_tx.client_mutation_id and existing_tx.client_mutation_id != mutation_id:
                            raise ValidationError("Transaction UUID already exists with a different mutation_id.")
                        tx = existing_tx
                    else:
                        wallet_id = payload.get("wallet_id")
                        if not isinstance(wallet_id, str) or not wallet_id.strip():
                            raise ValidationError({"wallet": "Wallet is required."})
                        try:
                            wallet_uuid = uuid.UUID(wallet_id.strip())
                        except ValueError as exc:
                            raise ValidationError({"wallet": "Invalid wallet UUID."}) from exc
                        wallet = Wallet.objects.filter(pk=wallet_uuid).first()
                        if wallet is None:
                            raise ValidationError({"wallet": "Wallet not found."})

                        category = None
                        cat_id = payload.get("category_id")
                        if cat_id not in (None, ""):
                            if not isinstance(cat_id, str):
                                raise ValidationError({"category": "Invalid category UUID."})
                            try:
                                cat_uuid = uuid.UUID(cat_id.strip())
                            except ValueError as exc:
                                raise ValidationError({"category": "Invalid category UUID."}) from exc
                            category = Category.objects.filter(pk=cat_uuid).first()
                            if category is None:
                                raise ValidationError({"category": "Category not found."})

                        shared_space = None
                        space_id = payload.get("shared_space_id")
                        if space_id not in (None, ""):
                            if not isinstance(space_id, str):
                                raise ValidationError({"shared_space": "Invalid shared space UUID."})
                            try:
                                space_uuid = uuid.UUID(space_id.strip())
                            except ValueError as exc:
                                raise ValidationError({"shared_space": "Invalid shared space UUID."}) from exc
                            shared_space = SharedSpace.objects.filter(pk=space_uuid).first()
                            if shared_space is None:
                                raise ValidationError({"shared_space": "Shared space not found."})

                        raw_amt = payload.get("amount")
                        if isinstance(raw_amt, (dict, list, bool, float)) or raw_amt in (None, ""):
                            raise ValidationError({"amount": "Enter a valid decimal amount."})

                        raw_manual_rate = payload.get("manual_rate")
                        manual_rate_dec = None
                        if raw_manual_rate not in (None, ""):
                            if isinstance(raw_manual_rate, (dict, list, bool, float)):
                                raise ValidationError({"manual_rate": "Enter a valid decimal exchange rate."})
                            manual_rate_dec = Decimal(str(raw_manual_rate))

                        payer = _resolve_sync_payer(user, payload)
                        tx_date = _parse_iso_date_strict(payload.get("transaction_date"))

                        tx = create_transaction_entry(
                            user=user,
                            tx_id=entity_uuid,
                            transaction_type=str(payload.get("transaction_type") or Transaction.Type.EXPENSE),
                            amount=Decimal(str(raw_amt)),
                            wallet=wallet,
                            transaction_date=tx_date,
                            category=category,
                            scope=str(payload.get("scope") or wallet.ownership_scope),
                            shared_space=shared_space or wallet.shared_space,
                            paid_by=payer,
                            description=str(payload.get("description") or ""),
                            notes=str(payload.get("notes") or ""),
                            manual_rate=manual_rate_dec,
                            client_mutation_id=mutation_id,
                        )
                    outcome = {
                        "mutation_id": mutation_id,
                        "entity_id": str(tx.id),
                        "status": "applied",
                        "revision": tx.revision,
                        "conversion_status": tx.conversion_status,
                    }
                    SyncMutationReceipt.objects.create(
                        user=user,
                        mutation_id=mutation_id,
                        request_fingerprint=fingerprint,
                        entity_type="transaction",
                        entity_id=tx.id,
                        status="applied",
                        response_payload=outcome,
                    )
                    results.append(outcome)
                    applied_count += 1

                elif op == "update_transaction":
                    validated_rev = parse_positive_revision(base_revision, required=True)
                    tx = Transaction.objects.select_for_update().filter(pk=entity_uuid).first()
                    if tx is None:
                        raise ValidationError("Transaction not found.")

                    wallet = tx.wallet
                    if "wallet_id" in payload and payload["wallet_id"] not in (None, ""):
                        if not isinstance(payload["wallet_id"], str):
                            raise ValidationError({"wallet": "Invalid wallet UUID."})
                        try:
                            w_uuid = uuid.UUID(payload["wallet_id"].strip())
                        except ValueError as exc:
                            raise ValidationError({"wallet": "Invalid wallet UUID."}) from exc
                        wallet = Wallet.objects.filter(pk=w_uuid).first()
                        if wallet is None:
                            raise ValidationError({"wallet": "Wallet not found."})

                    category = tx.category
                    if "category_id" in payload:
                        if payload["category_id"] in (None, ""):
                            category = None
                        else:
                            if not isinstance(payload["category_id"], str):
                                raise ValidationError({"category": "Invalid category UUID."})
                            try:
                                c_uuid = uuid.UUID(payload["category_id"].strip())
                            except ValueError as exc:
                                raise ValidationError({"category": "Invalid category UUID."}) from exc
                            category = Category.objects.filter(pk=c_uuid).first()
                            if category is None:
                                raise ValidationError({"category": "Category not found."})

                    tx_date = (
                        _parse_iso_date_strict(payload["transaction_date"])
                        if payload.get("transaction_date") not in (None, "")
                        else tx.transaction_date
                    )
                    raw_amt = payload.get("amount", tx.amount)
                    if isinstance(raw_amt, (dict, list, bool, float)) or raw_amt in (None, ""):
                        raise ValidationError({"amount": "Enter a valid decimal amount."})

                    raw_manual_rate = payload.get("manual_rate")
                    manual_rate_dec = None
                    if raw_manual_rate not in (None, ""):
                        if isinstance(raw_manual_rate, (dict, list, bool, float)):
                            raise ValidationError({"manual_rate": "Enter a valid decimal exchange rate."})
                        manual_rate_dec = Decimal(str(raw_manual_rate))

                    payer = _resolve_sync_payer(user, payload)

                    updated_tx = update_transaction_entry(
                        user=user,
                        tx=tx,
                        transaction_type=str(payload.get("transaction_type") or tx.transaction_type),
                        amount=Decimal(str(raw_amt)),
                        wallet=wallet,
                        transaction_date=tx_date,
                        category=category,
                        paid_by=payer,
                        description=str(payload.get("description", tx.description) or ""),
                        notes=str(payload.get("notes", tx.notes) or ""),
                        manual_rate=manual_rate_dec,
                        expected_revision=validated_rev,
                        require_revision=True,
                    )
                    outcome = {
                        "mutation_id": mutation_id,
                        "entity_id": str(updated_tx.id),
                        "status": "applied",
                        "revision": updated_tx.revision,
                    }
                    SyncMutationReceipt.objects.create(
                        user=user,
                        mutation_id=mutation_id,
                        request_fingerprint=fingerprint,
                        entity_type="transaction",
                        entity_id=updated_tx.id,
                        status="applied",
                        response_payload=outcome,
                    )
                    results.append(outcome)
                    applied_count += 1

                elif op == "delete_transaction":
                    validated_rev = parse_positive_revision(base_revision, required=True)
                    tx = Transaction.objects.select_for_update().filter(pk=entity_uuid).first()
                    if tx is None:
                        raise ValidationError("Transaction not found.")
                    deleted_tx = delete_transaction_entry(
                        user=user,
                        tx=tx,
                        expected_revision=validated_rev,
                        require_revision=True,
                    )
                    outcome = {
                        "mutation_id": mutation_id,
                        "entity_id": str(deleted_tx.id),
                        "status": "applied",
                        "revision": deleted_tx.revision,
                        "is_deleted": True,
                    }
                    SyncMutationReceipt.objects.create(
                        user=user,
                        mutation_id=mutation_id,
                        request_fingerprint=fingerprint,
                        entity_type="transaction",
                        entity_id=deleted_tx.id,
                        status="applied",
                        response_payload=outcome,
                    )
                    results.append(outcome)
                    applied_count += 1

                else:
                    raise ValidationError(f"Unsupported sync operation: {op}")

        except IntegrityError:
            concurrent_receipt = SyncMutationReceipt.objects.filter(
                user=user,
                mutation_id=mutation_id,
            ).first()
            if concurrent_receipt is not None and concurrent_receipt.request_fingerprint in ("", fingerprint):
                cached = dict(concurrent_receipt.response_payload)
                cached["idempotent_replay"] = True
                results.append(cached)
                applied_count += 1
            else:
                msg = "Concurrent modification or duplicate identifier detected."
                failed_entity_ids.add(entity_key)
                results.append(
                    {
                        "mutation_id": mutation_id,
                        "entity_id": str(entity_uuid),
                        "status": "conflict",
                        "error": msg,
                        "message": msg,
                    }
                )
                conflict_count += 1
        except ConflictError as exc:
            server_tx = Transaction.objects.filter(pk=entity_uuid).first()
            msg = "; ".join(exc.messages)
            failed_entity_ids.add(entity_key)
            outcome = {
                "mutation_id": mutation_id,
                "entity_id": str(entity_uuid),
                "status": "conflict",
                "error": msg,
                "message": msg,
                "server_revision": server_tx.revision if server_tx else None,
                "server_is_deleted": server_tx.is_deleted if server_tx else None,
            }
            results.append(outcome)
            conflict_count += 1
        except PermissionDenied as exc:
            msg = str(exc)
            failed_entity_ids.add(entity_key)
            results.append(
                {
                    "mutation_id": mutation_id,
                    "entity_id": str(entity_uuid),
                    "status": "forbidden",
                    "error": msg,
                    "message": msg,
                }
            )
            error_count += 1
        except (ValidationError, InvalidOperation, ValueError, TypeError) as exc:
            if hasattr(exc, "message_dict"):
                msg = "; ".join(f"{k}: {', '.join(v)}" for k, v in exc.message_dict.items())
            elif hasattr(exc, "messages"):
                msg = "; ".join(exc.messages)
            else:
                msg = str(exc)
            failed_entity_ids.add(entity_key)
            results.append(
                {
                    "mutation_id": mutation_id,
                    "entity_id": str(entity_uuid),
                    "status": "error",
                    "error": msg,
                    "message": msg,
                }
            )
            error_count += 1

    all_succeeded = (error_count == 0 and conflict_count == 0 and blocked_count == 0)
    last_sync_iso = None
    if all_succeeded and is_final_batch:
        pref, _ = UserPreference.objects.get_or_create(user=user)
        now = timezone.now()
        pref.last_sync_at = now
        pref.save(update_fields=["last_sync_at", "updated_at"])
        last_sync_iso = now.isoformat()
    else:
        pref = UserPreference.objects.filter(user=user).first()
        if pref and pref.last_sync_at:
            last_sync_iso = pref.last_sync_at.isoformat()

    if all_succeeded:
        batch_status = "success"
    elif applied_count > 0:
        batch_status = "partial"
    else:
        batch_status = "failed"

    return {
        "batch_status": batch_status,
        "applied_count": applied_count,
        "error_count": error_count,
        "conflict_count": conflict_count,
        "blocked_count": blocked_count,
        "results": results,
        "last_sync_at": last_sync_iso,
    }


@transaction.atomic
def share_personal_transaction(
    *,
    user,
    transaction_id,
    shared_space_id,
    include_notes: bool = False,
) -> PersonalTransactionShare:
    try:
        tx_uuid = uuid.UUID(str(transaction_id))
    except (ValueError, TypeError):
        raise ValidationError("Invalid transaction ID.")
    try:
        space_uuid = uuid.UUID(str(shared_space_id))
    except (ValueError, TypeError):
        raise ValidationError("Invalid shared space ID.")

    tx = Transaction.objects.filter(pk=tx_uuid).first()
    if not tx:
        raise ValidationError("Transaction not found.")
    if tx.owner_id != user.id:
        raise PermissionDenied("You can only share personal transactions that you own.")
    if tx.scope != Transaction.Scope.PERSONAL:
        raise ValidationError("Only personal transactions can be shared.")
    if tx.transaction_type not in (Transaction.Type.INCOME, Transaction.Type.EXPENSE) or tx.transfer_id is not None or tx.is_fee:
        raise ValidationError("Only regular personal income and expense transactions can be shared.")
    if tx.is_deleted:
        raise ValidationError("Deleted transactions cannot be shared.")

    shared_space = SharedSpace.objects.filter(pk=space_uuid).first()
    if not shared_space:
        raise ValidationError("Shared space not found.")

    assert_space_member(user, shared_space)

    partner_membership = (
        SharedSpaceMembership.objects.filter(
            shared_space=shared_space,
            is_active=True,
        )
        .exclude(user=user)
        .select_related("user")
        .first()
    )
    if not partner_membership:
        raise ValidationError("This shared space does not have an active partner.")

    partner = partner_membership.user
    if partner.id == user.id:
        raise ValidationError("You cannot share a personal transaction with yourself.")

    active_share = PersonalTransactionShare.objects.filter(
        transaction=tx,
        shared_space=shared_space,
        recipient=partner,
        is_revoked=False,
    ).first()

    now = timezone.now()
    if active_share:
        if active_share.include_notes != bool(include_notes):
            active_share.include_notes = bool(include_notes)
            active_share.revision += 1
            active_share.updated_at = now
            active_share.save(update_fields=["include_notes", "revision", "updated_at"])
        return active_share

    existing_share = PersonalTransactionShare.objects.filter(
        transaction=tx,
        shared_space=shared_space,
        recipient=partner,
        is_revoked=True,
    ).first()

    if existing_share:
        existing_share.is_revoked = False
        existing_share.revoked_at = None
        existing_share.include_notes = bool(include_notes)
        existing_share.revision += 1
        existing_share.updated_at = now
        existing_share.full_clean()
        existing_share.save(
            update_fields=["is_revoked", "revoked_at", "include_notes", "revision", "updated_at"]
        )
        return existing_share

    share = PersonalTransactionShare(
        transaction=tx,
        shared_space=shared_space,
        owner=user,
        recipient=partner,
        include_notes=bool(include_notes),
    )
    share.full_clean()
    share.save()
    return share


@transaction.atomic
def update_personal_transaction_share(
    *,
    user,
    share_id,
    include_notes: bool,
) -> PersonalTransactionShare:
    try:
        share_uuid = uuid.UUID(str(share_id))
    except (ValueError, TypeError):
        raise ValidationError("Invalid share ID.")

    share = (
        PersonalTransactionShare.objects.select_related("shared_space", "recipient", "transaction")
        .filter(pk=share_uuid)
        .first()
    )
    if not share:
        raise ValidationError("Share record not found.")
    if share.owner_id != user.id:
        raise PermissionDenied("You can only modify your own shares.")
    if share.is_revoked:
        raise ValidationError("Cannot update a revoked share.")

    is_owner_active = SharedSpaceMembership.objects.filter(
        shared_space=share.shared_space,
        user=share.owner,
        is_active=True,
    ).exists()
    if not is_owner_active:
        raise ValidationError("The granting owner is no longer an active member of this shared space.")

    is_recipient_active = SharedSpaceMembership.objects.filter(
        shared_space=share.shared_space,
        user=share.recipient,
        is_active=True,
    ).exists()
    if not is_recipient_active:
        raise ValidationError("The partner is no longer an active member in this shared space.")

    tx = share.transaction
    if tx.is_deleted:
        raise ValidationError("Deleted transactions cannot be updated.")
    if tx.owner_id != user.id or tx.scope != Transaction.Scope.PERSONAL:
        raise ValidationError("Only personal transactions owned by you can be shared.")

    if share.include_notes != bool(include_notes):
        share.include_notes = bool(include_notes)
        share.revision += 1
        share.updated_at = timezone.now()
        share.save(update_fields=["include_notes", "revision", "updated_at"])

    return share


@transaction.atomic
def revoke_personal_transaction_share(
    *,
    user,
    share_id=None,
    transaction_id=None,
    shared_space_id=None,
) -> list[PersonalTransactionShare]:
    now = timezone.now()
    if share_id is not None:
        try:
            share_uuid = uuid.UUID(str(share_id))
        except (ValueError, TypeError):
            raise ValidationError("Invalid share ID.")
        share = PersonalTransactionShare.objects.filter(pk=share_uuid).first()
        if not share:
            raise ValidationError("Share record not found.")
        if share.owner_id != user.id:
            raise PermissionDenied("You can only revoke your own shares.")
        if not share.is_revoked:
            share.is_revoked = True
            share.revoked_at = now
            share.revision += 1
            share.updated_at = now
            share.save(update_fields=["is_revoked", "revoked_at", "revision", "updated_at"])
        return [share]

    if transaction_id is not None:
        try:
            tx_uuid = uuid.UUID(str(transaction_id))
        except (ValueError, TypeError):
            raise ValidationError("Invalid transaction ID.")
        qs = PersonalTransactionShare.objects.filter(
            transaction_id=tx_uuid,
            owner=user,
            is_revoked=False,
        )
        if shared_space_id is not None:
            try:
                space_uuid = uuid.UUID(str(shared_space_id))
            except (ValueError, TypeError):
                raise ValidationError("Invalid shared space ID.")
            qs = qs.filter(shared_space_id=space_uuid)

        revoked = []
        for s in qs:
            s.is_revoked = True
            s.revoked_at = now
            s.revision += 1
            s.updated_at = now
            s.save(update_fields=["is_revoked", "revoked_at", "revision", "updated_at"])
            revoked.append(s)
        return revoked

    raise ValidationError("Either share_id or transaction_id must be provided to revoke sharing.")


@transaction.atomic
def save_personal_balance_share_permission(
    *,
    user,
    shared_space_id,
    show_balance: bool,
    wallet_ids: list,
) -> PersonalBalanceSharePermission:
    try:
        space_uuid = uuid.UUID(str(shared_space_id))
    except (ValueError, TypeError):
        raise ValidationError("Invalid shared space ID.")

    shared_space = SharedSpace.objects.filter(pk=space_uuid).first()
    if not shared_space:
        raise ValidationError("Shared space not found.")

    assert_space_member(user, shared_space)

    partner_membership = (
        SharedSpaceMembership.objects.filter(
            shared_space=shared_space,
            is_active=True,
        )
        .exclude(user=user)
        .select_related("user")
        .first()
    )
    if not partner_membership:
        raise ValidationError("This shared space does not have an active partner.")

    partner = partner_membership.user
    if partner.id == user.id:
        raise ValidationError("You cannot disclose personal balance to yourself.")

    valid_wallets = []
    if wallet_ids:
        for wid in wallet_ids:
            try:
                wid_uuid = uuid.UUID(str(wid))
            except (ValueError, TypeError):
                raise ValidationError("Invalid wallet ID.")
            w = Wallet.objects.filter(pk=wid_uuid).first()
            if not w:
                raise ValidationError(f"Wallet '{wid}' not found.")
            if w.owner_id != user.id:
                raise ValidationError("You can only select personal wallets that you own.")
            if w.ownership_scope != Wallet.OwnershipScope.PERSONAL:
                raise ValidationError("Shared wallets cannot be selected for personal balance sharing.")
            if w.is_archived:
                raise ValidationError(f"Archived wallet '{w.name}' cannot be selected.")
            valid_wallets.append(w)

    perm = PersonalBalanceSharePermission.objects.filter(
        owner=user,
        recipient=partner,
        shared_space=shared_space,
        is_revoked=False,
    ).first()

    now = timezone.now()
    if not perm:
        # Check if there is an inactive/revoked permission to reactivate
        perm = PersonalBalanceSharePermission.objects.filter(
            owner=user,
            recipient=partner,
            shared_space=shared_space,
            is_revoked=True,
        ).first()
        if perm:
            perm.is_revoked = False
            perm.revoked_at = None
            perm.show_balance = bool(show_balance)
            perm.updated_at = now
            perm.save(update_fields=["is_revoked", "revoked_at", "show_balance", "updated_at"])
        else:
            perm = PersonalBalanceSharePermission.objects.create(
                owner=user,
                recipient=partner,
                shared_space=shared_space,
                show_balance=bool(show_balance),
            )
    else:
        perm.show_balance = bool(show_balance)
        perm.updated_at = now
        perm.save(update_fields=["show_balance", "updated_at"])

    perm.selected_wallets.set(valid_wallets)
    return perm


@transaction.atomic
def revoke_personal_balance_share_permission(
    *,
    user,
    shared_space_id,
) -> list[PersonalBalanceSharePermission]:
    try:
        space_uuid = uuid.UUID(str(shared_space_id))
    except (ValueError, TypeError):
        raise ValidationError("Invalid shared space ID.")

    qs = PersonalBalanceSharePermission.objects.filter(
        owner=user,
        shared_space_id=space_uuid,
        is_revoked=False,
    )
    now = timezone.now()
    revoked = []
    for p in qs:
        p.is_revoked = True
        p.revoked_at = now
        p.show_balance = False
        p.updated_at = now
        p.save(update_fields=["is_revoked", "revoked_at", "show_balance", "updated_at"])
        revoked.append(p)
    return revoked


