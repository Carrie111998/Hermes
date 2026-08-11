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
- Working document：[architecture/system-context.md](./architecture/system-context.md)

## 本次已完成

- 创建专用研究分支。
- 建立研究目录和跨会话恢复协议。
- 将完整研究路线固化到 `PLAN.md`。
- 建立进度、基线、源码索引和模块模板。
- 建立第一版系统上下文图。

## 尚未完成

- 尚未对系统上下文图中的每条边进行源码和测试验证。
- 尚未创建进程/部署图。
- 尚未开始 Canonical CLI Turn 调用链。

## 下次会话的准确动作

1. 读取本目录的 `README.md`、`PROGRESS.md` 和本文件。
2. 阅读 `website/docs/developer-guide/architecture.md`。
3. 从 `hermes_cli/main.py`、`cli.py`、`gateway/run.py`、`tui_gateway/server.py` 和 `acp_adapter/` 验证入口边。
4. 从 `run_agent.py`、`agent/agent_init.py` 验证共享 Agent 核心边界。
5. 更新 `architecture/system-context.md` 的证据表。
6. 创建 `architecture/process-model.md`。

## 工作区提示

本文件生成时尚未创建提交。继续前先运行 `git status --short --branch` 确认工作树状态。
