---
sidebar_position: 3
title: '学习路径'
description: '根据你的经验水平和目标选择 Hermes Agent 文档的学习路径。'
---

# 学习路径

Hermes Agent 可以做很多事情 — CLI 助手、Telegram/Discord 机器人、任务自动化、RL 训练等等。本页面帮助你根据经验水平和想要实现的目标确定从哪里开始以及阅读什么内容。

:::tip 从这里开始
如果你还没有安装 Hermes Agent，请从 [安装指南](/docs-cn/getting-started/installation) 开始，然后运行 [快速入门](/docs-cn/getting-started/quickstart)。下面的所有内容都假设你已经有一个可工作的安装。
:::

## 如何使用本页面

- **知道你的水平？** 跳转到 [经验水平表](#by-experience-level) 并按照你层级的阅读顺序进行。
- **有特定目标？** 跳到 [按用例](#by-use-case) 并找到匹配的场景。
- **只是浏览？** 查看 [核心功能](#key-features-at-a-glance) 表以快速了解 Hermes Agent 可以做什么。

## 按经验水平

| 级别 | 目标 | 推荐阅读 | 时间估算 |
|---|---|---|---|
| **初学者** | 启动并运行，进行基本对话，使用内置工具 | [安装](/docs-cn/getting-started/installation) → [快速入门](/docs-cn/getting-started/quickstart) → [CLI 使用](/docs-cn/user-guide/cli) → [配置](/docs-cn/user-guide/configuration) | ~1 小时 |
| **中级** | 设置消息机器人，使用高级功能如记忆、cron 作业和技能 | [会话](/docs-cn/user-guide/sessions) → [消息传递](/docs-cn/user-guide/messaging) → [工具](/docs-cn/user-guide/features/tools) → [技能](/docs-cn/user-guide/features/skills) → [记忆](/docs-cn/user-guide/features/memory) → [Cron](/docs-cn/user-guide/features/cron) | ~2–3 小时 |
| **高级** | 构建自定义工具、创建技能、使用 RL 训练模型、为项目做贡献 | [架构](/docs-cn/developer-guide/architecture) → [添加工具](/docs-cn/developer-guide/adding-tools) → [创建技能](/docs-cn/developer-guide/creating-skills) → [RL 训练](/docs-cn/user-guide/features/rl-training) → [贡献](/docs-cn/developer-guide/contributing) | ~4–6 小时 |

## 按用例

选择与你想做的事情匹配的场景。每个场景都按你应该阅读的顺序链接到相关文档。

### "我想要一个 CLI 编码助手"

将 Hermes Agent 用作交互式终端助手来编写、审查和运行代码。

1. [安装](/docs-cn/getting-started/installation)
2. [快速入门](/docs-cn/getting-started/quickstart)
3. [CLI 使用](/docs-cn/user-guide/cli)
4. [代码执行](/docs-cn/user-guide/features/code-execution)
5. [上下文文件](/docs-cn/user-guide/features/context-files)
6. [提示与技巧](/docs-cn/guides/tips)

:::tip
使用上下文文件直接将文件传递到你的对话中。Hermes Agent 可以读取、编辑和运行你项目中的代码。
:::

### "我想要一个 Telegram/Discord 机器人"

在你喜欢的消息传递平台上部署 Hermes Agent 作为机器人。

1. [安装](/docs-cn/getting-started/installation)
2. [配置](/docs-cn/user-guide/configuration)
3. [消息传递概述](/docs-cn/user-guide/messaging)
4. [Telegram 设置](/docs-cn/user-guide/messaging/telegram)
5. [Discord 设置](/docs-cn/user-guide/messaging/discord)
6. [语音模式](/docs-cn/user-guide/features/voice-mode)
7. [在 Hermes 中使用语音模式](/docs-cn/guides/use-voice-mode-with-hermes)
8. [安全性](/docs-cn/user-guide/security)

有关完整的项目示例，请参阅：
- [每日简报机器人](/docs-cn/guides/daily-briefing-bot)
- [团队 Telegram 助手](/docs-cn/guides/team-telegram-assistant)

### "我想要自动化任务"

安排重复性任务、运行批处理作业或将代理操作链接在一起。

1. [快速入门](/docs-cn/getting-started/quickstart)
2. [Cron 调度](/docs-cn/user-guide/features/cron)
3. [批处理](/docs-cn/user-guide/features/batch-processing)
4. [委托](/docs-cn/user-guide/features/delegation)
5. [钩子](/docs-cn/user-guide/features/hooks)

:::tip
Cron 作业让 Hermes Agent 按计划运行任务 — 每日摘要、定期检查、自动报告 — 无需你在场。
:::

### "我想要构建自定义工具/技能"

用你自己的工具和可重用技能包扩展 Hermes Agent。

1. [工具概述](/docs-cn/user-guide/features/tools)
2. [技能概述](/docs-cn/user-guide/features/skills)
3. [MCP（模型上下文协议）](/docs-cn/user-guide/features/mcp)
4. [架构](/docs-cn/developer-guide/architecture)
5. [添加工具](/docs-cn/developer-guide/adding-tools)
6. [创建技能](/docs-cn/developer-guide/creating-skills)

:::tip
工具是代理可以调用的单个函数。技能是捆绑在一起的工具、提示和配置的集合。从工具开始，逐步过渡到技能。
:::

### "我想要训练模型"

使用强化学习和 Hermes Agent 的内置 RL 训练管道微调模型行为。

1. [快速入门](/docs-cn/getting-started/quickstart)
2. [配置](/docs-cn/user-guide/configuration)
3. [RL 训练](/docs-cn/user-guide/features/rl-training)
4. [提供者路由](/docs-cn/user-guide/features/provider-routing)
5. [架构](/docs-cn/developer-guide/architecture)

:::tip
当你已经了解 Hermes Agent 如何处理对话和工具调用的基础知识时，RL 训练效果最好。如果你是新手，请先运行初学者路径。
:::

### "我想把它用作 Python 库"

以编程方式将 Hermes Agent 集成到你自己的 Python 应用程序中。

1. [安装](/docs-cn/getting-started/installation)
2. [快速入门](/docs-cn/getting-started/quickstart)
3. [Python 库指南](/docs-cn/guides/python-library)
4. [架构](/docs-cn/developer-guide/architecture)
5. [工具](/docs-cn/user-guide/features/tools)
6. [会话](/docs-cn/user-guide/sessions)

## 核心功能一览

不确定有什么可用？这是主要功能的快速目录：

| 功能 | 作用 | 链接 |
|---|---|---|
| **工具** | 代理可以调用的内置工具（文件 I/O、搜索、shell 等） | [工具](/docs-cn/user-guide/features/tools) |
| **技能** | 添加新功能的可安装插件包 | [技能](/docs-cn/user-guide/features/skills) |
| **记忆** | 跨会话的持久化记忆 | [记忆](/docs-cn/user-guide/features/memory) |
| **上下文文件** | 将文件和目录馈送到对话中 | [上下文文件](/docs-cn/user-guide/features/context-files) |
| **MCP** | 通过模型上下文协议连接到外部工具服务器 | [MCP](/docs-cn/user-guide/features/mcp) |
| **Cron** | 安排重复的代理任务 | [Cron](/docs-cn/user-guide/features/cron) |
| **委托** | 生成子代理进行并行工作 | [委托](/docs-cn/user-guide/features/delegation) |
| **代码执行** | 运行以编程方式调用 Hermes 工具的 Python 脚本 | [代码执行](/docs-cn/user-guide/features/code-execution) |
| **浏览器** | 网页浏览和抓取 | [浏览器](/docs-cn/user-guide/features/browser) |
| **钩子** | 事件驱动的回调和中间件 | [钩子](/docs-cn/user-guide/features/hooks) |
| **批处理** | 批量处理多个输入 | [批处理](/docs-cn/user-guide/features/batch-processing) |
| **RL 训练** | 使用强化学习微调模型 | [RL 训练](/docs-cn/user-guide/features/rl-training) |
| **提供者路由** | 跨多个 LLM 提供者路由请求 | [提供者路由](/docs-cn/user-guide/features/provider-routing) |

## 接下来读什么

根据你现在的位置：

- **刚完成安装？** → 前往 [快速入门](/docs-cn/getting-started/quickstart) 运行你的第一次对话。
- **完成了快速入门？** → 阅读 [CLI 使用](/docs-cn/user-guide/cli) 和 [配置](/docs-cn/user-guide/configuration) 来自定义你的设置。
- **熟悉基础知识？** → 探索 [工具](/docs-cn/user-guide/features/tools)、[技能](/docs-cn/user-guide/features/skills) 和 [记忆](/docs-cn/user-guide/features/memory) 以解锁代理的全部功能。
- **为团队设置？** → 阅读 [安全性](/docs-cn/user-guide/security) 和 [会话](/docs-cn/user-guide/sessions) 以了解访问控制和对话管理。
- **准备构建？** → 跳入 [开发者指南](/docs-cn/developer-guide/architecture) 以了解内部结构并开始贡献。
- **想要实际示例？** → 查看 [指南](/docs-cn/guides/tips) 部分以获取真实世界的项目和提示。

:::tip
你不需要阅读所有内容。选择符合你目标的路径，按顺序跟随链接，你将很快提高工作效率。你可以随时回到此页面找到你的下一步。
:::

