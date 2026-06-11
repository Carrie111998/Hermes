# Prompt Compiler

Status: spec-only — `ultra_prompt_compile` / `ultra_prompt_enhance` and a
provider-aware media prompt compiler do not exist; adjacent machinery exists
at both ends (agent system-prompt assembly, and per-provider payload
builders inside media plugins).
Date: 2026-06-11

Sources:

- Docs: `docs/ultra-studio-product-specs/04-skill-tool-prompt-contract.md`
  (§Prompt Compiler Boundary, §Model / Prompt Tools, §Clarification Rules),
  `docs/ultra-studio-agent-skill-tool-prompt-design.md` (§Prompt Design:
  Global Agent Prompt Addendum, Router Prompt, Workflow Skill Prompt
  Pattern, No-Fake Prompt Clause), `03-media-asset-contract.md`
  (§Media Job Envelope: `prompt`, `negative_prompt`,
  `provider_constraints`), `06-delivery-plan.md` (P1 item 4)
- Code (adjacent, verified this session): `agent/prompt_builder.py`
  (agent/system prompt assembly — skills manifest, environment hints,
  HERMES.md context scanning; not a media compiler),
  `agent/system_prompt.py`, `plugins/video_gen/atlas/client.py`
  (`build_payload` — provider payload construction from already-resolved
  args), `tools/video_generation_tool.py` (`_build_dynamic_video_schema`,
  `_format_model_caveats` — constraint surfacing),
  `trajectory_compressor.py` (unrelated to media prompts; listed to
  disambiguate)

## Purpose & Scope

The Prompt Compiler turns structured intent into provider-specific payloads:
"The agent should collect structured intent and asset roles. A prompt
compiler turns that into provider-specific payloads"
(`04-skill-tool-prompt-contract.md` §Prompt Compiler Boundary).

Pipeline position:

```text
user request -> route -> workflow plan -> asset role manifest
  -> provider constraints -> prompt compile -> job create
```

The compiler must know: target media type, model family, aspect ratio,
duration, reference assets, negative constraints, workflow skill, and
provider input limits (§Prompt Compiler Boundary).

Scope: the compile/enhance tool contracts, inputs/outputs, constraint
enforcement, repair-plan compilation for retries, and the boundary against
(a) the LLM system prompt builder (`agent/prompt_builder.py`, a different
component despite the name) and (b) provider clients, which receive compiled
payloads and only do transport-level normalization.

## Implementation Status

| Status | Item | Citation |
|---|---|---|
| Implemented (adjacent) | Agent/system prompt assembly: skills manifest, environment hints, context file scanning | `agent/prompt_builder.py`, `agent/system_prompt.py` — LLM-side, not media payloads |
| Implemented (adjacent) | Provider payload construction from resolved args (model route, image input normalization) | `plugins/video_gen/atlas/client.py` (`build_payload`, `normalize_image_input`) |
| Implemented (adjacent) | Constraint surfacing to the agent before submission (dynamic schema + caveats) | `tools/video_generation_tool.py` (`_build_dynamic_video_schema`, `_format_model_caveats`) |
| Specified, not built | `ultra_prompt_compile` tool | `04-skill-tool-prompt-contract.md` §Model / Prompt Tools; zero hits in code (rg, this session) |
| Specified, not built | `ultra_prompt_enhance` tool | same |
| Specified, not built | Asset role manifest as compiler input (typed refs with roles) | §Prompt Compiler Boundary; `hermes-asset-library-backend-design.md` §生成链路 |
| Specified, not built | Negative-constraint handling per model family | §Prompt Compiler Boundary |
| Specified, not built | Compiled repair plan for `ultra_media_job_retry` | `03-media-asset-contract.md` §Required Job Tools |
| Specified, not built | Workflow-skill prompt patterns feeding the compiler (per-skill compile recipes in `references/`) | `ultra-studio-agent-skill-tool-prompt-design.md` §Workflow Skill Prompt Pattern, §Skill Package Structure |
| Gap | Where prompt-injection scrubbing applies to user-supplied creative prompts before provider submission | not specified anywhere in the pack |

## User Entry Points

None directly — the compiler is agent infrastructure:

- Workflow skills call compile after collecting structured fields
  (`12-workflow-router.md` hands off `handoff{}` -> workflow -> compile).
- `prompt-repair` skill (P0 skill set) compiles repair plans from failed-job
  evidence.
- The inspector's "retry/repair plan" action triggers a recompile (planned,
  `03-inspector-live-panel.md`).
- Users see compiler effects only as job parameters on the asset/job card —
  internal prompt templates are not shown by default
  (`01-product-surface.md` §Non-Goals).

## Feature List

| Feature | Status |
|---|---|
| Compile structured intent + asset roles -> provider payload | Planned (core contract) |
| Pre-flight constraint validation against the model family | Planned (registry from `19-model-catalog-provider-constraints.md`; today partially enforced by dynamic tool schema) |
| Aspect ratio / duration / resolution normalization to nearest allowed value with explicit notice | Planned |
| Reference-asset role mapping into provider fields (style ref, first frame, character) | Planned |
| Negative prompt construction where supported | Planned |
| Prompt enhancement (`ultra_prompt_enhance`) as an explicit, separate step | Planned |
| Repair-plan compilation from typed provider errors | Planned |
| Per-skill compile recipes loaded from skill `references/` | Planned |
| Provider transport normalization (data URIs, route ids) | Implemented in clients (`build_payload`) — stays below the compiler |
| Prompt hash emission for lineage | Planned (`03-media-asset-contract.md` §Lineage requires prompt hash) |

## State Machine

