# Ultra Studio 产品规格包

状态：working specification pack（进行中规格包）  
日期：2026-06-10  
范围：基于 Hermes 分支构建、以 Atlas 为优先的创意智能体 Ultra Studio。

## 为什么这不是一份 PRD

Ultra Studio 不是一个单一功能。它是产品界面、智能体运行时、媒体任务系统、资产库、记忆层、市集和技能运行时的协同整体。

单份 PRD 会掩盖重要的边界。本规格包按决策面拆分工作，以便工程、设计和产品团队无需阅读全部内容即可审阅正确的文档。

## 文档地图

| 文档 | 用途 | 主要读者 |
|---|---|---|
| [产品界面](01-product-surface) | 产品外壳、用户任务、左侧导航、聊天、检查器。 | 产品 + 设计 |
| [Agent 运行时合约](02-agent-runtime-contract) | 会话、网关、沙盒、事件流、审批生命周期。 | 后端 + 智能体运行时 |
| [媒体与资产合约](03-media-asset-contract) | 媒体任务、资产、来源链路、下载、质检、角色/元素。 | 后端 + 前端 |
| [技能、工具与提示词合约](04-skill-tool-prompt-contract) | 工作流路由器、技能、工具、提示词编译器、澄清规则。 | 智能体 + 工作流工程 |
| [记忆 / 应用市场 / 文件](05-memory-marketplace-files) | 记忆、市集、文件、任务文件系统、技能/模板目录。 | 产品 + 平台 |
| [交付计划](06-delivery-plan) | 里程碑、P0/P1/P2、验收检查、发布关口。 | 全员 |
| [组件规格索引](components/README) | 各组件完整功能规格（19 个组件）。 | 工程 |
| [可视化导读](../visual-guide) | 四张图解释 P0 闭环、三栏界面、系统分层和阅读路线。 | 全员 |
| [中文详细文档总入口](../README) | 连接产品规格、组件、调研、基建和专题。 | 产品 + 工程 |
| [信息保留与上线说明](../preservation-and-deploy) | 说明 Markdown 主源、图谱资源、构建输出和部署方式。 | 产品 + 工程 |

## 源参考

使用以下作为当前源材料。VitePress 站点优先链接中文详细版；少数旧版源文件保留为仓库内核对路径，不作为站点路由。

- [Ultra Studio 完整建设图谱](../architecture-blueprint)
- [Ultra Studio 完整长期参考](../long-term-reference)
- [最终调研分析包中文详细版](../research-analysis/00-index)
- [基础设施设计中文详细版](../infra-design/00-index)
- [研究附录与开放问题](../research-analysis/90-research-appendix-open-questions)
- [Manus 缺口调研中文详细版](../standalone/ultra-studio-agent-manus-gap-research)
- [技能/工具/提示词规格中文详细版](../standalone/ultra-studio-agent-skill-tool-prompt-design)
- [真实聊天智能体 UI 契约中文详细版](../standalone/hermes-real-chat-agent-ui)
- [资产服务组件规格](components/09-asset-service)
- [TokenRouter 组件规格](components/17-tokenrouter)
- [CometAPI 媒体网关组件规格](components/18-cometapi-media-gateway)

## 产品形态

Ultra Studio 是一台创意任务计算机：

```text
left nav shell
  + creative chat
  + inspector/live panel
  + sandbox/task filesystem
  + browser contexts
  + durable media jobs
  + human approvals
  + skill registry
  + provider constraints
  + artifact/provenance ledgers
  + asset library
```

## 顶层验收标准

当满足以下条件时，产品达到可用状态：

- 用户可以从网页聊天启动创意会话。
- 系统能够将请求路由到正确的技能，或询问一个有价值的缺失字段问题。
- 上传的媒体成为类型化资产，而非纯提示词文本。
- Atlas 图像/视频任务创建真实的任务记录和最终资产。
- UI 流式传输思考/状态/工具/媒体事件，而非冻结。
- 检查器显示当前任务、选中资产、质检证据、下载、元素创建和角色创建。
- 记忆、市集、文件和任务作为一级导航界面存在。
- 智能体无法在没有产物、观察记录或账本记录的情况下声称完成。

## 本规格包范围之外

- 上游 Hermes 贡献策略。
- 定价页面文案。
- 公开营销站点。
- Atlas 之外的提供商接入，除非用作约束研究。
- 虚假演示流程。
