"""Cache-safe refresh payloads for long-lived Hermes sessions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent.skill_commands import reload_skills
from tools.memory_tool import MemoryStore


@dataclass(frozen=True)
class SoftRefreshResult:
    """Model-facing tail context and user-facing refresh summary."""

    context_note: str
    report: str
    skills: dict[str, Any]


def build_soft_refresh(*, skills_result: dict[str, Any] | None = None) -> SoftRefreshResult:
    """Read current profile memory and rescan skills without prompt mutation."""
    skills = skills_result if skills_result is not None else reload_skills()
    store = MemoryStore()
    store.load_from_disk()

    memory_block = store.format_for_system_prompt("memory")
    user_block = store.format_for_system_prompt("user")
    sections = [
        "[USER INITIATED SESSION REFRESH:",
        "The following current active-profile context supersedes older conflicting rules.",
    ]
    if memory_block:
        sections.extend(("", memory_block))
    if user_block:
        sections.extend(("", user_block))
    if not memory_block and not user_block:
        sections.extend(("", "No non-empty MEMORY.md or USER.md content was found."))
    sections.append("]")

    added = skills.get("added", [])
    removed = skills.get("removed", [])
    skill_summary = f"skills re-scanned ({skills.get('total', 0)} available)"
    if added:
        skill_summary += "; added: " + ", ".join(item["name"] for item in added)
    if removed:
        skill_summary += "; removed: " + ", ".join(item["name"] for item in removed)
    memory_status = "MEMORY.md loaded" if memory_block else "MEMORY.md missing or empty"
    user_status = "USER.md loaded" if user_block else "USER.md missing or empty"
    report = f"Refreshed {skill_summary}; {memory_status}; {user_status}. Gateway not restarted."

    return SoftRefreshResult("\n".join(sections), report, skills)
