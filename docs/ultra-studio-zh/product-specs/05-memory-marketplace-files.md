# Memory、Marketplace 和 Files

Status: product/platform specification（产品/平台规范）  
Date: 2026-06-10

## Goal（目标）

定义在创意智能体（creative agent）产品中可见的左侧导航界面：Marketplace、Files、Memory 和 Tasks。这些与 Inspector 不同。

Inspector 用于选中的对象。这些界面用于浏览持久的工作区状态。

## Marketplace（市场）

Marketplace 是可复用创意能力的目录。

它应包含：

- workflow skills（工作流技能）
- prompt recipes（提示词配方）
- storyboard templates（故事板模板）
- model recipes（模型配方）
- reusable Elements（可复用元素）
- character packs（角色包）
- project templates（项目模板）

Marketplace 条目字段：

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

Marketplace 在首个版本中并非公共应用商店。它可以先作为一个由已检入（checked-in）的技能元数据和精选模板支持的本地目录。

## Memory（记忆）

Memory 存储应影响未来工作的持久性事实。

Memory 类别：

- user preferences（用户偏好）
- brand rules（品牌规则）
- project facts（项目事实）
- reusable prompt decisions（可复用提示词决策）
- model preferences（模型偏好）
- rejected styles（已拒绝的风格）
- safety/policy notes（安全/策略备注）

Memory 必须是可见且可编辑的。隐藏的 Memory 会产生信任问题。

必需行为：

- 展示当前工作区/项目存在哪些 Memory
- 允许 delete/revoke（删除/撤销）
- 展示来源会话或用户操作
- 区分 user-authored memory（用户编写的记忆）与 inferred memory（推断的记忆）
- 绝不存储 provider secrets（提供商机密）

## Files（文件）

Files 是任务/工作区对象，不一定是可复用的资源。

File 类别：

- uploaded originals（上传的原始文件）
- downloaded web artifacts（下载的网络产物）
- generated task files（生成的任务文件）
- logs（日志）
- prompt plans（提示词计划）
- storyboard sheets（故事板表格）
- rendered outputs（渲染输出）

Files 可以被提升为 assets（资源），但不应自动成为可复用的项目资源。

## Tasks（任务）

Tasks 代表工作历史和运行中的作业。

Task 行应展示：

- title（标题）
- session id（会话 ID）
- last user request（最后用户请求）
- status（状态）
- active jobs（活跃作业）
- output count（输出数量）
- date（日期）
- source: web | tui | cli | panel（来源：web | tui | cli | panel）

点击 Task 应恢复：

- transcript（对话记录）
- active/complete jobs（活跃/已完成作业）
- task files（任务文件）
- selected model（选中的模型）
- active skill profile（活跃技能配置文件）
- relevant memory（相关记忆）

## Search（搜索）

Search 应涵盖：

- messages（消息）
- tasks（任务）
- files（文件）
- assets（资源）
- memory（记忆）
- marketplace entries（市场条目）

搜索结果卡片必须展示 type（类型）和 source（来源）。model recipe（模型配方）不应看起来像生成的图像。memory（记忆）不应看起来像 file（文件）。

## Access Control（访问控制）

最小权限：

- read（读取）
- use（使用）
- update（更新）
- delete（删除）
- revoke（撤销）
- share（共享）

规则：

- Marketplace 条目可以在未启用状态下可见。
- Memory 按 user/workspace/project（用户/工作区/项目）划分作用域。
- Files 按 session/project（会话/项目）划分作用域。
- Assets 按 project/workspace（项目/工作区）和权限规则划分作用域。
- 共享对话并不意味着共享 sandbox（沙盒）或 credentials（凭证）。

## Acceptance（验收标准）

- 左侧导航栏展示 Marketplace、Files、Memory 和 Tasks。
- Memory 条目可被检查并 revoke（撤销）。
- Files 可被提升为 assets（资源）。
- Marketplace 可展示 installed（已安装）和 disabled（已禁用）的工作流。
- 搜索结果按类型区分（typed），不混合不同界面（surfaces）。
