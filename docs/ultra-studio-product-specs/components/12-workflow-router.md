# Workflow Router

Status: partial — prompt-level skill package and allowlist helpers exist; all
runtime wiring (typed route tool, router-first enforcement, events, evals) is
spec-only.

Sources:

- Code: `skills/creative/workflow-router/SKILL.md` (v1.0.0, 172 lines),
  `hermes_cli/ultra_studio_skills.py`,
  `tests/hermes_cli/test_ultra_studio_skills.py`, `tools/skills_tool.py`,
  `agent/skill_utils.py`, `agent/prompt_builder.py`, `toolsets.py`,
  `plugins/image_gen/atlas/`, `plugins/video_gen/atlas/`,
  `skills/creative/media-qa/SKILL.md`, `skills/creative/prompt-repair/SKILL.md`,
  `skills/creative/infographic-md-flow/SKILL.md`
- Docs: `docs/ultra-studio-product-specs/00-index.md`, `01-product-surface.md`,
  `02-agent-runtime-contract.md`, `03-media-asset-contract.md`,
  `04-skill-tool-prompt-contract.md`, `06-delivery-plan.md`,
  `docs/ultra-studio-agent-skill-tool-prompt-design.md`

## Purpose & Scope

The Workflow Router is the intent-classification front door for Ultra Studio
creative sessions. Every creative request must pass through it before any media
generation. The router classifies the user request and uploaded assets, selects
exactly one workflow skill (or none), detects missing blocking fields, and
either answers, asks one clarifying question, or hands off to a workflow skill
or generation tool.

Core rule (SKILL.md): route before generating. The router never generates media
itself.

In scope:

- Intent classification and execution-mode selection.
- Asset role classification for uploads attached to a request.
- Workflow skill selection within the Ultra Studio catalog.
- Ask-once clarification policy.
- Structured handoff to workflow skills and generation tools.
- The Ultra Studio skill allowlist that keeps the router's catalog focused.

Out of scope:

- Media generation, prompt compilation, and provider/model selection
  (workflow skills and `ultra_*` tools own these).
- TokenRouter credential/model routing. TokenRouter is a separate spec-only
  backend component (02 §Sandbox Lifecycle; 03 `tokenrouter_decision_id`).
  This document does not specify it.
- Model-specific prompt lore. Design doc: router owns classification; workflow
  skills own execution.

## Implementation Status

| Layer | Status | Evidence |
|---|---|---|
| Router skill package (routing rules, output YAML, ask-once policy, asset classification, Atlas tool discipline, handoff format, failure guards) | Implemented (prompt-level only) | `skills/creative/workflow-router/SKILL.md` v1.0.0 |
| 8 intent classes: chat, image_generate, image_edit, video_generate, video_from_image, qa, repair, planning | Implemented (prompt contract) | SKILL.md |
| Allowlist computation helpers + default 4-skill catalog (workflow-router, infographic-md-flow, media-qa, prompt-repair) | Implemented but unwired — no production caller | `hermes_cli/ultra_studio_skills.py`; rg shows callers only in tests and research docs |
| Allowlist unit/integration tests (9 tests, incl. discovery filtering) | Implemented | `tests/hermes_cli/test_ultra_studio_skills.py` |
| Skill discovery runtime the router rides on (`skills_list`/`skill_view`, disabled filtering, skills index in system prompt) | Implemented (generic, not router-specific) | `tools/skills_tool.py`, `agent/skill_utils.py`, `agent/prompt_builder.py`, `toolsets.py` |
| Low-level generation tools the router selects between (`image_generate`, `video_generate`) | Implemented | `toolsets.py`; `plugins/image_gen/atlas/`, `plugins/video_gen/atlas/` |
| Handoff-target skill packages (media-qa, prompt-repair, infographic-md-flow) | Implemented (prompt packages) | `skills/creative/*/SKILL.md` |
| Allowlist bootstrap at startup/CLI | Specified, not built | 06-delivery-plan P0 item 1 |
| Router-first runtime connection (guaranteed load for creative sessions) | Specified, not built | 06-delivery-plan P0 item 4 |
| `ultra_skill_route` typed tool emitting machine-readable route JSON | Specified, not built | design doc §Layer 2; 04 §Acceptance |
| Downstream `ultra_media_job_*` / `ultra_asset_*` / catalog / constraints / prompt tools | Specified, not built | 04 §Tool Groups; 03 §Required Job Tools; rg confirms no code |
| Routing-phase UI (Thinking state, question cards, `routing` status label) | Specified, not built | 01 §Required States; 02 §Event Stream; web/src has zero references |
| Skill Trigger Eval / Skill Output Contract Eval; named router tests | Specified, not built | 04 §Required Skill Runtime Objects; design doc §Validation Matrix; tests absent |
| `references/router-schema.md` cold-layer schema file | Specified, not built | design doc §Skill Package Structure; skill dir contains only SKILL.md |
| Single canonical router output schema | Open gap — resolved by this spec (see Data Model) | Three divergent schemas: SKILL.md vs 04 vs design doc |
| Router output persistence/observability | Open gap | No spec or code answers where the route object lands |
| Router error/fallback semantics (ambiguous intent, disabled workflow) | Open gap | No typed routing errors anywhere |
| Re-routing policy mid-session | Open gap | Undefined |
| Bypass prevention (agent calling generation tools without routing) | Open gap | "Route before generating" is prompt-level only |

