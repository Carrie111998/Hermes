# 文档网站地图

状态：站点导航与信息地图  
日期：2026-06-11

## 这个站点解决什么

这个站点把 Ultra Studio 的产品设计、组件规格、基建方案、调研结论和长期路线放到一个可搜索的 VitePress 文档网站里。以后优先读 Markdown 页面；构建时由 VitePress 生成正式 HTML。

## 推荐阅读顺序

| 顺序 | 页面 | 用途 |
|---|---|---|
| 1 | [可视化导读](visual-guide) | 用图先建立 P0 闭环、产品外壳、系统层级和阅读路线。 |
| 2 | [设计主线](00-design-spine) | 锁定产品边界、P0 闭环、当前状态和目标架构，不让文档散掉。 |
| 3 | [权限边界](permission-boundary-design) | 明确 Prompt、UI、Router、Policy、Asset、Worker 的授权边界。 |
| 4 | [完整建设图谱](architecture-blueprint) | 看控制面、执行面、数据面、安全运维和路线图如何连接。 |
| 5 | [产品规格包](product-specs/00-index) | 了解 Ultra Studio 要做成什么，不先陷入实现细节。 |
| 6 | [组件规格索引](product-specs/components/README) | 查每个 UI、服务、基建组件的功能、API、状态和验收。 |
| 7 | [基础设施设计总览](infra-design/00-index) | 看 Gateway、Sandbox、TokenRouter、CometAPI、数据面和安全边界。 |
| 8 | [Hermes Fork 隔离迁移](infra-design/08-hermes-fork-isolation-migration) | 把当前 fork 里的多租户鉴权、UI、文档和接入点拆成可迁移清单。 |
| 9 | [调研分析总览](research-analysis/00-index) | 理解为什么 P0 要薄做，哪些能力必须后置。 |
| 10 | [长期参考](long-term-reference) | 保留未来云化、多租户、市场、CometAPI 和安全运营目标。 |
| 11 | [源文档归档](source-archive/README) | 查所有旧文档、Notion/Lark 导出和可读原文镜像。 |

## 主要分区

| 分区 | 内容 | 什么时候读 |
|---|---|---|
| 设计主线 | 产品边界、P0 闭环、当前状态、目标架构、source of truth | 做任何新功能前先读 |
| 权限边界 | Principal、PolicyChecker、Asset ACL、Worker envelope、错误和审计 | 涉及工具、资产、额度、凭证或 worker 前先读 |
| 产品规格 | 产品界面、Agent 运行时、媒体资产、技能工具、Memory、Marketplace、交付计划 | 定义要做什么、P0/P1/P2 怎么切 |
| 组件规格 | 19 个组件的完整功能规格 | 分任务、开 issue、写代码前读 |
| 基建设计 | 控制面、执行面、数据面、安全运维、验证路线、Hermes fork 隔离迁移 | 防止 P0 写死，保留未来扩展边界 |
| 调研分析 | P0 垂直切片、安全凭证、云能力后置、迁移接口 | 判断优先级和取舍 |
| 独立专题 | 真实聊天 UI、Manus 差距、Skill/Tool/Prompt 规格 | 回答专项问题 |
| 图谱 | SVG 总图和长期路线图 | 给产品、设计、工程同步全局视角 |
| 源文档归档 | 站点外 217 个历史文件清单，可读原文镜像，raw JSON/PDF 登记 | 核对旧资料、迁移遗漏、追溯 Notion/Lark 来源 |

## 状态词

| 状态 | 含义 |
|---|---|
| 已实现 | 仓库里有真实代码或可运行路径。 |
| 部分实现 | 有相邻机制、原型或旧实现，但没有接入 Ultra Studio runtime。 |
| 已规定，未构建 | 文档已经定义契约，但没有运行时代码。 |
| P0 | 真实聊天 Agent、上传、Atlas 媒体任务、资产展示、历史恢复。 |
| P1 | 产品化资产库、角色/元素复用、市场、更多工作流。 |
| P2 | 云端多租户、TokenRouter、CometAPI、沙箱生命周期、协作与运营。 |

## 术语口径

| 读者用语 | 开发标识 | 意思 |
|---|---|---|
| 来源链路 | `lineage`, `asset_lineage` | 资产从哪里来，由哪个任务、模型、提示词、输入资产生成。 |
| 权限规则 | `acl`, `asset_acl` | 谁能读取、使用、更新、删除或撤销资产。 |
| 操作记录 | `audit`, `asset_audit_events` | 谁在什么时候做了什么，方便追查和恢复。 |
| 核对来源 | source docs | 中文整理层背后的英文源文档或旧文档。 |

## 维护规则

新增或移动文档时，至少更新三个地方：

1. [中文总入口](README)
2. 本页文档地图
3. VitePress 侧边栏配置：`.vitepress/config.mts`
4. 如果新增的是站点外源文件，同步更新 [源文档归档](source-archive/README)

如果新增的是架构图或视觉说明，还要更新 [可视化导读](visual-guide) 或 [完整建设图谱](architecture-blueprint)。
