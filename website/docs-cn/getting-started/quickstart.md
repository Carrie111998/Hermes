---
sidebar_position: 1
title: "快速入门"
description: "与 Hermes Agent 的首次对话 — 从安装到聊天，不到 5 分钟"
---

# 快速入门

本指南将帮助你从零开始建立一个可工作的 Hermes 设置，能够经受实际使用。安装、选择提供者、验证工作聊天，并确切知道出现问题时该如何处理。

## 适用对象

- 全新接触，希望获得最短路径的可工作设置
- 更换提供者，不想因配置错误浪费时间
- 为团队、机器人或常驻工作流设置 Hermes
- 对"已安装，但它仍然什么都不做"感到厌烦

## 最快路径

选择符合你目标的一行：

| 目标 | 首先这样做 | 然后这样做 |
|---|---|---|
| 我只想让 Hermes 在我的机器上工作 | `hermes setup` | 运行真实聊天并验证其响应 |
| 我已经知道我的提供者 | `hermes model` | 保存配置，然后开始聊天 |
| 我想要一个机器人或常驻设置 | CLI 工作后运行 `hermes gateway setup` | 连接 Telegram、Discord、Slack 或另一个平台 |
| 我想要本地或自托管模型 | `hermes model` → 自定义端点 | 验证端点、模型名称和上下文长度 |
| 我想要多提供者回退 | 首先运行 `hermes model` | 在基本聊天工作后再添加路由和回退 |

**经验法则：** 如果 Hermes 无法完成正常聊天，则不要添加更多功能。首先完成一次干净的对话，然后再添加网关、cron、技能、语音或路由。

---

## 1. 安装 Hermes Agent

运行一键安装程序：

```bash
# Linux / macOS / WSL2 / Android (Termux)
curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash
```

:::tip Android / Termux
如果你在手机上安装，请参阅专门的 [Termux 指南](./termux.md)，了解经过测试的手动路径、支持的附加功能和当前 Android 特定限制。
:::

