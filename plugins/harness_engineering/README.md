# Harness Engineering Plugin

Hermes plugin for Harness / Agenting Engineering soft preflight and task intake.

The bundled copy uses the repo skill helper at
`skills/software-development/harness-agenting-engineering/scripts/harness_intake.py`.
Existing profile installs that provide `~/.hermes/bin/hermes-harness` continue to
work as a fallback.

## Install into a Hermes profile

```bash
mkdir -p ~/.hermes/plugins
cp -R plugins/harness_engineering ~/.hermes/plugins/harness_engineering
```

Restart Hermes WebUI/gateway/CLI after installation.

## Modes

Preferred persistent config:

```yaml
harness_engineering:
  preflight_mode: advisory  # advisory | strict | off
```

`HERMES_HARNESS_PREFLIGHT` is still supported as an operator override for local
experiments and tests.

The plugin registers `/intake`, `hermes harness ...`, and `pre_gateway_dispatch`.

## Task classification

Phase 2 adds `hermes harness classify` as an advisory-only classifier. It returns
stable routing fields for chat, WebUI, gateway, or future Kanban intake without
starting work, writing state, or changing dispatch behavior.

```bash
hermes harness classify --text "Refactor auth token storage and add tests" --format json
```

High-risk, multi-agent, or scheduled/ops tasks are marked `intake_required`;
small code changes, research, and plain chat remain advisory/direct routes.
