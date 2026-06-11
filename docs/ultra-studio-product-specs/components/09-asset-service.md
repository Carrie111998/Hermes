# Asset Service

Status: spec-only — the service, its entities, and its APIs are fully
designed but no asset service code, tables, or endpoints exist in the repo;
uploads today land as chat attachments without asset records.
Date: 2026-06-11

Sources:

- Docs: `docs/hermes-asset-library-backend-design.md` (entire doc: §架构形状,
  §核心实体, §API 面, §搜索和索引, §生成链路, §实时事件, §错误策略, §P0 切片),
  `docs/ultra-studio-product-specs/03-media-asset-contract.md` (§Asset Types,
  §Asset Lifecycle, §Lineage, §QA, §Acceptance),
  `docs/hermes-soulid-element-asset-model.md` (§资产类型, §最小数据模型,
  §工具映射, §安全要求), `02-agent-runtime-contract.md` (§Error Contract),
  `06-delivery-plan.md` (P0 item 9, P1 items 6-7)
- Code: none — `rg -li "asset"` over `tools/`, `plugins/`, `gateway/`
  returns only unrelated hits (UI themes, checkpoint manager); verified this
  session. Upload intake exists only as a chat attachment path
  (`web/src/components/chat/ChatComposer.tsx`).

## Purpose & Scope

The Asset Service is the system of record for typed product assets: uploads,
generated outputs, reusable Elements/Characters/Soul IDs, Collections, Smart
Groups, ACLs, lineage, and audit. Its design rule: generation consumes
structured asset refs — "生成调用只消费结构化 asset refs，不能靠 prompt 里
裸写 asset id" (`hermes-asset-library-backend-design.md` §目标).

State ownership (`§架构形状`): Asset Service owns strongly consistent asset
state; Object Storage holds binaries/thumbnails only; the Search Indexer owns
a rebuildable projection; the Media Job Service must register outputs back
here; TokenRouter checks `asset_ref` permissions before generation.

Scope: entities, API surface, ref validation, indexing pipeline, events,
ACL/audit. The browsing UI is `08-asset-library-ui.md`; job execution is
`10-media-job-service.md`; credential policy is `17-tokenrouter.md`.

## Implementation Status

| Status | Item | Citation |
|---|---|---|
| Specified, not built | `assets` / `asset_lineage` / `generation_jobs` / `collections` / `smart_groups` / `asset_references` / `asset_acl` / `asset_audit_events` entities | `hermes-asset-library-backend-design.md` §核心实体 |
| Specified, not built | Two-phase upload (`uploads/init` signed URL -> `uploads/complete` -> async thumbnail/metadata/embedding) | §上传入库 |
| Specified, not built | List/detail/lineage/audit read APIs | §资产列表和详情 |
| Specified, not built | Collections CRUD (static membership) | §Collection |
| Specified, not built | Smart Groups preview/save/query (dynamic, never materialized) | §Smart Group |
| Specified, not built | References create/status for element/character/soul_id with honest `queued/training/ready` | §Character / Element / Soul ID; `hermes-soulid-element-asset-model.md` |
| Specified, not built | Mention/picker search endpoint with ACL filtering | §`@` mention 查询 |
| Specified, not built | Ref validation step in the generation chain | §生成链路 |
| Specified, not built | Event emission (`asset.*`, `reference.status`, `collection.updated`, …) | §实时事件 |
| Specified, not built | Two-layer search (hard filter + FTS/embedding recall) with index-pending degradation | §搜索和索引 |
| Specified, not built | Typed error table, fail-closed generation | §错误策略 |
| Gap | No storage backend chosen (object store, DB, vector index) | see Open Questions |

## User Entry Points

The service is not user-facing; it is reached through:

- Asset Library UI (gallery, detail, collections, mention menu) —
  `08-asset-library-ui.md`.
- Chat composer structured submits (mentions + attachments).
- Agent tools: the asset tool group `ultra_asset_upload / list / inspect /
  download / promote` (`04-skill-tool-prompt-contract.md` §Asset Tools;
  spec-only) and the reference tools mapped in
  `hermes-soulid-element-asset-model.md` §工具映射.
