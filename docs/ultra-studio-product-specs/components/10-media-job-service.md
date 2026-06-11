# Media Job Service

Status: partial — synchronous image/video generation tools with provider
registries and Atlas submit/poll clients are implemented; the durable
MediaJob envelope, `ultra_media_job_*` tool group, job persistence, and job
events are spec-only.
Date: 2026-06-11

Sources:

- Docs: `docs/ultra-studio-product-specs/03-media-asset-contract.md`
  (§Media Job Envelope, §Required Job Tools, §Asset Lifecycle, §QA),
  `02-agent-runtime-contract.md` (§Event Stream, §Error Contract),
  `04-skill-tool-prompt-contract.md` (§Media Job Tools),
  `06-delivery-plan.md` (P0 items 5-7, Launch Gates),
  `docs/hermes-asset-library-backend-design.md` (§生成链路,
  `generation_jobs` entity)
- Code (verified this session): `tools/video_generation_tool.py`
  (`_handle_video_generate`, `_resolve_active_provider`,
  `check_video_generation_requirements`, `_format_model_caveats`,
  `_build_dynamic_video_schema`, `_normalize_reference_images`),
  `tools/image_generation_tool.py`, `agent/video_gen_provider.py`,
  `agent/video_gen_registry.py`, `agent/image_gen_provider.py`,
  `agent/image_gen_registry.py`, `agent/image_routing.py`,
  `plugins/video_gen/atlas/client.py` (`submit`, `poll`, `build_payload`,
  `extract_prediction_id`, `first_output_url`, `normalize_image_input`,
  `resolve_credentials`), `plugins/video_gen/atlas/catalog.py`
  (`ATLAS_FAMILIES`), `plugins/image_gen/atlas/` (`catalog.py`,
  `client.py`), plus `fal`/`xai`/`openai` provider plugins under
  `plugins/image_gen/`, `plugins/video_gen/`

## Purpose & Scope

The Media Job Service runs image/video/audio generation as durable,
provider-neutral jobs. The contract rule: "Provider APIs should not be
exposed raw to the agent. Use a provider-neutral job envelope"
(`03-media-asset-contract.md` §Media Job Envelope). A media job may outlive
a websocket reconnect, browser refresh, or worker restart
(`02-agent-runtime-contract.md`).

Scope: job creation/status/cancel/retry/finalize, the MediaJob envelope,
provider adapters, polling, output registration handoff to the Asset
Service, and job events. Model/constraint metadata is
`19-model-catalog-provider-constraints.md`; prompt payload construction is
`13-prompt-compiler.md`; credential policy is `17-tokenrouter.md`.

## Implementation Status

| Status | Item | Citation |
|---|---|---|
| Implemented | Agent video generation tool with config-driven provider/model resolution | `tools/video_generation_tool.py` (`_read_configured_video_provider`, `_resolve_active_provider`, `_handle_video_generate`) |
| Implemented | Agent image generation tool with routing | `tools/image_generation_tool.py`, `agent/image_routing.py` |
| Implemented | Provider registry layer (Atlas, FAL, xAI, OpenAI) behind provider ABCs | `agent/video_gen_registry.py`, `agent/image_gen_registry.py`, `plugins/video_gen/`, `plugins/image_gen/` |
| Implemented | Atlas async submit + poll with prediction-id extraction and output-URL discrimination (polling URLs are not outputs) | `plugins/video_gen/atlas/client.py` (`submit`, `poll`, `extract_prediction_id`, `first_output_url`, `_looks_like_media_output`) |
| Implemented | Server-side credential resolution (no key in prompts/UI) | `plugins/video_gen/atlas/client.py` (`resolve_credentials`); launch gate "Atlas credential path is explicit" (`06-delivery-plan.md`) |
| Implemented | Reference-image normalization (local file -> data URI) | `plugins/video_gen/atlas/client.py` (`normalize_image_input`, `_image_to_data_uri`) |
| Implemented | Per-model constraint surfacing into the tool schema and caveats | `tools/video_generation_tool.py` (`_build_dynamic_video_schema`, `_format_model_caveats`), `plugins/video_gen/atlas/catalog.py` (`ATLAS_FAMILIES` durations/resolutions/audio) |
| Specified, not built | Durable MediaJob record (envelope with `job_id`, `session_id`, `run_id`, `tool_call_id`, `tokenrouter_decision_id`, …) | `03-media-asset-contract.md` §Media Job Envelope; the current tool call is synchronous in-turn with no persisted job row |
| Specified, not built | `ultra_media_job_create / status / cancel / retry / finalize` tool group | `03-media-asset-contract.md` §Required Job Tools; zero `media_job` hits in code (rg, this session) |
| Specified, not built | `media_job.created` / `media_job.updated` gateway events | `02-agent-runtime-contract.md` §Event Stream |
| Specified, not built | Output registration into the Asset Service (`finalize` -> assets, thumbnails, lineage) | `03-media-asset-contract.md`; `hermes-asset-library-backend-design.md` §生成链路 |
| Specified, not built | Job survival across worker/session interruption | `06-delivery-plan.md` P2 gate |
| Gap | Cancel/retry semantics per provider (which Atlas routes support cancel) | unspecified |

