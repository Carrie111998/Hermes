"""Kanban worker spawn packet builder and factory contract checker.

This module generates the spawn packet that replaces the four-word
"work kanban task {id}" stub. It parses MANUALS from card bodies and
emits a structured first-turn instruction set.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional, Tuple

Manual = Tuple[str, str]  # (\"read_file\", path) | (\"skill_view\", name)

_REQUIRED_HEADINGS = ("GOAL:", "REFS:", "MANUALS:", "PROCEDURE:", "DONE:", "FAIL:")


def parse_manuals(body: Optional[str]) -> List[Manual]:
    """Extract MANUALS list from card body.
    
    Returns list of (kind, value) tuples where kind is 'read_file' or 'skill_view'.
    """
    if not body:
        return []
    # Match MANUALS: section with leading whitespace/bullet list
    match = re.search(r"(?ms)^MANUALS:\s*\n((?:[ \t]*-.*\n)+)", body)
    if not match:
        return []
    
    out: List[Manual] = []
    for raw in match.group(1).splitlines():
        line = raw.strip()
        if not line.startswith("-"):
            continue
        line = line.lstrip("- ").strip()
        if line.lower().startswith("read_file:"):
            path = line.split(":", 1)[1].strip()
            out.append(("read_file", path))
        elif line.lower().startswith("skill_view:"):
            name = line.split(":", 1)[1].strip()
            out.append(("skill_view", name))
    return out


def build_worker_spawn_prompt(
    task_id: str,
    *,
    body: Optional[str] = None,
    board: Optional[str] = None,
    assignee: Optional[str] = None,
    profile_home: Optional[str] = None,
) -> str:
    """Build the spawn packet instruction for a kanban worker.
    
    Args:
        task_id: The task id (e.g., "t_deadbeef")
        body: The card body (may contain MANUALS section)
        board: The kanban board slug
        assignee: The assigned profile/worker
        profile_home: Home directory of the assigned profile (to find AGENTS.md)
    
    Returns:
        A multi-line instruction string (replaces "work kanban task {id}").
    """
    manuals = parse_manuals(body)
    
    # Inject AGENTS.md from profile_home if it exists and not already listed
    if profile_home:
        agents = Path(profile_home) / "AGENTS.md"
        if agents.is_file() and not any(
            kind == "read_file" and Path(val) == agents for kind, val in manuals
        ):
            manuals = [("read_file", str(agents))] + manuals
    
    # Build numbered load instructions
    load_lines = []
    for i, (kind, val) in enumerate(manuals, 1):
        if kind == "read_file":
            load_lines.append(f"{i}. read_file({val!r}) — full file, no offset/limit")
        else:
            load_lines.append(
                f"{i}. skill_view(name={val!r}) — full file, no offset/limit"
            )
    
    if not load_lines:
        load_lines.append(
            "1. If the card body has MANUALS, load each in one ordered set "
            "(read_file paths, then skill_view names). Full file, no offset/limit."
        )
    
    board_s = board or "(current board)"
    loads = "\n".join(load_lines)
    
    return (
        f"Work kanban task {task_id} on board {board_s} "
        f"(assignee={assignee or 'unset'}).\n"
        "You are a dispatcher-spawned worker. HERMES_KANBAN_BOARD is already pinned. "
        "Do not mint cards. Do not touch other boards.\n\n"
        "BEFORE any other tool:\n"
        f"{loads}\n\n"
        "Then read this task. PROCEDURE on the card is steps, not a substitute "
        "for those files. Do not guess from skill index lines.\n\n"
        "When the DONE MEASURE is met: kanban_request_review with "
        "metadata changed_files, verification, residual_risk and "
        "artifacts=[the DONE file]. Do not kanban_complete to done.\n"
        "If DONE cannot be met: kanban_block and write handoff #3. "
        "Until the MANUALS loads succeed, you have not started."
    )


class FactoryCardContractError(ValueError):
    """Raised when a card does not meet the factory 5-field+MANUALS contract."""

    pass


def assert_factory_card_contract(body: Optional[str]) -> None:
    """Check that a card has all required fields for factory work.
    
    Required headings (case-sensitive, line-start): GOAL:, REFS:, MANUALS:, PROCEDURE:, DONE:, FAIL:
    
    Raises:
        FactoryCardContractError: If any required heading is missing.
    """
    text = body or ""
    missing = [
        h[:-1] for h in _REQUIRED_HEADINGS
        if not re.search(rf"(?m)^{h}", text)
    ]
    if missing:
        raise FactoryCardContractError(
            "card is a wish, not a contract; missing " + ", ".join(missing)
        )
