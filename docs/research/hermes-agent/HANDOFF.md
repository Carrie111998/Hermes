---
title: "Latest Architecture Study Handoff"
status: active
source_commit: 26350357d7
previous_commit: dd0827710
updated_at: 2026-08-30
---

# 最新会话交接

## 当前状态

- Branch：`docs/hermes-architecture-deep-dive`
- Baseline：`26350357d7`（2026-08-30 同步自上游 `main`；上一基线 `dd0827710`）
- Milestone：M3 前置 — 基线重验（M3 本体 blocked）
- Working document：[BASELINE.md](./BASELINE.md) 的受影响模块表

## 本次已完成（会话 07）

- 加固 remote 配置：`upstream` 推送地址设为 `no_push`，`gh` PR base 改为个人 fork，杜绝误推/误开 PR 到 `NousResearch/hermes-agent`。
- `main` fast-forward 到上游 `26350357d7`（5,922 commits，无本地独有提交）。
- 研究分支合并 `main`，零冲突；`AGENTS.md` 的研究段落与 `docs/research/hermes-agent/` 29 个文件完好。
- 用统一口径回算新旧基线规模指标，并在 `BASELINE.md` 中固化计数方法。
- 按路径变更量确定受影响模块，M1/M2 全部领域均有实质变更。
- 按基线更新协议分层标记文档：10 份代码断言类转 `needs revalidation` + `revalidation_target`，3 份管理类推进 `source_commit`。
- 更新 `BASELINE.md`、`PROGRESS.md`，新增 `journal/2026-08-30-session-07.md`。

## 尚未完成

- **推送被阻塞**：`main` 与研究分支均未推到个人 fork。`gh` token scopes 为 `gist, read:org, repo`，缺 `workflow`，而本批 commit 修改了 `.github/workflows/ci-review-comment.yml`。环境中无 SSH key。
  解封：用户在交互式终端执行 `gh auth refresh -h github.com -s workflow`，随后 `git push origin main` 与 `git push -u origin docs/hermes-architecture-deep-dive`。
- M1/M2 全部结论尚未按 `26350357d7` 复核。
- `OPEN-M2-001` 尚未通过 real SessionDB cold-resume test 复现，且在新基线上是否仍存在未取证。
- 工作区仍未发现项目 `.venv`/`venv` pytest executable；至今只阅读了行为测试源码，未运行任何测试。

## 下次会话的准确动作

1. 读取本目录的 `README.md`、`PROGRESS.md`、`BASELINE.md` 和本文件。
2. 确认推送是否已解封；未解封则先提示用户执行 `gh auth refresh -s workflow`。
3. 按 `BASELINE.md` 受影响模块表复核 M1：优先 `hermes_cli/`（1,118 commits）与 `apps/desktop/`（1,708 commits），核对 `architecture/process-model.md` 的入口与 ownership 结论。
4. 复核 M2：核对 `flows/canonical-cli-turn.md` 与 `flows/canonical-tool-turn.md` 的符号名与调用顺序，重点 `cli.py`（+4,346 行）、`tools/`、`agent/`。
5. 逐条复核 `INVARIANTS.md`，区分仍成立、已变更、已失效。
6. 每份复核通过的文档推进 frontmatter `source_commit` 到 `26350357d7` 并移除 `revalidation_target`。
7. M1/M2 复核完成后解除 M3 blocked，进入 `agent/conversation_loop.py` 状态机（该文件自旧基线起被 90 个 commit 修改）。

## 工作区提示

研究分支应包含 `ea3bfe794`、`ab0e14d73`、`2c39face3`、`4dabf7827`、`6fd28cf63`、`0d7915c6f`，以及同步合并 commit `149bc07446`。继续前运行 `git status --short --branch` 与 `git log --oneline -3`。