## User Entry Points

- Chat generation requests routed by `workflow-router` into the media job
  path (`12-workflow-router.md`; today: direct tool calls
  `tools/image_generation_tool.py` / `tools/video_generation_tool.py`).
- Inspector retry/repair action on a failed job (planned,
  `03-inspector-live-panel.md`).
- Task restore re-attaching active jobs (planned,
  `07-tasks-session-history.md`).
- No direct user-facing API; everything flows through agent tools and
  gateway events.

## Feature List

| Feature | Status |
|---|---|
| Text-to-video and image-to-video via Atlas model routes | Implemented (`ATLAS_FAMILIES` `text_model`/`image_model` routes) |
| Image generation via Atlas/FAL/xAI/OpenAI providers | Implemented (provider plugins + registries) |
| Reference image inputs (upload -> data URI) | Implemented (`normalize_image_input`) |
| Provider/model selection from config with availability checks | Implemented (`check_video_generation_requirements`, `_resolve_active_provider`) |
| Model caveat messaging to the agent (durations, resolution, audio) | Implemented (`_format_model_caveats`) |
| Async polling of provider predictions | Implemented in-turn (`poll`); durable cross-turn polling planned |
| Durable job records with status queries | Planned |
| Cancel a queued/running job | Planned (`ultra_media_job_cancel`) |
| Retry with compiled repair plan | Planned (`ultra_media_job_retry`; pairs with `prompt-repair` skill) |
| Finalize: register outputs as assets with lineage + thumbnails | Planned (`ultra_media_job_finalize`) |
| Job events to the UI | Planned |
| Seed capture and reproducibility params in the envelope | Planned (envelope field exists in spec; not all providers return seeds) |
| TokenRouter decision linkage (`tokenrouter_decision_id`) | Planned; depends on `17-tokenrouter.md` |

## State Machine

Job lifecycle (`03-media-asset-contract.md`):

```text
job.created -> job.running -> job.succeeded -> asset.processing -> asset.ready
job.created | job.running -> job.failed
job.created | job.running -> job.canceled        (where provider supports)
job.running -> job.timeout                       (maps to `job_timeout` error)
```

- `created -> running`: provider accepted the submission
  (`extract_prediction_id` returns an id today).
- `running -> succeeded`: poll returns a real media output URL — a polling
  or status URL must never be treated as output
  (`first_output_url` / `_looks_like_media_output` encode this rule).
- `succeeded -> asset.*`: finalize hands off to the Asset Service; the job
  is not "done" for the UI until `asset.ready`.
- `failed` keeps the full provider error and remains inspectable
  (`03-media-asset-contract.md` §Acceptance).
- Retry creates a new job linked to the old one; it never mutates the failed
  record.

## APIs & Events

Implemented (agent tool surface): `generate_video` / image generation tools
registered via the tool registry (`model_tools.py` dispatch); provider calls
through `plugins/*/atlas/client.py` `submit`/`poll` against the Atlas API
(`ATLAS_API_KEY` server-side via `resolve_credentials`).

Planned tool group (verbatim, `03-media-asset-contract.md` §Required Job
Tools):

| Tool | Purpose |
|---|---|
| `ultra_media_job_create` | Create image/video/audio job with structured inputs. |
| `ultra_media_job_status` | Return durable job state and progress. |
| `ultra_media_job_cancel` | Cancel queued/running job if supported. |
| `ultra_media_job_retry` | Retry with compiled repair plan. |
| `ultra_media_job_finalize` | Register outputs as assets, thumbnails, lineage. |
| `ultra_media_constraints_get` | Return model/provider limits before prompt compile. |

Planned events: `media_job.created`, `media_job.updated`, then `asset.ready`
(`02-agent-runtime-contract.md` §Event Stream). Job progress for the UI rides
`tool.progress` until durable jobs land.

