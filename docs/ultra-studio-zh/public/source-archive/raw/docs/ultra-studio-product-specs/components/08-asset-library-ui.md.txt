# Asset Library UI

Status: spec-only — no asset library page, gallery, mention menu, or picker
exists in `web/src`; the design is fully specified in the asset library
backend design doc, and a static layout demo exists outside the repo.
Date: 2026-06-11

Sources:

- Docs: `docs/hermes-asset-library-backend-design.md` (§前端交互契约,
  §`@` mention 查询, §Picker 与 ask_user_question 复用, §实时事件,
  §前端必须配合的点), `docs/ultra-studio-product-specs/03-media-asset-contract.md`
  (§Asset Card UI, §Asset Types, §QA, §Acceptance),
  `01-product-surface.md` (§Right: Inspector / Live Panel),
  `06-delivery-plan.md` (P0 item 9, P1 items 6-7),
  `docs/hermes-soulid-element-asset-model.md` (§UI 要求)
- Demo (layout reference only, outside the repo):
  `/Users/lifcc/Desktop/code/work/infra/her/asset-library-demo.html`
- Code: none — `web/src/pages/` contains no asset/library/gallery page
  (listing verified this session). The chat-side `ChatInspector.tsx` is
  specified separately in `03-inspector-live-panel.md`.

## Purpose & Scope

The Asset Library UI is the browsing, search, and reuse surface for the
project's typed assets: uploads (`media_input`), generated outputs
(`image_job` / `video_job` / `audio_job`), reusable `element` /
`character` / `soul_id` references, plus Collections and Smart Groups. It is
"项目级资产服务，而不是普通图库" (`hermes-asset-library-backend-design.md`
§目标) — every card action feeds structured asset refs back into generation.

Scope: gallery and filtering, asset detail view, Collections and Smart
Groups UI, the `@` mention menu and picker in the chat composer, and
realtime updates. Backend semantics (entities, ACL, lineage, indexing) are
owned by `09-asset-service.md`; the per-selection context panel is
`03-inspector-live-panel.md`.

## Implementation Status

| Status | Item | Citation |
|---|---|---|
| Specified, not built | Asset gallery with hard filters (type, media_type, status, endpoint, source, collection) | `hermes-asset-library-backend-design.md` §资产列表和详情 (list API params) |
| Specified, not built | Asset detail view: prompt/params/seed/model context, lineage, ACL, audit | `hermes-asset-library-backend-design.md` §资产列表和详情 (detail payload) |
| Specified, not built | Asset card actions: download, inspect, reuse, convert to element, create character | `03-media-asset-contract.md` §Asset Card UI |
| Specified, not built | Collections UI (manual membership) | `hermes-asset-library-backend-design.md` §Collection |
| Specified, not built | Smart Group builder with preview-before-save | `hermes-asset-library-backend-design.md` §Smart Group |
| Specified, not built | Composer `@` mention menu with typed, grouped results | `hermes-asset-library-backend-design.md` §`@` mention 查询 |
| Specified, not built | Shared picker for `ask_user_question(entity)` contexts | `hermes-asset-library-backend-design.md` §Picker 与 ask_user_question 复用 |
| Specified, not built | Structured message submit (mention payload + attachments), not plain text | `hermes-asset-library-backend-design.md` §前端交互契约 |
| Specified, not built | Realtime event subscription (`asset.ready`, `job.status`, …) | `hermes-asset-library-backend-design.md` §实时事件 |
| Specified, not built | Element/Character creation entry points from result cards | `06-delivery-plan.md` P1 item 7; `hermes-soulid-element-asset-model.md` §UI 要求 |
| Layout reference | Static gallery demo (no data wiring) | `asset-library-demo.html` (outside repo; not a code claim) |

## User Entry Points

