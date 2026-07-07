# SkyAI Voice Contract v0.1

Status: DEV HTTP adapter skeleton with tests. This gate does not deploy,
restart services, create PBX routes, register SIP endpoints, process audio, or
change production traffic.

## TL;DR

SkyAI voice should be a separate Voice Gateway in front of the existing SkyAI
chat backends. The PBX talks SIP/RTP to the gateway. The gateway handles call
state, audio codecs, STT, TTS, barge-in, silence timeout, and transfer. SkyAI
receives clean text turns plus call metadata through a stable adapter contract.

This keeps the future PBX work reusable when production moves from current
SkyAI v1 to SkyAI v2 Hermes. The gateway must target a stable SkyAI adapter,
not a frontend widget implementation.

## Current SkyAI Architecture

### Current production chat backend

Current production SkyAI is a chat-only backend behind the production ingress:

- environment: production;
- service identity: `skyai-hermes-assistant-prod`;
- current version family: `skyai-chat-prod.v*`;
- public chat surface: `POST /chatkit/message`;
- health/readiness surfaces: `GET /health`, `GET /ready`, `GET /version`;
- current customer model lane: `openai-codex` / `gpt-5.5` through the existing
  Codex OAuth runtime;
- response mode: final response only, not streaming;
- public tools: catalog cache, product detail, public slots, campaign facts,
  support facts, cards, and sanitized mirror evidence;
- no admin, DevOps, Shopify admin, order mutation, payment mutation, voucher
  mutation, raw analytics disclosure, or Muncho brain access.

### SkyAI v2 Hermes canary backend

SkyAI v2 is a Hermes profile plus `skyai_customer` plugin and a canary gateway:

- environment: DEV/canary;
- runtime: Hermes/AIAgent;
- toolset: `skyai_customer`;
- public-safe canary surfaces:
  - `GET /health`;
  - `GET /ready`;
  - `GET /version`;
  - `POST /chatkit/message`;
  - `POST /chatkit/dev-message`;
  - `POST /qa/compare`;
- response mode: final response only;
- context model: each turn sends a bounded conversation history to Hermes, with
  a stable conversation id. The canary gateway trims history to the most recent
  turns before calling Hermes.

### Auth model

The public widget path is protected at the ingress/app layer. The v2 canary
gateway requires bearer auth for non-loopback binds. A future Voice Gateway must
use server-to-server auth only:

- private network path plus bearer token for MVP;
- later mTLS or signed HMAC requests if the gateway is split across hosts;
- no browser/client-side token;
- no customer-provided secret in voice prompts.

### Escalation today

SkyAI can tell the customer how to reach the SkyVision team and should include
official contacts when it does so. The DEV voice adapter can return a
`transfer_to_human` action. The future Voice Gateway must execute that action
as PBX/SIP behavior and report it back to SkyAI operational logs.

## Voice Gaps

SkyAI chat does not currently provide:

- STT;
- TTS;
- realtime audio streaming;
- barge-in/interruption handling;
- call state;
- DTMF handling;
- silence timeout;
- call recording policy;
- verified caller identity;
- PBX transfer execution;
- per-call latency telemetry;
- voice-specific transcript retention controls.

## Recommended Voice Gateway Design

### MVP path: SIP extension to local Voice Gateway

Use a dedicated `SkyAI Voice Gateway` as a SIP user agent registered to the
office PBX as a test extension, for example `399`.

Known PBX assumptions:

- PBX: ZYCOO CooVox-U20 / Asterisk 1.8.7.1;
- SIP transport: UDP 5060;
- codecs: `alaw` preferred, `ulaw` fallback;
- DTMF: `rfc2833`;
- first route: test extension or test IVR option only.

Flow:

```text
PBX extension/IVR
  -> SIP/RTP to SkyAI Voice Gateway
  -> STT
  -> SkyAI Voice Adapter
  -> SkyAI v1 or v2 chat backend
  -> TTS
  -> RTP audio back to caller
```

This is safer than routing the legacy PBX directly to an external AI voice
endpoint. It lets us keep call transfer, logging, codec conversion, and
fallbacks under our control.

### Lowest-latency path

For the lowest latency and most natural turn-taking, the gateway should support
a realtime lane after the turn-based MVP:

- streaming audio in;
- streaming STT partials;
- VAD and endpointing;
- barge-in to cancel current TTS when the caller starts speaking;
- incremental TTS or speech-to-speech output;
- per-call latency metrics from speech end to first audio out.

OpenAI's Realtime API is the strongest OpenAI-native candidate for this lane:
official docs position realtime sessions as the path for live audio that needs
low latency, with `gpt-realtime-2` for low-latency voice agents and
`gpt-realtime-whisper` for streaming transcription. The same docs also expose
SIP as an option for telephony voice agents, but that path uses OpenAI API
authentication and project/SIP configuration.

### Hybrid OpenAI API audio + Hermes/OAuth reasoning

Approved MVP lane:

```text
PBX/SIP media gateway
  -> OpenAI API STT
  -> SkyAI /voice/turn
  -> Hermes/Codex OAuth reasoning
  -> OpenAI API TTS
  -> RTP audio back to caller
```

