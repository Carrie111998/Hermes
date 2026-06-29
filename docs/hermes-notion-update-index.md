# Hermes Notion 更新索引

状态：已合并 Notion 更新
源快照：`docs/notion-source/hermes/`
Notion 根页面：`Hermes`
刷新日期：2026-06-02

## 用途

这份索引说明 Notion 更新如何进入本地文档体系。它不是大而全的合并稿，而是把新增内容按架构边界拆成子文档，避免把 `references`、TokenRouter、CometAPI、Soul ID、Element、tool contract 混在同一个 spec 里。

## 源快照

| Notion 页面 | 本地源文件 |
|---|---|
| Hermes root | `docs/notion-source/hermes/pages/00-hermes.md` |
| soulID | `docs/notion-source/hermes/pages/01-soulid.md` |
| 测试实际模型 | `docs/notion-source/hermes/pages/02-测试实际模型.md` |
| 复现方式 | `docs/notion-source/hermes/pages/03-复现方式.md` |
| 资产管理 Element Management | `docs/notion-source/hermes/pages/04-资产管理-element-management.md` |
| 工具调用说明文档 | `docs/notion-source/hermes/pages/05-工具调用说明文档.md` |
| tokenrouter | `docs/notion-source/hermes/pages/06-tokenrouter.md` |
| CometAPI | `docs/notion-source/hermes/pages/07-cometapi.md` |

## 已更新文档

| 主题 | 本地文档 | 为什么拆开 |
|---|---|---|
| Hermes `references` 知识模型 | `docs/hermes-references-knowledge-model.md` | 这是 Agent 认知层和 Skill 安全边界，不属于媒体资产或网关 spec。 |
| TokenRouter 凭证流 | `docs/hermes-tokenrouter-credential-flow.md` | 这是控制面、凭证、quota、audit 边界，需要单独作为安全设计文档。 |
| CometAPI 媒体网关 | `docs/hermes-cometapi-media-gateway.md` | 这是媒体数据面和多模态预处理，不应塞进 TokenRouter 文档。 |
| Soul ID 与 Element 资产模型 | `docs/hermes-soulid-element-asset-model.md` | 这是持久化语义资产模型，直接影响工具、数据库和生成链路。 |
| Soul ID 测试与复现计划 | `docs/hermes-soulid-reproduction-and-test-plan.md` | 测试实际模型和开源复现是实验计划，不是生产架构事实。 |
| Notion 工具合约映射 | `docs/hermes-tool-contracts-from-notion.md` | 工具 schema 是接口清单，应该和高层架构分开维护。 |
| HTML 导航页 | `docs/hermes-notion-update-index.html` | 给浏览器阅读用的拆分入口。 |

## 已触达的原有文档

| 原有文档 | 更新规则 |
|---|---|
| `docs/hermes-open-source-architecture-plan.md` | 只补充 Notion refresh addenda 和相关子文档链接，不展开全部内容。 |
| `docs/higgsfield-supercomputer-dialogue-architecture-research.md` | 只加入 Notion source map 和把新结论指向子文档；继续保留 evidence policy。 |
| `docs/hermes-real-chat-agent-ui.md` | 只更新 attachment/reference 的 UI 边界，不改变真实 Hermes chat contract。 |

## 拆分决策

- `references/` 是 Hermes Agent Runtime 的知识分层机制：System Prompt 是热层，`SKILL.md` 是温层，`references/` 是冷启动/惰性加载层。
- 用户上传的 `<attached_references>` 是会话输入素材，不等同于 Skill 内部 `references/`。
- 生成链路里的 `image_job`、`video_job`、`media_input`、`soul_id`、`element_id` 是媒体资产引用，不等同于文档知识库。
- TokenRouter 是控制面和凭证交换边界；CometAPI 是媒体数据面和物理预处理边界。
- Soul ID/Element 是资产管理模型；Soul ID 的底层 LoRA/adapter 细节在未验证前只作为 Notion/实验假设记录。
- `default_api:*` 工具清单进入 tool contract 文档；MVP 实现时应映射为本地 Hermes tool names，而不是直接照抄外部命名。

## 实现优先级

1. 先做 `references`/Skill 的本地加载、权限和导出保护。
2. 再做 tool contract 的本地映射：`ask_user_question`、`higgsfield_generate`、`higgsfield_soul_id`、`higgsfield_element`、`video_analyze`。
3. 接着做 TokenRouter 的 JWT、quota、provider key blind-state 设计。
4. 最后再评估 CometAPI 是否进入 MVP；本阶段可以先保留为 future media gateway。

## 验证备注

- Notion 源内容已保存到本地，但 Notion API token 不写入这些文档。
- Notion 中的 `[LITERAL PROMPT]` 和 `[INFERENCE]` 等标签在相关位置保留为证据标签。
- 任何关于 Higgsfield 生产内部实现的说法，在成为实现依赖前仍需要 API、网络或源码证据。