## Data Model

Implemented: none persisted — job state lives in the tool call's in-memory
flow for the duration of a turn.

Planned: the MediaJob envelope (verbatim fields,
`03-media-asset-contract.md`):

```yaml
MediaJob:
  job_id:
  session_id:
  run_id:
  tool_call_id:
  provider:
  model:
  media_type:
  mode:
  status:
  input_assets:
  prompt:
  negative_prompt:
  provider_constraints:
  seed:
  tokenrouter_decision_id:
  output_assets:
  error:
```

Boundary note: `hermes-asset-library-backend-design.md` defines a
`generation_jobs` table on the Asset Service side; reconciling that table
with this envelope (single owner + event mirror) is an open question shared
with `09-asset-service.md`.

## UI Behavior

(Service obligations; rendering specs live in `02-creative-chat-ui.md` and
`03-inspector-live-panel.md`.)

- Every job submission yields a job card datum: id, provider/model,
  media_type, status, progress.
- Status updates are pushed, not polled by the browser; refresh re-hydrates
  from durable state (planned).
- Failed jobs expose the typed error and provider error class for the
  inspector's repair plan.
- Outputs render only after asset registration (`asset.ready`), preventing
  fake completion claims — "The agent cannot claim completion without an
  event, artifact, or ledger record" (`00-index.md` §Top-Level Acceptance).

## Permissions & Error Handling

Jobs execute under the session's user/workspace/project scope; input asset
refs must pass Asset Service validation and (when present) TokenRouter
ACL/quota checks before submission (`hermes-asset-library-backend-design.md`
§生成链路).

Typed errors (from `02-agent-runtime-contract.md` §Error Contract):
`missing_credential`, `unsupported_model_capability`, `invalid_asset_ref`,
`provider_rejected_input`, `quota_exceeded`, `job_timeout`,
`asset_upload_failed`.

Implemented today: missing provider/credential produces an explicit tool
error (`_missing_provider_error`, `check_video_generation_requirements`);
Atlas poll errors surface to the agent rather than faking success.

Hard rules (launch gates, `06-delivery-plan.md`): no fake media URLs, no
hardcoded job results, no accidental FAL/Comfy fallback — provider switching
is config, never a silent runtime fallback.

## Acceptance Criteria

- A clear video request creates a real provider job and returns either a
  real output or a typed blocker (`04-skill-tool-prompt-contract.md`
  §Acceptance).
- Polling URLs are never registered as outputs (regression-guarded by
  `first_output_url` behavior).
- Once durable jobs land: refreshing the browser mid-job preserves the job;
  `ultra_media_job_status` returns the same state the UI shows.
- `finalize` produces asset ids with lineage linking job, inputs, model,
  prompt hash, seed (`03-media-asset-contract.md` §Lineage).
- A failed job remains inspectable with provider error class intact.
- Cancel on a supporting route transitions to `canceled` without phantom
  outputs.
- No code path constructs a media URL that was not returned by a provider.

## Non-Goals

- Owning asset state after registration (Asset Service owns it).
- Provider credential storage or exchange (TokenRouter scope).
- Prompt construction logic (Prompt Compiler scope) — the job service takes
  compiled payloads.
- Media preprocessing/trimming pipelines (CometAPI scope,
  `18-cometapi-media-gateway.md`).
- Exposing raw provider dashboards or API shapes to the agent or UI.

## Open Questions

1. Durable job store location: gateway-side DB vs asset-service
   `generation_jobs` — who is the single writer?
2. Polling ownership once jobs are durable: gateway background worker vs
   per-session resume polling; what happens to in-flight polls on worker
   restart?
3. Cancel support matrix per Atlas route (Wan/Seedance/Kling families in
   `ATLAS_FAMILIES`) is unverified.
4. Seed availability: which providers return seeds, and is `seed` required
   for the reproducibility promise or best-effort?
5. Mapping from the current `generate_video`/image tool contracts to
   `ultra_media_job_create`: rename-with-alias is disallowed by repo
   conventions — migrate tool names in one pass or keep current names as the
   implementation of the `ultra_*` contract?
6. Concurrency limits per provider/workspace before TokenRouter exists —
   config-level caps or unlimited in P0?
7. Audio jobs (`audio_job` type exists in the asset contract): which
   provider backs them first, and does TTS (`tools/tts_tool.py`,
   `agent/tts_provider.py`) fold into this service or stay separate?
