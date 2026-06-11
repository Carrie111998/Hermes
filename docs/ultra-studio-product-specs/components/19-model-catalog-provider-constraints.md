# Model Catalog / Provider Constraints

Status: partial — Atlas media-model catalogs with per-family constraints
exist as code constants and feed dynamic tool schemas; LLM model metadata
and a Models admin page exist; the queryable `ultra_model_catalog` /
`ultra_media_constraints_get` tools and a unified constraints registry are
spec-only.
Date: 2026-06-11

Sources:

- Docs: `docs/ultra-studio-product-specs/04-skill-tool-prompt-contract.md`
  (§Model / Prompt Tools, §Prompt Compiler Boundary),
  `03-media-asset-contract.md` (`provider_constraints` envelope field,
  `ultra_media_constraints_get`), `01-product-surface.md` (model picker),
  `06-delivery-plan.md` (P1 item 4: "Prompt compiler and provider
  constraints registry", P3 item 3),
  `docs/higgsfield-supercomputer-dialogue-architecture-research.md`,
  `docs/ultra-studio-agent-manus-gap-research.md`
- Code (verified this session): `plugins/video_gen/atlas/catalog.py`
  (`ATLAS_FAMILIES`, `DEFAULT_MODEL`, `VALID_ASPECT_RATIOS`),
  `plugins/image_gen/atlas/catalog.py`, `tools/video_generation_tool.py`
  (`_build_dynamic_video_schema`, `_format_model_caveats`,
  `check_video_generation_requirements`), `agent/model_metadata.py`
  (provider/base-url detection, capability probing),
  `agent/models_dev.py`, `web/src/pages/ModelsPage.tsx`,
  `cli-config.yaml.example`

## Purpose & Scope

This component answers two questions for every other component, before
money is spent: "which models exist for this media type" and "what will this
model accept" (durations, resolutions, aspect ratios, audio support, input
counts, reference roles). The prompt compiler consumes constraints before
payload construction (`04-skill-tool-prompt-contract.md` §Prompt Compiler
Boundary); the router uses capability facts for clarification decisions
("model capability" is a legitimate ask trigger, §Clarification Rules).

Two catalogs exist conceptually and must not be conflated:

1. LLM/chat model catalog — which model runs the agent
   (implemented: `agent/model_metadata.py`, `agent/models_dev.py`,
   `web/src/pages/ModelsPage.tsx`).
2. Media model catalog — which models generate media and under what
   constraints (implemented as code constants per provider plugin;
   spec target is a queryable registry).

This spec covers the media catalog and constraints registry, plus the
boundary to the LLM catalog. Job execution is `10-media-job-service.md`.

## Implementation Status

| Status | Item | Citation |
|---|---|---|
| Implemented | Atlas video family catalog: display name, speed/price hints, strengths, text/image route ids, allowed durations, resolutions, audio flag | `plugins/video_gen/atlas/catalog.py` (`ATLAS_FAMILIES`; e.g. `wan-2.6-flash` -> durations (5,10,15), 720P, no audio) |
| Implemented | Atlas image catalog and routing helpers | `plugins/image_gen/atlas/catalog.py` |
| Implemented | Defaults and validation constants (default model, valid aspect ratios) | `plugins/video_gen/atlas/catalog.py` (`DEFAULT_MODEL`, `VALID_ASPECT_RATIOS`) |
| Implemented | Constraints injected into the agent-visible tool schema per active model | `tools/video_generation_tool.py` (`_build_dynamic_video_schema`) |
| Implemented | Human-readable caveats returned to the agent (duration/resolution/audio limits) | `tools/video_generation_tool.py` (`_format_model_caveats`) |
| Implemented | Provider availability checks before offering capability | `tools/video_generation_tool.py` (`check_video_generation_requirements`), config via `cli-config.yaml.example` |
| Implemented | LLM model metadata: provider inference from base URLs, local endpoint detection, capability flags | `agent/model_metadata.py` (`_infer_provider_from_url`, `is_local_endpoint`, `detect_local_server_type`), `agent/models_dev.py` |
| Implemented | Models admin UI for LLM endpoints | `web/src/pages/ModelsPage.tsx` |
| Specified, not built | `ultra_model_catalog` tool (queryable catalog for the agent) | `04-skill-tool-prompt-contract.md` §Model / Prompt Tools |
| Specified, not built | `ultra_media_constraints_get` tool (limits before prompt compile) | `03-media-asset-contract.md` §Required Job Tools |
| Specified, not built | Provider constraints registry as a service/data layer (vs per-plugin constants) | `06-delivery-plan.md` P1 item 4 |
| Specified, not built | Model recipes as marketplace items; model benchmarking reports | `05-memory-marketplace-files.md` §Marketplace; `06-delivery-plan.md` P3 item 3 |
| Gap | Per-model input-asset constraints (max reference images, mask support, role types) are not modeled in `ATLAS_FAMILIES` | — |

