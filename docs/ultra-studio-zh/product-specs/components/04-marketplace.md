# 04 应用市场（Marketplace）

状态：仅规格阶段（spec-only）—— 作为产品界面，尚无应用市场页面、路由或目录。  
相邻机制已经存在：技能安装/启用（skill install/enable）、插件清单（plugin manifests）、技能中心（Skills Hub）等可作为后续实现基础。  
日期：2026-06-11

来源：

- 文档：`docs/ultra-studio-product-specs/01-product-surface.md`（§Information Architecture，§Left Nav Shell）、`05-memory-marketplace-files.md`（§Marketplace，§Search，§Access Control，§Acceptance）、`04-skill-tool-prompt-contract.md`（§Required Skill Runtime Objects，§Required Creative Skills）、`06-delivery-plan.md`（P1 item 8，P3 item 2）、`docs/ultra-studio-agent-skill-tool-prompt-design.md`（§Skill Catalog Target，§Final Visible Catalog）
- 代码（相邻机制，本次会话已验证）：`tools/skills_hub.py`、`tools/skill_manager_tool.py`、`tools/skills_tool.py`、`tools/skills_sync.py`、`tools/skills_guard.py`、`agent/skill_bundles.py`、`web/src/pages/SkillsPage.tsx`、`web/src/pages/PluginsPage.tsx`、`skills/`（分类目录）、`optional-skills/`

## 目的与范围

应用市场（Marketplace）是可复用创意能力的目录展示层：工作流技能、提示词配方、故事板模板、模型配方、可复用元素、角色包和项目模板（`05-memory-marketplace-files.md` §Marketplace）。它回答"这个产品能为我做什么"，而无需用户知晓技能名称。

根据同一规格，应用市场"在首个版本中并非公共应用商店。它可以先作为一个由已签入技能元数据和精选模板支持的本地目录启动。"发布流程明确推迟至 P3（`06-delivery-plan.md` P3 item 2）。

本规格涵盖目录模型、条目生命周期、浏览/安装/启用行为，以及与技能注册表（`11-skill-registry.md`）的边界划分：技能注册表负责运行时发现、加载和许可名单强制执行；应用市场负责面向用户的目录、展示和获取流程。

## 实现状态

| 状态 | 条目 | 引用 |
|---|---|---|
| 已实现，相邻能力（Implemented adjacent） | 技能包扫描/列表/保存/删除及斜杠命令解析 | `agent/skill_bundles.py`（`scan_bundles`、`list_bundles`、`save_bundle`、`delete_bundle`、`resolve_bundle_command_key`） |
| 已实现，相邻能力（Implemented adjacent） | Skills Hub 元数据模型及带锁定安装路径的受保护远程获取 | `tools/skills_hub.py`（`SkillMeta`、`SkillBundle`、`_normalize_lock_install_path`、`_guarded_http_get`） |
| 已实现，相邻能力（Implemented adjacent） | 面向代理的技能安装/管理工具表面 | `tools/skill_manager_tool.py`、`tools/skills_tool.py`、`tools/skills_sync.py` |
| 已实现，相邻能力（Implemented adjacent） | 获取时的技能安全门控 | `tools/skills_guard.py`、`tools/skills_ast_audit.py`、`tools/skill_provenance.py` |
| 已实现，相邻能力（Implemented adjacent） | 列出技能与插件并支持启用/禁用的仪表板页面 | `web/src/pages/SkillsPage.tsx`、`web/src/pages/PluginsPage.tsx` |
| 已实现，相邻能力（Implemented adjacent） | 按类别组织的已签入技能库 | `skills/`（例如 `skills/creative/`）、`optional-skills/` |
| 已规定，未构建（Specified, not built） | 应用市场导航入口及可浏览目录页面 | `01-product-surface.md` §Left Nav Shell；`web/src`、`agent/`、`plugins/`、`gateway/` 中零 `marketplace` 命中（rg，本次会话） |
| 已规定，未构建（Specified, not built） | 应用市场条目封装包（`kind`、`inputs_schema`、`provider_constraints`、`status`） | `05-memory-marketplace-files.md` §Marketplace |
| 已规定，未构建（Specified, not built） | 非技能条目类型：配方、模板、元素包、角色包 | `05-memory-marketplace-files.md` §Marketplace |
| 已规定，未构建（Specified, not built） | 应用市场搜索集成与类型化结果卡片 | `05-memory-marketplace-files.md` §Search |
| 已规定，未构建（Specified, not built） | 应用市场本地目录里程碑 | `06-delivery-plan.md` P1 item 8 |
| 已规定，未构建（Specified, not built） | 发布流程 | `06-delivery-plan.md` P3 item 2 |

