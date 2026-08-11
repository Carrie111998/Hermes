---
title: "Latest Architecture Study Handoff"
status: active
source_commit: dd0827710
updated_at: 2026-08-11
---

# 最新会话交接

## 当前状态

- Branch：`docs/hermes-architecture-deep-dive`
- Baseline：`dd0827710`
- Milestone：M1 — 系统全景架构
- Working document：[architecture/process-model.md](./architecture/process-model.md)

## 本次已完成

- 创建专用研究分支。
- 建立研究目录和跨会话恢复协议。
- 将完整研究路线固化到 `PLAN.md`。
- 建立进度、基线、源码索引和模块模板。
- 建立第一版系统上下文图。
- 提交研究基础设施：`ea3bfe794 docs(study): initialize Hermes architecture deep dive`。
- 逐入口核对 Classic CLI、TUI、Dashboard、Desktop、Gateway/API、ACP、Batch 和 Cron 的 Agent 构造与进程边界。
- 创建进程/部署图并记录 live state、SessionDB、profile 和可选 compute-host 的 ownership。

## 尚未完成

- 尚未创建一级模块依赖图。
- 尚未创建顶层数据流图。
- 尚未开始 Canonical CLI Turn 调用链。

## 下次会话的准确动作

1. 读取本目录的 `README.md`、`PROGRESS.md` 和本文件。
2. 读取 `architecture/system-context.md` 与 `architecture/process-model.md`，不要重新调查已验证入口。
3. 从 `run_agent.py`、`agent/agent_init.py`、`model_tools.py`、`hermes_state.py` 和 `hermes_cli/plugins.py` 提取一级模块依赖方向。
4. 创建 `architecture/module-map.md`，只画稳定模块边界，不深入回合细节。
5. 创建 `architecture/data-flow.md`，区分 runtime message、API-bound content、tool result、SessionDB、Memory 与 Skills。
6. 更新进度与交接；若 M1 完成，下一步进入 M2 的 Classic CLI Canonical Turn。

## 工作区提示

当前分支应至少包含初始化提交 `ea3bfe794`；继续前运行 `git status --short --branch` 并确认最新 M1 研究单元已提交。
