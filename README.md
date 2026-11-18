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