## User Entry Points

- Model picker in the chat composer (`01-product-surface.md` §Center) —
  user-facing selection among allowed media models (planned for media; the
  LLM picker exists via `ModelInfoCard`/`ModelPickerDialog` components in
  `web/src/components/`).
- Agent-side: router/compiler queries (`ultra_model_catalog`,
  `ultra_media_constraints_get`; planned).
- Inspector: shows provider/model and input constraints for the selected job
  (`01-product-surface.md` §Right).
- Admin: Models page for LLM endpoints (implemented); media model
  enablement via config (`cli-config.yaml.example`).

## Feature List

| Feature | Status |
|---|---|
| Enumerate available media models per media type with availability checks | Implemented in-plugin; not yet exposed as a tool |
| Per-family constraints: durations, resolutions, audio | Implemented (`ATLAS_FAMILIES`) |
| Aspect ratio validation | Implemented (`VALID_ASPECT_RATIOS`) |
| Dynamic tool schema reflecting active model constraints | Implemented (`_build_dynamic_video_schema`) |
| Capability caveats messaged to the agent | Implemented (`_format_model_caveats`) |
| Queryable catalog tool for router/compiler (`ultra_model_catalog`) | Planned |
| Pre-compile constraints fetch (`ultra_media_constraints_get`) | Planned |
| Input-asset constraints (reference count, roles, mask/edit support) | Planned |
| Workspace model allowlists (policy input to TokenRouter) | Planned (`17-tokenrouter.md` §策略输入: model allowlist) |
| Cost/price metadata beyond the informal `price` string | Planned |
| Model recipes (curated parameter presets) | Planned (Marketplace item kind) |
| Benchmark/quality reports per model | Planned (P3) |

## State Machine

Catalog entries are configuration, not stateful objects. Effective
availability of a model is derived:

```text
declared (in catalog constants)
  -> configured (provider + credentials present; check_*_requirements passes)
  -> allowed    (workspace allowlist / TokenRouter policy permits)
  -> selectable (UI/agent may choose it)
declared but unconfigured -> hidden or blocked-with-reason (never silently substituted)
deprecated route -> blocked-with-reason, existing lineage still resolvable
```

Rule: a model that fails the configured check must produce a typed
`missing_credential` / provider-missing error, not a fallback to another
provider (launch gate: "No accidental FAL/Comfy fallback",
`06-delivery-plan.md`).

## APIs & Events

Implemented (in-process): catalog lookups inside provider plugins
(`plugins/video_gen/atlas/catalog.py` helpers), schema/caveat injection at
tool-definition time (`model_tools.py` `get_tool_definitions` path), LLM
metadata fetch (`agent/models_dev.py`).

Planned tools (spec contracts):

```text
ultra_model_catalog(media_type?, provider?) ->
  [{ model_id, display, media_type, provider, route_ids,
     constraints_ref, price_hint, status }]

ultra_media_constraints_get(model_id) ->
  { durations, resolutions, aspect_ratios, audio,
    max_reference_images, input_roles, prompt_limits,
    negative_prompt_support, seed_support }
```

No gateway events; catalog changes are deploy/config-time. The model picker
reads the same catalog through a dashboard API (shape TBD).

