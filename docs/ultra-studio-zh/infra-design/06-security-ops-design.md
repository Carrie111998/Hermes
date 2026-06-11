文档路径：docs/ultra-studio-zh/infra-design/06-security-ops-design.md

# 安全与运维设计

状态：基建设计中文详细版  
来源：`docs/ultra-studio-infra-design/06-security-ops-design.md` 与中文阅读层 `docs/ultra-studio-docs-zh/infra-06-security-ops-design.html`  
日期：2026-06-11

## 定位

覆盖 zero-trust isolation、guardrails、egress、操作记录、observability、service mesh、GitOps、incident checks。

本页把源文档转换成中文实施视角：先说明它解决的问题，再拆出当前结论、执行影响、验证方式和开放问题。它用于中文阅读和排期，英文源保留为核对材料。

## 核心结论

- 云 creative agent 一旦能访问浏览器、文件、API key、媒体资产，就必须有操作记录和隔离。
- P0 先保证 secret 不进前端/日志，工具错误不静默降级。
- 这是中文阅读层，原文仍然保留为核对来源。需要写代码或开 issue 时，先读本页，再回到原文看细节。

## 关键内容

### 这份文档解决什么

- 云 creative agent 一旦能访问浏览器、文件、API key、媒体资产，就必须有操作记录和隔离。
### P0 相关结论

- P0 先保证 secret 不进前端/日志，工具错误不静默降级。
- 云化前补 egress、操作记录、observability、policy tests。
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
| Security Objective | 安全目标 |
| Trust Boundaries | 信任边界 |
| Secrets Policy | 密钥策略 |
| Egress Policy | 出口策略 |
| Guardrails and Exfiltration Defense | 护栏与防泄漏 |
| Service Mesh / Internal mTLS | 服务网格 / 内部 mTLS |
| Observability | 可观测性 |
| 操作记录事件（Audit Events） | 操作记录事件 |
| GitOps and Secrets Delivery | GitOps 与密钥交付 |
| Operational Runbooks | 运维 Runbook |
| Security Tests | 安全测试 |
