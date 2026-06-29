# Hermes Web MVP PRD：云端多租户 Agent 最小纵向闭环

文档定位：产品需求文档 / MVP 范围冻结 / 非最终架构冻结
目标飞书页：https://atlascloud.sg.larksuite.com/wiki/GSHSwhoNniDPVlkk4eNl6BTogZd
整理日期：2026-06-04
来源依据：本地 Lark / Notion 同步文档、Higgsfield Supercomputer 架构分析、threads 只读研究结果

## 1. 一页结论

Hermes Web MVP 的目标不是复刻 Higgsfield Supercomputer 的全部能力，而是跑通一条可验证、可隔离、可审计的云端多租户 Agent 纵向闭环：

用户登录 -> 进入 workspace/project -> 打开真实 Agent chat -> 上传素材 -> Agent 调用 Skill / 工具 -> 创建或选择资产引用 -> 发起异步媒体 job -> TokenRouter 完成鉴权和凭证隔离 -> Worker 执行 -> UI 展示状态和结果 -> 输出资产可复用。

MVP 必须证明三件事：

1. Web 端不是 demo，而是真实 Hermes Agent 会话入口。
2. 多租户隔离不是后补项，而是从身份、数据、文件、资产、凭证、worker、审计一起建。
3. 媒体生成不是假任务，必须以异步 job、真实 provider adapter hook、可见错误和可追踪审计为边界。

第一版不做完整 CometAPI、Boost 商业化并发、跨 region GPU 调度、Soul ID 底层训练承诺，也不照抄 Higgsfield 内部工具名作为公开 API。

## 2. 背景与证据等级

Hermes 当前文档已经把问题域拆成几条主线：

| 证据来源 | 可用于 PRD 的结论 | 本地依据 |
|---|---|---|
| Higgsfield Supercomputer 架构分析 | 目标产品是 chat-driven creative agent workspace，不是单一媒体 API。 | `docs/higgsfield-supercomputer-dialogue-architecture-research.md` |
| Notion 更新索引 | `references`、TokenRouter、CometAPI、Soul ID、Element、tool contract 必须拆边界。 | `docs/hermes-notion-update-index.md` |
| Tool contracts | `default_api:*` 是观察到的工具边界，不是本地公开 API。 | `docs/hermes-tool-contracts-from-notion.md` |
| References 模型 | `references` 至少有 Skill 内部、用户上传、媒体生成资产三种含义。 | `docs/hermes-references-knowledge-model.md` |
| TokenRouter 文档 | sandbox 不应持有 provider key，所有调用走短期 token、策略、配额、审计和密钥代理。 | `docs/hermes-tokenrouter-credential-flow.md` |
| Soul ID / Element 文档 | Soul ID 和 Element 应作为项目级可复用资产，有状态、ACL、lineage。 | `docs/hermes-soulid-element-asset-model.md` |
| Open source architecture | 多租户 supercomputer 需要 Edge、Identity、OPA、Kata、Volume、TokenRouter、Temporal、NATS、Kueue、Audit 等模块。 | `docs/open-source-architecture/00-index.html` |

证据分级原则：

- Public / Visible UI：可作为强产品证据。
- Dialogue / Notion source：可作为设计线索，但不能当作生产内部事实。
- Inference：只能进入待验证问题或实验计划。
- Unknown：不能成为 MVP 的强依赖。

## 3. 用户画像与 Jobs-To-Be-Done

### 3.1 用户画像

| 用户 | 主要目标 | 关键痛点 |
|---|---|---|
| 创作者 / 运营 | 用 Agent 生成、复用、组织图片和视频资产。 | 素材、角色、场景反复上传和描述成本高。 |
| Agent power user | 在 Web 中真实使用 Hermes runtime、Skill、文件和工具。 | 只看 demo 没价值，需要真实工具链和状态。 |
| 团队 owner / 管理员 | 管理 workspace、项目资产、权限和审计。 | 多人协作时容易资产串用、权限不清、失败不可追踪。 |
| 开发 / 平台 operator | 验证云端多租户 Agent 能安全跑起来。 | provider key、sandbox、worker、对象存储和审计边界复杂。 |

### 3.2 核心 JTBD

