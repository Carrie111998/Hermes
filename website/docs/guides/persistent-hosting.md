---
sidebar_position: 18
title: "Persistent Hosting (24/7 Gateway)"
description: "Run the Hermes gateway around the clock on any host — Docker Compose, systemd, or a container platform — bootstrapping credentials from 1Password at startup"
---

# Persistent Hosting (24/7 Gateway)

This guide sets up the Hermes messaging gateway as an always-on service on a host you control — a $5 VPS, a homelab box, or a container platform — with **no long-lived credentials stored on disk**. At every startup the gateway pulls its secrets (Slack tokens, provider API keys, ...) from 1Password using a single bootstrap token, so rotating a credential is one change in 1Password and a restart (or just waiting out the cache TTL — the long-lived gateway re-fetches periodically).

Three paths are covered:

- [Path A — Docker Compose](#path-a--docker-compose) (recommended for any VPS)
- [Path B — bare systemd](#path-b--bare-systemd) (no containers)
- [Path C — container platforms](#path-c--container-platforms-flyio-railway-) (Fly.io, Railway, ...)

All three share the same 1Password prerequisite, so start there.

## Prerequisite: the 1Password layout

1. In the [1Password admin console](https://my.1password.com), go to **Developer → Service Accounts** and create a service account. Grant it **read access to one vault only** (e.g. `Private`). Copy the token — it starts with `ops_` and cannot be shown again.
2. In that vault, create an item titled **`Hermes Gateway`** with one field per environment variable the gateway needs. Field labels map 1:1 onto env var names. For a Slack gateway that's:

   | Field label       | Value                                  |
   |-------------------|----------------------------------------|
   | `SLACK_BOT_TOKEN` | `xoxb-...your-bot-token...`            |
   | `SLACK_APP_TOKEN` | `xapp-...your-app-level-token...`      |

   Add provider keys (`OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`, ...) as extra fields in the same item if you want those centrally managed too.

The `ops_` service account token is the **only** secret that ever touches the host, and it belongs in the process environment — never in `config.yaml`, never in a committed file. Everything below uses the placeholder `ops_...your-service-account-token...`; substitute your real token only in the designated environment file or shell.

## Path A — Docker Compose

Works on any host with Docker. Uses the repo's [`docker-compose.yml`](https://github.com/NousResearch/hermes-agent/blob/main/docker-compose.yml); state persists in `~/.hermes` on the host, mounted at `/opt/data` in the container, so `docker compose pull && docker compose up -d` upgrades without losing anything.

```bash
git clone https://github.com/NousResearch/hermes-agent
cd hermes-agent
mkdir -p ~/.hermes
```

**1. First-run setup** (interactive, once):

```bash
docker run -it --rm -v ~/.hermes:/opt/data nousresearch/hermes-agent setup
```

**2. Enable the 1Password bootstrap.** Merge the `secrets:` block from [`deploy/config.example.yaml`](https://github.com/NousResearch/hermes-agent/blob/main/deploy/config.example.yaml) into `~/.hermes/config.yaml` — it enables `secrets.onepassword` with vault `Private`, item `Hermes Gateway`, and `override_existing: true`. Or run the wizard in a throwaway container:

```bash
docker run -it --rm -v ~/.hermes:/opt/data \
  -e OP_SERVICE_ACCOUNT_TOKEN \
  nousresearch/hermes-agent secrets onepassword setup --vault "Private" --item "Hermes Gateway"
```

**3. Provide the bootstrap token.** Two equivalent options:

- Put `OP_SERVICE_ACCOUNT_TOKEN=ops_...your-service-account-token...` in `~/.hermes/.env` on the host (`chmod 600`). It rides into the container with the volume; nothing else to wire.
- Or keep it out of the data volume: export it in the host environment (or a `.env` file next to `docker-compose.yml`, excluded from git) and uncomment this line in the `gateway` service of `docker-compose.yml`:

  ```yaml
      # - OP_SERVICE_ACCOUNT_TOKEN=${OP_SERVICE_ACCOUNT_TOKEN}
  ```

**4. Start it:**

```bash
HERMES_UID=$(id -u) HERMES_GID=$(id -g) docker compose up -d gateway
```

The `gateway` service already has `restart: unless-stopped`, so it survives crashes and host reboots (as long as the Docker daemon is enabled at boot). The official image ships the `onepassword` extra pre-installed — no first-boot SDK download.

**Verify:**

```bash
docker compose logs -f gateway            # watch startup; Slack should connect
docker compose exec gateway hermes secrets onepassword status
```

`status` should report the SDK version, `enabled: true`, vault/item, and a successful test fetch listing the env vars that resolved (values are never printed).

## Path B — bare systemd

For a Linux host without containers. Two options:

- **Simple (single-user machine):** `hermes gateway install` — the built-in installer creates and manages a systemd *user* unit (or launchd agent on macOS) for whatever user you're logged in as. Combine with `loginctl enable-linger $USER` to survive logout.
- **Dedicated host (recommended for a VPS):** the hardened system unit shipped at [`deploy/hermes-gateway.service`](https://github.com/NousResearch/hermes-agent/blob/main/deploy/hermes-gateway.service), which runs the gateway as an unprivileged `hermes` service account, starts at boot with nobody logged in, restarts on failure (`Restart=always`, `RestartSec=10`), and confines writes to the service user's home (`ProtectSystem=strict`, `NoNewPrivileges`, `PrivateTmp`).

The dedicated-host setup, condensed (the unit file's header comment has the copy-pasteable version):

```bash
# 1. Service user + install
sudo useradd -r -m -d /home/hermes -s /bin/bash hermes
sudo -iu hermes bash -c 'curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash'

# 2. One-time interactive config (model, platforms, 1Password)
sudo -iu hermes hermes setup
sudo -iu hermes hermes gateway setup
sudo -iu hermes env OP_SERVICE_ACCOUNT_TOKEN=ops_...your-service-account-token... \
  hermes secrets onepassword setup --vault "Private" --item "Hermes Gateway"

# 3. Bootstrap token in a root-owned env file (this file holds ONLY that token)
sudo install -d -m 750 /etc/hermes
sudo install -m 600 deploy/gateway.env.example /etc/hermes/gateway.env
sudoedit /etc/hermes/gateway.env    # paste the real ops_ token

# 4. Install + start
sudo cp deploy/hermes-gateway.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hermes-gateway
```

**Verify:**

```bash
systemctl status hermes-gateway
journalctl -u hermes-gateway -f     # gateway logs stream to the journal
sudo -iu hermes env OP_SERVICE_ACCOUNT_TOKEN=ops_...your-service-account-token... \
  hermes secrets onepassword status
```

## Path C — container platforms (Fly.io, Railway, ...)

Any platform that runs an OCI image and supports persistent volumes can host the same `nousresearch/hermes-agent` image:

- **Image / command:** `nousresearch/hermes-agent`, command `gateway run`.
- **Volume:** mount a persistent volume at `/opt/data` (this is `~/.hermes` — config, sessions, memories). Without it you lose all state on every deploy.
- **Secret:** set `OP_SERVICE_ACCOUNT_TOKEN` through the platform's secret store (`fly secrets set OP_SERVICE_ACCOUNT_TOKEN=ops_...`, Railway *Variables*, etc.) — never in the image or a checked-in config.
- **Config:** seed the volume once with a `config.yaml` that has the `secrets.onepassword` block enabled (e.g. `fly ssh console` + paste, or run the container's `secrets onepassword setup` subcommand interactively).
- **Networking:** outbound-only platforms work fine — Telegram/Discord/Slack (Socket Mode) all use outbound connections, no inbound port needed. Only expose port 8642 if you deliberately enable the [OpenAI-compatible API server](/docs/user-guide/features/api-server), and read its security notes first.

## Logs & operations

| Deployment | Logs |
|------------|------|
| Docker Compose | `docker compose logs -f gateway` (or `docker logs hermes`) |
| systemd | `journalctl -u hermes-gateway -f` |
| All | `~/.hermes/logs/` (`/opt/data/logs/` in-container) — runtime log files |

- **Rotating a credential:** change the field in the 1Password item. The gateway re-fetches after `cache_ttl_seconds` (default 300 s); a restart forces it immediately.
- **Rotating the bootstrap token itself:** generate a new service account token in 1Password, update `/etc/hermes/gateway.env` (systemd) or the host env / `~/.hermes/.env` (Docker), restart the gateway, then revoke the old token.
- **Upgrades:** `docker compose pull && docker compose up -d` (Docker) or `sudo -iu hermes hermes update` + `sudo systemctl restart hermes-gateway` (systemd).

## Security checklist

- The `ops_` token grants read access to everything in its vault — scope the service account to a dedicated vault, prefer a dedicated item, and treat the token like a root credential.
- Never commit the token, an env file containing it, or a `config.yaml` with inline credentials. Config references the token only by env var *name* (`service_account_token_env`).
- Keep the dashboard and API server off or bound to localhost unless you've read [their security notes](/docs/user-guide/security).
