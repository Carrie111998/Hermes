# Hermes Docker deployment contract

The Docker deployment uses one service, `gateway`. The dashboard is enabled
inside that service on `127.0.0.1:9119`; a second dashboard container must not
be started because it would contend for the same host-network port.

## Required environment

Create `/home/deploy/hermes-agent/.env` from
`docker-compose.env.example` and set:

```dotenv
HERMES_UID=999
HERMES_GID=987
HERMES_DATA_DIR=/home/deploy/.hermes
```

`HERMES_UID` and `HERMES_GID` must equal the numeric owner of
`HERMES_DATA_DIR`. The directory must already exist and must contain the
persistent Hermes profile. Never rely on Compose's process `HOME` or use
`~/.hermes` in a deployment command.

The Compose file deliberately has no UID/GID defaults. An unset or invalid
identity fails before container creation. The wrapper additionally verifies
the data directory owner and absolute path.

## Safe lifecycle commands

From the project directory, use the wrapper for all lifecycle operations:

```sh
./scripts/docker-compose-hermes config
./scripts/docker-compose-hermes up -d
./scripts/docker-compose-hermes ps
```

The wrapper does not read or print application secrets. The image startup hook
also remaps the internal `hermes` user and repairs its targeted state
ownership on boot. The Compose healthcheck then fails if the runtime UID/GID
or the critical persistent paths are inaccessible.

Before an image update or recreation, preserve the Compose file, `.env`,
`config.yaml`, `webui/settings.json`, and the database/WAL files. Do not copy
state between profiles; verify application-version and database compatibility
first.

## Update compatibility

The identity contract lives in Compose and the wrapper, not in a mutable image
layer. Future image updates retain the bind mount and pass the same explicit
UID/GID. After an update, verify the health status and `/api/config`,
`/api/skills`, `/api/dashboard/plugins`, and the dashboard WebSocket before
declaring the deployment healthy.
