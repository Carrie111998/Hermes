"""
A1.6: HL-AOS Write Sink Guard

Provides fail-closed enforcement: write operations from sessions with C2+ classification
are blocked unless the target path is within configured allowed_paths.

Design:
- Reads frozen classification taint from agent via read_hl_aos_classification()
- For C0/C1: no restrictions
- For C2/C3/C4: requires target path to be within allowed_paths (from agent config)
- Fail-closed: denies all writes when no taint is present or when path is not allowed

Integration points:
- File tools (write_file, append, rename, remove)
- Memory tool (writes to MEMORY.md, USER.md)
- Terminal tool (file write operations)
- Code execution tool (file write operations)
"""

from pathlib import Path
from typing import Optional
from agent.hl_aos_classification import read_hl_aos_classification, classification_source

# Egress sinks that are always denied in C2/C3/C4 regardless of path configuration
EGRESS_SINKS = frozenset({"terminal", "web_fetch", "web_search", "browser", "fetch"})

def check_write_permission(agent, target_path: str) -> Optional[str]:
    """
    Check if a write operation is permitted given the agent's HL-AOS classification.

    Args:
        agent: The agent instance (provides access to frozen taint)
        target_path: Path being written to

    Returns:
        None if operation is allowed, error message string if denied

    Fail-closed: when taint is absent/unclassified, returns denial with guidance.
    """
    classification = read_hl_aos_classification(agent)

    if not classification:
        return (
            "Write operation denied: agent has no HL-AOS classification. "
            "To enable writes, ensure the agent has the hl_aos_taint_classification attribute. "
            "If this is intentional, the operator must set hl_aos_taint_classification='C0' or configure allowed_paths."
        )

    # C0, C1: no restrictions
    if classification in ("C0", "C1"):
        return None

    # C2, C3, C4: require path-based filtering
    if classification in ("C2", "C3", "C4"):
        allowed_paths = getattr(agent, "hl_aos_allowed_paths", [])

        if not allowed_paths:
            return (
                f"Write operation denied: {classification} session requires "
                "hl_aos_allowed_paths configuration. Set hl_aos_allowed_paths attribute "
                "to permit writes from this classification level."
            )

        # Check if target_path is within any allowed path
        try:
            target = Path(target_path).resolve()
            for allowed in allowed_paths:
                allowed_resolved = Path(allowed).resolve()
                if str(target).startswith(str(allowed_resolved)):
                    return None
        except Exception as e:
            return f"Write operation denied: path resolution failed ({e})"

        return (
            f"Write operation denied: target path '{target_path}' is not within "
            f"hl_aos_allowed_paths for {classification} session. "
            f"Allowed paths: {allowed_paths}"
        )

    # Unknown classification: fail-closed
    return (
        f"Write operation denied: unknown classification '{classification}'. "
        "Only C0, C1, C2, C3, C4 are recognized."
    )


def check_write_permission_with_context(agent, target_path: str, context: dict | None = None) -> Optional[str]:
    """Context-aware wrapper for write-sink guard integrations.

    The current A1.6 policy is path/classification based.  ``context`` is
    accepted so tool-registry and middleware callers can pass the full tool
    request without creating a second API shape; future evidence hooks can use
    it for action-class or correlation metadata without changing callers.
    """
    return check_write_permission(agent, target_path)


def check_egress_permission(agent, tool_name: str) -> Optional[str]:
    """
    Check if an egress operation (network/tool invocation) is permitted given the agent's HL-AOS classification.

    Args:
        agent: The agent instance (provides access to frozen taint)
        tool_name: Name of the tool being invoked

    Returns:
        None if operation is allowed, error message string if denied

    Egress sinks (terminal, web_fetch, web_search, fetch, browser) are always denied
    for C2/C3/C4 sessions unless explicitly allowed via hl_aos_allowed_egress.
    """
    classification = read_hl_aos_classification(agent)

    if not classification:
        return (
            "Egress operation denied: agent has no HL-AOS classification. "
            "To enable egress, ensure the agent has the hl_aos_taint_classification attribute. "
            "If this is intentional, the operator must set hl_aos_taint_classification='C0' or configure allowed_egress."
        )

    # C0, C1: no restrictions
    if classification in ("C0", "C1"):
        return None

    # C2, C3, C4: require explicit egress allowlist
    if classification in ("C2", "C3", "C4"):
        # Non-egress tools pass through without allowlist requirement —
        # file writes, memory, read-only tools etc. have their own guards.
        if tool_name not in EGRESS_SINKS:
            return None

        allowed_egress = getattr(agent, "hl_aos_allowed_egress", [])

        if not allowed_egress:
            source = classification_source(agent)
            return (
                f"Egress operation denied: tool '{tool_name}' is blocked for "
                f"{classification} ({source}) session — hl_aos_allowed_egress is "
                "empty. Set hl_aos_allowed_egress attribute to permit "
                "egress from this classification level."
            )

        # Check if tool_name is in allowed_egress
        if tool_name not in allowed_egress:
            return (
                f"Egress operation denied: tool '{tool_name}' is not in "
                f"hl_aos_allowed_egress for {classification} session. "
                f"Allowed tools: {allowed_egress}"
            )

        return None

    # Unknown classification: fail-closed
    return (
        f"Egress operation denied: unknown classification '{classification}'. "
        "Only C0, C1, C2, C3, C4 are recognized."
    )