Nothing in the planned or gap rows is shipped. Today the router works only as a
prompt package discovered through generic Hermes skill discovery.

## User Entry Points

| Entry | Behavior | Status |
|---|---|---|
| Chat message in an Ultra Studio creative session (web UI per 01) — any media request ("make a cat video", "generate a product photo") | Hits the router before any generation | Planned wiring; today only generic Hermes chat exists |
| Vague creative request ("我要做一个视频") | Router classifies, then asks exactly one blocking question via an ask-user-question card | SKILL.md policy implemented as prompt text; UI card planned |
| File upload alongside a prompt | Router classifies the asset role (foundation / logo / style_reference / source_video / ignore) before tool use | SKILL.md prompt rules implemented; typed `media_input` upload planned (06 P0 item 3) |
| Follow-up on an existing output ("why did this fail?", "fix it") | Router hands off to media-qa / prompt-repair intents | Skill packages implemented; runtime handoff unenforced |
| Indirect entry via Hermes skill discovery: agent sees workflow-router in the system-prompt skills index and loads it with `skill_view` | Only activation path that exists today | Implemented (generic mechanism) |

## Feature List

| # | Feature | Status |
|---|---|---|
| 1 | workflow-router SKILL.md prompt package: classification rules, routing output YAML template, ask-once policy, asset role classification, Atlas tool discipline, handoff format, failure guards | Implemented |
| 2 | 8 intent classes: chat, image_generate, image_edit, video_generate, video_from_image, qa, repair, planning | Implemented |
| 3 | Ultra Studio allowlist helpers + default 4-skill allowlist with tests (`hermes_cli/ultra_studio_skills.py`) | Implemented (unwired) |
| 4 | Generic skill discovery runtime: `skills_list`/`skill_view` progressive disclosure, `skills.disabled` / `skills.platform_disabled` filtering, external skill dirs, skills index in system prompt | Implemented |
| 5 | Downstream handoff-target skill packages: media-qa, prompt-repair, infographic-md-flow with `related_skills` cross-links | Implemented |
| 6 | Low-level generation tools the router selects between: `image_generate`, `video_generate` (Atlas plugins) | Implemented |
| 7 | Allowlist bootstrap wired into startup/CLI so the Ultra profile hides the ~80 unrelated skills | Planned (06 P0 item 1) |
| 8 | Router-first runtime connection — guaranteed router load for creative sessions instead of best-effort metadata matching | Planned (06 P0 item 4) |
| 9 | `ultra_skill_route` typed tool emitting machine-readable route JSON consumable by tools and UI | Planned (design doc §Layer 2) |
| 10 | Downstream tool surface the router hands off to: `ultra_media_job_create/status/cancel/retry/finalize`, `ultra_asset_upload/list/inspect/download/promote`, `ultra_model_catalog`, `ultra_media_constraints_get`, `ultra_prompt_compile`, `ultra_prompt_enhance` | Planned (04 §Tool Groups; 03) |
| 11 | Router confidence score + reason + recommended_tools fields | Planned (design doc schema only) |
| 12 | Routing targets product-photoshoot, product-md-flow, ugc-flow, app-sizzle, cinematic-trailer/cinematic-flow, typography-md-flow, amazon-product-listing, character-consistency | Planned (04 P0/P1; skills verified absent) |
| 13 | Skill Trigger Eval + Skill Output Contract Eval harness for routing accuracy | Planned (04 §Required Skill Runtime Objects) |
| 14 | Routing-phase UI: Thinking state streaming route/plan text, structured question cards, `routing` status label | Planned (01; 02; design doc) |
| 15 | `references/router-schema.md` cold-layer schema file inside the skill package | Planned (design doc §Skill Package Structure) |
| 16 | Single canonical router output schema across SKILL.md / 04 / design doc | Gap — resolved by Data Model below; sources must be updated to match |
| 17 | Router output persistence/observability (log, session attachment, gateway event) | Gap |
| 18 | Router error/fallback semantics (ambiguous classification, unknown intent, disabled/missing workflow skill) | Gap |
| 19 | Re-routing policy mid-session (user pivot, post-repair retry exhaustion) | Gap |
| 20 | Enforcement that the router cannot be bypassed (nothing prevents direct `image_generate`/`video_generate` calls) | Gap |