- Asset library page (location in nav vs My office is an open question;
  `01-product-surface.md` IA does not list a dedicated Assets entry — see
  §Non-Goals "do not merge Marketplace, Memory, and Files into one generic
  Assets page", which implies assets are their own surface).
- `@` mention in the chat composer (`@asset`, `@character`, `@element`,
  `@collection`, `@group` prefixes).
- Picker opened by `ask_user_question(entity)` during a workflow.
- Inspector "reuse" / "convert to element" / "create character" actions on a
  selected asset (`03-inspector-live-panel.md`).
- Upload completion: a new upload appears in both the library and the chat
  side panel (`hermes-asset-library-backend-design.md` §前端必须配合的点).

## Feature List

| Feature | Status |
|---|---|
| Gallery grid with thumbnails per media type | Planned |
| Hard filters: project, type, media_type, status, endpoint, source, collection, time | Planned |
| Keyword + semantic search (degrades to FTS when index pending) | Planned (`hermes-asset-library-backend-design.md` §搜索和索引 degradation rule) |
| Asset detail view with generation context (endpoint, model_route, prompt, seed, params, request/run/session ids) | Planned |
| Lineage panel ("where did this come from") | Planned |
| Audit trail view | Planned |
| Download via signed/real URLs | Planned |
| Reuse asset into composer as structured ref | Planned |
| Save as Element / build Character from eligible assets | Planned |
| Collections: create, rename, add/remove members | Planned |
| Smart Groups: build query, preview hit count, save; live evaluation on open | Planned |
| `@` mention menu grouped by type with status subtitles and thumbnails | Planned |
| Picker contexts: `chat_prompt`, `asset_picker`, `character_picker`, `smart_group_builder`, `collection_member_add` | Planned |
| Revoked assets hidden from mention; `not_ready` shown but not usable | Planned |
| Realtime status updates on cards (uploading -> processing -> ready) | Planned |

## State Machine

The UI renders the asset lifecycle owned by the Asset Service:

```text
uploading -> processing -> ready -> archived
failed / revoked / deleted (terminal or gated)
```

UI rendering rules per state:

| State | Card behavior |
|---|---|
| `uploading` / `processing` | Placeholder thumbnail + progress; not selectable for `use` |
| `ready` | Full actions (reuse, download, element/character where eligible) |
| `failed` | Visible with error chip; inspectable, not usable (`03-media-asset-contract.md` §Acceptance: "Failed jobs remain inspectable") |
| `revoked` | Excluded from mention/picker results; visible in library only with revoked badge |
| `archived` | Hidden from default view; reachable via filter |

Reference (`element`/`character`/`soul_id`) statuses render
`queued -> training -> ready` / `failed` / `revoked` with no fake `ready`
(`hermes-asset-library-backend-design.md` §P0 切片 item 4).

## APIs & Events

The UI consumes the Asset Service API (defined in `09-asset-service.md`,
verbatim from the design doc):

- `GET /api/assets?…` list with hard filters; `GET /api/assets/{id}`,
  `/lineage`, `/audit`.
- `GET /api/assets/mentions?q=&project_id=&types=&context=` — single shared
  endpoint for mention menu and all picker contexts.
- Collections and Smart Groups CRUD + `smart-groups/preview`.
- `POST /api/assets/references` for element/character/soul_id creation.

Submitted chat messages carry structure, not just text:

```json
{
  "text": "用 @Luna 生成一个天台夜景视频",
  "mentions": [{ "span": [2,7], "entity_type": "character",
                 "entity_id": "char_luna", "asset_ref_type": "soul_id",
                 "operation": "use" }],
  "attachments": [{ "asset_id": "media_input_123", "role": "image_reference" }]
}
```

Subscribed events: `asset.upload.started`, `asset.processing`,
`asset.ready`, `asset.failed`, `asset.revoked`, `asset.indexed`,
`collection.updated`, `smart_group.updated`, `reference.status`,
`job.status` (`hermes-asset-library-backend-design.md` §实时事件).

## Data Model

The UI owns no durable state. Client-side state it must maintain:

- Composer mention tokens as structured objects (id, type, span, ref type) —
  "Composer 内部维护 mention token，不只存文本"
  (`hermes-asset-library-backend-design.md` §前端必须配合的点).
- Gallery filter state and cursor pagination.
- Event-driven cache of asset cards (id -> status/thumbnail), invalidated by
  the events above; no optimistic `ready` states.
- Smart Group builder draft (`query_json` mirror) until saved.

All authoritative data comes from the Asset Service; the UI must not derive
permissions client-side.

## UI Behavior

- Mention menu groups results: Assets, Characters, Elements, Collections,
  Smart Groups; each row shows label, type, status subtitle
  ("face identity · ready · 4 source images"), and thumbnail.
- `@collection` / `@group` selection opens a preview (member list / hit
  count) and requires explicit confirm before expanding — never silently
  inserts many assets.
- Same-name entities require explicit disambiguation in the menu.
- `not_ready` items render selectable-disabled with reason; submitting one
  anyway returns `asset_not_ready` from the backend and the composer shows
  it inline.
- Asset detail "reuse" inserts a structured ref into the composer; element /
  character buttons call the Asset Service, not local state.
- Cards never expose internal filesystem paths
  (`03-media-asset-contract.md` §Asset Card UI).
- Upload progress is visible; a finished upload appears in library and chat
  side panel simultaneously.
- Empty library renders blank with an upload affordance; no sample assets.

## Permissions & Error Handling

The list API returns only `read`-permitted assets; `use` is enforced again
at submit time. UI surfaces the typed errors from the design doc:

| Error | UI behavior |
|---|---|
| `asset_access_denied` | Card action blocked with permission notice. |
| `asset_not_ready` | Inline composer error on the mention chip. |
| `asset_revoked` | Mention chip turns invalid; message blocked until removed. |
| `asset_not_found` | Stale card removed; toast with refresh hint. |
| `upload_mime_not_allowed` | Upload rejected pre-flight with allowed types. |
| `smart_group_query_invalid` | Builder shows field-level validation. |
| `collection_expand_requires_confirmation` | Preview/confirm dialog (the normal path, not an error toast). |

Fail-closed rule: generation submit with any invalid ref is blocked
entirely; no warning-and-continue (`hermes-asset-library-backend-design.md`
§错误策略).

## Acceptance Criteria

- The mention flow works end to end: typing `@lun` lists Luna with status
  subtitle; selecting inserts a structured ref; submitting passes
  `mentions[]` (verifiable in the request payload).
- `@collection` cannot expand without a confirm step.
- A revoked asset disappears from mention results within one event cycle.
- Asset detail shows real model/job/prompt/input details for a generated
  asset (`03-media-asset-contract.md` §Acceptance).
- Download delivers the real binary via storage URL or local
  materialization.
- A Smart Group preview hit count matches the saved group's first
  evaluation.
- Failed and `not_ready` assets render truthfully; no card ever fakes
  `ready`.

## Non-Goals

- Owning asset state, ACL evaluation, or lineage computation (Asset Service
  owns these).
- A public/shared gallery or publishing surface.
- Image editing tools inside the library (generation workflows own edits).
- Merging Marketplace/Memory/Files into this surface
  (`01-product-surface.md` §Non-Goals).
- Plain-text `@Luna` parsing as a permission mechanism — the backend rejects
  ambiguous plain-text resolves by design.

## Open Questions

1. Nav placement: dedicated Assets entry vs a tab inside My office — the IA
   tree in `01-product-surface.md` lists neither explicitly.
2. Virtualized grid requirements: expected library sizes and thumbnail
   loading strategy are unspecified.
3. Multi-select and bulk actions (add N assets to a collection) — not in
   the design doc; P1 need?
4. Does the mention endpoint dedupe across types when one entity matches as
   both asset and element source?
5. Video card preview behavior (hover-play vs static first frame) — QA
   contract guarantees a first frame (`03-media-asset-contract.md` §QA),
   nothing more.
6. Where Smart Group similarity threshold UI lives (slider in builder?) and
   its default (design doc example uses 0.78).
