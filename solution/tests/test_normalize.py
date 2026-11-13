from datetime import date
from decimal import Decimal

import pytest

from ledger.normalize import (
    mask_phone,
    normalize_header,
    normalize_phone,
    parse_date,
    parse_decimal,
    parse_enum,
    parse_timestamp,
)


@pytest.mark.parametrize("raw", [
    "9876543210", "+919876543210", "+91 98765 43210", "91-98765-43210",
    "(+91) 9876543210", "09876543210", " 98765-43210 ",
])
def test_phone_formats_all_normalise_to_same_value(raw):
    assert normalize_phone(raw) == "+919876543210"


@pytest.mark.parametrize("raw", [
    "", "12345", "5876543210",       # starts with 5
    "919876543210000",               # too long
    "19876543210",                   # 11 digits not starting with 0
    "abc", None,
])
def test_invalid_phones(raw):
    assert normalize_phone(raw) is None


def test_mask_phone():
    assert mask_phone("+919876543210") == "+91******3210"


def test_header_normalisation():
    assert normalize_header("  Mobile__No ") == "mobile no"
    assert normalize_header("Policy\t No") == "policy no"


@pytest.mark.parametrize("raw,expected", [
    ("2026-12-01", date(2026, 12, 1)),
    ("01/12/2026", date(2026, 12, 1)),   # day-first
    ("01-12-2026", date(2026, 12, 1)),
    ("01-Dec-2026", date(2026, 12, 1)),
    ("01-dec-2026", date(2026, 12, 1)),
])
def test_valid_dates(raw, expected):
    assert parse_date(raw) == expected


@pytest.mark.parametrize("raw", ["31/02/2026", "2026-13-01", "01-Foo-2026", "1/12/2026", "12/01-2026", "tomorrow"])
def test_invalid_dates(raw):
    assert parse_date(raw) is None


@pytest.mark.parametrize("raw,expected", [
    ("₹27,554", "27554.00"), ("Rs. 24787", "24787.00"), ("rs24787", "24787.00"),
    ("INR 25301.4", "25301.40"), ("4737.145", "4737.15"), ("0", "0.00"),
    ("1,00,000", None),  # Indian lakh grouping is not plain thousands grouping
])
def test_decimals(raw, expected):
    result = parse_decimal(raw)
    assert (str(result) if result is not None else None) == expected


@pytest.mark.parametrize("raw", ["-5", "abc", "₹", "12,34"])
def test_invalid_decimals(raw):
    assert parse_decimal(raw) is None


def test_enum_is_case_insensitive_and_lower_cased():
    assert parse_enum(" HI ", ["en", "hi"]) == "hi"
    assert parse_enum("Hindi", ["en", "hi"]) is None


def test_timestamps_all_end_up_in_ist():
    assert parse_timestamp("2026-10-05T13:20:21Z").isoformat() == "2026-10-05T18:50:21+05:30"
    assert parse_timestamp("2026-10-05T10:00:00+05:30").isoformat() == "2026-10-05T10:00:00+05:30"
    # naive means IST
    assert parse_timestamp("2026-10-03 10:01:00").isoformat() == "2026-10-03T10:01:00+05:30"
    # UTC late evening is the next IST date
    assert parse_timestamp("2026-10-05T20:00:00Z").date() == date(2026, 10, 6)
    assert parse_timestamp("garbage") is None
