"""Why the wallet locks rows: unlocked read-check-update under parallel load (DESIGN.md §4).

N concurrent withdrawals hit one wallet. Each is a transaction on its own connection, at
Postgres' default READ COMMITTED. Three ways to write it:

  naive-absolute  SELECT balance; check balance >= amount in Python; UPDATE SET balance = <new>
                  -> lost updates: many withdrawals pay out, few are deducted
  naive-relative  SELECT balance; check in Python; UPDATE SET balance = balance - amount
                  -> overdraft: every withdrawal that passed the stale check is deducted
  locked          SELECT balance ... FOR UPDATE; check; UPDATE SET balance = balance - amount
                  -> correct: exactly balance // amount succeed, the rest are refused

By default the naive variants use a barrier: every withdrawal finishes its read before any of
them writes. That forces the worst-case interleaving, so the anomaly shows on every run instead
of by luck. --no-barrier replaces it with a random 0-5 ms "think time" between read and write,
which is closer to real traffic and still shows the anomaly, but by a varying amount. The locked
variant can't use a barrier (only one session holds the lock at a time) and doesn't need one.

Runs in its own throwaway schema `naive_demo` (one table, NO CHECK constraint, so the overdraft
shows as a negative number instead of an error), as wallet_owner, and drops it afterwards. The
real money tables are never touched.

    uv run python scripts/naive_transfer_demo.py                  # dev database
    uv run python scripts/naive_transfer_demo.py --target test    # test database
    uv run python scripts/naive_transfer_demo.py --workers 100 --no-barrier --json out.json
"""

import argparse
import asyncio
import json
import random
import sys
from dataclasses import asdict, dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

VARIANTS = ("naive-absolute", "naive-relative", "locked")
# The schema name is written out in every statement (no string formatting into SQL).
_SELECT = "SELECT balance FROM naive_demo.wallet WHERE id = 1"
_SELECT_FOR_UPDATE = "SELECT balance FROM naive_demo.wallet WHERE id = 1 FOR UPDATE"


@dataclass
class Result:
    variant: str
    workers: int
    initial_balance: int
    amount: int
    succeeded: int
    paid_out: int
    final_balance: int
    expected_final: int  # initial - paid_out: what the balance should be after these payouts
    discrepancy: int  # final - expected; > 0 means money was paid out without being deducted
    overdraft: bool  # final balance below zero

    @property
    def correct(self) -> bool:
        return (
            self.discrepancy == 0
            and not self.overdraft
            and self.succeeded == self.initial_balance // self.amount
        )


async def _setup(engine: AsyncEngine, balance: int) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA IF EXISTS naive_demo CASCADE"))
        await conn.execute(text("CREATE SCHEMA naive_demo"))
        await conn.execute(
            text("CREATE TABLE naive_demo.wallet (id int PRIMARY KEY, balance bigint NOT NULL)")
        )
        await conn.execute(text("INSERT INTO naive_demo.wallet VALUES (1, :b)"), {"b": balance})


