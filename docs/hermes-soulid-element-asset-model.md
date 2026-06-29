# Soul ID 与 Element 资产模型

状态：资产模型设计补充
来源：

- `docs/notion-source/hermes/pages/01-soulid.md`
- `docs/notion-source/hermes/pages/04-资产管理-element-management.md`

## 范围

Soul ID and Element are persistent semantic assets. They let the agent reuse identity, character, prop, and environment references across tasks without asking the user to re-upload or re-describe everything.

This document defines the local Hermes asset model. Claims about exact Higgsfield model internals such as LoRA, adapters, or cross-attention should remain experimental until verified.

## 资产类型

| 资产类型 | 用途 | 创建方式 | 消费方式 |
|---|---|---|---|
| `soul_id` | Face/identity consistency. | `higgsfield_soul_id(action="create")` equivalent. | Image/video generation models that support identity conditioning. |
| `element` | Persistent character, environment, prop, or object reference. | `higgsfield_element(action="create")` equivalent. | Prompt compiler, image/video generation, asset reuse. |
| `media_input` | Uploaded media ID. | Upload API or media ingestion. | Vision/video/audio analysis and generation. |
| `image_job` / `video_job` | Generated output reference. | Generation jobs. | Reuse as image/video reference or asset seed. |

## Soul ID 流程

1. User uploads identity photos.
2. Hermes stores files as scoped project assets.
3. Asset manager starts an asynchronous training or identity-reference job.
4. Job returns `reference_id`.
5. Project asset store records `reference_id`, owner, source files, status, and lineage.
6. Generation calls pass `soul_id`/`reference_id` as structured params, not natural-language-only descriptions.

## Element 流程

Element should behave differently from Soul ID:

- It can wrap existing generated outputs or uploaded media.
- It classifies the asset as character, environment, prop, or another configured category.
- It may be inserted into prompts through an element tag or passed as a structured generation param.
- It does not imply retraining by default.

## 最小数据模型

| Table | Key fields |
|---|---|
| `asset_references` | `id`, `tenant_id`, `workspace_id`, `project_id`, `type`, `name`, `status`, `created_by`, `created_at` |
| `asset_reference_sources` | `reference_id`, `source_asset_id`, `source_kind`, `lineage_order` |
| `identity_jobs` | `id`, `reference_id`, `status`, `training_type`, `input_count`, `started_at`, `completed_at`, `error_code` |
| `generation_jobs` | `id`, `model`, `media_type`, `status`, `params`, `output_asset_id`, `usage_event_id` |
| `asset_acl` | `asset_id`, `subject_type`, `subject_id`, `permission` |

Use tenant/workspace/project filters and PostgreSQL RLS as a second line of defense.

## 工具映射

| Notion tool | Local Hermes MVP name | Notes |
|---|---|---|
| `higgsfield_soul_id` | `identity_reference_train` / `identity_reference_status` | Keep provider-specific name behind adapter. |
| `higgsfield_element` | `asset_reference_create` / `asset_reference_list` | Works for character, environment, prop. |
| `higgsfield_generate` | `media_generate` | Consumes `soul_id`, `element_id`, `media_input`, `image_job`, `video_job`. |
| `higgsfield_attachments_list` | `asset_reference_list` or `attachment_list` | Split persistent assets from session uploads. |

## UI 要求

- Asset picker must distinguish `Soul ID`, `Element`, upload, image job, and video job.
- The user should see training/status states for identity assets.
- The chat composer can attach files, but persistent asset creation should be explicit.
- Generation result cards should expose "reuse as reference" and "save as Element" actions.

## 安全要求

- An asset ID is not permission by itself; TokenRouter or the asset service must verify ACL.
- Deleted or revoked assets cannot be used by later jobs.
- Prompt text cannot smuggle an unauthorized `element_id` or `soul_id`.
- Source upload paths must not be exposed to another tenant or workspace.

## MVP 验收检查

- User can create a pending identity reference and poll status.
- User can create an Element from an uploaded image or generated job.
- Generation can consume an authorized `soul_id` or `element_id`.
- Generation fails with a visible error for unauthorized or not-ready assets.
- Asset lineage links output back to the original upload or generation job.