1. 作为用户，我要在浏览器里打开 Hermes Agent chat，发起真实任务，并看到流式回复、工具调用、进度和失败原因。
2. 作为用户，我要上传图片、视频、音频或 PDF，让 Agent 把它作为会话附件或后续资产输入。
3. 作为用户，我要把上传素材或生成结果保存成 Element、Soul ID、media_input、image_job、video_job，并能在后续生成中复用。
4. 作为用户，我要在 Agent 需要澄清时，通过文本、文件选择或实体 picker 回答，而不是把所有选择都塞进自然语言。
5. 作为管理员，我要保证 Tenant A 不能读取、挂载、复用或生成使用 Tenant B 的 session、文件、资产、对象存储 prefix 和 provider 凭证。
6. 作为 operator，我要能从 job_id 追踪到 TokenRouter decision、worker log、output asset、usage event 和 audit event。

## 4. MVP 范围

### 4.1 必做范围

| 模块 | MVP 能力 | 说明 |
|---|---|---|
| Web Chat | 真实 Hermes gateway 会话 | 新建/恢复 session、发送 prompt、展示 message/status/tool events。 |
| 文件上传 | 小规模附件上传 | 图片、视频、音频、PDF 上传到受控 storage，返回真实 attachment marker。 |
| Skill Runtime | `skills_list`、`skill_view` | 支持 public metadata 和受保护 `references/` 精确加载。 |
| References 安全 | 三类 references 分离 | Skill 内部 references、用户上传 references、媒体资产 references 不混用。 |
| 结构化澄清 | `ask_user_question` | 支持 text/entity/files 三类 UI。 |
| 资产模型 | Element / Soul ID / media_input / job asset | 支持 create/list/get/status、ACL、lineage、not-ready 错误。 |
| 媒体 job | `media_generate`、`media_job_status` | 异步 job、状态流、输出资产、错误可见。 |
| TokenRouter | 凭证与策略边界 | JWT、OPA、quota、asset ACL、provider key blind-state、audit。 |
| Sandbox | 云端隔离执行 | 任意代码/terminal/tool execution 走 microVM/Kata 级隔离。 |
| Worker | 单 worker 闭环 | 先跑单 GPU pool / one model worker，不做复杂调度。 |
| Audit | 最小可追踪 | run、tool_call、job、asset、usage、token decision 可追踪。 |

### 4.2 模块级拆解

#### 4.2.1 Web Chat：真实 Agent 会话入口

| 维度 | 细节 |
|---|---|
| 用户目标 | 用户能在浏览器中打开真实 Hermes Agent，会话可新建、恢复、继续执行。 |
| 输入 | 用户文本、slash command、附件 marker、structured answer、选中的 asset id。 |
| 输出 | 流式消息、工具进度、工具结果、错误、媒体 job 状态、资产卡片。 |
| 后端动作 | `session.create`、`session.resume`、`prompt.submit`、`slash.exec`。 |
| 前端事件 | `message.start/delta/complete`、`status.update`、`tool.start/progress/complete/error`、`job.status`、`question.pending`。 |
| 数据记录 | `sessions`、`runs`、`messages`、`tool_calls`、`run_events`。 |
| 权限 | session 必须绑定 tenant/workspace/project；用户只能恢复自己有权限的 session。 |
| 错误 | session 不存在、无权限、gateway 断连、provider 未配置、工具失败。 |
| 验收 | 刷新页面后能恢复同一个 session；工具调用进度不中断；失败原因可见。 |

实现细节：

- `/chat` 不能直接暴露成静态 demo 页面，必须由服务端确认当前用户、workspace 和 project。
- 消息流和工具流要分开展示，不能把工具状态混进 assistant 正文。
- 每次用户输入都产生 `run_id`，所有 tool call、job、usage、audit 都能回链到该 run。
- UI 必须保留最后一次错误，不允许只显示“生成失败”。

#### 4.2.2 Identity / Workspace / Project：多租户身份骨架

