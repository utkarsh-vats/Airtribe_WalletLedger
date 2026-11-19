# Airtribe_WalletLedger

Payments-grade wallet backend with a double-entry ledger. See `DESIGN.md` for the full design.

## Quick start

```bash
docker compose up -d --build
curl http://127.0.0.1:8010/health        # {"status":"ok"}
```

That's all a fresh clone needs. Compose starts four services:

| Service | What it does |
|---|---|
| `postgres` | PostgreSQL 18.3. First start creates the app roles and the test database. |
| `redis` | Redis 8, with eviction off (`noeviction`) so queued work is never silently dropped. |
| `migrate` | One-shot: `alembic upgrade head` on the dev and the test database, then exits. |
| `api` | FastAPI on uvicorn. Starts only after `migrate` exits successfully. |

Every service has a memory limit: Postgres 1g, Redis 256m, api 512m, migrate 256m.

## Configuration

`.env` is optional: `docker-compose.yml` has a local-dev default for every variable, so a fresh
clone starts with `docker compose up`; copy `.env.example` to `.env` only to override values.

The default passwords are weak on purpose and safe only because Postgres and Redis bind to
`127.0.0.1`. Set real ones in `.env` for anything else.

| Variable | Default | Used for |
|---|---|---|
| `API_BIND_HOST` / `API_HOST_PORT` | `127.0.0.1` / `8010` | Where the API is published |
| `POSTGRES_HOST_PORT` / `REDIS_HOST_PORT` | `5433` / `6380` | Host ports (always on `127.0.0.1`) |
| `POSTGRES_PASSWORD` | `walletledger` | Postgres superuser |
| `WALLET_OWNER_PASSWORD` / `WALLET_APP_PASSWORD` | `wallet_owner` / `wallet_app` | App roles, see below |
| `DATABASE_URL`, `TEST_DATABASE_URL`, `MIGRATION_DATABASE_URL`, `TEST_MIGRATION_DATABASE_URL` | localhost:5433 URLs | Running the app, tests and Alembic from the host |
| `JWT_SECRET` | A dev-only value, refused when `APP_ENV=prod` | Signing access tokens; at least 32 bytes. Generate one with `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `ADMIN_EMAIL` / `ADMIN_PASSWORD` | Empty (no admin is created) | The admin user, seeded into the dev database by `migrate`; the password needs at least 12 characters |
| `ADMIN_PHONE` | `+910000000000` | The seeded admin's phone (the column is required) |

Postgres reads its passwords only when the data volume is first created. After changing them,
or `docker/postgres/initdb/`, the volume has to be recreated with
`docker compose down -v`, which **deletes all data** in this project's databases.

### Networking

Host ports are set in `.env` (see `.env.example`). Postgres and Redis always bind to
`127.0.0.1`. The API binds to `API_BIND_HOST` (default `127.0.0.1`), which can be set to a
specific interface IP, such as a Tailscale address, to reach the API from other devices.

**Docker-published ports bypass UFW.** Docker writes its own iptables rules ahead of UFW's, so
a port published on `0.0.0.0` is reachable from every network the host is on, whatever UFW
says. That's why every port here binds to a specific interface address and never to `0.0.0.0`.

## Databases and roles

One Postgres container holds two databases, `walletledger` (dev) and `walletledger_test`
(tests and CI), with the same two roles in each:

| Role | Can do | Used by |
|---|---|---|
| `wallet_owner` | Owns both databases, the `public` schema and every object in it | Alembic only |
| `wallet_app` | Connect, plus whatever each migration explicitly grants it; it can't create any object, not even a temporary table | The API (and the worker, once it exists) |

Default `PUBLIC` access is revoked, so `wallet_app` can't create tables and can't read a table
until a migration grants it. This is what lets the database itself enforce rules such as
"ledger entries are insert-only" (`DESIGN.md` §3.3). The roles and the test database are created
by `docker/postgres/initdb/01-roles-and-databases.sh`.

## Migrations

Migrations are hand-written SQL in `migrations/versions/` (`op.execute(...)`); there is no
autogenerate. They are numbered `0001`, `0002`, … and each runs in its own transaction.
`migrations/env.py` refuses to run unless connected as `wallet_owner`, so objects are never
owned by the runtime role.

```bash
uv run alembic upgrade head                    # dev database
uv run alembic -x target=test upgrade head     # test database
uv run alembic upgrade head --sql              # print the SQL instead of running it
```

`docker compose up` runs both upgrades for you through the `migrate` service, and then seeds the
admin user into the dev database (see [Authentication](#authentication)).

| Migration | Adds |
|---|---|
| `0001` | Empty baseline |
| `0002` | `users`, `accounts`, and the three system accounts (`cash_in`, `cash_out`, `fee_revenue`) |
| `0003` | `transfers`, `ledger_entries`, `holds`, `transfer_status_history`, and their indexes |
| `0004` | Append-only triggers; `wallet_app` grants on the money tables |
| `0005` | Deferred per-transfer balance check |
| `0006` | Status-history trigger |
| `0007` | `idempotency_keys`, `risk_rules` (seeded), `risk_flags`, `reconciliation_runs`, `reconciliation_issues`, `job_locks` (seeded) |
| `0008` | Revokes `TEMPORARY` from `wallet_app` |
| `0009` | Lets `wallet_app` update `users.password_hash`, so login can upgrade old hashes |
| `0010` | Column-level `INSERT` grants on `users` and `accounts` |

## Schema and what the database enforces

Tables, relationships and the reasoning behind each constraint are in
[`docs/schema.md`](docs/schema.md), with an ER diagram. Money is `BIGINT` paise everywhere.

The rules that keep the books correct are enforced by Postgres itself, so a bug in the API
can't break them:

| Rule | How |
|---|---|
| A wallet is never overdrawn | `CHECK (account_type <> 'user_wallet' OR balance - held_amount >= 0)` on `accounts` |
| Ledger entries and the audit log can't be changed | `BEFORE UPDATE OR DELETE` and `BEFORE TRUNCATE` triggers raise, even for the owner; `wallet_app` can only insert entries and only read the audit log |
| Every transfer balances | Deferred constraint triggers check at `COMMIT`: a completed or reversed transfer has entries whose debits and credits each equal its amount; a held or rejected one has none. Violations raise SQLSTATE `23514` |
| Every status change is recorded | A trigger writes `transfer_status_history` on insert and on every real status change. The actor and the reason come from `app.actor_id` and `app.status_reason`, set for the current transaction only by `app/db.py` (`set_actor`, `set_status_reason`) |
| A retried request can't run twice | `UNIQUE NULLS NOT DISTINCT (user_id, key)` on `idempotency_keys` |
| The runtime role can only do its job | `wallet_app` gets table and column grants written in each migration, for example only `UPDATE (status, updated_at)` on `transfers`. On `users` it can insert only `name`, `email`, `phone` and `password_hash`, so role and KYC status always start at `user` / `pending`. On `accounts` it can insert only `user_id`, `account_type` and `label`, so every wallet starts at a zero balance and gains money only through ledger postings. It can't create any object |

Two of these triggers need care when changing them:

- The status-history function is `SECURITY DEFINER` (owned by `wallet_owner`, `search_path`
  pinned, `EXECUTE` revoked from `PUBLIC`), because `wallet_app` has no `INSERT` on the audit
  table. A history row can only come from a real status change.
- Tests can't commit rows into append-only tables, because those rows could never be cleaned up.
  Schema tests run in rolled-back transactions and use `SET CONSTRAINTS ALL IMMEDIATE` to run
  the deferred balance check before the rollback.

Every `CHECK`, `UNIQUE` and unique index in the schema has a test that tries to break it and
asserts the exact constraint name (`tests/integration/test_schema_rejections.py`). A guard test
fails if a new constraint is added without one.

## Authentication

| Endpoint | Who | Does |
|---|---|---|
| `POST /users` | Anyone | Registers a user: `{name, email, phone, password}` → `201`. New users are role `user`, KYC `pending` |
| `POST /auth/login` | Anyone | `{email, password}` → `200 {access_token, token_type: "bearer"}` |
| `POST /admin/users/{id}/kyc` | Admin | `{kyc_status: "pending" \| "verified" \| "rejected"}` → `200` with the user; `404 USER_NOT_FOUND` |

These don't take an `Idempotency-Key` yet; every `POST` except login will require one from
milestone M3.

- **Registration** normalizes the phone to E.164 before storing it, so `98765 43210`,
  `09876543210` and `+91 98765 43210` are one number. A bare 10-digit number is read as an
  Indian mobile; other countries need the `+` prefix. An email or phone already in use gets
  `409 EMAIL_ALREADY_REGISTERED` or `409 PHONE_ALREADY_REGISTERED`. That does reveal whether an
  email or phone is registered, which the contract requires. Unknown fields such as `role` are
  rejected with `400`.
- **Login** gives one answer, `401 INVALID_CREDENTIALS`, for an unknown email and for a wrong
  password. An unknown email still costs a full password check, so response time doesn't give it
  away. The password check runs between two short database transactions, never inside one.
- **The admin** is created by `migrate` from `ADMIN_EMAIL` and `ADMIN_PASSWORD`, running as
  `wallet_owner`, because `wallet_app` can't create admins. It is skipped when either variable
  is empty. Rerunning it with a new `ADMIN_PASSWORD` resets the admin's password. If
  `ADMIN_EMAIL` belongs to an existing non-admin user, `migrate` fails instead of promoting them.

- **Access tokens** are HS256 JWTs signed with `JWT_SECRET`, valid for 60 minutes, with no
  refresh tokens. They carry the user id (`sub`) and `role`. Routes take the caller from the
  `CurrentUser` dependency, or `AdminUser` for admin-only routes. A missing, malformed, wrongly
  signed or expired token gets `401 UNAUTHORIZED` with `WWW-Authenticate: Bearer`, and the
  message never says which check failed. A valid token without the admin role gets
  `403 FORBIDDEN` on admin routes.
- **The role lives in the token,** so admin checks need no database read. The trade-off: if a
  user's role changes, the change applies from their next token, so within 60 minutes. No
  endpoint changes roles today.
- **Expiry is checked against an injected clock** (`app/clock.py`), not the wall clock, so tests
  pin time instead of sleeping.
- **Passwords** are hashed with argon2id at OWASP's baseline: 19 MiB of memory, 2 iterations,
  1 lane. Hashing runs in worker threads, never on the event loop, with at most 4 at once. That
  keeps a burst of logins from stalling other requests and caps argon2's memory at about 76 MiB.
  Hashes record their own parameters, so if the parameters change, an older hash is upgraded
  on the user's next successful login.

## Wallets

| Endpoint | Who | Does |
|---|---|---|
| `POST /users/{id}/wallets` | That user only | Opens a wallet: optional `{label}` (1–50 characters, default `"main"`) → `201 {id, user_id, label, currency, balance, held_amount, available, created_at}` |
| `GET /wallets/{id}/balance` | The wallet's owner, or an admin | `200 {wallet_id, currency, balance, held_amount, available}` |

All amounts are integer paise, and `available = balance − held_amount`. A new wallet is in INR
with a zero balance. The database sets those values, and `wallet_app` can't write them on
insert.

**Ownership rules:**

- **Opening a wallet is self-only, admins included.** Opening one under another user's id gets
  `403 FORBIDDEN`, and nobody can open a wallet on someone else's behalf. A user can hold
  several wallets with different labels; reusing one of their own labels gets
  `409 WALLET_LABEL_TAKEN`.
- **Reading a balance** is allowed for the wallet's owner, and for an admin on any user's
  wallet. Another user gets `403 FORBIDDEN`. The statement endpoint (step 2.10) will follow the
  same rule.
- **System accounts** (`cash_in`, `cash_out` and `fee_revenue`, ids 1–3) aren't wallets. Their
  ids get `404 WALLET_NOT_FOUND` for everyone, admins included, exactly like an id that doesn't
  exist.

## Isolation and concurrency

[`docs/isolation.md`](docs/isolation.md) records a two-session lab run in real `psql` sessions,
with the commands and their actual output:

| Case | Result |
|---|---|
| 1. Re-read after another session commits | READ COMMITTED sees the new value; REPEATABLE READ keeps its snapshot until the transaction ends |
| 2. Lost update from read-check-write | READ COMMITTED lets both withdrawals commit, and one overwrites the other; REPEATABLE READ fails the second with `40001` |
| 3. READ COMMITTED + `SELECT … FOR UPDATE` (the design's fix) | The second session waits for the lock, then reads the current balance and refuses; no error, no retry |
| 4. Write skew across two rows | REPEATABLE READ lets both commit; SERIALIZABLE cancels one with `40001` |
| 5. REPEATABLE READ with a join | One consistent snapshot across both tables, taken at the first query, not at `BEGIN` |

So every transfer runs at READ COMMITTED and locks the accounts it touches in ascending id
order before checking funds. Reconciliation reads at REPEATABLE READ. To re-run the lab, with
the compose stack up and `tmux` installed:

```bash
scripts/run_isolation_lab.sh      # writes notes/lab-A.log and notes/lab-B.log
```

[`docs/naive-transfer.md`](docs/naive-transfer.md) shows the same bug under load. 75 concurrent
₹10 withdrawals from a ₹500 wallet, so at most 50 can succeed:

| Variant | Succeeded | Final balance (paise) | Outcome |
|---|---:|---:|---|
| naive-absolute (`SET balance = <computed>`) | 75 | 49000 | ₹740 paid out without being deducted |
| naive-relative (`SET balance = balance − amount`) | 75 | −25000 | Overdraft |
| locked (`SELECT … FOR UPDATE`, then check) | 50 | 0 | Correct |

A barrier forces every naive withdrawal to read before any writes, so the result is exact on
every run. With `--no-barrier` (random think time) the results were the same except the
absolute variant ended at 48000. The real schema's `accounts_available_check` would stop the
relative overdraft. It can't stop the absolute lost update, because 49000 is a legal balance.
So the design relies on row locks, with constraints as a backstop.

```bash
uv run python scripts/naive_transfer_demo.py --workers 75 --balance 50000 --amount 1000 [--no-barrier]
```

## Development and tests

Requires [uv](https://docs.astral.sh/uv/); Python 3.14 is installed by uv from `.python-version`.

```bash
uv sync
docker compose up -d --build                   # Postgres and Redis for integration tests
uv run ruff check . && uv run ruff format --check .
uv run pytest -q
```

Tests run against real Postgres, and **never against the dev database**: before anything is
collected, `tests/conftest.py` stops the run (exit code 4) unless `TEST_DATABASE_URL` names a
database ending in `_test` that differs from `DATABASE_URL`. It then points `DATABASE_URL` and
`MIGRATION_DATABASE_URL` at the test database for the whole run, and the integration fixtures
upgrade it to head first.

| Suite | Needs |
|---|---|
| `tests/unit/` | Nothing |
| `tests/integration/` | Postgres from `docker compose` |

## CI

`.github/workflows/ci.yml` runs on every push to `main` and every pull request, with two jobs:

- **`test`**: Postgres 18.3 and Redis 8 as service containers. It runs the same
  `docker/postgres/initdb` script inside the Postgres container, so CI has the same roles and
  `walletledger_test` database as compose. Then lint, both migrations, and `pytest`.
- **`compose-smoke`**: the fresh-clone check. With no `.env`, it runs `docker compose up -d
  --build`, waits up to 90 s for `/health`, and checks that `migrate` exited 0. It dumps every
  service's logs on failure and always tears the stack down.
