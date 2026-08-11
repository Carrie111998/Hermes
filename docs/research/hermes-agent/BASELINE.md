---
title: "Hermes Architecture Study Baseline"
status: active
source_commit: dd0827710
verified_at: 2026-08-11
---

# 研究基线

## 当前快照

| 项目 | 值 |
|---|---|
| Repository | `NousResearch/hermes-agent` |
| Study branch | `docs/hermes-architecture-deep-dive` |
| Source branch | `main` |
| Source commit | `dd0827710` |
| Python package version | `0.19.1` |
| Python requirement | `>=3.11,<3.14` |
| Snapshot date | `2026-08-11` |

## 初始规模

以下数字用于理解研究范围，不作为稳定行为契约：

| 指标 | 初始值 |
|---|---:|
| Git 管理文件 | 8,370 |
| Python 行数 | 约 260,970 |
| TypeScript/TSX 行数 | 约 440,023 |
| Python `test_*.py` 模块 | 2,618 |
| 前端 test/spec 文件 | 641 |
| bundled `plugin.yaml` | 96 |
| bundled `SKILL.md` | 71 |
| optional `SKILL.md` | 111 |

## 基线更新协议

每次把 `main` 合入或 rebase 到研究分支后：

1. 记录旧 commit、新 commit 和同步日期；
2. 使用路径变更判断受影响模块；
3. 将相关文档状态改为 `needs revalidation`；
4. 重跑关键行为测试或重新追踪入口；
5. 更新模块 frontmatter 的 `source_commit`；
6. 不要仅因文档仍能渲染就认为架构结论有效。

## 重验记录

| 日期 | 旧基线 | 新基线 | 受影响模块 | 结果 |
|---|---|---|---|---|
| 2026-08-11 | — | `dd0827710` | 全部 | 初始基线 |

