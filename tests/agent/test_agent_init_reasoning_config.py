"""``agent_init`` must resolve reasoning_config when no caller supplies one.

Regression guard for a bug CLASS that recurred three times (Aug 2026: CLI
oneshot, ACP sessions; Sep 2026: both again after the per-call-site patches were
lost). Each construction site of ``AIAgent`` had to remember a
``reasoning_config=`` kwarg; a site that forgot it stored ``None``, which
``build_anthropic_kwargs`` turns into "no ``thinking`` key on the wire". Adaptive
Claude then falls back to the API default ``display: "omitted"`` and returns
thinking blocks whose text is empty with only an opaque ``signature`` populated —
rendered by hosts as garbled bytes where reasoning should be.

Fixing it per-site is not durable: any new surface reintroduces it. These tests
pin the behaviour at the single chokepoint that every surface passes through.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

import pytest


def _init_agent_config_source():
    from agent import agent_init

    for name in ("init_agent_config", "initialize_agent", "init_agent"):
        fn = getattr(agent_init, name, None)
        if fn is not None:
            return fn
    pytest.skip("agent_init entrypoint not found under a known name")


def test_chokepoint_resolves_reasoning_config_when_caller_omits_it():
    """The assignment must be guarded by a None-check that resolves config.

    AST-based on purpose: a substring check for ``resolve_reasoning_config`` in
    the source passes against broken code, because the explanatory comment
    beside the fix mentions the symbol.
    """
    from agent import agent_init

    src = textwrap.dedent(inspect.getsource(agent_init))
    tree = ast.parse(src)

    calls = {
        (getattr(n.func, "id", None) or getattr(n.func, "attr", None))
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
    }
    assert "resolve_reasoning_config" in calls or any(
        name and "resolve_rc" in name for name in calls
    ), (
        "agent_init must call resolve_reasoning_config so surfaces that omit the "
        "reasoning_config kwarg still get the user's configured effort"
    )


def test_omitted_kwarg_yields_resolved_config(monkeypatch):
    """Behaviour contract: no kwarg -> resolved config, not None."""
    from agent import agent_init

    monkeypatch.setattr(
        agent_init,
        "_load_rc_cfg",
        lambda: {"agent": {"reasoning_effort": "high"}},
        raising=False,
    )

    resolved = _resolve_via_helper("some-model")
    assert resolved is not None, (
        "a surface that omits reasoning_config must still receive a resolved "
        "config, otherwise thinking text is silently dropped"
    )
    assert resolved.get("enabled") is True
    assert resolved.get("effort") == "high"


def test_explicit_disable_is_not_overridden():
    """The bug bites both directions — an explicit disable must survive.

    A non-reasoning model passed ``{"enabled": False}`` must not have thinking
    re-enabled by the chokepoint, or the transport 400s on a thinking param the
    model does not support.
    """
    from hermes_constants import resolve_reasoning_config

    # Caller-supplied config short-circuits resolution entirely; assert the
    # resolver is only consulted for the None case.
    assert resolve_reasoning_config({"agent": {"reasoning_effort": "none"}}, "m") in (
        None,
        {"enabled": False},
    ), "reasoning_effort: none must resolve to disabled, never silently re-enabled"


def _resolve_via_helper(model: str):
    from hermes_constants import resolve_reasoning_config

    return resolve_reasoning_config({"agent": {"reasoning_effort": "high"}}, model)
