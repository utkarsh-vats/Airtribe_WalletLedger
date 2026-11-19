"""Step 2.4: scripts/naive_transfer_demo.py shows the anomalies it claims, deterministically.

With the barrier, every naive withdrawal reads before any writes, so the outcome is exact.
Runs as wallet_owner against the test database, in the demo's own throwaway schema.
"""

import importlib.util
import pathlib
from collections.abc import AsyncIterator
from types import ModuleType

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.config import get_settings

WORKERS, BALANCE, AMOUNT = 10, 5_000, 1_000  # at most 5 may succeed


def _load_demo() -> ModuleType:
    path = pathlib.Path(__file__).parents[2] / "scripts" / "naive_transfer_demo.py"
    spec = importlib.util.spec_from_file_location("naive_transfer_demo", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


demo = _load_demo()


@pytest.fixture
async def demo_engine(migrated_test_db: None) -> AsyncIterator[AsyncEngine]:
    settings = get_settings()
    assert settings.migration_database_url == settings.test_migration_database_url
    engine = create_async_engine(settings.migration_database_url, pool_size=WORKERS, max_overflow=0)
    try:
        yield engine
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DROP SCHEMA IF EXISTS naive_demo CASCADE"))
        await engine.dispose()


async def _run(engine: AsyncEngine, variant: str):  # type: ignore[no-untyped-def]
    return await demo.run_variant(engine, variant, workers=WORKERS, balance=BALANCE, amount=AMOUNT)


async def test_absolute_write_loses_updates(demo_engine: AsyncEngine) -> None:
    r = await _run(demo_engine, "naive-absolute")

    # All 10 read 5000 and passed the check; each wrote 5000 - 1000.
    assert (r.succeeded, r.paid_out, r.final_balance) == (10, 10_000, 4_000)
    assert r.discrepancy == 4_000 - (5_000 - 10_000)  # 9000 paid out but never deducted
    assert not r.correct


async def test_relative_write_overdraws(demo_engine: AsyncEngine) -> None:
    r = await _run(demo_engine, "naive-relative")

    # All 10 passed the stale check; every deduction landed.
    assert (r.succeeded, r.final_balance) == (10, -5_000)
    assert r.overdraft and r.discrepancy == 0
    assert not r.correct


async def test_row_lock_is_correct(demo_engine: AsyncEngine) -> None:
    r = await _run(demo_engine, "locked")

    assert (r.succeeded, r.paid_out, r.final_balance) == (5, 5_000, 0)
    assert r.correct


async def test_report_lists_every_variant(demo_engine: AsyncEngine) -> None:
    results = [await _run(demo_engine, v) for v in demo.VARIANTS]

    report = demo.format_report(results, use_barrier=True)

    for variant in demo.VARIANTS:
        assert f"| {variant} |" in report
