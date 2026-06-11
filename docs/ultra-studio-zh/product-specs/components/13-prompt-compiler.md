# 提示词编译器（Prompt Compiler）

状态：spec-only（仅规范）—— `ultra_prompt_compile` / `ultra_prompt_enhance` 以及一个感知提供商的媒体提示词编译器尚不存在；两端存在相邻的机械结构（智能体系统提示词组装，以及媒体插件内部各提供商的负载构建器）。
日期：2026-06-11

来源：

- 文档：`docs/ultra-studio-product-specs/04-skill-tool-prompt-contract.md`（§Prompt Compiler Boundary、§Model / Prompt Tools、§Clarification Rules），`docs/ultra-studio-agent-skill-tool-prompt-design.md`（§Prompt Design: Global Agent Prompt Addendum、Router Prompt、Workflow Skill Prompt Pattern、No-Fake Prompt Clause），`03-media-asset-contract.md`（§Media Job Envelope: `prompt`、`negative_prompt`、`provider_constraints`），`06-delivery-plan.md`（P1 第 4 项）
- 代码（相邻，本次会话已验证）：`agent/prompt_builder.py`（智能体/系统提示词组装——技能清单、环境提示、HERMES.md 上下文扫描；非媒体编译器），`agent/system_prompt.py`，`plugins/video_gen/atlas/client.py`（`build_payload`——从已解析的参数构建提供商负载），`tools/video_generation_tool.py`（`_build_dynamic_video_schema`、`_format_model_caveats`——约束展示），`trajectory_compressor.py`（与媒体提示词无关；列出以消除歧义）

## 目的与范围

提示词编译器将结构化意图转换为特定于提供商的负载：“智能体应收集结构化意图和资产角色。提示词编译器将其转换为特定于提供商的负载”（`04-skill-tool-prompt-contract.md` §Prompt Compiler Boundary）。

管道位置：

```text
user request -> route -> workflow plan -> asset role manifest
  -> provider constraints -> prompt compile -> job create
```

编译器必须知晓：目标媒体类型、模型家族、宽高比、时长、参考资产、负向约束、工作流技能，以及提供商输入限制（§Prompt Compiler Boundary）。

范围：编译/增强工具合约、输入/输出、约束强制执行、重试的修复计划编译，以及与以下两者的边界：（a）LLM 系统提示词构建器（`agent/prompt_builder.py`，尽管名称相似，但是一个不同的组件）和（b）提供商客户端（接收编译后的负载，仅执行传输层规范化）。

## 实现状态

| 状态 | 项目 | 引用 |
|---|---|---|
| 已实现，相邻能力（Implemented adjacent） | 智能体/系统提示词组装：技能清单、环境提示、上下文文件扫描 | `agent/prompt_builder.py`、`agent/system_prompt.py` —— LLM 侧，非媒体负载 |
| 已实现，相邻能力（Implemented adjacent） | 从已解析的参数构建提供商负载（模型路由、图像输入规范化） | `plugins/video_gen/atlas/client.py`（`build_payload`、`normalize_image_input`） |
| 已实现，相邻能力（Implemented adjacent） | 在提交前向智能体展示约束（动态 schema + 注意事项） | `tools/video_generation_tool.py`（`_build_dynamic_video_schema`、`_format_model_caveats`） |
| 已规定，未构建（Specified, not built） | `ultra_prompt_compile` 工具 | `04-skill-tool-prompt-contract.md` §Model / Prompt Tools；代码中零命中（rg，本次会话） |
| 已规定，未构建（Specified, not built） | `ultra_prompt_enhance` 工具 | 同上 |
| 已规定，未构建（Specified, not built） | 作为编译器输入的资产角色清单（带角色的类型化引用） | §Prompt Compiler Boundary；`hermes-asset-library-backend-design.md` §生成链路 |
| 已规定，未构建（Specified, not built） | 按模型家族处理负向约束 | §Prompt Compiler Boundary |
| 已规定，未构建（Specified, not built） | 用于 `ultra_media_job_retry` 的编译修复计划 | `03-media-asset-contract.md` §Required Job Tools |
| 已规定，未构建（Specified, not built） | 输入编译器的工作流技能提示词模式（`references/` 中各技能的编译配方） | `ultra-studio-agent-skill-tool-prompt-design.md` §Workflow Skill Prompt Pattern、§Skill Package Structure |
| 缺口（Gap） | 在提交给提供商之前，对用户提供的创意提示词应用提示词注入清洗的位置 | 文档包中任何地方均未规定 |

