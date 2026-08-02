# Agent 级 A/B：真实运行证据

## 重要约束

旧版本的 A/B 脚本会固定返回“Baseline 2 轮、Enhanced 1 轮”，那是模拟器，不能
作为记忆有效性的证据。当前实现已经改成真实子进程 runner：

```bash
python experiments/team_memory_ab_test/scripts/run_experiment.py \
  --source-home /absolute/path/to/a/copy-of-hermes-home
```

`--source-home` 必须显式提供。runner 为每个任务和每次 repetition 创建 baseline/enhanced
两个临时 `HERMES_HOME`，复制相同的配置/凭据输入，设置相同的 workdir 和 prompt，分别
启动 Hermes `-z`。Enhanced 只额外启用 `team-memory` 并加载审阅过的种子库；不会修改真实
profile，也不会重启生产 gateway。每个进程退出后会删除临时 home 中复制的 `auth.json`
和 `.env`，因此 `--keep-temp` 也不会保留凭据。

## 测试矩阵

固定 20 个任务，覆盖 Backend、Frontend、DevOps；默认每个任务重复 2 次，得到 40 对
配对运行。每个 repetition 的两个 arm 使用同一 prompt 和显式关键词 oracle，且 baseline
和 enhanced 的先后顺序确定性轮换。每个进程记录：

- Hermes return code、完成状态和错误；
- API calls、total tokens、总耗时；
- `team_memory_search` 实际调用次数；
- 关键词命中/缺失、响应 hash 和有限预览。

响应预览只进入本地实验报告，不提交 Git。报告不保存密钥，metrics 也会对常见
`api_key/token/password/secret` 值做脱敏。

## 决策规则

默认至少 30 对配对运行。默认配置的 40 对样本超过这个门槛；若手动降低 repetition
或任务数，少于该数量时报告是
`insufficient_sample`，不能写成“显著提升”。当前 runner 不伪造 p-value；模型、
网络和 provider 噪声需要在独立统计复核中处理。候选 Go 仅表示样本足够、Enhanced
成功率不低于 Baseline 且中位耗时下降，仍需人工抽样检查检索准确率和错误内容。

先 dry-run 确认任务数，不调用模型：

```bash
python experiments/team_memory_ab_test/scripts/run_experiment.py \
  --source-home /absolute/path/to/profile --repetitions 2 --dry-run
```

## 回滚

A/B 只写临时目录，结束后默认删除。Stage 1 线上开关仍由新进程读取
`team_memory.enabled: false` 控制；A/B 不改变这个开关，也不修改任何 Agent Markdown。