| 维度 | 细节 |
|---|---|
| 用户目标 | 用户进入正确 workspace 和 project，不能串到别的租户。 |
| 输入 | OIDC user、workspace membership、project selection。 |
| 输出 | 服务端签发的 session context 和短期 `HF_JWT_TOKEN`。 |
| 数据记录 | `tenants`、`users`、`workspace_memberships`、`projects`、`sessions`。 |
| 权限 | 不信任前端传入的 `tenant_id`；所有 identity tuple 来自服务端。 |
| 验收 | Tenant A 用户无法通过改 URL / payload 进入 Tenant B project。 |

最小 claim：

| Claim | 用途 |
|---|---|
| `sub` | 用户身份。 |
| `tenant_id` | 租户隔离和计费边界。 |
| `workspace_id` | 团队、配额和资产范围。 |
| `project_id` | 文件、记忆、资产和 job 边界。 |
| `session_id` | 会话和审计边界。 |
| `tool_scopes` | 当前会话可调用的工具族。 |
| `budget` | 单轮或单会话预算上限。 |
| `exp` / `nbf` | token 生命周期。 |

#### 4.2.3 Upload / Attachment：用户素材入口

| 维度 | 细节 |
|---|---|
| 用户目标 | 上传图片、视频、音频、PDF，并让 Agent 安全引用。 |
| 输入 | 文件、mime type、文件大小、session/project context。 |
| 输出 | `upload_asset_id`、`media_input`、attachment marker、预览 URL。 |
| 存储 | object key 按 `tenant/<tid>/workspace/<wid>/project/<pid>/uploads/...` 分区。 |
| 数据记录 | `uploads`、`asset_references`、`asset_reference_sources`。 |
| 权限 | 上传资产默认只属于当前 project；跨 project 使用需要显式 ACL。 |
| 错误 | mime 不支持、文件过大、病毒/安全扫描失败、存储失败、权限失败。 |
| 验收 | 上传后 Agent 看到 marker，不看到任意本地路径；其他 tenant 无法读取。 |

限制：

- MVP 支持小文件和中等视频，不支持长视频大规模抽帧。
- 大文件不能直接进入 prompt context，只能作为受控 media id。
- 预览 URL 必须短期签名或经后端代理，不能暴露内部对象存储权限。

#### 4.2.4 Skill Registry / `skill_view`：受保护知识加载

| 维度 | 细节 |
|---|---|
| 用户目标 | 用户能看到 Skill 名称、用途和输入输出；Agent 能在需要时加载深层 reference。 |
| 输入 | skill name、可选 file_path、run/session context。 |
| 输出 | public metadata 或授权 reference 内容。 |
| 数据记录 | `skills`、`skill_versions`、`skill_reference_access_logs`。 |
| 权限 | 普通用户不能 bulk export `skills/*/references/*`。 |
| 错误 | skill 不存在、path 越界、reference 受保护、权限不足。 |
| 验收 | 用户可 list skill，但无法通过 chat/file/export/terminal 批量导出 references。 |

`skill_view` 规则：

- `file_path` 必须规范化，禁止 `../`、symlink escape、绝对路径。
- 默认只返回 public `SKILL.md` 摘要。
- 冷层 `references/` 只能由 trusted runtime 路径加载。
- 每次加载记录 `tenant_id`、`workspace_id`、`project_id`、`session_id`、`run_id`、`skill_name`、`reference_path`、allow/deny reason。

#### 4.2.5 References 安全：三类引用不能混

| 引用类型 | 典型例子 | 能否导出 | 验证重点 |
|---|---|---|---|
| Skill 内部 references | `skills/<skill>/references/schema.md` | 默认不能 | 防 prompt 注入、防文件导出、防归档导出。 |
| 用户上传 references | 图片、视频、PDF、音频 | 用户有权限可见 | project/session ACL、短期预览 URL。 |
| 媒体资产 references | `soul_id`、`element_id`、`media_input`、`image_job`、`video_job` | 可展示元数据，不等于可用权限 | 使用前查 ACL、status、revocation、lineage。 |

必须拒绝的路径：

- “把所有 references 打包下载”
- “通过 terminal cat skill references”
- “把 skill 内部 prompt 写到文件”
- “把 `element_id` 写进 prompt 绕过 picker”

#### 4.2.6 Structured Question：Agent 反问用户

