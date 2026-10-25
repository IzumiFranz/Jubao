from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone as dt_timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import Currency, ExchangeRate, SharedSpace, SharedSpaceMembership


FRANKFURTER_ENDPOINT = "https://api.frankfurter.app/latest"
FRANKFURTER_V2_ENDPOINT = "https://api.frankfurter.dev/v2/rates"
OPENEXCHANGERATES_ENDPOINT = "https://openexchangerates.org/api/latest.json"

FRANKFURTER_SUPPORTED_CODES = frozenset({"EUR", "JPY", "KWD", "PHP", "TWD", "USD"})
REQUEST_TIMEOUT_SECONDS = 5
REFRESH_COOLDOWN = timedelta(minutes=15)
STALE_THRESHOLD = timedelta(hours=48)
RATE_QUANTUM = Decimal("0.00000001")


class ExchangeRateRefreshError(Exception):
    pass


@dataclass(frozen=True)
class RateLookupResult:
    from_code: str
    to_code: str
    rate: Decimal | None
    provider: str
    rate_timestamp: datetime | None
    retrieved_at: datetime | None
    is_manual: bool
    is_stale: bool
    age_hours: int | None
    status: str  # "identity", "available", "stale", "unsupported", "missing"

    @property
    def status_label(self) -> str:
        if self.status == "identity":
            return "Same currency (1:1)"
        if self.status == "available":
            return f"Available ({self.provider})"
        if self.status == "stale":
            return f"Stale rate ({self.age_hours}h old, {self.provider})"
        if self.status == "unsupported":
            return "Provider does not cover this currency pair (use manual rate)"
        return "Exchange rate unavailable"


def get_configured_provider_info():
    oxr_app_id = (os.environ.get("OPENEXCHANGERATES_APP_ID") or "").strip()
    if oxr_app_id:
        supported = ["EUR", "JPY", "KWD", "PHP", "TWD", "USD"]
        try:
            active_codes = list(Currency.objects.filter(is_active=True).values_list("code", flat=True))
            unsupported = sorted([c for c in active_codes if c not in supported])
        except Exception:
            unsupported = []
        return {
            "key": "openexchangerates",
            "name": "Open Exchange Rates (hourly reference feed)",
            "host": "openexchangerates.org",
            "cadence": "Hourly reference updates (free plan: 1,000 requests/month, USD base).",
            "coverage_note": "Supports EUR, JPY, KWD, PHP, TWD, and USD.",
            "attribution": "Rates provided by Open Exchange Rates (openexchangerates.org).",
            "update_frequency": "Hourly reference updates (free plan: 1,000 requests/month, USD base).",
            "supported_codes": supported,
            "unsupported_codes": unsupported,
        }

    supported = sorted(FRANKFURTER_SUPPORTED_CODES)
    try:
        active_codes = list(Currency.objects.filter(is_active=True).values_list("code", flat=True))
        unsupported = sorted([c for c in active_codes if c not in supported])
    except Exception:
        unsupported = []
    return {
        "key": "frankfurter_v2",
        "name": "Frankfurter (daily reference rates)",
        "host": "api.frankfurter.dev",
        "cadence": "Daily reference rates published by central banks and official sources (not real-time).",
        "coverage_note": "Covers EUR, JPY, KWD, PHP, TWD, and USD.",
        "attribution": "Reference rates provided by Frankfurter (api.frankfurter.dev).",
        "update_frequency": "Daily reference rates published by central banks and official sources (not real-time).",
        "supported_codes": supported,
        "unsupported_codes": unsupported,
    }


def quantize_money(amount: Decimal, currency: Currency) -> Decimal:
    quantum = Decimal(10) ** (-currency.decimal_places)
    return Decimal(amount).quantize(quantum, rounding=ROUND_HALF_UP)


