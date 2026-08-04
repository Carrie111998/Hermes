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

## Behavior Versioning

SkyAI has two version markers:

- `version` is the technical runtime line, for example `skyai-hermes-v2.canary`.
- `behavior_version` is the customer-facing behavior line, for example `v2.2`.

Increase `behavior_version` only for meaningful behavior changes: new customer
knowledge domains, sales/support policy changes, prompt architecture changes,
voice behavior changes, or changes that affect what customers see/hear. Do not
increase it for formatting-only fixes, tests, refactors, logging, deployment
wiring, or typo-only patches.

Current line:

- `v2.0` - initial SkyAI Hermes v2 canary baseline.
- `v2.1` - BookNow/checkout completion boundary, voice handoff behavior, and
  recent customer-safe business knowledge refinements.
- `v2.2` - catalog selection context for broad searches: Hermes receives
  diverse evidence ordering, category mix, and repeated-category signals
  without backend pruning or keyword routing.
- `v2.3` - campaign gift entitlement knowledge: gifts/bonuses are not manual
  voucher-code entries; they link automatically through logged-in orders or the
  order email.
- `v2.4` - campaign bonus transfer nuance: the bonus stays linked to the buyer
  by default, while founder-approved human exceptions may use Emil's public
  phone and SkyVision's founding flight story.
- `v2.5` - session continuity: Hermes treats prior turns as shared context,
  checks its draft against its own earlier answers, removes unnecessary
  repetition, and repeats earlier details only when the customer explicitly
  asks or prior information must be corrected. No backend deduplication or
  keyword guard is added.
- `v2.6` - voucher issuer boundary: before giving account, activation, or
  redemption steps, Hermes reasons about who issued the voucher. SkyVision
  profile flows apply only to SkyVision-issued vouchers; externally issued
  vouchers remain under their issuer even when the same experience is listed
  by SkyVision. No brand-specific classifier or router is added.
- `v2.7` - contextual voucher and contact judgment: an unspecified voucher in a
  SkyVision conversation is normally treated as SkyVision-issued, while Hermes
  asks about the issuer only when the conversation creates a concrete
  compatibility doubt. Written customer contact uses `info@skyvision.bg`;
  `reservations@skyvision.bg` remains an automated notification sender, not a
  reply channel. No issuer classifier, keyword guard, or contact router is
  added.
- `v2.8` - campaign-gift time and validity reasoning: receipt of the main
  voucher is not treated as the purchase or entitlement-creation date; Hermes
  checks historical campaign terms, separate validity, use state, and current
  usability before ownership, transfer, exception, or escalation guidance.
  Missing evidence permits only a possible-expiry statement and a lookup, not
  an expiry fact or promised exception. No runtime status classifier, phrase
  matcher, or response guard is added.
- `v2.9` - service-specific reservation cancellation policy reasoning: Hermes
  uses exact service context plus the existing public product-detail tool to
  refresh current structured `cancellationPolicy` facts by canonical slug. The
  structured field has precedence over product prose; missing/fetch-failed
  detail stays unverified. No universal cancellation-hour constant, classifier,
  router, or customer-answer template is added.
- `v2.10` - free panoramic campaign-flight reservation knowledge: Hermes treats
  the bonus as a separate zero-price campaign entitlement, answers the profile
  self-service booking path (`Ваучери` → `Резервирай` → available slot → online
  completion), applies BookNow unlock, 24h reservation, 72h cancellation, and
  weather rebooking boundaries, and avoids paid MTO-Sport/CAVALON detours,
  pilot-contact-as-normal-booking, and invented early-arrival rules. No keyword
  router, catalog blacklist/filter, template, or post-processing adapter is
  added.

## Canonical Source

This plugin in `lomliev/hermes-agent` is the only source for future SkyAI
customer behavior. The former standalone repository
`lomliev/skyvision-hermes-ai-assistant` is a read-only historical archive and
must not receive new prompt, knowledge, evaluation, voice-brain, or deployment
work.

The `skyai_v1_chatkit` voice target is retained only as a wire-compatibility
label for historical DEV comparison. It is not a second business brain and
does not make the archived repository a runtime source for SkyAI v2.

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
  --model-default gpt-5.6-sol \
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
documented in `docs/skyai-voice-contract-v0.1.md`.

For the approved low-latency canary, validate the Realtime configuration
separately. This does not call OpenAI, open a WebSocket, process audio, touch
PBX/SIP/RTP, or print secrets:

```bash
python scripts/skyai_voice_openai_realtime_preflight.py --require-key
```

Realtime voice uses OpenAI for the live audio loop and keeps SkyAI v2 as the
Hermes knowledge/tool brain. Gateway-owned repeated filler phrases are not
allowed in this lane; any short spoken bridge while tools run must be
contextual and model-owned.

