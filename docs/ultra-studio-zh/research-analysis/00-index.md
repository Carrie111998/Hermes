文档路径：docs/ultra-studio-zh/research-analysis/00-index.md

# 调研分析总览

状态：调研分析中文详细版  
来源：`docs/ultra-studio-research-analysis/00-index.md` 与中文阅读层 `docs/ultra-studio-docs-zh/research-00-index.html`  
日期：2026-06-11

## 定位

最终调研分析包，把 P0 MVP、agent/tool/media 合同、安全边界、云基础设施、完整系统视角串起来。

本页把源文档转换成中文实施视角：先说明它解决的问题，再拆出当前结论、执行影响、验证方式和开放问题。它用于中文阅读和排期，英文源保留为核对材料。

## 核心结论

- 它回答“为什么这些设计这么切”，尤其是为什么先做薄 P0，同时保留后续扩展接口。
- P0 不做完整云平台，但必须保留 credential、job、asset、event、router 的接口。
- 把 research 里的 cloud infra 明确降级为 later，避免误进入 P0。
- 这是中文阅读层，原文仍然保留为核对来源。需要写代码或开 issue 时，先读本页，再回到原文看细节。

## 关键内容

### 这份文档解决什么

- 它回答“为什么这些设计这么切”，尤其是为什么先做薄 P0，同时保留后续扩展接口。
### P0 相关结论

- P0 不做完整云平台，但必须保留 credential、job、asset、event、router 的接口。
- 把 research 里的 cloud infra 明确降级为 later，避免误进入 P0。
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
| Verdict | 结论 |
| Result Documents | 产出文档 |
| Authoritative Sources | 权威来源 |
| Thread Results Incorporated | 已纳入的并行调研结果 |
| Current Truth | 当前真实状态 |
| Decision Log | 决策记录 |
| Completion Standard | 完成标准 |