def convert_decimal_amount(amount: Decimal, rate: Decimal, target_currency: Currency) -> Decimal:
    if isinstance(amount, float) or isinstance(rate, float):
        raise ValueError("Financial conversion requires Decimal values, not float.")
    dec_amount = Decimal(str(amount))
    dec_rate = Decimal(str(rate))
    if not dec_amount.is_finite() or not dec_rate.is_finite() or dec_rate <= Decimal("0"):
        raise ValueError("Amount and exchange rate must be finite and rate must be positive.")
    return quantize_money(dec_amount * dec_rate, target_currency)


def _build_authorized_rate_filter(*, user=None, scope: str = "personal", shared_space: SharedSpace | None = None) -> Q:
    """
    Builds a strict QuerySet filter that always includes global provider rates
    and ONLY includes manual rates belonging to the caller's explicit authorized
    user/scope/shared_space context. Unscoped legacy manual rates are never eligible.
    """
    allowed_q = Q(is_manual=False, scope=ExchangeRate.Scope.PROVIDER)
    if user is None or not getattr(user, "is_authenticated", False):
        return allowed_q

    if scope == "shared":
        if shared_space is not None:
            is_member = SharedSpaceMembership.objects.filter(
                shared_space=shared_space,
                user=user,
                is_active=True,
            ).exists()
            if is_member:
                allowed_q |= Q(
                    is_manual=True,
                    scope=ExchangeRate.Scope.SHARED,
                    shared_space=shared_space,
                )
    elif scope == "personal":
        allowed_q |= Q(
            is_manual=True,
            scope=ExchangeRate.Scope.PERSONAL,
            created_by=user,
            shared_space__isnull=True,
        )
    return allowed_q


def get_accessible_exchange_rates_qs(user):
    allowed_q = Q(is_manual=False, scope=ExchangeRate.Scope.PROVIDER)
    if user is not None and getattr(user, "is_authenticated", False):
        space_ids = list(
            SharedSpaceMembership.objects.filter(user=user, is_active=True).values_list(
                "shared_space_id", flat=True
            )
        )
        allowed_q |= Q(
            is_manual=True,
            scope=ExchangeRate.Scope.PERSONAL,
            created_by=user,
            shared_space__isnull=True,
        )
        if space_ids:
            allowed_q |= Q(
                is_manual=True,
                scope=ExchangeRate.Scope.SHARED,
                shared_space_id__in=space_ids,
            )
    return ExchangeRate.objects.filter(allowed_q).select_related(
        "from_currency", "to_currency", "shared_space", "created_by"
    )


