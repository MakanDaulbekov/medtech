"""
Validation rules from the spec:
1. Price > 0 and is numeric
2. NonResident price >= Resident price
3. Service name not empty
4. Price date not in future
5. Dedup: same partner + service + date → keep new, archive old
6. Price change > 50% → anomaly flag
7. Non-KZT → convert (stub: use 1.0 rate; hook for real API)
"""
import logging
from datetime import date
from decimal import Decimal
from typing import Optional

logger = logging.getLogger(__name__)

# Stub exchange rates (replace with real API call)
STUB_RATES = {"USD": Decimal("450"), "RUB": Decimal("4.9"), "KZT": Decimal("1")}


def convert_to_kzt(amount: Optional[Decimal], currency: str) -> Optional[Decimal]:
    if amount is None:
        return None
    rate = STUB_RATES.get(currency, Decimal("1"))
    return amount * rate


def validate_price_item(item: dict, effective_date: Optional[date]) -> dict:
    """
    Validates and enriches a raw parsed item dict.
    Returns same dict with added keys:
      - validation_warnings: list[str]
      - is_valid: bool
    """
    warnings = []

    price_r = item.get("price_resident_kzt")
    price_nr = item.get("price_nonresident_kzt")
    currency = item.get("currency_original", "KZT")
    name = item.get("service_name_raw", "").strip()

    # Rule 1: name not empty
    if not name or len(name) < 2:
        return {**item, "is_valid": False, "validation_warnings": ["Empty service name"]}

    # Rule 3: price > 0
    if price_r is not None:
        if not isinstance(price_r, Decimal):
            price_r = Decimal(str(price_r))
        if price_r <= 0:
            warnings.append("Resident price <= 0")
            price_r = None

    if price_nr is not None:
        if not isinstance(price_nr, Decimal):
            price_nr = Decimal(str(price_nr))
        if price_nr <= 0:
            warnings.append("Non-resident price <= 0")
            price_nr = None

    # Rule 7: currency conversion
    if currency != "KZT":
        if price_r is not None:
            price_r = convert_to_kzt(price_r, currency)
        if price_nr is not None:
            price_nr = convert_to_kzt(price_nr, currency)
        warnings.append(f"Converted from {currency} to KZT (stub rate)")

    # Rule 2: nonresident >= resident
    if price_r and price_nr and price_nr < price_r:
        warnings.append(f"NonResident ({price_nr}) < Resident ({price_r})")

    # Rule 4: effective date not in future
    if effective_date and effective_date > date.today():
        warnings.append(f"Effective date {effective_date} is in the future")

    item = {
        **item,
        "price_resident_kzt": price_r,
        "price_nonresident_kzt": price_nr,
        "currency_original": currency,
        "validation_warnings": warnings,
        "is_valid": True,
    }
    return item


def check_price_anomaly(
    new_price: Optional[Decimal],
    prev_price: Optional[Decimal],
    threshold_pct: float = 50.0,
) -> bool:
    """Return True if price changed more than threshold_pct %."""
    if new_price is None or prev_price is None or prev_price == 0:
        return False
    change_pct = abs((new_price - prev_price) / prev_price) * 100
    return change_pct > threshold_pct