## State Machine

No formal router state machine exists in code or in any upstream spec; the
router today is stateless prompt text. This section defines the normative
lifecycle this component must follow once the runtime connection (06 P0 item 4)
ships. The phase names below are proposed by this spec; only the conceptual
flow is sourced (SKILL.md + design doc).

States:

| State | Meaning | Triggered by |
|---|---|---|
| `received` | Creative request (and any uploads) entered the session | User `prompt.submit` |
| `classifying` | Router classifies intent and asset roles | Agent (router-first load) |
| `asked` | Exactly one blocking question rendered to the user | Router, when a blocking field is missing (`execution_mode: ask_once`) |
| `answered` | User replied to the question | User |
| `routed` | Route object finalized: intent, execution mode, workflow skill or none | Router |
| `handed_off` | Workflow skill or generation tool received the handoff brief | Agent, per route object |
| `answer_only` (terminal) | Chat/planning intents answered with no media work | Router |

Transitions:

```text
received -> classifying            (agent loads router first)
classifying -> routed              (no blocking field missing)
classifying -> asked               (one blocking field missing; ask_once)
asked -> answered -> classifying   (re-classify with the answer)
routed -> handed_off               (generate_now / inspect_then_generate /
                                    repair_then_retry)
routed -> answer_only              (chat / planning intents)
```

Execution modes act as router terminal outcomes (SKILL.md, implemented as
prompt contract): `answer_only`, `ask_once`, `generate_now`,
`inspect_then_generate`, `repair_then_retry`.

Unspecified transitions (open gaps, see Open Questions):

- Timeout or user ignoring the question in `asked`.
- User's answer to the one question is itself ambiguous (ask-once allows no
  second question).
- Two fields blocking at once.
- Re-entry after a mid-session pivot or after N failed prompt-repair retries.

Downstream machines the router feeds, for orientation only (both planned, not
owned by the router):

- Media job: `queued -> preparing -> submitting -> generating -> polling ->
  downloading -> completed`, with `failed` / `cancelled` / `timeout` branches
  (design doc §Tool Contract Details).
- Session surface: `Empty -> Thinking -> Waiting for user -> Creating ->
  Complete | Failed` (01 §Required States).

## APIs & Events

### Tools

| Tool | Role for the router | Status |
|---|---|---|
| `skills_list` / `skill_view` / `skill_manage` | Current router load mechanism via progressive disclosure | Implemented (`tools/skills_tool.py`; registered in `toolsets.py`) |
| `image_generate` / `video_generate` | The router's `primary_tool` targets today | Implemented (`toolsets.py`; Atlas plugins) |
| `ultra_skill_route` | Typed tool that returns the structured route object | Proposed (design doc §Layer 2); no implementation |
| `ultra_media_job_create/status/cancel/retry/finalize` | Handoff target for generation routes per 04 | Proposed; no implementation |
| `ultra_asset_upload/list/inspect/download/promote` | Asset surface behind asset role classification | Proposed; no implementation |
| `ultra_model_catalog`, `ultra_media_constraints_get` | Capability queries (e.g. image-edit support) the router needs | Proposed; no implementation |
| `ultra_prompt_compile`, `ultra_prompt_enhance` | Post-route pipeline stages | Proposed; no implementation |