## 用户入口点

无直接入口——编译器是智能体基础设施：

- 工作流技能在收集结构化字段后调用编译（`12-workflow-router.md` 将 `handoff{}` -> 工作流 -> 编译交接）。
- `prompt-repair` 技能（P0 技能集）从失败作业的证据中编译修复计划。
- 检查器的“重试/修复计划”操作触发重新编译（已规划，`03-inspector-live-panel.md`）。
- 用户仅在资产/作业卡片的作业参数中看到编译器效果——默认情况下不显示内部提示词模板（`01-product-surface.md` §Non-Goals）。

## 功能列表

| 功能 | 状态 |
|---|---|
| 将结构化意图 + 资产角色编译为提供商负载 | 已规划，核心合约（Planned） |
| 针对模型家族的飞行前约束验证 | 已规划，来自 `19-model-catalog-provider-constraints.md` 的注册表（Planned）；目前部分由动态工具 schema 强制执行 |
| 宽高比/时长/分辨率规范化至最近允许值并显式通知 | 已规划（Planned） |
| 参考资产角色映射到提供商字段（风格参考、首帧、角色） | 已规划（Planned） |
| 在支持的情况下构建负向提示词 | 已规划（Planned） |
| 提示词增强（`ultra_prompt_enhance`）作为显式、独立的步骤 | 已规划（Planned） |
| 从类型化提供商错误编译修复计划 | 已规划（Planned） |
| 从技能 `references/` 加载各技能编译配方 | 已规划（Planned） |
| 提供商传输规范化（data URI、路由 ID） | 已实现（Implemented）于客户端（`build_payload`）—— 保持在编译器之下 |
| 用于来源链路的提示词哈希输出 | 已规划（Planned）（`03-media-asset-contract.md` §来源链路（Lineage） 需要提示词哈希） |

## 状态机

编译是一个纯函数，而非有状态对象。合约是一个具有显式失败结果的两阶段流程：

```text
collect (router/skill fills intent + asset role manifest)
  -> validate (constraints from catalog; asset refs already permission-checked)
       -> compiled (payload + prompt_hash + constraints snapshot)
       -> rejected (typed: unsupported_model_capability | missing_field
                    | invalid_asset_ref)
compiled -> submitted (job create consumes payload verbatim)
failed job -> repair_compile (error class + evidence -> adjusted payload)
```

规则：

- `rejected` 必须指明确切的字段和允许值；路由器的澄清规则决定是否询问用户（`04-skill-tool-prompt-contract.md` §Clarification Rules）。
- 作业服务按原样消费编译后的负载，不重写提示词；任何编译后修改均属合约违规。
- 修复编译从不静默更改模型家族；切换模型是用户/路由器的决策。

## API 与事件

规划中的工具合约（名称来自 `04-skill-tool-prompt-contract.md`）：

```text
ultra_prompt_compile(
  intent, workflow_skill, model_id,
  asset_roles: [{ref, role}],
  fields: {aspect_ratio?, duration?, resolution?, audio?, …},
  negative_constraints?
) -> {
  payload,                 # provider-ready
  prompt_hash,
  constraints_snapshot,    # frozen catalog constraints used
  notices[]                # normalizations applied
}

ultra_prompt_enhance(prompt, model_id, style_context?) ->
  { enhanced_prompt, rationale }
```

`ultra_prompt_compile` 通过 `ultra_media_constraints_get` 读取约束（`19-model-catalog-provider-constraints.md`）—— 它不嵌入自身的模型限制副本。无网关事件；编译失败以类型化错误形式出现在工具通道上。

## 数据模型

编译器不持久化任何内容。其输出进入：

