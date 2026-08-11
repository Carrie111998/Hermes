---
title: "Hermes Architecture Glossary"
status: draft
source_commit: dd0827710
verified_at: 2026-08-11
---

# 术语表

| 术语 | 工作定义 |
|---|---|
| AIAgent | Hermes 的共享同步 Agent 编排核心；入口最终通过它或兼容运行时执行对话回合。 |
| Turn | 一次用户输入及其所有模型调用、工具迭代、最终响应和回合后处理。 |
| Iteration | Agent Loop 中一次 Provider 调用及其后续处理，不等同于用户回合。 |
| System prompt snapshot | 会话建立时冻结并持久化的系统提示词。 |
| `api_content` | 与干净 transcript content 并存的 API 重放 sidecar，保存实际上发送给 Provider 的字节。 |
| Context compression | 在上下文压力下压缩历史并维护 Session lineage 的机制。 |
| Tool | 模型可见的结构化 schema 与宿主 handler 的组合。 |
| Toolset | 一组工具或其他 Toolset 的命名组合。 |
| `check_fn` | 决定某工具/Toolset 是否在当前运行时暴露给模型的服务可用性检查。 |
| Memory | `MEMORY.md` 中精炼、常驻的环境事实、经验和状态。 |
| User profile | `USER.md` 中关于用户身份、偏好和交互方式的有界信息。 |
| Session archive | SQLite `state.db` 中的完整持久会话及 FTS5 索引。 |
| Skill | 以 `SKILL.md` 为入口、按需加载的程序性知识包，可带 references/templates/scripts。 |
| Background Review | 隔离 Agent fork 对近期对话进行 Memory/Skill 审查的回合后流程。 |
| Curator | 按使用状态维护技能生命周期，并可选执行 LLM consolidation 的后台维护系统。 |
| ProviderProfile | 模型供应商对 URL、API 模式、模型目录和消息适配行为的统一声明。 |
| Gateway | 将多消息平台事件映射到统一 Agent Session 和投递语义的长驻服务。 |
| Session key | Gateway 用于按平台、聊天、用户和线程定位活动 Session 的路由键。 |
| Profile | 独立的 Hermes home 岛，拥有自己的配置、凭据、Memory、Skills、Sessions 和 Gateway 状态。 |
| Narrow waist | 保持 Agent 核心和每次请求携带的模型工具面尽量小，把能力扩展放到边缘。 |
| Canonical store | 发生恢复或并发争议时作为权威事实来源的持久存储。 |

