# 技能、工具与提示词合约

状态：workflow specification  
日期：2026-06-10

## 目标

防止代理表现得像一个拥有 87 个无关技能的通用助手。Ultra Studio 应该暴露一个专注的创意技能系统，具备渐进式加载、类型化工具和提供商感知的提示词编译。

## 技能层级

```text
skill metadata
  -> SKILL.md
  -> references/
  -> scripts/
  -> assets/
```

启动时仅加载技能元数据。`SKILL.md` 在路由后加载。大型模式、提示词编译器、评分标准和示例属于 `references/`。

## 所需的技能运行时对象

| 对象 | 用途 |
|---|---|
| `Skill Registry` | 发现、启用/禁用、版本、配置文件筛选。 |
| `Skill Allowlist Profile` | Ultra Studio 可见的技能集。 |
| `Skill Trigger Eval` | 测试路由是否选择了正确的技能。 |
| `Skill Output Contract Eval` | 测试交接模式和缺失字段。 |
| `Skill Resource Loader` | 仅在需要时加载 references/scripts/assets。 |

## 路由器输出

`workflow-router` 应该生成一个结构化对象：

```json
{
  "intent": "image_generate | video_generate | edit | asset_search | chat | unknown",
  "execution_mode": "answer | ask | tool | workflow",
  "workflow_skill": "infographic-md-flow",
  "primary_tool": "ultra_media_job_create",
  "asset_roles": [],
  "missing": [],
  "handoff": {}
}
```

路由器不得自行生成媒体。它选择下一个工作流或询问缺失的字段。

## 澄清规则

仅在答案会改变时才询问：

- 输出类型
- 宽高比
- 资源角色
- 工作流
- 成本/并发
- 模型能力

不要在路由前询问通用问题。像"制作一个视频"这样的模糊请求应该路由到 `video_generate` 意图，然后询问一个关于类型或源素材的有用问题。

## 工具组

### 资源工具（Asset Tools）

- `ultra_asset_upload`
- `ultra_asset_list`
- `ultra_asset_inspect`
- `ultra_asset_download`
- `ultra_asset_promote`

### 媒体作业工具（Media Job Tools）

- `ultra_media_job_create`
- `ultra_media_job_status`
- `ultra_media_job_cancel`
- `ultra_media_job_retry`
- `ultra_media_job_finalize`

### 模型/提示词工具（Model / Prompt Tools）

- `ultra_model_catalog`
- `ultra_media_constraints_get`
- `ultra_prompt_compile`
- `ultra_prompt_enhance`

## 提示词编译器边界

代理应该收集结构化的意图和资源角色。提示词编译器将其转换为特定于提供商的有效载荷。

```text
user request
  -> route
  -> workflow plan
  -> asset role manifest
  -> provider constraints
  -> prompt compile
  -> job create
```

编译器应该知道：

- 目标媒体类型
- 模型家族
- 宽高比
- 时长
- 参考资源
- 负面约束
- 工作流技能
- 提供商输入限制

## 必需的创意技能

P0：

- `workflow-router`
- `infographic-md-flow`
- `media-qa`
- `prompt-repair`
- `product-photoshoot`
- `product-md-flow`

P1：

- `ugc-flow`
- `cinematic-flow`
- `typography-md-flow`
- `amazon-product-listing`
- `character-consistency`

## 非目标

- 默认情况下不暴露无关的通用技能。
- 在禁用/允许列表验证之前不要删除上游技能。
- 不要让插件替换工作流技能。
- 不要在提示词中绕过 Atlas 约束。

## 验收

- 可见技能列表是 Ultra 专注的。
- 通用视频请求不会触发随机的 ASCII/Comfy/Manim 技能。
- 明确的图像请求调用图像路径而不询问无关问题。
- 明确的视频请求创建真实的媒体作业或返回类型化的阻塞信息。
- 技能输出对工具和 UI 来说足够机器可读。