Pipeline position (04 §Prompt Compiler Boundary): user request -> **route** ->
workflow plan -> asset role manifest -> provider constraints -> prompt compile
-> job create.

### Events

All routing-phase events are proposed; none exist in code.

| Event | Carries | Status |
|---|---|---|
| `status.update` with `routing` / `planning` labels | Phase signal for the inspector | Proposed (02 §Event Stream; design doc UI labels) |
| `thinking.delta` | Streamed route/plan text in the Thinking state | Proposed (02) |
| `tool.start/progress/complete/error` | Tool-call telemetry around handoff | Proposed (02) |
| `approval.requested/resolved` | Cost-gated route confirmation | Proposed (02 §Human Approval Gateway); owner undefined (gap) |
| `media_job.created/updated`, `asset.ready` | Downstream of handoff | Proposed (02) |
| Route-specific event (e.g. `route.complete`) | Machine-readable route decision for the UI | Gap — not defined in 02's event list; required to satisfy 04's "machine-readable for tools and UI" acceptance |

### Session methods

`session.create`, `session.resume` (restores active skill profile),
`prompt.submit`, `slash.exec` (02 §Session Lifecycle) — all proposed. Whether
`session.resume` restores the last route decision is undefined (gap).

## Data Model

### Canonical route object (normative decision of this spec)

Three divergent schemas exist. This spec declares the canonical schema:
**SKILL.md v1.0.0 as the base**, extended with two additive fields from the
design doc (`confidence`, `reason`). Rationale: SKILL.md is the only shipped
surface, and its enums match the tools that actually exist; the design doc's
`asset_roles` array shape requires `asset_id` values that cannot exist before
`ultra_asset_*` ships.

```yaml
RouteObject:
  intent: chat | image_generate | image_edit | video_generate |
          video_from_image | qa | repair | planning
  execution_mode: answer_only | ask_once | generate_now |
                  inspect_then_generate | repair_then_retry
  workflow_skill: null | infographic-md-flow | media-qa | prompt-repair
  primary_tool: null | image_generate | video_generate
  aspect_ratio: null | 16:9 | 9:16 | 1:1 | 4:3 | 3:4
  asset_roles:            # role-keyed map of asset references
    foundation: []
    image_reference: []
    logo: []
    style_reference: []
    source_video: []
    ignore: []
  missing: []             # string[], blocking fields only
  confidence: 0.0         # float, additive (design doc); semantics: see gap
  reason: ""              # string, additive (design doc)
  handoff:
    brief: ""
    constraints: []
    allowed_text: []
```

Superseded variants (must be reconciled to the canonical schema above):

| Source | Divergence | Disposition |
|---|---|---|
| 04 §Router Output | `intent` adds `edit/asset_search/unknown`, drops edit/from-image split; `execution_mode: answer\|ask\|tool\|workflow`; `primary_tool: ultra_media_job_create` | Superseded; `primary_tool` migrates to `ultra_media_job_create` only when that tool ships |
| Design doc §Progressive Disclosure | `intent: chat\|image\|video\|edit\|analysis\|unsupported`; `workflow` enum incl. product-photoshoot/ugc-flow/app-sizzle/cinematic-trailer; `execution_mode: answer\|ask\|generate_image\|generate_video\|multi_stage`; `asset_roles` as `[{asset_id, role}]`; adds `recommended_tools` | Superseded except `confidence`/`reason` (adopted). `workflow_skill` enum expands only when target skills ship and enter the allowlist. `asset_roles` migrates to the asset-id array form when `ultra_asset_*` ships. `multi_stage` remains an open question |

Open schema items: `confidence` threshold semantics (what flips
`generate_now` to `ask_once`) and multi-stage representation are undefined —
see Open Questions.

### Other entities

