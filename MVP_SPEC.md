# Stage 1 MVP 实际规格

## 目标

为芯相的 Frontend、Backend、DevOps Agent 提供一个受控的、可审阅的共享
决策/API/规范检索面。它不是个人记忆 provider，也不自动修改 Agent Markdown。

## 运行契约

插件是 `plugins/team_memory`，通过 Hermes 原生插件发现加载。启用需要：

```yaml
plugins:
  enabled: [team-memory]
team_memory:
  enabled: true
  workspace_id: xinxiang
  database_path: /absolute/path/xinxiang.db
```

`database_path` 是显式共享边界；profile 默认不会互相读取。`workspace_id` 是每次
查询、写入、指标读取的强制过滤条件。

## 数据表

主表 `shared_memory` 使用 SQLite，字段包括：

| 字段 | 用途 |
| --- | --- |
| `workspace_id` | 多 profile 的共享工作区边界 |
| `project_id` | 同一工作区内的项目过滤 |
| `memory_key` | 幂等种子和人工更新键 |
| `category` | `architecture_decision` / `api_contract` / `best_practice` 等 |
| `title`, `content` | 可检索内容 |
| `author`, `source_type`, `source_ref` | 来源追踪 |
| `review_status` | `approved` / `draft` / `archived` |
| `valid_until` | 规范化为 UTC；过期契约不再返回，operator 可审计列出 |
| `created_at`, `updated_at` | 时间审计 |

`shared_memory_fts` 是 external-content FTS5 表。初始化/迁移会重建索引；UPDATE
使用 FTS5 的 delete + insert 事件，避免旧 API 契约残留在索引中。

## Agent 工具

只有 `team_memory_search(query, category)`。工具：

- flag 未开启或 workspace 未配置时不出现在工具 schema。
- 只搜索当前 workspace，project 由 profile 配置约束。
- 英文/数字走安全的 FTS5 prefix query；中文无空格时走有界 `LIKE` fallback。
- 每条内容和总结果均有长度上限。
- 搜索失败返回不泄露 SQL/路径的错误码。
- metrics 失败不影响 Agent 主任务。

## Operator CLI

```text
hermes team-memory init --workspace <id>
hermes team-memory status [--workspace <id>]
hermes team-memory add --workspace <id> --category ... --title ... --content ... --author ...
hermes team-memory search <query> --workspace <id>
hermes team-memory list --workspace <id> [--include-expired]
hermes team-memory delete <id> --workspace <id> --yes
hermes team-memory migrate --workspace <id>
hermes team-memory metrics --workspace <id>
hermes team-memory uninstall --yes
```

Agent 没有写入工具；人工录入是 Stage 1 的质量闸门。种子加载使用稳定
`memory_key`，可重复运行而不重复插入。

## 非目标

Stage 1 不做知识图谱、向量检索、自动 NER/RE、消息队列、自动写入、APM 深度关联、
多模态统一查询或活跃会话热切换。
