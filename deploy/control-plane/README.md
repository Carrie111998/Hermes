# Hermes Control Plane

Remote MongoDB (replica set) + certificate authority that **authorizes agent PCs**.

There are **two installers** in this project:

| Script | Where | Purpose |
|--------|-------|---------|
| [`scripts/install-control-plane.sh`](../../scripts/install-control-plane.sh) / `.ps1` | Always-on server | Mongo RS + CA + enroll |
| [`scripts/install-agent.sh`](../../scripts/install-agent.sh) / `.ps1` | Each PC | Hermes binary + bootstrap/certs |

## Ports on the control plane

| Port | Service | Auth |
|------|---------|------|
| 27017–27019 | MongoDB RS | X.509 (or SCRAM lab fallback) |
| **8744** | **Orchestrator API** | **mTLS — client cert required or connection dropped** |
| 8743 | Enroll (one-time code) | Code only (pre-cert bootstrap) |

Agents call `https://<server>:8744/cluster/*` with the same `agent.pem` used for Mongo.
Without that cert the TLS handshake fails — no anonymous orchestrator access.

**On the server** (after `install-control-plane.sh`):

```bash
hermes agent add
# or: hermes agent add --name home-pc --ttl 300 --hosts 192.168.1.10:27017,192.168.1.10:27018,192.168.1.10:27019
```

You get a one-time code (e.g. `K7Q2-M9XB`). The server waits ~5 minutes.

**On the agent PC:**

```bash
hermes db connect
```

Enter the server address (`IP:8743`) and the code. The PC downloads its X.509
bundle, writes `bootstrap.yaml` + `certs/`, and verifies Mongo.

Legacy offline bundles (`./scripts/enroll-agent.sh --name …`) still work.

## Agent authorization (X.509)

Preferred model: **mutual TLS with client certificates**.

1. Control plane generates a private CA (`certs/ca.key` — never leave the server).
2. `enroll-agent.sh --name home-pc` issues a client cert:
   - Subject: `CN=home-pc,OU=hermes-agents,O=Hermes`
   - Registers that subject as a MongoDB `$external` user with read/write on
     `hermes_shared` + `hermes_profile_<profile>`
3. The PC receives a **bundle**:
   - `bootstrap.yaml` (URI with `authMechanism=MONGODB-X509`, no password)
   - `certs/ca.crt`
   - `certs/agent.pem` (certificate + private key)
4. Hermes connects with TLS; Mongo authenticates the cert. Compromising one PC
   does not reveal other agents’ keys. Revoke by dropping the `$external` user.

### Fallback: SCRAM user/password

Lab/dev only: credentials in `certs/app-credentials.txt` and a URI like
`mongodb://hermesApp:***@hosts/?replicaSet=rs0`. Prefer X.509 for real fleets.

### Optional online enroll API

```bash
# on control plane
HERMES_ENROLL_TOKEN=... docker compose --profile enroll up -d
# on agent PC
./scripts/install-agent.sh --enroll-url http://cp:8743/enroll --token "$TOKEN" --name home-pc
```

## Quick start

```bash
# Server
./scripts/install-control-plane.sh
# Edit deploy/control-plane/.env → HERMES_MONGO_HOSTS=your.lan.ip:27017,...
cd deploy/control-plane && ./scripts/enroll-agent.sh --name home-pc

# PC (copy bundles/home-pc or home-pc.zip first)
./scripts/install-agent.sh --bundle ./home-pc
hermes storage status
hermes cluster status
```

Windows: `.\scripts\install-control-plane.ps1` and `.\scripts\install-agent.ps1`.

## Lab (no TLS, for integration tests)

```bash
docker compose -f deploy/control-plane/docker-compose.lab.yml up -d
export HERMES_MONGO_URI='mongodb://127.0.0.1:27017/?directConnection=true'
pytest tests/hermes_storage/test_mongo_integration.py -m integration -q
```

## Layout

```
deploy/control-plane/
  docker-compose.yml
  .env.example
  certs/                 # CA, server PEM, keyfile (gitignored secrets)
  bundles/<name>/        # per-agent enrollment output
  scripts/
    gen-ca.sh
    init-replica.sh
    enroll-agent.sh
    enroll_server.py
```
