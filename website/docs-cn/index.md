---
slug: /
sidebar_position: 0
title: "Hermes Agent 文档"
description: "由 Nous Research 构建的自我改进 AI 代理。内置学习循环，从经验中创建技能，在使用过程中改进技能，并跨会话记忆。"
hide_table_of_contents: true
displayed_sidebar: docs
---

# Hermes Agent

由 [Nous Research](https://nousresearch.com) 构建的自我改进 AI 代理。这是唯一具有内置学习循环的代理 — 它从经验中创建技能，在使用过程中改进技能，促使自己持久化知识，并在跨会话中建立对你的深入理解模型。

<div style={{display: 'flex', gap: '1rem', marginBottom: '2rem', flexWrap: 'wrap'}}>
  <a href="/docs-cn/getting-started/installation" style={{display: 'inline-block', padding: '0.6rem 1.2rem', backgroundColor: '#FFD700', color: '#07070d', borderRadius: '8px', fontWeight: 600, textDecoration: 'none'}}>开始使用 →</a>
  <a href="https://github.com/NousResearch/hermes-agent" style={{display: 'inline-block', padding: '0.6rem 1.2rem', border: '1px solid rgba(255,215,0,0.2)', borderRadius: '8px', textDecoration: 'none'}}>在 GitHub 上查看</a>
</div>

## 什么是 Hermes Agent？

它不是一个绑定到 IDE 的代码助手或围绕单个 API 的聊天机器人包装器。它是一个**自主代理**，运行时间越长，能力越强。它可以存在于任何地方 — $5 的 VPS、GPU 集群，或在空闲时几乎零成本的无服务器基础设施（Daytona、Modal）。当你自己在云 VM 上工作时，可以通过 Telegram 与它对话，而你无需亲自 SSH 登录。它不依赖于你的笔记本电脑。

## 快速链接

| | |
|---|---|
| 🚀 **[安装指南](/docs-cn/getting-started/installation)** | 在 Linux、macOS 或 WSL2 上 60 秒内完成安装 |
| 📖 **[快速入门教程](/docs-cn/getting-started/quickstart)** | 你的第一次对话和要尝试的关键功能 |
| 🗺️ **[学习路径](/docs-cn/getting-started/learning-path)** | 找到适合你经验水平的文档 |
| ⚙️ **[配置](/docs-cn/user-guide/configuration)** | 配置文件、提供者、模型和选项 |
| 💬 **[消息网关](/docs-cn/user-guide/messaging)** | 设置 Telegram、Discord、Slack 或 WhatsApp |
| 🔧 **[工具与工具集](/docs-cn/user-guide/features/tools)** | 47 个内置工具及其配置方法 |
| 🧠 **[记忆系统](/docs-cn/user-guide/features/memory)** | 跨会话增长的持久化记忆 |
| 📚 **[技能系统](/docs-cn/user-guide/features/skills)** | 代理创建和重用的程序性记忆 |
| 🔌 **[MCP 集成](/docs-cn/user-guide/features/mcp)** | 连接到 MCP 服务器，过滤其工具，安全地扩展 Hermes |
| 🧭 **[在 Hermes 中使用 MCP](/docs-cn/guides/use-mcp-with-hermes)** | 实用的 MCP 设置模式、示例和教程 |
| 🎙️ **[语音模式](/docs-cn/user-guide/features/voice-mode)** | 在 CLI、Telegram、Discord 和 Discord VC 中进行实时语音交互 |
| 🗣️ **[在 Hermes 中使用语音模式](/docs-cn/guides/use-voice-mode-with-hermes)** | Hermes 语音工作流程的实际设置和使用模式 |
| 🎭 **[个性与 SOUL.md](/docs-cn/user-guide/features/personality)** | 使用全局 SOUL.md 定义 Hermes 的默认声音 |
| 📄 **[上下文文件](/docs-cn/user-guide/features/context-files)** | 塑造每次对话的项目上下文文件 |
| 🔒 **[安全性](/docs-cn/user-guide/security)** | 命令批准、授权、容器隔离 |
| 💡 **[提示与最佳实践](/docs-cn/guides/tips)** | 充分利用 Hermes 的快速技巧 |
| 🏗️ **[架构](/docs-cn/developer-guide/architecture)** | 底层工作原理 |
| ❓ **[常见问题与故障排除](/docs-cn/reference/faq)** | 常见问题和解决方案 |

## 核心特性

- **闭环学习** — 代理整理的记忆与定期提示、自主技能创建、使用过程中的技能自我改进、带有 LLM 摘要的 FTS5 跨会话召回，以及 [Honcho](https://github.com/plastic-labs/honcho) 辩证用户建模
- **随处运行，不仅限于你的笔记本电脑** — 6 种终端后端：本地、Docker、SSH、Daytona、Singularity、Modal。Daytona 和 Modal 提供无服务器持久化 — 你的环境在空闲时休眠，成本几乎为零
- **在你所在的地方生活** — CLI、Telegram、Discord、Slack、WhatsApp、Signal、Matrix、Mattermost、Email、SMS、钉钉、飞书、企业微信、BlueBubbles、Home Assistant — 一个网关支持 15+ 平台
- **由模型训练者构建** — 由 [Nous Research](https://nousresearch.com) 创建，这是 Hermes、Nomos 和 Psyche 背后的实验室。可与 [Nous Portal](https://portal.nousresearch.com)、[OpenRouter](https://openrouter.ai)、OpenAI 或任何端点配合使用
- **计划自动化** — 内置 cron，可传递到任何平台
- **委托与并行化** — 生成隔离的子代理进行并行工作流。通过 `execute_code` 的程序化工具调用将多步骤管道折叠为单次推理调用
- **开放标准技能** — 兼容 [agentskills.io](https://agentskills.io)。技能是可移植、可共享的，并通过技能中心由社区贡献
- **完整的 Web 控制** — 搜索、提取、浏览、视觉、图像生成、TTS
- **MCP 支持** — 连接到任何 MCP 服务器以扩展工具功能
- **研究就绪** — 批处理、轨迹导出、使用 Atropos 进行 RL 训练。由 [Nous Research](https://nousresearch.com) 构建 — Hermes、Nomos 和 Psyche 模型背后的实验室

