# Charterforge Configuration Reference

Canonical state lives at `~/.charterforge` on POSIX and
`%LOCALAPPDATA%\charterforge` on Windows.

| File or directory | Purpose |
|---|---|
| `config.yaml` | Behavioral configuration and standing charter |
| `.env` | Secrets only |
| `objectives.db` | Governed business authority state |
| `kanban.db` | Default task board |
| `logs/` | Runtime logs |
| `backups/` | Backup artifacts |
| `profiles/` | Employee/profile-scoped configuration |

`CHARTERFORGE_HOME` overrides the state root. `HERMES_HOME` is accepted as a
lower-precedence migration alias. Canonical `CHARTERFORGE_*` values are mapped
process-locally for inherited internal readers when launched through the
`charterforge` command.

The `agentic` section controls business operation. Important fields include:

- `enabled`, `operating_mode`, `operator_role`, and `runtime_host`;
- `allowed_capabilities`, `allowed_systems`, and
  `forbidden_capabilities`;
- `solo_founder.toolsets` and `solo_founder.skills`;
- risk, irreversible-action, spend, permit-TTL, and resource limits;
- finance, tax profile, compliance, security, organization, and initial
  mandate settings.

Non-secret behavior belongs in `config.yaml`. Credentials, API keys, payment
provider secrets, and tokens belong in `.env` or an external secret source.

For long-running business operation, `agentic.security.require_runtime_baseline`
can require a human-accepted runtime baseline before cycles execute. Inspect it
with `charterforge business runtime-drift` and accept a reviewed change with
`charterforge business runtime-rebaseline --reason "..."`. Rebaselining does
not resume autonomy; the operator must separately resume it.

Use `charterforge config`, `charterforge setup`, and
`charterforge business --help` to inspect the implemented command surfaces.
