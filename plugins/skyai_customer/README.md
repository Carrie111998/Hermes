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

## Architecture Contract

Read `ARCHITECTURE.md` before changing this plugin. The top-level rule is:
Hermes reasons; this backend provides public facts, structured evidence,
transport, cards, and safety boundaries. Do not add keyword routers,
customer-visible template logic, or one-off phrase guards around Hermes.

## Voice Contract

Future PBX/voice work is documented in `docs/skyai-voice-contract-v0.1.md`.
That contract keeps telephony concerns in a separate SkyAI Voice Gateway and
keeps this plugin as the public-safe customer knowledge/tool layer. The DEV
gateway exposes HTTP transcript/event adapter endpoints under `/voice/*`; it
does not include SIP, STT, TTS, RTP, PBX configuration, or production routing.

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

Bootstrap the dedicated SkyAI v2 DEV profile. Use `--inherit-model-config`
when the root Hermes config exists; it copies only non-secret provider/model
fields. VM canaries may pass the same non-secret fields explicitly:

```bash
python scripts/skyai_v2_bootstrap_dev_profile.py \
  --apply \
  --inherit-model-config \
  --model-default gpt-5.5 \
  --model-provider openai-codex \
  --model-base-url https://chatgpt.com/backend-api/codex \
  --model-api-mode codex_responses
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
`--live-model` flag. Private RFC1918 binds still require `--allow-public-bind`;
public or wildcard binds also require a bearer token from
`SKYAI_V2_CANARY_TOKEN`.

Voice adapter smoke can be simulated without audio:

```bash
curl -X POST http://127.0.0.1:8787/voice/turn \
  -H 'Content-Type: application/json' \
  -d '{"call_id":"call-dev-1","conversation_id":"voice-dev-1","transcript":"Търся подарък за рожден ден.","is_final":true,"stt_confidence":0.95}'
```

For a fuller no-audio contract smoke across `/voice/start`, `/voice/turn`,
`/voice/event`, and `/voice/end`, use the DEV helper:

```bash
python scripts/skyai_voice_contract_smoke.py \
  --base-url http://127.0.0.1:8787 \
  --backend-target skyai_v2_chatkit
```

Against a private/GCP DEV endpoint, pass the endpoint and keep the bearer token
in the configured environment variable rather than on the command line:

```bash
SKYAI_V2_CANARY_TOKEN=... \
python scripts/skyai_voice_contract_smoke.py \
  --base-url https://<dev-skyai-endpoint> \
  --token-env SKYAI_V2_CANARY_TOKEN \
  --backend-target skyai_v2_chatkit
```

## DEV OpenAI Audio Preflight

The approved voice MVP keeps SkyAI reasoning on the Hermes/Codex OAuth lane and
uses OpenAI API only for STT/TTS in the media gateway. Configure the audio
secret on the DEV gateway host only:

```text
VOICE_TOOLS_OPENAI_KEY
```

Then verify the non-secret setup without calling OpenAI or printing the key:

```bash
python scripts/skyai_voice_openai_audio_preflight.py --require-key
```

Initial audio candidates are `gpt-4o-transcribe`,
`gpt-4o-mini-transcribe`, `gpt-4o-mini-tts`, and the voice candidates
documented in `docs/skyai-voice-contract-v0.1.md`. Realtime remains a later
canary behind the same voice gateway contract.

Voice calls are mirrored by the same DEV Discord sidecar when
`SKYAI_DISCORD_MIRROR_ENABLED=true` and
`SKYAI_DISCORD_MIRROR_CHANNEL_ID=1510888721614901358` are configured. The
voice mirror is an operational side effect of `/voice/start`, `/voice/turn`,
`/voice/event`, and `/voice/end`; it is not a model tool. DEV voice threads are
marked with `🎙️` and `🧪 TEST` unless the gateway explicitly marks a future
production call as real/customer.
