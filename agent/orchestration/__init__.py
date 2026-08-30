"""Multi-agent orchestration helpers built on Hermes delegation.

This package intentionally does not implement a second agent runtime.  It
normalizes task context, handoff prompts, loop limits, and audit logging around
existing Hermes mechanisms such as ``delegate_task`` and Kanban.
"""

from .task_context import TaskContext
from .loop_guard import LoopGuard
from .agent_runner import run_development_workflow

__all__ = ["TaskContext", "LoopGuard", "run_development_workflow"]
