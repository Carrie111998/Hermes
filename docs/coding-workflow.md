# Hermes 编码闭环

当用户明确要求修改代码时，`coding_worker` 会在指定 Git 仓库中执行：

1. Codex 或 Claude Code 实现任务并运行相关验证。
2. 另一种 Worker 独立审查未提交差异。
3. Hermes 读取真实输出后再汇报；Worker 不会自行提交、推送或创建 PR。

## 配置

```yaml
agent:
  coding_worker:
    enabled: true
    worker: codex
    review_worker: claude
    timeout_seconds: 900
```

Worker 只接受 Git 仓库目录，且仅支持 `codex`、`claude`。CLI 必须已登录。

## VS Code ACP

安装 ACP Client 扩展后，将 `integrations/vscode-settings.json` 中的 `acp.agents` 合并到 VS Code 用户设置；然后在 ACP Client 中选择 **Hermes Agent**。

## Issue → PR

GitHub CLI 登录后，可在代码仓库内由 Hermes 按以下顺序执行：读取 Issue → 建分支 → 调用 `coding_worker` → 验证 → 提交 → 创建 PR。未登录时流程会在 GitHub 操作前明确停止，不会伪造 PR。
