---
sidebar_position: 3
title: "持久化记忆"
description: "Hermes Agent 如何跨会话记忆 — MEMORY.md、USER.md 和会话搜索"
---

# 持久化记忆

Hermes Agent 拥有有限且经过整理的跨会话持久化记忆功能。这让它能够记住你的偏好、项目、环境以及学到的知识。

## 工作原理

代理的记忆由两个文件组成：

| 文件 | 用途 | 字符限制 |
|------|---------|------------|
| **MEMORY.md** | 代理的个人笔记 — 环境事实、约定、学到的内容 | 2,200 字符（约 800 token） |
| **USER.md** | 用户档案 — 你的偏好、沟通风格、期望 | 1,375 字符（约 500 token） |

两者都存储在 `~/.hermes/memories/` 目录中，并在会话开始时作为冻结快照注入到系统提示中。代理通过 `memory` 工具管理自己的记忆 — 可以添加、替换或删除条目。

:::info
字符限制确保记忆保持聚焦。当记忆已满时，代理会合并或替换条目以为新信息腾出空间。
:::

## 记忆在系统提示中的呈现方式

在每个会话开始时，记忆条目从磁盘加载并渲染到系统提示中作为一个冻结块：

```
══════════════════════════════════════════════
MEMORY（你的个人笔记）[67% — 1,474/2,200 字符]
══════════════════════════════════════════════
用户的项目是一个位于 ~/code/myapi 的 Rust Web 服务，使用 Axum + SQLx
§
这台机器运行 Ubuntu 22.04，安装了 Docker 和 Podman
§
用户喜欢简洁的回复，不喜欢冗长的解释
```

格式包括：
- 显示存储类型（MEMORY 或 USER PROFILE）的标题
- 使用百分比和字符计数，让代理了解容量
- 用 `§`（段落符号）分隔符分隔的各个条目
- 条目可以是多行的

**冻结快照模式：** 系统提示注入在会话开始时捕获一次，会话期间永不改变。这是有意为之的 — 它保留了 LLM 的前缀缓存以提高性能。当代理在会话期间添加/删除记忆条目时，更改会立即持久化到磁盘，但直到下一个会话开始才会出现在系统提示中。工具响应始终显示实时状态。

## 记忆工具操作

代理使用带有以下操作的 `memory` 工具：

- **add** — 添加新的记忆条目
- **replace** — 用更新的内容替换现有条目（通过 `old_text` 使用子字符串匹配）
- **remove** — 删除不再相关的条目（通过 `old_text` 使用子字符串匹配）

没有 `read` 操作 — 记忆内容会在会话开始时自动注入到系统提示中。代理将其记忆视为对话上下文的一部分。

### 子字符串匹配

`replace` 和 `remove` 操作使用短唯一子字符串匹配 — 你不需要完整的条文本。`old_text` 参数只需要是一个能唯一标识一个条目的子字符串：

```python
# 如果记忆包含 "User prefers dark mode in all editors"
memory(action="replace", target="memory",
       old_text="dark mode",
       content="User prefers light mode in VS Code, dark mode in terminal")
```

如果子字符串匹配多个条目，将返回错误，要求提供更具体的匹配。

## 两个目标详解

### `memory` — 代理的个人笔记

用于代理需要记住的有关环境、工作流程和经验教训的信息：

- 环境事实（操作系统、工具、项目结构）
- 项目约定和配置
- 发现的工具特性和变通方法
- 已完成的任务日记条目
- 有效的技能和技巧

### `user` — 用户档案

用于有关用户身份、偏好和沟通风格的信息：

- 姓名、角色、时区
- 沟通偏好（简洁 vs 详细、格式偏好）
- 讨厌的事项和需要避免的事情
- 工作流习惯
- 技术技能水平

## 应该保存什么 vs 跳过什么

### 这些要保存（主动保存）

代理会自动保存 — 你不需要询问。它在学到以下内容时会保存：

- **用户偏好：** "我更喜欢 TypeScript 而不是 JavaScript" → 保存到 `user`
- **环境事实：** "这台服务器运行 Debian 12 和 PostgreSQL 16" → 保存到 `memory`
- **纠正：** "Docker 命令不要使用 `sudo`，用户在 docker 组中" → 保存到 `memory`
- **约定：** "项目使用制表符，120 字符行宽，Google 风格文档字符串" → 保存到 `memory`
- **已完成的工作：** "于 2026-01-15 将数据库从 MySQL 迁移到 PostgreSQL" → 保存到 `memory`
- **明确请求：** "记住我的 API 密钥每月轮换" → 保存到 `memory`

### 这些跳过

