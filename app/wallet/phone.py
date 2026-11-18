"""Phone numbers are stored in E.164 (`+919876543210`), so formatting can't slip two spellings
of one number past `users_phone_key` (docs/schema.md §4).

Accepted input: an E.164 number with optional spaces, dashes, dots or brackets; a leading `00`
international prefix; or, since the product is India-only (INR), a bare 10-digit Indian mobile
number (first digit 6-9) or one prefixed with `91` / `0`. Anything else is rejected.
"""

import re

E164 = re.compile(r"^\+[1-9][0-9]{7,14}$")  # the same pattern as users_phone_e164_check
_SEPARATORS = re.compile(r"[\s\-.()]")
_INDIAN_MOBILE = re.compile(r"^[6-9][0-9]{9}$")


class InvalidPhoneError(ValueError):
    pass


def normalize_phone(raw: str) -> str:
    """Return the E.164 form of `raw`, or raise InvalidPhoneError."""
    compact = _SEPARATORS.sub("", raw.strip())
    if compact.startswith("00"):
        compact = "+" + compact[2:]

    if compact.startswith("+"):
        candidate = compact
    elif _INDIAN_MOBILE.match(compact):
        candidate = "+91" + compact
    elif len(compact) == 12 and compact.startswith("91") and _INDIAN_MOBILE.match(compact[2:]):
        candidate = "+" + compact
    elif len(compact) == 11 and compact.startswith("0") and _INDIAN_MOBILE.match(compact[1:]):
        candidate = "+91" + compact[1:]  # trunk prefix
    else:
        raise InvalidPhoneError("phone must be in international format, e.g. +919876543210")

    if not E164.match(candidate):
        raise InvalidPhoneError("phone must be in international format, e.g. +919876543210")
    return candidate