Compilation is a pure function, not a stateful object. The contract is a
two-phase flow with explicit failure outcomes:

```text
collect (router/skill fills intent + asset role manifest)
  -> validate (constraints from catalog; asset refs already ACL-checked)
       -> compiled (payload + prompt_hash + constraints snapshot)
       -> rejected (typed: unsupported_model_capability | missing_field
                    | invalid_asset_ref)
compiled -> submitted (job create consumes payload verbatim)
failed job -> repair_compile (error class + evidence -> adjusted payload)
```

Rules:

- `rejected` must name the exact field and allowed values; the router's
  clarification rules decide whether to ask the user
  (`04-skill-tool-prompt-contract.md` §Clarification Rules).
- The job service consumes compiled payloads without re-writing prompts; any
  post-compile mutation is a contract violation.
- Repair compilation never silently changes the model family; switching
  models is a user/router decision.

## APIs & Events

Planned tool contracts (names from `04-skill-tool-prompt-contract.md`):

```text
ultra_prompt_compile(
  intent, workflow_skill, model_id,
  asset_roles: [{ref, role}],
  fields: {aspect_ratio?, duration?, resolution?, audio?, …},
  negative_constraints?
) -> {
  payload,                 # provider-ready
  prompt_hash,
  constraints_snapshot,    # frozen catalog constraints used
  notices[]                # normalizations applied
}

ultra_prompt_enhance(prompt, model_id, style_context?) ->
  { enhanced_prompt, rationale }
```

`ultra_prompt_compile` reads constraints via `ultra_media_constraints_get`
(`19-model-catalog-provider-constraints.md`) — it does not embed its own
copy of model limits. No gateway events; compile failures surface on the
tool channel as typed errors.

## Data Model

The compiler persists nothing. Its outputs land in:

- The MediaJob envelope: `prompt`, `negative_prompt`,
  `provider_constraints` (snapshot), `seed` passthrough
  (`03-media-asset-contract.md` §Media Job Envelope).
- Lineage: `prompt hash`, `seed/params`
  (`03-media-asset-contract.md` §Lineage).

Compile recipes (planned) live inside skill packages under `references/`
(per-model prompt patterns, rubric constraints), loaded on demand by the
skill resource loading path — not in a central template DB
(`ultra-studio-agent-skill-tool-prompt-design.md` §Skill Package Structure).

## UI Behavior

- Internal prompt templates are not exposed by default
  (`01-product-surface.md` §Non-Goals); the inspector shows the final
  compiled prompt, params, and constraint snapshot for a job — facts, not
  templates.
- Normalization notices ("duration adjusted 12s -> 10s for wan-2.6") must
  surface in the job card/tool progress so users understand divergence from
  their request.
- Enhancement is opt-in and visible: an enhanced prompt is shown as such,
  never silently substituted (consistent with the No-Fake Prompt Clause,
  `ultra-studio-agent-skill-tool-prompt-design.md` §Prompt Design).

## Permissions & Error Handling

- The compiler trusts only structured asset refs that already passed Asset
  Service validation; it never resolves plain-text mentions
  (`hermes-asset-library-backend-design.md` §前端交互契约).
- Typed rejections: `unsupported_model_capability` (field outside
  constraints), `missing_field` (blocking field for this workflow —
  feeds ask-one-question routing), `invalid_asset_ref` (ref/role mismatch,
  e.g. video asset in an image-only role).
- No silent clamping: every normalization emits a notice; out-of-range
  values that cannot be normalized reject.
- The compiler must not embed credentials or internal endpoints in
  payloads; provider routing/credentials are attached downstream
  (TokenRouter / provider clients).

## Acceptance Criteria

- For a fixed structured intent, compile output is deterministic (same
  payload + prompt_hash), enabling lineage dedup and retry comparison.
- A request violating a family constraint is rejected pre-submission with
  the exact field and allowed values (no provider round-trip).
- Compiled payloads for Atlas routes pass `build_payload` without
  modification beyond transport normalization.
- A failed job retried via repair plan produces a visibly different,
  explained payload (diffable in the inspector).
- Prompt hash recorded on every job matches a recompute from stored intent.
- No code path lets raw user text bypass compile for a workflow-routed
  media job ("Do not bypass Atlas constraints in prompts",
  `04-skill-tool-prompt-contract.md` §Non-Goals).

## Non-Goals

- Being the LLM system prompt builder (`agent/prompt_builder.py` is a
  separate, existing component; the name collision is historical).
- Creative ideation/brainstorming — enhancement refines a provided prompt;
  content invention belongs to workflow skills.
- Model selection (router/catalog decide; the compiler receives
  `model_id`).
- Provider transport details (data URI encoding, HTTP shapes — provider
  clients own these).
- A user-facing template editor.

## Open Questions

1. Runtime shape: a real tool the LLM calls, or a deterministic library
   invoked inside workflow skills' scripts? (The contract names tools; the
   skill design doc implies skill-local recipes.)
2. The existing `image_edit` intent path ("use the image tool only if the
   active tool supports editing", flagged in `12-workflow-router.md` Open
   Question 8) — does edit-capability checking live in compile or routing?
3. `ultra_prompt_enhance` model: enhanced by the agent LLM itself or a
   dedicated cheaper model? Cost accounting for enhancement calls?
4. Prompt-injection scrubbing for user creative prompts: required before
   provider submission, or is provider-side safety sufficient?
5. Localization: prompts arrive in Chinese/English mixed (real usage);
   do compile recipes translate, and is that visible?
6. Where does the deterministic compile function run when workflows execute
   inside a sandbox (`14-sandbox-lifecycle.md`) — host-side tool or
   sandbox-side library?
