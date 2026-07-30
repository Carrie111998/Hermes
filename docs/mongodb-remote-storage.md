# MongoDB remote storage for Hermes

Hermes can keep durable state in a remote MongoDB replica set so the agent
binary is portable across PCs. On each machine you only keep a bootstrap file
(+ TLS client cert).

## Two installers

| Role | Script |
|------|--------|
| **Control plane** (DB + CA + enroll) | [`scripts/install-control-plane.sh`](../scripts/install-control-plane.sh) / `.ps1` |
| **Agent PC** | [`scripts/install-agent.sh`](../scripts/install-agent.sh) / `.ps1` |

Details: [`deploy/control-plane/README.md`](../deploy/control-plane/README.md).

### Friendly enroll (recommended)

```bash
# 1) Server (once)
./scripts/install-control-plane.sh

# 2) Server — issue a one-time code and wait (~5 min)
hermes agent add
# → Code: K7Q2-M9XB
# → tell the PC: hermes db connect

# 3) Agent PC
hermes db connect
# Address: 192.168.1.10:8743
# Code:    K7Q2-M9XB
```

Offline USB bundles still work: `enroll-agent.sh` + `install-agent.sh --bundle …`.

## How agents authenticate

**Preferred — X.509 client certificates (Mongo + orchestrator)**

1. Control plane owns a private CA.
2. Enrolling a PC issues `CN=<name>,OU=hermes-agents,O=Hermes`.
3. That cert is required for:
   - **MongoDB** (`authMechanism=MONGODB-X509`)
   - **Orchestrator API** on `:8744` (**mTLS** — no client cert ⇒ handshake dropped)
4. One-time enroll on `:8743` is the only path that does not need a cert yet
   (chicken-and-egg); after `hermes db connect` everything else is cert-gated.

Revoke a stolen laptop by deleting its `$external` user and its cert will no
longer open Mongo or the orchestrator.

**Fallback — SCRAM user/password** in the URI (labs only). See
`deploy/control-plane/certs/app-credentials.txt` after install.

**Optional enroll HTTP API** (`docker compose --profile enroll`): agents call
`POST /enroll` with a bearer token to download a fresh bundle.

## Database layout

| Database | Contents |
|----------|----------|
| `hermes_shared` | skills, knowledge, shared settings, cluster orchestrator, `agent_registry` |
| `hermes_profile_<name>` | soul, memory, secrets, config, sessions, per-PC `machine_*` overlays |

## Per-PC overlays

```bash
hermes machine show
hermes machine list
hermes machine set-overlay --file overlay.yaml
```

## Cluster / active agent

```bash
hermes cluster status
hermes cluster activate HOME-PC
```

From chat: tools `cluster_status` / `cluster_activate`, slash `/cluster`.

Messaging gateway moves with a lease handoff (one Telegram bot owner at a time),
with rollback and auto-failover.

## Fail-hard (no silent local fallback)

When Mongo mode is on (`bootstrap.yaml` / `HERMES_MONGO_URI`), Hermes **never**
silently falls back to local durable state (`state.db`, `config.yaml`, `.env`,
`SOUL.md`, memory files, classic skills dir). A Mongo outage raises
`MongoStorageError` instead of writing/reading only on that PC — otherwise
active/passive fleets would split brain.

Classic local layout is used **only** when Mongo mode is off.

Unknown `SessionDB` methods on the Mongo adapter raise `AttributeError`
(no silent `__getattr__` stubs).

## Lab integration (no TLS)

```bash
docker compose -f deploy/control-plane/docker-compose.lab.yml up -d
export HERMES_MONGO_URI='mongodb://127.0.0.1:27017/?directConnection=true'
pytest tests/hermes_storage/test_mongo_integration.py -q
```

## Dependency

`pymongo` is a core dependency. Without bootstrap URI Hermes keeps the classic
local `$HERMES_HOME` file/SQLite layout.
