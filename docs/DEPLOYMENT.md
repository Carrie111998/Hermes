# Deploying the Hermes Gateway 24/7

Quick index for running the gateway as an always-on service that bootstraps
its credentials (e.g. `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN`) from 1Password at
startup, so no long-lived secrets sit on the host.

**Full guide (published docs):** [`website/docs/guides/persistent-hosting.md`](../website/docs/guides/persistent-hosting.md)
→ https://hermes-agent.nousresearch.com/docs/guides/persistent-hosting

## The pieces

| File | Purpose |
|------|---------|
| [`docker-compose.yml`](../docker-compose.yml) | `gateway` service with `restart: unless-stopped`, `~/.hermes` volume, and an opt-in `OP_SERVICE_ACCOUNT_TOKEN` passthrough (commented) |
| [`Dockerfile`](../Dockerfile) | Official image; ships the `onepassword` extra pre-installed |
| [`deploy/hermes-gateway.service`](../deploy/hermes-gateway.service) | Hardened systemd system unit (`Restart=always`, `NoNewPrivileges`, `ProtectSystem=strict`, `PrivateTmp`) for container-less hosts |
| [`deploy/gateway.env.example`](../deploy/gateway.env.example) | Template for `/etc/hermes/gateway.env` — holds only `OP_SERVICE_ACCOUNT_TOKEN` |
| [`deploy/config.example.yaml`](../deploy/config.example.yaml) | Sample `secrets.onepassword` block for `~/.hermes/config.yaml` (vault `Private`, item `Hermes Gateway`, `override_existing: true`) |

## Three supported paths

1. **Docker Compose on any VPS** — `docker compose up -d gateway`; state lives in `~/.hermes` on the host.
2. **Bare systemd** — dedicated `hermes` service user + `deploy/hermes-gateway.service`; or, on a single-user machine, just `hermes gateway install`.
3. **Container platforms (Fly.io, Railway, ...)** — same image, persistent volume at `/opt/data`, `OP_SERVICE_ACCOUNT_TOKEN` via the platform secret store.

## 1Password layout

Service account with read access to one vault (`Private`), item `Hermes
Gateway`, one field per env var (`SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, ...).
The `ops_` service account token is the only secret that touches the host and
lives exclusively in the process environment — never in `config.yaml`, never
in a committed file (placeholders like `ops_...your-token...` only).

First-run check: `hermes secrets onepassword status`.
Logs: `docker compose logs -f gateway`, `journalctl -u hermes-gateway -f`, or `~/.hermes/logs/`.
