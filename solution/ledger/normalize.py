"""Pure functions that turn messy input values into canonical ones.

Every function returns None when the value is invalid, so callers decide
whether that means "reject the row" or "store null and count a warning".
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

IST = timezone(timedelta(hours=5, minutes=30))

# ---------------------------------------------------------------- headers


def normalize_header(header: str) -> str:
    """Lower-case, trim, and collapse every run of '_' or whitespace to one space."""
    return re.sub(r"[_\s]+", " ", header.strip().lower()).strip()


# ---------------------------------------------------------------- phone


def normalize_phone(raw: str | None) -> str | None:
    """Return '+91XXXXXXXXXX' or None if this is not a valid Indian mobile."""
    if raw is None:
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10 and digits[0] in "6789":
        return "+91" + digits
    return None


def mask_phone(canonical: str) -> str:
    return "+91******" + canonical[-4:]


# ---------------------------------------------------------------- date

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_DAY_FIRST = re.compile(r"^(\d{2})[/-](\d{2})[/-](\d{4})$")
_DAY_MON = re.compile(r"^(\d{2})-([A-Za-z]{3})-(\d{4})$")


def parse_date(raw: str) -> date | None:
    """Accept YYYY-MM-DD, DD/MM/YYYY, DD-MM-YYYY, DD-Mon-YYYY. Day-first."""
    raw = raw.strip()
    try:
        if m := _ISO.match(raw):
            return date(int(m[1]), int(m[2]), int(m[3]))
        if m := _DAY_FIRST.match(raw):
            # Don't accept mixed separators like 01/12-2026.
            if raw[2] != raw[5]:
                return None
            return date(int(m[3]), int(m[2]), int(m[1]))
        if m := _DAY_MON.match(raw):
            month = _MONTHS.get(m[2].lower())
            if month is None:
                return None
            return date(int(m[3]), month, int(m[1]))
    except ValueError:  # impossible date such as 31/02
        return None
    return None


# ---------------------------------------------------------------- decimal

_CURRENCY_PREFIX = re.compile(r"^(₹|rs\.?|inr)\s*", re.IGNORECASE)
_NUMBER = re.compile(r"^\d{1,3}(,\d{3})+(\.\d+)?$|^\d+(\.\d+)?$")


def parse_decimal(raw: str) -> Decimal | None:
    """Parse '₹27,554', 'Rs. 24787', 'INR 25301.4' etc. Must be >= 0."""
    value = _CURRENCY_PREFIX.sub("", raw.strip(), count=1)
    if not _NUMBER.match(value):  # also rejects '-5' and 'abc'
        return None
    try:
        number = Decimal(value.replace(",", ""))
    except InvalidOperation:
        return None
    return number.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------- enum


def parse_enum(raw: str, allowed: list[str]) -> str | None:
    value = raw.strip().lower()
    return value if value in {v.lower() for v in allowed} else None


# ---------------------------------------------------------------- timestamps


def parse_timestamp(raw: str) -> datetime | None:
    """Parse an event timestamp to an aware datetime in IST.

    '...Z' is UTC, '...+05:30' is an offset, and a naive value means IST.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST)
