---
title: "Hermes Architecture Source Map"
status: active
source_commit: dd0827710
verified_at: 2026-08-11
confidence: medium
---

# 源码、文档与测试地图

这是研究入口索引，不替代模块文档。状态为初始映射，进入对应里程碑后逐项验证和补充测试入口。

| 领域 | 主要生产代码 | 官方文档 | 测试入口 |
|---|---|---|---|
| Agent 生命周期 | `run_agent.py`, `agent/agent_init.py`, `agent/conversation_loop.py`, `agent/turn_context.py`, `agent/turn_finalizer.py` | `website/docs/developer-guide/agent-loop.md` | `tests/run_agent/`, `tests/agent/` |
| Prompt 构建 | `agent/system_prompt.py`, `agent/prompt_builder.py`, `agent/coding_context.py` | `website/docs/developer-guide/prompt-assembly.md` | `tests/agent/`, `tests/run_agent/` 中 prompt 相关测试 |
| Context/Cache | `agent/context_engine.py`, `agent/context_compressor.py`, `agent/prompt_caching.py`, `agent/conversation_compression.py` | `website/docs/developer-guide/context-compression-and-caching.md` | `tests/agent/`, `tests/run_agent/` 中 compression/cache 测试 |
| Provider/Transport | `providers/`, `hermes_cli/runtime_provider.py`, `agent/transports/`, `agent/anthropic_adapter.py` | `website/docs/developer-guide/provider-runtime.md` | `tests/providers/`, `tests/agent/` |
| Tool Runtime | `model_tools.py`, `tools/registry.py`, `toolsets.py` | `website/docs/developer-guide/tools-runtime.md` | `tests/tools/`, `tests/run_agent/` |
| Terminal/Environment | `tools/terminal_tool.py`, `tools/process_registry.py`, `tools/environments/` | `website/docs/user-guide/features/tools.md` | `tests/tools/`, `tests/integration/` |
| Programmatic Execution | `tools/code_execution_tool.py` | `website/docs/user-guide/features/code-execution.md` | `tests/tools/` 中 code execution 测试 |
| Delegation | `tools/delegate_tool.py`, `agent/delegation_context.py` | `website/docs/guides/delegation-patterns.md` | `tests/tools/` 中 delegate 测试 |
| Built-in Memory | `tools/memory_tool.py`, `agent/system_prompt.py` | `website/docs/user-guide/features/memory.md` | `tests/tools/`, `tests/agent/` 中 memory 测试 |
| External Memory | `agent/memory_provider.py`, `agent/memory_manager.py`, `plugins/memory/` | `website/docs/developer-guide/memory-provider-plugin.md` | `tests/plugins/`, provider-specific tests |
| Session Storage | `hermes_state.py`, `hermes_state_schema.py`, `hermes_state_search.py`, `hermes_state_portability.py` | `website/docs/developer-guide/session-storage.md` | `tests/hermes_state/`, `tests/state/` |
| Skills | `tools/skills_tool.py`, `tools/skill_manager_tool.py`, `tools/skill_usage.py`, `agent/skill_commands.py` | `website/docs/user-guide/features/skills.md` | `tests/skills/`, `tests/tools/` |
| Background Learning | `agent/background_review.py`, `tools/skill_provenance.py`, `tools/write_approval.py` | Memory/Skills 文档相关章节 | `tests/agent/`, `tests/tools/` 中 background review 测试 |
| Curator | `agent/curator.py`, `agent/curator_backup.py`, `tools/skill_usage.py` | `website/docs/user-guide/features/curator.md` | `tests/agent/`, `tests/skills/` 中 curator 测试 |
| Cron | `cron/jobs.py`, `cron/scheduler.py`, `tools/cronjob_tools.py` | `website/docs/user-guide/features/cron.md` | `tests/cron/` |
| Kanban | `plugins/kanban/`, `tools/kanban_tools.py`, `hermes_cli/kanban_db.py` | `docs/kanban/` | `tests/plugins/`, kanban 相关测试 |
| Gateway | `gateway/run.py`, `gateway/session.py`, `gateway/delivery.py`, `gateway/platforms/base.py` | `website/docs/developer-guide/gateway-internals.md` | `tests/gateway/` |
| Slash Commands | `hermes_cli/commands.py`, `cli.py`, `gateway/slash_commands.py`, `hermes_cli/slash_exec.py` | CLI/Gateway 用户文档 | `tests/cli/`, `tests/gateway/` |
| Plugin System | `hermes_cli/plugins.py`, `plugins/` | `website/docs/user-guide/features/plugins.md` | `tests/plugins/` |
| TUI | `ui-tui/src/`, `tui_gateway/` | `ui-tui/README.md`, TUI 用户文档 | `tests/tui_gateway/`, `ui-tui` Vitest |
| Desktop | `apps/desktop/`, `apps/shared/` | `apps/desktop/README.md` | Desktop Vitest/Playwright |
| Dashboard | `web/`, `hermes_cli/web_server.py`, `hermes_cli/web_routers/` | Web Dashboard 用户文档 | `tests/dashboard/`, `web` Vitest |
| ACP | `acp_adapter/` | `website/docs/developer-guide/acp-internals.md` | `tests/acp/`, `tests/acp_adapter/` |
| Security | `tools/approval.py`, `tools/write_approval.py`, `tools/skills_guard.py`, gateway pairing/auth paths | Security 用户文档 | `tests/security` 相关路径、`tests/tools/`, `tests/gateway/` |
| Trajectories/Batch | `batch_runner.py`, `agent/trajectory.py`, `trajectory_compressor.py` | Architecture/研究文档 | trajectory/batch 相关测试 |

## 入口命令

由 `pyproject.toml` 暴露：

| 命令 | Python 入口 |
|---|---|
| `hermes` | `hermes_cli.main:main` |
| `hermes-agent` | `run_agent:main` |
| `hermes-acp` | `acp_adapter.entry:main` |

