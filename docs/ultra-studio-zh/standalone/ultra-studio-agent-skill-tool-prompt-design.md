文档路径：docs/ultra-studio-zh/standalone/ultra-studio-agent-skill-tool-prompt-design.md

# 完整 Skill / Tool / Prompt 实施规格

状态：独立专题中文详细版  
来源：`docs/ultra-studio-agent-skill-tool-prompt-design.md` 与中文阅读层 `docs/ultra-studio-docs-zh/standalone-skill-tool-prompt.html`  
日期：2026-06-11

## 定位

把 fork 改造成 Atlas-first 视频创作 agent 的完整实施规格。

本页把源文档转换成中文实施视角：先说明它解决的问题，再拆出当前结论、执行影响、验证方式和开放问题。它用于中文阅读和排期，英文源保留为核对材料。

## 核心结论

- 这是中文阅读层，原文仍然保留为核对来源。需要写代码或开 issue 时，先读本页，再回到原文看细节。

## 关键内容

### 这份文档解决什么

- 这份文档最接近“怎么改代码”。它覆盖 skill 裁剪、workflow-router、Atlas tools、prompt templates 和 disable-first/delete-later 策略。
### P0 相关结论

- 可见技能目录收窄到视频/图片创作。
- 明确禁止 FAL、ComfyUI 等非 Atlas 后端作为默认路径。
- 上传媒体变 typed assets。
### 阅读位置

- 这是中文阅读层，原文仍然保留为核对来源。需要写代码或开 issue 时，先读本页，再回到原文看细节。

## 对实现的影响

- P0 只做能跑通真实链路的最小闭环；没有真实 runtime 的能力必须标记为 spec-only 或 blocked。
- 所有跨组件状态都要有明确 owner：Session、MediaJob、Asset、Skill、TokenRouter、CometAPI、Memory 不能相互复制状态。
- 上层 UI 只能展示真实 API、事件和持久层返回的内容，不能用静态 demo 替代产品能力。
- 设计应保留未来云端、多租户、市场、浏览器上下文和操作记录链路的接口边界，但不要在 P0 过早实现复杂平台。

## 验证方式

| 层面 | 验证 |
|---|---|
| 文档 | 能找到明确 owner、状态、P0/P1 切分和开放问题。 |
| 代码 | `rg` 能定位真实实现；spec-only 能明确说明没有代码。 |
| UI | 用户看得到加载、进度、失败、完成和历史恢复。 |
| 数据 | 任务、资产、事件、操作记录 id 可串起来。 |

## 与相邻文档的关系

- 产品规格定义用户界面和 runtime 合约。
- 组件规格把每个界面/服务拆成可实施功能。
- 基建设计定义 Gateway、Sandbox、Data Plane、Security、TokenRouter、CometAPI 的长期边界。
- 调研分析决定 P0 应该做什么、什么必须推迟。

## 开放问题

- 当前文档中的 spec-only 能力是否需要先建最小接口，还是只保留文档边界？
- 哪些能力必须进 P0 验收，哪些只保留到 P1/P2？
- 未来 cloud 版本需要保留的字段，是否会污染本地 MVP 的实现复杂度？

## 与英文源文档的结构对应

| 英文章节 | 中文章节 |
|---|---|
| Objective | 目标 |
| Done When | 完成条件 |
| Non-Goals | 非目标 |
| Evidence | 证据 |
| Notion Alignment | Notion 对齐 |
| Source Of Truth Map | 真实来源地图 |
| Chosen Architecture | 已选架构 |
| Atlas API Contract | Atlas API 合约 |
| Skill Catalog Target | Skill 目录目标 |
| Progressive Disclosure Strategy | 渐进式披露策略 |
| Skill Package Structure | Skill 包结构 |
| Prompt Design | Prompt 设计 |
| Disabling and Deletion Plan | 禁用与删除计划 |
| Implementation Roadmap | 实现路线图 |
| Validation Matrix | 验证矩阵 |
| Main Risks | 主要风险 |
| Decision | 决策 |
