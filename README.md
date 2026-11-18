# Airtribe_WalletLedger

Payments-grade wallet backend with a double-entry ledger. See `DESIGN.md` for the full design.

## Networking

`.env` is optional: `docker-compose.yml` has a local-dev default for every variable, so a fresh
clone starts with `docker compose up`; copy `.env.example` to `.env` only to override values.

Host ports are set in `.env` (see `.env.example`). Postgres and Redis always bind to
`127.0.0.1`. The API binds to `API_BIND_HOST` (default `127.0.0.1`), which can be set to a
specific interface IP, such as a Tailscale address, to reach the API from other devices.

**Docker-published ports bypass UFW.** Docker writes its own iptables rules ahead of UFW's, so
a port published on `0.0.0.0` is reachable from every network the host is on, whatever UFW
says. That's why every port here binds to a specific interface address and never to `0.0.0.0`.
