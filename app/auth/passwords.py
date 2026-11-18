"""Password hashing with argon2id (argon2-cffi defaults: t=3, m=64 MiB, p=4).

Hashes are self-describing (`$argon2id$v=19$m=…`), so parameters can be raised later and old
hashes upgraded on the next successful login via `needs_rehash`.
"""

from functools import lru_cache

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """True if `password` matches. A malformed stored hash counts as a mismatch."""
    try:
        return _hasher.verify(password_hash, password)
    except VerifyMismatchError, VerificationError, InvalidHashError:
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    return _hasher.hash("not-a-real-password")


def verify_against_dummy(password: str) -> None:
    """Spend the same time as a real verify. Login calls this when the email is unknown, so
    response time doesn't reveal which emails are registered."""
    verify_password(_dummy_hash(), password)