## 用户入口点

- 左侧导航中的 `Marketplace` 入口（计划中；见 `01-left-nav-shell.md`）。
- 跨表面搜索以类型化卡片形式返回应用市场条目（计划中，`05-memory-marketplace-files.md` §Search）。
- 路由回退：当 `workflow-router` 未找到匹配工作流时，可将用户指向最近可用工作流的应用市场入口（计划中；路由合约见 `12-workflow-router.md`）。
- 当前最接近的入口点：仪表板技能页面（`web/src/pages/SkillsPage.tsx`）和插件页面（`web/src/pages/PluginsPage.tsx`），它们列出并切换能力，但面向管理而非目录。

## 功能列表

| 功能 | 状态 |
|---|---|
| 按类别和类型分组浏览目录 | 已规划（Planned） |
| 条目详情视图：描述、输入模式、输出类型、必需工具、提供商约束、版本 | 已规划（Planned） |
| 按工作区安装/启用/禁用条目 | 已规划（Planned）（技能级启用/禁用机制已存在：`tools/skill_manager_tool.py`、`web/src/pages/SkillsPage.tsx`） |
| 显示每个条目的 `installed / available / disabled / deprecated`（已安装/可用/已禁用/已弃用）状态 | 已规划（Planned） |
| 由已签入技能元数据支持的本地目录 | 已规划（Planned）（元数据源已存在：`agent/skill_bundles.py`、`skills/`） |
| 精选模板条目（故事板、项目模板） | 已规划（Planned）；代码中不存在模板注册表 |
| 作为目录条目的元素包/角色包 | 已规划（Planned）；依赖资源服务引用（`09-asset-service.md`） |
| 作为目录条目的模型配方 | 已规划（Planned）；依赖模型目录（`19-model-catalog-provider-constraints.md`） |
| 可见但未启用的条目 | 已规划（Planned）（`05-memory-marketplace-files.md` §Access Control） |
| 使用类型化结果卡片搜索应用市场条目 | 已规划（Planned） |
| 发布用户创作的条目 | 已规划（Planned），仅 P3 |
| 获取时的安全审查（来源、AST 检查） | 已实现（Implemented），针对技能（`tools/skills_guard.py`、`tools/skills_ast_audit.py`）；尚未连接至应用市场 UI |

## 状态机

条目状态，依据 `05-memory-marketplace-files.md` §Marketplace：

```text
available -> installed -> disabled -> installed
installed -> deprecated (catalog owner action)
available -> deprecated
```

- `available`：在目录中可见，未在任何运行时配置文件中激活。
- `installed`：已获取到工作区；Skill Registry 仍可能将其从活动配置文件中过滤掉（参见 `11-skill-registry.md`）。
- `disabled`：已获取但显式关闭；必须保持可见（`05-memory-marketplace-files.md` §Access Control："Marketplace items can be visible without being enabled"）。
- `deprecated`：保持可检查以供溯源；无法新安装。

状态转换除 `deprecated` 外均为用户操作，`deprecated` 是目录所有者操作。不允许自动安装；安装技能条目不得绕过技能守卫路径（`tools/skills_guard.py`）。

## API 与事件

代码中尚不存在 Marketplace API。提议的合约（与包的 asset/service 信封风格一致）：

```http
GET  /api/marketplace/items?kind=&category=&status=&q=&cursor=
GET  /api/marketplace/items/{item_id}
POST /api/marketplace/items/{item_id}/install
POST /api/marketplace/items/{item_id}/disable
POST /api/marketplace/items/{item_id}/enable
```

条目信封（引自 `05-memory-marketplace-files.md`）：

```yaml
id:
kind: skill | recipe | template | element_pack | character_pack
title:
description:
category:
inputs_schema:
output_type:
required_tools:
provider_constraints:
version:
status: installed | available | disabled | deprecated
```

事件（提议的，遵循 `02-agent-runtime-contract.md` 的网关事件命名）：

- `marketplace.item.installed`
- `marketplace.item.status_changed`

现有的相邻界面：技能管理通过 agent 工具（`tools/skill_manager_tool.py`、`tools/skills_tool.py`）以及 `web/src/pages/SkillsPage.tsx` 使用的仪表板配置 API 运行；Marketplace API 应包装这些而非重复它们。

## 数据模型

计划中的实体（目前尚无持久化）：

