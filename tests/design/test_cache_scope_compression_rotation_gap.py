"""Standalone design-gap reproduction for issue #79017.

Not a fix, and not exercising production code -- #78959 (the PR that
introduces the actual `_cache_scope_from_session_id` scoping concept this
test is about) hasn't merged yet, so there is nothing in `main` to import.

This file inlines a minimal reference copy of the scoping rule #78959
proposes, purely to demonstrate -- self-contained, reviewable on `main`
today, with zero dependency on that PR's merge state -- why "scope by
physical session_id" alone is insufficient once you add context-compression
rotation to the picture. See #79017 for the full design discussion.

Once #78959 merges, this can be deleted or repointed at the real
`agent.transports.codex._cache_scope_from_session_id` -- whichever
maintainers prefer.
"""

import re

import pytest

_CRON_SESSION_ID_RE = re.compile(r"^(cron_.+)_\d{8}_\d{6}$")


def _cache_scope_from_session_id(session_id: str) -> str:
    """Mirrors the scoping rule proposed in #78959: pass non-cron session
    ids through unchanged; strip only cron's per-fire timestamp."""
    match = _CRON_SESSION_ID_RE.match(session_id or "")
    return match.group(1) if match else (session_id or "")


@pytest.mark.xfail(
    reason="#79017: scoping by physical session_id alone has no concept of "
    "a logical conversation identity distinct from the physical id, so a "
    "context-compression rotation (which mints a new physical session_id "
    "mid-conversation) always breaks cache-scope continuity. Needs a design "
    "decision (a separate logical cache-scope), not a one-line fix.",
    strict=True,
)
def test_compression_rotation_preserves_cache_scope():
    root_session = "session-root-abc123"
    # A compression rotation mints a new physical session_id for the same
    # logical conversation (see agent/conversation_compression.py).
    rotated_session = "session-rotated-def456"

    root_scope = _cache_scope_from_session_id(root_session)
    rotated_scope = _cache_scope_from_session_id(rotated_session)

    # Desired behavior once #79017 lands: the rotated segment of the SAME
    # conversation should keep the same cache scope as its root.
    assert root_scope == rotated_scope
