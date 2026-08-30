#!/usr/bin/env python3
"""High-level multi-agent workflow tool built on delegate_task.

This is a thin orchestration layer, not a separate agent runtime.  It keeps the
existing Hermes orchestrator in charge while spawning real delegated child
AIAgent runs for Developer, Tester, and Reviewer roles.
"""

from __future__ import annotations

import json
from typing import Any

from hermes_cli.config import load_config

from agent.orchestration import TaskContext, run_development_workflow
from tools.delegate_tool import check_delegate_requirements, delegate_task
from tools.registry import registry, tool_error


MULTI_AGENT_ORCHESTRATE_SCHEMA = {
    "name": "multi_agent_orchestrate",
    "description": (
        "Run a structured Developer→Tester→Reviewer multi-agent workflow using "
        "real Hermes delegate_task child agents. Use for complex development "
        "issues when independent implementation, QA, and review improve quality. "
        "The parent/orchestrator remains responsible for final verification, "
        "commit, push, PR, and user communication."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "objective": {
                "type": "string",
                "description": "Concrete goal for this workflow, e.g. 'Bearbeite Issue #1234'.",
            },
            "task_context": {
                "type": "object",
                "description": (
                    "Shared TaskContext object. Include issue_id, issue_title, "
                    "issue_description, acceptance_criteria, repository, branch, "
                    "current_status, relevant_files, logs, screenshots, existing "
                    "findings, decisions, and open problems. Do not include secrets."
                ),
                "properties": {
                    "issue_id": {"type": "string"},
                    "issue_title": {"type": "string"},
                    "issue_description": {"type": "string"},
                    "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
                    "repository": {"type": "string"},
                    "branch": {"type": "string"},
                    "current_status": {"type": "string"},
                    "relevant_files": {"type": "array", "items": {"type": "string"}},
                    "logs": {"type": "array", "items": {"type": "string"}},
                    "screenshots": {"type": "array", "items": {"type": "string"}},
                    "developer_findings": {"type": "array", "items": {"type": "string"}},
                    "test_results": {"type": "array", "items": {"type": "string"}},
                    "reviewer_findings": {"type": "array", "items": {"type": "string"}},
                    "open_problems": {"type": "array", "items": {"type": "string"}},
                    "decisions": {"type": "array", "items": {"type": "string"}},
                    "metadata": {"type": "object"},
                },
            },
            "max_correction_loops": {
                "type": "integer",
                "description": "Maximum automatic Developer→Tester/Reviewer correction loops. Default and recommended max is 3.",
            },
            "run_reviewer": {
                "type": "boolean",
                "description": "Whether to run a reviewer after tests pass. Default true.",
            },
            "developer_toolsets": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional toolsets for Developer agent. Default: terminal,file,web.",
            },
            "tester_toolsets": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional toolsets for Test/QA agent. Default: terminal,file,browser.",
            },
            "reviewer_toolsets": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional toolsets for Review agent. Default: terminal,file.",
            },
            "debugger_toolsets": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional toolsets for Debug agent. Default: terminal,file,browser.",
            },
        },
        "required": ["objective", "task_context"],
    },
}


def multi_agent_orchestrate(
    *,
    objective: str,
    task_context: dict[str, Any],
    max_correction_loops: int | None = None,
    run_reviewer: bool | None = None,
    developer_toolsets: list[str] | None = None,
    tester_toolsets: list[str] | None = None,
    reviewer_toolsets: list[str] | None = None,
    debugger_toolsets: list[str] | None = None,
    parent_agent=None,
) -> str:
    if parent_agent is None:
        return tool_error("multi_agent_orchestrate requires a parent agent context.")
    if not objective or not str(objective).strip():
        return tool_error("objective is required.")
    if not isinstance(task_context, dict):
        return tool_error("task_context must be an object.")

    loops = max_correction_loops if max_correction_loops is not None else 3
    try:
        cfg = (load_config().get("multi_agent") or {})
    except Exception:
        cfg = {}
    roles_cfg = cfg.get("roles") if isinstance(cfg.get("roles"), dict) else {}
    if max_correction_loops is None:
        loops = cfg.get("max_correction_loops", 3)
    if not bool(cfg.get("enabled", True)):
        return tool_error("multi_agent orchestration is disabled by config (multi_agent.enabled=false).")
    run_reviewer = bool(cfg.get("require_review", True) if run_reviewer is None else run_reviewer)
    try:
        loops = int(loops)
    except Exception:
        return tool_error("max_correction_loops must be an integer.")
    if loops < 1:
        return tool_error("max_correction_loops must be >= 1.")
    if loops > 3:
        loops = 3

    ctx = TaskContext.from_mapping(task_context)
    result = run_development_workflow(
        task_context=ctx,
        delegate_fn=delegate_task,
        parent_agent=parent_agent,
        objective=objective,
        max_correction_loops=loops,
        run_reviewer=bool(run_reviewer),
        developer_toolsets=developer_toolsets or (roles_cfg.get("developer", {}) or {}).get("toolsets"),
        tester_toolsets=tester_toolsets or (roles_cfg.get("tester", {}) or {}).get("toolsets"),
        reviewer_toolsets=reviewer_toolsets or (roles_cfg.get("reviewer", {}) or {}).get("toolsets"),
        debugger_toolsets=debugger_toolsets or (roles_cfg.get("debugger", {}) or {}).get("toolsets"),
    )
    return json.dumps(result, ensure_ascii=False)


registry.register(
    name="multi_agent_orchestrate",
    toolset="delegation",
    schema=MULTI_AGENT_ORCHESTRATE_SCHEMA,
    handler=lambda args, **kw: multi_agent_orchestrate(
        objective=args.get("objective", ""),
        task_context=args.get("task_context") or {},
        max_correction_loops=args.get("max_correction_loops"),
        run_reviewer=args.get("run_reviewer"),
        developer_toolsets=args.get("developer_toolsets"),
        tester_toolsets=args.get("tester_toolsets"),
        reviewer_toolsets=args.get("reviewer_toolsets"),
        debugger_toolsets=args.get("debugger_toolsets"),
        parent_agent=kw.get("parent_agent"),
    ),
    check_fn=check_delegate_requirements,
    emoji="🧑‍💻",
)
