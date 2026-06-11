# CometAPI Media Gateway

Status: spec-only — explicitly a future service, "not an MVP default
capability"; zero code exists (rg for `cometapi`/`comet` over the repo
returns no source hits, verified this session).
Date: 2026-06-11

Sources:

- Docs: `docs/hermes-cometapi-media-gateway.md` (entire doc; itself sourced
  from `docs/notion-source/hermes/pages/07-cometapi.md`),
  `docs/hermes-tokenrouter-credential-flow.md` (control-plane counterpart),
  `docs/ultra-studio-product-specs/02-agent-runtime-contract.md`
  (§Error Contract), `03-media-asset-contract.md` (asset refs),
  `00-index.md` (source references)
- Code: none. The nearest implemented machinery is small-file handling in
  the upload/attachment path and vision tools (`tools/vision_tools.py`,
  `tools/transcription_tools.py`), which are per-turn tools, not a media
  data plane.

## Purpose & Scope

CometAPI is the media data-plane gateway for images, audio, and video. It is
separate from TokenRouter by design: TokenRouter handles control
(credentials, policy, quota, billing); CometAPI handles large binary/media
retrieval, trimming, sampling, packaging, and native multimodal injection
(`hermes-cometapi-media-gateway.md` §范围).

Build trigger: "should not be built into the first web MVP unless the
product needs long-video analysis, external social URL ingestion, or
expensive multimodal preprocessing at scale" (§范围). Until then, the MVP
path is normal upload storage plus simple file parsing, with `video_analyze`
kept as a tool contract backed by a simple worker (§MVP 定位).

Scope: responsibility layers, the `video_analyze`/`audio_analyze` tool
contracts, the TokenRouter relationship, caching, and failure rules.

## Implementation Status

| Status | Item | Citation |
|---|---|---|
| Specified, not built | Resolver layer for external media URLs (YouTube, TikTok, Instagram, direct uploads) | `hermes-cometapi-media-gateway.md` §职责 |
| Specified, not built | Physical preprocessing: time-window trim, frame downsampling, transcode, audio/subtitle extraction | §职责 |
| Specified, not built | Multimodal packaging: timestamp-aligned frames + audio + transcript + metadata | §职责 |
| Specified, not built | Model injection: convert processed media into target-model native multimodal parts | §职责 |
| Specified, not built | Tenant-safe processed-chunk cache keyed on source/time-window/fps/resolution/scope | §职责, §失败策略 |
| Specified, not built | `video_analyze` / `audio_analyze` tool contracts | §请求形态 |
| Specified, not built | Delegated-token integration with TokenRouter, usage signals back to control plane | §与 TokenRouter 的关系 |
| Gap | MVP "simple worker path" for `video_analyze` also does not exist yet — the tool contract is unimplemented in any form (no `video_analyze` hits in `tools/`, verified this session) | §MVP 定位 |

## User Entry Points

None directly; CometAPI is agent-infrastructure. Reached via:

- Agent tool calls `video_analyze` / `audio_analyze` during creative
  workflows ("analyze this YouTube reference", "what happens at 02:10").
- Workflow skills that take external reference URLs as inputs (UGC,
  cinematic flows in `04-skill-tool-prompt-contract.md` P1 skills).
- Never via raw large binaries in the prompt: "accept a stable media ID or
  URL, not raw large binaries from the Agent prompt" (§请求形态).

## Feature List

| Feature | Status |
|---|---|
| `video_analyze(video_source, prompt, start/end offsets, fps, media_resolution, text_only)` | Planned (contract defined verbatim in §请求形态) |
| `audio_analyze` equivalent | Planned |
| External URL resolution incl. social platforms | Planned |
| Bounded time-window analysis without full-frame context stuffing | Planned (acceptance: 30-minute video analyzable through a bounded window) |
| Frame/audio cache with tenant-safe keys | Planned |
| Usage signal emission to TokenRouter/control plane | Planned |
| Text-only fallback explicitly marked when frames/audio unavailable | Planned (§失败策略) |
| MVP interim: simple worker behind the same tool contract | Planned, precedes the service itself |

## State Machine

A CometAPI request is a bounded pipeline, not a long-lived stateful object:

```text
received -> resolved -> preprocessing -> packaged -> injected -> completed
              |             |                                     |
              v             v                                     v
        resolve_failed  preprocess_failed                  model_call_failed
(any stage) -> cache_hit shortcut for previously processed chunks
```

- `resolved`: source fetched or located (cache key computed from source,
  time window, fps, resolution, access scope).
- `cache_hit`: identical chunk reused — must validate tenant scope before
  reuse (§失败策略: "Cached media chunks must be tenant-safe").
