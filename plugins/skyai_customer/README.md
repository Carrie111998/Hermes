# SkyAI Customer Plugin

SkyAI Customer is the first clean-room SkyVision customer-facing Hermes v2
plugin. It is intentionally narrow and public-safe:

- search SkyVision public catalog cache;
- fetch public product detail by URL/path;
- fetch public product slots by product id;
- append sanitized local/dev events for an append-only customer intelligence
  spine.

It does **not** include DevOps, Git, Render, GCP admin, Shopify admin, Muncho
brain, raw customer database, payments, voucher lookup, order lookup, or write
actions.

## Intended Runtime Boundary

Customer-facing Hermes may call this plugin. Muncho remains the internal
operator/supervisor and may observe sanitized reports, but SkyAI customer
memory must not be written into Muncho canonical brain.

## Event Log

`skyai_event_log_append` writes local JSONL by default:

```text
$HERMES_HOME/skyai_v2/events.jsonl
```

This is only a development stand-in. Production should move to a dedicated
Cloud SQL schema such as `skyai_ci.events` with append-only insert privileges.
Do not enable a generic `DATABASE_URL` fallback for SkyAI customer
intelligence.

## DEV Canary Gateway

Bootstrap the dedicated SkyAI v2 DEV profile:

```bash
python scripts/skyai_v2_bootstrap_dev_profile.py --apply
```

Start the FAB-compatible canary surface in dry-run mode:

```bash
python -m plugins.skyai_customer.dev_gateway \
  --dev \
  --profile-home ~/.hermes/profiles/skyai-v2-dev
```

Smoke it locally:

```bash
curl http://127.0.0.1:8787/health
curl -X POST http://127.0.0.1:8787/chatkit/dev-message \
  -H 'Content-Type: application/json' \
  -d '{"conversation_id":"dev-smoke","message":"Здравей, търся подарък за двама"}'
```

Dry-run is the default. Calling the live Hermes model requires the explicit
`--live-model` flag. Non-loopback binds require both `--allow-public-bind` and
a bearer token from `SKYAI_V2_CANARY_TOKEN`.
