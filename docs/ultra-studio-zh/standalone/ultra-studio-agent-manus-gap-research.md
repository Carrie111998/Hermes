文档路径：docs/ultra-studio-zh/standalone/ultra-studio-agent-manus-gap-research.md

# Manus 差距调研

状态：独立专题中文详细版  
来源：`docs/ultra-studio-agent-manus-gap-research.md` 与中文阅读层 `docs/ultra-studio-docs-zh/standalone-manus-gap.html`  
日期：2026-06-11

## 定位

比较 Ultra Studio/Hermes fork 与 Manus-style general agent 的差距。

本页把源文档转换成中文实施视角：先说明它解决的问题，再拆出当前结论、执行影响、验证方式和开放问题。它用于中文阅读和排期，英文源保留为核对材料。

## 核心结论

- Ultra Studio 需要 stronger task-computer infra，但 P0 不必一次性实现全部。
- 这是中文阅读层，原文仍然保留为核对来源。需要写代码或开 issue 时，先读本页，再回到原文看细节。

## 关键内容

### 这份文档解决什么

- Manus 的核心不是模型，而是任务计算机：sandbox、browser、filesystem、artifact、long-running lifecycle、skills、human takeover。
### P0 相关结论

- Ultra Studio 需要 stronger task-computer infra，但 P0 不必一次性实现全部。
- 优先补 artifact browser、task files、durable media jobs。
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
| Sources Checked | 已检查来源 |
| Core Finding | 核心发现 |
| Gap 1: Sandbox Lifecycle Is Too Shallow | Gap 1: Sandbox Lifecycle Is Too Shallow |
| Gap 2: Artifact Browser / Task File System | Gap 2: Artifact Browser / Task File System |
| Gap 3: Cloud Browser and Local Browser Split | Gap 3: Cloud Browser and Local Browser Split |
| Gap 4: Local Desktop Bridge | Gap 4: Local Desktop Bridge |
| Gap 5: Skill Progressive Disclosure and Skill Packaging | Gap 5: Skill Progressive Disclosure and Skill Packaging |
| Gap 6: Human Approval / Takeover Controls | Gap 6: Human Approval / Takeover Controls |
| Gap 7: Sharing and Collaboration Privacy | Gap 7: Sharing and Collaboration Privacy |
| Gap 8: Provenance and Anti-Fake Output | Gap 8: Provenance and Anti-Fake Output |
| What This Adds to the Current Architecture | 对当前架构的补充 |
| Recommended Build Order | 建议建设顺序 |
| Bottom Line | 底线结论 |
| Round 2: Broader Agent Infrastructure Research | Round 2: Broader Agent Infrastructure Research |
| New Finding A: Sandbox Should Be a Product Primitive, Not an Implementation Detail | New Finding A: Sandbox Should Be a Product Primitive, Not an Implementation Detail |
| New Finding B: Browser Sessions Need Persistent Contexts | New Finding B: Browser Sessions Need Persistent Contexts |
| New Finding C: Computer Use Requires Stronger Safety Than Tool Calling | New Finding C: Computer Use Requires Stronger Safety Than Tool Calling |
| New Finding D: Durable Execution Is the Missing Backbone for Long Media Jobs | New Finding D: Durable Execution Is the Missing Backbone for Long Media Jobs |
| New Finding E: Media APIs Converge on Queue + Status + Webhook + Upload | New Finding E: Media APIs Converge on Queue + Status + Webhook + Upload |
| New Finding F: Skill Standards Require Registries, Not Just Folders | New Finding F: Skill Standards Require Registries, Not Just Folders |
| New Finding G: Browser/Computer Agents Need Observation Loops | New Finding G: Browser/Computer Agents Need Observation Loops |
| Revised Architecture Additions | 修订后的架构补充 |
| Revised P0 Build Order | 修订后的 P0 建设顺序 |
| Revised P1 Build Order | 修订后的 P1 建设顺序 |
| Design Principle After Round 2 | 第二轮后的设计原则 |
