# AgentOps Phase 1 运行、迁移与回滚设计

| 字段 | 内容 |
|---|---|
| 范围 | Phase 1 control-plane foundation |
| 状态 | 实施设计；本文件不安装服务、不执行迁移、不修改任何现有运行面 |
| 默认状态目录 | `~/.hermes/agentops/`，仅在 Owner 显式启动 daemon 后创建 |
| 默认权限 | `observe_only` |

## 1. Phase 1 可写边界

在本阶段，控制面只可在显式配置的 AgentOps state directory 内创建下列文件：`state.db`、SQLite WAL/SHM、`event-spool/`、`event-spool/quarantine/`、短期备份文件和 UDS socket。它不得写入 Hermes 的 session `state.db`、Cron jobs、配置、日志、Gateway、LaunchAgent、代码仓库或业务数据。

插件 import 和 `agentops doctor` 均不得创建状态目录、数据库、socket 或后台线程。只有人工显式运行 `hermes agentops daemon --config <path>`（并在插件已被手动启用）才允许创建 AgentOps 自身状态。

## 2. 数据库迁移与备份

1. Store 以 `schema_migrations` 的单调整型版本确认数据库版本。
2. 新数据库从空状态创建 schema v1，并启用 WAL、foreign keys 和 `busy_timeout`。
3. 将来升级已有数据库前，Store 使用 SQLite backup API 在同一 AgentOps 受控目录生成时间戳备份，完成校验后才运行迁移事务。
4. 发现未知的未来 schema、损坏数据库或迁移异常时，daemon 不尝试修复或降级；它在 health 中记录安全启动原因，保持 `observe_only`。
5. Phase 1 不承诺 downgrade migration。回滚采用停止对应 daemon、验证备份、恢复 AgentOps `state.db`，然后以旧二进制重新启动。恢复动作只可针对配置中的 AgentOps SQLite 路径，不能引用 Hermes 现有 `state.db`。

## 3. Event spool 恢复

Event 先由 Producer 写入 AgentOps 自身 spool；文件名为 event ID，使用同目录临时文件和原子 replace。daemon 启动时按稳定顺序重放：

1. 用 schema-v1 与 secret gate 验证事件。
2. 通过 `event_id` 向 SQLite 幂等 append。
3. 成功或重复后删除 spool 文件。
4. 未知 schema、损坏 JSON 或无效事件进入 quarantine；若原始内容包含 Secret，只保存哈希和理由的脱敏 metadata，不保留原文。

spool 只提供本地崩溃恢复，不能触发 Target 行为。spool 超过配置预算时拒绝新事件并将控制面标为 degraded/observe-only，不丢失静默地转为写修复。

## 4. daemon 启动、停止和未来 launchd

Phase 1 仅允许测试或人工前台 daemon。没有 plist、没有 `launchctl bootstrap`、没有自动启动。

未来（不在本阶段）的 launchd 设计为：

- Label：`ai.hermes.agentops-control`；
- 独立固定 Python/venv 和已冻结的 release 目录；
- `KeepAlive` 只恢复 AgentOps 进程，不赋予重启任何 Target 的权限；
- ProgramArguments 只允许 `hermes agentops daemon --config <managed-config>`；
- 启动前验证 `0700` state directory、`0600` UDS、配置哈希和唯一进程/控制器锁；
- 安装、升级、卸载与现有 `ai.hermes.gateway*` 和 `com.molly.hermes-ai-native.*` 服务严格分离。

在 G4 前，未来 service 即使被安装也只能是 `observe_only`；它不能替换当前 `hermes_gateway_watchdog.py` 或其它 watchdog 的写职责。

## 5. 卸载与应急回滚

**正常卸载：** 先停止 AgentOps daemon；确认 UDS 不再监听；保留 `state.db`、spool、备份与审计导出；仅在 Owner 明确选择后移除 AgentOps 自身程序或 state directory。不会停止任何 Hermes Gateway 或删除任何既有服务。

**Phase 1 回滚：** 停止测试 daemon，删除其临时 socket，恢复该测试/AgentOps Store 的最近验证备份；如果没有 AgentOps 服务，则无需触碰 launchd。Gateway 继续由已有 launchd/watchdog 管理。

**安全事件：** 如果发现审计链异常、Secret 持久化、未知控制器或 UDS 权限不符合预期，停止 daemon（或保持无 store health）、保留脱敏证据，禁止进一步执行；本阶段不存在需要自动回滚的 Target 写动作。