- Media Job Service output registration (`ultra_media_job_finalize` path).
- Files promotion (`06-files-task-file-browser.md` promote action).

## Feature List

| Feature | Status |
|---|---|
| Register uploads as `media_input` with mime/size validation | Planned (P0 切片 1) |
| Register generation outputs as `image_job`/`video_job`/`audio_job` assets | Planned (P0 item 9 in `06-delivery-plan.md`) |
| Asset lifecycle management (`uploading -> processing -> ready -> archived`) | Planned |
| Lineage graph (parents, source job, provider job, model, prompt hash, seed, user/session/run) | Planned (`03-media-asset-contract.md` §Lineage) |
| Element / Character / Soul ID references with provider training states | Planned (P0 切片 4: mock provider allowed, fake `ready` forbidden) |
| Collections (manual) and Smart Groups (dynamic query) | Planned (P0 切片 2-3) |
| Mention/picker search with `context=` variants | Planned (P0 切片 5) |
| Ref validation before generation (`media_generate` pre-check) | Planned (P0 切片 6) |
| Event fanout to UI | Planned (P0 切片 7) |
| ACL enforcement (read/use/update/delete/revoke) | Planned |
| Audit events incl. `mention_resolve` | Planned |
| Keyword extraction + prompt/visual embeddings | Planned; degradation to FTS-only is required behavior, not an error |

## State Machine

Asset lifecycle (`03-media-asset-contract.md` §Asset Lifecycle, status enum
in §核心实体):

```text
uploading -> processing -> ready -> archived
                 |-> failed
ready -> revoked
any -> deleted (explicit)
```

Generated outputs chain from jobs:

```text
job.created -> job.running -> job.succeeded -> asset.processing -> asset.ready
```

Reference (`asset_references.status`):

```text
queued -> training -> ready
queued -> failed
ready -> revoked
```

Transition triggers: upload completion (system), processing pipeline
(system), revoke (user with `revoke` permission), archive (user), training
progression (provider callbacks/polling). Revoked assets must drop out of
the mention/search usable set via the index pipeline (`asset.revoked ->
remove from usable set`).

## APIs & Events

Verbatim API surface from `hermes-asset-library-backend-design.md`:

```http
POST /api/assets/uploads/init           # returns upload_id, asset_id, put_url
POST /api/assets/uploads/complete
GET  /api/assets?project_id=&media_type=&type=&status=&endpoint=&source=&collection_id=&q=&cursor=
GET  /api/assets/{asset_id}
GET  /api/assets/{asset_id}/lineage
GET  /api/assets/{asset_id}/audit
POST /api/assets/collections            (+ PATCH/DELETE, members add/remove)
POST /api/assets/smart-groups/preview   (+ CRUD, /assets evaluation)
POST /api/assets/references             # element | character | soul_id
GET  /api/assets/mentions?q=&project_id=&types=&context=
```

Detail responses include the generation `context` block (endpoint,
model_route, prompt, params, seed, request/run/session ids), lineage, acl,
audit.

Events: `asset.upload.started`, `asset.processing`, `asset.ready`,
`asset.failed`, `asset.revoked`, `asset.indexed`, `collection.updated`,
`smart_group.updated`, `reference.status`, `job.status`.

Agent tool mapping (spec-only): `ultra_asset_upload/list/inspect/download/
promote` wrap these endpoints for the agent
(`04-skill-tool-prompt-contract.md` §Asset Tools).

## Data Model

Authoritative entity definitions are in
`hermes-asset-library-backend-design.md` §核心实体 and are not duplicated
here in full. Summary:

- `assets`: tenant/workspace/project scoping; `type` covers `media_input |
  image_job | video_job | audio_job | mesh_job | element | character |
  soul_id`; generation context fields (prompt, endpoint, model_route,
  params_json, seed); `object_key`/`thumbnail_key` into object storage.
- `asset_lineage`: parent links with `relation: input | output |
  derived_from | saved_as | character_source` and ordering.
