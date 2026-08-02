# Stage 1 评估与回滚框架

## 评估对象

评估的是 `team_memory_search` 是否让真实 Agent 在芯相跨角色协作任务中更快找到
已审阅的 API 契约、架构决策和工程规范。它不评估个人 memory provider，也不要求
用户参与或感知实验。

## 指标

| 指标 | 采集方式 | 用途 |
| --- | --- | --- |
| Tool exposure | registry schema | flag 关闭后应为 0 |
| Search latency | 独立 metrics DB | 观察 FTS/CJK fallback 成本 |
| Retrieval count | metrics DB | 观察 Agent 是否实际使用 |
| Result relevance | 人工抽样/任务 oracle | 防止错误契约被采用 |
| Task success | 同 prompt 配对运行 | 观察业务结果 |
| API calls/tokens/time | Hermes `--usage-file` + stopwatch | 观察效率和成本 |
| Scope violations | workspace/project 负向测试 | 安全闸门 |

## 验收门

合并前必须通过：真实插件发现、临时 `HERMES_HOME`、英文和中文检索、FTS 更新/删除、
分类/项目/workspace 隔离、feature flag schema gating、CLI 解析和配置备份恢复演练。

真实 A/B 使用 20 个固定任务，默认每个任务重复 2 次，共 40 对样本；决策门槛仍是
最少 30 对真实进程。样本不足只记录为观察结果，不作 ROI、显著性或阶段 2 的承诺。

## 回滚演练

1. 将每个相关 profile 的 `team_memory.enabled` 改为 `false`。
2. 启动新 Hermes 进程，确认 `team_memory_search` 不在工具 schema。
3. 不重建正在运行的会话，不重启无关 gateway。
4. 如需删除数据，仅运行 `hermes team-memory uninstall --yes`，目标由当前配置解析。
5. 恢复配置前先解析 YAML，再运行 `hermes dump` 和插件/CLI smoke test。

个人 memory、session search、`SOUL.md`、`MEMORY.md`、`USER.md` 和其他 profile 数据
不在回滚目标内。