## Data Model

Implemented: typed dicts in code —

```text
ATLAS_FAMILIES[model_key] = {
  display, speed, price, strengths,
  text_model,  # Atlas route for text-to-video
  image_model, # Atlas route for image-to-video
  durations: tuple, resolutions: tuple, audio: bool
}
```

Planned registry entity (the P1 "provider constraints registry"):

```text
model_catalog_entries
- model_id, provider, media_type
- display, description
- route_ids { text:, image:, edit:, … }
- constraints_json   (durations, resolutions, aspect_ratios, audio,
                      max_reference_images, input_roles, prompt_limits)
- price_meta
- status: active | deprecated
- version, updated_at
```

Migration rule: plugin constants remain the bootstrap source; the registry
must be seeded from them rather than hand-duplicated, so a single source of
truth survives (repo anti-duplication rule).

## UI Behavior

- The model picker shows display name, speed/price hint, and strengths —
  the same fields `ATLAS_FAMILIES` already carries — and only lists
  `selectable` models.
- Choosing a model updates composer affordances (e.g. duration choices,
  audio toggle) from constraints, mirroring how
  `_build_dynamic_video_schema` already constrains the agent.
- The inspector shows the selected job's model, route id, and the
  constraints that applied at submission time (frozen copy, since the
  catalog may change later).
- Unconfigured providers render as visible-but-blocked with the reason
  (missing credential/config), never hidden silently and never auto-swapped.

## Permissions & Error Handling

- Workspace model allowlists are policy inputs evaluated by TokenRouter
  (`17-tokenrouter.md`); the catalog only declares, policy decides. Until
  TokenRouter exists, config-level enablement is the gate.
- Typed errors: `unsupported_model_capability` (request violates
  constraints — e.g. 1080p on a 720P-only family), `missing_credential`
  (configured check fails), denial from policy
  ("A model outside the workspace allowlist is denied",
  `hermes-tokenrouter-credential-flow.md` §MVP 验收检查).
- Constraint violations must be caught pre-submission by the compiler using
  registry data; provider-side rejections (`provider_rejected_input`) are
  the backstop, not the primary validation.

## Acceptance Criteria

- The agent can enumerate available video models and their
  duration/resolution/audio limits without calling a provider (today via
  schema/caveats; post-P1 via `ultra_model_catalog`).
- A request exceeding a family constraint fails pre-flight with
  `unsupported_model_capability` and a corrected-options message.
- Disabling a provider in config removes its models from selectable state
  with a visible reason; no silent fallback occurs.
- The inspector shows the constraints snapshot for any past job.
- Catalog/registry and plugin constants cannot disagree (registry seeded
  from constants; CI check or single import path).
- Model picker price/speed hints match catalog metadata exactly.

## Non-Goals

- LLM/chat model endpoint management redesign (exists:
  `web/src/pages/ModelsPage.tsx`; out of scope here beyond the boundary
  note).
- Automatic model selection optimization / bandit routing (P3 benchmarking
  may inform it later).
- Pricing display as billing truth — `price` hints are informational until
  TokenRouter usage metering exists.
- Editing provider routes from the UI in P0/P1 (config/deploy-time only).

## Open Questions

1. Registry runtime shape: in-process module reading checked-in data vs a
   service endpoint — does anything need cross-process freshness before
   TokenRouter?
2. Who owns the constraints snapshot stored on a job: Media Job Service
   envelope (`provider_constraints` field) freezing registry output at
   submit time?
3. Input-asset constraint vocabulary: roles (`style_reference`,
   `character`, `first_frame`…) need a closed enum shared with the Asset
   Service ref roles.
4. How do image-edit capabilities (mask, inpaint) get modeled — per-route
   flags or a separate capability matrix?
5. Atlas catalog drift: what process keeps `ATLAS_FAMILIES` in sync with
   upstream Atlas route changes (manual today)?
6. Should the LLM catalog and media catalog share the picker UI pattern but
   stay separate data sources, or unify behind one catalog API with a
   `kind` discriminator?
