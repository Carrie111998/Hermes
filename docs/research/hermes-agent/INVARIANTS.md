---
title: "Hermes Cross-Cutting Invariants"
status: needs revalidation
source_commit: dd0827710
revalidation_target: 26350357d7
verified_at: 2026-08-11
confidence: medium
---

# 跨模块关键不变量

本表从项目开发指南和初步源码阅读提取。进入相应模块后，必须补充具体生产代码和测试证据。

| ID | 不变量 | 初始证据 | 状态 |
|---|---|---|---|
| INV-001 | 同一会话的系统提示词保持字节稳定，除上下文压缩等显式重建边界外不得中途变化 | `AGENTS.md`, `agent/system_prompt.py` | verified by code/docs |
| INV-002 | 动态的当前轮次上下文应进入 API-bound message，而不是改写已缓存系统提示词 | `agent/turn_context.py` 的 `api_content` | verified by code |
| INV-003 | 持久消息历史必须满足 Provider 可接受的角色交替 | `AGENTS.md`, `agent/message_sanitization.py` | needs targeted tests |
| INV-004 | 每个 assistant tool call 必须有匹配的 tool result，反之亦然；invalid、blocked、interrupt 和 timeout 分支也必须闭合配对 | `agent/conversation_loop.py`, `agent/tool_executor.py`, `tests/run_agent/test_tool_call_incremental_persistence.py` | verified by code/tests |
| INV-005 | 有副作用工具执行前，assistant tool-call row 必须先进入 canonical SessionDB；明确持久化失败时不得投影 intent 或执行 handler | `agent/conversation_loop.py`, `tests/run_agent/test_tool_call_incremental_persistence.py` | verified by code/tests |
| INV-006 | 可恢复 transcript 必须包含本轮 canonical core assistant response；持久化后追加的安全 footer 或 `transform_llm_output` 属于 delivery projection，不保证逐字回写该 row | `agent/turn_finalizer.py`, `tests/agent/test_turn_finalizer_final_response_persistence.py` | verified by code/tests |
| INV-007 | Background Review 不得污染主对话、主 Session 生命周期或主工具权限 | `agent/background_review.py` | verified by code |
| INV-008 | Memory 当前会话快照冻结；工具写入可立即落盘，但不隐式改变当前 prompt | `tools/memory_tool.py`, Memory 文档 | verified by code/docs |
| INV-009 | 子 Agent 不能通过委派获得父 Agent 没有的能力 | `tools/delegate_tool.py` | needs targeted tests |
| INV-010 | Core model tool surface 必须保持窄；非普遍能力优先 gate、plugin、skill 或 MCP | `AGENTS.md` Footprint Ladder | design contract |
| INV-011 | 非秘密行为配置进入 `config.yaml`；`.env` 只承担凭据 | `AGENTS.md` | design contract |
| INV-012 | 项目或用户插件的 arbitrary code 必须经过明确的信任/启用边界 | `hermes_cli/plugins.py`, Plugin 文档 | needs full verification |
| INV-013 | SessionDB 是所有会话消息的 canonical store；gateway routing JSON 不是会话数据库 | Session 文档、state code | needs code walkthrough |
| INV-014 | 压缩和后台 fork 不得让同一父 Session 产生未被入口采用的竞争性 lineage | compression/background review comments | needs compression study |
| INV-015 | tool result 必须在 completion UI 和下一次 Provider 请求前持久化；明确失败时停止后续 call/segment/推理 | `agent/tool_executor.py`, `agent/conversation_loop.py`, `tests/run_agent/test_tool_call_incremental_persistence.py` | verified by code/tests |

## 状态说明

- `verified by code`：当前生产代码直接实施该约束。
- `verified by code/docs`：代码和官方文档相互印证。
- `needs targeted tests`：尚未定位或运行行为契约测试。
- `design contract`：项目明确要求，但仍需检查所有重要路径是否遵守。
