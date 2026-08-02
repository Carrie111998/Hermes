# 芯相多 Agent 记忆：三阶段可回滚路线

## Stage 0：契约冻结

Stage 1 只增加一个可选插件，不改变 Hermes 内置 memory provider、session
search、模型调用链或任何 Agent 的 `SOUL.md`、`MEMORY.md`、`USER.md`。

共享边界必须显式写在每个参与 profile 的 `config.yaml`：

```yaml
plugins:
  enabled: [team-memory]
team_memory:
  enabled: false
  workspace_id: xinxiang
  project_id: xinxiang-app
  database_path: /Users/xinxin/.hermes/team-memory/xinxiang.db
```

同一 `database_path` + `workspace_id` 才表示 Frontend、Backend、DevOps
共享一个工作区。缺省路径仍是当前 profile 下的
`$HERMES_HOME/plugins/shared_memory.db`，不会意外跨 profile 共享。

## Stage 1：已实现的 SQLite + FTS5 MVP

实现位置：

- `plugins/team_memory/plugin.yaml`：标准插件清单。
- `plugins/team_memory/__init__.py`：使用 `PluginContext` 注册 CLI 和 gated tool。
- `plugins/team_memory/storage.py`：schema、迁移、scope、FTS5、CJK fallback、过期审计、指标。
- `plugins/team_memory/tool.py`：只读 `team_memory_search`，feature flag 关闭时不进入 tool schema。
- `plugins/team_memory/cli.py`：`hermes team-memory init/status/search/list/add/delete/migrate/metrics/uninstall`。
- `tests/plugins/test_team_memory_plugin.py`：真实插件发现、registry、CLI、scope、中文、FTS 更新/删除、过期和迁移测试。

Agent 只能搜索；写入由操作者通过 CLI 完成。每条记录包含
`workspace_id`、`project_id`、`memory_key`、来源、审核状态和有效期。内容返回
有上限，metrics 使用独立 SQLite 文件并记录 `agent_variant`，不会把指标锁带入主搜索库。

## 开启与回滚

```bash
hermes plugins enable team-memory
hermes config set --force team_memory.enabled true
hermes config set --force team_memory.workspace_id xinxiang
hermes team-memory init --workspace xinxiang
```

关闭时改为 `false`，然后启动新 Hermes 进程/新会话。活跃会话不热替换工具表，
从而不破坏 prompt cache 和工具快照稳定性：

```yaml
team_memory:
  enabled: false
```

删除 Stage 1 数据必须显式确认。`list` 默认只显示有效记录，审计过期契约时使用
`--include-expired`：

```bash
hermes team-memory list --workspace xinxiang --include-expired
hermes team-memory uninstall --yes
```

配置恢复使用既有备份演练，不复制或提交包含 `auth.json` 的备份目录。

## Stage 1 验收门

必须同时满足：真实插件发现成功；FTS 英文和 CJK 查询成功；旧内容更新后不再命中；
workspace/project 不能越界；关闭 flag 后 schema 消失；个人 memory、session search、
Agent Markdown 文件不变；临时 `HERMES_HOME` smoke test 通过。

## Stage 2：证据驱动的 Profile 过滤

只有 Stage 1 产生至少 30 对真实 Agent A/B 运行，并且人工抽样确认检索结果质量后，
才考虑把 profile 的角色/项目过滤加入查询路由。仍使用当前插件接口和 SQLite，新增字段
必须可迁移；不引入 Neo4j、向量库、NER/RE 训练或消息队列。

## Stage 3：按需关联能力

只有出现稳定的多跳查询需求，且 FTS + 反向链接 + 标签无法满足，才评估关系表或图存储。
每个候选特性单独 feature flag、单独数据迁移和单独回滚，不允许一次性切换记忆引擎。