SkyAI text generation continues to use the existing Codex OAuth/Pro lane for
the business reply. STT/TTS use the OpenAI API through a dedicated DEV secret:

```text
VOICE_TOOLS_OPENAI_KEY
```

Do not reuse or print Codex OAuth material for audio. Do not commit this secret
to the repo. Prefer storing it in GCP Secret Manager or the VM service
environment for the DEV voice gateway only.

Initial OpenAI audio model candidates:

- STT primary: `gpt-4o-transcribe`;
- STT fast/cost fallback: `gpt-4o-mini-transcribe`;
- TTS: `gpt-4o-mini-tts`;
- first voice candidates for Bulgarian QA: `marin`, with `alloy` as a known
  compatibility fallback;
- later realtime transcription: `gpt-realtime-whisper`.

Run the DEV preflight before any live media call:

```bash
python scripts/skyai_voice_openai_audio_preflight.py --json
```

For deployment gates where the key must already be present:

```bash
python scripts/skyai_voice_openai_audio_preflight.py --require-key
```

The preflight does not call OpenAI and never prints the key. It verifies that
the audio layer is configured separately from the Hermes/OAuth reasoning lane.

After the dedicated key is installed in the DEV gateway environment, run the
billable live audio smoke explicitly:

```bash
python scripts/skyai_voice_openai_audio_smoke.py --live-openai --json
```

The live smoke sends one short Bulgarian text sample through TTS, feeds the
generated audio back through STT, reports latency and transcript, and deletes
the generated audio by default. It does not call SkyAI, PBX, SIP, RTP, Discord,
Shopify, vouchers, orders, payments, or production traffic.

### OAuth through Pro account boundary

Public OpenAI API docs describe API authentication through bearer API
keys or short-lived access tokens. ChatGPT Pro OAuth is not a supported audio
API auth path in the public OpenAI API contract. Therefore:

- Hybrid MVP can reuse Codex OAuth for the text reply only, while STT/TTS use
  OpenAI API billing through the dedicated audio key;
- the lowest-latency OpenAI Realtime/API path should be treated as a later
  explicit API-billing switch;
- the Voice Gateway contract must keep provider choice pluggable so we can
  switch from `hybrid_openai_api_audio_codex_oauth_reasoning` to
  `openai_realtime_api` without changing PBX routing or SkyAI prompts.

## Required API Contract

The Voice Gateway talks to SkyAI through a stable adapter. These endpoints are
registered on the DEV/canary HTTP gateway as transcript/event endpoints. They
do not accept raw audio and do not implement SIP, STT, TTS, RTP, or PBX
configuration.

### `POST /voice/start`

Starts a call session and returns initial assistant behavior.

Required request fields:

```json
{
  "call_id": "pbx-unique-call-id",
  "conversation_id": "skyai-voice-pbx-399-...",
  "caller_id": "+359...",
  "did": "+359...",
  "pbx_extension": "399",
  "department": "sales",
  "language": "bg-BG",
  "source": "zycoo-coovox-u20",
  "codec": "alaw",
  "dtmf": "rfc2833",
  "recording_notice_played": false,
  "metadata": {
    "ivr_path": "test",
    "office_hours_state": "open"
  }
}
```

Response:

```json
{
  "status": "ok",
  "call_id": "pbx-unique-call-id",
  "conversation_id": "skyai-voice-pbx-399-...",
  "action": "speak",
  "spoken_reply": "Здравейте, свързахте се със SkyVision...",
  "display_reply": "Optional transcript-safe text",
  "voice": {
    "language": "bg-BG",
    "style": "warm_skyvision"
  },
  "session_state": {
    "handoff_allowed": true,
    "recording_allowed": false
  }
}
```

### `POST /voice/turn`

Sends a user transcript turn to SkyAI. Partial transcripts are allowed only for
latency telemetry and barge-in decisions; SkyAI should answer final transcripts.

Request:

```json
{
  "call_id": "pbx-unique-call-id",
  "conversation_id": "skyai-voice-pbx-399-...",
  "turn_index": 2,
  "transcript": "Искам подарък за рожден ден около София.",
  "is_final": true,
  "stt_confidence": 0.91,
  "language": "bg-BG",
  "metadata": {
    "barge_in": false,
    "silence_ms": 650,
    "dtmf": null
  }
}
```

Response:

```json
{
  "status": "ok",
  "action": "speak",
  "spoken_reply": "Чудесно, нека го направим специален...",
  "display_reply": "Чудесно, нека го направим специален...",
  "cards": [],
  "transfer": null,
  "transfer_reason": null,
  "target": null,
  "end_call": false,
  "telemetry": {
    "skyai_latency_ms": 1200,
    "first_audio_budget_ms": 2500
  }
}
```

For `transfer_to_human` responses, the gateway should prefer the structured
`transfer` object when available, while `transfer_reason` and `target` duplicate
`transfer.reason` and `transfer.target` for simpler compatibility clients.

### `POST /voice/event`

Reports non-transcript call events:

- `dtmf`;
- `barge_in`;
- `silence_timeout`;
- `low_stt_confidence`;
- `caller_requested_human`;
- `gateway_error`;
- `tts_error`;
- `stt_error`.