- `generation_jobs`: provider job mirror with `request_json`,
  `output_asset_id`, `usage_event_id`, `error_code` (shared boundary with
  `10-media-job-service.md` — see Open Questions).
- `asset_references`: element/character/soul_id with provider_reference_id.
- `asset_acl`: subject (user/project/workspace/service_account) ×
  permission (read/use/update/delete/revoke).
- `asset_audit_events`: actor, action (incl. `mention_resolve`), run/request
  ids.
- `asset_search_index`: prompt text, keywords, endpoint family, embeddings —
  rebuildable, never authoritative.

## UI Behavior

(Service-side obligations to the UI; full UI spec in
`08-asset-library-ui.md`.)

- List APIs return only `read`-permitted rows; `allowed_operations` are
  precomputed per mention item so the UI never guesses.
- Mention results exclude `revoked`, include `not_ready` as non-usable.
- Detail payloads carry everything the inspector needs in one call (asset +
  context + lineage + acl + audit).
- Events carry enough to update a card without refetch (`asset_id`, status,
  `thumbnail_url`).
- Upload init returns size caps up front (`max_size`) so the UI can
  pre-validate.

## Permissions & Error Handling

ACL verbs: `read | use | update | delete | revoke` per subject scopes
user/project/workspace/service_account. Mention resolution writes audit
events (`mention_resolve`). Plain-text identity claims are rejected: the
backend must refuse ambiguous plain-text resolves and only trust structured
`entity_id`s (§前端交互契约).

Typed errors (§错误策略): `asset_access_denied`, `asset_not_ready`,
`asset_revoked`, `asset_not_found`, `upload_mime_not_allowed`,
`smart_group_query_invalid`, `collection_expand_requires_confirmation`.

Hard rules:

- Fail closed on any ref error in the generation chain — no
  warning-and-continue (§错误策略, consistent with U-29 semantics).
- Index unavailability degrades search (semantic recall "pending"), it never
  hides assets (§搜索和索引).
- Reference training may use a mock provider in P0 but must never fake
  `ready` (§P0 切片 4).
- Never log or return provider secrets in context payloads
  (`hermes-soulid-element-asset-model.md` §安全要求).

## Acceptance Criteria

- Upload init/complete produces an `assets` row that transitions
  `uploading -> processing -> ready` with a real thumbnail
  (`06-delivery-plan.md` P0; `03-media-asset-contract.md` §Acceptance:
  "Uploads and generated media produce asset ids").
- A finalized media job's outputs appear as assets with full lineage
  ("All generation results have lineage").
- `GET /api/assets/{id}` returns the generation context block sufficient for
  the inspector.
- A revoked asset disappears from mention results and fails `use` with
  `asset_revoked`.
- Smart Group open re-evaluates the query live (no stored member list).
- Tenant/project isolation: cross-project asset ids return
  `asset_not_found`.
- Every `use` in generation produces an audit event traceable by run id.

## Non-Goals

- Executing media jobs (Media Job Service) or holding provider credentials
  (TokenRouter).
- Storing binaries in the service DB — object storage owns bytes.
- Public sharing/publishing surfaces.
- Building the vector/semantic index before FTS + hard filters work.
- Auto-promotion of task files (explicit promotion only).

## Open Questions

1. Storage selection: object store (S3-compatible? local disk for
   self-host?), relational DB, and FTS/vector engine are all unchosen.
2. `generation_jobs` table ownership: this design doc places it here, while
   `10-media-job-service.md`'s MediaJob envelope implies the job service
   owns job state — one table with two writers, or mirror-by-event?
3. Multi-tenant deployment shape for self-hosted Hermes: are tenant_id /
   workspace_id real boundaries in MVP or constant defaults?
4. Thumbnail/embedding pipeline runtime: in-process worker vs queue; what
   is the P0-acceptable latency for `processing -> ready`?
5. Does `ultra_asset_download` materialize to the task file root (link to
   `06-files-task-file-browser.md`) or stream signed URLs only?
6. Soul ID provider abstraction: which provider backs `soul_id` training in
   P1, and what happens to `ready` references when that provider is
   disabled?