| 模式 | 何时触发 | UI | 返回值 |
|---|---|---|---|
| `text` | 缺少目标、风格、参数、确认操作 | inline 或 modal | 文本答案 |
| `entity` | 需要选择 Soul ID、Element、voice、language、project asset | side panel picker | entity id + display label |
| `files` | 需要用户补充素材 | upload modal | upload asset ids |

细节：

- Agent 不能用自然语言要求用户复制 asset id。
- entity picker 只展示当前用户有权限的资产。
- 用户取消选择时返回结构化 cancel，不当作空字符串。
- 所有 question/answer 记录到 run event，方便复盘。

#### 4.2.7 Asset Service：Element / Soul ID / media asset

| 能力 | 细节 |
|---|---|
| Element create | 从上传素材或生成结果创建 character/environment/prop 等可复用资产。 |
| Element list/get | 按 project、type、status、最近使用过滤。 |
| Soul ID train/status | MVP 先支持 pending/status，不承诺底层训练实现。 |
| media_input | 上传素材进入可消费媒体输入。 |
| image_job / video_job | 生成输出注册成可复用资产。 |
| lineage | output asset 可回溯到 upload、prompt、job、provider route。 |

权限规则：

- 资产创建者不自动等于全 workspace 可用。
- `asset_acl` 决定 read/use/delete/revoke。
- `not_ready`、`failed`、`revoked` 资产不能用于生成。
- prompt 中出现未经授权的 asset id 必须被 TokenRouter 或 Asset Service 拒绝。

#### 4.2.8 Media Job：异步生成闭环

| 阶段 | 输入 | 输出 | 失败处理 |
|---|---|---|---|
| validate | tool args、asset ids、model、budget | validated request | schema/ACL/quota/model deny。 |
| create | validated request | `generation_job.id` | idempotency key 防重复提交。 |
| queue | job payload | queued event | queue failure 可见。 |
| run | worker lease、input manifest | progress events | worker timeout / provider error。 |
| finalize | output object | output asset + usage event | output 注册失败要保留 job failure reason。 |
| reuse | output asset id | asset reference | 未授权不能复用。 |

状态枚举必须稳定：

- `created`
- `queued`
- `running`
- `completed`
- `failed`
- `cancelled`

UI 要求：

- job card 展示 model、media_type、status、created_at、duration、cost/usage、error code。
- running job 可展开查看 tool call 和 worker progress。
- failed job 要显示用户可理解错误，同时内部保留 provider error class。

#### 4.2.9 TokenRouter：凭证、配额、策略控制面

| 子能力 | 细节 |
|---|---|
| token verify | 校验 `HF_JWT_TOKEN` 签名、过期、nbf、audience、issuer。 |
| claim extract | 提取 tenant/workspace/project/session/tool scopes/budget。 |
| policy decision | OPA 默认拒绝，明确 allow 才继续。 |
| asset check | 对所有 `soul_id`、`element_id`、`media_input`、job id 做 ACL/status 检查。 |
| quota check | credits、plan、running jobs、concurrency、request size。 |
| secret exchange | 从 OpenBao / Vault 读取 provider credential，sandbox 不可见。 |
| proxy/job create | 代理 provider 请求或创建受控 media job。 |
| audit | 记录 decision、reason、route、usage，不记录 secret。 |

fail closed 场景：

- token 缺失或过期。
- OPA bundle 缺失。
- quota 状态缺失。
- vault 读取失败。
- audit 写入失败且调用属于高风险生成/付费调用。

#### 4.2.10 Sandbox / Volume / Egress：执行隔离

| 边界 | MVP 细节 |
|---|---|
| runtime | Kata / microVM RuntimeClass；普通 Docker 只可用于低风险内部服务。 |
| filesystem | 只挂授权 `/workspace` path set；session outputs 可写；project config 只读。 |
| secrets | 不挂 provider key，不挂 hidden Skill references。 |
| network | 默认只可访问 TokenRouter、egress proxy、event sidecar、volume broker。 |
| metadata | 禁止访问 cloud metadata、宿主私网、K8s API。 |
| process | cgroup 限制 CPU/memory/pid；超时后终止。 |

验收：