async def _teardown(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA IF EXISTS naive_demo CASCADE"))


async def _withdraw(
    engine: AsyncEngine,
    variant: str,
    amount: int,
    barrier: asyncio.Barrier | None,
    rng: random.Random,
) -> bool:
    """One withdrawal in its own transaction. True if it 'paid out'."""
    async with engine.connect() as conn, conn.begin():
        balance = await conn.scalar(text(_SELECT_FOR_UPDATE if variant == "locked" else _SELECT))
        ok = balance >= amount  # the check, done in application code
        if barrier is not None:
            await barrier.wait()  # everyone has read; now everyone writes
        elif variant != "locked":
            await asyncio.sleep(rng.uniform(0, 0.005))  # "think time" between read and write
        if not ok:
            return False
        if variant == "naive-absolute":
            await conn.execute(
                text("UPDATE naive_demo.wallet SET balance = :new WHERE id = 1"),
                {"new": balance - amount},
            )
        else:
            await conn.execute(
                text("UPDATE naive_demo.wallet SET balance = balance - :amt WHERE id = 1"),
                {"amt": amount},
            )
        return True


async def run_variant(
    engine: AsyncEngine,
    variant: str,
    *,
    workers: int,
    balance: int,
    amount: int,
    use_barrier: bool = True,
    seed: int = 0,
) -> Result:
    await _setup(engine, balance)
    barrier = asyncio.Barrier(workers) if use_barrier and variant != "locked" else None
    rng = random.Random(seed)  # noqa: S311 (think-time jitter, not security)
    outcomes = await asyncio.gather(
        *(_withdraw(engine, variant, amount, barrier, rng) for _ in range(workers))
    )
    async with engine.connect() as conn:
        final = await conn.scalar(text("SELECT balance FROM naive_demo.wallet WHERE id = 1"))
    succeeded = sum(outcomes)
    paid = succeeded * amount
    return Result(
        variant=variant,
        workers=workers,
        initial_balance=balance,
        amount=amount,
        succeeded=succeeded,
        paid_out=paid,
        final_balance=final,
        expected_final=balance - paid,
        discrepancy=final - (balance - paid),
        overdraft=final < 0,
    )


async def run_all(
    url: str, *, workers: int, balance: int, amount: int, use_barrier: bool, seed: int
) -> list[Result]:
    # One connection per worker, so every withdrawal really runs concurrently.
    engine = create_async_engine(url, pool_size=workers, max_overflow=0)
    try:
        async with engine.connect() as conn:
            role = await conn.scalar(text("SELECT current_user"))
        if role != "wallet_owner":
            raise SystemExit(f"must run as wallet_owner (creates a schema); connected as {role}")
        results = []
        for variant in VARIANTS:
            results.append(
                await run_variant(
                    engine,
                    variant,
                    workers=workers,
                    balance=balance,
                    amount=amount,
                    use_barrier=use_barrier,
                    seed=seed,
                )
            )
        return results
    finally:
        await _teardown(engine)
        await engine.dispose()


def format_report(results: list[Result], *, use_barrier: bool) -> str:
    first = results[0]
    lines = [
        f"{first.workers} concurrent withdrawals of {first.amount} paise from a wallet holding "
        f"{first.initial_balance} paise (at most {first.initial_balance // first.amount} "
        "can succeed); READ COMMITTED.",
        "Naive interleaving: "
        + ("barrier (all read, then all write)" if use_barrier else "random 0-5 ms think time"),
        "",
        "| variant | succeeded | paid out | final balance | should be | discrepancy | overdraft "
        "| correct |",
        "|---|---:|---:|---:|---:|---:|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r.variant} | {r.succeeded} | {r.paid_out} | {r.final_balance} | "
            f"{r.expected_final} | {r.discrepancy:+d} | {'yes' if r.overdraft else 'no'} | "
            f"{'yes' if r.correct else 'NO'} |"
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--target", choices=("dev", "test"), default="dev")
    parser.add_argument("--workers", type=int, default=50)
    parser.add_argument("--balance", type=int, default=10_000, help="paise")
    parser.add_argument("--amount", type=int, default=1_000, help="paise per withdrawal")
    parser.add_argument("--no-barrier", action="store_true", help="random think time instead")
    parser.add_argument("--seed", type=int, default=0, help="think-time RNG seed")
    parser.add_argument("--json", metavar="PATH", help="also write the results as JSON")
    args = parser.parse_args()

    sys.path.insert(0, ".")  # run from the repo root: `uv run python scripts/...`
    from app.config import get_settings

    settings = get_settings()
    url = (
        settings.test_migration_database_url
        if args.target == "test"
        else settings.migration_database_url
    )
    use_barrier = not args.no_barrier
    results = asyncio.run(
        run_all(
            url,
            workers=args.workers,
            balance=args.balance,
            amount=args.amount,
            use_barrier=use_barrier,
            seed=args.seed,
        )
    )
    print(format_report(results, use_barrier=use_barrier))
    if args.json:
        with open(args.json, "w") as f:
            json.dump([asdict(r) for r in results], f, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
