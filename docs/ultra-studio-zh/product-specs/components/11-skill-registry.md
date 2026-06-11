# 技能注册表

状态：partial（部分实现）—— 技能发现、元数据扫描、渐进式提示加载、安装/同步/防护工具、使用追踪以及技能管理页面已实现；Ultra Studio 的允许列表配置文件和两个技能评估仅处于 spec-only（仅规格）阶段。
日期：2026-06-11

来源：

- 文档：`docs/ultra-studio-product-specs/04-skill-tool-prompt-contract.md`（§Skill Layers、§Required Skill Runtime Objects、§Required Creative Skills、§Non-Goals、§Acceptance），`docs/ultra-studio-agent-skill-tool-prompt-design.md`（§Skill Catalog Target、§Final Visible Catalog、§Progressive Disclosure Strategy、§Skill Package Structure、§Disabling and Deletion Plan），`06-delivery-plan.md`（P0 第 1 项、P3 第 1 项）
- 代码（本次会话已验证）：`agent/skill_bundles.py`（`scan_bundles`、`list_bundles`、`save_bundle`、`delete_bundle`、`resolve_bundle_command_key`、`build_bundle_invocation_message`），`agent/prompt_builder.py`（`_build_skills_manifest`、`_load_skills_snapshot`、`_write_skills_snapshot`、`_parse_skill_file`、`clear_skills_system_prompt_cache`），`agent/skill_commands.py`、`agent/skill_preprocessing.py`、`agent/skill_utils.py`，`tools/skills_hub.py`（`SkillMeta`、`SkillBundle`、locked install paths、`_guarded_http_get`），`tools/skill_manager_tool.py`、`tools/skills_tool.py`、`tools/skills_sync.py`、`tools/skills_guard.py`、`tools/skills_ast_audit.py`、`tools/skill_provenance.py`、`tools/skill_usage.py`、`skills/`（category tree incl. `skills/creative/`、`skills/index-cache`），`web/src/pages/SkillsPage.tsx`、`agent/insights.py`（`_get_skill_usage`）

## 目的与范围

技能注册表负责技能的发现、启用/禁用、版本控制和配置文件过滤（`04-skill-tool-prompt-contract.md` §Required Skill Runtime Objects）。其产品目的：防止智能体"表现得像一个拥有 87 个无关技能的通用助手"——Ultra Studio 通过渐进式加载暴露一组聚焦的创意技能集。

技能分层契约（§Skill Layers）：启动时仅加载技能元数据；`SKILL.md` 在路由后加载；大型模式/编译器/评分标准位于 `references/` 目录中，仅在需要时加载。

范围：发现与元数据、渐进式加载、允许列表配置文件、安装/更新/移除、安全门控、使用遥测以及两个必需的评估。路由决策归属 `12-workflow-router.md`；目录选购界面归属 `04-marketplace.md`。

## 实现状态

| 状态 | 项目 | 引用 |
|---|---|---|
| 已实现（Implemented） | 按类别组织的磁盘技能树，带索引缓存 | `skills/`（例如 `skills/creative/infographic-md-flow/SKILL.md`）、`skills/index-cache` |
| 已实现（Implemented） | 包扫描、列表、保存/删除、斜杠命令解析、调用消息组装 | `agent/skill_bundles.py` |
| 已实现（Implemented） | 向系统提示的渐进式披露：技能清单 + 按 mtime 键控的快照缓存、前置元数据解析 | `agent/prompt_builder.py`（`_build_skills_manifest`、`_load_skills_snapshot`、`_parse_skill_file`） |
| 已实现（Implemented） | 调用时的技能命令处理和预处理 | `agent/skill_commands.py`、`agent/skill_preprocessing.py`、`agent/skill_utils.py` |
| 已实现（Implemented） | 带元数据模型的 Hub 安装路径、锁定安装路径、受保护的 HTTP 获取 | `tools/skills_hub.py` |
| 已实现（Implemented） | 面向智能体的技能管理工具（列表/安装/管理/同步） | `tools/skill_manager_tool.py`、`tools/skills_tool.py`、`tools/skills_sync.py` |
| 已实现（Implemented） | 安全门控：获取防护、AST 检查、写入来源溯源 | `tools/skills_guard.py`、`tools/skills_ast_audit.py`、`tools/skill_provenance.py` |
| 已实现（Implemented） | 使用追踪和分析 | `tools/skill_usage.py`、`agent/insights.py`（`_get_skill_usage`） |
| 已实现（Implemented） | 技能管理页面（浏览/切换） | `web/src/pages/SkillsPage.tsx` |
| 已规定，未构建（Specified, not built） | 技能允许列表配置文件——Ultra Studio 可见技能集作为命名、强制执行的配置文件 | `04-skill-tool-prompt-contract.md` §Required Skill Runtime Objects；`06-delivery-plan.md` P0 第 1 项 "Ultra profile/allowlist bootstrap" |
| 已规定，未构建（Specified, not built） | 技能触发评估（路由选择正确的技能） | `04-skill-tool-prompt-contract.md`；`06-delivery-plan.md` P3 第 1 项 |
| 已规定，未构建（Specified, not built） | 技能输出契约评估（交接模式、缺失字段） | `04-skill-tool-prompt-contract.md` |
| 已规定，未构建（Specified, not built） | P0 创意技能：`workflow-router`、`media-qa`、`prompt-repair`、`product-photoshoot`、`product-md-flow` 作为已发布技能（`infographic-md-flow` 已存在：`skills/creative/infographic-md-flow/`） | `04-skill-tool-prompt-contract.md` §Required Creative Skills |
| 已规定，未构建（Specified, not built） | 上游技能的 A 阶段禁用优先 / B 阶段归档计划 | `ultra-studio-agent-skill-tool-prompt-design.md` §Disabling and Deletion Plan |

