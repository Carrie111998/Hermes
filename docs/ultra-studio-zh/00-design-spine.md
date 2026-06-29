# Ultra Studio 设计主线

状态：目标架构 + P0 实施基线
日期：2026-06-15

## 目的

这份文档是 Ultra Studio 的设计主线。它回答三个问题：

- 我们到底要把 Hermes fork 改造成什么产品。
- 哪些能力是 P0 必须跑通的真实闭环。
- 哪些能力只是目标架构，不能误写成当前已实现。

后续 PRD、组件规格、权限设计、API 契约和实施路线都应回到这里对齐。

## 产品边界

Ultra Studio 是一个 Atlas-first 创作型 Agent。它优先服务图片、视频、资产复用、角色一致性和创作工作流，不是通用 Hermes 管理台，也不是 fake media demo。

第一版只证明一条真实链路：

```text
User Message
  -> Gateway / Session
  -> Workflow Router
  -> Tool Intent
  -> Policy Preflight
  -> Approval if needed
  -> Atlas Media Job
  -> Asset Register
  -> Event Stream
  -> History / Reuse
```

## 当前状态

| 模块 | 当前状态 | 说明 |
|---|---|---|
| Web Chat | partial | 有真实聊天与上传雏形，但不是完整产品运行时。 |
| Workflow Router | partial / specified | 有设计和部分相邻逻辑，仍需变成稳定 contract。 |
| Prompt Compiler | specified, not built | 需要把 skill brief 编译成工具参数，不靠自然语言裸传。 |
| Atlas Image / Video Tools | partial | 已有 Atlas provider 方向，但需要统一 job envelope 和事件。 |
| Media Job Service | partial / specified | 有提交和轮询雏形，缺 durable job 记录和完整状态流。 |
| Asset Service | specified, not built | 资产、lineage、ACL、audit、collection、smart group 尚未落地。 |
| TokenRouter | specified, not built | 目标控制面；P0 可用进程内 PolicyChecker 替代。 |
| CometAPI | future | 长视频、帧采样、多模态预处理数据面，不进 P0。 |
| Approval Gateway | partial | 已有 clarify / pending prompt 机制，缺 durable decision record。 |
| Audit Chain | specified, not built | 需要串起 session、run、tool call、decision、job、asset、usage。 |

## P0 闭环

P0 只做真实可用，不做完整云平台。

1. 用户打开创作聊天界面。
2. 用户输入图片或视频生成需求。
3. 用户可以上传或选择当前 project 内的资产。
4. Workflow Router 产出结构化 intent，而不是直接触发生成。
5. Prompt Compiler 把 intent 编译成工具参数。
6. PolicyChecker 做最小权限、能力、模型和成本预检。
7. 高风险或高成本动作进入 Approval Gateway。
8. Atlas adapter 创建真实 media job。
9. job 状态通过事件流进入 UI。
10. 输出注册为 asset，并保存 lineage。
11. 历史会话可以恢复。
12. asset 可以 inspect、reuse、download。

## P0 不变量

| 不变量 | 含义 |
|---|---|
| 不做 fake job | UI 不能展示假成功或硬编码生成结果。 |
| prompt 不是权限 | prompt 中裸写 asset id 不代表授权。 |
| UI 不是权限源 | 前端只展示 allowed operations，不做最终授权。 |
| Router 不判权限 | Workflow Router 只判断意图和缺字段。 |
| Worker 不拿 key | provider key 不能进入 worker payload、浏览器、日志或资产上下文。 |
| 失败要结构化 | quota、ACL、provider、approval、job 失败都必须有稳定错误码。 |
| 结果要可追踪 | failed job 必须能追到 run、tool call、policy decision 和 worker log。 |

## 架构分层

```text
Product UI
  Chat, Inspector, Asset Library, History

Gateway / Session
  identity binding, session create/resume, event stream

Agent Runtime
  workflow router, prompt compiler, tool intent

Control Plane
  policy checker, approval, tokenrouter target, quota, audit

Execution Plane
  media job service, worker, Atlas provider adapter

Data Plane
  asset service, object store, lineage, search, collection, smart group

Security / Ops
  credential boundary, sandbox, logs, decision records, recovery
```

## Source of Truth

| 合约 | 权威来源 |
|---|---|
| 会话状态 | Session / Gateway |
| 工作流意图 | Workflow Router |
| 工具参数 | Prompt Compiler 输出 |
| 授权决策 | PolicyChecker / TokenRouter |
| 人工确认 | Approval Gateway |
| 作业状态 | Media Job Service |
| 资产状态 | Asset Service |
| 二进制文件 | Object Store |
| 搜索结果 | 可重建索引，不是权威状态 |
| UI 展示 | 后端事件和查询结果的投影 |

## P1 / P2 方向

P1 产品化：

- 资产库完整 UI。
- Collection 和 Smart Group。
- Character / Element / Soul ID 的真实状态。
- 更多视频 workflow skill。
- 更完整的 durable approval 和 audit 页面。

P2 云化：

- TokenRouter service。
- OPA / Vault / Redis quota。
- CometAPI 媒体数据面。
- 多租户隔离和账单。
- 沙箱生命周期、worker 池和运营监控。

## 非目标

P0 不做：

- 完整 CometAPI。
- 完整 Vault / OPA / Redis quota。
- 跨 tenant 云商业计费。
- 公开 Marketplace。
- 任意长视频社媒 URL 抓取。
- 真实 Soul ID 训练承诺。
- worker 直接持有 provider key。
- 用 prompt 文本绕过结构化 asset ref。

## 判断标准

一个功能能进入 P0，必须满足：

1. 服务真实创作闭环。
2. 有明确 owner 和状态源。
3. 有结构化 API 或事件。
4. 有权限和错误边界。
5. 能被本地测试或手工验证证明。

不满足这些条件的能力进入 P1/P2，或者只保留为研究文档。
