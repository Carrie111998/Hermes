"""Version comparison for live Grace operating instructions."""

from __future__ import annotations

import os
import re
from pathlib import Path


_PATTERN = re.compile(r"GRACE_CLAWOPS_POLICY_VERSION:\s*([A-Za-z0-9._-]+)")
_SECTION = re.compile(
    r"(?ms)^## Grace to ClawOps Delegation Contract\s*$.*?(?=^##\s|\Z)"
)
_APPROVAL_TOKEN = re.compile(r"核准[ \t\u3000]+")
_VALID_APPROVAL_TOKEN = re.compile(
    r"核准[ \t\u3000]+([0-9a-f]{16})(?=$|[ \t\u3000，,。.!！？?、:：;；])"
)


def _policy_blocks_from_text(text: str) -> list[str]:
    return [
        match.group(0).strip()
        for match in _SECTION.finditer(str(text or ""))
    ]


def active_policy_version() -> str:
    path = Path(os.getenv("HERMES_AGENTS_POLICY", "~/.hermes/AGENTS.md")).expanduser()
    if not path.exists():
        return ""
    match = _PATTERN.search(path.read_text(encoding="utf-8"))
    return match.group(1) if match else ""


def stored_prompt_matches_active_policy(prompt: str) -> bool:
    current = str(prompt or "")
    block = active_policy_prompt_block()
    if block:
        return _policy_blocks_from_text(current) == [block]
    version = active_policy_version()
    return not version or f"GRACE_CLAWOPS_POLICY_VERSION: {version}" in current


def active_policy_prompt_block() -> str:
    """Return the live Grace/ClawOps section that must survive prompt assembly."""
    configured_path = os.getenv("HERMES_AGENTS_POLICY", "").strip()
    path = Path(configured_path or "~/.hermes/AGENTS.md").expanduser()
    if not path.exists():
        if configured_path:
            raise RuntimeError(
                f"Configured Grace policy file does not exist: {path}"
            )
        return ""
    match = _SECTION.search(path.read_text(encoding="utf-8"))
    if match is None:
        if configured_path:
            raise RuntimeError(
                "Configured Grace policy file is missing the "
                "'Grace to ClawOps Delegation Contract' section."
            )
        return ""
    return match.group(0).strip()


def ensure_active_policy_prompt(prompt: str) -> str:
    """Append the active policy when Hermes' cwd-based prompt builder omitted it."""
    current = str(prompt or "")
    block = active_policy_prompt_block()
    existing_blocks = _policy_blocks_from_text(current)
    if not block or existing_blocks == [block]:
        return current
    if existing_blocks:
        replacements = iter([f"{block}\n\n"] + [""] * (len(existing_blocks) - 1))
        return _SECTION.sub(lambda _match: next(replacements), current)
    return f"{current.rstrip()}\n\n# Live Grace Operating Policy\n\n{block}\n"


def approval_turn_prompt(message_text: str) -> str:
    """Force token-shaped approval turns through the authoritative tool."""
    match = _APPROVAL_TOKEN.search(str(message_text or ""))
    if match is None:
        return ""
    return (
        "[Trusted approval-turn routing]\n"
        "The current authenticated user message contains an approval-token attempt. "
        "Do not judge its wording, validity, expiry, action, platform, or scope "
        "from conversation history and do not answer with approval instructions "
        "from memory. You MUST call clawops_delegate now with the unchanged "
        "contract and the approval_token copied from the user message. The tool "
        "result is authoritative. "
        "If it reports an expired or invalid challenge, state that exact reason; "
        "then retry the same unchanged contract without approval_token only when "
        "a fresh replacement challenge is still required. Never claim punctuation "
        "or polite wording is invalid unless the tool actually returns that error."
    )


def approval_token_candidate(message_text: str) -> str:
    """Return a syntactically valid challenge token from an approval turn."""
    match = _VALID_APPROVAL_TOKEN.search(str(message_text or ""))
    return match.group(1) if match else ""