- **琐碎/明显的信息：** "用户询问了 Python" — 太模糊，没有用处
- **容易重新发现的事实：** "Python 3.12 支持 f-string 嵌套" — 可以通过网络搜索找到
- **原始数据转储：** 大型代码块、日志文件、数据表 — 对记忆来说太大
- **会话特定的临时信息：** 临时文件路径、一次性调试上下文
- 已在上下文文件中的信息：SOUL.md 和 AGENTS.md 内容

## 容量管理

记忆有严格的字符限制以保持系统提示的大小可控：

| 存储 | 限制 | 典型条目数 |
|-------|-------|----------------|
| memory | 2,200 字符 | 8-15 个条目 |
| user | 1,375 字符 | 5-10 个条目 |

### 记忆已满时会发生什么

当你尝试添加会超出限制的条目时，工具会返回错误：

```json
{
  "success": false,
  "error": "Memory at 2,100/2,200 chars. Adding this entry (250 chars) would exceed the limit. Replace or remove existing entries first.",
  "current_entries": ["..."],
  "usage": "2,100/2,200"
}
```

然后代理应该：
1. 读取当前条目（在错误响应中显示）
2. 识别可以删除或合并的条目
3. 使用 `replace` 将相关条目合并为更短的版本
4. 然后 `add` 新条目

**最佳实践：** 当记忆超过 80% 容量时（在系统提示标题中可见），在添加新条目之前先合并条目。例如，将三个单独的 "project uses X" 条目合并为一个全面的项目描述条目。

### 良好记忆条目的实际示例

**紧凑、信息密集的条目效果最好：**

```
# 好：打包多个相关事实
User runs macOS 14 Sonoma, uses Homebrew, has Docker Desktop and Podman. Shell: zsh with oh-my-zsh. Editor: VS Code with Vim keybindings.

# 好：具体、可操作的约定
Project ~/code/api uses Go 1.22, sqlc for DB queries, chi router. Run tests with 'make test'. CI via GitHub Actions.

# 好：带上下文的经验教训
The staging server (10.0.1.50) needs SSH port 2222, not 22. Key is at ~/.ssh/staging_ed25519.

# 坏：太模糊
User has a project.

# 坏：太冗长
On January 5th, 2026, the user asked me to look at their project which is
located at ~/code/api. I discovered it uses Go version 1.22 and...
```

## 重复预防

记忆系统会自动拒绝完全重复的条目。如果你尝试添加已存在的内容，它会返回成功并显示 "no duplicate added" 消息。

## 安全扫描

记忆条目在被接受之前会扫描注入和泄露模式，因为它们会被注入到系统提示中。匹配威胁模式（提示注入、凭证泄露、SSH 后门）或包含不可见 Unicode 字符的内容会被阻止。

## 会话搜索

除了 MEMORY.md 和 USER.md，代理还可以使用 `session_search` 工具搜索其过去的对话：

- 所有 CLI 和消息传递会话都存储在 SQLite（`~/.hermes/state.db`）中，支持 FTS5 全文搜索
- 搜索查询返回相关的过去对话，并使用 Gemini Flash 进行摘要
- 代理可以找到几周前讨论过的内容，即使这些内容不在其活动记忆中

```bash
hermes sessions list    # 浏览过去的会话
```

### session_search vs memory

| 特性 | 持久化记忆 | 会话搜索 |
|---------|------------------|----------------|
| **容量** | 总共约 1,300 token | 无限制（所有会话） |
| **速度** | 即时（在系统提示中） | 需要搜索 + LLM 摘要 |
| **用例** | 关键事实在上下文中始终可用 | 查找特定的过去对话 |
| **管理** | 由代理手动整理 | 自动 — 存储所有会话 |
| **Token 成本** | 每个会话固定（约 1,300 token） | 按需（需要时搜索） |

**记忆**适用于应该始终在上下文中的关键事实。**会话搜索**适用于"我们上周讨论过 X 吗？"这类查询，代理需要从过去的对话中回忆具体内容。

## 配置

```yaml
# 在 ~/.hermes/config.yaml 中
memory:
  memory_enabled: true
  user_profile_enabled: true
  memory_char_limit: 2200   # 约 800 token
  user_char_limit: 1375     # 约 500 token
```

## 外部记忆提供者

对于超越 MEMORY.md 和 USER.md 的更深层次的持久化记忆，Hermes 附带了 8 个外部记忆提供者插件 — 包括 Honcho、OpenViking、Mem0、Hindsight、Holographic、RetainDB、ByteRover 和 Supermemory。

外部提供者与内置记忆**并行运行**（从不替代它），并添加了知识图谱、语义搜索、自动事实提取和跨会话用户建模等功能。

```bash
hermes memory setup      # 选择一个提供者并配置它
hermes memory status     # 检查哪些处于活动状态
```

查看 [记忆提供者](./memory-providers.md) 指南以获取每个提供者的完整详细信息、设置说明和比较。

