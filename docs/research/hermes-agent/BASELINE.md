---
title: "Hermes Architecture Study Baseline"
status: active
source_commit: 26350357d7
previous_commit: dd0827710
verified_at: 2026-08-30
---

# 研究基线

## 当前快照

| 项目 | 值 |
|---|---|
| Repository | `NousResearch/hermes-agent`（经个人 fork `superyin37/hermes-agent` 同步） |
| Study branch | `docs/hermes-architecture-deep-dive` |
| Source branch | `main` |
| Source commit | `26350357d7` |
| Previous baseline | `dd0827710` |
| Python package version | `0.20.6` |
| Python requirement | `>=3.11,<3.14` |
| Snapshot date | `2026-08-30` |

## 规模指标

以下数字用于理解研究范围，不作为稳定行为契约。

**计数口径**（跨基线可比的前提，务必沿用）：文件集合取 `git ls-files`（历史基线用 `git ls-tree -r --name-only <commit>`）；行数为裸 `wc -l`，不排除空行与注释；测试模块匹配 `(^|/)test_[^/]*\.py$`；前端测试匹配 `\.(test|spec)\.(ts|tsx|js|jsx)$`；bundled skills 取 `skills/`，optional skills 取 `optional-skills/`。

| 指标 | `dd0827710` | `26350357d7` | 变化 |
|---|---:|---:|---:|
| Git 管理文件 | 8,370 | 10,736 | +2,366 |
| Python 行数 | 1,454,674 | 1,917,783 | +463,109 |
| TypeScript/TSX 行数 | 440,023 | 652,032 | +212,009 |
| Python `test_*.py` 模块 | 2,624 | 3,452 | +828 |
| 前端 test/spec 文件 | 641 | 1,051 | +410 |
| `plugin.yaml` | 96 | 103 | +7 |
| bundled `SKILL.md` | 71 | 81 | +10 |
| optional `SKILL.md` | 111 | 122 | +11 |

> 初始基线记录的 Python 行数为 `260,970`，用上述口径无法复现（同 commit 实测 1,454,674；排除 `tests/` 为 826,335）。该行原始工具口径未记录，已按现口径重算，历史值不再作为比较基准。其余各项均以现口径精确复现了初始基线的记录值。

## 基线更新协议

每次把 `main` 合入或 rebase 到研究分支后：

1. 记录旧 commit、新 commit 和同步日期；
2. 使用路径变更判断受影响模块；
3. 将相关文档状态改为 `needs revalidation`，并写入 `revalidation_target`；
4. 重跑关键行为测试或重新追踪入口；
5. 重验通过后才更新该文档 frontmatter 的 `source_commit`，并移除 `revalidation_target`；
6. 不要仅因文档仍能渲染就认为架构结论有效。

`source_commit` 的语义是**该文档结论实际被验证的 commit**，不是工作区当前 commit；未重验时不得推进。

## 受影响模块（`dd0827710..26350357d7`）

| 研究领域 | 主要路径 | commits | 变更量 |
|---|---|---:|---|
| Agent 核心 | `agent/` | 785 | 121 files, +33,977/-5,834 |
| CLI 编排 | `hermes_cli/` | 1,118 | 171 files, +58,702/-6,203 |
| Classic CLI 回合 | `cli.py` | 114 | +4,346/-708 |
| Tool Runtime | `tools/` | 637 | 114 files, +35,168/-5,455 |
| Gateway | `gateway/` | 426 | 55 files, +20,572/-2,209 |
| TUI 后端 | `tui_gateway/` | 256 | 19 files, +10,353/-1,184 |
| Desktop | `apps/desktop/` | 1,708 | 1,407 files, +227,365/-21,481 |
| Session 存储 | `hermes_state.py` | 152 | +6,935/-671 |
| Cron | `cron/` | 182 | 11 files, +8,256/-776 |
| Dashboard | `web/` | 57 | 67 files, +4,234/-166 |
| TUI 前端 | `ui-tui/` | 72 | 109 files, +5,666/-395 |
| ACP | `acp_adapter/` | 16 | 5 files, +247/-80 |
| Provider | `providers/` | 7 | 3 files, +226/-10 |
| Batch | `batch_runner.py` | 5 | +77/-27 |

M1 与 M2 覆盖的每个领域都发生了实质变更，无一可按"未受影响"处理。

## 重验记录

| 日期 | 旧基线 | 新基线 | 受影响模块 | 结果 |
|---|---|---|---|---|
| 2026-08-11 | — | `dd0827710` | 全部 | 初始基线 |
| 2026-08-30 | `dd0827710` | `26350357d7` | 全部（见上表） | 已同步并标记；M1/M2 文档转 `needs revalidation`，结论尚未按新代码复核 |
