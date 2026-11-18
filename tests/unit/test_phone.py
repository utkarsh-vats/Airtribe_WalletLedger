"""Step 2.1b: phone numbers normalize to E.164 or are rejected."""

import pytest

from app.wallet.phone import E164, InvalidPhoneError, normalize_phone


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+919876543210", "+919876543210"),
        ("+91 98765 43210", "+919876543210"),
        ("+91-98765-43210", "+919876543210"),
        ("+91 (98765) 43210", "+919876543210"),
        (" +91.98765.43210 ", "+919876543210"),
        ("0091 98765 43210", "+919876543210"),
        ("9876543210", "+919876543210"),  # bare Indian mobile
        ("98765 43210", "+919876543210"),
        ("919876543210", "+919876543210"),  # country code without +
        ("09876543210", "+919876543210"),  # trunk prefix
        ("+14155552671", "+14155552671"),  # non-Indian numbers need the +
        ("+442071838750", "+442071838750"),
    ],
)
def test_normalizes_to_e164(raw: str, expected: str) -> None:
    assert normalize_phone(raw) == expected
    assert E164.match(expected)


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "12345",
        "5876543210",  # 10 digits but not an Indian mobile (starts with 5)
        "4155552671",  # US number without country code: ambiguous
        "+0919876543210",  # country code can't start with 0
        "+1234567",  # too short
        "+1234567890123456",  # too long
        "+91 98765 4321O",  # letter O
        "++919876543210",
        "+91 98765 43210 ext 5",
    ],
)
def test_rejects_non_e164(raw: str) -> None:
    with pytest.raises(InvalidPhoneError):
        normalize_phone(raw)
