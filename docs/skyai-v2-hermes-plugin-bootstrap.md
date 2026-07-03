# SkyAI Hermes v2 Plugin Bootstrap

Date: 2026-07-03

## Decision

SkyAI v2 starts as a plugin/skills layer on top of clean upstream Hermes
(`NousResearch/hermes-agent`) rather than a fork that patches core files.

This keeps daily upstream sync small:

- upstream Hermes changes are merged into a clean base;
- SkyAI changes stay in `plugins/skyai_customer/`, skills, config, tests, and
  external services;
- customer-facing runtime has no Muncho/admin/DevOps capabilities.

## Initial Capability Slice

The first slice adds:

- public catalog search tool;
- public product detail tool;
- public product slots tool;
- sanitized local append-only event stub;
- dedicated `skyai-v2-dev` profile bootstrap script;
- DEV-only FAB-compatible canary endpoint;
- operator skill describing SkyAI v2 boundaries, tone, BookNow/campaign
  knowledge, and learning loop.

## Not In This Gate

- No PROD traffic switch.
- No Cloud SQL provisioning.
- No Redis provisioning.
- No Discord bot permission changes.
- No customer/order/payment/voucher mutation.
- No Muncho canonical brain writes.

## Next Gates

1. Enable the plugin in a dedicated SkyAI v2 Hermes profile.
2. Run the DEV canary gateway locally or behind a DEV-only ingress.
3. Add Cloud SQL `skyai_ci.events` insert-only backend behind explicit
   `SKYAI_CI_DATABASE_URL` / `SKYAI_CI_EVENT_WRITE_ENABLED` gates.
4. Mirror every canary thread to the SkyAI Discord channel.
5. Run side-by-side live canary with instant rollback to current PROD SkyAI.

## DEV Canary Bootstrap

```bash
python scripts/skyai_v2_bootstrap_dev_profile.py --apply
python -m plugins.skyai_customer.dev_gateway \
  --dev \
  --profile-home ~/.hermes/profiles/skyai-v2-dev
curl -X POST http://127.0.0.1:8787/chatkit/dev-message \
  -H 'Content-Type: application/json' \
  -d '{"conversation_id":"dev-smoke","message":"Здравей, търся подарък за двама"}'
```

The canary gateway is loopback/dry-run by default. Live model calls require
`--live-model`; public binds require an explicit token gate.

## Daily Upstream Sync Policy

Run daily after fetching `NousResearch/hermes-agent`:

```bash
git fetch origin --prune
python scripts/skyai_v2_upstream_sync_check.py origin/main
scripts/run_tests.sh \
  tests/plugins/test_skyai_customer_plugin.py \
  tests/plugins/test_skyai_customer_schema.py \
  tests/plugins/test_skyai_customer_dev_gateway.py \
  tests/scripts/test_skyai_v2_bootstrap_dev_profile.py \
  tests/scripts/test_skyai_v2_upstream_sync_check.py \
  -q
git diff --check
rg -n "^(<<<<<<<|>>>>>>>)" -S . -g '!node_modules' -g '!venv' -g '!.venv'
```

The sync check must stay green before any DEV or PROD canary work. If it fails,
SkyAI v2 has started touching Hermes core and needs a separate architecture
review before merge.