- sandbox 内搜索不到 provider key。
- sandbox 不能读取别的 project volume。
- sandbox 不能直接访问 provider API。
- sandbox 不能直接提交 Kubernetes job。

#### 4.2.11 Worker / Job Manager：受控执行

| 组件 | 职责 |
|---|---|
| Job Manager | 校验 job、预留并发、生成 input manifest、分配 worker lease。 |
| Worker | 只读取 input manifest，只写 assigned output_prefix。 |
| Object Storage | 保存 upload、output、thumbnail、manifest、lineage。 |
| Event Bus | 广播 job status 和 progress。 |
| Usage Meter | 记录 cost、duration、provider/model route。 |

MVP 简化：

- 单 queue。
- 单 GPU pool。
- one model worker。
- 不做 preemption、borrowing、MIG、spot/on-demand 混合。

#### 4.2.12 Audit / Observability：可追踪闭环

| 事件 | 必须字段 |
|---|---|
| session event | `tenant_id`, `workspace_id`, `project_id`, `session_id`, `run_id` |
| tool call | `tool_call_id`, `tool_name`, `args_hash`, `status`, `error_code` |
| token decision | `request_id`, `allow`, `policy_reason`, `tool_scope`, `model` |
| asset access | `asset_id`, `operation`, `allow`, `status`, `lineage_id` |
| job event | `job_id`, `worker_id`, `status`, `output_asset_id`, `error_code` |
| usage event | `provider`, `model`, `media_type`, `credits_delta`, `duration_ms` |

要求：

- 普通 logs 用于观测，append-only audit 才是审计真相源。
- 任意 failed media job 必须能从 `job_id` 追到 run、tool call、TokenRouter decision、worker log、usage event。
- 不记录 provider key、完整敏感 prompt、私有对象存储签名 URL。

### 4.3 明确不做

| 不做项 | 原因 |
|---|---|
| 完整 CometAPI | 第一版先用对象存储 + worker 预处理；长视频、社媒 URL、帧缓存成为瓶颈后再拆。 |
| fake job runner | MVP 必须证明真实链路，不能用假结果污染判断。 |
| 自动启动生成 | 成本不可控；live Atlas/provider 调用必须 opt-in。 |
| `default_api:*` 公开化 | 这是观察/来源命名，不是本地公开 API。 |
| Higgsfield 专名核心化 | 本地接口应 provider-neutral，Higgsfield 只作为 adapter。 |
| Soul ID 底层训练承诺 | LoRA / adapter / ControlNet 等没有验证前不能写成事实。 |
| Boost upsell / 并发售卖 | 并发上限作为 policy data，不做商业化 upsell 机制。 |
| 跨 region GPU 调度 | 第一版单 region / 单 queue / 单 worker 闭环。 |
| zero-upload feature-vector caching | 高复杂度，且有跨租户缓存安全风险。 |
| 普通 Docker 多租户执行 | 不能承载任意租户代码，必须走 microVM/Kata 级边界。 |
| bulk export Skill internals | 用户不能通过 file/export/terminal/connector 导出 protected `references/`。 |

## 5. 产品体验需求

### 5.1 Web Chat 页面

MVP 页面结构：

- 左侧：workspace/project/session 列表。
- 中间：真实 Agent chat transcript。
- 右侧：tool progress、assets、job status、structured prompt 面板。
- 顶部：模型/工具状态、预算/credits、provider 配置状态。

必须展示的事件：

- `message.start`
- `message.delta`
- `message.complete`
- `status.update`
- `tool.start`
- `tool.progress`
- `tool.complete`
- `tool.error`
- pending user question
- job queued/running/completed/failed

### 5.2 附件上传

流程：

1. 用户点击 paperclip 或拖拽文件。
2. 浏览器上传到 `POST /api/chat/uploads`。
3. 后端按 tenant/workspace/project/session 存储文件。
4. 返回真实 upload asset 和 attachment marker。
5. 前端把 marker 发送到 Hermes gateway。
6. Agent 看到的是受控引用，不是任意本地路径。

验收：

- 上传成功后能在 chat 和 assets 面板同时看到。
- 上传失败必须返回结构化错误。
- 上传路径不暴露给其他 tenant。
- 大文件不直接塞进 prompt context。

