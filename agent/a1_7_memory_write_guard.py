"""
A1.7: HL-AOS Memory Write Sink Guard

Cross-session persistence boundary. Memory writes (MEMORY.md/USER.md) from C2+
sessions are denied unless the target path is explicitly in hl_aos_allowed_paths.

Rationale: memory persists across sessions. A C2 payload written from a C0 agent
would leak confidential content back into less-restricted sessions via MEMORY.md
snapshot injection.

Fail-closed: missing classification or missing allowed_paths denies all writes.
"""

from pathlib import Path
from typing import Optional, Dict, Any
from agent.hl_aos_classification import read_hl_aos_classification, classification_source


# Memory tool names
MEMORY_SINKS = frozenset({"memory"})


def check_memory_write_permission(
    agent,
    target: str,
    content: str
) -> Optional[str]:
    """
    Check if a memory write is permitted given the agent's HL-AOS classification.

    Args:
        agent: The agent instance
        target: "memory" or "user"
        content: The content being written

    Returns:
        None if allowed, error message string if denied
    """
    classification = read_hl_aos_classification(agent)

    # Fail-closed: missing classification denies
    if not classification:
        return (
            "Memory write denied: agent has no HL-AOS classification. "
            "Set hl_aos_taint_classification before writing memory."
        )

    # C0, C1: no restrictions
    if classification in ("C0", "C1"):
        return None

    # C2, C3, C4: require explicit allowed_paths AND explicit memory sink opt-in
    if classification in ("C2", "C3", "C4"):
        allowed_paths = getattr(agent, "hl_aos_allowed_paths", [])

        if not allowed_paths:
            source = classification_source(agent)
            return (
                f"Memory write denied: {classification} ({source}) session "
                "requires hl_aos_allowed_paths configuration."
            )

        # Determine the memory file path
        from tools.memory_tool import get_memory_dir
        mem_dir = get_memory_dir()
        filename = "MEMORY.md" if target == "memory" else "USER.md"
        memory_path = str(mem_dir / filename)

        # Check if memory_path is equal to or contained within any allowed_path.
        # Use Path semantics, not raw string prefix matching, so a sibling such as
        # /tmp/mem does not authorize /tmp/memory/MEMORY.md.
        try:
            target_resolved = Path(memory_path).resolve()
            for allowed in allowed_paths:
                allowed_resolved = Path(allowed).resolve()
                if target_resolved == allowed_resolved or allowed_resolved in target_resolved.parents:
                    return None
        except Exception as e:
            return f"Memory write denied: path resolution failed ({e})"

        return (
            f"Memory write denied: {filename} is not within hl_aos_allowed_paths "
            f"for {classification} session."
        )

    # Unknown classification: fail-closed
    return (
        f"Memory write denied: unknown classification '{classification}'. "
        "Only C0, C1, C2, C3, C4 are recognized."
    )