| Entity | Fields | Persistence | Status |
|---|---|---|---|
| Skill frontmatter | `name`, `description`, `version`, `author`, `license`, `platforms[]`, `metadata.hermes.tags[]`, `metadata.hermes.related_skills[]` | SKILL.md files; parsed by `tools/skills_tool.py` `_parse_frontmatter` | Implemented |
| Allowlist config fragment | `{skills: {disabled: string[]}}` or `{skills: {platform_disabled: {<platform>: string[]}}}` | Config consumed by `_is_skill_disabled`; producer helpers are side-effect free, no persistence path defined | Implemented helpers, unwired (gap: which file, when, per-platform vs global) |
| Media job record the route feeds | `job_id`, `session_id`, `tool_call_id`, `provider`, `model`, `media_type`, `mode`, `status`, `input_assets`, `prompt`, `negative_prompt`, `provider_constraints`, `seed`, `tokenrouter_decision_id`, `output_assets`, `error` | Planned (03) | Planned |
| High-level tool result envelope | success form `{success, job_id, state, provider, model, modality, artifacts[], workflow: {skill, stage}}`; error form `{success: false, error_type, message, recoverable, next_action}` | Planned (design doc) | Planned |
| Route object persistence | Undefined — logged? attached to session? emitted as event? | None | Gap |

## UI Behavior

All router-related UI is planned. `web/src` contains zero Ultra Studio /
workflow-router / routing references (verified by rg).

- Thinking state: center streams route/plan text; inspector shows the current
  reasoning phase (01 §Required States).
- Waiting-for-user state: center renders a structured ask-user-question card —
  at most one blocking question per the ask-once policy; inspector shows
  missing-field context (01).
- The router must not dump its internal YAML routing object to the user unless
  the user asks for the routing trace (SKILL.md §Routing Output).
- Final answers must not expose raw job IDs, internal paths, or template
  markers (design doc §Notion Alignment).
- The session must not auto-generate media on open; user intent drives
  execution (01 §Center) — mirrors the SKILL.md core rule.
- Failed routing/generation surfaces as typed error cards with actionable
  recovery, not generic apologies (01 Failed state; 02 §Error Contract).
- Phase labels spanning routing + generation (design doc): thinking, routing,
  planning, preparing_assets, creating_image, storyboarding, creating_video,
  polling, reviewing, complete, failed.

## Permissions & Error Handling

Implemented as prompt rules (SKILL.md):

- Atlas tool discipline: credentials and provider settings are server-side.
  The router never asks the user to paste provider keys, and never asks which
  low-level provider/model to use.
- Failure guards: no hardcoded demo responses; no fake job IDs, asset URLs, or
  completion states; no hidden provider-workflow switches; no invented
  metrics, brands, logos, or capabilities; no media generation for greetings,
  questions, or planning requests.
- When generation fails due to missing config or backend capability, report
  the real missing capability. Route to prompt-repair only if a retry can
  change the outcome (prompt-repair SKILL.md repeats: do not rewrite prompts
  for credential blockers).

Planned:

- Approval gateway (02 §Human Approval Gateway): money-spending,
  private-media, logged-in-account, local-command, and publish actions require
  approve/edit/reject/respond decisions that survive refresh. Whether the
  router or the workflow skill raises `approval.requested` for cost-gated
  routes is undefined (gap).
- Typed error vocabulary around handoffs (02 §Error Contract):
  `missing_credential`, `unsupported_model_capability`, `invalid_asset_ref`,
  `provider_rejected_input`, `quota_exceeded`, `job_timeout`,
  `asset_upload_failed`, `sandbox_unavailable`, `approval_required`. Plus
  tool-level `error_type` enum
  `auth_required|unsupported_parameter|provider_error|timeout|empty_response|invalid_asset`
  with `next_action`
  `configure_atlas|remove_reference_images|retry_status|ask_user` (design doc).

Gap: no routing-specific error types exist anywhere. Candidate types named in
the gap analysis — `ambiguous_intent`, `workflow_unavailable`,
`asset_role_conflict` — must be defined before the runtime connection ships.

## Acceptance Criteria

P0 (from 04 §Acceptance and 06-delivery-plan):

1. With the Ultra profile active, `skills_list` returns an Ultra-focused
   catalog; the ~80 unrelated skills are hidden (allowlist bootstrap wired,
   verifiable via the existing `test_ultra_allowlist_hides_unrelated_skills_from_discovery`
   pattern against real startup, not a monkeypatch).
2. A generic video request does not trigger ASCII/Comfy/Manim or any
   non-catalog skill.
