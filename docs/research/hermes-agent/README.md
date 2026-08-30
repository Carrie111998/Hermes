---
title: "Hermes Agent Architecture Deep Dive"
status: active
source_commit: 26350357d7
verified_at: 2026-08-30
confidence: high
---

# Hermes Agent 架构深度研究

本目录是一项长期、可跨会话恢复的 Hermes Agent 架构研究。目标不是逐文件复述代码，而是建立一套由源码、测试、运行验证和 Git 历史共同支撑的架构手册，解释 Hermes 的设计、边界、不变量、扩展方式和演进原因。

## 会话启动协议

继续本研究时，按顺序阅读：

1. 本文件；
2. [PROGRESS.md](./PROGRESS.md)；
3. [HANDOFF.md](./HANDOFF.md)；
4. `HANDOFF.md` 指向的当前模块文档；
5. 仅加载当前问题需要的源码、测试和 Git 历史。

不要在每次会话中读取整个 `journal/` 或所有已完成模块。旧过程材料只在需要追溯证据时加载。

## 研究原则

- 以真实运行链路组织研究，不按目录平铺复述。
- 区分文档声称、代码事实、测试契约和研究推断。
- 所有结论标记 `verified`、`inferred` 或 `historical`。
- 重大设计判断必须能指向源码、测试或 Git 历史证据。
- 对关键机制同时研究正常路径、失败路径、恢复路径和并发路径。
- 图是可修正的架构假设，不是一开始就固定的结论。
- 研究分支只承载文档；发现的代码修复应进入独立分支。

## 当前入口

- 完整路线：[PLAN.md](./PLAN.md)
- 总体进度：[PROGRESS.md](./PROGRESS.md)
- 最新交接：[HANDOFF.md](./HANDOFF.md)
- 研究基线：[BASELINE.md](./BASELINE.md)
- 源码导航：[SOURCE-MAP.md](./SOURCE-MAP.md)
- 关键不变量：[INVARIANTS.md](./INVARIANTS.md)
- 术语表：[GLOSSARY.md](./GLOSSARY.md)
- 设计决策：[DECISIONS.md](./DECISIONS.md)

## 内容地图

| 目录 | 内容 |
|---|---|
| [`architecture/`](./architecture/) | 系统上下文、进程模型、模块图和总体数据流 |
| [`modules/`](./modules/) | Agent Loop、Prompt、Provider、Tools、Memory、Skills 等模块深入研究 |
| [`flows/`](./flows/) | CLI、Gateway、压缩、后台学习、委派和 Cron 的端到端时序 |
| [`security/`](./security/) | 信任边界、权限矩阵和威胁模型 |
| [`journal/`](./journal/) | 逐会话研究记录；默认不自动加载 |
| [`templates/`](./templates/) | 模块研究和会话记录模板 |

## 每次会话结束协议

1. 更新当前模块文档及相关 Mermaid 图。
2. 将结论的证据、置信度和适用 commit 写清楚。
3. 更新 [PROGRESS.md](./PROGRESS.md)。
4. 重写 [HANDOFF.md](./HANDOFF.md)，只保留最新交接。
5. 需要保留的探索过程写入 `journal/YYYY-MM-DD-session-N.md`。
6. 检查文档链接与 Git diff。
7. 使用小而明确的 `docs(study): ...` 提交保存阶段成果。

