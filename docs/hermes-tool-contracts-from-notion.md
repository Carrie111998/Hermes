# Notion 工具合约映射

状态：接口捕获与本地映射
来源：`docs/notion-source/hermes/pages/05-工具调用说明文档.md`

## 范围

The Notion page captures a `default_api:*` tool namespace and a list of toolsets. This document turns that into local Hermes MVP guidance.

Do not treat `default_api:*` as a public API. It is an observed/source-described tool boundary. Hermes should map it into local tool names and adapters.

## 工具集族

Notion lists these loadable toolsets:

`ads`, `artifacts`, `ask_user_question`, `browser`, `browser-cdp`, `clarify`, `code_execution`, `connectors`, `data_ingestion`, `debugging`, `delegation`, `discord`, `feishu_doc`, `feishu_drive`, `file`, `higgsfield`, `higgsfield_audio`, `homeassistant`, `image_gen`, `instagram`, `memory`, `messaging`, `moa`, `rl`, `safe`, `scheduling`, `search`, `session_search`, `skills`, `spotify`, `terminal`, `tiktok`, `todo`, `trends`, `tts`, `video_adapt`, `vision`, `web`, `youtube`.

For MVP, keep the first local slice smaller:

| MVP 工具族 | 选择原因 |
|---|---|
| `skills` | Needed for Skill loading and protected `references/`. |
| `file` | Needed for local workspace artifacts and attachments. |
| `terminal` | Needed for real agent execution. |
| `artifacts` | Needed for chat state and generated outputs. |
| `memory` / `todo` | Needed for agent continuity. |
| `higgsfield` adapter | Needed for media generation and assets. |
| `vision` / `video_adapt` | Needed for media input analysis. |
| `ask_user_question` | Needed for structured clarification and asset pickers. |

## 高优先级工具合约

### `ask_user_question`

Required modes:

- `text`: free-form clarification.
- `entity`: choose `soul_id`, `element`, `voice`, or `language`.
- `files`: request uploads with accept/min/max constraints.

Local UI implication: the web MVP needs modal or side-panel prompts, not just text chat.

### `higgsfield_generate`

Key fields:

- `requests[]`
- `model`
- `params`
- `media_type`
- `async`
- `concurrency`
- `limits`
- `poll_interval`
- `timeout_seconds`

Local implementation: map to `media_generate`, enqueue a job, stream status, and return output asset IDs.

### `higgsfield_soul_id`

Actions:

- `create`
- `delete`
- `list`
- `status`

Local implementation: map to identity-reference creation and status polling. Use visible states: `queued`, `not_ready`, `in_progress`, `completed`, `failed`.

### `higgsfield_element`

Actions:

- `create`
- `get`
- `list`

Local implementation: map to asset reference create/list/get. Support categories such as character, environment, and prop.

### `video_analyze` and `audio_analyze`

Key fields:

- source URL or local media ID.
- prompt/category.
- optional model.
- time window.
- fps and media resolution.
- text-only mode.

Local implementation: simple worker first; CometAPI only after scale need.

## 本地命名映射

| Notion name | Local interface | Why |
|---|---|---|
| `default_api:higgsfield_generate` | `media_generate` | Keeps provider-specific names behind adapters. |
| `default_api:higgsfield_job_status` | `media_job_status` | Same job model across providers. |
| `default_api:higgsfield_soul_id` | `identity_reference_*` | Describes product role rather than provider. |
| `default_api:higgsfield_element` | `asset_reference_*` | Works for non-Higgsfield assets too. |
| `default_api:higgsfield_upload` | `media_upload` | Common media ingestion. |
| `default_api:skill_view` | `skill_view` | Core Hermes concept can keep this name. |
| `default_api:skills_list` | `skills_list` | Public Skill registry surface. |

## MVP 工具建设路径

1. Implement `skills_list` and `skill_view` with protected reference loading.
2. Implement `media_upload` and attachment markers for chat.
3. Implement `asset_reference_create/list/get` for Element.
4. Implement `identity_reference_train/status/list` with a fake-provider-disabled default. If no real provider is configured, return a visible unavailable error.
5. Implement `media_generate` as async job orchestration with real provider adapter hooks.
6. Implement `ask_user_question` UI prompts for entity and file selection.

## 验收检查

- Tool schema validation rejects undeclared fields.
- Tool errors are visible and structured.
- No tool silently fabricates media, assets, or model responses.
- Asset IDs are checked against tenant/workspace/project ACL before tool execution.
- `skill_view` can load authorized references but cannot be used as a bulk exfiltration path.