def _derive_cached_same_date_rate(
    *,
    from_currency: Currency,
    to_currency: Currency,
    auth_filter: Q,
    for_date=None,
    require_date_match: bool = False,
) -> RateLookupResult | None:
    now = timezone.now()

    # 1. Safe inverse: to_currency -> from_currency
    inv_qs = ExchangeRate.objects.filter(
        auth_filter,
        from_currency=to_currency,
        to_currency=from_currency,
    )
    if for_date is not None and require_date_match:
        inv_qs = inv_qs.filter(rate_timestamp__date=for_date)
    rec_inv = inv_qs.order_by("-rate_timestamp", "-retrieved_at").first()
    if rec_inv and rec_inv.rate and rec_inv.rate > Decimal("0"):
        derived_rate = (Decimal("1") / rec_inv.rate).quantize(RATE_QUANTUM, rounding=ROUND_HALF_UP)
        if derived_rate > Decimal("0"):
            age_delta = max(timedelta(0), now - rec_inv.rate_timestamp)
            age_hours = int(age_delta.total_seconds() // 3600)
            is_stale = age_delta > STALE_THRESHOLD
            return RateLookupResult(
                from_code=from_currency.code,
                to_code=to_currency.code,
                rate=derived_rate,
                provider=rec_inv.provider,
                rate_timestamp=rec_inv.rate_timestamp,
                retrieved_at=rec_inv.retrieved_at,
                is_manual=rec_inv.is_manual,
                is_stale=is_stale,
                age_hours=age_hours,
                status="stale" if is_stale else "available",
            )

    # 2. Pivot: find a pivot currency P where P -> from_curr and P -> to_curr exist with identical observation date
    pivot_qs = Currency.objects.filter(is_active=True).exclude(pk__in=[from_currency.pk, to_currency.pk])
    for pivot in pivot_qs:
        qa = ExchangeRate.objects.filter(auth_filter, from_currency=pivot, to_currency=from_currency)
        qb = ExchangeRate.objects.filter(auth_filter, from_currency=pivot, to_currency=to_currency)
        if for_date is not None and require_date_match:
            qa = qa.filter(rate_timestamp__date=for_date)
            qb = qb.filter(rate_timestamp__date=for_date)

        rec_a = qa.order_by("-rate_timestamp", "-retrieved_at").first()
        rec_b = qb.order_by("-rate_timestamp", "-retrieved_at").first()

        if rec_a and rec_b and rec_a.rate and rec_b.rate:
            if rec_a.rate_timestamp.date() == rec_b.rate_timestamp.date():
                if rec_a.rate > Decimal("0") and rec_b.rate > Decimal("0"):
                    cross_rate = (rec_b.rate / rec_a.rate).quantize(RATE_QUANTUM, rounding=ROUND_HALF_UP)
                    if cross_rate > Decimal("0"):
                        effective_ts = min(rec_a.rate_timestamp, rec_b.rate_timestamp)
                        retrieved_ts = (
                            min(rec_a.retrieved_at, rec_b.retrieved_at)
                            if (rec_a.retrieved_at and rec_b.retrieved_at)
                            else (rec_a.retrieved_at or rec_b.retrieved_at)
                        )
                        age_delta = max(timedelta(0), now - effective_ts)
                        age_hours = int(age_delta.total_seconds() // 3600)
                        is_stale = age_delta > STALE_THRESHOLD
                        provider_label = (
                            rec_a.provider if rec_a.provider == rec_b.provider else "cross"
                        )
                        is_manual = rec_a.is_manual or rec_b.is_manual
                        return RateLookupResult(
                            from_code=from_currency.code,
                            to_code=to_currency.code,
                            rate=cross_rate,
                            provider=provider_label,
                            rate_timestamp=effective_ts,
                            retrieved_at=retrieved_ts,
                            is_manual=is_manual,
                            is_stale=is_stale,
                            age_hours=age_hours,
                            status="stale" if is_stale else "available",
                        )

    return None


def get_latest_rate(
    from_currency: Currency,
    to_currency: Currency,
    *,
    user=None,
    scope: str = "personal",
    shared_space: SharedSpace | None = None,
    for_date=None,
    require_date_match: bool = False,
) -> RateLookupResult:
    if from_currency.pk == to_currency.pk:
        now = timezone.now()
        return RateLookupResult(
            from_code=from_currency.code,
            to_code=to_currency.code,
            rate=Decimal("1.00000000"),
            provider="identity",
            rate_timestamp=now,
            retrieved_at=now,
            is_manual=False,
            is_stale=False,
            age_hours=0,
            status="identity",
        )

    auth_filter = _build_authorized_rate_filter(
        user=user,
        scope=scope,
        shared_space=shared_space,
    )
    qs = ExchangeRate.objects.filter(
        auth_filter,
        from_currency=from_currency,
        to_currency=to_currency,
    )

    if for_date is not None and require_date_match:
        qs = qs.filter(rate_timestamp__date=for_date)

    record = qs.order_by("-rate_timestamp", "-retrieved_at").first()
    if record is None:
        derived = _derive_cached_same_date_rate(
            from_currency=from_currency,
            to_currency=to_currency,
            auth_filter=auth_filter,
            for_date=for_date,
            require_date_match=require_date_match,
        )
        if derived is not None:
            return derived

        provider_info = get_configured_provider_info()
        if (
            from_currency.code in provider_info["unsupported_codes"]
            or to_currency.code in provider_info["unsupported_codes"]
        ):
            status = "unsupported"
        else:
            status = "missing"
        return RateLookupResult(
            from_code=from_currency.code,
            to_code=to_currency.code,
            rate=None,
            provider="",
            rate_timestamp=None,
            retrieved_at=None,
            is_manual=False,
            is_stale=False,
            age_hours=None,
            status=status,
        )

    now = timezone.now()
    age_delta = max(timedelta(0), now - record.rate_timestamp)
    age_hours = int(age_delta.total_seconds() // 3600)
    is_stale = age_delta > STALE_THRESHOLD
    return RateLookupResult(
        from_code=from_currency.code,
        to_code=to_currency.code,
        rate=record.rate,
        provider=record.provider,
        rate_timestamp=record.rate_timestamp,
        retrieved_at=record.retrieved_at,
        is_manual=record.is_manual,
        is_stale=is_stale,
        age_hours=age_hours,
        status="stale" if is_stale else "available",
    )


@transaction.atomic
def record_manual_exchange_rate(
    *,
    user,
    from_currency: Currency,
    to_currency: Currency,
    rate: Decimal,
    scope: str = ExchangeRate.Scope.PERSONAL,
    shared_space: SharedSpace | None = None,
    effective_date=None,
    rate_timestamp=None,
) -> ExchangeRate:
    if user is None or not getattr(user, "is_authenticated", False):
        raise PermissionDenied("Authentication is required to record a manual exchange rate.")
    if from_currency.pk == to_currency.pk:
        raise ValidationError("Source and target currencies must be different for a manual rate.")
    if scope not in (ExchangeRate.Scope.PERSONAL, ExchangeRate.Scope.SHARED):
        raise ValidationError({"scope": "Manual exchange rate scope must be Personal or Shared."})

    resolved_space = None
    if scope == ExchangeRate.Scope.SHARED:
        if shared_space is None:
            membership = (
                SharedSpaceMembership.objects.select_related("shared_space")
                .filter(user=user, is_active=True)
                .order_by("joined_at")
                .first()
            )
            if membership is None:
                raise PermissionDenied("You must belong to an active shared space to record a shared rate.")
            resolved_space = membership.shared_space
        else:
            is_member = SharedSpaceMembership.objects.filter(
                shared_space=shared_space,
                user=user,
                is_active=True,
            ).exists()
            if not is_member:
                raise PermissionDenied("You are not an active member of this shared space.")
            resolved_space = shared_space

    if isinstance(rate, float):
        raise ValidationError({"rate": "Exchange rate must be a decimal value, not float."})
    try:
        dec_rate = rate if isinstance(rate, Decimal) else Decimal(str(rate))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationError({"rate": "Enter a valid decimal exchange rate."}) from exc

    if not dec_rate.is_finite() or dec_rate <= Decimal("0"):
        raise ValidationError({"rate": "Exchange rate must be greater than zero."})

    quantized_rate = dec_rate.quantize(RATE_QUANTUM, rounding=ROUND_HALF_UP)
    if quantized_rate <= Decimal("0"):
        raise ValidationError({"rate": "Exchange rate is too small."})

    if rate_timestamp is not None:
        rate_ts = rate_timestamp
    elif effective_date is not None:
        rate_ts = datetime.combine(effective_date, time(12, 0), tzinfo=dt_timezone.utc)
    else:
        rate_ts = timezone.now()

    entry = ExchangeRate(
        from_currency=from_currency,
        to_currency=to_currency,
        rate=quantized_rate,
        provider="manual",
        is_manual=True,
        scope=scope,
        shared_space=resolved_space,
        rate_timestamp=rate_ts,
        created_by=user,
    )
    entry.full_clean()
    entry.save()
    return entry


def _parse_positive_decimal(raw_val, label="rate") -> Decimal:
    try:
        dec = Decimal(str(raw_val))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ExchangeRateRefreshError(f"Invalid {label} received from provider.") from exc
    if not dec.is_finite() or dec <= Decimal("0"):
        raise ExchangeRateRefreshError(f"Non-positive or non-finite {label} received from provider.")
    return dec


def _fetch_json_url(url: str):
    req = Request(url, headers={"Accept": "application/json", "User-Agent": "BudgetTracker/1.0"})
    try:
        with urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            if resp.status == 429:
                raise ExchangeRateRefreshError("Exchange-rate provider rate limit reached. Try again later.")
            if resp.status != 200:
                raise ExchangeRateRefreshError(f"Provider returned HTTP {resp.status}.")
            raw_body = resp.read().decode("utf-8")
    except HTTPError as exc:
        if exc.code == 429:
            raise ExchangeRateRefreshError("Exchange-rate provider rate limit reached. Try again later.") from exc
        raise ExchangeRateRefreshError(f"Exchange-rate provider returned HTTP {exc.code}.") from exc
    except URLError as exc:
        raise ExchangeRateRefreshError("Unable to reach the exchange-rate provider.") from exc
    except TimeoutError as exc:
        raise ExchangeRateRefreshError("Exchange-rate provider request timed out.") from exc

    try:
        payload = json.loads(raw_body, parse_float=Decimal)
    except ValueError as exc:
        raise ExchangeRateRefreshError("Invalid JSON response from exchange-rate provider.") from exc

    if not isinstance(payload, (dict, list)):
        raise ExchangeRateRefreshError("Unexpected response structure from exchange-rate provider.")
    return payload


@transaction.atomic
def refresh_exchange_rates(*, user=None, base_currency_code: str = "PHP", force: bool = False, http_fetcher=None) -> dict:
    fetcher = http_fetcher or _fetch_json_url
    now = timezone.now()

    oxr_app_id = (os.environ.get("OPENEXCHANGERATES_APP_ID") or "").strip()
    target_provider = "openexchangerates" if oxr_app_id else "frankfurter_v2"

    latest_auto = (
        ExchangeRate.objects.filter(
            is_manual=False,
            scope=ExchangeRate.Scope.PROVIDER,
            provider=target_provider,
        )
        .order_by("-retrieved_at")
        .first()
    )
    if not force and latest_auto and (now - latest_auto.retrieved_at) < REFRESH_COOLDOWN:
        return {
            "status": "cooldown",
            "message": "Exchange rates were refreshed recently. Using cached provider rates.",
            "refreshed": False,
            "cached": True,
            "pairs_stored": 0,
            "provider": latest_auto.provider,
            "rate_timestamp": latest_auto.rate_timestamp,
            "retrieved_at": latest_auto.retrieved_at,
        }

    active_currencies = {
        c.code: c for c in Currency.objects.filter(is_active=True)
    }
    if len(active_currencies) < 2:
        raise ExchangeRateRefreshError("At least two active currencies are required to refresh rates.")

    try:
        rates_to_store = []  # list of (from_curr, to_curr, rate_val, eff_date, rate_ts)
        provider_key = target_provider
        latest_effective_ts = None

        if oxr_app_id:
            symbols = ",".join(sorted(active_currencies.keys()))
            query = urlencode({"app_id": oxr_app_id, "symbols": symbols})
            payload = fetcher(f"{OPENEXCHANGERATES_ENDPOINT}?{query}")
            if not isinstance(payload, dict):
                raise ExchangeRateRefreshError("Provider response did not include a valid rates structure.")
            raw_rates = payload.get("rates")
            if not isinstance(raw_rates, dict):
                raise ExchangeRateRefreshError("Provider response did not include a rates mapping.")
            ts_epoch = payload.get("timestamp")
            if isinstance(ts_epoch, (int, float)):
                oxr_date = datetime.fromtimestamp(int(ts_epoch), tz=dt_timezone.utc).date()
            else:
                oxr_date = now.date()
            rate_ts = datetime.combine(oxr_date, time.min, tzinfo=dt_timezone.utc)
            latest_effective_ts = rate_ts

            usd_rates = {"USD": Decimal("1")}
            for code, val in raw_rates.items():
                code = str(code).strip().upper()
                if code in active_currencies:
                    usd_rates[code] = _parse_positive_decimal(val, label=f"rate for {code}")

            codes = sorted(usd_rates.keys())
            for from_code in codes:
                for to_code in codes:
                    if from_code == to_code:
                        continue
                    cross_rate = (usd_rates[to_code] / usd_rates[from_code]).quantize(
                        RATE_QUANTUM, rounding=ROUND_HALF_UP
                    )
                    if cross_rate > Decimal("0"):
                        rates_to_store.append((
                            active_currencies[from_code],
                            active_currencies[to_code],
                            cross_rate,
                            oxr_date,
                            rate_ts,
                        ))
        else:
            base_code = (
                base_currency_code
                if (base_currency_code and base_currency_code in active_currencies)
                else ("PHP" if "PHP" in active_currencies else ("USD" if "USD" in active_currencies else sorted(active_currencies.keys())[0]))
            )
            quotes = [c for c in sorted(active_currencies.keys()) if c != base_code]
            query = urlencode({"base": base_code, "quotes": ",".join(quotes)})
            payload = fetcher(f"{FRANKFURTER_V2_ENDPOINT}?{query}")

            quotes_data = {}  # quote_code -> {"rate": Decimal, "date": date, "timestamp": datetime}

            if isinstance(payload, list):
                for item in payload:
                    if not isinstance(item, dict):
                        raise ExchangeRateRefreshError("Provider response contains non-object items.")
                    item_base = item.get("base")
                    if not item_base or str(item_base).strip().upper() != base_code:
                        raise ExchangeRateRefreshError(f"Provider quote base '{item_base}' does not match expected base '{base_code}'.")
                    quote_code = item.get("quote")
                    if not quote_code or not isinstance(quote_code, str):
                        raise ExchangeRateRefreshError("Provider quote item missing quote currency.")
                    quote_code = quote_code.strip().upper()
                    if quote_code not in active_currencies or quote_code == base_code:
                        continue
                    rate_val = _parse_positive_decimal(item.get("rate"), label=f"rate for {quote_code}")
                    date_raw = item.get("date")
                    if not date_raw or not isinstance(date_raw, str) or not date_raw.strip():
                        raise ExchangeRateRefreshError(f"Provider quote for {quote_code} is missing an effective date.")
                    try:
                        quote_date = datetime.strptime(date_raw.strip(), "%Y-%m-%d").date()
                    except ValueError as exc:
                        raise ExchangeRateRefreshError(f"Invalid date format '{date_raw}' received from provider for {quote_code}.") from exc
                    quote_ts = datetime.combine(quote_date, time.min, tzinfo=dt_timezone.utc)
                    quotes_data[quote_code] = {
                        "rate": rate_val,
                        "date": quote_date,
                        "timestamp": quote_ts,
                    }
            elif isinstance(payload, dict):
                raw_rates = payload.get("rates")
                if not isinstance(raw_rates, dict):
                    raise ExchangeRateRefreshError("Provider response did not include a rates mapping.")
                date_raw = payload.get("date")
                if date_raw and isinstance(date_raw, str):
                    try:
                        parsed_date = datetime.strptime(date_raw.strip(), "%Y-%m-%d").date()
                    except ValueError as exc:
                        raise ExchangeRateRefreshError(f"Invalid date format '{date_raw}' in provider response.") from exc
                else:
                    ts_epoch = payload.get("timestamp")
                    if isinstance(ts_epoch, (int, float)):
                        parsed_date = datetime.fromtimestamp(int(ts_epoch), tz=dt_timezone.utc).date()
                    else:
                        raise ExchangeRateRefreshError("Provider response is missing an effective date.")
                shared_ts = datetime.combine(parsed_date, time.min, tzinfo=dt_timezone.utc)
                for code, val in raw_rates.items():
                    code = str(code).strip().upper()
                    if code in active_currencies and code != base_code:
                        rate_val = _parse_positive_decimal(val, label=f"rate for {code}")
                        quotes_data[code] = {
                            "rate": rate_val,
                            "date": parsed_date,
                            "timestamp": shared_ts,
                        }
            else:
                raise ExchangeRateRefreshError("Unexpected response structure from exchange-rate provider.")

            if len(quotes_data) == 0:
                raise ExchangeRateRefreshError("Insufficient rate quotes returned by provider.")

            # 1. Direct quotes from base
            for quote_code, qdata in quotes_data.items():
                rates_to_store.append((
                    active_currencies[base_code],
                    active_currencies[quote_code],
                    qdata["rate"].quantize(RATE_QUANTUM, rounding=ROUND_HALF_UP),
                    qdata["date"],
                    qdata["timestamp"],
                ))
                if latest_effective_ts is None or qdata["timestamp"] > latest_effective_ts:
                    latest_effective_ts = qdata["timestamp"]

            # 2. Direct inverse quotes to base
            for quote_code, qdata in quotes_data.items():
                inv_rate = (Decimal("1") / qdata["rate"]).quantize(RATE_QUANTUM, rounding=ROUND_HALF_UP)
                if inv_rate > Decimal("0"):
                    rates_to_store.append((
                        active_currencies[quote_code],
                        active_currencies[base_code],
                        inv_rate,
                        qdata["date"],
                        qdata["timestamp"],
                    ))

            # 3. Safe cross-rates between quotes: ONLY when observation dates match!
            quote_codes = sorted(quotes_data.keys())
            for code_a in quote_codes:
                for code_b in quote_codes:
                    if code_a == code_b:
                        continue
                    data_a = quotes_data[code_a]
                    data_b = quotes_data[code_b]
                    if data_a["date"] != data_b["date"]:
                        # Mismatched effective dates; do not synthesize an invalid same-date cross rate
                        continue
                    cross_rate = (data_b["rate"] / data_a["rate"]).quantize(
                        RATE_QUANTUM, rounding=ROUND_HALF_UP
                    )
                    if cross_rate > Decimal("0"):
                        rates_to_store.append((
                            active_currencies[code_a],
                            active_currencies[code_b],
                            cross_rate,
                            data_a["date"],
                            data_a["timestamp"],
                        ))

    except ExchangeRateRefreshError as exc:
        return {
            "status": "error",
            "message": str(exc),
            "refreshed": False,
            "cached": False,
            "pairs_stored": 0,
            "provider": "",
            "rate_timestamp": None,
            "retrieved_at": None,
        }

    stored_count = 0
    for from_curr, to_curr, rate_val, eff_date, rate_ts in rates_to_store:
        existing = ExchangeRate.objects.filter(
            from_currency=from_curr,
            to_currency=to_curr,
            provider=provider_key,
            scope=ExchangeRate.Scope.PROVIDER,
            is_manual=False,
            rate_timestamp__date=eff_date,
        ).first()

        if existing:
            existing.rate = rate_val
            existing.rate_timestamp = rate_ts
            existing.retrieved_at = now
            existing.save(update_fields=["rate", "rate_timestamp", "retrieved_at"])
        else:
            ExchangeRate.objects.create(
                from_currency=from_curr,
                to_currency=to_curr,
                rate=rate_val,
                provider=provider_key,
                is_manual=False,
                scope=ExchangeRate.Scope.PROVIDER,
                shared_space=None,
                rate_timestamp=rate_ts,
                retrieved_at=now,
                created_by=user if user and getattr(user, "is_authenticated", False) else None,
            )
        stored_count += 1

    return {
        "status": "updated",
        "message": f"Updated {stored_count} exchange-rate pairs from {provider_key}.",
        "refreshed": True,
        "cached": False,
        "pairs_stored": stored_count,
        "provider": provider_key,
        "rate_timestamp": latest_effective_ts or now,
        "retrieved_at": now,
    }

    return {
        "status": "updated",
        "message": f"Updated {stored_count} exchange-rate pairs from {provider_key}.",
        "refreshed": True,
        "cached": False,
        "pairs_stored": stored_count,
        "provider": provider_key,
        "rate_timestamp": rate_ts,
        "retrieved_at": now,
    }