- Failures are terminal per request and must be structured; there is no
  retry state inside the gateway (callers decide).

## APIs & Events

Tool contract (verbatim, §请求形态):

```text
video_analyze(
  video_source,
  prompt,
  start_offset_sec?,
  end_offset_sec?,
  fps?,
  media_resolution?,
  text_only?
)
```

Flow (§未来架构):

```text
Agent tool call
  -> TokenRouter policy and quota check
  -> CometAPI delegated media request
  -> resolver/download/cache
  -> trim/downsample/transcode
  -> multimodal packaging
  -> model call through approved provider route
  -> result and usage event returned to Agent
```

Division of labor with TokenRouter (§与 TokenRouter 的关系): TokenRouter owns
JWT validation, provider secrets, quota/billing; CometAPI receives verified
scoped requests (or validates a delegated token), performs large binary
fetch and preprocessing, builds native multimodal payloads, and emits usage
signals back. No gateway events are defined for the UI; results return on
the tool channel (`tool.progress`/`tool.complete`).

## Data Model

Planned (no persistence exists):

```text
media_chunks (cache)
- chunk_id
- source            (url or asset/media id)
- time_window       (start_sec, end_sec)
- fps, resolution
- access_scope      (tenant/workspace boundary key)
- artifact_keys     (frames, audio, transcript, metadata in object storage)
- created_at, last_hit_at

cometapi_requests (audit)
- request_id, run_id, tool_call_id
- tenant_id, workspace_id, project_id
- source, time_window, params
- cache_hit: bool
- status, error_class
- usage_event_id    (links into TokenRouter usage records)
```

Cache keys must include access scope — identical public URLs fetched for two
tenants may share, but private uploads never cross tenants (§验收检查:
"Tenant A cannot reuse Tenant B's private upload cache").

## UI Behavior

CometAPI has no UI of its own. Obligations to the UI through the tool
channel:

- Long analyses surface as `tool.progress` updates, not silence.
- A text-only degraded response is visibly marked as degraded
  ("Text-only fallback is allowed only when the response explicitly marks
  that frames/audio were unavailable", §失败策略) — the chat renderer must
  show that marker.
- Resolver failures render as structured tool errors with a user-actionable
  message (bad URL, region-blocked, private video), never as fabricated
  analysis (§失败策略: no "silently switch to fake analysis").

## Permissions & Error Handling

- Every request passes TokenRouter policy first (tool scope, quota, tenant)
  before CometAPI does any fetch (§未来架构).
- Social/external resolver failures must not leak proxy credentials or
  internal fetch infrastructure details (§失败策略).
- Error classes (minimum): `resolve_failed`, `unsupported_source`,
  `window_out_of_range`, `preprocess_failed`, `quota_exceeded` (from
  TokenRouter), `provider_rejected_input` — mapped onto the typed error
  channel of `02-agent-runtime-contract.md`.
- Degradation rule: partial results (e.g. frames ok, transcript missing)
  must enumerate what is missing; silent partial analysis is forbidden.

## Acceptance Criteria

Verbatim checks from `hermes-cometapi-media-gateway.md` §验收检查:

- A 30-minute video can be analyzed through a bounded time window without
  placing all frames in prompt context.
- Repeated analysis of the same URL/time window hits cache.
- Tenant A cannot reuse Tenant B's private upload cache.
- A failed resolver returns a structured error that the UI can display.
- TokenRouter can trace the CometAPI request into usage and billing records.

Plus the MVP-phase check: `video_analyze` exists as a tool contract backed
by the simple worker path, and swapping the backend to CometAPI later does
not change the tool's schema.

## Non-Goals

- Building this service for the first web MVP (explicit in §范围/§MVP 定位).
- Generation of media (Media Job Service scope) — CometAPI only analyzes
  and packages inputs.
- Credential management or billing decisions (TokenRouter scope).
- A general scraping platform: resolvers exist for media analysis inputs,
  not bulk content harvesting.
- Storing analysis results as product assets automatically (callers decide
  what to persist via task files / Asset Service).

## Open Questions

1. Build trigger metrics: what measured threshold (repeat-analysis volume,
   preprocessing cost) justifies replacing the simple worker with the
   service?
2. The simple worker path itself: process-local in the gateway vs a queue
   worker; where do interim artifacts (extracted frames) live — task file
   root?
3. Delegated token shape: does CometAPI validate `HF_JWT_TOKEN` itself or
   only accept TokenRouter-forwarded verified requests?
4. Social platform resolver legality/ToS boundaries per platform — which
   resolvers ship at all?
5. Which models' "native multimodal parts" are targeted first (the
   packaging layer is model-specific by definition)?
6. Cache eviction policy and storage budget for processed chunks.
