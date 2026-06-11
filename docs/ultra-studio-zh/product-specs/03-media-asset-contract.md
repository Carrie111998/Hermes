# 媒体与资产合约

Status: product/backend contract  
Date: 2026-06-10

## 目标

将每一次上传、生成的图像、生成的视频、角色和元素都作为类型化的产品对象。Agent 应传递结构化的资产引用，而非将原始 ID 粘贴到提示词文本中。

## 资产类型

| 类型 | 含义 |
|---|---|
| `media_input` | 用户上传的图像/视频/音频/文件。 |
| `image_job` | 生成的图像输出。 |
| `video_job` | 生成的视频输出。 |
| `audio_job` | 生成的音频输出。 |
| `element` | 可复用的视觉元素，如产品、徽标、道具、场景。 |
| `character` | 可复用的人物/生物/虚拟形象身份。 |
| `soul_id` | 提供者或平台身份引用，用于保持一致性。 |
| `task_file` | 尚未提升为产品资产的会话文件。 |

## 媒体作业封装

不应将提供者 API 直接暴露给 Agent。使用与提供者无关的作业封装。

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

## 必需的作业工具

| 工具 | 用途 |
|---|---|
| `ultra_media_job_create` | 使用结构化输入创建图像/视频/音频作业。 |
| `ultra_media_job_status` | 返回持久化的作业状态和进度。 |
| `ultra_media_job_cancel` | 如支持，取消已排队/运行中的作业。 |
| `ultra_media_job_retry` | 使用编译后的修复计划重试。 |
| `ultra_media_job_finalize` | 将输出注册为资产、缩略图和来源链路。 |
| `ultra_media_constraints_get` | 在提示词编译前返回模型/提供者限制。 |

## 资产生命周期

```text
uploading
  -> processing
  -> ready
  -> archived
```

失败状态：

- `failed`
- `revoked`
- `deleted`

生成的输出：

```text
job.created
  -> job.running
  -> job.succeeded
  -> asset.processing
  -> asset.ready
```

## 资产卡片 UI

每张媒体卡片应展示：

- preview
- status
- media type
- provider/model
- dimensions/duration
- prompt hash
- input asset refs
- job id
- download
- inspect
- reuse
- convert to element
- create character, when eligible

默认情况下，卡片不应暴露内部文件系统路径。

## 来源链路

来源链路必须捕获：

- parent asset ids
- source job id
- provider job id
- model and endpoint
- prompt hash
- seed/params
- user/session/run
- output asset ids

这是“再次使用此资产”、“制作变体”、“为何失败”以及“该资产来自何处”的基础。

## 质量保证（QA）

质量保证（QA）必须将观察到的事实与推断的质量分开。

观察到：

- media can be downloaded
- file exists
- duration/dimensions
- first frame/thumbnail available
- job succeeded

推断：

- prompt alignment
- style fit
- character consistency
- readability
- visual defects

若无观察步骤或用户审核，Agent 不能声称视觉质量。

## 验收标准

- 上传内容和生成媒体都会产生资产 ID。
- 所有生成结果都有来源链路。
- 检查器可以打开任意资产卡片，并展示模型、作业、提示词和输入详情。
- 下载操作使用真实存储地址、对象地址或本地落盘文件。
- 失败作业仍可检查。