## 用户入口点

- 隐式：每个被路由的请求都可能激活由 `workflow-router`（规划中路由）或斜杠命令（已实现：`resolve_bundle_command_key`）选择的技能。
- 聊天/TUI 中的斜杠命令 `/skill-name`（已实现）。
- 用于浏览和切换的技能管理页面（已实现：`SkillsPage.tsx`）。
- 市场安装流程（规划中；包装 hub 安装路径）。
- 智能体自助服务：技能管理工具让智能体可以列出/检查技能（已实现），受防护规则门控。

## 功能列表

| 功能 | 状态 |
|---|---|
| 仅元数据启动加载（名称/描述/命令） | 已实现（Implemented）（`prompt_builder.py` 中的清单 + 快照） |
| 仅在激活时加载 `SKILL.md` 正文 | 已实现（Implemented）（包调用路径） |
| `references/` / `scripts/` / `assets/` 延迟加载 | Partial（部分实现）—— 技能树中存在包结构；正式的 Skill Resource Loader 对象处于规格语言阶段（`04` §Required Skill Runtime Objects） |
| 启用/禁用单个技能 | 已实现（Implemented）（管理页面 + 配置） |
| 命名允许列表配置文件（Ultra 配置文件过滤可见集） | 已规划（Planned） |
| 从 hub 安装，带锁定路径 + 受保护获取 | 已实现（Implemented）（`tools/skills_hub.py`） |
| 获取安全：防护 + AST 检查 + 溯源 | 已实现（Implemented） |
| 每个技能的版本追踪 | Partial（部分实现）—— hub 包携带版本（`SkillBundle`）；本地树版本控制仅为 git 级别 |
| 每个技能的使用遥测 | 已实现（Implemented）（`tools/skill_usage.py`、insights） |
| 触发评估工具 | 已规划（Planned） |
| 输出契约评估工具 | 已规划（Planned） |
| 上游技能禁用优先迁移 | 已规划（Planned）（A/B 阶段计划） |
| 每个会话的技能配置文件（会话状态中的活跃技能配置文件） | 已规划（Planned）（`02-agent-runtime-contract.md` §Session Lifecycle） |

## 状态机

每个技能的行政状态：

```text
discovered (on disk, scanned into manifest)
  -> enabled    (visible to routing/prompt for the active profile)
  -> disabled   (hidden from routing; files remain — disable-first rule)
enabled <-> disabled (admin/profile toggle)
disabled -> archived  (Phase B physical move, only after allowlist verification)
```

每次调用的生命周期：

```text
metadata in prompt -> routed/invoked -> SKILL.md loaded
  -> references loaded on demand -> executed -> usage recorded
```

规则：上游技能在禁用/允许列表验证前绝不删除（`04-skill-tool-prompt-contract.md` §Non-Goals）；启用 hub 安装的技能要求防护 + 检查通过已成功。

## API 与事件

已实现（进程内 / 工具表面）：

- `scan_bundles()` / `list_bundles()` / `get_bundle(name)` / `save_bundle(...)` / `delete_bundle(name)` —— `agent/skill_bundles.py`。
- 斜杠解析 `resolve_bundle_command_key(command)` 和调用消息组装 `build_bundle_invocation_message(...)`。
- 带有缓存失效的系统提示技能清单/快照（`clear_skills_system_prompt_cache`）。
- 智能体工具：技能列表/安装/同步（`tools/skills_tool.py`、`tools/skill_manager_tool.py`、`tools/skills_sync.py`）。
- 由 `SkillsPage.tsx` 使用的仪表板技能 API（页面已验证；路由形状此处不再推导）。

