# Mongo remote storage, multi-PC cluster, and mTLS

When `bootstrap.yaml` (or `HERMES_MONGO_URI`) is present, Hermes runs in
**Mongo remote mode**. The agent binary stays on the PC; durable state lives
in a remote MongoDB replica set. The same X.509 client cert unlocks Mongo
**and** the control-plane orchestrator (mTLS).

## Detect mode

```bash
hermes storage status
# Mongo mode: ON  → remote architecture
# Mongo mode: OFF → classic local $HERMES_HOME files
```

Do **not** tell a Mongo-mode user to edit `~/.hermes/config.yaml` or
`MEMORY.md` as the source of truth — those are local-only / migration paths.

## What lives where

| Data | Local (classic) | Mongo remote |
|------|-----------------|--------------|
| Settings | `config.yaml` | profile DB `config` + shared `settings` |
| Secrets | `.env`, `auth.json` | profile DB `secrets` |
| Identity | `SOUL.md` | profile DB `soul` |
| Memory | `memories/*.md` | profile DB `memories` |
| Skills | `$HERMES_HOME/skills/` | `hermes_shared` + GridFS (materialized to local cache) |
| Sessions | `state.db` | profile DB `sessions` / `messages` |
| PC-specific cwd/docker/MCP/browser | in `config.yaml` | `machine_<id>` overlay collection |
| Cluster presence / active agent | n/a | `hermes_shared` + orchestrator `:8744` |

**On the PC in Mongo mode, only keep:**

```
$HERMES_HOME/bootstrap.yaml
$HERMES_HOME/certs/ca.crt
$HERMES_HOME/certs/agent.pem
```

(+ optional logs, skill cache, PIDs).

## Install / enroll (user-friendly)

**Control plane (server with Mongo + orchestrator):**

```bash
./scripts/install-control-plane.sh   # or .ps1 on Windows
hermes agent add                     # prints one-time code, waits ~5 min
```

**Agent PC:**

```bash
hermes db connect
# Address: 192.168.1.10:8743
# Code:    K7Q2-M9XB
```

Receives certs, writes bootstrap, pings Mongo. After that, cluster tools use
mTLS to `https://<server>:8744`.

Legacy offline bundles: `deploy/control-plane/scripts/enroll-agent.sh` +
`scripts/install-agent.sh --bundle …`.

## Ports on the control plane

| Port | Role | Auth |
|------|------|------|
| 27017–27019 | Mongo replica set | X.509 (SCRAM lab fallback) |
| **8744** | Orchestrator API | **mTLS — no client cert ⇒ handshake dropped** |
| 8743 | Enroll (one-time code) | Code only (pre-cert) |

## CLI the agent should use

```bash
hermes storage status|migrate|init-bootstrap
hermes agent add [--name PC] [--ttl 300] [--hosts host:27017,...]
hermes db connect [--host IP:8743] [--code CODE]
hermes machine show|list|set-overlay
hermes cluster status
hermes cluster activate <hostname|machine_id|node_id>
```

In-chat: tools `cluster_status` / `cluster_activate`, slash `/cluster`.

## Config merge in Mongo mode

Effective config =

```
hermes_shared.settings
  ⊕ hermes_profile_<name>.config
  ⊕ machine_<id> overlay   # terminal.cwd, docker, browser, local mcp_servers, api bind
```

Shared behavioral settings (model, memory policy, …) are remote.
Machine-local paths/backends are per-PC overlays — use `hermes machine set-overlay`,
not a hand-edited local yaml as the primary store.

`hermes config set` still works: it persists into Mongo (profile + overlay split)
when Mongo mode is on.

## Messaging gateway handoff

Only **one** Telegram/Discord/… gateway may own a bot token.

`cluster activate` → old PC releases messaging → new PC acquires after health-check.
Failure → rollback to previous owner + chat notify.
Owner dies → auto-failover (default) to another online node.

## Secrets

Prefer X.509 (no password in URI). Bootstrap `tls.cert_key_file` is secret —
mode `0600`. Do not commit `agent.pem` or control-plane `ca.key`.

## Invariants for Mongo mode

- **Fail-hard** — if Mongo is unreachable, raise `MongoStorageError`; never
  silently use local `state.db` / `config.yaml` / `.env` / `SOUL.md` / memory
  files (that splits active/passive fleet state). Fix connectivity or remove
  `bootstrap.yaml` to leave Mongo mode.
- Unknown SessionDB methods on the Mongo adapter raise `AttributeError`
  (no silent stubs).
- Never instruct “edit MEMORY.md / SOUL.md on disk” as the durable path —
  use the `memory` tool / soul store (they write Mongo when configured).
- Never open a second messaging gateway for the same bot on another PC.
- Never point the agent at orchestrator over plain HTTP without certs.
- Skills scripts still need a local FS cache — `get_skills_dir()` materializes
  from GridFS automatically (and fails hard if materialize cannot reach Mongo).
