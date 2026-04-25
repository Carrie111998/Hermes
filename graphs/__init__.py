"""Hermes JobFlow as LangGraph (Phase B of ADR-0020).

Stage-1 (shipped): Matcher scoring as a typed graph node. Default via `invoke()`.
Stage-3 (shipped): Full pipeline Matcher->Tailor->HITL->Apply->Tracker with
checkpointing. Access via `invoke_full()`.

Public API:
    from graphs import (
        build_jobflow_graph,   # Stage-1 graph (Matcher-only)
        build_full_graph,      # Stage-3 graph (full pipeline + checkpointer)
        JobFlowState,
        invoke,                # Stage-1 runner
        invoke_full,           # Stage-3 runner with checkpointing
    )
"""

from .jobflow import (
    JobFlowState,
    build_full_graph,
    build_jobflow_graph,
    invoke,
    invoke_full,
    resume_full,
)
from .critic import (
    CriticState,
    build_critic_graph,
    invoke_critic,
)

__all__ = [
    # JobFlow
    "JobFlowState",
    "build_full_graph",
    "build_jobflow_graph",
    "invoke",
    "invoke_full",
    "resume_full",
    # Critic
    "CriticState",
    "build_critic_graph",
    "invoke_critic",
]
