---
title: "Architecture Study Decisions"
status: active
source_commit: dd0827710
verified_at: 2026-08-11
---

# 研究决策记录

这里记录研究过程本身的决策；产品架构决策在对应模块文档中另行说明。

## STUDY-001 — 使用独立长期文档分支

- 状态：accepted
- 日期：2026-08-11
- 决策：研究文档进入 `docs/hermes-architecture-deep-dive`，不与产品代码改动混合。
- 原因：研究跨多个会话且图文变更量大，需要稳定、可回滚的持久工作区。
- 后果：从研究中发现的代码修复必须另开实现分支。

## STUDY-002 — 使用分层恢复文档

- 状态：accepted
- 日期：2026-08-11
- 决策：自动入口只读取 `README.md`、`PROGRESS.md`、`HANDOFF.md` 和当前模块；旧 journal 不自动加载。
- 原因：既要跨会话恢复，又要避免把全部研究历史塞入每次上下文。
- 后果：每次会话结束必须维护最新交接。

## STUDY-003 — 图使用 Mermaid 源文本

- 状态：accepted
- 日期：2026-08-11
- 决策：默认把 Mermaid 嵌入 Markdown，不以 PNG/SVG 作为唯一来源。
- 原因：可审查 diff、易于版本化和持续修正。
- 例外：只有 Mermaid 无法表达或需要像素级说明时才增加生成图片。

## STUDY-004 — 按运行链路而非目录组织

- 状态：accepted
- 日期：2026-08-11
- 决策：先追 Canonical Turn，再按 Agent、Prompt、Tools、Memory、Skills 等模块深入。
- 原因：Hermes 的关键约束跨越多个目录，逐文件阅读会丢失生命周期和 ownership。

