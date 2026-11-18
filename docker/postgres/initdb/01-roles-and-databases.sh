#!/usr/bin/env bash
# Runs once, when the Postgres data volume is first initialised (docker-entrypoint-initdb.d).
#
# Creates the two application roles and the test database:
#   wallet_owner  owns the databases and every schema object; Alembic connects as this role.
#   wallet_app    runtime role; gets CONNECT + schema USAGE here (no TEMPORARY), and table-level
#                 grants are written explicitly in each migration (least privilege).
# Passwords are passed as psql variables (:'name'), never spliced into SQL text.
set -euo pipefail

: "${POSTGRES_USER:?}" "${POSTGRES_DB:?}"
: "${WALLET_OWNER_PASSWORD:?}" "${WALLET_APP_PASSWORD:?}"
TEST_DB="${POSTGRES_TEST_DB:-walletledger_test}"

psql_su() {
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --no-psqlrc "$@"
}

psql_su --dbname "$POSTGRES_DB" \
  -v owner_pw="$WALLET_OWNER_PASSWORD" \
  -v app_pw="$WALLET_APP_PASSWORD" \
  -v main_db="$POSTGRES_DB" \
  -v test_db="$TEST_DB" <<'SQL'
CREATE ROLE wallet_owner LOGIN PASSWORD :'owner_pw';
CREATE ROLE wallet_app   LOGIN PASSWORD :'app_pw';

ALTER DATABASE :"main_db" OWNER TO wallet_owner;
CREATE DATABASE :"test_db" OWNER wallet_owner;
SQL

# Same privileges on the dev and the test database.
for db in "$POSTGRES_DB" "$TEST_DB"; do
  psql_su --dbname "$db" -v db="$db" <<'SQL'
REVOKE ALL ON DATABASE :"db" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"db" TO wallet_app;

-- public is owned by pg_database_owner (PG15+), i.e. wallet_owner. Nobody else may create in it.
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO wallet_app;
SQL
done

echo "walletledger init: roles wallet_owner, wallet_app; databases $POSTGRES_DB, $TEST_DB"
