"""Demonstrates the compression-rotation cache-scope gap tracked by #79017.

This is NOT a fix -- it's a reproduction for maintainers to look at while
deciding whether the "logical cache-scope" redesign proposed in #79017 is
worth doing. See the issue for the full design discussion.

_cache_scope_from_session_id() (introduced by #78959 for issue #78941)
scopes prompt_cache_key by the *physical* session_id. That's correct for
unrelated sessions and for cron re-fires, but context-compression rotation
mints a brand new physical session_id mid-conversation to segment the
transcript -- so the same logical conversation goes cache-cold at every
rotation boundary.
"""

import pytest

from agent.transports import get_transport


@pytest.fixture
def transport():
    import agent.transports.codex  # noqa: F401
    return get_transport("codex_responses")


@pytest.mark.xfail(
    reason="#79017: cache scope has no concept of a logical conversation "
    "identity distinct from the physical session_id, so compression "
    "rotation always goes cache-cold. Needs a design decision, not a "
    "one-line fix -- see the issue.",
    strict=True,
)
def test_compression_rotation_preserves_cache_scope(transport):
    root_session = "session-root-abc123"
    # A compression rotation mints a new physical session_id for the same
    # logical conversation (see agent/conversation_compression.py).
    rotated_session = "session-rotated-def456"

    root_kw = transport.build_kwargs(
        model="gpt-5.4",
        messages=[{"role": "system", "content": "You are a helpful assistant."}],
        tools=[],
        session_id=root_session,
        is_codex_backend=True,
    )
    rotated_kw = transport.build_kwargs(
        model="gpt-5.4",
        messages=[{"role": "system", "content": "You are a helpful assistant."}],
        tools=[],
        session_id=rotated_session,
        is_codex_backend=True,
    )

    # Desired behavior once #79017 lands: the rotated segment of the SAME
    # conversation should keep the same cache scope as its root.
    assert root_kw["prompt_cache_key"] == rotated_kw["prompt_cache_key"]