### 5.3 结构化澄清

`ask_user_question` 必须支持：

| 模式 | 用途 | UI |
|---|---|---|
| `text` | 普通澄清问题 | inline prompt / modal |
| `entity` | 选择 Soul ID、Element、voice、language 等实体 | side panel picker |
| `files` | 请求上传指定类型文件 | upload modal |

不允许所有选择都退化成自然语言问答，否则资产选择和权限校验会失控。

### 5.4 资产面板

资产类型：

- upload
- `media_input`
- `soul_id`
- `element`
- `image_job`
- `video_job`
- generated output

每个资产卡片至少展示：

- 名称
- 类型
- 状态
- 所属 project
- 创建来源
- 最近使用时间
- 可执行操作：reuse、save as Element、view lineage、delete/revoke

## 6. Agent 与工具合约

### 6.1 本地工具命名

| 来源工具名 | 本地 MVP 接口 | 说明 |
|---|---|---|
| `default_api:higgsfield_generate` | `media_generate` | provider-neutral 异步媒体生成。 |
| `default_api:higgsfield_job_status` | `media_job_status` | 统一 job 状态查询。 |
| `default_api:higgsfield_upload` | `media_upload` | 通用媒体上传。 |
| `default_api:higgsfield_soul_id` | `identity_reference_train/status/list` | 身份引用，不暴露 provider 专名。 |
| `default_api:higgsfield_element` | `asset_reference_create/list/get` | 角色、环境、道具等资产引用。 |
| `default_api:skills_list` | `skills_list` | Skill public metadata。 |
| `default_api:skill_view` | `skill_view` | 精确加载授权 Skill 文件。 |
| `default_api:ask_user_question` | `ask_user_question` | 结构化用户澄清。 |

### 6.2 工具错误原则

工具错误必须结构化、可见、可审计：

- provider 未配置：返回 `provider_not_configured`。
- 资产未 ready：返回 `asset_not_ready`。
- 资产越权：返回 `asset_access_denied`。
- quota 不足：返回 `quota_exceeded`。
- token 过期：返回 `token_expired`。
- schema 错误：返回 `invalid_tool_args`。

禁止：

- 静默 fallback。
- 伪造媒体结果。
- 用自然语言掩盖工具失败。
- 把 provider key 或内部 fetch 细节暴露给用户。

## 7. References 与 Skill 安全

### 7.1 三类 references

| 类型 | 含义 | 安全规则 |
|---|---|---|
| Skill 内部 `references/` | Skill 的深层技术规范、流程、schema、业务规则。 | protected operational content，不允许普通用户导出。 |
| 用户上传 references | 用户上传的图片、视频、PDF、音频等素材。 | 按 session/project/asset ACL 管理。 |
| 媒体生成 references | `media_input`、`image_job`、`video_job`、`soul_id`、`element_id`。 | 是资产引用，不是权限凭证，使用前查 ACL。 |

### 7.2 Skill 加载分层

| 层级 | 加载时机 | 内容 |
|---|---|---|
| system prompt 热层 | 每一轮都有 | 身份、安全、核心工具规范。 |
| `SKILL.md` 温层 | 命中技能时 | 触发条件、输入输出、主流程。 |
| `references/` 冷层 | 需要细节时 | schema、模型参数、工作流细节、业务 SOP。 |

`skill_view` 必须记录：

- `tenant_id`
- `workspace_id`
- `project_id`
- `session_id`
- `run_id`
- `skill_name`
- `reference_path`
- allow/deny reason

## 8. 资产与媒体 job

### 8.1 最小数据模型

| 表 | 关键字段 |
|---|---|
| `asset_references` | `id`, `tenant_id`, `workspace_id`, `project_id`, `type`, `name`, `status`, `created_by`, `created_at` |
| `asset_reference_sources` | `reference_id`, `source_asset_id`, `source_kind`, `lineage_order` |
| `identity_jobs` | `id`, `reference_id`, `status`, `training_type`, `input_count`, `started_at`, `completed_at`, `error_code` |
| `generation_jobs` | `id`, `model`, `media_type`, `status`, `params`, `output_asset_id`, `usage_event_id` |
| `asset_acl` | `asset_id`, `subject_type`, `subject_id`, `permission` |