SkyAI may return `clarify`, `transfer_to_human`, `end_call`, or `speak`.

### `POST /voice/end`

Ends a call and records sanitized summary metadata.

Request fields:

- `call_id`;
- `conversation_id`;
- `ended_by`: `caller`, `assistant`, `human_transfer`, `gateway_error`;
- `duration_seconds`;
- `summary_for_ops`;
- `recording_stored`: boolean;
- `transcript_stored`: boolean.

## Action Semantics

Allowed response actions:

- `speak`: synthesize `spoken_reply` and continue;
- `clarify`: synthesize a short clarification after low confidence, silence,
  or ambiguous request;
- `transfer_to_human`: ask PBX/gateway to transfer to an operator or queue;
- `end_call`: play final message and hang up.

Transfer must be available by:

- DTMF `0`;
- explicit caller request for operator/person;
- repeated STT/TTS failures;
- protected cases that need authenticated customer/order/voucher handling;
- repeated low-confidence turns.

## Latency, Quality, And Provider Options

### Turn-based MVP

MVP can be turn-based:

1. PBX audio arrives at the gateway.
2. Gateway waits for end-of-speech.
3. STT produces a final transcript.
4. Gateway calls SkyAI `/voice/turn`.
5. Gateway synthesizes the final text response.
6. Gateway plays audio back over RTP.

Initial target to validate:

- p50 speech-end to first audio: <= 2500 ms;
- p95 speech-end to first audio: <= 6000 ms;
- Bulgarian STT confidence visible in logs;
- transfer fallback on repeated failures.

This path is easier to build and can reuse the current SkyAI text lane, but it
will not feel as fluid as a full realtime speech-to-speech model.

To avoid dead air during slow model/tool turns, latency masking belongs to the
media gateway, not the SkyAI reasoning prompt. The gateway may play short,
non-semantic fillers after fixed timers, for example:

- after ~900 ms: "Проверявам.";
- after ~5500 ms: "Още секунда, почти съм готов.";
- when a tool path is known: "Гледам свободните варианти."

These fillers must not invent facts and must not replace the final SkyAI
answer.

### Realtime lane

For a more natural assistant, test a realtime lane:

- p50 first audio target: <= 900 ms after speech end;
- p95 first audio target: <= 1800 ms after speech end;
- barge-in supported;
- streaming transcript deltas;
- no full-turn silence before the assistant starts preparing the response.

OpenAI Realtime with `gpt-realtime-2` is the preferred OpenAI evaluation
candidate once API billing is approved. If we need no OpenAI API billing during
MVP, evaluate local or non-OpenAI STT/TTS providers behind the same gateway
contract.

## Privacy And GDPR

Default policy:

- do not store raw audio by default;
- store transcripts only if there is a clear operational/legal basis;
- redact voucher codes, payment data, access tokens, and secrets from logs;
- do not expose tracking, analytics, internal metrics, or customer intelligence
  back through SkyAI;
- announce recording before recording;
- keep call summaries sanitized in Discord/internal reports;
- do not perform customer/order/payment/voucher mutations over voice without a
  future verified-auth flow.

## MVP Validation

Recommended first MVP:

1. DEV/test only PBX extension, for example `399`.
2. Inbound calls only.
3. No recording by default.
4. STT/TTS provider hidden behind the Voice Gateway interface.
5. SkyAI text backend target is configurable:
   - `skyai_v1_chatkit`;
   - `skyai_v2_chatkit`.
6. DTMF `0` or "оператор" transfers to a human route.
7. End-to-end test matrix:
   - greeting;
   - gift recommendation;
   - voucher support;
   - BookNow explanation;
   - free flight campaign question;
   - low confidence/noisy audio;
   - silence timeout;
   - transfer request.

Before any PBX write or SIP/RTP integration, validate the HTTP adapter with the
DEV-only no-audio smoke:

```bash
python scripts/skyai_voice_contract_smoke.py \
  --base-url http://127.0.0.1:8787 \
  --backend-target skyai_v2_chatkit
```

The smoke calls `/voice/start`, `/voice/turn`, `/voice/event`, and `/voice/end`
with synthetic data and verifies canonical response fields, action values,
flattened transfer compatibility fields, `raw_audio_stored=false`, and
`end_call=true` on call end. It does not touch SIP, RTP, PBX, STT, TTS,
customers, orders, vouchers, payments, Discord, or production traffic.

Validate the OpenAI API audio setup separately:

```bash
python scripts/skyai_voice_openai_audio_preflight.py --require-key
```

## Open Questions

- Which STT/TTS provider gives the best Bulgarian quality under office-phone
  audio conditions (`alaw`/`ulaw`, 8 kHz)?
- Is call recording needed, and what consent text should be played?
- What is the expected concurrent call target for MVP and for production?
- Which operator queue should receive transfers outside office hours?
- Voice sessions mirror to the same SkyAI Discord channel for now:
  `1510888721614901358`. Voice threads must be visibly marked with `🎙️`, and
  DEV/QA calls must be visibly marked with `🧪 TEST`.
