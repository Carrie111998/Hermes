"""Pipeline state module — canonical source of truth for JobFlow approvals/edits/archives.

Every surface that mutates job state (Control Center :9120, LangGraph
tracker_update_node, Dashboard :3001, Telegram reply handler once wired,
agent scripts) should route through `PipelineManager` so all dashboards +
notification channels see the same history.

Today `~/.hermes/workspaces/tracker/pipeline.json` already claims to be the
"Single source of truth" in its `_description` field, but N independent
writers (dashboard, agents, one-off scripts) touch it unlocked. This module
serializes writes via file locking (fcntl on Unix, msvcrt on Windows) and
offers an append-history-plus-current-state API so every surface sees a
consistent view.

Usage:
    from pipeline_state import PipelineManager
    mgr = PipelineManager()
    mgr.update_stage(
        job_id="linkedin-4392439748",
        new_stage="approved",
        actor="diego",
        source="control_center",
        notes="VP AI Products; comp band matches",
    )
    job = mgr.get_job("linkedin-4392439748")
    pending = mgr.list_jobs(stage="approved", limit=20)
"""

from .manager import (
    PipelineManager,
    PIPELINE_PATH,
    VALID_STAGES,
    get_default_manager,
)

__all__ = ["PipelineManager", "PIPELINE_PATH", "VALID_STAGES", "get_default_manager"]