### 8.2 状态机

Identity reference 状态：

- `queued`
- `not_ready`
- `in_progress`
- `completed`
- `failed`
- `revoked`

Generation job 状态：

- `created`
- `queued`
- `running`
- `completed`
- `failed`
- `cancelled`

### 8.3 资产权限规则

- asset ID 不是权限凭证。
- 每次生成前必须检查 asset ACL、tenant、workspace、project、status、revocation。
- prompt 文本不能绕过结构化 ACL。
- revoked / deleted / not_ready 资产不能被 job 消费。
- output asset 必须记录 lineage。

## 9. TokenRouter 与安全边界

### 9.1 最小职责

TokenRouter MVP 必须完成：

1. 校验 `HF_JWT_TOKEN`。
2. 提取 `tenant_id`、`workspace_id`、`project_id`、`session_id`、`tool_scopes`、`budget`、`exp`。
3. 调用 OPA 判断 allow/deny。
4. 检查 plan、quota、model allowlist、asset ACL。
5. 从 OpenBao / Vault 后端获取 provider key。
6. 代理 provider 调用或创建受控 job。
7. 记录 usage 和 audit。
8. 对过期、越权、缺 quota、vault failure fail closed。

### 9.2 隔离边界

| 边界 | MVP 要求 |
|---|---|
| 身份边界 | 不信任前端 tenant_id，所有 tenant/workspace/project/session 来自服务端 claims。 |
| sandbox 边界 | 任意代码/terminal/tool execution 走 microVM/Kata。 |
| 凭证边界 | sandbox 只有短期 token，没有 provider key。 |
| 数据边界 | tenant scoped 表必须有 tenant_id，PostgreSQL RLS 作为第二道防线。 |
| 文件边界 | Volume Mounter 只挂授权 path set，不挂 protected Skill internals。 |
| 资产边界 | `soul_id`、`element_id`、`media_input` 等使用前查 ACL。 |
| worker 边界 | worker 只拿 lease/input manifest 和 assigned output_prefix。 |
| 网络边界 | sandbox 默认只能访问 TokenRouter、egress proxy、event sidecar、volume broker。 |
| 审计边界 | 高风险调用 audit write failure 必须 fail closed。 |

## 10. 技术里程碑

### Milestone 0：PRD 与设计冻结

交付：

- 本 PRD。
- 系统对象模型草图。
- MVP 不做事项列表。
- 验收测试清单。

通过标准：

- 产品、工程、安全三方确认“不做什么”。
- 所有高成本项有明确 future parking lot。

### Milestone 1：Identity + Session + Web Chat

交付：

- OIDC 登录或本地模拟登录。
- tenant/workspace/project/session 数据模型。
- `/chat` 真实连接 Hermes gateway。
- session create/resume。
- message/tool/status event 渲染。

通过标准：

- 用户能打开 Web chat，发送真实 prompt，看到流式消息和工具事件。
- 没有 demo-only job endpoint。

### Milestone 2：Skill + References

交付：

- Skill registry。
- `skills_list`。
- `skill_view`。
- protected `references/` 访问控制。
- reference load audit。

通过标准：

- 普通用户可看 Skill 公开描述。
- 普通用户不能导出 protected `references/`。
- Skill runtime 可通过受信路径加载指定 reference。

### Milestone 3：Upload + Asset Service

交付：

- 文件上传。
- `asset_references` 和 ACL。
- Element create/list/get。
- Identity reference pending/status/list。
- Asset picker UI。

通过标准：

- 用户能上传素材并保存为 Element。
- 未授权或未 ready 资产无法被使用。

### Milestone 4：TokenRouter MVP

交付：

- `HF_JWT_TOKEN` 验证。
- OPA allow/deny。
- OpenBao / Vault provider key lookup。
- quota / model allowlist / asset ACL。
- usage / audit event。

通过标准：

- sandbox 中没有真实 provider key。
- 过期 token 在 provider 调用前被拒绝。
- Tenant A 不能访问 Tenant B 资产。

### Milestone 5：Media Job MVP

交付：

