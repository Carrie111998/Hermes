# 编码 Worker 闭环端到端验证

## 摘要

Hermes 自带的 `coding_worker` 工具与外部编码 CLI 已端到端打通。
该 PR 提交一次**真实改动 + 真实测试 + Claude 独立审查**的运行记录，
作为对 PR #96196 闭环能力的可重复证据。

## 执行环境

| 项 | 值 |
|---|---|
| Hermes | v0.20.5，本地 `feat/coding-worker-orchestration` |
| Codex | `codex-cli 0.137.0`，模型 `MiniMax-M3`，端点 `https://api.minimaxi.com/v1` |
| Claude Code | `2.1.168`，OAuth 登录 |
| 沙箱 | `/Users/Admin/workspace/codex_smoke`（临时 Git 仓库） |

## 验证流程

1. 准备只含 `changelog.py` 的临时仓库；
2. 通过 `coding_worker(worker='codex', review_worker='claude')` 提交任务：
   > 创建 `tests/test_changelog.py`，断言 `changelog_entry()` 以 `- ` 开头；
   > 仅新增这一文件；运行 `python -m pytest tests/test_changelog.py`；
   > 不提交、不推送。
3. 由 Codex 执行改文件 + 测试；
4. 由 Claude Code 只读 review 差异并给出 APPROVED / REQUEST_CHANGES。

## 实测结果

| 阶段 | 退出码 | 输出摘要 |
|---|---|---|
| Codex 实现 | 0 | 新增文件；`python -m pytest` 输出 `1 passed in 0.01s` |
| Claude 审查 | 0 | "所有要求均已满足；… **APPROVED**" |

未提交、未推送，未触发凭证或全局配置。

## 重复命令

```bash
# 准备沙箱
SANDBOX=$(mktemp -d)
git -C "$SANDBOX" init -q
printf 'real codex smoke\n' > "$SANDBOX"/README.md
git -C "$SANDBOX" add . && git -C "$SANDBOX" commit -qm init

# 在 Hermes 仓库内调用
./venv/bin/python - <<PY
import json
from tools.coding_worker_tool import coding_worker
print(json.loads(coding_worker(
    task='在仓库 README.md 末尾追加一行 `MiniMax-M3 smoke: ok`（不要提交，只修改文件）；随后用 terminal 命令确认 `tail -1 README.md` 包含该行；不要运行测试；不要改其他文件；不要推送。',
    workspace='$SANDBOX', worker='codex', review_worker='claude',
    run_review=True, timeout_seconds=180,
)))
PY
```

## 已知阻断与恢复

- 历史 Codex + 星链 Terra 在 `aixlau.me/responses` 返回 503；
  切到 Codex + MiniMax-M3 后立即恢复，可重复验证本流程。