:::tip Windows 用户
首先安装 [WSL2](https://learn.microsoft.com/en-us/windows/wsl/install)，然后在你的 WSL2 终端内运行上述命令。
:::

完成后，重新加载你的 shell：

```bash
source ~/.bashrc   # 或 source ~/.zshrc
```

有关详细安装选项、前置要求和故障排除，请参阅 [安装指南](./installation.md)。

## 2. 选择提供者

最重要的设置步骤。使用 `hermes model` 交互式地完成选择：

```bash
hermes model
```

良好默认值：

| 情况 | 推荐路径 |
|---|---|
| 最少摩擦 | Nous Portal 或 OpenRouter |
| 你已经有 Claude 或 Codex 授权 | Anthropic 或 OpenAI Codex |
| 你想要本地/私有推理 | Ollama 或任何自定义 OpenAI 兼容端点 |
| 你想要多提供者路由 | OpenRouter |
| 你有一个自定义 GPU 服务器 | vLLM、SGLang、LiteLLM 或任何 OpenAI 兼容端点 |

对于大多数首次用户：选择一个提供者，接受默认值，除非你知道为什么要更改它们。完整的提供者目录及环境变量和设置步骤在 [提供者](../integrations/providers.md) 页面。

:::caution 最小上下文：64K tokens
Hermes Agent 需要一个至少有 **64,000 tokens** 上下文的模型。窗口较小的模型无法为多步骤工具调用工作流保持足够的工作内存，并将在启动时被拒绝。大多数托管模型（Claude、GPT、Gemini、Qwen、DeepSeek）很容易满足此要求。如果你正在运行本地模型，请将其上下文大小设置为至少 64K（例如，llama.cpp 的 `--ctx-size 65536` 或 Ollama 的 `-c 65536`）。
:::

:::tip
你可以随时使用 `hermes model` 切换提供者 — 无锁定。有关所有支持的提供者和设置详情的完整列表，请参阅 [AI 提供者](../integrations/providers.md)。
:::

### 设置如何存储

Hermes 将密钥与普通配置分开：

- **密钥和令牌** → `~/.hermes/.env`
- **非秘密设置** → `~/.hermes/config.yaml`

正确设置值的最简单方法是通过 CLI：

```bash
hermes config set model anthropic/claude-opus-4.6
hermes config set terminal.backend docker
hermes config set OPENROUTER_API_KEY sk-or-...
```

正确的值会自动进入正确的文件。

## 3. 运行你的第一次聊天

```bash
hermes            # 经典 CLI
hermes --tui      # 现代 TUI（推荐）
```

你会看到一个欢迎横幅，显示你的模型、可用工具和技能。使用具体且易于验证的提示：

:::tip 选择你的界面
Hermes 附带两个终端界面：经典 `prompt_toolkit` CLI 和较新的 [TUI](../user-guide/tui.md) 带有模态覆盖、鼠标选择和非阻塞输入。两者共享相同的会话、斜杠命令和配置 — 用 `hermes` vs `hermes --tui` 试用每一个。
:::

```
用 5 个要点总结这个仓库，并告诉我主要入口点是什么。
```

```
检查我当前目录并告诉我看起来像主项目文件的是什么。
```

```
帮我为这个代码库建立一个干净的 GitHub PR 工作流。
```

**成功的样子：**

- 横幅显示你选择的模型/提供者
- Hermes 无错误地回复
- 如需要，它可以使用工具（终端、文件读取、网络搜索）
- 对话在多个回合中正常继续

如果这有效，你就度过了最难的部分。

## 4. 验证会话工作

在继续之前，确保恢复功能正常：

```bash
hermes --continue    # 恢复最近的会话
hermes -c            # 简短形式
```

这应该把你带回你刚才的会话。如果不这样，请检查你是否在同一个配置文件中，以及会话是否确实已保存。当你在多个设置或机器之间切换时，这很重要。

## 5. 尝试关键功能

### 使用终端

```
❯ 我的磁盘使用情况如何？显示最大的 5 个目录。
```

代理代表你运行终端命令并显示结果。

### 斜杠命令

键入 `/` 查看所有命令的自动补全下拉菜单：

| 命令 | 作用 |
|---------|-------------|
| `/help` | 显示所有可用命令 |
| `/tools` | 列出可用工具 |
| `/model` | 交互式切换模型 |
| `/personality pirate` | 尝试有趣的人格 |
| `/save` | 保存对话 |

### 多行输入

按 `Alt+Enter` 或 `Ctrl+J` 添加新行。非常适合粘贴代码或编写详细提示。

### 中断代理

如果代理花费太长时间，输入新消息并按 Enter — 它会中断当前任务并切换到你的新指令。`Ctrl+C` 也可以。

## 6. 添加下一层

仅在基础聊天工作后。选择你需要的：

### 机器人或共享助手

```bash
hermes gateway setup    # 交互式平台配置
```

连接 [Telegram](/docs/user-guide/messaging/telegram)、[Discord](/docs/user-guide/messaging/discord)、[Slack](/docs/user-guide/messaging/slack)、[WhatsApp](/docs/user-guide/messaging/whatsapp)、[Signal](/docs/user-guide/messaging/signal)、[Email](/docs/user-guide/messaging/email) 或 [Home Assistant](/docs/user-guide/messaging/homeassistant)。

### 自动化和工具

- `hermes tools` — 调整每个平台的工具访问权限
- `hermes skills` — 浏览和安装可重用工作流
- Cron — 仅在你的机器人或 CLI 设置稳定后

### 沙盒终端

为了安全起见，在 Docker 容器或远程服务器上运行代理：

```bash
hermes config set terminal.backend docker    # Docker 隔离
hermes config set terminal.backend ssh       # 远程服务器
```

### 语音模式

```bash
pip install "hermes-agent[voice]"
# 包含免费的本地语音转文本的 faster-whisper
```

然后在 CLI 中：`/voice on`。按 `Ctrl+B` 录音。参见 [语音模式](../user-guide/features/voice-mode.md)。

### 技能

```bash
hermes skills search kubernetes
hermes skills install openai/skills/k8s
```

或在聊天会话中使用 `/skills`。

### MCP 服务器

```yaml
# 添加到 ~/.hermes/config.yaml
mcp_servers:
  github:
    command: npx
    args: ["-y", "@modelcontextprotocol/server-github"]
    env:
      GITHUB_PERSONAL_ACCESS_TOKEN: "ghp_xxx"
```

### 编辑器集成 (ACP)

```bash
pip install -e '.[acp]'
hermes acp
```

参见 [ACP 编辑器集成](../user-guide/features/acp.md)。

---

## 常见失败模式

这些是浪费最多时间的问题：

| 症状 | 可能原因 | 修复 |
|---|---|---|
| Hermes 打开但给出空或损坏的回复 | 提供者授权或模型选择错误 | 再次运行 `hermes model` 并确认提供者、模型和授权 |
| 自定义端点"工作"但返回垃圾 | 错误的基础 URL、模型名称或实际上不兼容 OpenAI | 首先在单独客户端中验证端点 |
| 网关启动但没有人可以给它发消息 | 机器人令牌、允许列表或平台设置不完整 | 重新运行 `hermes gateway setup` 并检查 `hermes gateway status` |
| `hermes --continue` 找不到旧会话 | 切换了配置文件或会话从未保存 | 检查 `hermes sessions list` 并确认你在正确的配置文件中 |
| 模型不可用或奇怪的回退行为 | 提供者路由或回退设置过于激进 | 在基础提供者稳定之前关闭路由 |
| `hermes doctor` 标记配置问题 | 配置值缺失或过时 | 修复配置，在添加功能之前重新测试纯聊天 |

## 恢复工具包

当感觉不对劲时，使用此顺序：

1. `hermes doctor`
2. `hermes model`
3. `hermes setup`
4. `hermes sessions list`
5. `hermes --continue`
6. `hermes gateway status`

该序列可让你从"异常感觉"快速回到已知状态。

---

## 快速参考

| 命令 | 描述 |
|---------|-------------|
| `hermes` | 开始聊天 |
| `hermes model` | 选择你的 LLM 提供者和模型 |
| `hermes tools` | 配置每个平台启用哪些工具 |
| `hermes setup` | 完整设置向导（一次性配置所有内容） |
| `hermes doctor` | 诊断问题 |
| `hermes update` | 更新到最新版本 |
| `hermes gateway` | 启动消息网关 |
| `hermes --continue` | 恢复最后会话 |

## 下一步

- **[CLI 指南](../user-guide/cli.md)** — 掌握终端界面
- **[配置](../user-guide/configuration.md)** — 自定义你的设置
- **[消息网关](../user-guide/messaging/index.md)** — 连接 Telegram、Discord、Slack、WhatsApp、Signal、Email 或 Home Assistant
- **[工具与工具集](../user-guide/features/tools.md)** — 探索可用功能
- **[AI 提供者](../integrations/providers.md)** — 完整提供者列表和设置详情
- **[技能系统](../user-guide/features/skills.md)** — 可重用工作流和知识
- **[提示与最佳实践](../guides/tips.md)** — 高级用户提示