- MediaJob 信封：`prompt`、`negative_prompt`、`provider_constraints`（快照）、`seed` 透传（`03-media-asset-contract.md` §Media Job Envelope）。
- 来源链路：`prompt hash`、`seed/params`（`03-media-asset-contract.md` §来源链路（Lineage））。

编译配方（已规划）位于技能包内的 `references/` 下（各模型的提示词模式、评分约束），由技能资源加载路径按需加载——不在中央模板数据库中（`ultra-studio-agent-skill-tool-prompt-design.md` §Skill Package Structure）。

## UI 行为

- 默认情况下不暴露内部提示词模板（`01-product-surface.md` §Non-Goals）；检查器显示作业的最终编译提示词、参数和约束快照——是事实，而非模板。
- 规范化通知（“时长调整 12s -> 10s 以适配 wan-2.6”）必须在作业卡片/工具进度中展示，以便用户理解与其请求的偏差。
- 增强是选择加入且可见：增强的提示词会明确标识为此类，从不静默替换（与 No-Fake Prompt Clause 一致，`ultra-studio-agent-skill-tool-prompt-design.md` §Prompt Design）。

## 权限与错误处理

- 编译器仅信任已通过资产服务验证的结构化资产引用；它从不解析纯文本提及（`hermes-asset-library-backend-design.md` §前端交互契约）。
- 类型化拒绝：`unsupported_model_capability`（字段超出约束）、`missing_field`（此工作流的阻塞字段——反馈至一问一答路由）、`invalid_asset_ref`（引用/角色不匹配，例如在仅图像角色中使用视频资产）。
- 无静默钳制：每次规范化都会发出通知；无法规范化的越界值将被拒绝。
- 编译器不得在负载中嵌入凭据或内部端点；提供商路由/凭据在下游附加（TokenRouter / 提供商客户端）。

## 验收标准

- 对于固定的结构化意图，编译输出是确定性的（相同的负载 + prompt_hash），支持来源链路去重和重试比较。
- 违反家族约束的请求在提交前被拒绝，并指明确切的字段和允许值（无需提供商往返）。
- 用于 Atlas 路由的编译负载在传输规范化之外无需修改即可通过 `build_payload`。
- 通过修复计划重试的失败作业会产生 visibly 不同、已解释的负载（可在检查器中比较差异）。
- 每个作业上记录的提示词哈希与根据存储意图重新计算的哈希匹配。
- 无代码路径允许原始用户文本绕过工作流路由媒体作业的编译（“不要绕过 Atlas 对提示词的约束”，`04-skill-tool-prompt-contract.md` §Non-Goals）。

## 非目标

- 成为 LLM 系统提示词构建器（`agent/prompt_builder.py` 是一个独立的、现有的组件；名称冲突是历史遗留问题）。
- 创意构思/头脑风暴——增强会优化提供的提示词；内容创作属于工作流技能。
- 模型选择（路由器/目录决定；编译器接收 `model_id`）。
- 提供商传输细节（data URI 编码、HTTP 结构——提供商客户端拥有这些）。
- 用户面向的模板编辑器。

## 开放问题

1. 运行时形态：是 LLM 调用的真实工具，还是工作流技能脚本内部调用的确定性库？（合约命名工具；技能设计文档暗示技能本地配方。）
2. 现有的 `image_edit` 意图路径（“仅当活动工具支持编辑时才使用图像工具”，在 `12-workflow-router.md` 开放问题 8 中标记）—— 编辑能力检查是在编译中还是在路由中？
3. `ultra_prompt_enhance` 模型：由智能体 LLM 自身增强，还是由专用廉价模型增强？增强调用的成本核算？
4. 用户创意提示词的提示词注入清洗：在提交给提供商之前是否需要，还是提供商侧的安全措施已足够？
5. 本地化：提示词以中英混合形式到达（实际使用）；编译配方是否翻译，以及这是否可见？
6. 当工作流在沙箱（`14-sandbox-lifecycle.md`）内执行时，确定性编译函数在哪里运行——是主机侧工具还是沙箱侧库？
