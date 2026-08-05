"""Fast-path fixtures shared across tests/run_agent/.

Many tests in this directory exercise the retry/backoff paths in the
agent loop. Production code uses ``jittered_backoff(base_delay=5.0)``
with a ``while time.time() < sleep_end`` loop — a single retry test
spends 5+ seconds of real wall-clock time on backoff waits.

Mocking ``jittered_backoff`` to return 0.0 collapses the while-loop
to a no-op (``time.time() < time.time() + 0`` is false immediately),
which handles the most common case without touching ``time.sleep``.

We deliberately DO NOT mock ``time.sleep`` here — some tests
(test_interrupt_propagation, test_primary_runtime_restore, etc.) use
the real ``time.sleep`` for threading coordination or assert that it
was called with specific values. Tests that want to additionally
fast-path direct ``time.sleep(N)`` calls in production code should
monkeypatch ``run_agent.time.sleep`` locally (see
``test_anthropic_error_handling.py`` for the pattern).
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _fast_retry_backoff(monkeypatch):
    """Short-circuit retry backoff for all tests in this directory."""
    try:
        import run_agent
    except ImportError:
        return

    monkeypatch.setattr(run_agent, "jittered_backoff", lambda *a, **k: 0.0)
    # The conversation loop was extracted out of run_agent.py into
    # ``agent.conversation_loop``, which imports ``jittered_backoff``
    # directly (``from agent.retry_utils import jittered_backoff``).
    # Patching ``run_agent.jittered_backoff`` alone misses every retry
    # path under the new module — tests that exercise rate-limit /
    # invalid-response / server-error retries burn real wall-clock
    # seconds per retry. Patch both for full coverage.
    try:
        from agent import conversation_loop as _conv_loop
        monkeypatch.setattr(_conv_loop, "jittered_backoff", lambda *a, **k: 0.0)
    except ImportError:
        pass


@pytest.fixture(autouse=True)
def _forward_tool_defs_meta(monkeypatch):
    """Auto-adapt ``run_agent.get_tool_definitions_with_meta`` to legacy mocks.

    Commit 69c12e8b7 (fix/66826-gottz-race) switched ``agent_init`` to call
    ``_ra().get_tool_definitions_with_meta(...)`` — which returns
    ``(tools, pre_assembly_names)`` — instead of ``get_tool_definitions()``.
    Tests that patch only ``run_agent.get_tool_definitions`` (40+ files in
    this suite) stopped intercepting the call and silently ran the REAL tool
    assembly, regressing 14 tests and slowing the rest. This fixture makes
    ``with_meta`` forward to whatever the test's ``get_tool_definitions``
    mock returns — deriving the pre-assembly names from the mocked defs — so
    single-site patches keep working unchanged:

    - a test that patches ``run_agent.get_tool_definitions`` (with a Mock or
      a plain function) gets ``with_meta`` returning ``(its_return_value,
      [names...])``;
    - a test that explicitly patches ``with_meta`` wins (its patch lands on
      top of this one and replaces it for the call);
    - a test that patches neither runs the REAL ``with_meta`` untouched
      (forwarded through to the original implementation).
    """
    try:
        import run_agent
    except ImportError:
        return
    from unittest.mock import Mock

    real_legacy = run_agent.get_tool_definitions
    real_with_meta = run_agent.get_tool_definitions_with_meta

    def _side_effect(**kwargs):
        legacy = run_agent.get_tool_definitions
        if legacy is not real_legacy:
            # The test patched get_tool_definitions — forward the with_meta
            # call to it and derive pre-assembly names from the returned defs.
            tools = legacy(**kwargs)
            names = []
            if isinstance(tools, list):
                for t in tools:
                    if isinstance(t, dict):
                        fn = t.get("function")
                        if isinstance(fn, dict) and fn.get("name"):
                            names.append(fn["name"])
            return tools, names
        # Unpatched — run the real implementation untouched.
        return real_with_meta(**kwargs)

    monkeypatch.setattr(
        run_agent, "get_tool_definitions_with_meta", Mock(side_effect=_side_effect)
    )
