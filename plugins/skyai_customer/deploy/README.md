# SkyAI Production Release Contract

## Verified current topology

As read on 2026-07-30, `skyai-prod-ingress` in
`adventico-ai-platform/europe-west3` is a Cloud Run **proxy only**. It has no
application command or SkyAI secret bindings; it forwards through
`SKYAI_UPSTREAM_BASE_URL`. The application runtime is:

```text
skyai-prod-ingress (Cloud Run proxy)
  -> skyai-runtime-prod-01 (VM)
  -> skyai-v2-hermes-prod.service (systemd)
  -> /opt/skyai-v2/releases/<immutable-release>
```

The live SkyVision Next bundle maps `skyvision.bg` and `www.skyvision.bg`
exactly to `skyai-prod-ingress`; DEV hosts map to the separate v2 DEV ingress.
Consequently, public website conversations traverse this PROD proxy-to-VM path.

The public PROD `/version` baseline observed on 2026-07-30 reports
`behavior=v2.7`, build
`92fd0078b20f42f3d0227f8f47f04a5cf7bb8fca`, profile
`/var/lib/skyai/codex/profiles/skyai-v2-prod`, and `live_model=true`, with no
new mirror implementation marker. Preserve that baseline and resolve its full
active release path during the owner-authenticated preflight before any
cutover; this commit is the exact current PROD rollback anchor.

The sanitized 2026-07-25 rollout receipt is historical evidence of the release
mechanism, not the current head. It records an immutable source archive, an
active release below `/opt/skyai-v2/releases/`, a retained rollback release,
candidate imports using the existing SkyAI service environment, one bounded
systemd restart, and internal/public health checks. It does **not** establish
the repository-root Dockerfile as the current PROD application build.

The exact live `ExecStart`, interpreter, and environment-file paths still need
owner-authenticated read-only capture at release time. Do not guess them and do
not turn the proxy into the application runtime.

## Current VM release gate

`skyai-v2-hermes-prod.service.d/20-production-gateway.conf.template` is a
repository-owned render input for the existing VM service. It is not installed
and is intentionally invalid while any `*_MUST_BE_BOUND` sentinel remains.
Before an approved release, the release owner must:

1. Build an immutable candidate release from the reviewed Git commit outside
   the active symlink.
2. Resolve the service's actual Python interpreter and environment-file paths
   from the live unit without printing secret values.
3. Install
   `plugins/skyai_customer/requirements-discord-mirror.txt` into the candidate
   service environment, or equivalently sync the exact
   `skyai-discord-mirror` package extra from `pyproject.toml` and `uv.lock`.
4. Under that exact interpreter and service environment, call
   `load_production_settings(os.environ)` and
   `verify_production_dependencies()` from
   `plugins.skyai_customer.production_gateway`. Either failure blocks cutover.
5. Bind the exact private VM address as `SKYAI_PRODUCTION_BIND_HOST` and the
   exact Serverless VPC Access connector network as
   `SKYAI_TRUSTED_PROXY_CIDR`. Authorization uses the transport peer address
   only; `Forwarded` and `X-Forwarded-For` never participate. A dedicated
   ingress bearer token may be bound as an additional authorization path, but
   is not required when the exact trusted proxy boundary is present.
6. Bind the Discord bot token and mirror-only PostgreSQL URL through the
   existing owner-controlled secret/environment mechanism. No secret belongs
   in the release archive or this repository.
7. Render the drop-in with the exact candidate release, interpreter, and
   environment-file paths. Its `ExecStart` has no `--dev` compatibility path.
8. Apply the mirror-only schema and least-privilege grants in a separately
   approved database gate, then validate the rendered unit before the one
   bounded service cutover.
9. Preserve the previous release target for automatic rollback and verify
   `/health`, `/version`, and `/ready`. `/ready` stays false until the durable
   worker completes its first successful database poll.

The existing DEV command remains separate:

```bash
python -m plugins.skyai_customer.dev_gateway --dev
```

Production must use:

```bash
python -m plugins.skyai_customer.production_gateway
```

The production module accepts no command-line flags, including `--dev`.

## Future Cloud Run reference

`future-cloud-run-app-runtime.template.yaml` is an explicitly future, inert
reference for a possible application-runtime migration. It is not the current
release manifest and must never be applied to `skyai-prod-ingress`. Its raw
service name, image, identity, VPC, profile, build, and secret sentinels keep it
unapplyable until a separately designed and approved migration exists.
