# SkyAI Hermes v2 Plugin Bootstrap

Date: 2026-07-03

## Current Canonical Status

As of 2026-07-24, SkyAI v2 is the production customer assistant and this
plugin worktree is the only canonical source for future SkyAI behavior.
Production health reports the Hermes runtime with the `skyai_customer` toolset.

The former standalone repository `lomliev/skyvision-hermes-ai-assistant` is a
read-only historical v1/clean-room archive. Its separate `skyai_v1` DEV ingress
may remain temporarily available as frozen comparison evidence, but it is not
the production source and must not receive new behavior or deployment work.
See `docs/skyai-v1-legacy-archive.md`.

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
- curated public campaign knowledge tool;
- Discord mirror v2 sidecar for canary conversations;
- DEV-only comparison endpoint for SkyAI v2 canary vs current PROD SkyAI;
- sanitized local append-only event stub;
- dedicated `skyai-v2-dev` profile bootstrap script;
- DEV-only FAB-compatible canary endpoint;
- operator skill describing SkyAI v2 boundaries, tone, BookNow/campaign
  knowledge, and learning loop.

## Not In This Gate

- No PROD traffic switch.
- No Cloud SQL provisioning.
- No Redis provisioning.
- No Discord bot permission changes in code; mirror activates only when the
  DEV runtime is configured with a bot token and channel id.
- No customer/order/payment/voucher mutation.
- No Muncho canonical brain writes.

## Next Gates

1. Enable the plugin in a dedicated SkyAI v2 Hermes profile.
2. Run the DEV canary gateway locally or behind a DEV-only ingress.
3. Add Cloud SQL `skyai_ci.events` insert-only backend behind explicit
   `SKYAI_CI_DATABASE_URL` / `SKYAI_CI_EVENT_WRITE_ENABLED` gates.
4. Mirror every canary thread to the SkyAI Discord channel.
5. Run side-by-side live canary through `/qa/compare` with instant rollback to
   current PROD SkyAI.

## DEV Canary Bootstrap

```bash
python scripts/skyai_v2_bootstrap_dev_profile.py \
  --apply \
  --inherit-model-config \
  --model-default gpt-5.6-sol \
  --model-provider openai-codex \
  --model-base-url https://chatgpt.com/backend-api/codex \
  --model-api-mode codex_responses
python -m plugins.skyai_customer.dev_gateway \
  --dev \
  --profile-home ~/.hermes/profiles/skyai-v2-dev
curl -X POST http://127.0.0.1:8787/chatkit/dev-message \
  -H 'Content-Type: application/json' \
  -d '{"conversation_id":"dev-smoke","message":"Здравей, търся подарък за двама"}'
curl -X POST http://127.0.0.1:8787/qa/compare \
  -H 'Content-Type: application/json' \
  -d '{"conversation_id":"compare-smoke","message":"Има ли масаж в София?"}'
```

The canary gateway is loopback/dry-run by default. Live model calls require
`--live-model`. Private RFC1918 binds still require `--allow-public-bind`;
public or wildcard binds also require an explicit token gate.

Optional DEV-only environment gates:

- `SKYAI_DISCORD_MIRROR_ENABLED=1`
- `SKYAI_DISCORD_BOT_TOKEN=...`
- `SKYAI_DISCORD_MIRROR_CHANNEL_ID=1510888721614901358`
- `SKYAI_DISCORD_MIRROR_CREATE_THREADS=1`
- `SKYAI_COMPARE_PROD_BASE_URL=https://<current-prod-skyai>`

The Discord mirror and comparison endpoint are gateway sidecars, not model
tools. The customer-facing Hermes model cannot call Discord, mutate PROD, or
read internal reports.

Every enabled chat and voice conversation gets its own thread under channel
`1510888721614901358`. Chat and voice are distinguished only by the exact
structural surface; request text, headers, URLs, IPs, and test/customer labels
never classify or route a mirror.

## Three-Hour Upstream Sync Policy

The deterministic upstream-sync rail is documented in
`docs/skyai-v2-upstream-sync-automation.md`. Its default mode is read-only:

```bash
python3 scripts/skyai_v2_upstream_sync_routine.py
```

The scheduled mode may build/test and push one fork-only candidate PR:

```bash
python3 scripts/skyai_v2_upstream_sync_routine.py --execute --push-pr
```

It runs every three hours and never auto-merges or deploys. For a manual
verification after fetching `NousResearch/hermes-agent`:

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

The recurring rail also stops on dirty canonical state, unknown merge
conflicts, concurrent execution, or any failed SkyAI/voice/architecture/schema
test. Runtime deployment remains a separate explicit gate.
