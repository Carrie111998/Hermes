# Hermes Docker deployment hardening report

Date: 2026-07-15

## Finding

The failure mode was caused by an implicit Compose fallback to UID/GID
`10000:10000` combined with a bind mount owned by `deploy` (`999:987`). The
WebUI's static files could still be served, but the non-root gateway/dashboard
process could not read the persistent profile.

The active deployment also used `~/.hermes`, which made the selected profile
depend on the caller's `HOME`, and contained a redundant standalone dashboard
service competing with the dashboard already enabled in `gateway`.

## Changes

- Removed UID/GID fallback values from `docker-compose.yml`.
- Replaced `~/.hermes` with required absolute `HERMES_DATA_DIR`.
- Removed the redundant default `dashboard` service; `gateway` owns port 9119.
- Added a fail-closed Compose healthcheck for runtime UID/GID, profile access,
  and local dashboard readiness.
- Added `scripts/docker-compose-hermes`, which validates `.env`, numeric IDs,
  absolute data path, and exact directory ownership before Compose runs.
- Added `docker-compose.env.example` and `docs/docker-deployment.md`.
- The live project `.env` now contains the deployment's explicit
  `HERMES_UID=999`, `HERMES_GID=987`, and `HERMES_DATA_DIR=/home/deploy/.hermes`.
  It remains ignored and was not committed.

## Verification

- Wrapper config: passed.
- Missing `.env` preflight: failed closed with exit 64.
- Owner mismatch preflight: failed closed with exit 64.
- Raw Compose with missing required variables: failed closed.
- Live service: `hermes-agent`, running and `healthy`, restart count 0.
- Runtime identity: UID/GID `999:987`.
- Persistent profile: 28 skills and 14 plugins visible to runtime.
- Authenticated `/api/config`: HTTP 200.
- Authenticated `/api/skills`: HTTP 200.
- Authenticated `/api/dashboard/plugins`: HTTP 200.
- WebSocket handshake: HTTP 101 Switching Protocols.
- Public Nginx probe: expected HTTP 401 Basic Auth; Nginx was not modified.

## Git

Commits created:

- `02e9d6596b` — harden Docker identity and persistent state contract
- `65a83285f9` — use absolute s6 helper in Docker healthcheck
- `9aa5f7c86b` — include dashboard readiness in Docker healthcheck

The pre-existing untracked `.codegraph/` directory was left untouched.

No user data, Docker volumes, profiles, or Nginx configuration were modified.
