# Hermes Enterprise — Deployment Packaging

Packaging for the Hermes Enterprise control plane: one container image, a
Helm chart for the controller, and a smoke script.

## Topology

```
┌───────────────────────────────────────────────────────────────┐
│ cluster                                                       │
│                                                               │
│  namespace: hermes-enterprise (chart install ns)              │
│  ┌─────────────────────────────┐                              │
│  │ OCC controller (Deployment) │  python -m enterprise.cli    │
│  │  • resource store (SQLite   │  ← PVC /opt/data             │
│  │    on PVC) + audit log      │                              │
│  │  • OCC API (Service +       │  ← Ingress (OIDC at ingress) │
│  │    optional Ingress)        │                              │
│  └──────────────┬──────────────┘                              │
│                 │ creates/reconciles at runtime               │
│                 ▼                                             │
│  namespace: hermes-<org-a>          namespace: hermes-<org-b> │
│  ┌──────────────────────────┐       ┌────────────────────┐    │
│  │ gateway (Deployment)     │       │ gateway ...        │    │
│  │  python -m hermes_cli    │       │                    │    │
│  │    .main gateway run     │       │                    │    │
│  │ + ServiceAccount,        │       │                    │    │
│  │   NetworkPolicy,         │       │                    │    │
│  │   agent workload pods    │       │                    │    │
│  └──────────────────────────┘       └────────────────────┘    │
└───────────────────────────────────────────────────────────────┘
```

- **OCC controller** — the single control-plane pod. Owns the enterprise
  resource store (SQLite on a PVC) and the audit trail, and provisions one
  namespace per org (prefix `hermes-`) with a gateway Deployment,
  ServiceAccount, and NetworkPolicies.
- **Per-namespace gateway** — one Hermes gateway per org, created by the
  controller at runtime (not by this chart). Runs the same image with the
  command overridden to `python -m hermes_cli.main gateway run`
  (foreground mode — the pod is the supervisor).
- **Agent workloads** — spawned inside the org namespace by the gateway;
  isolated by the controller-managed NetworkPolicies.

## Image

One image serves both roles (build from the repo root):

```bash
docker build -f deploy/enterprise/Dockerfile -t hermes-enterprise:dev .
```

- Multi-stage on `python:3.12-slim`; `pip install .` into `/opt/hermes/.venv`.
- Runs as non-root user `hermes` (UID 10000), state under `/opt/data`.
- Default entrypoint: `python -m enterprise.cli` (controller).
  Gateway pods override the command.

## Install

```bash
helm install occ deploy/enterprise/helm \
  --namespace hermes-enterprise --create-namespace \
  --set image.repository=ghcr.io/nousresearch/hermes-enterprise \
  --set image.tag=v0.1.0
```

Expose the API behind OIDC (annotations depend on your auth proxy):

```bash
helm upgrade occ deploy/enterprise/helm -n hermes-enterprise \
  --set ingress.enabled=true \
  --set ingress.host=occ.example.com \
  --set-string 'ingress.annotations.nginx\.ingress\.kubernetes\.io/auth-url=https://oauth2.example.com/oauth2/auth'
```

Smoke test (docker build if available, venv import fallback otherwise):

```bash
bash deploy/enterprise/smoke.sh
```

## Values

| Key | Default | Description |
| --- | --- | --- |
| `image.repository` | `ghcr.io/nousresearch/hermes-enterprise` | Controller image |
| `image.tag` | chart appVersion | Controller image tag |
| `image.pullPolicy` | `IfNotPresent` | Pull policy |
| `gatewayImage.repository` | same image | Image the controller uses for org gateways (`OCC_GATEWAY_IMAGE`) |
| `gatewayImage.tag` | chart appVersion | Gateway image tag |
| `occ.port` | `8800` | OCC API port |
| `occ.extraArgs` | `[]` | Extra args to `python -m enterprise.cli` |
| `occ.env` / `occ.envFrom` | `[]` | Extra env / secret refs for the controller |
| `occ.resources` | 100m/256Mi → 1/1Gi | Controller resources |
| `persistence.enabled` | `true` | PVC for SQLite state + audit |
| `persistence.storageClass` | `""` (cluster default) | StorageClass |
| `persistence.size` | `5Gi` | PVC size |
| `serviceAccount.create` / `.name` | `true` / fullname | Controller SA |
| `rbac.create` | `true` | ClusterRole/Binding for namespace + gateway management |
| `rbac.namespacePrefix` | `hermes-` | Org namespace prefix enforced by the controller |
| `service.type` / `.port` | `ClusterIP` / `8800` | OCC API service |
| `ingress.enabled` | `false` | Ingress for the OCC API |
| `ingress.annotations` | `{}` | **Put your OIDC auth annotations here** |
| `ingress.host` / `.path` / `.tls` | `occ.example.com` / `/` / `[]` | Routing |

### RBAC notes

The controller needs cluster-scoped grants because Kubernetes RBAC cannot
scope by namespace *name pattern*: it manages `namespaces` (create/delete
`hermes-<org>`), and within them `deployments`, `serviceaccounts`,
`networkpolicies`, plus `configmaps`/`secrets`/`services`/`pvcs` for gateway
plumbing and read-only `pods`/`logs`/`events`. The `hermes-` prefix
restriction is enforced in the controller and recorded in its audit log.

### Why one replica?

The controller keeps its resource store in SQLite on a ReadWriteOnce PVC.
The Deployment is pinned to `replicas: 1` with a `Recreate` strategy so an
upgrade never runs two writers against the same database file.
