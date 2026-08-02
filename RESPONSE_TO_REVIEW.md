# 对评审意见的落地回应

评审结论被采纳：第一阶段不做知识图谱、向量库、NER/RE 训练或消息队列，先用成熟的
SQLite + FTS5 验证价值，并把投入拆成可快速回滚的插件。

本次修复补齐了原候选方案的工程缺口：

- 修复 FTS 查询错误和 external-content UPDATE 残索引。
- 改用 Hermes 原生 `plugin.yaml` + `register(ctx)`，工具通过 `check_fn` gated。
- CLI 改为无冲突的 `hermes team-memory`，不覆盖现有 `hermes memory` provider 命令。
- 增加 workspace/project/source/review/expiry 字段，默认不跨 profile 共享。
- 增加中文 fallback、结果长度上限、SQLite busy timeout/WAL 和独立 metrics DB。
- Agent 只读；人工 CLI 写入并有删除确认。
- A/B 从模拟器改成真实隔离 Hermes 进程；少于 30 对样本不允许做 Go 结论。
- 测试始终使用临时 `HERMES_HOME`；备份目录和 `auth.json` 不进入 Git。

成本/ROI 不在代码中承诺。只有真实运行数据、人工相关性抽样和独立统计复核后，才决定
是否进入 Profile 过滤或更复杂的关联存储。
