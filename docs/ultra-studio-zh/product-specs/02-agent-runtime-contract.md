# Agent Runtime Contract

Status: runtime specification  
Date: 2026-06-10

## Goal

定义 Web UI、Hermes 网关、沙盒、Agent 运行时、工具以及长时间运行的媒体作业之间必须发生的交互。

本合约将"聊天处于活动状态"与"媒体作业正在运行"分离开来。媒体作业的生命周期可能超过 WebSocket 重连、浏览器刷新或 Worker 重启。

## Runtime Shape

```text
Web UI
  -> Edge/Gateway
  -> session.create / session.resume
  -> prompt.submit
  -> Agent runtime
  -> skill router
  -> tool call
  -> media job event log
  -> worker/provider
  -> asset registration
  -> event fanout back to UI
```

## Session Lifecycle

必需方法：

- `session.create`：创建新的对话和运行上下文。
- `session.resume`：恢复消息、活动作业、已选资产和任务文件。
- `prompt.submit`：提交用户文本和键入的附件。
- `slash.exec`：显式操作的可选命令路径。

会话状态必须包含：

- user/workspace/project ids
- model selection
- active skill profile
- active sandbox id（如果已附加）
- active task files root
- active media jobs
- selected assets

## Event Stream

UI 不应仅轮询对话记录。它需要网关事件。

必需事件：

| Event | Purpose |
|---|---|
| `message.start` | Assistant 消息开始。 |
| `message.delta` | 流式文本。 |
| `message.complete` | Assistant 回合完成。 |
| `thinking.delta` | 可选的推理/状态文本。 |
| `status.update` | 高级阶段变更。 |
| `tool.start` | 工具调用开始。 |
| `tool.progress` | 工具状态/进度。 |
| `tool.complete` | 工具调用完成。 |
| `tool.error` | 工具失败并返回类型化错误。 |
| `media_job.created` | 持久化媒体作业已创建。 |
| `media_job.updated` | 作业状态已变更。 |
| `asset.ready` | 输出资产已注册并可预览。 |
| `approval.requested` | 需要用户决策。 |
| `approval.resolved` | 用户已批准/编辑/拒绝。 |

## Sandbox Lifecycle

沙盒是一台任务计算机，而非实现细节。

必需操作：

- `sandbox.create`
- `sandbox.attach`
- `sandbox.sleep`
- `sandbox.wake`
- `sandbox.recycle`
- `sandbox.restore_artifacts`

沙盒不得持有静态的 Provider 密钥。它接收一个短期受限的令牌（如 `HF_JWT_TOKEN`），然后由 TokenRouter 处理凭证交换和 Provider 策略。

## Task Files

每个会话可能会生成尚未成为产品资产的文件：

- uploaded originals
- prompt JSON
- storyboard JSON/images
- generated scripts
- logs
- thumbnails
- intermediate frames
- final media

任务文件仅通过显式注册或提升才能成为资产库条目。

## Human Approval Gateway

对于涉及花钱、暴露私有媒体、接触已登录账户、运行本地命令或对外发布的操作，需要审批。

决策类型：

- `approve`
- `edit`
- `reject`
- `respond`

Agent 必须能够暂停并从持久化状态恢复。页面刷新不得丢失审批请求。

## Error Contract

错误必须是类型化的：

- `missing_credential`
- `unsupported_model_capability`
- `invalid_asset_ref`
- `provider_rejected_input`
- `quota_exceeded`
- `job_timeout`
- `asset_upload_failed`
- `sandbox_unavailable`
- `approval_required`

不要将这些转换为模糊的道歉信息。UI 和 Agent 都需要类型化的错误，以便显示恢复操作。

## Acceptance

- 在媒体作业期间刷新浏览器不会丢失该作业。
- 恢复的会话会显示活动的媒体作业及其当前状态。
- 失败的 Provider 请求会显示可见的 Provider 错误和重试路径。
- Agent 不能在没有事件、产物或账本记录的情况下声称已完成。
