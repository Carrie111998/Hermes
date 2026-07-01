"""
A1.8: HL-AOS execute_code nested write-sink guard.

execute_code can mutate files through two paths that bypass direct write_file /
patch admission checks:

1. raw Python file APIs in the child process (open(), pathlib, os, subprocess),
2. sandbox RPC calls back into write-capable Hermes tools.

Because arbitrary Python cannot be reliably path-confined by static analysis,
C2+ sessions fail closed for execute_code unless a later policy introduces a
real sandbox/path-confinement proof.
"""

from typing import Optional

from agent.hl_aos_classification import read_hl_aos_classification, classification_source


EXECUTE_CODE_SINKS = frozenset({"execute_code"})


def check_execute_code_write_permission(agent, code: str) -> Optional[str]:
    """Return None when execute_code may run, else a denial string."""
    classification = read_hl_aos_classification(agent)

    if not classification:
        return (
            "execute_code denied: agent has no HL-AOS classification. "
            "Set hl_aos_taint_classification before running code."
        )

    if classification in ("C0", "C1"):
        return None

    if classification in ("C2", "C3", "C4"):
        source = classification_source(agent)
        return (
            f"execute_code denied: {classification} ({source}) sessions cannot run "
            "arbitrary Python because it can perform nested file writes via raw "
            "file APIs or sandbox RPC tools before path-scoped write guards can "
            "prove containment. Use audited first-class tools instead."
        )

    return (
        f"execute_code denied: unknown classification '{classification}'. "
        "Only C0, C1, C2, C3, C4 are recognized."
    )
