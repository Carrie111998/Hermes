"""Memory-write security gate + provenance tagging.

Blocks memory/vault/long-term writes whose content originated from untrusted
sources (web or tool output) — an indirect-prompt-injection guard. Callers pass
a provenance tag alongside the content; blocked writes are logged with a content
hash so the attempt is auditable without persisting the (possibly malicious)
content.

Hook point (Task B1/B2): call :func:`check_memory_write` in the memory write
path — ``tools/memory_tool.py::memory_tool`` right after ``_apply_write_gate``
(line ~1018) — and stamp the result of :func:`tag_provenance` alongside the
stored entry. That wiring is intentionally NOT applied here because this task's
acceptance boundary limits edits to trajectory.py / mcp_tool.py /
conversation_loop.py / memory_gate.py and the tests.
"""

import hashlib
import logging

logger = logging.getLogger(__name__)

# All recognised provenance labels (Task B2).
PROVENANCE_LABELS = frozenset({
    "user_instruction",
    "system_policy",
    "repo_trusted",
    "web_untrusted",
    "tool_untrusted",
    "memory_unverified",
})

# Provenance tags allowed to write. Everything else is fail-closed.
_TRUSTED = frozenset({"user_instruction", "system_policy", "repo_trusted"})


def check_memory_write(content: str, source_provenance: str) -> bool:
    """Return True if a memory write from ``source_provenance`` is allowed.

    Allows trusted provenance ("user_instruction", "system_policy",
    "repo_trusted"). Any other tag (including "web_untrusted",
    "tool_untrusted", "memory_unverified", empty, None, or an unknown value)
    is treated as untrusted (fail-closed) and blocked. Every blocked write is
    logged with a sha256 content hash and the provenance tag.
    """
    if source_provenance in _TRUSTED:
        return True

    content_hash = hashlib.sha256((content or "").encode("utf-8")).hexdigest()
    logger.warning(
        "memory write blocked: provenance=%s content_sha256=%s",
        source_provenance,
        content_hash,
    )
    return False


def tag_provenance(source_provenance: str) -> str:
    """Normalise a provenance label for storage as write metadata (Task B2).

    Unknown labels are coerced to "memory_unverified" so stored metadata is
    always one of the known labels.
    """
    return source_provenance if source_provenance in PROVENANCE_LABELS else "memory_unverified"