```text
marketplace_items
- id
- kind: skill | recipe | template | element_pack | character_pack
- title, description, category
- inputs_schema (JSON Schema)
- output_type
- required_tools (list)
- provider_constraints (ref into model catalog constraints)
- version
- source: checked_in | curated | user_published (P3)

marketplace_install_state
- item_id
- workspace_id
- status: installed | available | disabled | deprecated
- installed_by, installed_at
```

对于 `kind=skill`，条目必须引用现有的技能元数据作为唯一真实来源（`agent/skill_bundles.py` 包文件、`skills/*/` SKILL.md 前置元数据），而非复制它。对于 `kind=element_pack` / `character_pack`，条目引用由 Asset Service 拥有的 `asset_references`（`docs/hermes-asset-library-backend-design.md` §核心实体）；Marketplace 不得拥有资产状态。

## UI 行为

- 目录网格按类别分组；每张卡片显示种类徽章、标题、单行描述、状态标签和版本。
- 详情视图显示完整的条目信封，包括渲染为字段列表的 `inputs_schema`、`required_tools` 和 `provider_constraints`。
- 安装/启用/禁用操作是带确认的显式按钮；无悬停安装或自动启用。
- 已禁用和已弃用的条目以不同的视觉状态渲染，但仍可点击检查。
- Marketplace 搜索结果卡片必须具有视觉类型区分，使得"模型配方看起来不像生成的图片"（`05-memory-marketplace-files.md` §Search）。
- 非目标：不要将 Marketplace 合并到通用的"Assets"页面（`01-product-surface.md` §Non-Goals）。

## 权限与错误处理

权限（来自 `05-memory-marketplace-files.md` §Access Control）：

- `read`：任何工作区成员均可浏览目录，包括已禁用的条目。
- `use`：需要 `installed` 状态且被注册表端配置文件包含。
- `update` / `delete`：目录策展权限；在本地目录阶段，这意味着仓库维护者，而非终端用户。

错误合约（带类型的，遵循 `02-agent-runtime-contract.md` §Error Contract 风格）：

| Error | Trigger |
|---|---|
| `marketplace_item_not_found` | 未知或跨工作区的条目 ID。 |
| `marketplace_item_deprecated` | 尝试安装已弃用的条目。 |
| `skill_guard_rejected` | 技能条目未通过获取安全门（`tools/skills_guard.py`）；必须显示拒绝原因，不得吞没。 |
| `required_tool_missing` | 条目声明了当前部署未提供的 `required_tools`。 |

失败必须显式。因缺少必需工具或提供商而无法运行的条目应渲染为带原因的阻塞状态，绝不应静默隐藏。

## 验收标准

- 左侧导航栏暴露 Marketplace 入口，打开目录页面。
- 目录列出已检入的工作流技能，并显示正确的 installed/available/disabled 状态（P1 门槛："Marketplace shows available workflows and status"，`06-delivery-plan.md`）。
- 安装技能条目通过现有的守卫路径，结果在 Marketplace 和 Skill Registry 状态中均可见。
- 已禁用的条目保持可见且可检查。
- 搜索将 Marketplace 条目作为与 assets、files 和 memory 不同的类型化卡片返回。
- P1 阶段除 `skill` 外不需要其他条目种类；recipes、templates 和 packs 可渲染为"即将推出"类别，无需虚假条目。

## 非目标

- 在 P0-P2 阶段实现公共应用商店、支付功能或第三方发布。
- 取代 Skill Registry：Marketplace 从不将技能内容加载到代理上下文中；它只改变获取/启用状态。
- 基于路由决策自动安装技能。
- 拥有 Element/character pack 资源状态（由 Asset Service 拥有）。
- 由 Marketplace 驱动的上游 Hermes 技能移除（`04-skill-tool-prompt-contract.md` §Non-Goals：先禁用/允许列表）。

## 待解决问题

1. 非技能类型的 Catalog 真实来源：与技能一起签入的 YAML，还是在启动时填充的小型数据库表？
2. 安装状态是按工作区还是按用户？`05-memory-marketplace-files.md` 按用户/工作区/项目划分内存范围，但对 marketplace 安装状态保持沉默。
3. 版本固定：当签入的技能更新时，已安装的项目是自动升级还是保持已安装的版本？（`tools/skills_hub.py` 锁定安装路径表明固定是针对 hub 安装的意图。）
4. Ultra 允许列表配置文件（`04-skill-tool-prompt-contract.md`）是过滤 catalog 视图本身，还是仅过滤运行时激活？
5. 在统一搜索界面中，Marketplace 搜索结果如何与资源/文件/内存进行排序？
6. `PluginsPage` 风格的每个插件可见性标志是否应该统一为 marketplace 安装状态，还是保持为单独的管理概念。