Voice calls are mirrored by the same DEV Discord sidecar when
`SKYAI_DISCORD_MIRROR_ENABLED=1`,
`SKYAI_DISCORD_MIRROR_CREATE_THREADS=1`, and
`SKYAI_DISCORD_MIRROR_CHANNEL_ID=1510888721614901358` are configured, with the
bot secret supplied only through `SKYAI_DISCORD_BOT_TOKEN`. The voice mirror is
an operational side effect of `/voice/start`, `/voice/turn`, `/voice/event`,
and `/voice/end`; it is not a model tool. Each exact conversation id maps to
its own structural voice thread. Request content or provenance never assigns
test/customer meaning or changes the destination.

## Durable Discord Mirror Deployment Boundary

Production mirroring is a dedicated SkyAI operational outbox. It never uses
Canonical Brain, the `skyai_ci` event credential, or a generic
`DATABASE_URL`. The only accepted database secret is:

```text
SKYAI_DISCORD_MIRROR_DATABASE_URL
```

The current PROD application runs on the VM-owned
`skyai-v2-hermes-prod.service`; `skyai-prod-ingress` is only its Cloud Run
proxy. The immutable VM release must install the exact plugin-edge driver from
the `skyai-discord-mirror` package extra (or the equivalent exact requirements
file) into the service's candidate environment. For a local DEV runtime,
install the same exact requirement manually. Apply the mirror-only schema
before enabling the production gate:

```bash
python -m pip install -r plugins/skyai_customer/requirements-discord-mirror.txt
psql "$SKYAI_DISCORD_MIRROR_DATABASE_URL" \
  -f plugins/skyai_customer/schema/discord_mirror_delivery_v1.sql
```

Use a login such as `skyai_discord_mirror_runtime` with only `USAGE` on the
`skyai_discord_mirror` schema and `SELECT`, `INSERT`, and `UPDATE` on its
`threads` and `deliveries` tables. The migration revokes public access and
contains the exact recommended grants as comments, but deliberately does not
create a login or embed a password. The runtime role must not inherit any
Canonical Brain, `skyai_ci`, or general application role.

Production must run
`python -m plugins.skyai_customer.production_gateway`, using the fail-closed VM
release contract under `deploy/`. That entrypoint accepts no `--dev` escape
hatch and requires the dedicated DSN, Discord token, exact mirror channel,
immutable build identity, pinned driver, exact private bind address, and exact
trusted proxy CIDR before it listens. The trusted boundary uses only the
socket peer address; forwarded headers are ignored. A bearer token is an
optional additional authorization path. The separate `dev_gateway --dev` path
remains available for local DEV only. The request handler writes the exact
chat/voice mirror envelope and its lossless Discord chunks before any Discord
network call. If that insert fails, the HTTP request returns 503; if the insert
succeeds but Discord is unavailable, the customer response may complete while
the persisted delivery remains queued for bounded-lease retries.

Each durable request must carry a caller-created top-level string
`delivery_id`. The widget creates it before the HTTP request and the caller
must reuse it only for an exact replay of that same turn. The same stored
envelope is idempotent while two legitimately identical turns remain distinct
when they have different ids. This does **not** make model execution
exactly-once: a repeated HTTP request can run the model again, and a changed
response under an existing id is rejected as a conflicting exact envelope.

The worker contract is truthfully at-least-once. Before posting a starter or
content chunk, it scans retrievable Discord message history for the exact
deterministic nonce; zero matches posts, one match resumes from that message,
and multiple matches fail as ambiguous. New posts also use
`enforce_nonce=true`, but no permanent remote exactly-once claim is made.
Deliveries are claimed one at a time, and an owned lease is renewed during
bounded Discord I/O. Thread creation is serialized by a Postgres advisory lock
over the exact surface/channel/conversation identity. The recovery name is
hash-only and contains the full SHA-256 digest, so no raw conversation id is
placed in the thread name or starter. Multiple exact matches are treated as an
error rather than guessed.

The outbox stores a SHA-256 conversation identity and restricted transient raw
content/chunks. Successful deliveries retain that raw payload for seven days
by default (`604800` seconds); the worker then redacts only `delivered` rows in
bounded batches. `pending`, `leased`, and `retry` payloads are never eligible
for redaction, so retention cleanup cannot destroy an undelivered mirror.
`/ready` remains false until the worker completes its first successful database
poll. `/health` and `/ready` expose raw backlog counts, oldest-undelivered time,
retry facts, the at-least-once contract, and typed worker posture without
returning the DSN or bot secret.

The disposable Postgres integration harness is gated by
`SKYAI_TEST_POSTGRES_DSN`:

```bash
SKYAI_TEST_POSTGRES_DSN='postgresql://...' \
  pytest -q tests/plugins/test_skyai_customer_discord_delivery_postgres.py
```

The JSON thread map remains only as an explicit local/DEV compatibility path.
It is not the production persistence boundary and does not provide
cross-instance recovery.