已规划：

- 配置文件 API：解析活跃配置文件 -> 可见技能集；持久化为会话状态中的"活跃技能配置文件"。
- 评估工具入口点（触发评估、输出契约评估）可在 CI 中运行。

无网关事件；注册表变更通过提示重建和管理页面刷新呈现。

## 数据模型

已实现：

- 技能包：包含 `SKILL.md` 的目录（前置元数据：名称、描述、触发器），可选 `references/`、`scripts/`、`assets/`（`ultra-studio-agent-skill-tool-prompt-design.md` §Skill Package Structure；在 `_parse_skill_file` 中解析）。
- 包记录：`SkillMeta` / `SkillBundle`（hub 元数据、安装路径、类别）—— `tools/skills_hub.py`。
- 清单快照：名称 -> [偏移量/mtimes] 缓存，用于提示组装（`_build_skills_manifest`）。
- insights DB 中的使用记录（`_get_skill_usage`）。

已规划：

```text
skill_profiles
- profile_id            (e.g. "ultra-studio")
- visible_skills[]      (allowlist, not blocklist)
- default_for_surface: web | tui | cli
```

## UI 行为

- 技能页面按类别分组，显示启用状态、描述和来源（捆绑 vs hub 安装）；切换会更新活跃清单。
- 禁用的技能保持列出（可见但禁用），与市场可见性规则一致。
- Hub 安装在启用前呈现防护/检查结果；被拒绝的安装会逐字显示原因。
- 聊天界面从不显示原始技能注册表；用户通过市场和路由行为看到效果（可用工作流）。
- 斜杠命令弹出框（`web/src/components/SlashPopover.tsx`）列出活跃配置文件的命令可用技能。

## 权限与错误处理

- 技能安装/修改属于管理员范围；智能体发起的写入携带写入来源溯源（`tools/skill_provenance.py` `set_current_write_origin`），以便区分后台/智能体写入与用户操作。
- 防护失败对安装是终止性的（无部分启用）；`tools/skills_guard.py` 拒绝必须逐字呈现。
- 错误：`skill_not_found`（未知斜杠/包键）、`skill_disabled`（针对禁用技能的调用——必须说明哪个配置文件禁用了它）、`skill_install_rejected`（防护/检查）、`skill_sync_conflict`（通过 `tools/skills_sync.py` 的同步分歧）。
- 配置文件配置错误（空可见集）必须在启动时大声失败，而非静默暴露所有技能——这是 Ultra 配置文件存在以防止的故障模式。

## 验收标准

- 当 Ultra 配置文件活跃时，可见技能列表仅包含目录目标集（"可见技能列表是 Ultra 聚焦的"，`04-skill-tool-prompt-contract.md` §Acceptance）。
- 通用视频请求不会触发无关技能（ASCII / Comfy / Manim）—— 通过触发评估用例验证。
- 启动提示仅包含元数据；`SKILL.md` 正文在激活时加载（可通过提示大小/快照检查观察）。
- 禁用技能会在一次提示重建内将其从路由中移除，且文件保留在磁盘上。
- 带有失败 AST 检查的 hub 安装永远不会到达启用状态。
- 调用后使用行会出现在 insights 中。

## 非目标

- 作为目录 UI（Marketplace）或路由器（workflow-router）。
- 将删除上游技能作为清理策略——优先禁用/允许列表。
- 让插件替换工作流技能（`04-skill-tool-prompt-contract.md` §Non-Goals）。
- 技能市场发布（P3）。
- 每个技能的沙盒/运行时隔离（技能是提示+资源；执行隔离归属 `14-sandbox-lifecycle.md`）。

## 待解决问题

1. 配置文件存储和优先级：配置文件 vs DB；当会话状态的"活跃技能配置文件"与部署默认值冲突时会发生什么？
2. 评估工具形状：触发/输出评估是否阻塞 CI（现有 workflow-router 规格留有空闲通过栏——其待解决问题 9）？
3. Hub 安装的版本固定 vs git 跟踪的捆绑技能——安装的技能是否可就地升级，由谁批准？
4. Ultra 配置文件是否也过滤管理技能页面，还是仅运行时可见性？
5. 正式的 Skill Resource Loader：当前延迟加载是否足够，还是 `references/` 需要带预算核算的显式加载 API？
6. 多表面配置文件：TUI/CLI 会话默认获得 Ultra 配置文件还是完整的 Hermes 集？