3. A clear image request calls the image path without irrelevant questions.
4. A clear video request creates a real media job or returns a typed blocker
   (06 P0: "a real job or a typed blocker").
5. Router output is machine-readable for tools and UI — a structured route
   object exists outside prose (via `ultra_skill_route` or an equivalent
   event), matching the canonical schema in this spec.
6. The router never generates media itself; the route step emits no
   `image_generate`/`video_generate` call.

P1:

7. Vague requests ask exactly one useful question (06 P1; 00-index line 64:
   "route the request to the right skill or ask one useful missing field").
   "make a video" / "我要做一个视频" routes to a video intent and asks one
   question that changes output type, aspect ratio, asset role, workflow,
   cost/concurrency, or model capability — never a generic question before
   routing (04 §Clarification Rules).

Test gates (design doc §Validation Matrix — currently unwritten):

8. `test_workflow_router_routes_image_request_without_video_skill` passes.
9. `test_workflow_router_asks_for_vague_video_request` passes.
10. Skill Trigger Eval and Skill Output Contract Eval exist and run as router
    quality gates (04 §Required Skill Runtime Objects); dataset and pass bar
    are an open question.

Manual acceptance scenarios 1-5 (design doc): cat image, vague video,
image-to-5s-video, KPI video, focused skill list.

## Non-Goals

- The router does not generate media (04 §Router Output; SKILL.md core rule).
- The router does not contain model-specific prompt lore — it owns
  classification; workflow skills own execution (design doc §Main Risks).
- The router does not select low-level providers/models or handle credentials;
  TokenRouter/CometAPI credential routing stays in the provider/backend layer
  (design doc §Notion Alignment).
- Do not expose unrelated general-purpose skills by default (04 §Non-Goals).
- Do not delete upstream skills before disable/allowlist verification (04).
- Do not let plugins replace workflow skills (04).
- Do not bypass Atlas constraints in prompts (04).
- Clarification does not happen before route/skill selection — ask after
  routing, never as a generic pre-routing question (design doc §Notion
  Alignment; 04 §Clarification Rules).

## Open Questions

1. Transport: is the route object emitted as a tool result
   (`ultra_skill_route`), a gateway event, or kept as in-context working notes
   only? (This spec fixes the schema; transport remains open.)
2. Enforcement: how is router-first execution guaranteed for creative
   sessions — system-prompt mandate, forced pre-turn skill load, or a typed
   routing tool the model must call before `image_generate`/`video_generate`?
3. Allowlist bootstrap: CLI command vs startup hook vs config file; Ultra
   profile per-platform (`skills.platform_disabled`) or global
   (`skills.disabled`)?
4. Should `workflow_skill` stay limited to the 4 shipped skills, or be specced
   now for product-photoshoot/ugc-flow/app-sizzle/cinematic-trailer ahead of
   their delivery (06 P1)?
5. Is confidence scoring required, and what threshold flips execution mode
   from generate to ask?
6. Does the router re-run on mid-session pivots and after N failed
   prompt-repair retries, or does the workflow skill own all post-handoff
   decisions?
7. Which intents are cost-gated behind `approval.requested`, and does the
   router or the workflow skill raise the approval?
8. How does the router query backend capability (image-edit support,
   image-to-video support, max_reference_images) before
   `ultra_model_catalog` / `ultra_media_constraints_get` exist? SKILL.md's
   image_edit intent says "use the image tool only if the active tool supports
   editing" with no defined query path.
9. What is the eval dataset and pass bar for the Skill Trigger Eval and Skill
   Output Contract Eval, and do the two named router tests block CI?
10. Is the routing decision persisted to the session (02 includes "active
    skill profile" in session state but not "last route"), and does
    `session.resume` restore it?
11. Ask-once edge cases: behavior when the user's answer is itself ambiguous,
    or when two fields are blocking at once.
12. Multi-intent/batch requests ("make a product photo and then a UGC ad from
    it"): the design doc's `multi_stage` execution mode is the only hint; no
    representation is defined.
13. Catalog cross-link hygiene: infographic-md-flow's `related_skills` still
    references legacy skills (baoyu-infographic, manim-video, comfyui) that
    the Ultra profile disables; no rule governs catalog cross-links.
