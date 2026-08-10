# Hermes Agent — Compact Operating Contract

This repository is Hermes Agent: one agent core shared by CLI, gateway, TUI,
desktop, dashboard, ACP, profiles, plugins, MCP, skills, and cron.

## Non-negotiables

- Preserve per-conversation prompt caching. Do not mutate prior context,
  rebuild the system prompt mid-conversation, or change toolsets mid-session.
- Preserve strict message-role alternation.
- Keep the core tool schema narrow. Prefer an existing capability, CLI command,
  skill, service-gated tool, plugin, or MCP server before adding a core tool.
- Never put non-secret settings in `.env`; use `config.yaml` and `hermes config`.
- Keep secret and PII redaction enabled. Never print credentials, tokens, or
  private keys. Use least privilege and explicit approval for consequential
  writes.
- Validate real behavior, not only mocked unit paths. Run focused tests and the
  smallest relevant end-to-end check before claiming completion.
- Do not add speculative hooks, duplicate stores, outbound telemetry, or
  vendor-specific integrations to the core tree.

## Development workflow

1. Read the relevant source and tests before editing.
2. Reproduce the symptom with a tight test or CLI probe.
3. Make the smallest root-cause fix and add regression coverage.
4. Run focused tests, lint/compile checks, and `git diff --check`.
5. Verify live behavior when changing gateway/config/runtime code.
6. Preserve contributor credit and avoid unrelated formatting churn.

## Repository conventions

- Use `uv` for Python dependency resolution and the locked matrix.
- Use the managed Hermes Node/npm toolchain for JavaScript work.
- Use `AGENTS.md` files for portable project rules and skills for reusable
  procedures; detailed Hermes contributor guidance is in
  `docs/agent-guides/AGENTS-reference.md`.
- Keep package-local instructions close to the package they govern.
- Prefer read-only MCP integrations first; write-capable integrations require
  explicit scope, approval, and tests.

## Verification commands

```bash
uv lock --check
pytest <focused-tests> -q
ruff check <changed-files>
python -m py_compile <changed-python-files>
git diff --check
hermes doctor
hermes config check
```

## Safety boundaries

- Treat production, customer, financial, credential, and destructive actions
  as approval-gated even when a test or script appears routine.
- Do not broaden access to private URLs, messaging platforms, MCP servers, or
  filesystem paths without documenting the scope and verifying redaction.
- Do not install a second institutional memory daemon or database; use the
  existing APC/OpenClaw graph/wiki bridge where applicable.
