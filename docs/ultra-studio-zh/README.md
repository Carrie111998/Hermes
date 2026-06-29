文档路径：docs/ultra-studio-zh/README.md

# Ultra Studio 中文详细文档总入口

状态：中文详细版入口  
日期：2026-06-11

## 怎么读

这是把 Ultra Studio / Hermes Agent 相关产品、组件、调研和基建设计串起来的中文详细文档包。优先从这里阅读；需要核对英文原文时，再回到对应源文档。

- [先看可视化导读](visual-guide)：四张图解释 P0 闭环、三栏界面、系统分层和阅读路线。
- [设计主线](00-design-spine)：锁定产品边界、P0 闭环、当前状态、目标架构和 source of truth。
- [当前实现地图](implementation-map)：把当前代码事实、spec-only 缺口和下一步 P0 连接点放到同一张表里。
- [权限边界与零信任执行设计](permission-boundary-design)：定义 Prompt、UI、Router、Policy、Asset、Worker 的授权边界。
- [文档网站地图](site-map)：说明每个分区解决什么问题，以及推荐阅读顺序。
- [完整建设图谱](architecture-blueprint)：完整系统总图、控制面、执行面、数据面、安全运维和路线图。
- [完整长期参考](long-term-reference)：把 TokenRouter、CometAPI、Sandbox lifecycle、Asset Service、Memory、Marketplace、Ledger 和 Cloud tenant layer 放到同一张长期地图里。
- [源文档归档](source-archive/README)：列出站点外所有历史文档、Notion/Lark 导出、旧版 HTML 和专题 PRD，并保留可读原文镜像。
- [信息保留与上线说明](preservation-and-deploy)：说明 Markdown 主源、图谱资源、构建输出和部署方式。
- 产品规格：定义 Ultra Studio 要做成什么。
- 组件规格：定义每个 UI/服务/基建组件的功能、状态、API、数据和验收。
- 调研分析：定义 P0 做什么、哪些能力推迟。
- 基建设计：定义 Gateway、Sandbox、TokenRouter、CometAPI、数据面、安全运营等长期边界。
- 独立专题：补充 Skill/Tool/Prompt、Manus 差距、真实聊天 UI。

## 站点入口

- [设计主线](00-design-spine)
- [当前实现地图](implementation-map)
- [权限边界与零信任执行设计](permission-boundary-design)
- [文档网站地图](site-map)
- [源文档归档总览](source-archive/README)
- [完整源文档清单](source-archive/inventory)
- [信息保留与上线说明](preservation-and-deploy)

## 产品规格

- [Ultra Studio 产品规格包](product-specs/00-index)
- [Ultra Studio 产品界面](product-specs/01-product-surface)
- [Agent Runtime Contract](product-specs/02-agent-runtime-contract)
- [媒体与资产合约](product-specs/03-media-asset-contract)
- [技能、工具与提示词合约](product-specs/04-skill-tool-prompt-contract)
- [Memory、Marketplace 和 Files](product-specs/05-memory-marketplace-files)
- [Ultra Studio 交付计划](product-specs/06-delivery-plan)

## 组件规格

- [01 左侧导航外壳](product-specs/components/01-left-nav-shell)
- [02 创作聊天界面](product-specs/components/02-creative-chat-ui)
- [03 右侧检查器 / 实时面板](product-specs/components/03-inspector-live-panel)
- [Marketplace](product-specs/components/04-marketplace)
- [Memory](product-specs/components/05-memory)
- [06 文件 / 任务文件浏览器](product-specs/components/06-files-task-file-browser)
- [任务 / 会话历史](product-specs/components/07-tasks-session-history)
- [Asset Library UI](product-specs/components/08-asset-library-ui)
- [资产服务](product-specs/components/09-asset-service)
- [Media Job Service](product-specs/components/10-media-job-service)
- [技能注册表](product-specs/components/11-skill-registry)
- [12 工作流路由器](product-specs/components/12-workflow-router)
- [提示词编译器（Prompt Compiler）](product-specs/components/13-prompt-compiler)
- [14 沙箱生命周期](product-specs/components/14-sandbox-lifecycle)
- [15 人工审批网关](product-specs/components/15-human-approval-gateway)
- [16 观察与溯源账本](product-specs/components/16-observation-provenance-ledger)
- [17 TokenRouter 凭证与额度路由](product-specs/components/17-tokenrouter)
- [18 CometAPI 媒体网关](product-specs/components/18-cometapi-media-gateway)
- [19 模型目录与供应商约束](product-specs/components/19-model-catalog-provider-constraints)
- [Ultra Studio 组件中文详细规格索引](product-specs/components/README)

## 调研分析

- [调研分析总览](research-analysis/00-index)
- [P0 MVP 垂直切片](research-analysis/01-p0-mvp-vertical-slice)
- [P0 Agent / Skill / Tool / Media 合同](research-analysis/02-p0-agent-skill-tool-media-contracts)
- [P0 安全与凭证边界](research-analysis/03-p0-security-credential-boundaries)
- [后续云基础设施路线](research-analysis/04-later-cloud-infra-roadmap)
- [完整系统视角](research-analysis/05-complete-system-perspective)
- [扩展接口与迁移计划](research-analysis/06-extension-seams-migration-plan)
- [研究附录与开放问题](research-analysis/90-research-appendix-open-questions)

## 基建设计

- [基础设施设计总览](infra-design/00-index)
- [基建参考调研](infra-design/01-reference-research)
- [基础设施边界图](infra-design/02-boundary-map)
- [控制面设计](infra-design/03-control-plane-design)
- [执行面设计](infra-design/04-execution-plane-design)
- [数据面设计](infra-design/05-data-plane-design)
- [安全与运维设计](infra-design/06-security-ops-design)
- [基础设施验证路线](infra-design/07-validation-roadmap)
- [Hermes Fork 隔离与多租户控制面迁移](infra-design/08-hermes-fork-isolation-migration)

## 独立专题

- [真实聊天 Agent UI 合同](standalone/hermes-real-chat-agent-ui)
- [Manus 差距调研](standalone/ultra-studio-agent-manus-gap-research)
- [完整 Skill / Tool / Prompt 实施规格](standalone/ultra-studio-agent-skill-tool-prompt-design)

## 状态规则

- 已实现：仓库里有真实代码或可运行路径。
- 部分实现：有相邻机制或原型，但没有接到 Ultra Studio runtime。
- Spec-only：只有设计文档，没有运行时代码。
- P0：真实聊天 Agent + 上传 + Atlas 媒体任务 + 资产展示 + 历史恢复。
- P1/P2：市场、云隔离、TokenRouter、CometAPI、持久浏览器、协作和运营级能力。

## VitePress 文档站

这组 Markdown 可以直接作为 VitePress 文档站运行：

```bash
cd docs/ultra-studio-zh
npm install
npm run docs:dev
```

构建静态站点：

```bash
npm run docs:build
npm run docs:preview
```
