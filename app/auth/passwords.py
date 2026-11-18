"""Password hashing with argon2id at OWASP's baseline: m=19456 KiB (19 MiB), t=2, p=1.

Hashes are self-describing (`$argon2id$v=19$m=19456,t=2,p=1$…`), so parameters can change
later; `needs_rehash` tells login to upgrade an older hash after a successful verify.

argon2 is deliberately slow and memory-hard, so the public functions are async: each call runs
in a worker thread (never on the event loop), and at most `MAX_CONCURRENT_HASHES` run at once.
That caps argon2 memory at 4 x 19 MiB (about 76 MiB) however many logins arrive together; the rest
wait their turn without blocking other requests.
"""

from functools import lru_cache

from anyio import CapacityLimiter, to_thread
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

MEMORY_COST_KIB = 19_456
TIME_COST = 2
PARALLELISM = 1
MAX_CONCURRENT_HASHES = 4

_hasher = PasswordHasher(time_cost=TIME_COST, memory_cost=MEMORY_COST_KIB, parallelism=PARALLELISM)

# A semaphore for worker threads: to_thread.run_sync waits for a free slot before starting.
# Separate from anyio's default thread limiter, so hashing can't starve other thread work.
_limiter = CapacityLimiter(MAX_CONCURRENT_HASHES)


def _hash_sync(password: str) -> str:
    return _hasher.hash(password)


def _verify_sync(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except VerifyMismatchError, VerificationError, InvalidHashError:
        return False


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    return _hash_sync("not-a-real-password")


async def hash_password(password: str) -> str:
    return await to_thread.run_sync(_hash_sync, password, limiter=_limiter)


async def verify_password(password_hash: str, password: str) -> bool:
    """True if `password` matches. A malformed stored hash counts as a mismatch."""
    return await to_thread.run_sync(_verify_sync, password_hash, password, limiter=_limiter)


async def verify_against_dummy(password: str) -> None:
    """Spend the same time as a real verify. Login calls this when the email is unknown, so
    response time doesn't reveal which emails are registered."""
    await to_thread.run_sync(lambda: _verify_sync(_dummy_hash(), password), limiter=_limiter)


def needs_rehash(password_hash: str) -> bool:
    """True if the hash was made with other parameters (cheap: parses the prefix only)."""
    return _hasher.check_needs_rehash(password_hash)