- `media_generate`。
- `media_job_status`。
- async job lifecycle。
- 单 worker 执行。
- output asset registration。
- job -> TokenRouter -> worker -> output -> usage trace。

通过标准：

- 用户能发起异步媒体 job，UI 看到状态和结果。
- failed job 能追踪到 TokenRouter decision、worker log、usage event。
- provider 未配置时返回可见错误，不伪造结果。

## 11. 成功指标

| 指标 | MVP 目标 |
|---|---|
| Chat 可用性 | session create/resume/send/tool event 基础链路稳定可跑。 |
| 工具透明度 | 工具失败 100% 有结构化错误，无 silent fallback。 |
| 资产闭环 | upload -> Element/Soul ID -> media_generate -> output asset -> reuse 可跑通。 |
| 隔离安全 | Tenant A 不能读取、挂载、复用 Tenant B 的 session/file/asset/object prefix。 |
| 凭证安全 | sandbox 和 mounted files 中没有 provider key。 |
| 审计追踪 | failed job 可从 job_id 追到 decision、worker、asset、usage。 |
| 成本控制 | live provider 调用默认 opt-in，不自动生成。 |

## 12. 验收标准

### 12.1 产品验收

- 用户能在 `/chat` 创建或恢复真实 Hermes 会话。
- 用户能看到流式消息、工具开始、工具进度、工具完成和工具错误。
- 用户能上传图片/视频/音频/PDF，并在会话中引用。
- 用户能用 picker 选择 Element / Soul ID / upload / generated job。
- 用户能看到 media job 的 queued/running/completed/failed。
- 用户能复用生成结果作为后续 reference。

### 12.2 安全验收

- 普通用户不能导出 protected Skill `references/`。
- `skill_view` 只能读取授权路径。
- prompt 中伪造 `element_id` / `soul_id` 不会绕过 ACL。
- revoked / deleted / not_ready asset 不能被 job 使用。
- sandbox 没有 provider key。
- provider key 只在 TokenRouter/OpenBao 边界内使用。
- audit write failure 对高风险调用 fail closed。

### 12.3 工程验收

- 工具 schema 拒绝 undeclared fields。
- 所有工具错误结构化返回。
- `media_generate` 返回 job_id。
- `media_job_status` 返回稳定状态枚举。
- job output 注册为 asset，并记录 lineage。
- failed job 可追踪到 run_id、tool_call_id、job_id、tenant_id、worker log、usage_event_id。

## 13. 待验证问题

- Higgsfield 真实 web transport 是 SSE、WebSocket、Vercel AI SDK Data Stream，还是混合。
- `higgsfield_*` 工具真实 schema 是否存在公开 manifest。
- Soul ID 当前最小照片数、文件上限、状态枚举。
- TokenRouter 是否为 Higgsfield 内部真实服务名，还是架构解释名。
- CometAPI 是否为生产服务，还是设计名。
- Asset ref 的稳定内部类型名是否为 `image_job`、`video_job`、`media_input` 等。
- Connector OAuth scope、刷新、rate limit、错误模型。
- Scheduling 的 retry、timezone、skipped-run 行为。

## 14. Future Parking Lot

未来再评估：

- 完整 CometAPI 媒体数据面。
- 长视频 URL 解析、抽帧、转码和缓存。
- Boost concurrency 产品化。
- 多 region GPU 调度。
- Kueue fairness / preemption / spot-on-demand。
- Soul ID 真实训练后端。
- Skill package signing。
- protected bundle red-team。
- per-tenant encryption context。
- zero-upload reusable media cache。
- Scheduled publishing。
- Connectors OAuth 大规模接入。

## 15. 决策记录

| 决策 | 结论 |
|---|---|
| MVP 形态 | 真实 Web Agent + Skill + 资产/媒体 job 最小纵向闭环。 |
| 接口命名 | provider-neutral，本地用 `media_generate`、`asset_reference_*`、`identity_reference_*`。 |
| 安全优先级 | provider key、asset ACL、sandbox、protected references 是首版强边界。 |
| CometAPI | 不进首版默认能力，只作为 future media gateway。 |
| Soul ID | 首版实现资产状态和引用模型，不承诺底层训练实现。 |
| 生成成本 | live provider 调用 opt-in，不自动生成。 |
