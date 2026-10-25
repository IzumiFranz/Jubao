"""
Initial catalog of supported ISO 4217 currencies for Budget Tracker.

Authoritative Reference Source:
- ISO 4217 Currency Codes standard (maintained by SIX Financial Information AG,
  Official ISO 4217 Maintenance Agency, "List One: Currency, fund and precious
  metal codes"):
  - PHP: Philippine Peso (numeric code 608, minor unit: 2)
  - USD: US Dollar (numeric code 840, minor unit: 2)
  - EUR: Euro (numeric code 978, minor unit: 2)
  - JPY: Japanese Yen (numeric code 392, minor unit: 0)
  - KWD: Kuwaiti Dinar (numeric code 414, minor unit: 3)
  - TWD: New Taiwan dollar (numeric code 901, minor unit: 2; note: NTD is an
    informal abbreviation, whereas TWD is the ISO 4217 code)

Note:
This catalog defines the initial supported subset of currencies for the
application and is not a complete worldwide currency list.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class CurrencySpec:
    code: str
    name: str
    decimal_places: int


SUPPORTED_CURRENCIES: tuple[CurrencySpec, ...] = (
    CurrencySpec(code="EUR", name="Euro", decimal_places=2),
    CurrencySpec(code="JPY", name="Japanese Yen", decimal_places=0),
    CurrencySpec(code="KWD", name="Kuwaiti Dinar", decimal_places=3),
    CurrencySpec(code="PHP", name="Philippine Peso", decimal_places=2),
    CurrencySpec(code="TWD", name="New Taiwan dollar", decimal_places=2),
    CurrencySpec(code="USD", name="US Dollar", decimal_places=2),
)
