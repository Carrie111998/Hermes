"""Workflow engine plugin — registers the workflow_analyst auxiliary task.

Users configure the analyst under ``auxiliary.workflow_analyst`` in config.yaml.
The engine invokes it via ``get_text_auxiliary_client("workflow_analyst")``
for three analysis modes: escalation, status summary, and failure diagnosis.

See ``hermes_cli/workflow_analyst.py`` for the auxiliary module.
"""

from __future__ import annotations


def register(ctx):
    """Register the workflow_analyst auxiliary with the Hermes plugin system."""
    ctx.register_auxiliary_task(
        key="workflow_analyst",
        display_name="Workflow analyst",
        description="pipeline escalation, status, and failure analysis",
        defaults={
            "timeout": 180,
            "extra_body": {},
        },
    )
