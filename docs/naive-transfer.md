# Naive transfer demo

Why every transfer in the wallet locks the account rows it touches (`DESIGN.md` §4).
`scripts/naive_transfer_demo.py` fires a burst of concurrent withdrawals at one wallet and
writes the withdrawal three ways:

| Variant | What each withdrawal does | Expected failure |
|---|---|---|
| `naive-absolute` | `SELECT balance`; check `balance >= amount` in Python; `UPDATE … SET balance = <balance − amount>` | Lost updates: many pay out, few are deducted |
| `naive-relative` | `SELECT balance`; check in Python; `UPDATE … SET balance = balance − amount` | Overdraft: every withdrawal that passed the stale check is deducted |
| `locked` | `SELECT balance … FOR UPDATE`; check; `UPDATE … SET balance = balance − amount` | None: exactly `balance // amount` succeed |

Each withdrawal is its own transaction on its own connection, at Postgres's default
READ COMMITTED.

## Stampede-shaped run

Shape: 75 concurrent withdrawals of ₹10 (1000 paise) from a wallet holding ₹500 (50000 paise).
At most 50 can succeed, so the burst is 1.5× oversubscribed. Run on the dev database on
2026-11-19:

```bash
uv run python scripts/naive_transfer_demo.py --workers 75 --balance 50000 --amount 1000
uv run python scripts/naive_transfer_demo.py --workers 75 --balance 50000 --amount 1000 --no-barrier
```

**With the barrier (the default):**

| variant | succeeded | paid out | final balance | should be | discrepancy | overdraft | correct |
|---|---:|---:|---:|---:|---:|---|---|
| naive-absolute | 75 | 75000 | 49000 | -25000 | +74000 | no | NO |
| naive-relative | 75 | 75000 | -25000 | -25000 | +0 | yes | NO |
| locked | 50 | 50000 | 0 | 0 | +0 | no | yes |

**With `--no-barrier` (random 0–5 ms think time):**

| variant | succeeded | paid out | final balance | should be | discrepancy | overdraft | correct |
|---|---:|---:|---:|---:|---:|---|---|
| naive-absolute | 75 | 75000 | 48000 | -25000 | +73000 | no | NO |
| naive-relative | 75 | 75000 | -25000 | -25000 | +0 | yes | NO |
| locked | 50 | 50000 | 0 | 0 | +0 | no | yes |

All amounts are in paise. "Should be" is the starting balance minus what was paid out, and
"discrepancy" is the final balance minus that. A positive discrepancy is money paid out that was
never deducted.

- **naive-absolute:** all 75 withdrawals paid out ₹750 between them, but the balance only fell by
  ₹10. Every withdrawal read 50000 and wrote 49000, so each write overwrote the last.
- **naive-relative:** all 75 passed a check against a balance that was already stale. Every
  deduction landed, and the wallet ended at −₹250.
- **locked:** each withdrawal waited for the row lock and then checked the real balance.
  Exactly 50 succeeded, 25 were refused, and the wallet ended at zero.

### Why 75 workers, not 100

The 2.9 stampede test sends 100 transfers. The demo can't, on this server's Postgres. The
barrier needs every withdrawal's transaction open at the same moment, so the demo opens one
connection per worker. Postgres here allows `max_connections = 100`, of which 97 are for
non-superusers, and about 17 sessions were already open (the API's pool, idle `psql` sessions
and Postgres's own background processes). 75 workers fit. The shape that matters is kept: a
₹500 balance, ₹10 each, at most 50 can succeed, and 1.5× oversubscribed.

The real stampede doesn't have this limit. It goes through the API, whose pool is 10
connections, and the row locks queue the transfers.

## The barrier

The naive bugs only show when withdrawals overlap: each one must read before the others write.
Left to chance, how much they overlap varies from run to run. So by default the naive variants
wait at an `asyncio.Barrier` after their read: no withdrawal writes until every one has read.
That forces the worst-case interleaving, so the anomaly shows on every run, by an exact amount.
The integration test (`tests/integration/test_naive_transfer_demo.py`) relies on that to assert
exact numbers.

`--no-barrier` replaces the barrier with a random 0–5 ms pause between read and write, which is
closer to real traffic. The anomalies still showed in full in this run. Only the absolute
variant's final balance moved (48000 instead of 49000), because a couple of withdrawals happened
to read after another had written. That number varies between runs. The `locked` variant never
uses the barrier: only one session can hold the lock at a time, so a barrier would deadlock, and
it isn't needed.

## Would the real schema catch these?

The demo uses its own throwaway schema, `naive_demo`. Its one table has no `CHECK`, so the
overdraft shows as a negative number instead of an error. The real `accounts` table has
`accounts_available_check` (`balance − held_amount >= 0` on user wallets):

- **naive-relative:** the constraint **would** stop it. The 51st deduction would take the
  balance below zero, so it fails with a check violation.
- **naive-absolute:** the constraint **would not** stop it. Every write sets the balance to
  49000, which is a perfectly legal value, so no constraint can tell it was a lost update. Even
  the double-entry checks wouldn't stop it: they compare a transfer's own entries with each
  other, not with the balance column. Only reconciliation, which compares each balance with
  its ledger entries, would find it, and only after the money had left.

So the design's primary defence is the row lock: lock the accounts in ascending id order,
**then** check the funds (case 3 in [`isolation.md`](isolation.md)). Constraints are a backstop
that turns some bugs into errors instead of bad data. They can't be the only defence.

## Running it

```bash
uv run python scripts/naive_transfer_demo.py                  # dev database: 50 x 1000 from 10000
uv run python scripts/naive_transfer_demo.py --target test    # test database
uv run python scripts/naive_transfer_demo.py --no-barrier --seed 7 --json out.json
uv run pytest -q tests/integration/test_naive_transfer_demo.py
```

The script connects as `wallet_owner`, because it creates a schema, and refuses to run as anyone
else. It creates the `naive_demo` schema, runs the three variants, and drops the schema
afterwards. The real money tables are never touched. Keep `--workers` below the free
connections, about 75 on this server.
