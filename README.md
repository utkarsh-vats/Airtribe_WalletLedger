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

`docker compose up` runs both upgrades for you through the `migrate` service.

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
| The runtime role can only do its job | `wallet_app` gets table and column grants written in each migration, for example only `UPDATE (status, updated_at)` on `transfers`. It can't create any object |

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

Login endpoints arrive in step 2.1b; the pieces they use are in `app/auth/`.

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
